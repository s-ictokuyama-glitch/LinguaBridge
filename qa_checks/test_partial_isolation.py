"""Partial results must not escape into the confirmed or recorded path."""

import asyncio
import json
import threading
import time

from server.audio.vad import InterimSegment
from server.asr.base import ASRResult
from server.asr.fake_engine import FakeASREngine
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.recorder import SessionRecorder
from server.lesson import Lesson
from server.roster import Roster
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.helpers import speech_pcm
from tests.unit.test_backpressure import segment_of
from qa_checks.test_pipeline_contracts import NORMAL, ScriptedASR, Socket


def interim():
    return InterimSegment(speech_pcm(2000, 0.6), 0, 0.6, time.monotonic())


async def build(tmp_path, asr):
    config = make_ws_test_config()
    config.partial.enabled = True
    roster = Roster(config.limits)
    teacher, student = Socket(), Socket()
    await roster.add(teacher, "teacher")
    await roster.add(student, "student", "en")
    mt = FakeTranslationEngine()
    pipeline = Pipeline(roster, config, asr, mt)
    lesson = Lesson(pipeline, config, join_code=JOIN_CODE)
    lesson.set_recording(True)
    await lesson.control("start")
    pipeline._recorder = SessionRecorder(tmp_path, ["en"])
    return pipeline, lesson, teacher, student, mt


async def wait_for(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


def test_mon03_partial_never_enters_recording_or_confirmed_state(tmp_path):
    async def scenario():
        partial = "まだ確定していない説明"
        pipeline, lesson, teacher, student, mt = await build(tmp_path, ScriptedASR([ASRResult(partial), ASRResult(NORMAL)]))
        await pipeline.start()
        try:
            await asyncio.wait_for(pipeline._warmup_task, 2)
            pipeline._enqueue_interim(interim())
            await asyncio.wait_for(pipeline._asr_queue.join(), 2)
            await wait_for(lambda: any(f["type"] == "turn.partial" for f in teacher.frames))
            assert pipeline.history.seq_head == 0 and pipeline.history.since(0) == []
            assert mt.calls == [] and not pipeline._recorder.has_entries
            assert all(f["type"] not in {"caption", "turn.partial"} for f in student.frames)
            await pipeline._enqueue_asr(segment_of(1))
            await asyncio.wait_for(pipeline._asr_queue.join(), 2)
            await asyncio.wait_for(student.caption.wait(), 2)
            directory = await pipeline.finalize_recording()
            entries = [json.loads(line) for line in (directory / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
            assert [(e["seq"], e["ja"]) for e in entries] == [(1, NORMAL)]
            assert [(u.seq, u.text_ja) for u in pipeline.history.since(0)] == [(1, NORMAL)]
            assert mt.calls == [(NORMAL, "en")]
            assert partial not in (directory / "transcript.jsonl").read_text(encoding="utf-8")
        finally:
            await pipeline.stop()
    asyncio.run(scenario())


def test_mon03_busy_final_queue_skips_partial_without_delaying_final(tmp_path):
    async def scenario():
        pipeline, lesson, _, _, _ = await build(tmp_path, ScriptedASR([ASRResult(NORMAL)]))
        await pipeline._enqueue_asr(segment_of(1))
        pipeline._enqueue_interim(interim())
        assert pipeline.interims_skipped == 1
        assert pipeline._asr_queue.qsize() == 1
        await pipeline.start()
        try:
            await asyncio.wait_for(pipeline._asr_queue.join(), 2)
            assert pipeline.asr_partial_calls == 0 and pipeline.asr_calls == 1
            assert pipeline.history.seq_head == 1
        finally:
            await pipeline.stop()
    asyncio.run(scenario())


class GatedASR(FakeASREngine):
    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def transcribe(self, pcm16, sample_rate):
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("QA gate not released")
        return super().transcribe(pcm16, sample_rate)


def test_mon03_stale_turn_is_dropped_and_next_partial_can_run(tmp_path):
    async def scenario():
        asr = GatedASR()
        pipeline, lesson, teacher, _, mt = await build(tmp_path, asr)
        await pipeline.start()
        try:
            await asyncio.wait_for(pipeline._warmup_task, 2)
            pipeline._enqueue_interim(interim())
            await wait_for(asr.entered.is_set)
            old_turn = pipeline._assembler.current_turn_id
            pipeline._enqueue_interim(interim())
            assert pipeline.interims_skipped == 1  # One in-flight partial only.
            # Isolate a turn rollover during inference through the assembler's
            # public lifecycle, without pretending it is a browser/network race.
            pipeline._assembler.discard_reserved()
            new_turn = pipeline._assembler.reserve_turn_id()
            assert new_turn != old_turn
            asr.release.set()
            await asyncio.wait_for(pipeline._asr_queue.join(), 2)
            assert pipeline.partials_stale == 1 and pipeline.partials_sent == 0
            assert not pipeline._interim_in_flight
            assert all(f["type"] != "turn.partial" for f in teacher.frames)
            pipeline._enqueue_interim(interim())
            await asyncio.wait_for(pipeline._asr_queue.join(), 2)
            await wait_for(lambda: any(f["type"] == "turn.partial" for f in teacher.frames))
            assert [f["turn_id"] for f in teacher.frames if f["type"] == "turn.partial"] == [new_turn]
            assert pipeline.history.seq_head == 0 and pipeline.history.since(0) == []
            assert mt.calls == [] and not pipeline._recorder.has_entries
        finally:
            asr.release.set()
            await pipeline.stop()
    asyncio.run(scenario())
