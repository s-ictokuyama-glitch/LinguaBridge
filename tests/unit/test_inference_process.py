import asyncio
from functools import partial
import os
from pathlib import Path
import time
from types import MethodType

import psutil
import pytest

from server.inference import InferenceFailed, InferenceSucceeded, InferenceTimedOut
from server.inference_process import ProcessInferenceRunner
from server.model_files import TranslationModelError


class ProbeEngine:
    def __init__(self, directory, *, slow_start=False, fail_start=False):
        self.directory = Path(directory)
        self.slow_start = slow_start
        self.fail_start = fail_start

    def warmup(self):
        (self.directory / f"loaded-{os.getpid()}").touch()
        if self.fail_start:
            raise TranslationModelError("モデルを展開し直してください")
        if self.slow_start:
            self._wait("startup")

    def _wait(self, name):
        (self.directory / f"entered-{name}").write_text(str(os.getpid()))
        while not (self.directory / f"release-{name}").exists():
            time.sleep(0.01)

    def translate(self, text, lang):
        if text.startswith("hang"):
            self._wait(text)
        if text == "timeout-error":
            raise TimeoutError("model timeout")
        if text == "model-error":
            raise TranslationModelError("モデルを展開し直してください")
        if text == "crash":
            os._exit(23)
        if text == "unpicklable":
            return lambda: None
        return os.getpid(), text, lang


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def prepared(directory, **kwargs):
    factory = partial(ProbeEngine, directory, **kwargs)
    engine = factory()
    runner = ProcessInferenceRunner(engine, factory, name="test")
    result = await runner.run(engine.warmup, timeout=5)
    assert isinstance(result, InferenceSucceeded), result
    return engine, runner


def loaded_pids(directory):
    return [int(p.name.removeprefix("loaded-")) for p in directory.glob("loaded-*")]


def assert_reaped(directory):
    assert loaded_pids(directory)
    assert all(not psutil.pid_exists(pid) for pid in loaded_pids(directory))


def test_hang_timeout_reaps_old_process_and_next_request_succeeds(tmp_path):
    async def scenario():
        engine, runner = await prepared(tmp_path)
        try:
            result = await runner.run(partial(engine.translate, "hang-first", "en"), timeout=0.2)
            assert isinstance(result, InferenceTimedOut)
            old_pid = int((tmp_path / "entered-hang-first").read_text())
            assert not psutil.pid_exists(old_pid)
            assert not (tmp_path / "release-hang-first").exists()
            result = await runner.run(partial(engine.translate, "next", "zh"), timeout=5)
            assert isinstance(result, InferenceSucceeded)
            assert result.value[0] != old_pid
            assert result.value[1:] == ("next", "zh")
            assert len(loaded_pids(tmp_path)) == 2
        finally:
            await runner.aclose()
        assert_reaped(tmp_path)

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_waiting_request_cannot_retire_the_active_generation(tmp_path, cancel_waiter):
    async def scenario():
        engine, runner = await prepared(tmp_path)
        active = asyncio.create_task(runner.run(partial(engine.translate, "hang-active", "en"), timeout=None))
        try:
            await wait_for((tmp_path / "entered-hang-active").exists)
            pid = int((tmp_path / "entered-hang-active").read_text())
            waiting = asyncio.create_task(runner.run(partial(engine.translate, "queued", "zh"), timeout=0.05))
            if cancel_waiter:
                await asyncio.sleep(0)
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
            else:
                assert isinstance(await waiting, InferenceTimedOut)
            assert psutil.pid_exists(pid)
            assert not active.done()
            (tmp_path / "release-hang-active").touch()
            result = await active
            assert isinstance(result, InferenceSucceeded)
            assert result.value == (pid, "hang-active", "en")
            result = await runner.run(partial(engine.translate, "last", "zh"), timeout=1)
            assert result.value == (pid, "last", "zh")
        finally:
            await runner.aclose()
            await asyncio.gather(active, return_exceptions=True)
        assert_reaped(tmp_path)

    asyncio.run(scenario())


