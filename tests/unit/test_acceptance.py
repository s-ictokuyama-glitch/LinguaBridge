"""性能受け入れ判定ロジック（scripts/acceptance.py）のユニットテスト（#17）。"""

from __future__ import annotations

from scripts.acceptance import (
    LATENCY_MAX_LIMIT_S,
    DroppedAudio,
    LATENCY_MEDIAN_LIMIT_S,
    RSS_LIMIT_MB,
    judge,
    latency_drift,
    latency_stats,
    memory_trend,
    resource_trend,
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


class TestLatencyDrift:
    """遅延ドリフト（#32）。長時間試験で「だんだん遅くなる」を機械判定する。"""

    def test_none_when_only_one_window(self):
        # 5分窓に1つしか入らない＝推移が取れない
        assert latency_drift([(t, 3000) for t in range(0, 200, 10)]) is None

    def test_stable_latency_not_drifting(self):
        samples = [(float(t), 3000) for t in range(0, 1800, 10)]
        d = latency_drift(samples)
        assert d is not None
        assert len(d.windows) == 6  # 1800s / 300s
        assert not d.drifting
        assert d.increase_s == 0.0

    def test_growing_latency_flagged(self):
        # 窓ごとに 1s ずつ増える（最初 1s → 最後 6s）
        samples = [(float(t), 1000 + (t // 300) * 1000) for t in range(0, 1800, 10)]
        d = latency_drift(samples)
        assert d is not None
        assert d.drifting
        assert d.first_median_s == 1.0
        assert d.last_median_s == 6.0

    def test_small_wobble_not_drift(self):
        # +0.3s のゆらぎは絶対閾値未満なのでドリフトとみなさない
        samples = [(float(t), 3000 + (300 if t >= 900 else 0)) for t in range(0, 1800, 10)]
        d = latency_drift(samples)
        assert d is not None
        assert not d.drifting

    def test_empty_windows_are_counted_not_averaged(self):
        # 300..600s に caption が1件も無い＝黙った窓。中央値には混ぜず件数だけ残す
        samples = [(float(t), 3000) for t in list(range(0, 300, 10)) + list(range(600, 900, 10))]
        d = latency_drift(samples)
        assert d is not None
        assert d.empty_windows == 1
        assert len(d.windows) == 2


class TestResourceTrend:
    """スレッド・ハンドル・asyncioタスクのリーク検出（#32）。"""

    def test_none_when_too_few_samples(self):
        assert resource_trend("threads", [10, 10, 10]) is None

    def test_stable_not_growing(self):
        t = resource_trend("threads", [24.0] * 20)
        assert t is not None
        assert not t.growing
        assert t.peak == 24.0

    def test_monotonic_growth_flagged(self):
        # 30 → 90（+60本, +200%）＝明らかなリーク
        t = resource_trend("handles", [30.0] * 10 + [90.0] * 10)
        assert t is not None
        assert t.growing
        assert t.increase == 60.0

    def test_small_wobble_not_leak(self):
        # +5 程度のゆらぎは絶対閾値未満
        t = resource_trend("tasks", [40.0] * 10 + [45.0] * 10)
        assert t is not None
        assert not t.growing


class TestJudgeEndurance:
    """長時間試験の追加判定（#32）。既存の N-01/N-05 の意味は変えない。"""

    def _ok(self, **kw):
        return judge(
            latency_stats([3000, 3500, 4000]),
            memory_trend([2000.0] * 20),
            crashes=0,
            reconnect_failures=0,
            ran_seconds=3600,
            target_seconds=3600,
            **kw,
        )

    def test_drift_and_resources_are_optional(self):
        assert self._ok().passed  # 既存の呼び出し形は不変

    def test_drifting_latency_fails(self):
        drift = latency_drift([(float(t), 1000 + (t // 300) * 1000) for t in range(0, 1800, 10)])
        v = self._ok(drift=drift)
        assert not v.passed
        assert any("ドリフト" in r for r in v.reasons)

    def test_growing_resource_fails(self):
        growing = resource_trend("handles", [30.0] * 10 + [90.0] * 10)
        v = self._ok(resources=[growing])
        assert not v.passed
        assert any("handles" in r for r in v.reasons)

    def test_dropped_audio_fails(self):
        v = self._ok(dropped=DroppedAudio(segments=3, seconds=7.5))
        assert not v.passed
        assert any("破棄" in r for r in v.reasons)

    def test_rejected_frames_fail_separately(self):
        """容量側の破棄と不正入力側の破棄は別の理由として出る。"""
        v = self._ok(dropped=DroppedAudio(frames_rejected=2))
        assert not v.passed
        assert any("上限超過" in r for r in v.reasons)
        assert not any("処理が追いつかなかった" in r for r in v.reasons)

    def test_no_loss_passes(self):
        assert self._ok(dropped=DroppedAudio()).passed

    def test_healthy_endurance_passes(self):
        drift = latency_drift([(float(t), 3000) for t in range(0, 1800, 10)])
        stable = [resource_trend("threads", [24.0] * 20), resource_trend("tasks", [40.0] * 20)]
        v = self._ok(drift=drift, resources=stable, dropped=DroppedAudio())
        assert v.passed
