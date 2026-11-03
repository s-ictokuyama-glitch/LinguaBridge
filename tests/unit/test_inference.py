import asyncio
from collections import deque
from concurrent.futures import Executor, Future

import pytest

from server.inference import InferenceFailed, InferenceRunner, InferenceSucceeded, InferenceTimedOut


class ControlledExecutor(Executor):
    def __init__(self):
        self.active = None
        self.queued = deque()
        self.submissions = asyncio.Queue()
        self.stopped = False

    def submit(self, operation):
        if self.stopped:
            raise RuntimeError("executor stopped")
        future = Future()
        self.queued.append((future, operation))
        self._start_next()
        self.submissions.put_nowait(future)
        return future

    def _start_next(self):
        while self.active is None and self.queued:
            future, operation = self.queued.popleft()
            if future.set_running_or_notify_cancel():
                self.active = (future, operation)

    def complete(self):
        future, operation = self.active
        try:
            value = operation()
        except Exception as error:
            future.set_exception(error)
        else:
            future.set_result(value)
        self.active = None
        self._start_next()

    def shutdown(self, wait=True, *, cancel_futures=False):
        assert not wait, "shutdown must return while the active call is running"
        self.stopped = True
        if cancel_futures:
            while self.queued:
                future, _ = self.queued.popleft()
                future.cancel()


def test_success_measures_submission_to_completion(monkeypatch):
    async def scenario():
        executor = ControlledExecutor()
        runner = InferenceRunner(executor)
        now = 1.0
        monkeypatch.setattr("server.inference.monotonic", lambda: now)
        assert await runner.run(lambda: None, timeout=0) == InferenceTimedOut()
        await executor.submissions.get()
        running = asyncio.create_task(runner.run(lambda: "result", timeout=1))
        await executor.submissions.get()

        now = 1.0625
        executor.complete()
        now = 1.125
        executor.complete()

        assert await running == InferenceSucceeded("result", elapsed_ms=125)
        runner.stop()

    asyncio.run(scenario())


def test_operation_error_is_returned():
    error = ValueError("invalid input")

    def fail():
        raise error

    async def scenario():
        executor = ControlledExecutor()
        runner = InferenceRunner(executor)
        running = asyncio.create_task(runner.run(fail, timeout=1))
        await executor.submissions.get()

        executor.complete()

        assert await running == InferenceFailed(error)
        runner.stop()

    asyncio.run(scenario())


def test_timeout_keeps_running_call_and_cancels_queued_call():
    async def scenario():
        executor = ControlledExecutor()
        runner = InferenceRunner(executor)
        completed = []

        assert await runner.run(lambda: completed.append("late"), timeout=0) == InferenceTimedOut()
        active = await executor.submissions.get()
        assert active.running()

        assert await runner.run(lambda: completed.append("dropped"), timeout=0) == InferenceTimedOut()
        queued = await executor.submissions.get()
        assert queued.cancelled()

        next_call = asyncio.create_task(runner.run(lambda: completed.append("next"), timeout=1))
        next_future = await executor.submissions.get()
        assert not next_future.running()
        executor.complete()
        assert next_future.running()
        executor.complete()

        assert isinstance(await next_call, InferenceSucceeded)
        assert completed == ["late", "next"]
        runner.stop()

    asyncio.run(scenario())


def test_stop_cancels_queued_call_without_waiting_for_active_call():
    async def scenario():
        executor = ControlledExecutor()
        runner = InferenceRunner(executor)
        running = asyncio.create_task(runner.run(lambda: "result", timeout=None))
        active = await executor.submissions.get()
        waiting = asyncio.create_task(runner.run(lambda: pytest.fail("queued operation ran"), timeout=None))
        queued = await executor.submissions.get()

        runner.stop()

        assert active.running()
        assert queued.cancelled()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        executor.complete()
        assert isinstance(await running, InferenceSucceeded)
        result = await runner.run(lambda: pytest.fail("stopped executor submitted work"), timeout=None)
        assert isinstance(result, InferenceFailed)
        assert str(result.error) == "executor stopped"

    asyncio.run(scenario())
