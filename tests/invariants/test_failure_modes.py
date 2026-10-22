"""失敗系の不変条件（#23）。

「壊れ方」を固定する。モデル欠損・切断・遅い生徒・キュー溢れ・不正コード・
シャットダウンのそれぞれで、サーバーがどう振る舞うべきかを主張する。

P0（#25）の2件はここで緑になった（#25 で境界・バックプレッシャ・
生徒ごとの送信キューを入れた）。以前は `xfail(strict=True)` が付いており、
実装が直った時点で「予期せず通った」で赤くなってマーカーの除去を強制した。

キュー溢れと遅い生徒は Pipeline のシームで検証する。WS境界だと違反時に
receive が無限ブロックし、pytest-timeout の thread 方式がテストプロセスごと
落としてしまうため（xfail として報告されない）。
"""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np
import pytest
from starlette.testclient import TestClient

from server import ws_protocol as proto
from server.asr.base import ASREngine, ASRResult
from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, AsrConfig, ModelsConfig, MtConfig
from server.main import build_mt_engine, create_app
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.session import Client, Session
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.helpers import chunks, speech_pcm, utterance_bytes
from tests.integration.test_ws_boundary import (
    PHRASE_1000,
    join_student,
    join_teacher,
    send_utterance,
    start_session,
)

# キュー溢れ・遅い生徒の判定に使う上限。実装が正しければ桁違いに速く終わる
RESPONSIVE_S = 1.0
# 「遅い生徒」が1件の送信に費やす時間。RESPONSIVE_S より十分大きくする
SLOW_STUDENT_S = 3.0


class GatedASREngine(ASREngine):
    """transcribe が gate を待つASR。ASRキューを溢れさせるために使う。"""

    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate
        self.calls = 0

    def transcribe(self, pcm16: np.ndarray, sample_rate: int) -> ASRResult:
        self.calls += 1
        self.gate.wait()
        return ASRResult(text="（gate）")


class RecordingSocket:
    """ClientSocket の最小実装。送られたペイロードを貯める。"""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.sent: list[dict] = []

    async def send_json(self, data: object) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        self.sent.append(dict(data))  # type: ignore[arg-type]

    async def close(self, code: int = 1000) -> None:
        return None


def make_app(*, asr_engine=None, mt_engine=None):
    return create_app(
        make_ws_test_config(),
        asr_engine=asr_engine or FakeASREngine(),
        mt_engine=mt_engine or FakeTranslationEngine(["en", "zh"]),
        join_code=JOIN_CODE,
    )


# --------------------------------------------------------------------------
# モデル欠損: fail closed（クラウドにもフェイクにも逃げない）
# --------------------------------------------------------------------------


class TestFailClosedOnMissingModels:
    def test_missing_asr_model_never_falls_back_to_fake(self, tmp_path):
        config = AppConfig(
            models=ModelsConfig(dir=str(tmp_path)),
            asr=AsrConfig(engine="faster-whisper"),
            mt=MtConfig(engine="fake"),
        )
        with pytest.raises(FileNotFoundError, match="download_models"):
            create_app(config)

    def test_missing_hymt_model_never_falls_back_to_cloud(self, tmp_path):
        config = AppConfig(
            models=ModelsConfig(dir=str(tmp_path)),
            asr=AsrConfig(engine="fake"),
            mt=MtConfig(engine="hy-mt2"),
        )
        with pytest.raises(FileNotFoundError, match="download_models"):
            create_app(config)

    def test_truncated_model_is_rejected_not_loaded(self, tmp_path):
        """ダウンロード中断で途中まで落ちたGGUFを掴んだまま起動しない。"""
        gguf = tmp_path / "hy-mt2" / "Hy-MT2-1.8B-Q4_K_M.gguf"
        gguf.parent.mkdir(parents=True)
        gguf.write_bytes(b"\x00" * 1024)
        config = AppConfig(
            models=ModelsConfig(dir=str(tmp_path)),
            asr=AsrConfig(engine="fake"),
            mt=MtConfig(engine="hy-mt2"),
        )
        with pytest.raises(FileNotFoundError, match="不完全"):
            create_app(config)

    def test_unknown_engine_name_is_an_error_not_a_silent_fallback(self):
        config = AppConfig(mt=MtConfig(engine="some-cloud-api"))
        with pytest.raises(NotImplementedError, match="some-cloud-api"):
            build_mt_engine(config)


# --------------------------------------------------------------------------
# 切断・不正参加
# --------------------------------------------------------------------------


