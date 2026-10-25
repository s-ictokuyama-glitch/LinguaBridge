"""Clause-level acceptance coverage for filtering and pending-turn flushing."""

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
from starlette.testclient import TestClient

from server.asr.base import ASREngine, ASRResult
from server.asr.fake_engine import FakeASREngine
from server.asr.hallucination import hallucination_reason
from server.config import load_config
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from server.mt.hymt_engine import HyMt2Engine
from server.pipeline import Pipeline
from server.recorder import SessionRecorder
from server.lesson import Lesson
from server.roster import Roster
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.helpers import chunks, silence_pcm, speech_pcm
from tests.integration.test_ws_boundary import join_student, join_teacher
from tests.unit.test_backpressure import segment_of
from tests.unit.test_turn_assembler import make, part
from tests.unit.test_vad import feed_all, make_morph_segmenter

ROOT = Path(__file__).resolve().parent.parent
NORMAL = "光合成には日光が必要です。"


class ScriptedASR(ASREngine):
    def __init__(self, results):
        self.results = iter(results)

    def transcribe(self, pcm16, sample_rate):
        return next(self.results)


class Socket:
    def __init__(self):
        self.frames = []
        self.caption = asyncio.Event()

    async def send_json(self, payload):
        self.frames.append(payload)
        if payload["type"] == "caption":
            self.caption.set()

    async def close(self, code=1000):
        pass


