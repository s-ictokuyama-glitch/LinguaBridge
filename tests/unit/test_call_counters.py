"""通算の推論回数と破棄の計数器（#32）のユニットテスト。

スケーリングベンチ（`scripts/scale_bench.py`）は stats の3つの数だけを見て
「生徒が増えても ASR は増えない」「同一言語の人数で MT は増えない」を主張する。
その数が**何を数えているか**が曖昧だと、ベンチの表は読めても意味が読めない。
ここで固定するのは4点:

    1. `asr_calls` は**確定 Segment の推論回数**。partial は含めない
       （含めると「ASR回数は生徒数で不変」の主張に、生徒とは無関係な
         partial の増減が混ざって読めなくなる）
    2. `mt_calls` は**実際に推論した回数**。キャッシュヒットは含めない
       — 節約ぶんは `mt_cache_hits` が別に表す。合算すると
         「翻訳回数」がキャッシュの効きで動き、Hy-MT2 の実負荷を表さなくなる
    3. どちらも**通算値**（`median_*_ms` の deque(maxlen=) 標本とは別物）
    4. `asr_dropped_segments` は捨てた実数。先生への error 通知は
       クールダウンで間引かれるので、通知の数では実数が分からない
"""

from __future__ import annotations

import asyncio

import numpy as np

from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, LimitsConfig, VadConfig
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.session import Client, Session
from tests.helpers import silence_pcm, speech_pcm

SPEECH_S = 1.0
SILENCE_S = 1.2  # 既定 morph の force_silence_ms(1000ms) を超える長さ


def make_pipeline(
    *, students: int = 1, langs: list[str] | None = None, limits: LimitsConfig | None = None
) -> tuple[Pipeline, FakeASREngine, FakeTranslationEngine]:
    config = AppConfig(vad=VadConfig(engine="energy", threshold=300))
    if limits is not None:
        config = config.model_copy(update={"limits": limits})
    codes = langs or [lang.code for lang in config.languages]
    session = Session("0000")
    session.state = "live"
    for i in range(students):
        session.add_client(Client(id=f"s{i}", role="student", lang=codes[i % len(codes)], ws=None))
    asr = FakeASREngine()
    mt = FakeTranslationEngine(codes)
    return Pipeline(session, config, asr, mt), asr, mt


def utterance_bytes(key: int = 2000) -> bytes:
    return np.concatenate([speech_pcm(key, SPEECH_S), silence_pcm(SILENCE_S)]).tobytes()


async def run_utterances(pipeline: Pipeline, *keys: int) -> None:
    for key in keys:
        await pipeline.feed_audio(utterance_bytes(key))
    async with asyncio.timeout(10):
        while pipeline.audio_queue_seconds > 0 or pipeline.stats_snapshot().mt_queue_depth:
            await asyncio.sleep(0.01)
        await pipeline._mt_queue.join()


# ---- 1: ASR回数は発話の数であって生徒の数ではない ----


def test_asr_calls_count_utterances_not_students() -> None:
    async def scenario(students: int) -> int:
        pipeline, asr, _ = make_pipeline(students=students)
        await pipeline.start()
        try:
            await run_utterances(pipeline, 2000, 3000, 4000)
            stats = pipeline.stats_snapshot()
            assert stats.asr_calls == len(asr.calls), "計数器がエンジンの実呼び出しと食い違う"
            return stats.asr_calls
        finally:
            await pipeline.stop()

    counts = {n: asyncio.run(scenario(n)) for n in (1, 10, 40)}
    assert len(set(counts.values())) == 1, f"生徒数で ASR 回数が動いた: {counts}"


def test_partial_calls_are_counted_separately() -> None:
    """partial は `asr_calls` に混ざらない。

    既定（`partial.enabled: false`）では interim が1本も走らないので、
    発話を流したあと `asr_calls` だけが増えて `asr_partial_calls` は 0 のまま
    ——この2つが同じ計数器なら片方だけ動くことはありえない。
    """

    async def scenario() -> None:
        pipeline, asr, _ = make_pipeline()
        await pipeline.start()
        try:
            await run_utterances(pipeline, 2000, 3000)
            stats = pipeline.stats_snapshot()
            assert stats.asr_calls > 0, "確定 Segment の推論が数えられていない"
            assert stats.asr_partial_calls == 0, "partial 無効なのに interim が数えられた"
            # エンジンの実呼び出し回数は確定ぶんだけ＝partial は1本も走っていない
            assert len(asr.calls) == stats.asr_calls
        finally:
            await pipeline.stop()

    asyncio.run(scenario())


# ---- 2: MT回数は言語の数であって生徒の数ではない ----


def test_mt_calls_track_languages_not_students() -> None:
    async def scenario(students: int) -> int:
        pipeline, _, mt = make_pipeline(students=students, langs=["en"])
        await pipeline.start()
        try:
            await run_utterances(pipeline, 2000, 3000)
            stats = pipeline.stats_snapshot()
            assert stats.mt_calls == len(mt.calls), "計数器がエンジンの実呼び出しと食い違う"
            return stats.mt_calls
        finally:
            await pipeline.stop()

    counts = {n: asyncio.run(scenario(n)) for n in (1, 10, 40)}
    assert len(set(counts.values())) == 1, f"同一言語の人数で MT 回数が動いた: {counts}"


def test_mt_calls_scale_with_active_languages() -> None:
    """言語を増やせば回数は増える（増えないことの主張は生徒軸に限られる）。"""

    async def scenario(langs: list[str]) -> int:
        pipeline, _, _ = make_pipeline(students=len(langs), langs=langs)
        await pipeline.start()
        try:
            await run_utterances(pipeline, 2000)
            return pipeline.stats_snapshot().mt_calls
        finally:
            await pipeline.stop()

    one = asyncio.run(scenario(["en"]))
    two = asyncio.run(scenario(["en", "zh"]))
    assert two == one * 2, f"2言語で {two} 回（1言語 {one} 回の2倍のはず）"


