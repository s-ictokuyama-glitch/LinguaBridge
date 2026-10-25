"""Exact reporting boundaries; queue accumulation itself has separate tests."""

import pytest

from tests.unit.test_stats_breakdown import make_pipeline


@pytest.mark.parametrize("audio_seconds,mt_depth,expected", [
    (9.99, 0, False), (10.0, 0, True), (10.01, 0, True),
    (0, 2, False), (0, 3, True), (0, 4, True),
])
def test_mon08_shipped_overload_threshold_boundaries(audio_seconds, mt_depth, expected):
    pipeline = make_pipeline()
    assert pipeline._overload_audio_s == 10.0
    assert pipeline._overload_queue_depth == 3
    # Isolate the predicate at equality, without audio framing rounding away
    # 9.99/10.00 or a worker consuming a queued job during inspection.
    pipeline._queued_audio_s = audio_seconds
    for index in range(mt_depth):
        pipeline._mt_queue.put_nowait((0, index, None))
    assert pipeline.stats_snapshot().overloaded is expected
