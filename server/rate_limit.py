"""参加コードと先生コードの総当たり対策（plan.md E-09）。

同一IPからの連続失敗が上限に達したら一定時間 join を拒否する。
時計は注入可能（テストで待たずに検証するため）。
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Callable, Literal


Role = Literal["student", "teacher"]


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
        self._failures: dict[str, Counter[Role]] = {}
        self._blocked_until: dict[str, float] = {}

    def is_blocked(self, ip: str) -> bool:
        until = self._blocked_until.get(ip)
        if until is None:
            return False
        if self._clock() >= until:
            del self._blocked_until[ip]
            return False
        return True

    def record_failure(self, ip: str, *, role: Role = "student") -> None:
        failures = self._failures.setdefault(ip, Counter())
        failures[role] += 1
        if failures.total() >= self._max_failures:
            self._blocked_until[ip] = self._clock() + self._block_seconds
            del self._failures[ip]

    def record_success(self, ip: str, *, role: Role = "student") -> None:
        failures = self._failures.get(ip)
        if failures is not None:
            failures.pop(role, None)
            if not failures:
                del self._failures[ip]
