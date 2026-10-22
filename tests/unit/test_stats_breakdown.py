"""遅延の内訳（#30）のユニットテスト。

先生ページの stats はこれまで `queue_depth`（ASR待ちとMT待ちの合算件数）と
`median_delay_ms`（総遅延）しか持たず、「どこが詰まっているのか」が読めなかった。
内訳を足すにあたって固定するのは、**先生に誤読させない**ための4点:

    1. `asr_wait_seconds + asr_active_seconds == audio_queue_seconds`
       （合計の意味は変えていない。内訳は合計を割ったもの）
    2. ASR が1件を処理している最中、待ちは 0 になる
       — #25・#29 が二度指摘した「1発話が長いだけ」を「詰まっている」と読む誤りは、
         この2つを分けて初めて機械的に区別できる
    3. 翻訳キャッシュのヒットは `median_mt_ms` の母数に入らない
       — 実測ヒット率 27.8% で母数に混ぜると中央値が 0 へ引っ張られ、
         「翻訳は速い」という逆の結論を先生に見せてしまう
    4. 非live へ遷移したら内訳も総遅延と一緒に消える
       （合計 0ms なのに翻訳 850ms、という読めない表示を作らない）
"""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np
import pytest

from server.asr.fake_engine import FakeASREngine
from server.asr.base import ASRResult
from server.config import AppConfig, VadConfig
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.session import Client, Session
from tests.helpers import SAMPLE_RATE, silence_pcm, speech_pcm

SPEECH_S = 1.0
SILENCE_S = 1.2  # 既定 turn 戦略（morph）の force_silence_ms(1000ms) を超える長さ
MT_WORK_S = 0.05  # 「推論した」と「キャッシュで返した」を中央値で見分けられる長さ