@pytest.mark.parametrize("bad", [
    ASRResult(text=" "),
    ASRResult(text="雑音", no_speech_prob=0.600001),
    ASRResult(text="雑音", avg_logprob=-1.000001),
    ASRResult(text="あああ", compression_ratio=2.400001),
    ASRResult(text="ご視聴ありがとうございました。"),
], ids=["empty", "no_speech", "logprob", "repetition", "known_phrase"])
def test_asr05_filtered_result_never_enters_confirmed_path(bad, tmp_path):
    async def scenario():
        config = make_ws_test_config()
        roster = Roster(config.limits)
        teacher, student = Socket(), Socket()
        await roster.add(teacher, "teacher")
        await roster.add(student, "student", "en")
        mt = FakeTranslationEngine()
        pipeline = Pipeline(roster, config, ScriptedASR([bad, ASRResult(NORMAL)]), mt)
        lesson = Lesson(pipeline, config, join_code=JOIN_CODE)
        lesson.set_recording(True)
        await lesson.control("start")
        pipeline._recorder = SessionRecorder(tmp_path, ["en", "zh"])
        await pipeline.start()
        try:
            await asyncio.wait_for(pipeline._warmup_task, 2)
            for _ in range(2):
                await pipeline._enqueue_asr(segment_of(1))
            await asyncio.wait_for(pipeline._asr_queue.join(), 2)
            await asyncio.wait_for(pipeline._mt_queue.join(), 2)
            await asyncio.wait_for(student.caption.wait(), 2)
            assert pipeline.history.seq_head == 1
            assert [(u.seq, u.text_ja) for u in pipeline.history.since(0)] == [(1, NORMAL)]
            assert mt.calls == [(NORMAL, "en")]
            assert [(m["seq"], m["ja"]) for m in teacher.frames if m["type"] == "asr_final"] == [(1, NORMAL)]
            assert [(m["seq"], m["ja"]) for m in student.frames if m["type"] == "caption"] == [(1, NORMAL)]
            directory = await pipeline.finalize_recording()
            entries = [json.loads(line) for line in (directory / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
            assert [(entry["seq"], entry["ja"]) for entry in entries] == [(1, NORMAL)]
        finally:
            await pipeline.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("metrics", [
    {"no_speech_prob": 0.6}, {"compression_ratio": 2.4}, {"avg_logprob": -1.0},
])
def test_asr05_exact_threshold_is_not_discarded(metrics):
    assert hallucination_reason(ASRResult(NORMAL, **metrics)) is None


@pytest.mark.parametrize("action, state", [("pause", "paused"), ("end", "ended")])
def test_turn02_controls_flush_pending_morph_turn(action, state):
    config = make_ws_test_config()
    config.turn = load_config(ROOT / "config.yaml").turn
    assert config.turn.strategy == "morph"
    app = create_app(config, asr_engine=FakeASREngine(), mt_engine=FakeTranslationEngine(), join_code=JOIN_CODE)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as teacher, client.websocket_connect("/ws") as student:
            join_student(student, "en")
            join_teacher(teacher)
            teacher.send_json({"type": "control", "action": "start"})
            assert student.receive_json() == {"type": "session", "state": "live"}
            pcm = np.concatenate([speech_pcm(3000, 0.6), silence_pcm(0.6)])
            for chunk in chunks(pcm):
                teacher.send_bytes(chunk)
            teacher.send_json({"type": "control", "action": action})
            frames = []
            for _ in range(10):
                frames.append(student.receive_json())
                if any(m["type"] == "caption" for m in frames) and {"type": "session", "state": state} in frames:
                    break
            cards = [m for m in frames if m["type"] == "caption"]
            assert [(m["seq"], m["ja"]) for m in cards] == [(1, FakeASREngine.PHRASES[3000])]
            assert {"type": "session", "state": state} in frames


def test_turn02_shipped_twelve_segment_limit():
    config = load_config(ROOT / "config.yaml").turn.morph
    assert config.max_segments == 12
    assembler = make(max_segments=config.max_segments, max_turn_s=config.max_turn_s)
    for index in range(11):
        assert assembler.add_segment(part("この部分の", t_start=index, t_end=index + 0.5)) is None
    turn = assembler.add_segment(part("続きの", t_start=11, t_end=11.5))
    assert turn and turn.parts == 12 and turn.reason == "max_turn"


def test_turn02_shipped_thirty_second_limit():
    config = load_config(ROOT / "config.yaml").turn.morph
    assert config.max_turn_s == 30
    assembler = make(max_segments=config.max_segments, max_turn_s=config.max_turn_s)
    assert assembler.add_segment(part("この部分の", t_start=0, t_end=1)) is None
    turn = assembler.add_segment(part("続きの", t_start=29, t_end=30))
    assert turn and turn.reason == "max_turn"


def test_turn02_shipped_one_second_silence_boundary():
    config = load_config(ROOT / "config.yaml").turn.morph
    assert config.force_silence_ms == 1000
    segmenter = make_morph_segmenter(config.check_silence_ms, config.start_speech_ms, config.force_silence_ms)
    early = feed_all(segmenter, np.concatenate([speech_pcm(2000, 1), silence_pcm(0.9)]))
    assert not any(type(event).__name__ == "TurnBoundary" for event in early)
    later = feed_all(segmenter, silence_pcm(0.3))
    boundaries = [event for event in later if type(event).__name__ == "TurnBoundary"]
    assert len(boundaries) == 1
    assert boundaries[0].at == pytest.approx(2.0, abs=0.11)


@pytest.mark.parametrize("lang,label", [("en", "English"), ("zh", "Simplified Chinese")])
@pytest.mark.parametrize("tokens,length,expected", [(1, 2, 32), (100, 100, 224), (10000, 5000, 512), (None, 100, 224)])
def test_mt04_completion_receives_bounded_tokens_and_language(tmp_path, lang, label, tokens, length, expected):
    class LLM:
        def tokenize(self, text, **kwargs):
            if tokens is None:
                raise RuntimeError("tokenizer API unavailable")
            return [1] * tokens

        def create_chat_completion(self, **kwargs):
            self.kwargs = kwargs
            return {"choices": [{"message": {"content": " translated "}}]}
    model = tmp_path / "model.gguf"
    model.touch()
    engine = HyMt2Engine(model)
    engine._llm = LLM()
    original = "あ" * length
    assert engine.translate(original, lang) == "translated"
    assert engine._llm.kwargs["max_tokens"] == expected
    message = engine._llm.kwargs["messages"][0]
    assert message["role"] == "user"
    assert label in message["content"] and original in message["content"]
    assert "only output the translated result" in message["content"]
