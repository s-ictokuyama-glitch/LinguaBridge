import asyncio
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import numpy as np
from server.config import AppConfig, VadConfig
from server.pipeline import Pipeline
from server.roster import Roster
from server.asr.fake_engine import FakeASREngine
from server.mt.fake_engine import FakeTranslationEngine
from tests.helpers import RecordingSocket, speech_pcm, silence_pcm, wait_for

class GatedASR(FakeASREngine):
    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()
    def transcribe(self, pcm16, sample_rate):
        self.entered.set()
        assert self.release.wait(10), "ASR release timed out"
        return super().transcribe(pcm16, sample_rate)

class GatedMT(FakeTranslationEngine):
    def __init__(self):
        super().__init__(["en"])
        self.entered, self.release = threading.Event(), threading.Event()
    def translate(self, text, lang):
        self.entered.set()
        assert self.release.wait(10), "MT release timed out"
        return super().translate(text, lang)

async def main():
    roster, socket = Roster(), RecordingSocket()
    await roster.add(socket, "student", "en")
    asr, mt = GatedASR(), GatedMT()
    pipeline = Pipeline(roster, AppConfig(vad=VadConfig(engine="energy", threshold=300)), asr, mt)
    await pipeline.on_lesson_state("live")
    await pipeline.start()
    try:
        await pipeline.feed_audio(np.concatenate([speech_pcm(2000, .8), silence_pcm(1.2)]).tobytes())
        assert await asyncio.to_thread(asr.entered.wait, 5)
        draining = asyncio.create_task(pipeline.drain(timeout=5))
        await asyncio.sleep(.05)
        assert not draining.done(), "drain returned during blocked ASR"
        assert not mt.entered.is_set(), "MT started before ASR completion"
        asr.release.set()
        assert await asyncio.to_thread(mt.entered.wait, 5)
        await asyncio.sleep(.05)
        assert not draining.done(), "drain returned before newly created MT completed"
        mt.release.set()
        await asyncio.wait_for(draining, 5)
        caption = await wait_for(socket, "caption")
        assert caption["text"] == "[en] " + FakeASREngine.PHRASES[2000]
        stats = pipeline.stats_snapshot().model_dump()
        assert stats["asr_calls"] == stats["mt_calls"] == 1
        assert pipeline.queue_depth == stats["audio_queue_seconds"] == 0
        await pipeline.on_lesson_state("paused")
        await pipeline.feed_audio(speech_pcm(2000, .8).tobytes())
        await pipeline.drain(timeout=1)
        assert pipeline.history.seq_head == 1 and not pipeline.speaking
        print(json.dumps({"result":"PASS", "asr_then_new_mt_drain_order":True, "paused_audio_ignored":True, "caption":caption, "stats":stats}, ensure_ascii=False))
    finally:
        asr.release.set()
        mt.release.set()
        await pipeline.stop()
        for client in roster.clients:
            await roster.remove(client)

asyncio.run(main())
