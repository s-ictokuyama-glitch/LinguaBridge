"""OPS-05 のモデル検証と JOIN-05 の再参加契約を補完する。"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from server import main
from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, AsrConfig, ModelsConfig, MtConfig
from server.model_files import require_model_files
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config


MODEL_TARGETS = (
    pytest.param("asr", "ASRモデル", "directory", id="asr"),
    pytest.param("nllb", "NLLBモデル", "directory", id="nllb"),
    pytest.param("tokenizer", "NLLBトークナイザ", "directory", id="tokenizer"),
    pytest.param("hy-mt2.gguf", "Hy-MT2のGGUF", "file", id="hy-mt2"),
)


@pytest.mark.parametrize("relative_path,what,path_kind", MODEL_TARGETS)
@pytest.mark.parametrize("condition", ("missing", "one-byte-below"))
def test_model_file_guard_rejects_missing_or_undersized_artifacts(
    tmp_path: Path,
    relative_path: str,
    what: str,
    path_kind: str,
    condition: str,
) -> None:
    path = tmp_path / relative_path
    # 境界ロジックは小さい既知値で検査し、実際の各下限値は factory 契約で固定する。
    min_bytes = 2
    if condition == "one-byte-below":
        if path_kind == "directory":
            path.mkdir()
            artifact = path / "artifact.bin"
        else:
            artifact = path
        artifact.write_bytes(b"x")

    with pytest.raises(FileNotFoundError) as caught:
        require_model_files(path, min_bytes, what)

    message = str(caught.value)
    assert what in message
    assert str(path) in message
    assert "scripts/download_models.py" in message
    assert "モデルを取得してください" in message
    if condition == "missing":
        assert "見つからない" in message
    else:
        assert "1 bytes < 期待下限 2" in message
        assert "ダウンロードが中断された可能性" in message


@pytest.mark.parametrize(
    "engine,module_name,class_name,relative_path",
    (
        pytest.param(
            "faster-whisper",
            "server.asr.fw_engine",
            "FasterWhisperEngine",
            "faster-whisper-small",
            id="faster-whisper",
        ),
        pytest.param(
            "sherpa",
            "server.asr.sherpa_engine",
            "SherpaOnnxEngine",
            "reazonspeech-k2-v2",
            id="sherpa",
        ),
    ),
)
def test_asr_factory_enforces_model_file_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    engine: str,
    module_name: str,
    class_name: str,
    relative_path: str,
) -> None:
    constructed = object()
    engine_module = importlib.import_module(module_name)
    monkeypatch.setattr(engine_module, class_name, lambda *_args, **_kwargs: constructed)
    guard_calls: list[tuple[Path, int, str]] = []
    monkeypatch.setattr(main, "require_model_files", lambda *args: guard_calls.append(args))
    config = AppConfig(
        models=ModelsConfig(dir=str(tmp_path)),
        asr=AsrConfig(engine=engine),
        mt=MtConfig(engine="fake"),
    )

    assert main.build_asr_engine(config) is constructed
    assert guard_calls == [(tmp_path / relative_path, 10_000_000, "ASRモデル")]


@pytest.mark.parametrize(
    "engine,module_name,class_name,expected_calls",
    (
        pytest.param(
            "nllb",
            "server.mt.nllb_engine",
            "NllbEngine",
            (
                ("nllb-200-distilled-600M-ct2", 100_000_000, "NLLBモデル"),
                ("nllb-tokenizer", 100_000, "NLLBトークナイザ"),
            ),
            id="nllb",
        ),
        pytest.param(
            "hy-mt2",
            "server.mt.hymt_engine",
            "HyMt2Engine",
            (("hy-mt2/Hy-MT2-1.8B-Q4_K_M.gguf", 500_000_000, "Hy-MT2のGGUF"),),
            id="hy-mt2",
        ),
    ),
)
def test_mt_factory_enforces_model_file_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    engine: str,
    module_name: str,
    class_name: str,
    expected_calls: tuple[tuple[str, int, str], ...],
) -> None:
    constructed = object()
    engine_module = importlib.import_module(module_name)
    monkeypatch.setattr(engine_module, class_name, lambda *_args, **_kwargs: constructed)
    guard_calls: list[tuple[Path, int, str]] = []
    monkeypatch.setattr(main, "require_model_files", lambda *args: guard_calls.append(args))
    config = AppConfig(
        models=ModelsConfig(dir=str(tmp_path)),
        asr=AsrConfig(engine="fake"),
        mt=MtConfig(engine=engine),
    )

    assert main.build_mt_engine(config) is constructed
    assert guard_calls == [
        (tmp_path / relative_path, min_bytes, what)
        for relative_path, min_bytes, what in expected_calls
    ]


@pytest.mark.parametrize(
    "invalid_join",
    (
        pytest.param(
            {"type": "join", "role": "student", "code": JOIN_CODE},
            id="missing-lang",
        ),
        pytest.param(
            {"type": "join", "role": "student", "code": JOIN_CODE, "lang": "fr"},
            id="unsupported-lang",
        ),
    ),
)
def test_student_can_retry_supported_language_on_same_websocket_after_bad_lang(
    invalid_join: dict[str, str],
) -> None:
    app = main.create_app(
        make_ws_test_config(),
        asr_engine=FakeASREngine(),
        mt_engine=FakeTranslationEngine(["en", "zh"]),
        join_code=JOIN_CODE,
    )

    with TestClient(app) as client, client.websocket_connect("/ws") as student:
        student.send_json(invalid_join)
        assert student.receive_json() == {"type": "join_rejected", "reason": "bad_lang"}

        student.send_json(
            {"type": "join", "role": "student", "code": JOIN_CODE, "lang": "en"}
        )
        joined = student.receive_json()

    assert joined["type"] == "joined"
    assert joined["session_state"] == "idle"
    assert [language["code"] for language in joined["languages"]] == ["en", "zh"]
