import asyncio

import numpy as np
import pytest

from server.asr_backlog import Accepted, AsrBacklog, Dropped, FlushTurn, Skipped
from server.audio.vad import InterimSegment, Segment, TurnBoundary


def segment(samples: int) -> Segment:
    return Segment(np.zeros(samples, dtype=np.int16), 0, samples / 16000, 0)


def interim(samples: int = 16000) -> InterimSegment:
    return InterimSegment(np.zeros(samples, dtype=np.int16), 0, 1, 0)


def test_capacity_counts_waiting_and_active_and_accepts_one_oversized_audio():
    async def scenario():
        backlog = AsrBacklog(1)
        assert backlog.is_idle
        first = segment(8000)
        assert backlog.offer(first) is Accepted
        async with backlog.next() as event:
            assert event is first
            assert backlog.depth == 0
            assert not backlog.is_idle
            assert backlog.waiting_seconds == 0
            assert backlog.active_seconds == 0.5
            assert backlog.offer(segment(8000)) is Accepted
            assert backlog.offer(segment(1)) is Dropped
            assert backlog.total_seconds == backlog.waiting_seconds + backlog.active_seconds
        async with backlog.next():
            pass
        assert backlog.total_seconds == 0
        assert backlog.is_idle
        assert backlog.offer(segment(480000)) is Accepted
        assert backlog.offer(segment(1)) is Dropped
        assert backlog.dropped_segments == 2
        assert backlog.dropped_seconds == 2 / 16000
    asyncio.run(scenario())


def test_fifo_preserves_boundaries_at_capacity_and_interim_requires_full_idle():
    async def scenario():
        backlog = AsrBacklog(0.1, min_interim_audio_s=0.5)
        assert backlog.offer(interim(7999)) is Skipped
        initial = interim()
        assert backlog.offer(initial) is Accepted
        assert backlog.offer(interim()) is Skipped
        async with backlog.next() as event:
            assert event is initial
            assert backlog.offer(interim()) is Skipped
            audio = segment(16000)
            boundary = TurnBoundary(1, 0)
            flush = FlushTurn(0)
            for item in (audio, boundary, flush):
                assert backlog.offer(item) is Accepted
        for expected in (audio, boundary, flush):
            async with backlog.next() as event:
                assert event is expected
        await backlog.join()
        assert backlog.depth == 0
        assert backlog.interims_skipped == 3
        assert backlog.offer(interim()) is Accepted
    asyncio.run(scenario())


def test_cleanup_is_exception_and_cancellation_safe():
    async def scenario():
        backlog = AsrBacklog(10)
        backlog.offer(segment(16000))
        with pytest.raises(RuntimeError):
            async with backlog.next():
                raise RuntimeError("failed ASR")
        await backlog.join()
        assert backlog.total_seconds == 0
        entered = asyncio.Event()
        async def worker():
            async with backlog.next():
                entered.set()
                await asyncio.Event().wait()
        backlog.offer(segment(16000))
        task = asyncio.create_task(worker())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await backlog.join()
        assert backlog.total_seconds == 0
        assert backlog.offer(interim()) is Accepted
    asyncio.run(scenario())


def test_many_additions_subtractions_and_mutable_pcm_return_exactly_zero():
    async def scenario():
        backlog = AsrBacklog(1000)
        items = [segment(n) for n in range(1, 501)]
        for event in items:
            assert backlog.offer(event) is Accepted
        items[0].pcm = np.zeros(999, dtype=np.int16)
        for _ in items:
            async with backlog.next():
                assert backlog.total_seconds == backlog.waiting_seconds + backlog.active_seconds
        await backlog.join()
        assert backlog.total_seconds == backlog.waiting_seconds == backlog.active_seconds == 0.0
        assert backlog.offer(interim()) is Accepted
    asyncio.run(scenario())
