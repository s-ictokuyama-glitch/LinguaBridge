"""先生モニタリング（イシュー#15）のWS境界テスト。

統計の定期配信・過負荷警告・無音警告を、フェイクエンジンに gate を注入して
決定的に再現する。
"""

from __future__ import annotations

import threading

from starlette.testclient import TestClient

from server.asr.fake_engine import FakeASREngine
from server.config import MonitoringConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.integration.test_ws_boundary import (
    join_student,
    join_teacher,
    send_utterance,
    start_session,
)

# テストは統計間隔・無音閾値を小さくして待ち時間を詰める
FAST_MONITORING = MonitoringConfig(
    stats_interval_s=0.05, silence_warning_s=0.15, overload_queue_depth=3
)


def make_app(*, mt_engine=None, monitoring=FAST_MONITORING, join_code=JOIN_CODE):
    config = make_ws_test_config()
    config.monitoring = monitoring
    return create_app(
        config,
        asr_engine=FakeASREngine(),
        mt_engine=mt_engine or FakeTranslationEngine(["en", "zh"]),
        join_code=join_code,
    )


def drain_until(ws, predicate, max_msgs=400):
    """条件に合うメッセージが来るまで受信して返す。統計が定期配信されるので
    receive_json はブロックし続けない。"""
    for _ in range(max_msgs):
        msg = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError("条件に合うメッセージが所定回数内に届かなかった")


def next_stats(ws):
    return drain_until(ws, lambda m: m["type"] == "stats")


class TestStatsBroadcast:
    def test_stats_report_students_and_language_breakdown(self):
        app = make_app()
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as s_en1,
                client.websocket_connect("/ws") as s_en2,
                client.websocket_connect("/ws") as s_zh,
            ):
                join_teacher(teacher)
                join_student(s_en1, "en")
                join_student(s_en2, "en")
                join_student(s_zh, "zh")

                stats = next_stats(teacher)
                assert stats["students"] == 3
                assert stats["langs"] == {"en": 2, "zh": 1}
                assert stats["queue_depth"] == 0
                assert isinstance(stats["median_delay_ms"], int)
                assert stats["overloaded"] is False

    def test_stats_reflect_language_change_and_leave(self):
        app = make_app()
        with TestClient(app) as client, client.websocket_connect("/ws") as teacher:
            join_teacher(teacher)
            with client.websocket_connect("/ws") as student:
                join_student(student, "en")
                assert next_stats(teacher)["langs"] == {"en": 1}

                student.send_json({"type": "set_lang", "lang": "zh"})
                drain_until(teacher, lambda m: m["type"] == "stats" and m["langs"] == {"zh": 1})
            # 生徒切断後は接続数0に戻る（teacher は外側 with なので生存）
            drain_until(teacher, lambda m: m["type"] == "stats" and m["students"] == 0)

    def test_median_delay_populated_after_captions(self):
        app = make_app()
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student,
            ):
                join_teacher(teacher)
                join_student(student, "en")
                start_session(teacher)
                student.receive_json()  # session live
                for _ in range(3):
                    send_utterance(teacher, key=1000)
                for _ in range(3):
                    assert student.receive_json()["type"] == "caption"
                assert next_stats(teacher)["median_delay_ms"] >= 0


class TestOverloadWarning:
    def test_overload_appears_on_backlog_and_clears_when_drained(self):
        gate = threading.Event()  # セットされるまでMTワーカーを止める
        mt = FakeTranslationEngine(["en", "zh"], gate=gate)
        app = make_app(mt_engine=mt)
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student,
            ):
                join_teacher(teacher)
                join_student(student, "en")
                start_session(teacher)
                student.receive_json()  # session live

                # 5発話を送るとMTジョブが滞留する（ワーカーは1件目でgate待ち）
                for _ in range(5):
                    send_utterance(teacher, key=1000)

                overloaded = drain_until(
                    teacher, lambda m: m["type"] == "stats" and m["overloaded"]
                )
                assert overloaded["queue_depth"] >= 3

                # gate を開けると処理が進み、過負荷は解消する
                gate.set()
                cleared = drain_until(
                    teacher,
                    lambda m: m["type"] == "stats"
                    and not m["overloaded"]
                    and m["queue_depth"] == 0,
                )
                assert cleared["overloaded"] is False


class TestSilenceWarning:
    def test_mic_silent_warning_after_live_silence(self):
        app = make_app()
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student,
            ):
                join_teacher(teacher)
                join_student(student, "en")
                start_session(teacher)
                student.receive_json()  # session live
                # 音声を一切送らない → silence_warning_s 経過で mic_silent が届く
                warn = drain_until(
                    teacher, lambda m: m["type"] == "error" and m["code"] == "mic_silent"
                )
                assert "秒" in warn["message"]

    def test_no_silence_warning_before_live(self):
        # idle 中は無音でも警告しない（配信開始前）
        app = make_app()
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student,
            ):
                join_teacher(teacher)
                join_student(student, "en")
                # start しないまま複数回の統計を受ける間、mic_silent は来ない
                seen = [teacher.receive_json() for _ in range(8)]
                assert all(
                    not (m["type"] == "error" and m["code"] == "mic_silent") for m in seen
                )
                assert any(m["type"] == "stats" for m in seen)

    def test_teacher_swap_during_silence_rearms_warning(self):
        # live のまま先生が入れ替わっても、新しい先生が継続中の無音警告を見られる
        app = make_app()
        with TestClient(app) as client, client.websocket_connect("/ws") as teacher1:
            join_teacher(teacher1)
            start_session(teacher1)
            drain_until(teacher1, lambda m: m["type"] == "error" and m["code"] == "mic_silent")
            # 後勝ちで新しい先生に入れ替わる（state は live のまま）
            with client.websocket_connect("/ws") as teacher2:
                assert join_teacher(teacher2)["session_state"] == "live"
                warn = drain_until(
                    teacher2, lambda m: m["type"] == "error" and m["code"] == "mic_silent"
                )
                assert warn["code"] == "mic_silent"
