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
from server.audio.vad import Segment
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import _LIVE_PRIORITY, _REPLAY_PRIORITY, MTJob, Pipeline, Utterance
from server.session import Client, Session
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


def make_pipeline(**limit_overrides) -> tuple[Pipeline, Session]:
    config = make_ws_test_config()
    for key, value in limit_overrides.items():
        setattr(config.limits, key, value)
    session = Session(join_code=JOIN_CODE)
    session.state = "live"
    pipeline = Pipeline(session, config, FakeASREngine(), FakeTranslationEngine(["en"]))
    return pipeline, session


def segment_of(seconds: float) -> Segment:
    samples = np.full(int(SAMPLE_RATE * seconds), 1000, dtype=np.int16)
    return Segment(pcm=samples, t_start=0.0, t_end=seconds, closed_at=time.monotonic())


def attach_teacher(session: Session) -> CollectingSocket:
    ws = CollectingSocket()
    session.add_client(Client(id="teacher", role="teacher", lang=None, ws=ws))
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
            pipeline, _ = make_pipeline(asr_queue_seconds=5.0)
            for _ in range(20):
                await pipeline._enqueue_asr(segment_of(0.5))
            assert pipeline.audio_queue_seconds <= 5.0
            # 秒数で切っているなら 0.5秒×10件 は入る。件数の上限ではない
            assert pipeline.audio_queue_seconds == pytest.approx(5.0, abs=0.5)

        asyncio.run(scenario())

    def test_an_empty_queue_always_accepts_even_an_oversized_segment(self):
        """上限より長い1発話でも、待ち行列が空なら必ず受ける。
        でないと max_utterance_s による強制分割が丸ごと消える。"""

        async def scenario() -> None:
            pipeline, _ = make_pipeline(asr_queue_seconds=5.0)
            await pipeline._enqueue_asr(segment_of(30.0))
            assert pipeline.audio_queue_seconds == pytest.approx(30.0, abs=0.1)

        asyncio.run(scenario())

    def test_dropping_audio_is_reported_to_the_teacher(self):
        """黙って捨てない（Parapper R-6）。先生の画面に理由が出る。"""

        async def scenario() -> None:
            pipeline, session = make_pipeline(asr_queue_seconds=2.0)
            ws = attach_teacher(session)
            for _ in range(10):
                await pipeline._enqueue_asr(segment_of(1.0))
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
            pipeline, _ = make_pipeline(asr_queue_seconds=1.0)
            for _ in range(50):
                await asyncio.wait_for(pipeline._enqueue_asr(segment_of(1.0)), timeout=0.5)

        asyncio.run(scenario())


class TestMtQueueBound:
    def test_replay_jobs_are_suppressed_before_the_queue_is_full(self):
        """溢れそうなときに先に抑制されるのは再接続復元ジョブの方。
        復元は再接続でやり直せるが、ライブ字幕は失われたら戻らない。"""

        async def scenario() -> None:
            pipeline, _ = make_pipeline(mt_queue_max=10, mt_replay_watermark=4)
            utterance = Utterance(seq=1, t_start=0, t_end=1, text_ja="こんにちは", asr_ms=1)
            for _ in range(20):
                await pipeline._enqueue_mt(
                    _REPLAY_PRIORITY,
                    MTJob(utterance, "en", time.monotonic(), target_client_id="s1"),
                )
            assert pipeline._mt_queue.qsize() == 4, "復元ジョブが水位で止まっていない"

        asyncio.run(scenario())

    def test_live_jobs_use_the_capacity_the_replay_watermark_leaves(self):
        """ライブジョブは水位に縛られず、キュー上限まで積める。"""

        async def scenario() -> None:
            pipeline, _ = make_pipeline(mt_queue_max=10, mt_replay_watermark=4)
            utterance = Utterance(seq=1, t_start=0, t_end=1, text_ja="こんにちは", asr_ms=1)
            for _ in range(10):
                await pipeline._enqueue_mt(
                    _LIVE_PRIORITY, MTJob(utterance, "en", time.monotonic())
                )
            assert pipeline._mt_queue.qsize() == 10

        asyncio.run(scenario())

    def test_a_full_queue_never_drops_a_confirmed_utterance(self):
        """確定発話は捨てない（ミッションの明示要求）。満杯なら待つ。

        待つのはASRワーカーであって先生WSの受信ループではない。背圧は
        `_enqueue_asr` の秒数上限まで伝わり、そこで通知付きで捨てられる。
        """

        async def scenario() -> None:
            pipeline, session = make_pipeline(mt_queue_max=2, mt_replay_watermark=1)
            ws = attach_teacher(session)
            utterance = Utterance(seq=1, t_start=0, t_end=1, text_ja="こんにちは", asr_ms=1)
            job = MTJob(utterance, "en", time.monotonic())
            for _ in range(2):
                await pipeline._enqueue_mt(_LIVE_PRIORITY, job)

            blocked = asyncio.create_task(pipeline._enqueue_mt(_LIVE_PRIORITY, job))
            await settle()
            assert not blocked.done(), "満杯なのに確定発話を捨てて先へ進んでいる"
            await settle()
            assert "mt_backlog" in [m.get("code") for m in ws.sent], "過負荷を通知していない"

            pipeline._mt_queue.get_nowait()  # 1件はけたら待っていたジョブが入る
            await asyncio.wait_for(blocked, timeout=1.0)
            assert pipeline._mt_queue.qsize() == 2
            await pipeline.stop()

        asyncio.run(scenario())


