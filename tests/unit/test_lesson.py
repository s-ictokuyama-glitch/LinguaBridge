import asyncio
from contextlib import asynccontextmanager
from functools import wraps

import pytest

from server import ws_protocol as proto
from server.asr.fake_engine import FakeASREngine
from server.lesson import Lesson
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.roster import Roster
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.helpers import RecordingSocket, speech_pcm, utterance_bytes, wait_for
from tests.unit.test_rate_limit import FakeClock


def run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


@asynccontextmanager
async def classroom(config=None, **kwargs):
    config = config or make_ws_test_config()
    pipeline = Pipeline(Roster(config.limits), config, FakeASREngine(),
                        FakeTranslationEngine(config.language_codes))
    lesson = Lesson(pipeline, config, join_code=JOIN_CODE, **kwargs)
    await pipeline.start()
    try:
        yield lesson
    finally:
        await pipeline.stop()


async def join(lesson, role="student", lang="en", current=None, socket=None, **kwargs):
    socket = socket or RecordingSocket()
    client = await lesson.join(socket, current, proto.JoinMessage(
        type="join", role=role, lang=lang, code=kwargs.pop("code", JOIN_CODE), **kwargs
    ), "test-ip")
    return client, socket


async def speak(lesson, key=1000):
    for chunk in utterance_bytes(key):
        await lesson.pipeline.feed_audio(chunk)


@run_async
async def test_join_initial_state_and_validation():
    async with classroom() as lesson:
        client, socket = await join(lesson)
        msg = await wait_for(socket, "joined")
        assert (msg["seq_head"], msg["history_from"], msg["session_state"]) == (0, 1, "idle")
        assert [lang["code"] for lang in msg["languages"]] == ["en", "zh"]
        assert msg["speaking"] is False and msg["recording"] is False
        for options, reason in [({"code": "0000"}, "bad_code"),
                                ({"lang": "fr"}, "bad_lang"),
                                ({"lang": None}, "bad_lang")]:
            rejected, same = await join(lesson, current=client, socket=socket, **options)
            assert rejected is client
            assert (await wait_for(same, "join_rejected"))["reason"] == reason
        rejected, socket = await join(lesson, role="teacher", code="0000")
        assert rejected is None
        assert (await wait_for(socket, "join_rejected"))["reason"] == "bad_code"


@run_async
async def test_capacity_rejoin_and_teacher_do_not_consume_seats():
    config = make_ws_test_config()
    config.limits.max_students = 1
    async with classroom(config) as lesson:
        client, socket = await join(lesson)
        await wait_for(socket, "joined")
        current, _ = await join(lesson, current=client, socket=socket)
        assert current is not client
        await wait_for(socket, "joined")
        assert len(lesson.roster.students()) == 1
        rejected, other = await join(lesson)
        assert rejected is None
        assert (await wait_for(other, "join_rejected"))["reason"] == "full"
        teacher, _ = await join(lesson, role="teacher")
        assert teacher is not None


@run_async
async def test_rate_limit_clock_and_validation_order():
    clock = FakeClock()
    async with classroom(clock=clock) as lesson:
        for _ in range(5):
            _, socket = await join(lesson, code="0000", lang="fr")
            assert (await wait_for(socket, "join_rejected"))["reason"] == "bad_code"
        _, socket = await join(lesson)
        assert (await wait_for(socket, "join_rejected"))["reason"] == "rate_limited"
        clock.now = 60.0
        client, socket = await join(lesson)
        assert client is not None
        await wait_for(socket, "joined")
        for _ in range(4):
            await join(lesson, code="0000")
        _, socket = await join(lesson, lang="fr")
        assert (await wait_for(socket, "join_rejected"))["reason"] == "bad_lang"
        for _ in range(4):
            await join(lesson, code="0000")
        current, socket = await join(lesson)
        assert current is not None


@run_async
async def test_disconnect_auto_resume_and_repeated_leave():
    async with classroom() as lesson:
        _, student = await join(lesson)
        teacher, _ = await join(lesson, role="teacher")
        await lesson.control("start")
        await wait_for(student, "session", state="live")
        await lesson.roster.remove(teacher)
        await lesson.leave(teacher)
        await lesson.leave(teacher)
        await wait_for(student, "session", state="paused")
        assert lesson.auto_paused
        _, replacement = await join(lesson, role="teacher")
        assert (await wait_for(replacement, "joined"))["session_state"] == "paused"
        await wait_for(replacement, "session", state="live")
        await wait_for(student, "session", state="live")
        await speak(lesson)
        assert (await wait_for(student, "caption"))["seq"] == 1


@run_async
async def test_manual_pause_does_not_resume_and_ignores_audio():
    async with classroom() as lesson:
        _, student = await join(lesson)
        teacher, _ = await join(lesson, role="teacher")
        await lesson.control("start")
        await lesson.control("pause")
        await wait_for(student, "session", state="paused")
        await lesson.leave(teacher)
        _, replacement = await join(lesson, role="teacher")
        assert (await wait_for(replacement, "joined"))["session_state"] == "paused"
        await speak(lesson, 1000)
        await lesson.control("start")
        await speak(lesson, 2000)
        caption = await wait_for(student, "caption")
        assert caption["seq"] == 1 and caption["ja"] == "光合成には日光が必要です。"


@run_async
async def test_teacher_last_wins_and_old_leave_cannot_pause():
    async with classroom() as lesson:
        teacher, old = await join(lesson, role="teacher")
        await lesson.control("start")
        current, new = await join(lesson, role="teacher")
        assert old.closed_with == 4000
        await lesson.leave(teacher)
        assert lesson.roster.teacher() is current
        assert lesson.state == "live"
        assert (await wait_for(new, "joined"))["session_state"] == "live"


