"""ASR滞留の実時間指標 `audio_queue_seconds`（#24 の計装）のユニットテスト。

`queue_depth`（件数）では「3件詰まっている」が 3秒ぶんなのか 30秒ぶんなのか分からない。
E2E の基準線と P0是正（#25）のバックプレッシャ判定は「実時間でどれだけ遅れているか」を
見るので、秒で測る指標を別に持つ。

固定するのは2点だけ:
    - 積んだ音声の長さぶん増える（処理が終わるまで減らない）
    - 発話が処理し終われば 0 に戻る（取りこぼしで積み上がらない）
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, VadConfig
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.session import Session
from tests.helpers import SAMPLE_RATE, silence_pcm, speech_pcm

SPEECH_S = 1.0
SILENCE_S = 0.8


def make_pipeline() -> Pipeline:
    config = AppConfig(vad=VadConfig(engine="energy", threshold=300))
    session = Session("0000")
    session.state = "live"
    pipeline = Pipeline(
        session,
        config,
        FakeASREngine(),
        FakeTranslationEngine([lang.code for lang in config.languages]),
    )
    return pipeline


def utterance_bytes() -> bytes:
    return np.concatenate(
        [speech_pcm(2000, SPEECH_S), silence_pcm(SILENCE_S)]
    ).tobytes()


def test_queued_audio_grows_then_drains() -> None:
    async def scenario() -> None:
        pipeline = make_pipeline()
        assert pipeline.audio_queue_seconds == 0.0

        # ワーカー未起動なので、積んだぶんがそのまま滞留として見える
        await pipeline.feed_audio(utterance_bytes())
        queued = pipeline.audio_queue_seconds
        assert queued > 0
        # セグメントは音声＋発話終了判定ぶんの無音を含む。総入力長は超えない
        assert SPEECH_S <= queued <= SPEECH_S + SILENCE_S + 0.1

        await pipeline.start()
        try:
            async with asyncio.timeout(5):
                while pipeline.audio_queue_seconds > 0:
                    await asyncio.sleep(0.01)
        finally:
            await pipeline.stop()

    asyncio.run(scenario())


def test_stats_report_queued_audio_in_seconds() -> None:
    async def scenario() -> None:
        pipeline = make_pipeline()
        assert pipeline.stats_snapshot().audio_queue_seconds == 0.0
        await pipeline.feed_audio(utterance_bytes())
        stats = pipeline.stats_snapshot()
        assert stats.queue_depth == 1
        assert stats.audio_queue_seconds == pytest.approx(
            pipeline.audio_queue_seconds, abs=0.01
        )

    asyncio.run(scenario())


def test_dropped_utterance_does_not_leak_queued_audio() -> None:
    """ASRが例外を投げても滞留は解放される（積み上がって過負荷判定が張り付かない）。"""

    class BrokenASR(FakeASREngine):
        def transcribe(self, pcm16, sample_rate):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    async def scenario() -> None:
        config = AppConfig(vad=VadConfig(engine="energy", threshold=300))
        session = Session("0000")
        session.state = "live"
        pipeline = Pipeline(
            session,
            config,
            BrokenASR(),
            FakeTranslationEngine([lang.code for lang in config.languages]),
        )
        await pipeline.feed_audio(utterance_bytes())
        assert pipeline.audio_queue_seconds > 0
        await pipeline.start()
        try:
            async with asyncio.timeout(5):
                while pipeline.audio_queue_seconds > 0:
                    await asyncio.sleep(0.01)
        finally:
            await pipeline.stop()

    asyncio.run(scenario())


def test_flush_counts_partial_utterance() -> None:
    """一時停止・終了時の flush で確定した発話も滞留に数える。"""

    async def scenario() -> None:
        pipeline = make_pipeline()
        await pipeline.feed_audio(speech_pcm(2000, SPEECH_S).tobytes())  # 無音が来ず未確定
        assert pipeline.audio_queue_seconds == 0.0
        await pipeline.flush_audio()
        assert pipeline.audio_queue_seconds >= SPEECH_S - 0.1

    asyncio.run(scenario())


def test_sample_rate_conversion_is_seconds_not_samples() -> None:
    async def scenario() -> None:
        pipeline = make_pipeline()
        await pipeline.feed_audio(utterance_bytes())
        # サンプル数（約29,000）ではなく秒（約1.8）であること
        assert pipeline.audio_queue_seconds < (SPEECH_S + SILENCE_S) * SAMPLE_RATE / 100

    asyncio.run(scenario())
