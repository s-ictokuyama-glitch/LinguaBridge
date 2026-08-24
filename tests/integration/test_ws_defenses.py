"""WSの防御（#25 A-4 / whisper-flow W-4）。

Origin 検証・受信サイズ上限・生徒数上限。いずれも
「断るべきものを断る」と「断ってはいけないものを通す」の両方を主張する。
LAN内の正規の使い方（ブラウザ・replay_client）が巻き添えで落ちないことが要点。
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.asr.fake_engine import FakeASREngine
from server.main import create_app, origin_allowed
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config
from tests.integration.test_ws_boundary import join_student, join_teacher


def make_app(**limit_overrides):
    config = make_ws_test_config()
    for key, value in limit_overrides.items():
        setattr(config.limits, key, value)
    return create_app(
        config,
        asr_engine=FakeASREngine(),
        mt_engine=FakeTranslationEngine(["en", "zh"]),
        join_code=JOIN_CODE,
    )


class TestOriginRule:
    @pytest.mark.parametrize(
        "origin, host",
        [
            (None, "192.168.1.34:8000"),  # 非ブラウザ（replay_client 等）
            ("http://192.168.1.34:8000", "192.168.1.34:8000"),  # 生徒ページ
            ("https://192.168.1.34:8443", "192.168.1.34:8443"),  # 先生ページ
            ("http://localhost:8000", "localhost:8000"),
        ],
    )
    def test_same_host_and_headerless_clients_are_allowed(self, origin, host):
        assert origin_allowed(origin, host, []) is True

    @pytest.mark.parametrize(
        "origin",
        [
            "https://evil.example.com",
            "http://192.168.1.99:8000",  # LAN内の別ホストに置かれたページ
            "null",  # file:// / サンドボックス
        ],
    )
    def test_other_hosts_are_rejected(self, origin):
        assert origin_allowed(origin, "192.168.1.34:8000", []) is False

    def test_explicit_allow_list_wins(self):
        allowed = ["https://kiosk.school.local"]
        assert origin_allowed("https://kiosk.school.local", "192.168.1.34:8000", allowed) is True

    def test_rejected_origin_cannot_open_the_socket(self):
        with TestClient(make_app()) as client:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(
                    "/ws", headers={"origin": "https://evil.example.com"}
                ) as ws:
                    ws.receive_json()

    def test_same_origin_browser_client_still_works(self):
        with TestClient(make_app()) as client:
            with client.websocket_connect(
                "/ws", headers={"origin": "http://testserver"}
            ) as ws:
                assert join_student(ws, "en")["type"] == "joined"


class TestMessageSizeLimits:
    def test_oversized_text_is_refused_without_dropping_the_connection(self):
        """大きすぎるテキストは断るが、接続は維持する
        （先生の control が効かなくなる方が授業では困る）。"""
        with TestClient(make_app(max_text_bytes=256)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_text("x" * 1000)
                msg = ws.receive_json()
                assert msg["type"] == "error"
                assert msg["code"] == "too_large"
                # 接続は生きており、正規のメッセージは通る
                assert join_student(ws, "en")["type"] == "joined"

    def test_the_text_limit_counts_bytes_not_characters(self):
        """日本語は1文字3バイト。文字数で測ると上限が実質3倍になる。"""
        with TestClient(make_app(max_text_bytes=256)) as client:
            with client.websocket_connect("/ws") as ws:
                ws.send_text("あ" * 100)  # 100文字 = 300バイト
                assert ws.receive_json()["code"] == "too_large"

    def test_oversized_audio_frame_is_discarded_and_the_teacher_keeps_control(self):
        with TestClient(make_app(max_audio_bytes=3200)) as client:
            with client.websocket_connect("/ws") as teacher:
                join_teacher(teacher)
                teacher.send_json({"type": "control", "action": "start"})
                assert teacher.receive_json()["state"] == "live"
                teacher.send_bytes(b"\x00" * 100_000)  # 上限超過。捨てられる
                # 受信ループが生きていることを control の往復で示す
                teacher.send_json({"type": "control", "action": "pause"})
                assert teacher.receive_json()["state"] == "paused"


class TestStudentCap:
    def test_students_beyond_the_cap_are_told_the_room_is_full(self):
        with TestClient(make_app(max_students=2)) as client:
            with (
                client.websocket_connect("/ws") as a,
                client.websocket_connect("/ws") as b,
            ):
                assert join_student(a, "en")["type"] == "joined"
                assert join_student(b, "en")["type"] == "joined"
                with client.websocket_connect("/ws") as c:
                    assert join_student(c, "en") == {
                        "type": "join_rejected",
                        "reason": "full",
                    }

    def test_the_cap_does_not_block_a_rejoin_on_the_same_socket(self):
        """同じ接続での join し直し（言語変更のための再join等）は席を増やさない。"""
        with TestClient(make_app(max_students=1)) as client:
            with client.websocket_connect("/ws") as a:
                assert join_student(a, "en")["type"] == "joined"
                assert join_student(a, "zh")["type"] == "joined"

    def test_the_cap_does_not_block_the_teacher(self):
        with TestClient(make_app(max_students=1)) as client:
            with client.websocket_connect("/ws") as student:
                join_student(student, "en")
                with client.websocket_connect("/ws") as teacher:
                    assert join_teacher(teacher)["type"] == "joined"