@run_async
async def test_pause_finalizes_speech_before_silence_without_waiting_for_asr():
    async with classroom() as lesson:
        _, socket = await join(lesson)
        await lesson.control("start")
        await lesson.pipeline.feed_audio(speech_pcm(2000, 0.5).tobytes())
        await lesson.control("pause")
        await wait_for(socket, "session", state="paused")
        assert (await wait_for(socket, "caption"))["ja"] == "光合成には日光が必要です。"
        assert not lesson.pipeline.speaking


@run_async
async def test_end_drains_last_utterance_and_writes_translation(tmp_path):
    config = make_ws_test_config()
    config.recording.out_dir = str(tmp_path)
    async with classroom(config) as lesson:
        _, student = await join(lesson)
        _, teacher = await join(lesson, role="teacher")
        lesson.set_recording(True)
        assert (await wait_for(student, "recording"))["on"]
        assert (await wait_for(teacher, "recording"))["on"]
        await lesson.control("start")
        await lesson.pipeline.feed_audio(speech_pcm(2000, 0.5).tobytes())
        saved = await lesson.control("end")
        assert saved is not None
        await wait_for(student, "session", state="ended")
        caption = await wait_for(student, "caption")
        assert caption["seq"] == 1
        contents = "\n".join(p.read_text(encoding="utf-8") for p in saved.glob("*"))
        assert caption["text"] in contents
        lesson.set_recording(False)
        assert not (await wait_for(student, "recording"))["on"]
        assert not (await wait_for(teacher, "recording"))["on"]


@run_async
async def test_set_lang_routes_next_caption_and_invalid_language_keeps_choice():
    async with classroom() as lesson:
        client, socket = await join(lesson)
        await lesson.control("start")
        await lesson.set_lang(client, "fr")
        assert (await wait_for(socket, "error"))["code"] == "bad_lang"
        await speak(lesson)
        assert (await wait_for(socket, "caption"))["lang"] == "en"
        await lesson.set_lang(client, "zh")
        await speak(lesson, 2000)
        assert (await wait_for(socket, "caption"))["lang"] == "zh"


@pytest.mark.parametrize("last_seq", range(6))
@run_async
async def test_replay_is_exact_missing_range(last_seq):
    async with classroom() as lesson:
        _, teacher = await join(lesson, role="teacher")
        await lesson.control("start")
        for seq in range(1, 6):
            await speak(lesson)
            await wait_for(teacher, "asr_final", seq=seq)
        _, socket = await join(lesson, last_seq=last_seq)
        assert (await wait_for(socket, "joined"))["seq_head"] == 5
        replay = [await wait_for(socket, "caption") for _ in range(5 - last_seq)]
        assert [c["seq"] for c in replay] == list(range(last_seq + 1, 6))
        assert all(c["delay_ms"] == 0 for c in replay)
        await speak(lesson, 2000)
        assert (await wait_for(socket, "caption"))["seq"] == 6


@run_async
async def test_replay_mixes_cached_and_untranslated_and_respects_history_limit():
    config = make_ws_test_config()
    config.history_resend = 2
    async with classroom(config) as lesson:
        _, teacher = await join(lesson, role="teacher")
        observer, socket = await join(lesson)
        await lesson.control("start")
        for seq in (1, 2):
            await speak(lesson)
            await wait_for(socket, "caption", seq=seq)
        await lesson.leave(observer)
        await speak(lesson, 2000)
        await wait_for(teacher, "asr_final", seq=3)
        _, socket = await join(lesson, last_seq=0)
        initial = await wait_for(socket, "joined")
        assert initial["history_from"] == 2
        replay = [await wait_for(socket, "caption") for _ in range(2)]
        assert [c["seq"] for c in replay] == [2, 3]
        assert all(c["text"].startswith("[en]") for c in replay)


@run_async
async def test_default_recording_off_writes_nothing(tmp_path):
    config = make_ws_test_config()
    config.recording.out_dir = str(tmp_path)
    async with classroom(config) as lesson:
        _, student = await join(lesson)
        await lesson.control("start")
        await speak(lesson)
        await wait_for(student, "caption")
        assert await lesson.control("end") is None
        assert not list(tmp_path.iterdir())


@run_async
async def test_join_reports_current_recording_and_speaking():
    async with classroom() as lesson:
        lesson.set_recording(True)
        await lesson.control("start")
        await lesson.pipeline.feed_audio(speech_pcm(1000, 0.3).tobytes())
        _, student = await join(lesson)
        initial = await wait_for(student, "joined")
        assert initial["recording"] and initial["speaking"]
        assert initial["session_state"] == "live"


@run_async
async def test_pause_notification_precedes_pending_asr_completion():
    import threading
    from server.asr.base import ASRResult

    entered, release = threading.Event(), threading.Event()
    class GatedASR(FakeASREngine):
        def transcribe(self, pcm16, sample_rate):
            entered.set()
            release.wait(5)
            return ASRResult(text="光合成には日光が必要です。")

    config = make_ws_test_config()
    pipeline = Pipeline(Roster(config.limits), config, GatedASR(),
                        FakeTranslationEngine(config.language_codes))
    lesson = Lesson(pipeline, config, join_code=JOIN_CODE)
    await pipeline.start()
    try:
        _, student = await join(lesson)
        await lesson.control("start")
        await speak(lesson, 2000)
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(lesson.control("pause"), timeout=0.2)
        await wait_for(student, "session", state="paused")
        assert not any(m["type"] == "caption" for m in student.sent)
        release.set()
        await wait_for(student, "caption", seq=1)
    finally:
        release.set()
        await pipeline.stop()
