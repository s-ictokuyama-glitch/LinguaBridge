"""参加コード総当たり対策（plan.md E-09）。

同一IPからの連続失敗が上限に達したら一定時間 join を拒否する。
時計は注入可能（テストで待たずに検証するため）。
"""

from __future__ import annotations

import time
from typing import Callable


class JoinRateLimiter:
    def __init__(
        self,
        max_failures: int = 5,
        block_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._block_seconds = block_seconds
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}

    def is_blocked(self, ip: str) -> bool:
        until = self._blocked_until.get(ip)
        if until is None:
            return False
        if self._clock() >= until:
            del self._blocked_until[ip]
            return False
        return True

    def record_failure(self, ip: str) -> None:
        count = self._failures.get(ip, 0) + 1
        if count >= self._max_failures:
            self._blocked_until[ip] = self._clock() + self._block_seconds
            self._failures.pop(ip, None)  # ブロック解除後はゼロから数え直す
        else:
            self._failures[ip] = count

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)
