"""キューの境界とバックプレッシャ（#25 A-1 / A-2 / A-5）。

「壊れ方」を固定する。過負荷でも
- 先生WSの受信ループは止まらない（＝授業中に操作不能にならない）
- 確定発話は捨てない。捨てるのはASR前の音声と再接続復元ジョブだけ
- 捨てたときは黙っていない（先生へ通知が出る）

推論ワーカーは動かさず（`pipeline.start()` を呼ばない）、キューへの積み方だけを見る。
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from server.asr.fake_engine import FakeASREngine
from server.asr_backlog import AsrBacklog
from server.audio.vad import Segment
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.roster import Roster
from tests.helpers import RecordingSocket, silence_pcm, speech_pcm
from tests.conftest import JOIN_CODE, make_ws_test_config

SAMPLE_RATE = 16000


class CollectingSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed_with: int | None = None

    async def send_json(self, data: object) -> None:
        self.sent.append(dict(data))  # type: ignore[arg-type]

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


async def make_pipeline(**limit_overrides) -> tuple[Pipeline, Roster]:
    config = make_ws_test_config()
    for key, value in limit_overrides.items():
        setattr(config.limits, key, value)
    roster = Roster(config.limits)
    pipeline = Pipeline(roster, config, FakeASREngine(), FakeTranslationEngine(["en"]))
    await pipeline.set_accepting_audio(True)
    return pipeline, roster


def segment_of(seconds: float) -> Segment:
    samples = np.full(int(SAMPLE_RATE * seconds), 1000, dtype=np.int16)
    return Segment(pcm=samples, t_start=0.0, t_end=seconds, closed_at=time.monotonic())


async def attach_teacher(roster: Roster) -> CollectingSocket:
    ws = CollectingSocket()
    await roster.add(ws, "teacher")
    return ws


async def settle() -> None:
    """送信タスクが1件処理するのを待つ（配信は非同期）。"""
    for _ in range(20):
        await asyncio.sleep(0.01)


class TestAudioIngestBound:
    def test_audio_is_bounded_by_seconds_not_by_count(self):
        """件数ではなく秒数で切る。1発話は 0.3〜30秒と幅があり、
        件数では「実時間でどれだけ遅れているか」を表現できない。"""

        async def scenario() -> None:
            backlog = AsrBacklog(5.0)
            for _ in range(20):
                backlog.offer(segment_of(0.5))
            assert backlog.total_seconds <= 5.0
            # 秒数で切っているなら 0.5秒×10件 は入る。件数の上限ではない
            assert backlog.total_seconds == pytest.approx(5.0, abs=0.5)

        asyncio.run(scenario())

    def test_an_empty_queue_always_accepts_even_an_oversized_segment(self):
        """上限より長い1発話でも、待ち行列が空なら必ず受ける。
        でないと max_utterance_s による強制分割が丸ごと消える。"""

        async def scenario() -> None:
            backlog = AsrBacklog(5.0)
            backlog.offer(segment_of(30.0))
            assert backlog.total_seconds == pytest.approx(30.0, abs=0.1)

        asyncio.run(scenario())

    def test_dropping_audio_is_reported_to_the_teacher(self):
        """黙って捨てない（Parapper R-6）。先生の画面に理由が出る。"""

        async def scenario() -> None:
            pipeline, roster = await make_pipeline(asr_queue_seconds=2.0)
            ws = await attach_teacher(roster)
            for _ in range(10):
                await pipeline.feed_audio(
                    np.concatenate([speech_pcm(2000, 1.0), silence_pcm(1.2)]).tobytes()
                )
            await settle()
            codes = [m.get("code") for m in ws.sent]
            assert "audio_dropped" in codes, "音声を捨てたのに先生へ通知していない"
            assert codes.count("audio_dropped") == 1, "クールダウンが効かず通知を連発している"
            await pipeline.stop()

        asyncio.run(scenario())

    def test_feed_audio_does_not_block_when_the_backlog_is_full(self):
        """`feed_audio` は先生WSの受信ループの中にある。ここで待つと
        control(pause/end) まで読めなくなる。"""

        async def scenario() -> None:
            pipeline, _ = await make_pipeline(asr_queue_seconds=1.0)
            for _ in range(50):
                await asyncio.wait_for(
                    pipeline.feed_audio(
                        np.concatenate([speech_pcm(2000, 1.0), silence_pcm(1.2)]).tobytes()
                    ),
                    timeout=0.5,
                )

        asyncio.run(scenario())


class TestOverloadSignal:
    def test_overload_is_judged_on_seconds_for_the_asr_side(self):
        """ASR側の過負荷は件数でなく秒数で判定する（#25 A-5）。"""

        async def scenario() -> None:
            pipeline, _ = await make_pipeline(asr_queue_seconds=60.0)
            assert pipeline.stats_snapshot().overloaded is False
            # 1件しか積んでいなくても、長さが閾値を超えていれば過負荷
            await pipeline.feed_audio(
                np.concatenate([speech_pcm(2000, 10.0), silence_pcm(0.5)]).tobytes()
            )
            stats = pipeline.stats_snapshot()
            assert stats.queue_depth == 1
            assert 10.0 <= stats.audio_queue_seconds <= 10.5
            assert stats.overloaded is True, "件数が少ないだけで過負荷を見逃している"

        asyncio.run(scenario())

    def test_short_backlog_of_many_segments_is_not_overload(self):
        """逆に、細切れが数件並んでいるだけなら過負荷ではない。"""

        async def scenario() -> None:
            pipeline, _ = await make_pipeline(asr_queue_seconds=60.0)
            for _ in range(4):
                await pipeline.feed_audio(
                    np.concatenate([speech_pcm(2000, 0.3), silence_pcm(0.5)]).tobytes()
                )
            stats = pipeline.stats_snapshot()
            assert stats.queue_depth == 4
            assert stats.overloaded is False

        asyncio.run(scenario())