def test_cancelled_active_request_is_reaped_before_replacement(tmp_path):
    async def scenario():
        engine, runner = await prepared(tmp_path)
        task = asyncio.create_task(runner.run(partial(engine.translate, "hang-cancel", "en"), timeout=None))
        try:
            await wait_for((tmp_path / "entered-hang-cancel").exists)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert_reaped(tmp_path)
            outcome = await runner.run(partial(engine.translate, "next", "zh"), timeout=5)
            assert isinstance(outcome, InferenceSucceeded)
        finally:
            await runner.aclose()
        assert_reaped(tmp_path)

    asyncio.run(scenario())


def test_short_deadlines_share_startup_and_do_not_restart_it(tmp_path):
    async def scenario():
        factory = partial(ProbeEngine, tmp_path, slow_start=True)
        engine = factory()
        runner = ProcessInferenceRunner(engine, factory)
        try:
            first = await runner.run(partial(engine.translate, "first", "en"), timeout=0.05)
            assert isinstance(first, InferenceTimedOut)
            await wait_for((tmp_path / "entered-startup").exists)
            for _ in range(3):
                result = await runner.run(partial(engine.translate, "waiting", "zh"), timeout=0.05)
                assert isinstance(result, InferenceTimedOut)
            assert len(loaded_pids(tmp_path)) == 1
            (tmp_path / "release-startup").touch()
            result = await runner.run(partial(engine.translate, "next", "en"), timeout=5)
            assert isinstance(result, InferenceSucceeded)
            assert len(loaded_pids(tmp_path)) == 1
        finally:
            await runner.aclose()
        assert_reaped(tmp_path)

    asyncio.run(scenario())


@pytest.mark.parametrize("during_startup", [False, True])
@pytest.mark.parametrize("loop_factory", [None, asyncio.SelectorEventLoop])
def test_close_cancels_startup_or_active_hang_and_is_repeatable(tmp_path, during_startup, loop_factory):
    async def scenario():
        factory = partial(ProbeEngine, tmp_path, slow_start=during_startup)
        engine = factory()
        runner = ProcessInferenceRunner(engine, factory)
        active = asyncio.create_task(runner.run(partial(engine.translate, "hang-stop", "en"), timeout=None))
        marker = "startup" if during_startup else "hang-stop"
        await wait_for((tmp_path / f"entered-{marker}").exists)
        waiting = asyncio.create_task(runner.run(partial(engine.translate, "queued", "zh"), timeout=None))
        await asyncio.gather(runner.aclose(), runner.aclose())
        results = await asyncio.gather(active, waiting)
        assert all(isinstance(r, InferenceFailed) for r in results), results
        await runner.aclose()
        stopped = await runner.run(partial(engine.translate, "after-close", "en"), timeout=None)
        assert isinstance(stopped, InferenceFailed)
        assert_reaped(tmp_path)

    asyncio.run(scenario(), loop_factory=loop_factory)


def test_repeated_cancellation_does_not_interrupt_process_collection(tmp_path, monkeypatch):
    async def scenario():
        engine, runner = await prepared(tmp_path)
        task = asyncio.create_task(runner.run(partial(engine.translate, "hang-stop", "en"), timeout=None))
        await wait_for((tmp_path / "entered-hang-stop").exists)
        dispose = runner._dispose
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed(generation):
            entered.set()
            await release.wait()
            await dispose(generation)

        monkeypatch.setattr(runner, "_dispose", delayed)
        closing = asyncio.create_task(runner.aclose())
        await entered.wait()
        for _ in range(3):
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert_reaped(tmp_path)
        await runner.aclose()
        assert isinstance(await task, InferenceFailed)

    asyncio.run(scenario())


@pytest.mark.parametrize("operation,error_type", [
    ("timeout-error", TimeoutError), ("model-error", TranslationModelError), ("unpicklable", RuntimeError),
])
def test_model_failures_remain_failures_and_model_is_reused(tmp_path, operation, error_type):
    async def scenario():
        engine, runner = await prepared(tmp_path)
        try:
            for timeout in (None, 1):
                outcome = await runner.run(partial(engine.translate, operation, "en"), timeout=timeout)
                assert isinstance(outcome, InferenceFailed)
                assert isinstance(outcome.error, error_type)
            next_call = await runner.run(partial(engine.translate, "next", "zh"), timeout=1)
            assert isinstance(next_call, InferenceSucceeded)
            assert len(loaded_pids(tmp_path)) == 1
        finally:
            await runner.aclose()
        assert_reaped(tmp_path)

    asyncio.run(scenario())


