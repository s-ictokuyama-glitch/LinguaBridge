"""ASR の FIFO と音声容量を管理する。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum

from server.audio.vad import InterimSegment, Segment, TurnBoundary


@dataclass
class FlushTurn:
    """保留中の Turn を先行する音声の処理後に確定する。"""

    closed_at: float


AsrEvent = Segment | InterimSegment | TurnBoundary | FlushTurn


class OfferResult(Enum):
    Accepted = "accepted"
    Dropped = "dropped"
    Skipped = "skipped"


Accepted = OfferResult.Accepted
Dropped = OfferResult.Dropped
Skipped = OfferResult.Skipped


class AsrBacklog:
    """単一 ASR ワーカー用。確定音声だけを sample 数で容量会計する。"""

    def __init__(
        self,
        max_audio_seconds: float,
        sample_rate: int = 16000,
        min_interim_audio_s: float = 0.0,
    ) -> None:
        self._queue: asyncio.Queue[tuple[AsrEvent, int]] = asyncio.Queue()
        self._max_audio_seconds = max_audio_seconds
        self._sample_rate = sample_rate
        self._min_interim_audio_s = min_interim_audio_s
        self._waiting_samples = 0
        self._active_samples = 0
        self._active = False
        self.dropped_segments = 0
        self.dropped_seconds = 0.0
        self.interims_skipped = 0

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def is_idle(self) -> bool:
        """待機イベントも処理中イベントも無いか。"""
        return not self._active and self.depth == 0

    @property
    def waiting_seconds(self) -> float:
        return self._waiting_samples / self._sample_rate

    @property
    def active_seconds(self) -> float:
        return self._active_samples / self._sample_rate

    @property
    def total_seconds(self) -> float:
        return self.waiting_seconds + self.active_seconds

    def offer(self, event: AsrEvent) -> OfferResult:
        samples = event.pcm.size if isinstance(event, Segment) else 0
        if isinstance(event, InterimSegment):
            if (
                not self.is_idle
                or event.pcm.size / self._sample_rate < self._min_interim_audio_s
            ):
                self.interims_skipped += 1
                return Skipped
        total_samples = self._waiting_samples + self._active_samples
        if samples and total_samples and (
            (total_samples + samples) / self._sample_rate > self._max_audio_seconds
        ):
            self.dropped_segments += 1
            self.dropped_seconds += samples / self._sample_rate
            return Dropped
        self._waiting_samples += samples
        self._queue.put_nowait((event, samples))
        return Accepted

    @asynccontextmanager
    async def next(self) -> AsyncIterator[AsrEvent]:
        event, samples = await self._queue.get()
        self._waiting_samples -= samples
        self._active_samples += samples
        self._active = True
        try:
            yield event
        finally:
            self._active_samples -= samples
            self._active = False
            self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()