def test_cache_hits_are_not_counted_as_calls() -> None:
    """同じ原文の2回目は推論していないので `mt_calls` は増えない。"""

    async def scenario() -> None:
        pipeline, _, mt = make_pipeline(students=1, langs=["en"])
        await pipeline.start()
        try:
            await run_utterances(pipeline, 2000)
            after_first = pipeline.stats_snapshot().mt_calls
            await run_utterances(pipeline, 2000)  # 同一の原文
            stats = pipeline.stats_snapshot()
            assert stats.mt_cache_hits > 0, "2回目はキャッシュで返っているはず"
            assert stats.mt_calls == after_first, "キャッシュヒットが推論回数に混ざった"
            assert stats.mt_calls == len(mt.calls)
        finally:
            await pipeline.stop()

    asyncio.run(scenario())


# ---- 4: 破棄は通知の数ではなく実数で数える ----


def test_dropped_audio_is_counted_in_segments_and_seconds() -> None:
    """`limits.asr_queue_seconds` 到達で捨てたぶんが実数で残る。

    ワーカーを起動しないので、積んだ音声は誰も引き取らず必ず上限に当たる。
    """

    async def scenario() -> None:
        limits = LimitsConfig(asr_queue_seconds=2)
        pipeline, _, _ = make_pipeline(limits=limits)
        for key in range(2000, 2000 + 6 * 1000, 1000):
            await pipeline.feed_audio(utterance_bytes(key))
        stats = pipeline.stats_snapshot()
        assert stats.asr_dropped_segments > 0, "上限を超えたのに破棄が数えられていない"
        assert stats.asr_dropped_seconds > 0
        # 捨てたのは「上限を超えたぶん」であって全部ではない
        assert pipeline.audio_queue_seconds > 0

    asyncio.run(scenario())


def test_no_drops_in_a_healthy_run() -> None:
    async def scenario() -> None:
        pipeline, _, _ = make_pipeline()
        await pipeline.start()
        try:
            await run_utterances(pipeline, 2000, 3000)
            stats = pipeline.stats_snapshot()
            assert stats.asr_dropped_segments == 0
            assert stats.asr_dropped_seconds == 0.0
        finally:
            await pipeline.stop()

    asyncio.run(scenario())


# ---- 生存タスク数（外からは見えないので、サーバー自身が報告する） ----


def test_tasks_reflects_running_workers() -> None:
    """ワーカーを起動すればタスクは増え、止めれば戻る（タスクリークの検出面）。"""

    async def scenario() -> None:
        pipeline, _, _ = make_pipeline()
        idle = pipeline.stats_snapshot().tasks
        await pipeline.start()
        try:
            running = pipeline.stats_snapshot().tasks
            assert running > idle, "ワーカー3本＋warmup が生存タスクに出ていない"
        finally:
            await pipeline.stop()
        # stop 後にワーカーが残っていたらそれ自体がタスクリーク
        assert pipeline.stats_snapshot().tasks <= idle

    asyncio.run(scenario())


# ---- 端数の掃き出し（#32 で見つけた回帰） ----


def test_queued_audio_returns_to_exactly_zero() -> None:
    """全部さばいたら滞留は**厳密に 0**になる。

    積むのは到着順・引くのは処理順で足し引きの順序が違うため、2発話を
    続けて流すと 2.2e-16 が残っていた。残ると「滞留は空か」を `> 0` で
    見ている2か所が恒久的に「空でない」と読み、
      - `_enqueue_interim` が partial を二度と出さなくなる（#29 が静かに死ぬ）
      - `_enqueue_asr` の「空なら長さによらず必ず受ける」carve-out が消える
    という形で表に出る。どちらも例外もログも出さないので、
    この不変条件をテストで固定しておかないと気づけない。
    """

    async def scenario() -> None:
        pipeline, _, _ = make_pipeline()
        await pipeline.start()
        try:
            await run_utterances(pipeline, 2000, 3000, 4000)
            assert pipeline.audio_queue_seconds == 0.0, (
                f"滞留に端数が残った: {pipeline.audio_queue_seconds!r}"
            )
            assert not pipeline.audio_queue_seconds > 0  # 2か所の述語が見るのはこれ
        finally:
            await pipeline.stop()

    asyncio.run(scenario())


def test_interim_gate_reopens_after_the_queue_drains() -> None:
    """滞留が空に戻れば interim の門は再び開く（#32 で見つけた回帰の実害）。

    `_enqueue_interim` は次の述語で partial を抑制する:

        self._interim_in_flight or not self._asr_queue.empty() or self._queued_audio_s > 0

    端数 2.2e-16 が残ると第3項が**恒久的に真**になり、以後 interim は
    1件も積まれない ＝ partial 字幕（#29）が例外もログも出さずに死ぬ。
    ここで見るのは述語そのもので、partial の発火タイミング（ASR の速さに依存）
    ではない。発火の有無で書くと、フェイクASRが速すぎる日に緑のまま通ってしまう。
    """

    async def scenario() -> None:
        pipeline, _, _ = make_pipeline()
        await pipeline.start()
        try:
            await run_utterances(pipeline, 2000, 3000, 4000)
            suppressed = (
                pipeline._interim_in_flight
                or not pipeline._asr_queue.empty()
                or pipeline.audio_queue_seconds > 0
            )
            assert not suppressed, (
                "滞留が空なのに interim が抑制されている: "
                f"queued={pipeline.audio_queue_seconds!r}"
            )
        finally:
            await pipeline.stop()

    asyncio.run(scenario())
