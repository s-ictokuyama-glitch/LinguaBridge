"""実モデルを使う翻訳エンジン統合テスト（イシュー#12）。

モデル未取得の環境ではスキップされる。Hy-MT2 は temperature=0（貪欲）で
決定的にして翻訳内容を検証する。
"""

from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from server.config import AppConfig, MtConfig, VadConfig
from server.main import create_app
from server.asr.fake_engine import FakeASREngine
from tests.conftest import JOIN_CODE
from tests.helpers import utterance_bytes

MODELS_DIR = AppConfig().models.resolved_dir
SENTENCE = "光合成には日光と水と二酸化炭素が必要です。"

requires_nllb = pytest.mark.skipif(
    not (MODELS_DIR / "nllb-200-distilled-600M-ct2").exists(),
    reason="NLLBモデル未取得（scripts/download_models.py を実行）",
)
requires_hymt = pytest.mark.skipif(
    not (MODELS_DIR / "hy-mt2" / "Hy-MT2-1.8B-Q4_K_M.gguf").exists(),
    reason="Hy-MT2モデル未取得（scripts/download_models.py を実行）",
)

HIRAGANA = re.compile(r"[ぁ-ん]")


@pytest.fixture(scope="module")
def nllb_engine():
    from server.mt.nllb_engine import NllbEngine

    engine = NllbEngine(
        MODELS_DIR / "nllb-200-distilled-600M-ct2", MODELS_DIR / "nllb-tokenizer"
    )
    engine.warmup()
    return engine


@pytest.fixture(scope="module")
def hymt_engine():
    from server.mt.hymt_engine import HyMt2Engine

    engine = HyMt2Engine(
        MODELS_DIR / "hy-mt2" / "Hy-MT2-1.8B-Q4_K_M.gguf", threads=4, temperature=0.0
    )
    engine.warmup()
    return engine


@requires_nllb
class TestNllbEngine:
    def test_translates_to_english(self, nllb_engine):
        out = nllb_engine.translate(SENTENCE, "en")
        assert "photosynthesis" in out.lower()
        assert not HIRAGANA.search(out)

    def test_translates_to_simplified_chinese(self, nllb_engine):
        out = nllb_engine.translate(SENTENCE, "zh")
        assert "二氧化碳" in out  # 二酸化炭素の簡体字
        assert not HIRAGANA.search(out)


@requires_hymt
class TestHyMt2Engine:
    def test_translates_to_english(self, hymt_engine):
        out = hymt_engine.translate(SENTENCE, "en")
        assert "photosynthesis" in out.lower()
        assert not HIRAGANA.search(out)

    def test_translates_to_simplified_chinese(self, hymt_engine):
        out = hymt_engine.translate(SENTENCE, "zh")
        assert "光合作用" in out  # 光合成の正しい中国語訳（NLLBが誤りがちな箇所）
        assert not HIRAGANA.search(out)


@requires_hymt
def test_build_mt_engine_constructs_hymt_from_config():
    """受け入れ基準: 設定のみで hy-mt2 が結線される（config→ファクトリの経路）。"""
    from server.main import build_mt_engine
    from server.mt.hymt_engine import HyMt2Engine

    engine = build_mt_engine(AppConfig(mt=MtConfig(engine="hy-mt2")))
    assert isinstance(engine, HyMt2Engine)


@requires_nllb
class TestRealMtOverWebSocket:
    def test_fake_asr_text_reaches_student_really_translated(self):
        """受け入れ基準: 選択言語の生徒にのみ実訳文が届く（フェイクASR＋実NLLB）。

        WS境界の機能テスト様式のまま翻訳エンジンだけ実物に差し替える。
        （実ASR＋実翻訳のフル構成は実ソケットのスモークで別途確認）
        """
        config = AppConfig(
            vad=VadConfig(engine="energy", threshold=300),
            mt=MtConfig(engine="nllb"),
        )
        app = create_app(config, asr_engine=FakeASREngine(), join_code=JOIN_CODE)
        with TestClient(app) as client:
            with (
                client.websocket_connect("/ws") as teacher,
                client.websocket_connect("/ws") as student,
            ):
                student.send_json(
                    {"type": "join", "role": "student", "code": JOIN_CODE, "lang": "en"}
                )
                assert student.receive_json()["type"] == "joined"
                teacher.send_json({"type": "join", "role": "teacher", "code": JOIN_CODE})
                assert teacher.receive_json()["type"] == "joined"
                teacher.send_json({"type": "control", "action": "start"})
                assert student.receive_json() == {"type": "session", "state": "live"}

                # フェイクASRの規約: 振幅2000 → 「光合成には日光が必要です。」
                for chunk in utterance_bytes(2000):
                    teacher.send_bytes(chunk)

                cap = student.receive_json()
                assert cap["type"] == "caption"
                assert cap["ja"] == "光合成には日光が必要です。"
                assert cap["lang"] == "en"
                assert "photosynthesis" in cap["text"].lower()
                assert not HIRAGANA.search(cap["text"])
