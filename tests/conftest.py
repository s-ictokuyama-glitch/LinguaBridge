from __future__ import annotations

import pytest
from starlette.testclient import TestClient

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