class TestReplaySuppressionIsRecoverable:
    def test_a_student_whose_replay_cannot_be_queued_is_disconnected(self):
        """混雑で復元を積めなかったら黙って諦めず、接続を切って再接続に委ねる。

        黙って諦めると、その生徒はこのセッション中ずっとその字幕を取り戻せない
        （クライアントの last_seq は進まないが、再接続の契機が無いため）。
        """

        async def scenario() -> None:
            pipeline, session = make_pipeline(mt_queue_max=4, mt_replay_watermark=1)
            ws = CollectingSocket()
            student = Client(id="s1", role="student", lang="en", ws=ws)
            session.add_client(student)
            for seq in (1, 2, 3):
                session.add_history(
                    Utterance(seq=seq, t_start=0, t_end=1, text_ja="あ", asr_ms=1)
                )
            # 水位はすぐ埋まる（1件積んだ時点で以後は積めない）
            await pipeline.replay_history(student, last_seq=0)
            assert ws.closed_with == 1013, "復元を諦めたのに接続を維持している"
            assert session.clients.get("s1") is None
            await pipeline.stop()

        asyncio.run(scenario())

    def test_replay_that_fits_leaves_the_student_connected(self):
        async def scenario() -> None:
            pipeline, session = make_pipeline(mt_queue_max=64, mt_replay_watermark=32)
            ws = CollectingSocket()
            student = Client(id="s1", role="student", lang="en", ws=ws)
            session.add_client(student)
            for seq in (1, 2, 3):
                session.add_history(
                    Utterance(seq=seq, t_start=0, t_end=1, text_ja="あ", asr_ms=1)
                )
            await pipeline.replay_history(student, last_seq=0)
            assert ws.closed_with is None
            assert pipeline._mt_queue.qsize() == 3
            await pipeline.stop()

        asyncio.run(scenario())


class TestOverloadSignal:
    def test_overload_is_judged_on_seconds_for_the_asr_side(self):
        """ASR側の過負荷は件数でなく秒数で判定する（#25 A-5）。"""

        async def scenario() -> None:
            pipeline, _ = make_pipeline(asr_queue_seconds=60.0)
            assert pipeline.stats_snapshot().overloaded is False
            # 1件しか積んでいなくても、長さが閾値を超えていれば過負荷
            await pipeline._enqueue_asr(segment_of(10.0))
            stats = pipeline.stats_snapshot()
            assert stats.queue_depth == 1
            assert stats.audio_queue_seconds == pytest.approx(10.0, abs=0.1)
            assert stats.overloaded is True, "件数が少ないだけで過負荷を見逃している"

        asyncio.run(scenario())

    def test_short_backlog_of_many_segments_is_not_overload(self):
        """逆に、細切れが数件並んでいるだけなら過負荷ではない。"""

        async def scenario() -> None:
            pipeline, _ = make_pipeline(asr_queue_seconds=60.0)
            for _ in range(4):
                await pipeline._enqueue_asr(segment_of(0.3))
            stats = pipeline.stats_snapshot()
            assert stats.queue_depth == 4
            assert stats.overloaded is False

        asyncio.run(scenario())
