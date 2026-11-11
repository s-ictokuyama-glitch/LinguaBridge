import asyncio

import numpy as np
import pytest

from server.asr.base import ASRResult
from server.asr_backlog import FlushTurn
from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, PartialConfig, VadConfig
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


def make_pipeline(*, enabled=True, asr=None):
    config = AppConfig(vad=VadConfig(engine="energy", threshold=300))
    config.partial = PartialConfig(speaking_indicator=enabled)
    config.monitoring.silence_warning_s = 0
    return ObservedPipeline(Roster(), config, asr or FakeASREngine(), FakeTranslationEngine(["en"]))


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("resume", [True, False])
def test_same_open_utterance_after_capture_gap(enabled, resume):
    async def scenario():
        pipeline = make_pipeline(enabled=enabled)
        await pipeline.set_accepting_audio(True)
        await pipeline.feed_audio(speech_pcm(2000, 0.4).tobytes())
        await pipeline._check_mic_silence()
        await pipeline.feed_audio((speech_pcm(2000, 0.4) if resume else silence_pcm(0.1)).tobytes())
        await pipeline.set_accepting_audio(False)
        await pipeline.flush_audio()
        expected = [True, False, True, False] if resume else [True, False]
        assert pipeline.indicators == (expected if enabled else [])
        assert pipeline.speaking is False
        await pipeline.stop()
    asyncio.run(scenario())


def test_flush_clears_without_waiting_for_asr():
    async def scenario():
        pipeline = make_pipeline()
        await pipeline.set_accepting_audio(True)
        await pipeline.feed_audio(speech_pcm(2000, 0.4).tobytes())
        await pipeline.set_accepting_audio(False)
        await pipeline.flush_audio()
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
        await pipeline.set_accepting_audio(True)
        await pipeline.feed_audio(np.concatenate([speech_pcm(2000, 0.4), silence_pcm(1.2)]).tobytes())
        assert pipeline.indicators == [True]
        await pipeline.start()
        try:
            await pipeline.drain(timeout=1)
            assert pipeline.indicators == [True, False]
            assert pipeline.captions.history.seq_head == 0
            assert pipeline.queue_depth == pipeline.asr_queue_depth == pipeline.captions.queue_depth == 0
        finally:
            await pipeline.stop()
    asyncio.run(scenario())


def test_drain_gives_each_stage_the_full_timeout(monkeypatch):
    async def scenario():
        pipeline = make_pipeline()
        loop = asyncio.get_running_loop()
        now = loop.time()
        monkeypatch.setattr(loop, "time", lambda: now)
        pipeline._asr_backlog.offer(FlushTurn(closed_at=now))
        await pipeline.roster.add(RecordingSocket(), "student", "en")
        utterance = pipeline.captions.history.append(
            t_start=0, t_end=1, text_ja="こんにちは", asr_ms=1,
        )
        await pipeline.captions.publish(utterance, closed_at=now)
        draining = asyncio.create_task(pipeline.drain(timeout=1))
        await asyncio.sleep(0)
        try:
            now += 0.75
            async with pipeline._asr_backlog.next():
                pass
            await asyncio.sleep(0)
            now += 0.75
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            pipeline.captions.start()
            await draining
        finally:
            await pipeline.stop()

    asyncio.run(scenario())


def test_drain_times_out_with_unprocessed_audio():
    async def scenario():
        pipeline = make_pipeline()
        await pipeline.set_accepting_audio(True)
        await pipeline.feed_audio(np.concatenate([speech_pcm(2000, 0.4), silence_pcm(1.2)]).tobytes())
        with pytest.raises(TimeoutError):
            await pipeline.drain(timeout=0.01)
        assert pipeline.asr_queue_depth > 0
        await pipeline.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("failing_side", ["asr", "mt"])
def test_stop_attempts_both_runners_when_one_cleanup_fails(monkeypatch, failing_side):
    async def scenario():
        pipeline = make_pipeline()
        calls = []

        async def close(side):
            calls.append(side)
            if side == failing_side:
                raise RuntimeError(f"{side} cleanup failed")

        monkeypatch.setattr(pipeline._asr_runner, "aclose", lambda: close("asr"))
        monkeypatch.setattr(pipeline.captions._mt_runner, "aclose", lambda: close("mt"))
        for _ in range(2):
            with pytest.raises(RuntimeError, match=f"{failing_side} cleanup failed"):
                await pipeline.stop()
        assert sorted(calls) == ["asr", "mt"]

    asyncio.run(scenario())


def test_repeated_stop_cancellation_finishes_both_cleanups(monkeypatch):
    async def scenario():
        pipeline = make_pipeline()
        entered = {side: asyncio.Event() for side in ("asr", "mt")}
        release = asyncio.Event()
        completed = []

        async def close(side):
            entered[side].set()
            await release.wait()
            completed.append(side)

        monkeypatch.setattr(pipeline._asr_runner, "aclose", lambda: close("asr"))
        monkeypatch.setattr(pipeline.captions._mt_runner, "aclose", lambda: close("mt"))
        stopping = asyncio.create_task(pipeline.stop())
        await asyncio.gather(*(event.wait() for event in entered.values()))
        for _ in range(3):
            stopping.cancel()
            await asyncio.sleep(0)
            assert not stopping.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert sorted(completed) == ["asr", "mt"]
        await pipeline.stop()

    asyncio.run(scenario())
