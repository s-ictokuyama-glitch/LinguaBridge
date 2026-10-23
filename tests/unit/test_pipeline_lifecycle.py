"""公開入力から発話状態と排出・記録の契約を確かめる。"""
import asyncio
import json
from unittest.mock import AsyncMock

import numpy as np
import pytest

from server.asr.base import ASRResult
from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, PartialConfig, RecordingConfig, VadConfig
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.roster import Roster
from tests.helpers import RecordingSocket, silence_pcm, speech_pcm


class ObservedPipeline(Pipeline):
    def __init__(self, *args):
        super().__init__(*args)
        self.indicators = []

    async def _broadcast_all(self, payload):
        if payload["type"] == "speaking":
            self.indicators.append(payload["on"])


def make_pipeline(*, enabled=True, asr=None, out_dir=None):
    config = AppConfig(vad=VadConfig(engine="energy", threshold=300))
    config.partial = PartialConfig(speaking_indicator=enabled)
    config.monitoring.silence_warning_s = 0
    if out_dir is not None:
        config.recording = RecordingConfig(default_on=True, out_dir=str(out_dir))
    return ObservedPipeline(Roster(), config, asr or FakeASREngine(), FakeTranslationEngine(["en"]))


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("resume", [True, False])
def test_same_open_utterance_after_capture_gap(enabled, resume):
    async def scenario():
        pipeline = make_pipeline(enabled=enabled)
        await pipeline.on_lesson_state("live")
        await pipeline.feed_audio(speech_pcm(2000, 0.4).tobytes())
        await pipeline._check_mic_silence()
        await pipeline.feed_audio((speech_pcm(2000, 0.4) if resume else silence_pcm(0.1)).tobytes())
        await pipeline.on_lesson_state("paused")
        expected = [True, False, True, False] if resume else [True, False]
        assert pipeline.indicators == (expected if enabled else [])
        assert pipeline.speaking is False
        await pipeline.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["paused", "ended"])
def test_pause_or_end_clears_without_waiting_for_asr(state):
    async def scenario():
        pipeline = make_pipeline()
        await pipeline.on_lesson_state("live")
        await pipeline.feed_audio(speech_pcm(2000, 0.4).tobytes())
        await pipeline.on_lesson_state(state)
        assert pipeline.indicators == [True, False]
        assert pipeline.asr_queue_depth == pipeline.queue_depth > 0
        await pipeline.stop()
    asyncio.run(scenario())


def test_one_batch_opens_closes_and_hallucination_settles_off():
    class HallucinatingASR(FakeASREngine):
        def transcribe(self, pcm16, sample_rate):
            return ASRResult("ご視聴ありがとうございました", no_speech_prob=0.99)

    async def scenario():
        pipeline = make_pipeline(asr=HallucinatingASR())
        await pipeline.on_lesson_state("live")
        await pipeline.feed_audio(np.concatenate([speech_pcm(2000, 0.4), silence_pcm(1.2)]).tobytes())
        assert pipeline.indicators == [True]
        await pipeline.start()
        try:
            await pipeline.drain(timeout=1)
            assert pipeline.indicators == [True, False]
            assert pipeline.history.seq_head == 0
            assert pipeline.queue_depth == pipeline.asr_queue_depth == pipeline.mt_queue_depth == 0
        finally:
            await pipeline.stop()
    asyncio.run(scenario())


def test_drain_gives_each_stage_the_full_timeout(monkeypatch):
    async def scenario():
        pipeline = make_pipeline()
        timeouts = []
        original = asyncio.wait_for
        async def observe(awaitable, timeout):
            timeouts.append(timeout)
            return await original(awaitable, timeout)
        monkeypatch.setattr(asyncio, "wait_for", observe)
        await pipeline.drain(timeout=0.25)
        assert timeouts == [0.25, 0.25]
        await pipeline.stop()
    asyncio.run(scenario())


def test_drain_times_out_with_unprocessed_audio():
    async def scenario():
        pipeline = make_pipeline()
        await pipeline.on_lesson_state("live")
        await pipeline.feed_audio(np.concatenate([speech_pcm(2000, 0.4), silence_pcm(1.2)]).tobytes())
        with pytest.raises(TimeoutError):
            await pipeline.drain(timeout=0.01)
        assert pipeline.asr_queue_depth > 0
        await pipeline.stop()
    asyncio.run(scenario())


def test_finalize_recording_uses_drain_and_writes_partial_on_timeout(tmp_path, monkeypatch):
    async def scenario():
        pipeline = make_pipeline(out_dir=tmp_path)
        await pipeline.roster.add(RecordingSocket(), "student", "en")
        await pipeline.on_lesson_state("live")
        await pipeline.start()
        try:
            await pipeline.feed_audio(np.concatenate([speech_pcm(2000, 0.4), silence_pcm(1.2)]).tobytes())
            await pipeline.drain(timeout=1)
            drain = AsyncMock(side_effect=TimeoutError)
            monkeypatch.setattr(pipeline, "drain", drain)
            output = await pipeline.finalize_recording()
            drain.assert_awaited_once_with(timeout=30)
            entries = [json.loads(line) for line in (output / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
            assert len(entries) == 1
            assert entries[0]["ja"] == FakeASREngine.PHRASES[2000]
            assert entries[0]["translations"]["en"]["text"]
        finally:
            await pipeline.stop()
    asyncio.run(scenario())
