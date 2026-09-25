"""#46: 配布パッケージのビルドのうち、純粋な判定部分（DLL 依存の検査・同梱モデルの照合）。

ビルド全体の合否はビルド自身の自己検証で判定する（pytest には入れない）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.build_package import CODE_FILES, ROOT, BuildError, bundled_model_files, unknown_dependencies


def test_unknown_dependency_is_reported_per_module():
    imports = {
        "app/python/Lib/site-packages/llama_cpp/lib/llama.dll": [
            "KERNEL32.dll", "ggml.dll", "MSVCP140.dll", "cudart64_12.dll",
        ],
        "app/python/Lib/site-packages/llama_cpp/lib/ggml.dll": ["KERNEL32.dll", "libomp140.dll"],
    }

    unknown = unknown_dependencies(imports, bundled=["llama.dll", "ggml.dll"])

    assert unknown == {
        "app/python/Lib/site-packages/llama_cpp/lib/llama.dll": ["cudart64_12.dll"],
        "app/python/Lib/site-packages/llama_cpp/lib/ggml.dll": ["libomp140.dll"],
    }


def test_bundled_windows_and_vc_runtime_dependencies_pass():
    imports = {
        "a/_core.pyd": [
            "python312.dll", "Sherpa-Onnx-C-Api.DLL",  # 同梱物（大文字小文字は無視）
            "KERNEL32.dll", "ADVAPI32.dll", "api-ms-win-crt-runtime-l1-1-0.dll",  # Windows 標準
            "VCRUNTIME140.dll", "VCRUNTIME140_1.dll", "MSVCP140.dll", "VCOMP140.DLL",  # VC++
        ],
    }

    assert unknown_dependencies(imports, bundled=["python312.dll", "sherpa-onnx-c-api.dll"]) == {}


SPECS_FILES = {
    "reazonspeech-k2-v2": [
        "encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.onnx",
        "decoder-epoch-99-avg-1.int8.onnx", "joiner-epoch-99-avg-1.int8.onnx",
        "tokens.txt", "README.md",
    ],
    "hy-mt2": ["Hy-MT2-1.8B-Q4_K_M.gguf"],
    "nllb-200-distilled-600M-ct2": ["model.bin", "config.json", "shared_vocabulary.txt"],
    "nllb-tokenizer": [
        "sentencepiece.bpe.model", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json",
    ],
}


def populate(models_dir: Path, skip: str | None = None) -> None:
    for subdir, names in SPECS_FILES.items():
        if subdir == skip:
            continue
        for name in names:
            path = models_dir / subdir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        # Hugging Face のダウンロード作業領域は同梱しない
        cache = models_dir / subdir / ".cache" / "huggingface" / "lock"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"x")
    # 同梱対象外の whisper 系は運ばない
    (models_dir / "faster-whisper-small").mkdir()
    (models_dir / "faster-whisper-small" / "model.bin").write_bytes(b"x")


def test_bundled_models_are_the_default_set_and_nllb_without_download_cache(tmp_path):
    populate(tmp_path)

    files = bundled_model_files(tmp_path)

    relative = {path.relative_to(tmp_path).as_posix() for path in files}
    expected = {f"{subdir}/{name}" for subdir, names in SPECS_FILES.items() for name in names}
    assert relative == expected


@pytest.mark.parametrize(("missing", "spec_name"), [
    ("reazonspeech-k2-v2", "reazonspeech"),
    ("hy-mt2", "hy-mt2"),
    ("nllb-200-distilled-600M-ct2", "nllb"),
    ("nllb-tokenizer", "nllb-tokenizer"),
])
def test_missing_model_stops_the_build_with_the_download_command(tmp_path, missing, spec_name):
    populate(tmp_path, skip=missing)

    with pytest.raises(BuildError) as raised:
        bundled_model_files(tmp_path)

    message = str(raised.value)
    assert str(tmp_path / missing) in message
    assert f"scripts/download_models.py --only {spec_name}" in message


def test_partially_downloaded_model_names_the_missing_file(tmp_path):
    populate(tmp_path)
    (tmp_path / "reazonspeech-k2-v2" / "tokens.txt").unlink()

    with pytest.raises(BuildError, match="tokens.txt"):
        bundled_model_files(tmp_path)


def test_package_carries_every_script_the_launcher_dot_sources_or_starts():
    # run.ps1 → launcher.ps1、初回処理の first_run_admin.ps1 → os_setup.ps1（#47）
    for relative in ("scripts/run.ps1", "scripts/launcher.ps1", "scripts/first_run_admin.ps1",
                     "scripts/os_setup.ps1", "scripts/make_cert.py"):
        assert relative in CODE_FILES
        assert (ROOT / relative).is_file()
