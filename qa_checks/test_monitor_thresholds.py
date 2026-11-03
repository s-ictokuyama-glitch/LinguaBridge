"""Exact reporting boundaries; queue accumulation itself has separate tests."""

import asyncio

import pytest

from server.audio.vad import Segment
from tests.helpers import silence_pcm
from tests.unit.test_stats_breakdown import make_pipeline


@pytest.mark.parametrize("audio_seconds,mt_depth,expected", [
    (9.99, 0, False), (10.0, 0, True), (10.01, 0, True),
    (0, 2, False), (0, 3, True), (0, 4, True),
])
def test_mon08_shipped_overload_threshold_boundaries(audio_seconds, mt_depth, expected):
    async def scenario():
        pipeline = await make_pipeline(students=1 if mt_depth else 0)
        now = asyncio.get_running_loop().time()
        try:
            if audio_seconds:
                pipeline._asr_backlog.offer(Segment(
                    silence_pcm(audio_seconds), 0, audio_seconds, now,
                ))
            if mt_depth:
                for _ in range(mt_depth):
                    utterance = pipeline.captions.history.append(
                        t_start=0, t_end=1, text_ja="こんにちは", asr_ms=1,
                    )
                    await pipeline.captions.publish(utterance, closed_at=now)
            assert pipeline.stats_snapshot().overloaded is expected
        finally:
            await pipeline.stop()
    asyncio.run(scenario())
