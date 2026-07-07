"""翻訳エンジンのモデル非依存部分（言語マッピング・プロンプト・エラー伝播）のユニットテスト。

実モデルでの翻訳品質・疎通は tests/integration/test_real_mt.py。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.config import AppConfig, Language, ModelsConfig, MtConfig
from server.main import build_mt_engine
from server.mt.hymt_engine import HYMT_LANG_LABELS, HyMt2Engine, build_prompt
from server.mt.nllb_engine import NLLB_LANG_CODES, NllbEngine


def test_hymt_prompt_contains_lang_label_and_text():
    prompt = build_prompt("光合成には日光が必要です。", "Simplified Chinese")
    assert "Simplified Chinese" in prompt
    assert "光合成には日光が必要です。" in prompt
    assert "only output the translated result" in prompt  # 余計な説明の抑止指示


def test_supported_languages_cover_default_config():
    default_langs = set(AppConfig().language_codes)
    assert default_langs <= set(NLLB_LANG_CODES)
    assert default_langs <= set(HYMT_LANG_LABELS)


def test_nllb_unsupported_lang_raises_before_model_load(tmp_path: Path):
    engine = NllbEngine(tmp_path, tmp_path)  # 存在するダミーディレクトリ（ロードは起きない）
    with pytest.raises(ValueError, match="fr"):
        engine.translate("こんにちは", "fr")


def test_hymt_unsupported_lang_raises_before_model_load(tmp_path: Path):
    gguf = tmp_path / "dummy.gguf"
    gguf.write_bytes(b"")
    engine = HyMt2Engine(gguf)
    with pytest.raises(ValueError, match="fr"):
        engine.translate("こんにちは", "fr")


def test_missing_model_path_raises_with_guidance(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="download_models"):
        NllbEngine(tmp_path / "nai", tmp_path / "nai")
    with pytest.raises(FileNotFoundError, match="download_models"):
        HyMt2Engine(tmp_path / "nai.gguf")


def test_build_mt_engine_rejects_uncovered_language():
    # エンジンが対応しない言語が config にあると、モデルの有無以前に起動を拒否する
    # （E-14 の起動時版。言語検証はファイル検証より先）
    config = AppConfig(
        mt=MtConfig(engine="nllb"),
        languages=[Language(code="en", label="English"), Language(code="fr", label="Français")],
    )
    with pytest.raises(ValueError, match="fr"):
        build_mt_engine(config)


def test_build_mt_engine_fake_covers_any_config_language():
    config = AppConfig(
        mt=MtConfig(engine="fake"),
        languages=[Language(code="en", label="English"), Language(code="xx", label="Test")],
    )
    assert build_mt_engine(config) is not None


def test_build_mt_engine_detects_incomplete_model(tmp_path: Path):
    # E-13: ダウンロード中断等による不完全なモデルでは起動しない
    gguf = tmp_path / "hy-mt2" / "Hy-MT2-1.8B-Q4_K_M.gguf"
    gguf.parent.mkdir(parents=True)
    gguf.write_bytes(b"x" * 10)
    config = AppConfig(models=ModelsConfig(dir=str(tmp_path)), mt=MtConfig(engine="hy-mt2"))
    with pytest.raises(FileNotFoundError, match="不完全"):
        build_mt_engine(config)
