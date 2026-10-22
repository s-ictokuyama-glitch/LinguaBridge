from __future__ import annotations

import pytest
from starlette.testclient import TestClient, WebSocketTestSession

from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, VadConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine

JOIN_CODE = "4831"


@pytest.fixture(autouse=True)
def local_network_inventory(monkeypatch):
    """OS境界を固定し、開発PCのNICや接続中のWi-Fiにテストを依存させない。"""
    from server.network import InterfaceAddress

    monkeypatch.setattr("server.network.list_addresses", lambda: [
        InterfaceAddress("Wi-Fi", "192.168.5.25", True, "physical")
    ])


def make_ws_test_config() -> AppConfig:
    """WS境界テスト用の設定。VADは決定的な energy
    （テストが送る定数振幅PCMを Silero は音声と判定しないため）。"""
    return AppConfig(vad=VadConfig(engine="energy", threshold=300))


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
    return create_app(
        make_ws_test_config(),
        asr_engine=asr_engine,
        mt_engine=mt_engine,
        join_code=JOIN_CODE,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c
