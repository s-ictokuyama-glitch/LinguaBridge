"""Segment の ASR 結果を Turn（字幕カード1枚）へ束ねる状態機械（#27 / Parapper R-1）。

`Segment` = ASR に投げる音声1単位、`Turn` = 複数 Segment を束ねた発話。
これまで `Utterance` が両者を同一視していたため、無音長だけが区切りの根拠だった。

**タイマーを持たない。** 保留中の Turn が進む契機は次の2つだけで、
どちらも上流（VAD → ASR）から順序どおり届く:

  - 次の Segment の ASR 結果が来た（= 無音が force_silence_ms 未満だった）
  - `TurnBoundary` が来た（= 無音が force_silence_ms に達した）

これにより挙動が入力列だけで決まり、テストが決定的になる。

戦略:
  `simple` … Segment 1つ = Turn 1つ。**現行と完全に同一の挙動**（既定）
  `morph`  … 末尾の文法クラス（`boundary.py`）で確定／継続を決める
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from server.turn.boundary import BoundaryClass, BoundaryClassifier

SIMPLE = "simple"
MORPH = "morph"
STRATEGIES = (SIMPLE, MORPH)


@dataclass
class TurnPart:
    """Turn を構成する Segment 1つぶんの ASR 結果。"""

    text: str
    t_start: float
    t_end: float
    asr_ms: int
    closed_at: float  # time.monotonic()。delay_ms 計測の起点
    audio_s: float


@dataclass
class Turn:
    """確定した発話。字幕カード1枚に対応する。"""

    text: str
    t_start: float
    t_end: float
    asr_ms: int  # 構成 Segment の合計（1発話にかかった ASR 時間）
    closed_at: float  # 最後の Segment の確定時刻。ここから配信までが delay_ms
    parts: int  # 連結した Segment 数（1 なら連結なし）
    reason: str  # "strong_end" | "predicate_end" | "boundary" | "max_turn" | "flush"
    audio_s: float = 0.0
    # この Turn の識別子（#29）。`seq` と違い**確定前から**存在し、partial の宛先になる。
    # 先生UIは同じ turn_id の asr_final が来た時点で partial 行を差し替える
    turn_id: int = 0

    @property
    def merged(self) -> bool:
        return self.parts > 1


@dataclass
class _Pending:
    parts: list[TurnPart] = field(default_factory=list)

    def add(self, part: TurnPart) -> None:
        self.parts.append(part)

    @property
    def empty(self) -> bool:
        return not self.parts

    def span_s(self) -> float:
        if not self.parts:
            return 0.0
        return self.parts[-1].t_end - self.parts[0].t_start

    def build(self, reason: str, turn_id: int) -> Turn:
        # 日本語なので区切り文字は入れずに素直に連結する。末尾の読点は残す
        # （訳文側の手がかりになるうえ、消すと元テキストが復元できなくなる）
        return Turn(
            text="".join(p.text for p in self.parts),
            t_start=self.parts[0].t_start,
            t_end=self.parts[-1].t_end,
            asr_ms=sum(p.asr_ms for p in self.parts),
            closed_at=self.parts[-1].closed_at,
            parts=len(self.parts),
            reason=reason,
            audio_s=sum(p.audio_s for p in self.parts),
            turn_id=turn_id,
        )


class TurnAssembler:
    def __init__(
        self,
        classifier: BoundaryClassifier,
        *,
        strategy: str = SIMPLE,
        max_turn_s: float = 30.0,
        max_segments: int = 12,
    ) -> None:
        if strategy not in STRATEGIES:
            raise ValueError(f"未知の turn 戦略: {strategy}（{'/'.join(STRATEGIES)}）")
        self._classifier = classifier
        self._strategy = strategy
        self._max_turn_s = max_turn_s
        self._max_segments = max_segments
        self._pending = _Pending()
        self.last_class: BoundaryClass | None = None  # 直近の分類（ベンチ・デバッグ用）
        # turn_id は Turn の識別子（#29）。`seq` と違い**確定前**に採番される必要がある
        # ——partial は Turn が確定する前に先生へ届くので、宛先の identity が先に要る。
        # 保留が空のあいだは None で、最初の Segment か最初の interim のどちらか
        # 先に来た方で採番する
        self._turn_ids = itertools.count(1)
        self._turn_id: int | None = None
        self._revision = 0  # 現 turn 内で送出した partial の版番号

    @property
    def strategy(self) -> str:
        return self._strategy

    @property
    def has_pending(self) -> bool:
        return not self._pending.empty

    @property
    def current_turn_id(self) -> int | None:
        """進行中の Turn の識別子（#29）。確定・打ち切りで None に戻る。

        interim ASR の **stale 検出**に使う（Parapper R-7）: ASR を呼ぶ前の値と
        返ってきた後の値が違えば、その partial は既に確定した古い Turn のもの。
        """
        return self._turn_id

    def reserve_turn_id(self) -> int:
        """進行中の Turn の識別子を返す。無ければ採番する（#29）。

        **ASR を呼ぶ前**に呼ぶこと。partial の宛先をこの時点で固定しておかないと、
        推論中に Turn が確定した場合に「どの Turn のものだったか」が判定できない。
        """
        if self._turn_id is None:
            self._turn_id = next(self._turn_ids)
            self._revision = 0
        return self._turn_id

    def preview(self, interim_text: str) -> tuple[int, int, str]:
        """partial 用の表示テキストを作る（#29）。`(turn_id, revision, text)`。

        **保留中の Segment の連結 + interim_text** を返すのが要点。morph 戦略で
        複数 Segment を跨いでいる途中でも、先生には Turn の全文が見える
        （interim だけを出すと、確定した瞬間に文が前に伸びて読み直しになる）。

        状態を変えるのは revision の採番だけで、保留中の Segment には触らない。
        """
        turn_id = self.reserve_turn_id()
        self._revision += 1
        return turn_id, self._revision, "".join(p.text for p in self._pending.parts) + interim_text

    def discard_reserved(self) -> None:
        """保留中の Segment が無いまま Turn を畳む（#29）。

        partial だけ出て Segment が1つも来なかったとき（幻覚フィルタで捨てられた等）に
        呼ぶ。呼ばないと turn_id が居座り、次の発話の partial が古い Turn の
        続きとして先生UIに追記されてしまう。
        """
        if self._pending.empty:
            self._turn_id = None
            self._revision = 0

    def add_segment(self, part: TurnPart) -> Turn | None:
        """Segment の ASR 結果を投入する。Turn が確定したら返す。"""
        # Turn が開いた時点で採番する（#29）。`current_turn_id` が「Turn が開いている」と
        # 同値になり、interim の stale 検出がこの1つの値だけで決まる
        self.reserve_turn_id()
        self._pending.add(part)

        if self._strategy == SIMPLE:
            # 現行と同一: Segment 1つがそのまま Turn 1つ
            self.last_class = None
            return self._flush("segment")

        cls = self._classifier.classify(part.text)
        self.last_class = cls
        if cls.ends_turn:
            return self._flush(cls.value)

        # 継続クラス。連結が暴走しないよう上限で強制確定する
        if (
            len(self._pending.parts) >= self._max_segments
            or self._pending.span_s() >= self._max_turn_s
        ):
            return self._flush("max_turn")
        return None

    def on_boundary(self) -> Turn | None:
        """VAD が force_silence_ms 以上の無音を観測した。保留中があれば確定する。"""
        if self._pending.empty:
            self.discard_reserved()  # partial だけ出て Segment が来なかった場合（#29）
            return None
        return self._flush("boundary")

    def flush(self) -> Turn | None:
        """一時停止・終了で打ち切る。"""
        if self._pending.empty:
            self.discard_reserved()  # partial だけ出て Segment が来なかった場合（#29）
            return None
        return self._flush("flush")

    def reset(self) -> None:
        self._pending = _Pending()
        self.last_class = None
        self._turn_id = None
        self._revision = 0

    def _flush(self, reason: str) -> Turn:
        turn = self._pending.build(reason, self.reserve_turn_id())
        self._pending = _Pending()
        self._turn_id = None
        self._revision = 0
        return turn
