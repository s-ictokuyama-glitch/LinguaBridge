"""性能受け入れ判定ロジック（scripts/acceptance.py）のユニットテスト（#17）。"""

from __future__ import annotations

from scripts.acceptance import (
    LATENCY_MAX_LIMIT_S,
    LATENCY_MEDIAN_LIMIT_S,
    RSS_LIMIT_MB,
    judge,
    latency_stats,
    memory_trend,
)


class TestLatencyStats:
    def test_none_on_empty(self):
        assert latency_stats([]) is None

    def test_median_p95_max(self):
        s = latency_stats([1000, 2000, 3000, 4000, 100000])
        assert s is not None
        assert s.count == 5
        assert s.median_s == 3.0
        assert s.max_s == 100.0
        assert s.p95_s == 100.0  # 上位5%＝最大寄り


class TestMemoryTrend:
    def test_none_when_too_few_samples(self):
        assert memory_trend([100, 200, 300]) is None

    def test_stable_memory_not_increasing(self):
        t = memory_trend([1000] * 20)
        assert t is not None
        assert not t.increasing
        assert t.peak_mb == 1000

    def test_clear_leak_flagged(self):
        # 前半 ~1000MB、後半 ~2000MB（+1000MB, +100%）→ 増加傾向
        rss = [1000.0] * 10 + [2000.0] * 10
        t = memory_trend(rss)
        assert t is not None
        assert t.increasing
        assert t.increase_mb >= 900

    def test_small_wobble_not_leak(self):
        # +50MB 程度のゆらぎはリークとみなさない（絶対閾値未満）
        rss = [1000.0] * 10 + [1050.0] * 10
        t = memory_trend(rss)
        assert t is not None
        assert not t.increasing


class TestJudge:
    def _ok_latency(self):
        return latency_stats([3000, 3500, 4000])

    def _ok_memory(self):
        return memory_trend([2000.0] * 20)

    def test_all_pass(self):
        v = judge(
            self._ok_latency(),
            self._ok_memory(),
            crashes=0,
            reconnect_failures=0,
            ran_seconds=2700,
            target_seconds=2700,
        )
        assert v.passed
        assert v.reasons == []

    def test_median_over_budget_fails(self):
        over = latency_stats([6000, 6000, 6000])  # 6s > 5s
        v = judge(over, self._ok_memory(), crashes=0, reconnect_failures=0,
                  ran_seconds=2700, target_seconds=2700)
        assert not v.passed
        assert any(str(LATENCY_MEDIAN_LIMIT_S) in r for r in v.reasons)

    def test_max_over_budget_fails(self):
        over = latency_stats([3000, 3000, 9000])  # max 9s > 8s
        v = judge(over, self._ok_memory(), crashes=0, reconnect_failures=0,
                  ran_seconds=2700, target_seconds=2700)
        assert not v.passed
        assert any(str(LATENCY_MAX_LIMIT_S) in r for r in v.reasons)

    def test_memory_over_limit_fails(self):
        big = memory_trend([RSS_LIMIT_MB + 500.0] * 20)
        v = judge(self._ok_latency(), big, crashes=0, reconnect_failures=0,
                  ran_seconds=2700, target_seconds=2700)
        assert not v.passed
        assert any("N-05" in r for r in v.reasons)

    def test_crash_and_reconnect_failures_fail(self):
        v = judge(self._ok_latency(), self._ok_memory(), crashes=1,
                  reconnect_failures=2, ran_seconds=2700, target_seconds=2700)
        assert not v.passed
        assert any("クラッシュ" in r for r in v.reasons)
        assert any("切断復元失敗" in r for r in v.reasons)

    def test_incomplete_run_fails(self):
        v = judge(self._ok_latency(), self._ok_memory(), crashes=0,
                  reconnect_failures=0, ran_seconds=1000, target_seconds=2700)
        assert not v.passed
        assert any("完走" in r for r in v.reasons)

    def test_no_captions_fails(self):
        v = judge(None, self._ok_memory(), crashes=0, reconnect_failures=0,
                  ran_seconds=2700, target_seconds=2700)
        assert not v.passed
        assert any("caption" in r for r in v.reasons)
