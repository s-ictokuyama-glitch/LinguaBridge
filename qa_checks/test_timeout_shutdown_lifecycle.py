"""SAFE-03 / SAFE-09 の推論タイムアウトと停止時タスク寿命を検証する。"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable

import numpy as np

from server.asr.base import ASREngine, ASRResult
from server.asr.fake_engine import FakeASREngine
from server.main import create_app
from server.mt.base import TranslationEngine
from server.mt.fake_engine import FakeTranslationEngine
from server.caption_history import Utterance
from server.pipeline import MTJob, Pipeline, _LIVE_PRIORITY
from server.lesson import Lesson
from server.roster import Roster
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.helpers import utterance_bytes


class _RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed_with: int | None = None

    async def send_json(self, data: object) -> None:
        self.sent.append(dict(data))  # type: ignore[arg-type]

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


class _FirstASRCallBlocks(ASREngine):
    """最初の推論だけ固着し、解放後の呼び出しは即座に成功するASR。"""

    def __init__(self) -> None:
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def transcribe(self, pcm16: np.ndarray, sample_rate: int) -> ASRResult:
        del pcm16, sample_rate
        with self._lock:
            call = self.calls
            self.calls += 1
        if call == 0:
            self.first_started.set()
            self.release_first.wait()
            return ASRResult(text="タイムアウト済み")
        return ASRResult(text="後続の認識")


class _FirstMTCallBlocks(TranslationEngine):
    """最初の翻訳だけ固着し、解放後の呼び出しは即座に成功するMT。"""

    def __init__(self) -> None:
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    @property
    def model_version(self) -> str:
        return "first-call-blocks-v1"

    def translate(self, text_ja: str, target_lang: str) -> str:
        with self._lock:
            call = self.calls
            self.calls += 1
        if call == 0:
            self.first_started.set()
            self.release_first.wait()
            return f"[{target_lang}] タイムアウト済み"
        return f"[{target_lang}] 後続: {text_ja}"

    def supported_languages(self) -> list[str]:
        return ["en"]


async def _wait_until(
    predicate: Callable[[], bool], *, timeout_s: float = 2.0
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("条件成立を待ってタイムアウトしました")
        await asyncio.sleep(0.005)


def test_asr_timeout_releases_worker_loop_and_followup_can_complete() -> None:
    async def scenario() -> None:
        asr = _FirstASRCallBlocks()
        teacher = _RecordingSocket()
        config = make_ws_test_config()
        config.limits.asr_timeout_s = 0.2
        roster = Roster(config.limits)
        await roster.add(teacher, "teacher")
        pipeline = Pipeline(
            roster,
            config,
            asr,
            FakeTranslationEngine(["en"]),
        )
        lesson = Lesson(pipeline, config, join_code=JOIN_CODE)
        await lesson.control("start")
        await pipeline.start()
        asr_worker = next(task for task in pipeline._tasks if task.get_name() == "asr-worker")
        try:
            for chunk in utterance_bytes(1000):
                await pipeline.feed_audio(chunk)
            await _wait_until(asr.first_started.is_set)
            await _wait_until(
                lambda: any(message.get("code") == "asr_timeout" for message in teacher.sent)
            )

            for chunk in utterance_bytes(2000):
                await pipeline.feed_audio(chunk)

            # 1本目のexecutor threadはまだ固着中。それでもasync workerは次を取り出せる。
            await _wait_until(lambda: pipeline.asr_calls == 2)
            assert asr.release_first.is_set() is False
            assert asr_worker.done() is False

            asr.release_first.set()
            await _wait_until(
                lambda: any(
                    message.get("type") == "asr_final"
                    and message.get("ja") == "後続の認識"
                    for message in teacher.sent
                )
            )
        finally:
            asr.release_first.set()
            await pipeline.stop()

    asyncio.run(scenario())


def test_mt_timeout_releases_worker_loop_and_followup_can_complete() -> None:
    async def scenario() -> None:
        mt = _FirstMTCallBlocks()
        teacher = _RecordingSocket()
        student = _RecordingSocket()
        config = make_ws_test_config()
        config.limits.mt_timeout_s = 0.2
        roster = Roster(config.limits)
        await roster.add(teacher, "teacher")
        await roster.add(student, "student", "en")
        pipeline = Pipeline(roster, config, FakeASREngine(), mt)
        lesson = Lesson(pipeline, config, join_code=JOIN_CODE)
        await lesson.control("start")
        await pipeline.start()
        mt_worker = next(task for task in pipeline._tasks if task.get_name() == "mt-worker")
        try:
            for chunk in utterance_bytes(1000):
                await pipeline.feed_audio(chunk)
            await _wait_until(mt.first_started.is_set)
            await _wait_until(
                lambda: any(message.get("code") == "mt_timeout" for message in teacher.sent)
            )

            for chunk in utterance_bytes(2000):
                await pipeline.feed_audio(chunk)

            # executorは1本目の翻訳を実行中だが、async workerは次ジョブへ進める。
            await _wait_until(lambda: pipeline.mt_calls == 2)
            assert mt.release_first.is_set() is False
            assert mt_worker.done() is False

            mt.release_first.set()
            await _wait_until(
                lambda: any(
                    message.get("type") == "caption"
                    and message.get("ja") == "光合成には日光が必要です。"
                    and message.get("text")
                    == "[en] 後続: 光合成には日光が必要です。"
                    for message in student.sent
                )
            )
        finally:
            mt.release_first.set()
            await pipeline.stop()

    asyncio.run(scenario())


def test_lifespan_shutdown_closes_workers_and_senders_with_pending_mt() -> None:
    async def scenario() -> None:
        mt = _FirstMTCallBlocks()
        config = make_ws_test_config()
        config.limits.mt_timeout_s = 60.0
        app = create_app(
            config,
            asr_engine=FakeASREngine(),
            mt_engine=mt,
            join_code=JOIN_CODE,
        )
        lifespan = app.router.lifespan_context(app)
        pipeline = app.state.pipeline
        entered = False
        exited = False
        try:
            await lifespan.__aenter__()
            entered = True
            lesson = app.state.lesson
            await lesson.roster.add(_RecordingSocket(), "teacher")
            await lesson.roster.add(_RecordingSocket(), "student", "en")
            await lesson.control("start")

            worker_tasks = tuple(pipeline._tasks)
            sender_tasks = tuple(
                task for task in asyncio.all_tasks() if task.get_name().startswith("sender-")
            )
            assert {task.get_name() for task in worker_tasks} == {
                "asr-worker",
                "mt-worker",
                "stats-worker",
            }
            assert len(sender_tasks) == 2
            assert all(task.done() is False for task in (*worker_tasks, *sender_tasks))

            utterance = Utterance(
                seq=1,
                t_start=0.0,
                t_end=1.0,
                text_ja="終了を待つ翻訳",
                asr_ms=1,
            )
            await pipeline._enqueue_mt(
                _LIVE_PRIORITY,
                MTJob(utterance, "en", time.monotonic()),
            )
            await _wait_until(mt.first_started.is_set)
            mt_worker = next(task for task in worker_tasks if task.get_name() == "mt-worker")
            assert mt_worker.done() is False

            # executor threadは解放しない。lifespanは管理下のasync taskだけを有限時間で畳む。
            await asyncio.wait_for(lifespan.__aexit__(None, None, None), timeout=2.0)
            exited = True
            assert mt.release_first.is_set() is False
            assert all(task.done() for task in (*worker_tasks, *sender_tasks))
            assert pipeline._tasks == []
            assert pipeline._warmup_task is None
            assert not lesson.roster.clients
        finally:
            mt.release_first.set()
            if entered and not exited:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(pipeline.stop(), timeout=2.0)

    asyncio.run(scenario())
