import re

import pytest

from server.config import AppConfig
from server.mt.nllb_engine import NllbEngine


MODELS = AppConfig().models.resolved_dir
pytestmark = [
    pytest.mark.timeout(180),
    pytest.mark.skipif(
        not (MODELS / "nllb-200-distilled-600M-ct2").exists()
        or not (MODELS / "nllb-tokenizer").exists(),
        reason="NLLB モデルと tokenizer が必要",
    ),
]


@pytest.fixture(scope="module")
def engine():
    model = NllbEngine(MODELS / "nllb-200-distilled-600M-ct2", MODELS / "nllb-tokenizer")
    model.warmup()
    return model


@pytest.mark.parametrize("lang, pattern", [
    ("en", r"(?:hello|hi|hey)[.!]?"),
    ("zh", r"(?:你好|您好)[。.!！]?"),
])
@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="#53: beam=1 の NLLB は挨拶を反復するか、原文にない質問を足す",
)
def test_greeting_keeps_only_the_greeting(engine, lang, pattern):
    output = engine.translate("こんにちは。", lang)
    assert re.fullmatch(pattern, output, re.IGNORECASE), output


def test_english_classroom_sentence_keeps_the_required_materials(engine):
    output = engine.translate("光合成には日光と水と二酸化炭素が必要です。", "en").lower()
    for term in ("photosynthesis", "sunlight", "water", "carbon dioxide"):
        assert term in output, output


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="#53: NLLB は光合成を中国語の光合作用へ訳せない",
)
def test_chinese_classroom_sentence_keeps_the_scientific_term(engine):
    output = engine.translate("光合成には日光と水と二酸化炭素が必要です。", "zh")
    for term in ("光合作用", "水", "二氧化碳"):
        assert term in output, output
    assert "阳光" in output, output
