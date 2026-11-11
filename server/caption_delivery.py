"""確定した発話をライブ配信し、字幕の復元と言語切替を処理する。"""
from __future__ import annotations

import asyncio
import itertools
import logging
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial

from server import ws_protocol as proto
from server.caption_history import CaptionHistory, Translation, Utterance
from server.config import AppConfig
from server.inference import (
    InferenceFailed, InferenceRunner, InferenceSucceeded, InferenceTimedOut, Runner, finish_cleanup,
)
from server.model_files import TranslationModelError
from server.mt.base import TranslationEngine
from server.mt.cache import CacheKey, CacheStats, TranslationCache
from server.roster import Client, Roster
from server.turn.assembler import Turn

logger = logging.getLogger(__name__)

_SOURCE_LANG = "ja"
_DELAY_SAMPLE_SIZE = 20


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

@dataclass(frozen=True)
class CaptionMetrics:
    median_delay_ms: int
    median_mt_ms: int
    cache: CacheStats
    calls: int


class CaptionDelivery:
    def __init__(
        self, roster: Roster, config: AppConfig, mt_engine: TranslationEngine,
        notify_overload: Callable[[proto.ErrorCode, str], Awaitable[None]],
        *, runner: Runner | None = None,
    ) -> None:
        self.roster = roster
        self.history = CaptionHistory(config.history_resend)
        self._limits = config.limits
        self._notify_overload = notify_overload
        self._mt_runner = runner if runner is not None else InferenceRunner(name="mt")
        self._worker_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._calls = 0
        self._delay_samples: deque[int] = deque(maxlen=_DELAY_SAMPLE_SIZE)
        self._mt_ms_samples: deque[int] = deque(maxlen=_DELAY_SAMPLE_SIZE)
        self._mt = mt_engine
        self._mt_engine_name = config.mt.engine
        # 発話をまたぐ翻訳キャッシュ（#26 B-4）。「はい」「もう一度言います」のような
        # 繰り返しで Hy-MT2 を叩かない。**有界**（config で 0 にすると無効）
        self._mt_cache = TranslationCache(config.mt.cache_size)
        # モデル版はキーの一部。設定を変えたあとに古い設定の訳が返るのを防ぐ
        self._mt_model_version = mt_engine.model_version
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

    def start(self) -> None:
        self._worker_task = asyncio.create_task(self._mt_worker(), name="mt-worker")

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        await finish_cleanup(self._stop_task)

    async def _stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
            self._worker_task = None
        self._history_refreshes.clear()
        await self._mt_runner.aclose()

    async def warmup(self) -> InferenceSucceeded[None] | InferenceTimedOut | InferenceFailed:
        return await self._mt_runner.run(self._mt.warmup, timeout=None)

    async def drain(self, timeout: float | None) -> None:
        await asyncio.wait_for(self._mt_idle.wait(), timeout=timeout)

    @property
    def queue_depth(self) -> int:
        return self._mt_queue.qsize()

    def reset_latency(self) -> None:
        self._delay_samples.clear()
        self._mt_ms_samples.clear()

    def metrics(self) -> CaptionMetrics:
        return CaptionMetrics(
            int(statistics.median(self._delay_samples)) if self._delay_samples else 0,
            int(statistics.median(self._mt_ms_samples)) if self._mt_ms_samples else 0,
            self._mt_cache.stats(), self._calls,
        )

    def confirm(self, turn: Turn) -> Utterance:
        return self.history.append(
            t_start=turn.t_start,
            t_end=turn.t_end,
            text_ja=turn.text,
            asr_ms=turn.asr_ms,
            segments=turn.parts,
        )

    async def publish(self, utterance: Utterance, *, closed_at: float) -> None:
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
            self._calls += 1
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

    def set_language(self, client: Client, lang: str) -> None:
        if client.lang == lang:
            return
        client.lang = lang
        client.caption_languages.clear()
        self.cancel_history_refresh(client)
        if self._mt_queue.qsize() >= self._limits.mt_replay_watermark:
            return
        utterances = deque(self.history.since(0))
        if utterances:
            self._history_refreshes[client.id] = HistoryRefresh(lang, utterances)
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
