"""同期のモデル呼び出しを、エンジンごとの直列実行先で待つ。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from time import monotonic
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class InferenceSucceeded(Generic[T]):
    value: T
    elapsed_ms: int


@dataclass(frozen=True)
class InferenceTimedOut:
    pass


@dataclass(frozen=True)
class InferenceFailed:
    error: Exception


class InferenceRunner:
    """実行先を所有する。注入する Executor もタスクを直列に実行すること。"""

    def __init__(self, executor: Executor | None = None, *, name: str = "") -> None:
        self._executor = (
            executor if executor is not None else ThreadPoolExecutor(1, thread_name_prefix=name)
        )

    async def run(
        self, operation: Callable[[], T], *, timeout: float | None
    ) -> InferenceSucceeded[T] | InferenceTimedOut | InferenceFailed:
        started = monotonic()
        try:
            future = self._executor.submit(operation)
        except Exception as error:
            return InferenceFailed(error)

        try:
            value = await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except TimeoutError as error:
            if timeout is None:
                return InferenceFailed(error)
            # 未着手ならキャンセルできる。実行中なら次の仕事も同じ実行先で順番を待つ。
            future.cancel()
            return InferenceTimedOut()
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as error:
            return InferenceFailed(error)
        return InferenceSucceeded(value, int((monotonic() - started) * 1000))

    def stop(self) -> None:
        # 実行中のスレッドは中断できないので、その終了を待たない。
        self._executor.shutdown(wait=False, cancel_futures=True)
