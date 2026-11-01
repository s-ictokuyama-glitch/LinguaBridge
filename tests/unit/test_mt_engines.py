"""翻訳エンジンのモデル非依存部分（言語マッピング・プロンプト・エラー伝播）のユニットテスト。

実モデルでの翻訳品質・疎通は tests/integration/test_real_mt.py。
"""

from __future__ import annotations

from pathlib import Path
import asyncio
import logging
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from server.config import AppConfig, Language, ModelsConfig, MtConfig
from server.main import build_asr_engine, build_mt_engine
from server.mt.hymt_engine import HYMT_LANG_LABELS, HyMt2Engine, build_prompt
from server.mt.nllb_engine import NLLB_LANG_CODES, NllbEngine
from server.model_files import TranslationModelError


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
    with pytest.raises(FileNotFoundError, match="配布パッケージを展開し直し"):
        NllbEngine(tmp_path / "nai", tmp_path / "nai")
    with pytest.raises(FileNotFoundError, match="配布パッケージを展開し直し"):
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


def assert_private_output(text: str):
    for name in ("hy-mt", "hymt", "nllb", "gguf", "tencent", "ct2", "nai"):
        assert name not in text.lower()


@pytest.mark.parametrize("engine_name", ["hy-mt2", "nllb"])
@pytest.mark.parametrize("incomplete", [False, True])
def test_startup_model_file_errors_are_private(tmp_path, engine_name, incomplete):
    models = tmp_path / "models"
    config = AppConfig(models=ModelsConfig(dir=str(models)), mt=MtConfig(engine=engine_name))
    if incomplete:
        path = models / (config.mt.hy_mt2.gguf_path if engine_name == "hy-mt2"
                         else config.mt.nllb.model_dir + "/model.bin")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"stub")
    with pytest.raises(FileNotFoundError) as error:
        build_mt_engine(config)
    message = str(error.value)
    assert_private_output(message)
    assert str(models) in message
    assert "配布パッケージを展開し直してください" in message
    assert ("不完全" if incomplete else "見つからない") in message


@pytest.mark.parametrize("engine_name", ["hy-mt2", "nllb", "nllb-secret-engine"])
def test_startup_configuration_errors_are_private(engine_name):
    config = AppConfig(mt=MtConfig(engine=engine_name), languages=[Language(code="fr", label="French")])
    with pytest.raises(ValueError) as error:
        build_mt_engine(config)
    assert_private_output(str(error.value))
    assert "翻訳" in str(error.value)


@pytest.mark.parametrize("engine_class", [HyMt2Engine, NllbEngine])
def test_direct_engine_errors_are_private(tmp_path, engine_class):
    path = tmp_path / "nai.gguf"
    with pytest.raises(FileNotFoundError) as error:
        engine_class(path) if engine_class is HyMt2Engine else engine_class(path, path)
    assert_private_output(str(error.value))
    assert "models" in str(error.value)
    path.write_bytes(b"stub")
    engine = engine_class(path) if engine_class is HyMt2Engine else engine_class(path, path)
    with pytest.raises(ValueError) as error:
        engine.translate("こんにちは", "fr")
    assert_private_output(str(error.value))
    assert "fr" in str(error.value)


def stub_translation_libraries(monkeypatch, *, metadata=None):
    llm = Mock()
    llm.metadata = {"tokenizer.chat_template": "template"} if metadata is None else metadata
    llm.tokenize.return_value = [1]
    llm.create_chat_completion.return_value = {"choices": [{"message": {"content": "Hello."}}]}
    llama = Mock(return_value=llm)
    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=llama))
    monkeypatch.setattr("server.mt.hymt_engine.load_cpu_backends", lambda: None)
    translator = Mock()
    translator.translate_batch.return_value = [SimpleNamespace(hypotheses=[["eng_Latn", "hello"]])]
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(Translator=Mock(return_value=translator)))
    tokenizer = Mock()
    tokenizer.return_value.input_ids = [1]
    tokenizer.decode.return_value = "Hello."
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=Mock(return_value=tokenizer)),
    ))
    return llama


@pytest.mark.parametrize("engine_name", ["hy-mt2", "nllb"])
def test_warmup_logs_use_the_same_private_names(tmp_path, monkeypatch, caplog, engine_name):
    llama = stub_translation_libraries(monkeypatch)
    path = tmp_path / "Hy-MT2-1.8B-Q4_K_M.gguf"
    path.write_bytes(b"stub")
    engine = HyMt2Engine(path) if engine_name == "hy-mt2" else NllbEngine(tmp_path, tmp_path)
    version = engine.model_version
    with caplog.at_level(logging.INFO):
        engine.warmup()
        engine.warmup()
    records = caplog.records
    assert [record.getMessage() for record in records] == ["翻訳モデルを読み込み中", "翻訳モデルの準備完了"]
    assert {record.name for record in records} == {"server.mt"}
    formatter = logging.Formatter("%(levelname)s:%(name)s:%(message)s")
    assert_private_output("\n".join(formatter.format(record) for record in records))
    assert engine.model_version == version
    assert engine.translate("こんにちは", "en") == "Hello."
    if engine_name == "hy-mt2":
        assert llama.call_args.kwargs["verbose"] is False


def test_invalid_translation_format_is_private(tmp_path, monkeypatch):
    stub_translation_libraries(monkeypatch, metadata={})
    path = tmp_path / "Hy-MT2-1.8B-Q4_K_M.gguf"
    path.write_bytes(b"stub")
    with pytest.raises(ValueError) as error:
        HyMt2Engine(path).warmup()
    assert_private_output(str(error.value))
    assert "形式が不正" in str(error.value)
    assert "配布パッケージを展開し直してください" in str(error.value)


@pytest.mark.parametrize("incomplete", [False, True])
def test_asr_model_errors_keep_the_full_path_and_guidance(tmp_path, incomplete):
    config = AppConfig(models=ModelsConfig(dir=str(tmp_path)))
    path = config.models.resolve(config.asr.model)
    if incomplete:
        path.mkdir()
        (path / "model.bin").write_bytes(b"stub")
    with pytest.raises(FileNotFoundError) as error:
        build_asr_engine(config)
    assert str(path) in str(error.value)
    assert "ASRモデル" in str(error.value)
    assert "scripts/download_models.py" in str(error.value)


@pytest.mark.parametrize("expected", [True, False])
def test_warmup_only_hides_tracebacks_for_expected_translation_errors(app, mt_engine, caplog, expected):
    error = TranslationModelError("翻訳モデルの形式が不正です") if expected else RuntimeError("unexpected failure")
    mt_engine.warmup = Mock(side_effect=error)

    async def scenario():
        pipeline = app.state.pipeline
        try:
            await pipeline._run_warmup()
            assert pipeline.ready is False
        finally:
            await pipeline.stop()

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert str(error) in caplog.text
    assert bool(caplog.records[-1].exc_info) is (not expected)
