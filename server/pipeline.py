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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from server import ws_protocol as proto
from server.asr.base import ASREngine
from server.asr.hallucination import hallucination_reason
from server.audio.ingest import pcm16_from_bytes
from server.audio.vad import Segment, VoiceSegmenter, build_frame_vad
from server.config import AppConfig
from server.mt.base import TranslationEngine
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

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._asr_executor, self._asr.warmup)
        await loop.run_in_executor(self._mt_executor, self._mt.warmup)
        self._tasks = [
            asyncio.create_task(self._asr_worker(), name="asr-worker"),
            asyncio.create_task(self._mt_worker(), name="mt-worker"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
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
                continue
            asr_ms = int((time.monotonic() - started) * 1000)
            reason = hallucination_reason(result)
            if reason is not None:
                logger.info("発話を破棄（幻覚フィルタ: %s）", reason)
                continue
            utterance = Utterance(
                seq=self._session.next_seq(),
                t_start=segment.t_start,
                t_end=segment.t_end,
                text_ja=result.text,
                asr_ms=asr_ms,
            )
            self._session.add_history(utterance)
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
                    continue
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
            if job.target_client_id is not None:
                client = self._session.clients.get(job.target_client_id)
                if client is None:
                    continue  # 復元待ちの間に再切断。次回rejoinのlast_seqで再復元される
                if client.lang == job.lang:
                    await self._safe_send(client, caption.model_dump())
                elif client.lang is not None:
                    # 復元待ちの間に言語変更: 新しい言語で翻訳し直して届ける
                    await self._enqueue_mt(
                        _REPLAY_PRIORITY,
                        MTJob(
                            job.utterance,
                            client.lang,
                            job.closed_at,
                            target_client_id=client.id,
                        ),
                    )
            else:
                await self.broadcast_caption(caption)

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

    async def send_to_teacher(self, message: proto.AsrFinal | proto.ErrorMsg) -> None:
        teacher = self._session.teacher()
        if teacher is not None:
            await self._safe_send(teacher, message.model_dump())

    async def broadcast_session_state(self) -> None:
        payload = proto.SessionStateMsg(state=self._session.state).model_dump()
        for client in list(self._session.clients.values()):
            await self._safe_send(client, payload)

    @staticmethod
    async def _safe_send(client: Client, payload: dict) -> None:
        if client.ws is None:
            return
        try:
            await client.ws.send_json(payload)
        except Exception:
            pass  # 切断済み。クライアント除去はWSハンドラの finally が行う