class GatedASR(FakeASREngine):
    """transcribe に入ったことを知らせ、解放されるまで戻らないフェイクASR。

    「ASR が処理している最中」の stats を決定的に観測するために使う。
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def transcribe(self, pcm16: np.ndarray, sample_rate: int) -> ASRResult:
        self.entered.set()
        self.release.wait(timeout=5)
        return super().transcribe(pcm16, sample_rate)


class SlowMT(FakeTranslationEngine):
    """1件あたり確実に時間のかかるフェイク翻訳（中央値が 0 に潰れないようにする）。"""

    def translate(self, text_ja: str, target_lang: str) -> str:
        time.sleep(MT_WORK_S)
        return super().translate(text_ja, target_lang)


def make_pipeline(
    asr: FakeASREngine | None = None,
    mt: FakeTranslationEngine | None = None,
    *,
    students: int = 0,
) -> Pipeline:
    config = AppConfig(vad=VadConfig(engine="energy", threshold=300))
    session = Session("0000")
    session.state = "live"
    langs = [lang.code for lang in config.languages]
    for i in range(students):
        # ws=None のダミー。翻訳ジョブは「選択者がいる言語」にしか発生しないので、
        # 内訳に翻訳の数字を出すには生徒が要る
        session.add_client(Client(id=f"s{i}", role="student", lang=langs[0], ws=None))
    return Pipeline(session, config, asr or FakeASREngine(), mt or FakeTranslationEngine(langs))


def utterance_bytes(key: int = 2000) -> bytes:
    return np.concatenate([speech_pcm(key, SPEECH_S), silence_pcm(SILENCE_S)]).tobytes()


# ---- 1・2: 待ちと処理中を分ける ----


def test_breakdown_sums_to_audio_queue_seconds() -> None:
    """内訳は合計を割ったもの。`audio_queue_seconds` の意味は変わっていない。"""

    async def scenario() -> None:
        pipeline = make_pipeline()
        assert pipeline.asr_wait_seconds == 0.0
        assert pipeline.asr_active_seconds == 0.0

        await pipeline.feed_audio(utterance_bytes())
        # ワーカー未起動＝まだ誰も取り出していないので、全量が「待ち」
        assert pipeline.asr_active_seconds == 0.0
        assert pipeline.asr_wait_seconds == pytest.approx(
            pipeline.audio_queue_seconds, abs=1e-6
        )

    asyncio.run(scenario())


def test_in_flight_segment_is_not_counted_as_waiting() -> None:
    """処理中の1発話は「待ち 0秒」になる。

    #25・#29 が指摘した誤読そのもの: 合算値だけを見せると、長い発話を1件
    処理しているだけの健全な状態が「詰まっている」に見える。
    """

    asr = GatedASR()

    async def scenario() -> None:
        pipeline = make_pipeline(asr)
        await pipeline.feed_audio(utterance_bytes())
        await pipeline.start()
        try:
            async with asyncio.timeout(5):
                while not asr.entered.is_set():
                    await asyncio.sleep(0.01)
            stats = pipeline.stats_snapshot()
            assert stats.asr_active_seconds > 0
            assert stats.asr_wait_seconds == 0.0
            # 合計は従来どおり（滞留の指標としての意味を変えていない）
            assert stats.audio_queue_seconds == stats.asr_active_seconds
        finally:
            asr.release.set()
            await pipeline.stop()

    asyncio.run(scenario())


def test_waiting_and_active_are_reported_separately() -> None:
    """後ろで待っている発話があるときだけ「待ち」が立つ。"""

    asr = GatedASR()

    async def scenario() -> None:
        pipeline = make_pipeline(asr)
        await pipeline.feed_audio(utterance_bytes(2000))
        await pipeline.feed_audio(utterance_bytes(1000))
        await pipeline.start()
        try:
            async with asyncio.timeout(5):
                while not asr.entered.is_set():
                    await asyncio.sleep(0.01)
            stats = pipeline.stats_snapshot()
            assert stats.asr_active_seconds > 0
            assert stats.asr_wait_seconds > 0
            assert stats.asr_wait_seconds + stats.asr_active_seconds == pytest.approx(
                stats.audio_queue_seconds, abs=0.02
            )
        finally:
            asr.release.set()
            await pipeline.stop()

    asyncio.run(scenario())


def test_active_returns_to_zero_after_drain() -> None:
    """処理を終えれば「処理中」も 0 に戻る（失敗しても積み上がらない）。"""

    async def scenario() -> None:
        pipeline = make_pipeline()
        await pipeline.feed_audio(utterance_bytes())
        await pipeline.start()
        try:
            async with asyncio.timeout(5):
                while pipeline.audio_queue_seconds > 0:
                    await asyncio.sleep(0.01)
            assert pipeline.asr_active_seconds == 0.0
            assert pipeline.asr_wait_seconds == 0.0
        finally:
            await pipeline.stop()

    asyncio.run(scenario())


# ---- 3: 段ごとの中央値 ----


async def _run_utterances(pipeline: Pipeline, *keys: int) -> None:
    for key in keys:
        await pipeline.feed_audio(utterance_bytes(key))
    async with asyncio.timeout(10):
        while pipeline.audio_queue_seconds > 0 or pipeline.stats_snapshot().mt_queue_depth:
            await asyncio.sleep(0.01)
        await pipeline._mt_queue.join()


def test_medians_are_populated_after_processing() -> None:
    async def scenario() -> None:
        pipeline = make_pipeline(mt=SlowMT(["en", "zh"]), students=1)
        assert pipeline.stats_snapshot().median_asr_ms == 0  # 材料が無ければ 0
        assert pipeline.stats_snapshot().median_mt_ms == 0
        await pipeline.start()
        try:
            await _run_utterances(pipeline, 2000)
            stats = pipeline.stats_snapshot()
            assert stats.median_asr_ms >= 0  # フェイクASRは一瞬なので値は問わない
            assert stats.median_mt_ms >= MT_WORK_S * 1000 * 0.8
        finally:
            await pipeline.stop()

    asyncio.run(scenario())


def test_cache_hits_do_not_dilute_median_mt_ms() -> None:
    """キャッシュヒットは「推論していない」ので中央値の母数に入れない。

    入れると、同じ文が繰り返される授業ほど中央値が 0 に近づき、
    「翻訳は速い」という逆の結論を先生に見せてしまう。
    節約できたぶんは `mt_cache_hit_rate` が別に表している。
    """

    async def scenario() -> None:
        mt = SlowMT(["en", "zh"])
        pipeline = make_pipeline(mt=mt, students=1)
        await pipeline.start()
        try:
            # 同じ発話を2回。2回目は必ずキャッシュで返る
            await _run_utterances(pipeline, 2000)
            after_first = pipeline.stats_snapshot().median_mt_ms
            await _run_utterances(pipeline, 2000)
            stats = pipeline.stats_snapshot()

            assert stats.mt_cache_hits > 0, "2回目はキャッシュで返っているはず"
            assert len(mt.calls) == 1, "推論は1回だけ"
            # ヒットを 0ms として数えていれば中央値は半分以下に落ちる
            assert stats.median_mt_ms == after_first
            assert stats.median_mt_ms >= MT_WORK_S * 1000 * 0.8
        finally:
            await pipeline.stop()

    asyncio.run(scenario())


def test_mt_queue_depth_is_a_part_of_queue_depth() -> None:
    """翻訳待ちの件数は合算値の一部。合算値の意味は変えていない。"""

    async def scenario() -> None:
        pipeline = make_pipeline()
        stats = pipeline.stats_snapshot()
        assert stats.mt_queue_depth == 0
        await pipeline.feed_audio(utterance_bytes())
        stats = pipeline.stats_snapshot()
        assert stats.mt_queue_depth <= stats.queue_depth

    asyncio.run(scenario())


# ---- 4: 非live でのリセット ----


def test_breakdown_medians_reset_when_not_live() -> None:
    """一時停止・終了で内訳も消す。総遅延だけ 0 で内訳が残ると読めない表示になる。"""

    async def scenario() -> None:
        pipeline = make_pipeline(mt=SlowMT(["en", "zh"]), students=1)
        await pipeline.start()
        try:
            await _run_utterances(pipeline, 2000)
            assert pipeline.stats_snapshot().median_mt_ms > 0

            pipeline._session.state = "paused"
            await pipeline.broadcast_session_state()
            stats = pipeline.stats_snapshot()
            assert stats.median_delay_ms == 0
            assert stats.median_asr_ms == 0
            assert stats.median_mt_ms == 0
        finally:
            await pipeline.stop()

    asyncio.run(scenario())


def test_seconds_are_seconds_not_samples() -> None:
    """内訳も秒（サンプル数ではない）。合計と同じ単位であること。"""

    async def scenario() -> None:
        pipeline = make_pipeline()
        await pipeline.feed_audio(utterance_bytes())
        assert pipeline.asr_wait_seconds < (SPEECH_S + SILENCE_S) * SAMPLE_RATE / 100

    asyncio.run(scenario())
