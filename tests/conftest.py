from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig
from server.main import create_app
from server.mt.fake_engine import FakeTranslationEngine

JOIN_CODE = "4831"


@pytest.fixture
def asr_engine() -> FakeASREngine:
    return FakeASREngine()


@pytest.fixture
def mt_engine() -> FakeTranslationEngine:
    return FakeTranslationEngine(["en", "zh"])


@pytest.fixture
def app(asr_engine: FakeASREngine, mt_engine: FakeTranslationEngine):
    return create_app(
        AppConfig(),
        asr_engine=asr_engine,
        mt_engine=mt_engine,
        join_code=JOIN_CODE,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c