class TestDisconnect:
    def test_teacher_disconnect_midutterance_flushes_audio_and_pauses(
        self, client, asr_engine
    ):
        """発話の途中で先生が落ちても、溜まっていた音声は捨てずに確定させる。"""
        with client.websocket_connect("/ws") as student:
            join_student(student, "en")
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                start_session(teacher)
                assert student.receive_json()["state"] == "live"
                # 発話終了を判定する無音を付けずに切る（発話は宙ぶらりんのまま）
                for chunk in chunks(speech_pcm(1000, 1.0)):
                    teacher.send_bytes(chunk)

            seen: dict[str, dict] = {}
            while {"session", "caption"} - seen.keys():
                msg = student.receive_json()
                seen[msg["type"]] = msg

        assert seen["session"]["state"] == "paused", "先生切断で自動一時停止しない"
        assert seen["caption"]["ja"] == PHRASE_1000, "切断時に処理中の音声が失われた"
        assert len(asr_engine.calls) == 1

    def test_student_disconnect_does_not_stop_delivery_to_others(self, client, asr_engine):
        with client.websocket_connect("/ws") as teacher:
            join_teacher(teacher)
            with client.websocket_connect("/ws") as staying:
                join_student(staying, "en")
                with client.websocket_connect("/ws") as leaving:
                    join_student(leaving, "en")
                    start_session(teacher)
                    staying.receive_json()
                    leaving.receive_json()
                # 1人が去る
                send_utterance(teacher, key=1000)
                assert staying.receive_json()["ja"] == PHRASE_1000
        assert len(asr_engine.calls) == 1


class TestInvalidJoinCode:
    def test_rejection_does_not_leak_the_join_code(self, client):
        with client.websocket_connect("/ws") as intruder:
            msg = join_student(intruder, "en", code="0000")
            assert msg == {"type": "join_rejected", "reason": "bad_code"}
            assert JOIN_CODE not in str(msg)

    def test_rejected_client_receives_no_captions(self, client):
        """拒否された接続は字幕の配信対象にならない。

        「何も届かない」は直接は主張できないので、正しいコードで入り直したときの
        1通目が joined であること（配信済み字幕が溜まっていないこと）で示す。
        """
        with client.websocket_connect("/ws") as teacher:
            join_teacher(teacher)
            with client.websocket_connect("/ws") as intruder:
                assert join_student(intruder, "en", code="0000")["type"] == "join_rejected"
                start_session(teacher)
                send_utterance(teacher, key=1000)
                while teacher.receive_json()["type"] != "asr_final":
                    pass
                # ここまでで字幕1件が配信済み。拒否中の接続には届いていないはず
                assert join_student(intruder, "en")["type"] == "joined"


# --------------------------------------------------------------------------
# シャットダウン
# --------------------------------------------------------------------------


class TestShutdown:
    def test_shutdown_with_pending_translations_does_not_hang(self):
        gate = threading.Event()  # MTワーカーを止めたままサーバーを落とす
        mt = FakeTranslationEngine(["en", "zh"], gate=gate)
        started = time.monotonic()
        try:
            with TestClient(make_app(mt_engine=mt)) as client:
                with (
                    client.websocket_connect("/ws") as teacher,
                    client.websocket_connect("/ws") as student,
                ):
                    join_teacher(teacher)
                    join_student(student, "en")
                    start_session(teacher)
                    student.receive_json()
                    for _ in range(3):
                        send_utterance(teacher, key=1000)
                    while teacher.receive_json()["type"] != "asr_final":
                        pass
        finally:
            gate.set()  # 止めたワーカースレッドを解放（プロセス終了時のjoin対策）
        elapsed = time.monotonic() - started
        assert elapsed < 10, f"翻訳が滞留した状態の終了に {elapsed:.1f} 秒かかりました"


# --------------------------------------------------------------------------
# P0（#25）: 満杯キューと遅い生徒
# --------------------------------------------------------------------------


def _make_pipeline(asr, mt, session: Session) -> Pipeline:
    return Pipeline(session, make_ws_test_config(), asr, mt)