def test_child_crash_is_reaped_and_next_request_succeeds(tmp_path):
    async def scenario():
        engine, runner = await prepared(tmp_path)
        try:
            result = await runner.run(partial(engine.translate, "crash", "en"), timeout=2)
            assert isinstance(result, InferenceFailed)
            assert_reaped(tmp_path)
            result = await runner.run(partial(engine.translate, "next", "zh"), timeout=5)
            assert isinstance(result, InferenceSucceeded)
        finally:
            await runner.aclose()
        assert_reaped(tmp_path)

    asyncio.run(scenario())


def test_startup_failure_preserves_public_model_error(tmp_path):
    async def scenario():
        factory = partial(ProbeEngine, tmp_path, fail_start=True)
        engine = factory()
        runner = ProcessInferenceRunner(engine, factory)
        try:
            result = await runner.run(engine.warmup, timeout=5)
            assert isinstance(result, InferenceFailed)
            assert isinstance(result.error, TranslationModelError)
            assert str(result.error) == "モデルを展開し直してください"
            assert_reaped(tmp_path)
        finally:
            await runner.aclose()

    asyncio.run(scenario())


def test_invalid_callable_or_factory_does_not_fall_back_to_threads(tmp_path):
    async def scenario():
        engine = ProbeEngine(tmp_path)
        runner = ProcessInferenceRunner(engine, lambda: engine)
        other = ProbeEngine(tmp_path)
        forged = MethodType(lambda self: None, engine)
        forged.__func__.__name__ = "warmup"
        try:
            for operation in (lambda: "bad", other.warmup, forged,
                              partial(engine.translate, text="bad", lang="en")):
                result = await runner.run(operation, timeout=1)
                assert isinstance(result, InferenceFailed)
                assert isinstance(result.error, ValueError)
            result = await runner.run(engine.warmup, timeout=1)
            assert isinstance(result, InferenceFailed)
            assert not loaded_pids(tmp_path)
        finally:
            await runner.aclose()

    asyncio.run(scenario())


def test_create_app_uses_processes_for_uninjected_fake_engines():
    from server.main import create_app
    from server.publication import Publication
    from tests.helpers import make_ws_test_config

    async def scenario():
        config = make_ws_test_config()
        config.asr.engine = "fake"
        config.mt.engine = "fake"
        pipeline = create_app(config, Publication(config.server)).state.pipeline
        assert isinstance(pipeline._asr_runner, ProcessInferenceRunner)
        assert isinstance(pipeline.captions._mt_runner, ProcessInferenceRunner)
        config.asr.engine = "changed-after-build"
        config.mt.engine = "changed-after-build"
        try:
            await pipeline.start()
            await asyncio.wait_for(pipeline._warmup_task, 5)
            assert pipeline.ready
            pids = [runner._generation.process.pid
                    for runner in (pipeline._asr_runner, pipeline.captions._mt_runner)]
            assert len(set(pids)) == 2
            assert all(pid != os.getpid() and psutil.pid_exists(pid) for pid in pids)
        finally:
            await pipeline.stop()
        assert not any(psutil.pid_exists(pid) for pid in pids)

    asyncio.run(scenario())


def test_explicit_engine_injection_preserves_local_runner():
    from server.asr.fake_engine import FakeASREngine
    from server.inference import InferenceRunner
    from server.main import create_app
    from server.publication import Publication
    from tests.helpers import make_ws_test_config

    async def scenario():
        config = make_ws_test_config()
        config.asr.engine = "fake"
        config.mt.engine = "fake"
        pipeline = create_app(config, Publication(config.server), asr_engine=FakeASREngine()).state.pipeline
        assert isinstance(pipeline._asr_runner, InferenceRunner)
        assert isinstance(pipeline.captions._mt_runner, ProcessInferenceRunner)
        await pipeline.stop()

    asyncio.run(scenario())
