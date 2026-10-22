"""発話セグメンテーション。

VoiceSegmenter はフレーム単位のVAD判定から発話セグメントを組み立てる状態機械:
無音 min_silence_ms 継続で発話確定、max_utterance_s で強制分割（plan.md E-03）。

#27 で2つ増えた（どちらも既定では無効 = 現行と同一の挙動）:
  - start_speech_ms: 発話開始に必要な連続音声時間。VAD の誤検知1フレームで
    発話が立ち上がるのを防ぐ（Parapper B-3）
  - force_silence_ms: この長さの無音に達したら TurnBoundary を発行する。
    後段の TurnAssembler が「文法が継続と言っても、ここは無条件で切る」印に使う。
    None なら1つも発行しない

#29 でさらに2つ増えた（どちらも既定では無効 = 現行と同一の挙動）:
  - interim_silence_ms: 発話の**途中**でこの長さの無音に達したら InterimSegment を発行する。
    発話は閉じない。partial 字幕のための「息継ぎ駆動」の引き金（Parapper）。
    None なら1つも発行しない
  - utterances_opened: 発話が開いた回数のカウンタ。生徒の「発話中」インジケーターを
    ASR より先に立ち上げるために、呼び出し側が feed() の前後で差を見る。
    **イベント列には足していない** — 足すと既存の呼び出し側（テスト・ベンチ）が
    すべて新しいイベントを跨いで読む必要が出るため

フレーム判定器は差し替え可能: 本番は SileroVAD（ONNX, 32msフレーム）、
テスト・フォールバック用に EnergyVAD（RMS閾値, 100msフレーム）。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import numpy as np

if TYPE_CHECKING:
    from server.config import VadConfig


class FrameVAD(Protocol):
    frame_ms: int  # この実装が要求する判定フレーム長

    def is_speech(self, frame: np.ndarray) -> bool: ...

    def reset(self) -> None: ...


class EnergyVAD:
    """RMSエネルギーによるVAD（int16スケールの閾値）。テスト・フォールバック用。"""

    frame_ms = 100

    def __init__(self, threshold: float = 300.0) -> None:
        self.threshold = threshold

    def is_speech(self, frame: np.ndarray) -> bool:
        if frame.size == 0:
            return False
        rms = float(np.sqrt(np.mean(np.square(frame.astype(np.float64)))))
        return rms > self.threshold

    def reset(self) -> None:
        pass


class SileroVAD:
    """Silero VAD（faster-whisper 同梱の ONNX モデル）のストリーミング利用。

    512サンプル（32ms @16kHz）単位で音声確率を返す。同梱の SileroVADModel API は
    バッチ指向で呼び出しごとにRNN状態がリセットされるため、ONNXセッションを直接呼び、
    h/c 状態と直前64サンプルの文脈を自前で保持する（faster-whisper 1.2系で動作確認）。
    """

    FRAME_SAMPLES = 512
    _CONTEXT_SAMPLES = 64
    frame_ms = 32  # 512サンプル @16kHz

    def __init__(self, threshold: float = 0.5) -> None:
        from faster_whisper.vad import get_vad_model

        session = getattr(get_vad_model(), "session", None)
        if session is None:
            raise RuntimeError(
                "faster-whisper の Silero VAD 内部API（SileroVADModel.session）が"
                "見つからない。faster-whisper のバージョン変更が原因の可能性。"
                "requirements.txt のバージョン指定と本クラスの実装を確認のこと"
            )
        self._session = session
        self.threshold = threshold
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(self._CONTEXT_SAMPLES, dtype=np.float32)

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(self._CONTEXT_SAMPLES, dtype=np.float32)

    def is_speech(self, frame: np.ndarray) -> bool:
        audio = frame.astype(np.float32) / 32768.0
        if audio.size != self.FRAME_SAMPLES:  # 防御。VoiceSegmenter は常に固定長を渡す
            padded = np.zeros(self.FRAME_SAMPLES, dtype=np.float32)
            padded[: min(audio.size, self.FRAME_SAMPLES)] = audio[: self.FRAME_SAMPLES]
            audio = padded
        model_input = np.concatenate([self._context, audio])[None, :]
        out, self._h, self._c = self._session.run(
            None, {"input": model_input, "h": self._h, "c": self._c}
        )
        self._context = audio[-self._CONTEXT_SAMPLES:]
        return float(np.asarray(out).reshape(-1)[0]) > self.threshold


def build_frame_vad(vad_config: VadConfig) -> FrameVAD:
    """設定から FrameVAD 実装を作る。フレーム長は各実装の frame_ms 属性が持つ。"""
    if vad_config.engine == "silero":
        return SileroVAD(vad_config.threshold)
    if vad_config.engine == "energy":
        if vad_config.threshold <= 1.0:
            # silero用の確率閾値のまま energy に切り替えると全フレームが音声判定になる
            raise ValueError(
                f"energy VAD の threshold は int16 RMS スケール（例: 300）。"
                f"現在値 {vad_config.threshold} は silero 用の確率閾値の可能性"
            )
        return EnergyVAD(vad_config.threshold)
    raise ValueError(f"未知のVADエンジン: {vad_config.engine}")


@dataclass
class Segment:
    pcm: np.ndarray  # int16 mono
    t_start: float  # 音声先頭からの秒
    t_end: float  # 最後に音声を検出したフレームの末尾（秒）
    closed_at: float  # time.monotonic()。delay_ms 計測の起点
    forced: bool = False  # max_utterance_s による強制分割か


@dataclass
class InterimSegment:
    """発話の**途中**の音声（#29）。`Segment` と違い、この時点で発話は閉じていない。

    `interim_silence_ms` の短い無音（息継ぎ）を引き金に、そこまでの音声のコピーを持って
    発行される。後段は partial 字幕のために ASR を走らせるが、**確定字幕の経路には
    一切関与しない**（履歴・記録・翻訳のいずれにも載らない）。

    `pcm` は `Segment` と重複する音声である。同じ音声を2回 ASR に通すのが partial の
    コストそのもので、#28 で固定費が 1.088s → 0.027s になったから成立している。
    """

    pcm: np.ndarray  # int16 mono。発話開始からこのフレームまでのコピー
    t_start: float  # 音声先頭からの秒
    t_end: float  # 最後に音声を検出したフレームの末尾（秒）
    at: float  # time.monotonic()。first partial latency の起点


@dataclass
class TurnBoundary:
    """force_silence_ms 以上の無音を観測した印（#27）。音声は持たない。

    Segment とは別に、同じ順序で流す。Segment を min_silence_ms（morph では 320ms）で
    即座に出して ASR を始めつつ、「その後も無音が続いたか」を後追いで伝えるための印。
    force_silence_ms の到達を待ってから Segment を出すと、短くした意味が消える。
    """

    at: float  # 音声先頭からの秒（無音がしきい値に達した時刻）
    closed_at: float  # time.monotonic()


SegmenterEvent = Segment | TurnBoundary | InterimSegment


class VoiceSegmenter:
    def __init__(
        self,
        vad: FrameVAD,
        *,
        sample_rate: int = 16000,
        min_silence_ms: int = 500,
        max_utterance_s: int = 30,
        frame_ms: int = 100,
        pre_roll_ms: int = 240,
        start_speech_ms: int = 0,
        force_silence_ms: int | None = None,
        interim_silence_ms: int | None = None,
    ) -> None:
        if force_silence_ms is not None and force_silence_ms < min_silence_ms:
            raise ValueError(
                f"force_silence_ms({force_silence_ms}) は min_silence_ms({min_silence_ms}) "
                "以上にすること（Segment より先に Turn が確定してしまう）"
            )
        self._vad = vad
        self._sample_rate = sample_rate
        self._frame_len = sample_rate * frame_ms // 1000
        # 切り上げ: フレーム長で割り切れない場合も「min_silence_ms 以上の無音」を保証する
        self._silence_frames_to_close = max(1, -(-min_silence_ms // frame_ms))
        # 発話開始に必要な連続音声フレーム数（0/未満は1フレーム = 現行の挙動）
        self._speech_frames_to_open = max(1, -(-start_speech_ms // frame_ms))
        self._force_silence_frames = (
            None if force_silence_ms is None else max(1, -(-force_silence_ms // frame_ms))
        )
        # 発話の途中で InterimSegment を出す無音フレーム数（#29）。None で1つも出さない。
        # 切り上げなので「interim_silence_ms 以上の無音」を保証する
        self._interim_silence_frames = (
            None if interim_silence_ms is None else max(1, -(-interim_silence_ms // frame_ms))
        )
        # フレーム粒度で比較する。ms では違っても、切り上げで同じフレーム数に潰れることがある
        # （frame_ms=100 なら 96ms も 100ms も 1フレーム）。潰れた状態で有効にすると、
        # 同じフレームで interim と Segment 確定が同時に起き、interim が無意味になる
        if (
            self._interim_silence_frames is not None
            and self._interim_silence_frames >= self._silence_frames_to_close
        ):
            raise ValueError(
                f"interim_silence_ms({interim_silence_ms}) は min_silence_ms({min_silence_ms}) "
                f"より**フレーム単位で**短くすること（frame_ms={frame_ms} では "
                f"{self._interim_silence_frames} / {self._silence_frames_to_close} フレームで"
                "同じか長くなっており、Segment が先に閉じるので interim を出す意味がない）"
            )
        self._max_samples = max_utterance_s * sample_rate
        self._pending = np.zeros(0, dtype=np.int16)  # フレーム長未満の端数
        self._offset = 0  # 音声先頭からの処理済みサンプル数
        self.last_speech_at: float | None = None  # 最後に音声を検出した時刻（無音警告 E-01 用）
        self.utterances_opened = 0  # 発話が開いた回数（#29 の「発話中」インジケーター用）
        self._frames: list[np.ndarray] = []
        self._utt_start_sample = 0
        self._last_speech_end_sample = 0
        self._silence_run = 0
        self._speech_run = 0  # 発話開始待ちの連続音声フレーム数
        # 発話終了後、TurnBoundary をまだ出していない間だけ数える無音フレーム
        self._post_silence_run: int | None = None
        # プリロール: 発話開始判定の直前の音声を発話に含める（語頭の欠けを防ぐ。
        # VAD判定はフレーム粒度なので、開始フレームだけだと立ち上がりの子音が削れる）。
        # start_speech_ms ぶんのフレームも「開始と判定される前」なのでここに溜まる。
        # その数を足しておかないと、開始待ちのぶんだけプリロールが食われて語頭が削れる
        self._pre_roll: deque[np.ndarray] = deque(
            maxlen=max(0, pre_roll_ms // frame_ms) + self._speech_frames_to_open
        )

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def is_open(self) -> bool:
        """発話が進行中か（#29）。生徒の「発話中」インジケーターの再評価に使う。"""
        return bool(self._frames)

    def feed(self, pcm: np.ndarray) -> list[SegmenterEvent]:
        """音声を食わせ、確定した Segment と TurnBoundary を発生順に返す。

        force_silence_ms が None のときは TurnBoundary を1つも返さない
        （= #27 以前と同じく Segment だけが流れる）。
        """
        events: list[SegmenterEvent] = []
        buf = np.concatenate([self._pending, pcm]) if self._pending.size else pcm
        n_frames = buf.size // self._frame_len
        for i in range(n_frames):
            frame = buf[i * self._frame_len : (i + 1) * self._frame_len]
            events.extend(self._process_frame(frame))
        self._pending = np.array(buf[n_frames * self._frame_len :], dtype=np.int16)
        return events

    def flush(self) -> Segment | None:
        """進行中の発話を強制確定する（一時停止・終了時に呼ぶ）。"""
        self._pending = np.zeros(0, dtype=np.int16)
        if not self._frames:
            return None
        segment = self._close(forced=False)
        self._post_silence_run = None  # flush は無音待ちではないので印は出さない
        return segment

    def reset(self) -> None:
        """構築直後の状態へ戻す（新しい音声ストリームを流し始めるとき）。

        t_start/t_end は「音声先頭からの秒」なので、ストリームの先頭が変わる以上
        _offset も 0 に戻す。戻し忘れると次のストリームの時刻が前のぶんだけずれる。
        """
        self._pending = np.zeros(0, dtype=np.int16)
        self._pre_roll.clear()
        self._vad.reset()
        self._offset = 0
        self.last_speech_at = None
        self._post_silence_run = None
        self._reset_utterance()

    def _reset_utterance(self) -> None:
        self._frames = []
        self._utt_start_sample = 0
        self._last_speech_end_sample = 0
        self._silence_run = 0
        self._speech_run = 0

    def _process_frame(self, frame: np.ndarray) -> list[SegmenterEvent]:
        self._offset += frame.size
        speech = self._vad.is_speech(frame)
        if speech:
            self.last_speech_at = time.monotonic()

        if not self._frames:
            return self._process_idle_frame(frame, speech)

        events: list[SegmenterEvent] = []
        self._frames.append(frame)
        if speech:
            self._silence_run = 0
            self._last_speech_end_sample = self._offset
        else:
            self._silence_run += 1
            if self._silence_run >= self._silence_frames_to_close:
                return [self._close(forced=False)]
            interim = self._maybe_interim()
            if interim is not None:
                events.append(interim)
        segment = self._maybe_force_close()
        if segment is not None:
            events.append(segment)
        return events

    def _process_idle_frame(self, frame: np.ndarray, speech: bool) -> list[SegmenterEvent]:
        """発話が開いていないときのフレーム。開始判定と TurnBoundary をここで見る。"""
        # 音声・無音を問わず溜める。start_speech_ms の判定待ちのフレームも発話に含める
        self._pre_roll.append(frame)
        if not speech:
            self._speech_run = 0
            return self._tick_post_silence()

        self._post_silence_run = None  # 発話が再開したので無条件確定は起きない
        self._speech_run += 1
        if self._speech_run < self._speech_frames_to_open:
            return []  # まだ発話とみなさない（VAD の誤検知1枚で立ち上げない）

        opening = list(self._pre_roll)
        self._pre_roll.clear()
        self._utt_start_sample = self._offset - sum(f.size for f in opening)
        self._frames = opening
        self._last_speech_end_sample = self._offset
        self._silence_run = 0
        self._speech_run = 0
        # 発話が開いた回数（#29）。呼び出し側は feed() の前後の差で
        # 「この呼び出しの中で発話が始まったか」を判定する。開いてすぐ閉じた場合も
        # 数えられる（is_open の前後比較では取りこぼす）
        self.utterances_opened += 1
        segment = self._maybe_force_close()
        return [] if segment is None else [segment]

    def _tick_post_silence(self) -> list[SegmenterEvent]:
        """発話終了後に続く無音を数え、force_silence_ms に達したら1回だけ印を出す。"""
        if self._force_silence_frames is None or self._post_silence_run is None:
            return []
        self._post_silence_run += 1
        if self._post_silence_run < self._force_silence_frames:
            return []
        self._post_silence_run = None  # 1つの無音区間につき1回だけ
        return [TurnBoundary(at=self._offset / self._sample_rate, closed_at=time.monotonic())]

    def _maybe_interim(self) -> InterimSegment | None:
        """発話の途中の無音が interim_silence_ms に達した最初のフレームで1回だけ出す（#29）。

        **`==` で見るのが要点**。`>=` にすると息継ぎが長引くあいだ毎フレーム発行され、
        「partial の回数が発話のリズムに比例する」という Parapper 方式の前提が崩れる
        （タイマー駆動と変わらなくなり CPU 予算が読めなくなる）。
        """
        if self._silence_run != self._interim_silence_frames:
            return None
        return InterimSegment(
            pcm=np.concatenate(self._frames),
            t_start=self._utt_start_sample / self._sample_rate,
            t_end=self._last_speech_end_sample / self._sample_rate,
            at=time.monotonic(),
        )

    def _maybe_force_close(self) -> Segment | None:
        if self._offset - self._utt_start_sample >= self._max_samples:
            return self._close(forced=True)
        return None

    def _close(self, forced: bool) -> Segment:
        segment = Segment(
            pcm=np.concatenate(self._frames),
            t_start=self._utt_start_sample / self._sample_rate,
            t_end=self._last_speech_end_sample / self._sample_rate,
            closed_at=time.monotonic(),
            forced=forced,
        )
        # 直前まで数えていた無音を引き継ぐ（無音で閉じた場合は
        # すでに _silence_frames_to_close ぶん経っている）
        self._post_silence_run = self._silence_run
        self._reset_utterance()
        return segment
