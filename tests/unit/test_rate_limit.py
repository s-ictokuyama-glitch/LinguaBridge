"""参加コード総当たり対策（E-09）のユニットテスト。時計を注入して待たずに検証する。"""

from __future__ import annotations

from server.rate_limit import JoinRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make() -> tuple[JoinRateLimiter, FakeClock]:
    clock = FakeClock()
    return JoinRateLimiter(max_failures=5, block_seconds=60, clock=clock), clock


def test_not_blocked_below_threshold():
    limiter, _ = make()
    for _ in range(4):
        limiter.record_failure("ip1")
    assert not limiter.is_blocked("ip1")


def test_blocked_at_threshold():
    limiter, _ = make()
    for _ in range(5):
        limiter.record_failure("ip1")
    assert limiter.is_blocked("ip1")
    assert not limiter.is_blocked("ip2")  # 他IPは影響を受けない


def test_unblocked_after_block_period():
    limiter, clock = make()
    for _ in range(5):
        limiter.record_failure("ip1")
    clock.now = 59.9
    assert limiter.is_blocked("ip1")
    clock.now = 60.0
    assert not limiter.is_blocked("ip1")
    # 解除後はゼロから数え直し（1回の失敗で即ブロックされない）
    limiter.record_failure("ip1")
    assert not limiter.is_blocked("ip1")


def test_success_resets_failure_count():
    limiter, _ = make()
    for _ in range(4):
        limiter.record_failure("ip1")
    limiter.record_success("ip1")
    limiter.record_failure("ip1")
    assert not limiter.is_blocked("ip1")
