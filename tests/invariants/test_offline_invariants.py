"""不変条件: runtime offline mode = 0 external requests（#23）。

「完全ローカル・クラウド送信ゼロ」は依頼者が絶対要件として挙げた性質で、
ASRエンジン差し替え（#28）やモデル読み込み経路の変更で最も壊れやすい。
授業1回分を丸ごとソケットガード下で回し、ループバック・LAN 以外への接続が
1件も無いことを主張する。
"""

from __future__ import annotations

import socket

import pytest
from starlette.testclient import TestClient

from server.asr.fake_engine import FakeASREngine
from server.main import create_app, get_lan_ip
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.integration.test_ws_boundary import (
    join_student,
    join_teacher,
    send_utterance,
    start_session,
)
from tests.net_guard import guard_network


def make_app():
    return create_app(
        make_ws_test_config(),
        asr_engine=FakeASREngine(),
        mt_engine=FakeTranslationEngine(["en", "zh"]),
        join_code=JOIN_CODE,
    )


class TestGuardItself:
    """ガードが本当に検出できることを先に固定する
    （検出できないガードは「違反ゼロ」を常に主張してしまう）。"""

    def test_external_connect_is_detected(self):
        with guard_network() as guard:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))  # UDP なので実送信はしないが connect は記録される
            assert [d.host for d in guard.external] == ["8.8.8.8"]
        with pytest.raises(AssertionError, match="8.8.8.8"):
            guard.assert_no_external_traffic()

    def test_loopback_and_lan_are_not_violations(self):
        with guard_network() as guard:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("127.0.0.1", 9))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.168.1.42", 9))
        assert guard.external == []
        guard.assert_no_external_traffic()

    def test_restores_socket_module_on_exit(self):
        before = socket.socket.connect
        with guard_network():
            assert socket.socket.connect is not before
        assert socket.socket.connect is before


class TestNoExternalTraffic:
    def test_full_lesson_makes_no_external_connection(self):
        """起動 → 参加 → 配信 → 記録ON → 終了 の全経路で外部接続ゼロ。"""
        with guard_network() as guard:
            app = make_app()
            with TestClient(app) as client:
                with (
                    client.websocket_connect("/ws") as teacher,
                    client.websocket_connect("/ws") as student_en,
                    client.websocket_connect("/ws") as student_zh,
                ):
                    join_teacher(teacher)
                    join_student(student_en, "en")
                    join_student(student_zh, "zh")
                    start_session(teacher)
                    student_en.receive_json()  # session live
                    student_zh.receive_json()

                    teacher.send_json({"type": "recording", "on": True})
                    for key in (1000, 2000, 3000):
                        send_utterance(teacher, key=key)
                    for _ in range(3):
                        assert student_en.receive_json()["type"] in {"caption", "recording"}
                    teacher.send_json({"type": "control", "action": "end"})
                client.get("/api/config")
                client.get("/healthz")
                client.get("/api/teacher-info")  # get_lan_ip() を通る経路
        guard.assert_no_external_traffic()

    def test_teacher_info_does_not_resolve_public_hostnames(self):
        """参加コードの表示（get_lan_ip 経由）が FQDN を引かないこと。"""
        with guard_network() as guard:
            app = make_app()
            with TestClient(app) as client:
                assert client.get("/api/teacher-info").status_code == 200
        assert guard.external_lookups == []

    def test_get_lan_ip_makes_no_external_connection(self):
        """#23 以前の実装は 8.8.8.8 へ UDP connect していた。ここが回帰の本丸。"""
        with guard_network() as guard:
            get_lan_ip()
        guard.assert_no_external_traffic()
