"""オーケストレーター（plan.md §6.3）。

teacher WS → ingest → VoiceSegmenter → asr_queue → ASRワーカー(1スレッド)
  → TurnAssembler（文法境界で Segment を Turn へ連結。#27）
  → mt_queue（アクティブ言語ごとにジョブ展開） → MTワーカー(1スレッド)
  → 言語別ブロードキャスト / 先生へ asr_final

`Utterance` は **Turn**（字幕カード1枚）を表す。asr_queue を流れる `Segment` は
ASR に投げる音声1単位で、strategy=morph では複数の Segment が1つの Utterance になる
（strategy=simple では 1 Segment = 1 Utterance ＝ #27 以前と同一）。

ASR・MTは各1スレッドの ThreadPoolExecutor で直列実行する
（実エンジンの CTranslate2 / llama.cpp が内部でマルチスレッド推論するため）。
発話はスキップしない（キュー滞留時の警告表示は #15）。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import statistics
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from server import ws_protocol as proto
from server.asr.base import ASREngine
from server.asr.hallucination import hallucination_reason
from server.audio.ingest import pcm16_from_bytes
from server.audio.vad import (
    InterimSegment,
    Segment,
    SpeechActivity,
    TurnBoundary,
    VoiceSegmenter,
    build_frame_vad,
)
from server.asr_backlog import AsrBacklog, AsrEvent, Dropped, FlushTurn
from server.config import AppConfig
from server.caption_history import CaptionHistory, Translation, Utterance
from server.inference import InferenceFailed, InferenceRunner, InferenceSucceeded, InferenceTimedOut
from server.mt.base import TranslationEngine
from server.model_files import TranslationModelError
from server.mt.cache import CacheKey, TranslationCache
from server.roster import Client, Roster
from server.speaking import SpeakingSignal
from server.turn import TurnAssembler, build_boundary_classifier
from server.turn.assembler import Turn, TurnPart

logger = logging.getLogger(__name__)


@dataclass
class MTJob:
    utterance: Utterance
    lang: str
    closed_at: float  # 発話確定時刻（monotonic）。delay_ms の起点
    recipient_ids: tuple[str, ...] = ()
    replay: bool = False


@dataclass
class HistoryRefresh:
    lang: str
    utterances: deque[Utterance]


# MTキューの優先度: ライブ字幕（N-01の遅延基準の対象）が
# 再接続復元ジョブ（最大K=50件）に停滞させられないようにする
_LIVE_PRIORITY = 0
_REPLAY_PRIORITY = 1

# 遅延中央値の算出に使う直近captionの件数（stats用）
_DELAY_SAMPLE_SIZE = 20


def _median_ms(samples: deque[int]) -> int:
    """統計用の中央値（ミリ秒）。サンプルが無ければ 0 = 「まだ材料が無い」。"""
    return int(statistics.median(samples)) if samples else 0


# 同じ過負荷通知を先生へ送る最短間隔（秒）。溢れている間は毎件発生しうるので、
# 通知そのものが新しい過負荷にならないようにする
_NOTICE_COOLDOWN_S = 10.0

# 先生の話す言語。現状は日本語固定だが、キャッシュキーには明示的に含める（#26 B-4）
_SOURCE_LANG = "ja"


def _live_task_count() -> int:
    """生存 asyncio タスク数（#32 のタスクリーク検出）。

    `stats_snapshot()` は同期メソッドで、イベントループの外からも呼ばれる
    （ユニットテスト・将来の同期エンドポイント）。**stats は決して例外を
    投げてはならない**ので、ループが無ければ 0 を返す（見えない＝増えていない）。
    """
    try:
        return len(asyncio.all_tasks())
    except RuntimeError:
        return 0


class Pipeline:
    def __init__(
        self,
        roster: Roster,
        config: AppConfig,
        asr_engine: ASREngine,
        mt_engine: TranslationEngine,
        *,
        asr_runner: InferenceRunner | None = None,
        mt_runner: InferenceRunner | None = None,
    ) -> None:
        self.roster = roster
        self.history = CaptionHistory(config.history_resend)
        self._accepting_audio = False
        self.on_utterance: Callable[[Utterance], None] | None = None
        self._asr = asr_engine
        self._mt = mt_engine
        self._mt_engine_name = config.mt.engine
        # 発話をまたぐ翻訳キャッシュ（#26 B-4）。「はい」「もう一度言います」のような
        # 繰り返しで Hy-MT2 を叩かない。**有界**（config で 0 にすると無効）
        self._mt_cache = TranslationCache(config.mt.cache_size)
        # モデル版はキーの一部。設定を変えたあとに古い設定の訳が返るのを防ぐ
        self._mt_model_version = mt_engine.model_version
        frame_vad = build_frame_vad(config.vad)
        # partial 字幕（#29）。無効なら interim_silence_ms=None を渡すので、
        # VoiceSegmenter は InterimSegment を1つも出さない ＝ #29 以前と同一の挙動
        self._partial = config.partial
        # 分割パラメータは turn 戦略がまとめて決める（#27）。simple なら現行と同一
        self._segmenter = VoiceSegmenter(
            frame_vad,
            max_utterance_s=config.vad.max_utterance_s,
            frame_ms=frame_vad.frame_ms,
            pre_roll_ms=config.vad.pre_roll_ms,
            interim_silence_ms=(
                config.partial.interim_silence_ms if config.partial.enabled else None
            ),
            **config.turn.segmenter_kwargs(config.vad),
        )
        self._speaking_signal = SpeakingSignal(config.partial.speaking_indicator)
        # 計測用（#29 の判定材料）。partial の実効回数と stale で捨てた回数
        self.partials_sent = 0
        self.partials_stale = 0
        self._batch_interims_skipped = 0
        # 通算の推論回数（#32）。「生徒が増えても ASR は増えない」「同一言語の人数で
        # Hy-MT2 は増えない」を実エンジンの実負荷で数値にするための計数器。
        # `_asr_ms_samples` / `_mt_ms_samples` は deque(maxlen=) の**標本**なので
        # 通算回数には使えない（中央値と回数は別物）
        self.asr_calls = 0  # 確定 Segment の ASR 推論回数（partial は含めない）
        self.asr_partial_calls = 0  # interim（partial）の ASR 推論回数
        self.mt_calls = 0  # **実際に推論した**翻訳の回数（キャッシュヒットは含めない）
        # 上限超過で受け取らなかった音声フレーム数（`limits.max_audio_bytes`）。
        # 破棄地点はここと `_enqueue_asr` の2つで、意味が違う:
        # ここは**不正な入力**（100msフレームのはずが巨大）、あちらは**容量**。
        # 送信キュー溢れ（`delivery.py`）は破棄ではなく切断なので、
        # 長時間試験では disconnects の側に出る
        self.audio_frames_rejected = 0
        # Segment を Turn へ束ねる（#27）。ASR の**後段**に置く。文法クラスの判定に
        # ASR テキストが要るので、VAD の中では決められない
        self._assembler = TurnAssembler(
            build_boundary_classifier(config.turn.classifier),
            strategy=config.turn.strategy,
            max_turn_s=config.turn.morph.max_turn_s,
            max_segments=config.turn.morph.max_segments,
        )
        self._limits = config.limits
        self._asr_backlog = AsrBacklog(
            config.limits.asr_queue_seconds, self._segmenter.sample_rate,
            config.partial.min_interim_audio_s,
        )
        # (優先度, 連番, ジョブ)。連番は同一優先度内のFIFOを保証する
        self._mt_queue: asyncio.PriorityQueue[tuple[int, int, MTJob]] = asyncio.PriorityQueue(
            maxsize=config.limits.mt_queue_max
        )
        self._mt_counter = itertools.count()
        self._mt_work = asyncio.Event()
        self._mt_idle = asyncio.Event()
        self._mt_idle.set()
        self._history_refreshes: dict[str, HistoryRefresh] = {}
        self._caption_receipt_limit = 2 * config.history_resend
        self._notice_at: dict[str, float] = {}  # 過負荷通知のクールダウン（code -> 最終送信時刻）
        self._asr_runner = asr_runner if asr_runner is not None else InferenceRunner(name="asr")
        self._mt_runner = mt_runner if mt_runner is not None else InferenceRunner(name="mt")
        self._tasks: list[asyncio.Task[None]] = []
        self._warmup_task: asyncio.Task[None] | None = None
        self.ready = False  # モデルの事前ロード完了（/ready が 200 を返す条件）
        # モニタリング（#15）: 統計の定期配信と無音警告（E-01）
        self._stats_interval_s = config.monitoring.stats_interval_s
        self._silence_warning_s = config.monitoring.silence_warning_s
        self._overload_queue_depth = config.monitoring.overload_queue_depth
        self._overload_audio_s = config.monitoring.overload_audio_seconds
        self._delay_samples: deque[int] = deque(maxlen=_DELAY_SAMPLE_SIZE)
        # 遅延の内訳（#30）。`_delay_samples`（closed_at から生徒に届くまでの総遅延）を
        # 段ごとに割るための材料。どちらも「1件あたりの推論にかかった時間」であって
        # 待ち時間は含まない（待ちは秒数・件数の側で表す）
        self._asr_ms_samples: deque[int] = deque(maxlen=_DELAY_SAMPLE_SIZE)
        self._mt_ms_samples: deque[int] = deque(maxlen=_DELAY_SAMPLE_SIZE)
        self._live_since: float | None = None  # 無音計測の基準（live遷移でリセット）
        self._mic_silent_warned = False

    async def start(self) -> None:
        # ワーカーは即座に起動し、サーバーをすぐ応答可能にする（/ready 503 の窓を作る）。
        # モデルの warmup は背後で走らせ、完了で ready を立てる（/ready は #16 で 503→200）。
        # ワーカーは同一の単一スレッドExecutorを使うため warmup と実推論は直列化される。
        self._tasks = [
            asyncio.create_task(self._asr_worker(), name="asr-worker"),
            asyncio.create_task(self._mt_worker(), name="mt-worker"),
            asyncio.create_task(self._stats_worker(), name="stats-worker"),
        ]
        self._warmup_task = asyncio.create_task(self._run_warmup(), name="warmup")

    async def _run_warmup(self) -> None:
        for runner, warmup in (
            (self._asr_runner, self._asr.warmup),
            (self._mt_runner, self._mt.warmup),
        ):
            outcome = await runner.run(warmup, timeout=None)
            if isinstance(outcome, InferenceSucceeded):
                continue
            if isinstance(outcome, InferenceFailed):
                if isinstance(outcome.error, TranslationModelError):
                    logger.error("%s。/ready は 503 のままになります", outcome.error)
                else:
                    logger.error(
                        "モデルの事前ロードに失敗。/ready は 503 のままになります",
                        exc_info=outcome.error,
                    )
            return
        self.ready = True

    async def stop(self) -> None:
        if self._partial.enabled:
            # partial の実効値をログに残す（#29）。実授業では合成音源と息継ぎの
            # 分布が変わるので、`docs/bench/2026-08-24-partial.md` の数値がそのまま
            # 当てはまるとは限らない。現場で割に合っているかはこの3つで判断する
            logger.info(
                "partial: 送出 %d 件 / 背圧で見送り %d 件 / 確定後に届いて破棄 %d 件",
                self.partials_sent,
                self.interims_skipped,
                self.partials_stale,
            )
        if self._warmup_task is not None:
            self._warmup_task.cancel()
        for task in self._tasks:
            task.cancel()
        pending: list[asyncio.Task[None]] = [
            *self._tasks, *([self._warmup_task] if self._warmup_task else [])
        ]
        await asyncio.gather(*pending, return_exceptions=True)
        self._tasks = []
        self._warmup_task = None
        self._history_refreshes.clear()
        await self.roster.aclose()
        self._asr_runner.stop()
        self._mt_runner.stop()

    @property
    def queue_depth(self) -> int:
        return self.asr_queue_depth + self.mt_queue_depth

    @property
    def audio_queue_seconds(self) -> float:
        return self._asr_backlog.total_seconds

    @property
    def asr_active_seconds(self) -> float:
        """いま ASR が処理しているセグメントの長さ（#30）。処理中でなければ 0。"""
        return self._asr_backlog.active_seconds

    @property
    def asr_wait_seconds(self) -> float:
        """ASR に入るのを待っている音声の長さ（#30）。

        `audio_queue_seconds` から処理中のぶんを引いたもの。**先生が見るべきはこちら**で、
        こちらが 0 なら遅れているのは「1発話が長いから」であって滞留ではない。
        """
        return self._asr_backlog.waiting_seconds

    @property
    def speaking(self) -> bool:
        """先生が発話中か（#29）。参加時のスナップショットに使う。"""
        return self._speaking_signal.current

    @property
    def asr_dropped_segments(self) -> int:
        return self._asr_backlog.dropped_segments

    @property
    def asr_dropped_seconds(self) -> float:
        return self._asr_backlog.dropped_seconds

    @property
    def interims_skipped(self) -> int:
        return self._asr_backlog.interims_skipped + self._batch_interims_skipped

    @property
    def asr_queue_depth(self) -> int:
        return self._asr_backlog.depth

    @property
    def mt_queue_depth(self) -> int:
        return self._mt_queue.qsize()

    async def drain(self, timeout: float | None = 30) -> None:
        """ASR、MT の順に排出する。timeout は各段の待機秒数。"""
        await asyncio.wait_for(self._asr_backlog.join(), timeout=timeout)
        await asyncio.wait_for(self._mt_idle.wait(), timeout=timeout)

    async def _enqueue_asr(self, event: AsrEvent) -> None:
        outcome = self._asr_backlog.offer(event)
        if outcome is Dropped and isinstance(event, Segment):
            seconds = event.pcm.size / self._segmenter.sample_rate
            logger.warning("ASR待ちが上限に達したため %.2f 秒の音声を破棄しました", seconds)
            await self._notify_overload(
                "audio_dropped",
                "処理が追いつかず音声を一部破棄しました。ゆっくり話すか一度一時停止してください",
            )

    async def _broadcast_speaking(self, changed: bool | None) -> None:
        if changed is not None:
            await self._broadcast_all(proto.Speaking(on=changed).model_dump())

    async def _refresh_speaking(self) -> None:
        await self._broadcast_speaking(self._speaking_signal.on_asr_settled(
            self._segmenter.is_open, self._assembler.has_pending,
        ))

    # ---- 音声入力（先生WSハンドラから呼ばれる） ----

    async def feed_audio(self, data: bytes) -> None:
        if not self._accepting_audio:
            return  # 一時停止・終了中の音声は破棄
        events = self._segmenter.feed(pcm16_from_bytes(data))
        for i, event in enumerate(events):
            if isinstance(event, SpeechActivity):
                await self._broadcast_speaking(self._speaking_signal.on_vad_activity())
                continue
            # 同じバッチで後ろに Segment が控えているなら interim は積まない（#29）。
            # max_utterance_s の強制分割と息継ぎが同じフレームに重なるとこうなるが、
            # その interim は確定 Segment と同じ音声を先に ASR へ通すだけで、
            # **確定字幕を1回ぶん遅らせる**。partial の目的と正反対になる
            if isinstance(event, InterimSegment) and any(
                isinstance(later, Segment) for later in events[i + 1 :]
            ):
                self._batch_interims_skipped += 1
                continue
            await self._enqueue_asr(event)

    async def flush_audio(self) -> None:
        """進行中の発話を確定して処理に回す（一時停止・終了時）。

        保留中の Turn も打ち切る。ここで打ち切らないと、一時停止の直前に
        「継続」と判定された発話が次の再開まで字幕に出ない。
        """
        segment = self._segmenter.flush()
        if segment is not None:
            await self._enqueue_asr(segment)
        # ASR待ちの後ろに並べる（順序を保つため、直接 assembler を叩かない）
        await self._enqueue_asr(FlushTurn(closed_at=time.monotonic()))
        # 一時停止・終了で発話は必ず途切れる。ASR待ちの排出を待たずに畳む（#29）
        await self._broadcast_speaking(self._speaking_signal.on_mic_silent())

    # ---- ワーカー ----

    async def _asr_worker(self) -> None:
        while True:
            try:
                async with self._asr_backlog.next() as event:
                    if isinstance(event, Segment):
                        await self._process_segment(event)
                    elif isinstance(event, InterimSegment):
                        await self._process_interim(event)
                    else:
                        await self._close_pending_turn(event)
            finally:
                await self._refresh_speaking()

    async def _close_pending_turn(self, event: TurnBoundary | FlushTurn) -> None:
        """無音が force_silence_ms に達した / flush された。保留中の Turn を確定する。"""
        turn = (
            self._assembler.flush()
            if isinstance(event, FlushTurn)
            else self._assembler.on_boundary()
        )
        if turn is not None:
            await self._publish_turn(turn, closed_at=event.closed_at)

    async def _process_interim(self, interim: InterimSegment) -> None:
        """発話の途中の音声を ASR にかけ、partial を**先生にだけ**送る（#29）。

        確定字幕の経路には一切関与しない: 履歴にも記録にも翻訳にも載らず、
        `seq` を消費せず、`TurnAssembler` の保留中 Segment にも触らない。

        **stale 検出**（Parapper R-7）: ASR を呼ぶ前に turn_id を固定し、返ってきた
        時点で進行中の Turn が変わっていたら捨てる。推論中に Turn が確定すると、
        その partial は「もう確定した文の途中経過」なので、出すと先生の画面で
        確定済みの文が巻き戻る。
        """
        turn_id = self._assembler.reserve_turn_id()
        # 呼んだ時点で数える（#32）。stale で捨てても推論費用は払っている
        self.asr_partial_calls += 1
        outcome = await self._asr_runner.run(
            partial(self._asr.transcribe, interim.pcm, self._segmenter.sample_rate),
            timeout=self._limits.asr_timeout_s,
        )
        if isinstance(outcome, InferenceTimedOut):
            logger.warning("interim ASR がタイムアウトしました（partial のみ・確定字幕に影響なし）")
            return
        if isinstance(outcome, InferenceFailed):
            logger.warning(
                "interim ASR に失敗しました（partial のみ・確定字幕に影響なし）",
                exc_info=outcome.error,
            )
            return
        result = outcome.value
        if self._assembler.current_turn_id != turn_id:
            self.partials_stale += 1
            return
        if hallucination_reason(result) is not None:
            return  # 雑音の幻覚を先生に見せない。確定側と同じ基準で弾く
        if not result.text.strip():
            return
        _, revision, text = self._assembler.preview(result.text)
        self.partials_sent += 1
        await self.send_to_teacher(proto.TurnPartial(turn_id=turn_id, revision=revision, ja=text))

    async def _process_segment(self, segment: Segment) -> None:
        # 呼んだ時点で数える（#32）。タイムアウトしても幻覚で捨てても推論費用は
        # 払っているので、「何回 ASR を叩いたか」はここが正しい数え場所になる
        self.asr_calls += 1
        outcome = await self._asr_runner.run(
            partial(self._asr.transcribe, segment.pcm, self._segmenter.sample_rate),
            timeout=self._limits.asr_timeout_s,
        )
        if isinstance(outcome, InferenceTimedOut):
            logger.error(
                "ASRが %.0f 秒で返らないため、この発話を諦めます（スレッドは実行中のまま）",
                self._limits.asr_timeout_s,
            )
            await self._notify_overload("asr_timeout", "音声認識が応答しません。処理を1件飛ばしました")
            return
        if isinstance(outcome, InferenceFailed):
            logger.error("ASR failed; utterance dropped", exc_info=outcome.error)
            return
        result = outcome.value
        asr_ms = outcome.elapsed_ms
        # 遅延の内訳（#30）。幻覚で捨てる発話も推論費用は払っているので、
        # 破棄判定より前に数える（先生に見せるのは「ASRに何ms かかる機械か」）。
        # interim（partial）はここを通らない＝中央値に混ざらない
        self._asr_ms_samples.append(asr_ms)
        reason = hallucination_reason(result)
        if reason is not None:
            # 幻覚は Segment 単位で捨てる。保留中の Turn は壊さない
            # （雑音1枚を挟んだだけで前後の発話が分断されないように）
            logger.info("発話を破棄（幻覚フィルタ: %s）", reason)
            return
        turn = self._assembler.add_segment(
            TurnPart(
                text=result.text,
                t_start=segment.t_start,
                t_end=segment.t_end,
                asr_ms=asr_ms,
                closed_at=segment.closed_at,
                audio_s=segment.pcm.size / self._segmenter.sample_rate,
            )
        )
        if turn is not None:
            await self._publish_turn(turn, closed_at=segment.closed_at)

    async def _publish_turn(self, turn: Turn, *, closed_at: float) -> None:
        """確定した Turn を seq採番 → 履歴 → 先生へ asr_final → 翻訳ジョブ展開。

        `closed_at` は delay_ms の起点。Turn を確定させた最後の出来事の時刻を使う
        （連結された Turn では最後の Segment、TurnBoundary ならその観測時刻）。
        """
        if not turn.text.strip():
            return  # 空文字だけの Turn は字幕にしない
        utterance = self.history.append(
            t_start=turn.t_start,
            t_end=turn.t_end,
            text_ja=turn.text,
            asr_ms=turn.asr_ms,
            segments=turn.parts,
        )
        if self.on_utterance is not None:
            self.on_utterance(utterance)
        await self.send_to_teacher(
            proto.AsrFinal(
                seq=utterance.seq,
                ja=utterance.text_ja,
                asr_ms=utterance.asr_ms,
                # 先生UIはこれで partial 行を差し替える（#29）
                turn_id=turn.turn_id,
            )
        )
        recipients: dict[str, list[str]] = {}
        for client in self.roster.students():
            if client.lang is not None:
                recipients.setdefault(client.lang, []).append(client.id)
        for lang, ids in sorted(recipients.items()):
            await self._enqueue_mt(_LIVE_PRIORITY, MTJob(utterance, lang, closed_at, tuple(ids)))

    async def _enqueue_mt(self, priority: int, job: MTJob) -> bool:
        """翻訳ジョブを積む（#25 A-1: 無制限キューの有界化）。積めたら True。

        **確定発話は捨てない**（ミッションの明示要求）ので、溢れそうなときに
        先に抑制するのは再接続復元ジョブの方にする。復元は生徒が再接続すれば
        `last_seq` からやり直せるが、ライブ字幕は失われたら戻らない。

        ライブジョブが満杯に当たったときだけ待つ。待つのはASRワーカーであって
        先生WSの受信ループではなく、背圧は `_enqueue_asr` の秒数上限まで伝わって
        そこで（通知付きで）捨てられる。
        """
        if priority == _REPLAY_PRIORITY:
            if self._mt_queue.qsize() >= self._limits.mt_replay_watermark:
                logger.info("翻訳キューが混雑しているため再接続復元ジョブを見送りました")
                return False
            self._mt_queue.put_nowait((priority, next(self._mt_counter), job))
        else:
            entry = (priority, next(self._mt_counter), job)
            try:
                self._mt_queue.put_nowait(entry)
            except asyncio.QueueFull:
                await self._notify_overload(
                    "mt_backlog",
                    "翻訳が追いつかず字幕が遅れています。少し間を置いて話してください",
                )
                await self._mt_queue.put(entry)
        if self._mt_queue.qsize() >= self._limits.mt_replay_watermark:
            self._history_refreshes.clear()
        self._mt_idle.clear()
        self._mt_work.set()
        return True

    async def _mt_worker(self) -> None:
        while True:
            await self._mt_work.wait()
            if self._mt_queue.empty():
                if self._history_refreshes:
                    await self._refresh_one_caption()
                    await asyncio.sleep(0)
                else:
                    self._mt_work.clear()
                    self._mt_idle.set()
                continue
            _, _, job = self._mt_queue.get_nowait()
            try:
                await self._process_mt_job(job)
            finally:
                self._mt_queue.task_done()

    def _cache_key(self, text_ja: str, lang: str) -> CacheKey:
        return CacheKey(
            source_text=text_ja,
            source_lang=_SOURCE_LANG,
            target_lang=lang,
            engine=self._mt_engine_name,
            model_version=self._mt_model_version,
        )

    def _translation_from_cache(self, utterance: Utterance, lang: str) -> Translation | None:
        """発話をまたぐキャッシュを引く（#26 B-4）。

        **推論スレッドへ渡す前**に引くのが要点。エンジン側をラップして
        キャッシュすると、ヒットしても単一スレッドExecutorで実行中の推論
        （1件 570〜850ms）の後ろに並んでしまい、「Hy-MT2 の前に置く」意味が消える。
        """
        cached = self._mt_cache.get(self._cache_key(utterance.text_ja, lang))
        if cached is None:
            return None
        # mt_ms=0 は「推論していない」の意味。遅延の内訳（#30）でヒットが
        # 推論時間として数えられないようにする
        translation = Translation(
            lang=lang, text=cached, engine=self._mt_engine_name, mt_ms=0
        )
        utterance.translations[lang] = translation
        return translation

    async def _translate(self, utterance: Utterance, lang: str) -> Translation | None:
        translation = utterance.translations.get(lang)
        if translation is None:  # 再接続復元ジョブは翻訳済みのことがある
            translation = self._translation_from_cache(utterance, lang)
        if translation is None:
            # 呼んだ時点で数える（#32）。キャッシュヒットも復元済みの再送もここへ
            # 来ないので、これが「Hy-MT2 を叩いた回数」そのものになる
            self.mt_calls += 1
            outcome = await self._mt_runner.run(
                partial(self._mt.translate, utterance.text_ja, lang),
                timeout=self._limits.mt_timeout_s,
            )
            if isinstance(outcome, InferenceTimedOut):
                logger.error(
                    "翻訳が %.0f 秒で返らないため諦めます; lang=%s seq=%s",
                    self._limits.mt_timeout_s,
                    lang,
                    utterance.seq,
                )
                await self._notify_overload("mt_timeout", "翻訳が応答しません。字幕を1件飛ばしました")
                return None
            if isinstance(outcome, InferenceFailed):
                if isinstance(outcome.error, TranslationModelError):
                    logger.error("翻訳に失敗: %s", outcome.error)
                else:
                    logger.error("MT failed; lang=%s seq=%s", lang, utterance.seq, exc_info=outcome.error)
                return None
            text = outcome.value
            mt_ms = outcome.elapsed_ms
            # 遅延の内訳（#30）。**実際に推論したときだけ**数える。キャッシュヒットを
            # 母数に入れると（実測ヒット率 27.8%）中央値が 0 へ引っ張られ、
            # 「翻訳は速い」と誤読させる。節約ぶんは mt_cache_hit_rate が別に表す。
            # 再接続復元ジョブの推論は数える（`_delay_samples` と違い実CPU費用そのもの）
            self._mt_ms_samples.append(mt_ms)
            translation = Translation(
                lang=lang,
                text=text,
                engine=self._mt_engine_name,
                mt_ms=mt_ms,
            )
            self._mt_cache.put(self._cache_key(utterance.text_ja, lang), text)
            utterance.translations[lang] = translation
        return translation

    async def _process_mt_job(self, job: MTJob) -> None:
        pending = list(job.recipient_ids)
        lang = job.lang
        attempted: set[str] = set()
        while True:
            attempted.add(lang)
            translation = await self._translate(job.utterance, lang)
            delay_ms = 0 if job.replay else max(0, int((time.monotonic() - job.closed_at) * 1000))
            if translation is not None and lang == job.lang and not job.replay:
                self._delay_samples.append(delay_ms)

            remaining: list[str] = []
            next_lang = None
            captions: dict[str, dict] = {}
            for client_id in pending:
                client = self.roster.get(client_id)
                if client is None or client.lang is None:
                    continue
                selected = client.lang
                translated = job.utterance.translations.get(selected)
                if translated is not None:
                    if selected not in captions:
                        captions[selected] = proto.Caption(
                            seq=job.utterance.seq,
                            ja=job.utterance.text_ja,
                            text=translated.text,
                            lang=selected,
                            delay_ms=delay_ms,
                        ).model_dump()
                    if self.roster.send(client, captions[selected]):
                        self._remember_caption(client, job.utterance.seq, selected)
                elif selected not in attempted:
                    remaining.append(client_id)
                    if next_lang is None:
                        next_lang = selected
            if next_lang is None:
                return
            lang = next_lang
            pending = remaining

    def _remember_caption(self, client: Client, seq: int, lang: str) -> None:
        if len(client.caption_languages) >= self._caption_receipt_limit:
            client.caption_languages = {
                seq: lang for seq, lang in client.caption_languages.items()
                if seq >= self.history.history_from
            }
        if seq >= self.history.history_from:
            client.caption_languages[seq] = lang

    def refresh_history(self, client: Client) -> None:
        self._history_refreshes.pop(client.id, None)
        if client.lang is None or self._mt_queue.qsize() >= self._limits.mt_replay_watermark:
            return
        utterances = deque(self.history.since(0))
        if utterances:
            self._history_refreshes[client.id] = HistoryRefresh(client.lang, utterances)
            self._mt_idle.clear()
            self._mt_work.set()

    def cancel_history_refresh(self, client: Client) -> None:
        self._history_refreshes.pop(client.id, None)

    async def _refresh_one_caption(self) -> None:
        client_id = next(iter(self._history_refreshes))
        refresh = self._history_refreshes[client_id]
        client = self.roster.get(client_id)
        if client is None:
            del self._history_refreshes[client_id]
            return

        utterance = refresh.utterances.popleft()
        if client.caption_languages.get(utterance.seq) != refresh.lang:
            translation = await self._translate(utterance, refresh.lang)
            if self._history_refreshes.get(client_id) is not refresh:
                return
            if translation is not None and self.roster.contains(client):
                caption = proto.Caption(
                    seq=utterance.seq,
                    ja=utterance.text_ja,
                    text=translation.text,
                    lang=refresh.lang,
                    delay_ms=0,
                )
                if not self.roster.send(client, caption.model_dump(), drop_if_full=True):
                    del self._history_refreshes[client_id]
                    return
                self._remember_caption(client, utterance.seq, refresh.lang)
        if not refresh.utterances:
            del self._history_refreshes[client_id]

    async def replay_history(self, client: Client, last_seq: int) -> None:
        """再接続した生徒への差分復元（F-11）。

        訳文の有無を問わず全件をターゲット配信ジョブとして seq 順に積む
        （訳文済みはワーカーがエンジンを呼ばずキャッシュ配信）。低優先度なので
        ライブ字幕を停滞させない。ライブ配信との交錯による表示順・重複は
        クライアント側（seq順挿入・重複排除・連続確定watermark）が吸収する。
        """
        if client.lang is None:
            return
        for utterance in self.history.since(last_seq):
            queued = await self._enqueue_mt(
                _REPLAY_PRIORITY,
                MTJob(utterance, client.lang, time.monotonic(), (client.id,), replay=True),
            )
            if not queued:
                # 混雑で復元を積めなかった。ここで黙って諦めると、この生徒は
                # このセッション中ずっとその字幕を取り戻せない（クライアントの
                # last_seq は進まないが、再接続の契機が無い）。接続を切って
                # 自動再接続に載せ、空いてから同じ last_seq でやり直させる
                logger.info("復元を積めなかったため生徒を切断し、再接続でのやり直しに委ねます")
                await self.roster.retry(client)
                return

    # ---- 配信 ----

    async def broadcast_caption(self, caption: proto.Caption) -> None:
        self.roster.send_language(caption.lang, caption.model_dump())

    async def send_to_teacher(
        self, message: proto.AsrFinal | proto.ErrorMsg | proto.Stats | proto.TurnPartial
    ) -> None:
        self.roster.send_teacher(message.model_dump())

    async def set_accepting_audio(self, accepting: bool) -> None:
        self._accepting_audio = accepting
        await self._broadcast_speaking(self._speaking_signal.set_accepting_audio(accepting))
        self._mic_silent_warned = False
        self._live_since = time.monotonic() if accepting else None
        if not accepting:
            self._delay_samples.clear()
            self._asr_ms_samples.clear()
            self._mt_ms_samples.clear()

    async def _broadcast_all(self, payload: dict) -> None:
        self.roster.broadcast(payload)

    def on_teacher_joined(self) -> None:
        """新しい先生が接続したとき、進行中の無音警告を再武装する。
        live のまま先生が入れ替わった場合でも、新しい先生が継続中の無音を見られる（E-01）。"""
        self._mic_silent_warned = False

    # ---- モニタリング（#15） ----

    async def _stats_worker(self) -> None:
        while True:
            await asyncio.sleep(self._stats_interval_s)
            if self.roster.teacher() is None:
                continue
            await self.send_to_teacher(self.stats_snapshot())
            await self._check_mic_silence()

    def stats_snapshot(self) -> proto.Stats:
        students = self.roster.students()
        cache = self._mt_cache.stats()
        return proto.Stats(
            students=len(students),
            langs=dict(Counter(c.lang for c in students if c.lang)),
            queue_depth=self.queue_depth,
            median_delay_ms=_median_ms(self._delay_samples),
            # 過負荷判定はASR側を秒数へ移した（#25 A-5）。1発話は 0.3〜30秒と幅があるので
            # 件数では「どれだけ遅れているか」を表現できない。翻訳側は1件あたりの
            # 処理時間が揃っているので件数のままでよい
            overloaded=(
                self.audio_queue_seconds >= self._overload_audio_s
                or self._mt_queue.qsize() >= self._overload_queue_depth
            ),
            audio_queue_seconds=round(self.audio_queue_seconds, 2),
            # 内訳（#30）。合計は上の audio_queue_seconds のまま変えていない
            asr_wait_seconds=round(self.asr_wait_seconds, 2),
            asr_active_seconds=round(self.asr_active_seconds, 2),
            median_asr_ms=_median_ms(self._asr_ms_samples),
            mt_queue_depth=self._mt_queue.qsize(),
            median_mt_ms=_median_ms(self._mt_ms_samples),
            mt_cache_hit_rate=round(cache.hit_rate, 3),
            mt_cache_hits=cache.hits,
            mt_cache_size=cache.size,
            # 通算の推論回数と破棄（#32）。スケーリング試験はこの差分だけを見る
            asr_calls=self.asr_calls,
            asr_partial_calls=self.asr_partial_calls,
            mt_calls=self.mt_calls,
            audio_frames_rejected=self.audio_frames_rejected,
            asr_dropped_segments=self.asr_dropped_segments,
            asr_dropped_seconds=round(self.asr_dropped_seconds, 2),
            # 生存 asyncio タスク数（#32）。タスクリークはクライアント側からは
            # 原理的に見えない（プロセスのスレッド数にもハンドル数にも出ない）
            tasks=_live_task_count(),
        )

    async def _check_mic_silence(self) -> None:
        """配信中に一定時間音声が検出されないとき、先生へ一度だけ警告する（E-01）。
        音声が再開したら再武装する。マイク断（フレーム自体が来ない）も検出できる。"""
        if not self._accepting_audio or self._live_since is None:
            return
        last_activity = max(self._live_since, self._segmenter.last_speech_at or 0.0)
        silent_for = time.monotonic() - last_activity
        if silent_for < self._silence_warning_s:
            self._mic_silent_warned = False
            return
        if not self._mic_silent_warned:
            self._mic_silent_warned = True
            # マイクが死ぬとフレーム自体が来なくなり、VAD も ASR ワーカーも動かないので
            # 「発話中」を畳む契機が無い（#29）。ここが唯一の受け皿になる
            await self._broadcast_speaking(self._speaking_signal.on_mic_silent())
            await self.send_to_teacher(
                proto.ErrorMsg(
                    code="mic_silent",
                    message=f"{int(self._silence_warning_s)}秒以上音声がありません。"
                    "マイクのミュートや接続を確認してください",
                )
            )

    async def _notify_overload(self, code: proto.ErrorCode, message: str) -> None:
        """先生へ過負荷を知らせる。同じ code はクールダウン中は送らない
        （溢れている間は毎件発生しうるので、通知が新たな過負荷にならないように）。"""
        now = time.monotonic()
        last = self._notice_at.get(code)
        if last is not None and now - last < _NOTICE_COOLDOWN_S:
            return
        self._notice_at[code] = now
        await self.send_to_teacher(proto.ErrorMsg(code=code, message=message))
