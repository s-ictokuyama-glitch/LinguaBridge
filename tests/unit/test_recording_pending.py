import asyncio
from concurrent.futures import Executor, Future
from contextlib import asynccontextmanager
import json

import pytest

from server.asr.fake_engine import FakeASREngine
from server.lesson import Lesson
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.recorder import SessionRecorder
from server.roster import Roster
from tests.helpers import RecordingSocket, make_ws_test_config, speech_pcm, utterance_bytes


class ControlledExecutor(Executor):
    def __init__(self):
        self.pending = []

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        future.set_running_or_notify_cancel()
        self.pending.append((future, fn, args, kwargs))
        return future

    def complete(self):
        future, fn, args, kwargs = self.pending.pop(0)
        future.set_result(fn(*args, **kwargs))


async def settle(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate(), "queued work did not settle"


@asynccontextmanager
async def classroom(tmp_path, monkeypatch, strategy="simple", recording=True):
    config = make_ws_test_config()
    config.recording.default_on = recording
    config.recording.out_dir = str(tmp_path)
    config.turn.strategy = strategy
    pipeline = Pipeline(Roster(), config, FakeASREngine(), FakeTranslationEngine(["en"]))
    asr, mt = ControlledExecutor(), ControlledExecutor()
    pipeline._asr_executor.shutdown()
    pipeline._mt_executor.shutdown()
    pipeline._asr_executor, pipeline._mt_executor = asr, mt
    pipeline._tasks = [asyncio.create_task(pipeline._asr_worker()),
                       asyncio.create_task(pipeline._mt_worker())]
    lesson = Lesson(pipeline, config, join_code="1234")
    teacher = RecordingSocket()
    await pipeline.roster.add(teacher, "teacher")
    await pipeline.roster.add(RecordingSocket(), "student", "en")
    wait_for = asyncio.wait_for

    async def short_recording_wait(awaitable, timeout):
        if timeout == 30:
            await asyncio.sleep(0)
            if asr.pending or mt.pending:
                awaitable.cancel()
                raise TimeoutError
        return await wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", short_recording_wait)
    try:
        await lesson.control("start")
        yield lesson, teacher, asr, mt
    finally:
        await pipeline.stop()


async def speak(lesson, key):
    for chunk in utterance_bytes(key):
        await lesson.pipeline.feed_audio(chunk)


async def finish(asr, mt, pipeline):
    await settle(lambda: bool(asr.pending))
    asr.complete()
    await settle(lambda: bool(mt.pending))
    mt.complete()
    await pipeline.drain(timeout=1)
    await settle(lambda: not pipeline.recording_pending)


def recordings(tmp_path):
    return {
        path.parent: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for path in tmp_path.rglob("transcript.jsonl")
    }


@pytest.mark.parametrize("strategy", ["simple", "morph"])
@pytest.mark.parametrize("restart", [None, "start", "pause"])
def test_late_asr_is_saved_to_old_lesson_without_another_end(
    tmp_path, monkeypatch, strategy, restart
):
    async def scenario():
        async with classroom(tmp_path, monkeypatch, strategy) as (lesson, teacher, asr, mt):
            await speak(lesson, 1000)
            await finish(asr, mt, lesson.pipeline)
            await lesson.pipeline.feed_audio(speech_pcm(3000, 0.4).tobytes())
            await lesson.control("end")
            directory, entries = next(iter(recordings(tmp_path).items()))
            assert [entry["seq"] for entry in entries] == [1]
            if restart is not None:
                await lesson.control(restart)
                assert lesson.state == "ended", "pending recording accepted restart"
            await finish(asr, mt, lesson.pipeline)
            entries = recordings(tmp_path)[directory]
            assert [entry["ja"] for entry in entries] == [
                FakeASREngine.PHRASES[1000], FakeASREngine.PHRASES[3000]
            ]
            assert all(entry["translations"]["en"]["text"] for entry in entries)
            assert any(message.get("code") == "recording_pending" for message in teacher.sent)
            assert teacher.closed_with is None
            before = {path: path.read_bytes() for path in directory.iterdir()}
            await lesson.control(restart or "start")
            if restart == "pause":
                await lesson.control("start")
            await speak(lesson, 2000)
            await finish(asr, mt, lesson.pipeline)
            await lesson.control("end")
            assert len(recordings(tmp_path)) == 2
            assert before == {path: path.read_bytes() for path in directory.iterdir()}
    asyncio.run(scenario())


def test_late_translation_rewrites_provisional_files_without_another_control(tmp_path, monkeypatch):
    async def scenario():
        async with classroom(tmp_path, monkeypatch) as (lesson, _, asr, mt):
            await speak(lesson, 1000)
            await settle(lambda: bool(asr.pending))
            asr.complete()
            await settle(lambda: bool(mt.pending))
            await lesson.control("end")
            directory, entries = next(iter(recordings(tmp_path).items()))
            assert entries[0]["translations"] == {}
            mt.complete()
            await lesson.pipeline.drain(timeout=1)
            await settle(lambda: not lesson.pipeline.recording_pending)
            entries = recordings(tmp_path)[directory]
            assert set(entries[0]["translations"]) == {"en"}
            assert entries[0]["translations"]["en"]["text"] == f"[en] {FakeASREngine.PHRASES[1000]}"
            assert (directory / "transcript.en.md").exists()
            assert len(recordings(tmp_path)) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("restart", ["start", "pause"])
def test_recording_off_still_blocks_restart_until_old_asr_and_morph_flush_finish(
    tmp_path, monkeypatch, restart
):
    async def scenario():
        async with classroom(tmp_path, monkeypatch, "morph", recording=False) as (lesson, _, asr, mt):
            await lesson.pipeline.feed_audio(speech_pcm(3000, 0.4).tobytes())
            await lesson.control("end")
            await lesson.control(restart)
            assert lesson.state == "ended", "unrecorded pending audio accepted restart"
            await finish(asr, mt, lesson.pipeline)
            assert recordings(tmp_path) == {}
            await lesson.control("start")
            lesson.set_recording(True)
            await speak(lesson, 2000)
            await finish(asr, mt, lesson.pipeline)
            await lesson.control("end")
            entries = next(iter(recordings(tmp_path).values()))
            assert [entry["ja"] for entry in entries] == [FakeASREngine.PHRASES[2000]]
    asyncio.run(scenario())


def test_failed_background_save_notifies_and_retries_same_directory(tmp_path, monkeypatch):
    async def scenario():
        async with classroom(tmp_path, monkeypatch) as (lesson, teacher, asr, mt):
            await speak(lesson, 1000)
            await finish(asr, mt, lesson.pipeline)
            await lesson.pipeline.feed_audio(speech_pcm(2000, 0.4).tobytes())
            await lesson.control("end")
            directory = next(iter(recordings(tmp_path)))
            with monkeypatch.context() as patch:
                def fail_markdown(*args):
                    raise OSError("disk unavailable")
                patch.setattr(SessionRecorder, "_write_markdown", fail_markdown)
                await finish(asr, mt, lesson.pipeline)
                await settle(lambda: any(message.get("code") == "recording_save_failed" for message in teacher.sent))
                assert any(message.get("code") == "recording_save_failed" for message in teacher.sent)
                with pytest.raises(OSError, match="disk unavailable"):
                    await lesson.control("start")
                assert lesson.state == "ended"
            await lesson.control("start")
            assert list(recordings(tmp_path)) == [directory]
            assert [entry["seq"] for entry in recordings(tmp_path)[directory]] == [1, 2]
    asyncio.run(scenario())


def test_stop_cancels_pending_recording_finalizer(tmp_path, monkeypatch):
    async def scenario():
        async with classroom(tmp_path, monkeypatch) as (lesson, _, _, _):
            await lesson.pipeline.feed_audio(speech_pcm(1000, 0.4).tobytes())
            await lesson.control("end")
            assert lesson.pipeline.recording_pending
            tasks = asyncio.all_tasks() - {asyncio.current_task()}
            await lesson.pipeline.stop()
            assert not lesson.pipeline.recording_pending
            assert all(task.done() for task in tasks)
    asyncio.run(scenario())


@pytest.mark.parametrize("strategy", ["simple", "morph"])
def test_two_pending_asr_segments_finish_before_final_save(tmp_path, monkeypatch, strategy):
    async def scenario():
        async with classroom(tmp_path, monkeypatch, strategy) as (lesson, _, asr, mt):
            await speak(lesson, 1000)
            await lesson.pipeline.feed_audio(speech_pcm(2000, 0.4).tobytes())
            await lesson.control("end")
            assert recordings(tmp_path) == {}
            asr.complete()
            await settle(lambda: bool(asr.pending) and bool(mt.pending))
            mt.complete()
            assert lesson.pipeline.recording_pending
            assert recordings(tmp_path) == {}
            await finish(asr, mt, lesson.pipeline)
            entries = next(iter(recordings(tmp_path).values()))
            assert [entry["ja"] for entry in entries] == [
                FakeASREngine.PHRASES[1000], FakeASREngine.PHRASES[2000]
            ]
            assert all(entry["translations"]["en"]["text"] for entry in entries)
    asyncio.run(scenario())


def test_restart_callers_share_finalization_without_replacing_new_recording(tmp_path, monkeypatch):
    async def scenario():
        wait_for = asyncio.wait_for
        async with classroom(tmp_path, monkeypatch) as (lesson, _, asr, mt):
            await lesson.pipeline.feed_audio(speech_pcm(1000, 0.4).tobytes())
            await lesson.control("end")
            finalize = lesson.pipeline._finalize_lesson
            calls = 0
            release_second = asyncio.Event()

            async def staggered_finalize():
                nonlocal calls
                calls += 1
                second = calls == 2
                result = await finalize()
                if second:
                    await release_second.wait()
                return result

            with monkeypatch.context() as patch:
                patch.setattr(asyncio, "wait_for", wait_for)
                patch.setattr(lesson.pipeline, "_finalize_lesson", staggered_finalize)
                first = asyncio.create_task(lesson.control("start"))
                second = asyncio.create_task(lesson.control("start"))
                try:
                    await settle(lambda: calls == 2)
                    await finish(asr, mt, lesson.pipeline)
                    await first
                    await speak(lesson, 2000)
                    await finish(asr, mt, lesson.pipeline)
                    release_second.set()
                    await second
                finally:
                    release_second.set()
                    await asyncio.gather(first, second, return_exceptions=True)
            await lesson.control("end")
            assert sorted([entry["seq"] for entry in entries]
                          for entries in recordings(tmp_path).values()) == [[1], [2]]
    asyncio.run(scenario())