async def _wait_until(predicate, timeout_s: float) -> None:
    """predicate が真になるまで（最長 timeout_s）待つ。配信が非同期になったため必要。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not predicate():
        await asyncio.sleep(0.01)


class TestBackpressure:
    def test_feed_audio_never_blocks_when_asr_queue_is_full(self):
        """満杯のキューは音声を捨てるなり間引くなりして良いが、呼び出し元は止めない。

        止めると先生WSの受信ループが停止し、control（pause/end）も
        recording トグルも効かなくなる。授業中に操作不能になるということ。
        """

        async def scenario() -> None:
            gate = threading.Event()
            asr = GatedASREngine(gate)
            session = Session(join_code=JOIN_CODE)
            session.state = "live"
            # 上限を 2秒に絞る。1発話 = 音声1.0s + pre-roll なので数発話で必ず溢れる
            config = make_ws_test_config()
            config.limits.asr_queue_seconds = 2.0
            pipeline = Pipeline(session, config, asr, FakeTranslationEngine(["en"]))
            await pipeline.start()
            try:
                for _ in range(8):
                    for chunk in utterance_bytes(1000):
                        await asyncio.wait_for(
                            pipeline.feed_audio(chunk), timeout=RESPONSIVE_S
                        )
                # 実際に溢れたことを確かめる（溢れないなら何も主張できていない）
                assert pipeline.audio_queue_seconds <= config.limits.asr_queue_seconds
            finally:
                gate.set()
                await pipeline.stop()

        asyncio.run(scenario())


class TestHungInference:
    """推論が返らなくてもワーカーは死なない（#25 W-1 / whisper-flow の safe_transcribe）。

    タイムアウトは**スレッドを止めない**（run_in_executor の future をキャンセルしても
    実行中の関数は走り続ける）。主張できるのは「ループが解放され、滞留の帳簿が戻り、
    先生に理由が伝わる」ところまでで、それが正しい主張でもある。
    """

    def test_asr_timeout_releases_the_worker_and_tells_the_teacher(self):
        gate = threading.Event()

        async def scenario() -> None:
            session = Session(join_code=JOIN_CODE)
            session.state = "live"
            ws = RecordingSocket()
            session.add_client(Client(id="t", role="teacher", lang=None, ws=ws))
            config = make_ws_test_config()
            config.limits.asr_timeout_s = 0.2
            pipeline = Pipeline(session, config, GatedASREngine(gate), FakeTranslationEngine(["en"]))
            await pipeline.start()
            try:
                for chunk in utterance_bytes(1000):
                    await pipeline.feed_audio(chunk)
                assert pipeline.audio_queue_seconds > 0
                deadline = time.monotonic() + RESPONSIVE_S + 1.0
                while time.monotonic() < deadline and pipeline.audio_queue_seconds > 0:
                    await asyncio.sleep(0.02)
                assert pipeline.audio_queue_seconds == 0, "諦めたのに滞留の帳簿が戻っていない"
                await asyncio.sleep(0.05)  # 送信タスクが1件処理するのを待つ
                assert "asr_timeout" in [m.get("code") for m in ws.sent], (
                    "推論を諦めたことを先生へ伝えていない"
                )
            finally:
                gate.set()
                await pipeline.stop()

        try:
            asyncio.run(scenario())
        finally:
            gate.set()  # 止めたスレッドを解放（プロセス終了時のjoin対策）


class TestSlowStudent:
    def test_slow_student_does_not_delay_the_others(self):
        """遅い生徒1人が、他の生徒への配信を止めてはいけない。

        教室では電波の悪い端末が必ず1台は出る。そこに引きずられると
        クラス全員の字幕が遅れる。

        #25 で配信が生徒ごとの送信キュー経由（非同期）になったため、
        `broadcast_caption` の直後ではなく「RESPONSIVE_S 以内に速い生徒へ届き、
        その時点で遅い生徒はまだ送信中」であることを主張する。隔離の主張としては
        こちらの方が強い（速い側が届いた・遅い側に引きずられていない、の両方を見る）。
        """

        async def scenario() -> None:
            session = Session(join_code=JOIN_CODE)
            slow = RecordingSocket(delay_s=SLOW_STUDENT_S)
            fast = RecordingSocket()
            session.add_client(Client(id="slow", role="student", lang="en", ws=slow))
            session.add_client(Client(id="fast", role="student", lang="en", ws=fast))
            pipeline = _make_pipeline(
                FakeASREngine(), FakeTranslationEngine(["en"]), session
            )
            caption = proto.Caption(seq=1, ja="こんにちは", text="hi", lang="en", delay_ms=0)
            try:
                await asyncio.wait_for(
                    pipeline.broadcast_caption(caption), timeout=RESPONSIVE_S
                )
                await _wait_until(lambda: bool(fast.sent), RESPONSIVE_S)
                assert fast.sent, "遅い生徒より後ろの生徒に字幕が届いていない"
                assert not slow.sent, "遅い生徒がこの時点で送り終えているならテストが無効"
            finally:
                await pipeline.stop()

        asyncio.run(scenario())
