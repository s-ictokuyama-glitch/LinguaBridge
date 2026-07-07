from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, VadConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine

JOIN_CODE = "4831"


def test_config() -> AppConfig:
    """WS境界テスト用の設定。VADは決定的な energy
    （テストが送る定数振幅PCMを Silero は音声と判定しないため）。"""
    return AppConfig(vad=VadConfig(engine="energy", threshold=300))


@pytest.fixture
def asr_engine() -> FakeASREngine:
    return FakeASREngine()


@pytest.fixture
def mt_engine() -> FakeTranslationEngine:
    return FakeTranslationEngine(["en", "zh"])


@pytest.fixture
def app(asr_engine: FakeASREngine, mt_engine: FakeTranslationEngine):
    return create_app(
        test_config(),
        asr_engine=asr_engine,
        mt_engine=mt_engine,
        join_code=JOIN_CODE,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c
