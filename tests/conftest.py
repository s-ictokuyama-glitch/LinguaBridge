from __future__ import annotations

import pytest
from starlette.testclient import TestClient, WebSocketTestSession

from server.publication import Publication
from server.asr.fake_engine import FakeASREngine
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine

from tests.helpers import JOIN_CODE, make_ws_test_config


@pytest.fixture(autouse=True)
def local_network_inventory(monkeypatch):
    """OS境界を固定し、開発PCのNICや接続中のWi-Fiにテストを依存させない。"""
    from server.network import InterfaceAddress

    monkeypatch.setattr("server.network.list_addresses", lambda: [
        InterfaceAddress("Wi-Fi", "192.168.5.25", True, "physical")
    ])


# `speaking`（#29）は全クライアントへ届く通知で、既存のテストはどれも
# receive_json() を位置で読んでいる（126箇所）。ここで1箇所だけ透過的に読み飛ばす。
#
# **読み飛ばしはこのフィクスチャの中だけ**で、speaking 自体は
# tests/integration/test_partial.py が raw_receive_json() で生のフレーム列を読んで
# 検証する（「変化時だけ送る」「生徒に partial が届かない」はそこで固定される）。
_SKIPPED_FRAME_TYPES = ("speaking",)
# 差し替え前の実装。raw_receive_json はこれを直接呼ぶ（差し替えを迂回する）
_ORIGINAL_RECEIVE_JSON = WebSocketTestSession.receive_json


@pytest.fixture(autouse=True)
def skip_indicator_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """字幕・制御の流れを見るテストから「発話中」通知を隠す（#29）。"""

    def receive_json(self, mode: str = "text"):  # type: ignore[no-untyped-def]
        while True:
            msg = _ORIGINAL_RECEIVE_JSON(self, mode)
            if not isinstance(msg, dict) or msg.get("type") not in _SKIPPED_FRAME_TYPES:
                return msg

    monkeypatch.setattr(WebSocketTestSession, "receive_json", receive_json)


def raw_receive_json(ws, mode: str = "text"):  # type: ignore[no-untyped-def]
    """読み飛ばしを迂回して生のフレームを読む（#29 のテスト用）。"""
    return _ORIGINAL_RECEIVE_JSON(ws, mode)


@pytest.fixture
def asr_engine() -> FakeASREngine:
    return FakeASREngine()


@pytest.fixture
def mt_engine() -> FakeTranslationEngine:
    return FakeTranslationEngine(["en", "zh"])


@pytest.fixture
def app(asr_engine: FakeASREngine, mt_engine: FakeTranslationEngine):
    config = make_ws_test_config()
    return create_app(
        config, Publication(config.server),
        asr_engine=asr_engine,
        mt_engine=mt_engine,
        join_code=JOIN_CODE,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def launcher(tmp_path):
    from tests.listeners import Launcher
    return Launcher(tmp_path)
