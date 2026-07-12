"""オーケストレーター（plan.md §6.3）。

teacher WS → ingest → VoiceSegmenter → asr_queue → ASRワーカー(1スレッド)
  → mt_queue（アクティブ言語ごとにジョブ展開） → MTワーカー(1スレッド)
  → 言語別ブロードキャスト / 先生へ asr_final

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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from server import ws_protocol as proto
from server.asr.base import ASREngine
from server.asr.hallucination import hallucination_reason
from server.audio.ingest import pcm16_from_bytes
from server.audio.vad import Segment, VoiceSegmenter, build_frame_vad
from server.config import AppConfig
from server.mt.base import TranslationEngine
from server.recorder import SessionRecorder
from server.session import Client, Session

logger = logging.getLogger(__name__)


@dataclass
class Translation:
    lang: str
    text: str
    engine: str
    mt_ms: int


@dataclass
class Utterance:
    seq: int
    t_start: float
    t_end: float
    text_ja: str
    asr_ms: int
    created_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    translations: dict[str, Translation] = field(default_factory=dict)


@dataclass
class MTJob:
    utterance: Utterance
    lang: str
    closed_at: float  # 発話確定時刻（monotonic）。delay_ms の起点
    # 再接続時の差分復元（F-11）用: 指定時はこのクライアントにのみ届ける
    target_client_id: str | None = None


# MTキューの優先度: ライブ字幕（N-01の遅延基準の対象）が
# 再接続復元ジョブ（最大K=50件）に停滞させられないようにする
_LIVE_PRIORITY = 0
_REPLAY_PRIORITY = 1

# 遅延中央値の算出に使う直近captionの件数（stats用）
_DELAY_SAMPLE_SIZE = 20


class Pipeline:
    def __init__(
        self,
        session: Session,
        config: AppConfig,
        asr_engine: ASREngine,
        mt_engine: TranslationEngine,
    ) -> None:
        self._session = session
        self._asr = asr_engine
        self._mt = mt_engine
        self._mt_engine_name = config.mt.engine
        frame_vad = build_frame_vad(config.vad)
        self._segmenter = VoiceSegmenter(
            frame_vad,
            min_silence_ms=config.vad.min_silence_ms,
            max_utterance_s=config.vad.max_utterance_s,
            frame_ms=frame_vad.frame_ms,
            pre_roll_ms=config.vad.pre_roll_ms,
        )
        self._asr_queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
        # (優先度, 連番, ジョブ)。連番は同一優先度内のFIFOを保証する
        self._mt_queue: asyncio.PriorityQueue[tuple[int, int, MTJob]] = asyncio.PriorityQueue()
        self._mt_counter = itertools.count()
        self._asr_executor = ThreadPoolExecutor(1, thread_name_prefix="asr")
        self._mt_executor = ThreadPoolExecutor(1, thread_name_prefix="mt")
        self._tasks: list[asyncio.Task[None]] = []
        self._warmup_task: asyncio.Task[None] | None = None
        self.ready = False  # モデルの事前ロード完了（/healthz が 200 を返す条件）
        # モニタリング（#15）: 統計の定期配信と無音警告（E-01）
        self._stats_interval_s = config.monitoring.stats_interval_s
        self._silence_warning_s = config.monitoring.silence_warning_s
        self._overload_queue_depth = config.monitoring.overload_queue_depth
        self._delay_samples: deque[int] = deque(maxlen=_DELAY_SAMPLE_SIZE)
        # 記録（#18）: 記録ON中に確定した発話を蓄積し、終了時に書き出す
        self._recorder = SessionRecorder(
            config.recording.resolved_out_dir, config.language_codes
        )
        self._live_since: float | None = None  # 無音計測の基準（live遷移でリセット）
        self._mic_silent_warned = False

    async def start(self) -> None:
        # ワーカーは即座に起動し、サーバーをすぐ応答可能にする（healthz 503 の窓を作る）。
        # モデルの warmup は背後で走らせ、完了で ready を立てる（/healthz は #16 で 503→200）。
        # ワーカーは同一の単一スレッドExecutorを使うため warmup と実推論は直列化される。
        self._tasks = [
            asyncio.create_task(self._asr_worker(), name="asr-worker"),
            asyncio.create_task(self._mt_worker(), name="mt-worker"),
            asyncio.create_task(self._stats_worker(), name="stats-worker"),
        ]
        self._warmup_task = asyncio.create_task(self._run_warmup(), name="warmup")

    async def _run_warmup(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._asr_executor, self._asr.warmup)
            await loop.run_in_executor(self._mt_executor, self._mt.warmup)
        except Exception:
            # warmup失敗時は ready を立てない（/healthz は 503 のまま = 異常を正直に返す）。
            # 実推論側の遅延ロードで復旧する可能性はあるが、健全性としては未ロード扱い
            logger.exception("モデルの事前ロードに失敗。/healthz は 503 のままになります")
            return
        self.ready = True

    async def stop(self) -> None:
        if self._warmup_task is not None:
            self._warmup_task.cancel()
        for task in self._tasks:
            task.cancel()
        pending = [*self._tasks, *([self._warmup_task] if self._warmup_task else [])]
        await asyncio.gather(*pending, return_exceptions=True)
        self._tasks = []
        self._warmup_task = None
        self._asr_executor.shutdown(wait=False)
        self._mt_executor.shutdown(wait=False)

    @property
    def queue_depth(self) -> int:
        return self._asr_queue.qsize() + self._mt_queue.qsize()

    # ---- 音声入力（先生WSハンドラから呼ばれる） ----

    async def feed_audio(self, data: bytes) -> None:
        if self._session.state != "live":
            return  # 一時停止・終了中の音声は破棄
        for segment in self._segmenter.feed(pcm16_from_bytes(data)):
            await self._asr_queue.put(segment)

    async def flush_audio(self) -> None:
        """進行中の発話を確定して処理に回す（一時停止・終了時）。"""
        segment = self._segmenter.flush()
        if segment is not None:
            await self._asr_queue.put(segment)

    # ---- ワーカー ----

    async def _asr_worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            segment = await self._asr_queue.get()
            try:
                await self._process_segment(loop, segment)
            finally:
                # finalize_recording の join() が排出完了を検知できるよう必ず1回呼ぶ
                self._asr_queue.task_done()

    async def _process_segment(
        self, loop: asyncio.AbstractEventLoop, segment: Segment
    ) -> None:
        started = time.monotonic()
        try:
            result = await loop.run_in_executor(
                self._asr_executor,
                self._asr.transcribe,
                segment.pcm,
                self._segmenter.sample_rate,
            )
        except Exception:
            logger.exception("ASR failed; utterance dropped")
            return
        asr_ms = int((time.monotonic() - started) * 1000)
        reason = hallucination_reason(result)
        if reason is not None:
            logger.info("発話を破棄（幻覚フィルタ: %s）", reason)
            return
        utterance = Utterance(
            seq=self._session.next_seq(),
            t_start=segment.t_start,
            t_end=segment.t_end,
            text_ja=result.text,
            asr_ms=asr_ms,
        )
        self._session.add_history(utterance)
        if self._session.recording:  # 記録ON中の発話のみ蓄積（F-10）
            self._recorder.add(utterance)
        await self.send_to_teacher(
            proto.AsrFinal(seq=utterance.seq, ja=utterance.text_ja, asr_ms=asr_ms)
        )
        for lang in sorted(self._session.active_langs()):
            await self._enqueue_mt(_LIVE_PRIORITY, MTJob(utterance, lang, segment.closed_at))

    async def _enqueue_mt(self, priority: int, job: MTJob) -> None:
        await self._mt_queue.put((priority, next(self._mt_counter), job))

    async def _mt_worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            _, _, job = await self._mt_queue.get()
            try:
                await self._process_mt_job(loop, job)
            finally:
                self._mt_queue.task_done()

    async def _process_mt_job(self, loop: asyncio.AbstractEventLoop, job: MTJob) -> None:
        translation = job.utterance.translations.get(job.lang)
        if translation is None:  # 再接続復元ジョブは翻訳済みのことがある
            started = time.monotonic()
            try:
                text = await loop.run_in_executor(
                    self._mt_executor,
                    self._mt.translate,
                    job.utterance.text_ja,
                    job.lang,
                )
            except Exception:
                logger.exception("MT failed; lang=%s seq=%s", job.lang, job.utterance.seq)
                return
            translation = Translation(
                lang=job.lang,
                text=text,
                engine=self._mt_engine_name,
                mt_ms=int((time.monotonic() - started) * 1000),
            )
            job.utterance.translations[job.lang] = translation
        # delay_ms は「ライブ配信の処理遅延」の指標（E-05）。再接続復元の字幕は
        # 歴史的な再送なので 0 とし、生徒側の「遅延中」表示を誤発火させない
        delay_ms = (
            0
            if job.target_client_id is not None
            else max(0, int((time.monotonic() - job.closed_at) * 1000))
        )
        caption = proto.Caption(
            seq=job.utterance.seq,
            ja=job.utterance.text_ja,
            text=translation.text,
            lang=job.lang,
            delay_ms=delay_ms,
        )
        if job.target_client_id is None:
            self._delay_samples.append(caption.delay_ms)  # ライブ配信のみ統計対象
            await self.broadcast_caption(caption)
        else:
            await self._deliver_replay(job, caption)

    async def _deliver_replay(self, job: MTJob, caption: proto.Caption) -> None:
        """再接続復元ジョブの成果を対象クライアントにのみ届ける。"""
        assert job.target_client_id is not None
        client = self._session.clients.get(job.target_client_id)
        if client is None:
            return  # 復元待ちの間に再切断。次回rejoinのlast_seqで再復元される
        if client.lang == job.lang:
            await self._safe_send(client, caption.model_dump())
        elif client.lang is not None:
            # 復元待ちの間に言語変更: 新しい言語で翻訳し直して届ける
            await self._enqueue_mt(
                _REPLAY_PRIORITY,
                MTJob(job.utterance, client.lang, job.closed_at, target_client_id=client.id),
            )

    async def replay_history(self, client: Client, last_seq: int) -> None:
        """再接続した生徒への差分復元（F-11）。

        訳文の有無を問わず全件をターゲット配信ジョブとして seq 順に積む
        （訳文済みはワーカーがエンジンを呼ばずキャッシュ配信）。低優先度なので
        ライブ字幕を停滞させない。ライブ配信との交錯による表示順・重複は
        クライアント側（seq順挿入・重複排除・連続確定watermark）が吸収する。
        """
        if client.lang is None:
            return
        for utterance in self._session.history_entries_since(last_seq):
            await self._enqueue_mt(
                _REPLAY_PRIORITY,
                MTJob(utterance, client.lang, time.monotonic(), target_client_id=client.id),
            )

    # ---- 配信 ----

    async def broadcast_caption(self, caption: proto.Caption) -> None:
        payload = caption.model_dump()
        for client in self._session.students():
            if client.lang == caption.lang:
                await self._safe_send(client, payload)

    async def send_to_teacher(
        self, message: proto.AsrFinal | proto.ErrorMsg | proto.Stats
    ) -> None:
        teacher = self._session.teacher()
        if teacher is not None:
            await self._safe_send(teacher, message.model_dump())

    async def broadcast_session_state(self) -> None:
        if self._session.state == "live":
            # 無音警告（E-01）の基準を配信開始/再開時点にリセット
            self._live_since = time.monotonic()
            self._mic_silent_warned = False
        else:
            # 非live中はライブ遅延の指標を持ち越さない（一時停止・終了で古い値を出さない）
            self._delay_samples.clear()
        payload = proto.SessionStateMsg(state=self._session.state).model_dump()
        for client in list(self._session.clients.values()):
            await self._safe_send(client, payload)

    def on_teacher_joined(self) -> None:
        """新しい先生が接続したとき、進行中の無音警告を再武装する。
        live のまま先生が入れ替わった場合でも、新しい先生が継続中の無音を見られる（E-01）。"""
        self._mic_silent_warned = False

    # ---- 記録（#18） ----

    async def broadcast_recording(self) -> None:
        """記録ON/OFFを全クライアントへ通知（先生・生徒双方のインジケーター F-10）。"""
        payload = proto.RecordingState(on=self._session.recording).model_dump()
        for client in list(self._session.clients.values()):
            await self._safe_send(client, payload)

    async def finalize_recording(self) -> Path | None:
        """記録があれば全キューを排出して訳文を確定させ、ファイルへ書き出す。

        セッション終了時に呼ぶ。書き出したフォルダ、記録なしなら None を返す。
        """
        if not self._recorder.has_entries:
            return None
        try:
            await asyncio.wait_for(self._asr_queue.join(), timeout=30)
            await asyncio.wait_for(self._mt_queue.join(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("記録の書き出し前のキュー排出がタイムアウト。現時点の内容で書き出します")
        return self._recorder.write(self._session.started_at)

    # ---- モニタリング（#15） ----

    async def _stats_worker(self) -> None:
        while True:
            await asyncio.sleep(self._stats_interval_s)
            if self._session.teacher() is None:
                continue
            await self.send_to_teacher(self.stats_snapshot())
            await self._check_mic_silence()

    def stats_snapshot(self) -> proto.Stats:
        students = self._session.students()
        depth = self.queue_depth
        return proto.Stats(
            students=len(students),
            langs=dict(Counter(c.lang for c in students if c.lang)),
            queue_depth=depth,
            median_delay_ms=(
                int(statistics.median(self._delay_samples)) if self._delay_samples else 0
            ),
            overloaded=depth >= self._overload_queue_depth,
        )

    async def _check_mic_silence(self) -> None:
        """配信中に一定時間音声が検出されないとき、先生へ一度だけ警告する（E-01）。
        音声が再開したら再武装する。マイク断（フレーム自体が来ない）も検出できる。"""
        if self._session.state != "live" or self._live_since is None:
            return
        last_activity = max(self._live_since, self._segmenter.last_speech_at or 0.0)
        silent_for = time.monotonic() - last_activity
        if silent_for < self._silence_warning_s:
            self._mic_silent_warned = False
            return
        if not self._mic_silent_warned:
            self._mic_silent_warned = True
            await self.send_to_teacher(
                proto.ErrorMsg(
                    code="mic_silent",
                    message=f"{int(self._silence_warning_s)}秒以上音声がありません。"
                    "マイクのミュートや接続を確認してください",
                )
            )

    @staticmethod
    async def _safe_send(client: Client, payload: dict) -> None:
        if client.ws is None:
            return
        try:
            await client.ws.send_json(payload)
        except Exception:
            pass  # 切断済み。クライアント除去はWSハンドラの finally が行う
