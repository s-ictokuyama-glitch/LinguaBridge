"""#46: 配布パッケージのビルドのうち、純粋な判定部分（DLL 依存の検査・同梱モデルの照合）。

ビルド全体の合否はビルド自身の自己検証で判定する（pytest には入れない）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.build_package import (
    CODE_FILES,
    ROOT,
    BuildError,
    copy_code,
    plan_bundle,
    copy_models,
    write_distribution_config,
    write_version,
    llama_cpu_problem,
    pinned_version,
    unknown_dependencies,
)

# requirements.txt の CPU 版インデックスから入る llama-cpp-python 0.3.33 の wheel（#52 で配布して落ちたもの）を
# 開発機（Ryzen 7 8840U）で読んだ値
PREBUILT_0_3_33_SYSTEM_INFO = (
    "CPU : SSE3 = 1 | SSSE3 = 1 | AVX = 1 | AVX2 = 1 | F16C = 1 | FMA = 1 | AVX512 = 1 | "
    "LLAMAFILE = 1 | OPENMP = 1 | REPACK = 1 | "
)
PREBUILT_0_3_33_LIB = [
    "ggml-base.dll", "ggml-base.lib", "ggml-cpu.dll", "ggml-cpu.lib", "ggml.dll", "ggml.lib",
    "llama.dll", "llama.lib", "mtmd.dll", "mtmd.lib",
]


def test_prebuilt_avx512_llama_cpp_stops_the_build():
    problem = llama_cpu_problem(PREBUILT_0_3_33_SYSTEM_INFO, PREBUILT_0_3_33_LIB)

    assert problem is not None
    assert "AVX-512" in problem
    assert "GGML_CPU_ALL_VARIANTS" in problem


def test_cpu_selecting_llama_cpp_passes_even_when_it_picked_avx512_here():
    lib = ["ggml-base.dll", "ggml.dll", "llama.dll", "mtmd.dll",
           "ggml-cpu-x64.dll", "ggml-cpu-haswell.dll", "ggml-cpu-zen4.dll"]

    assert llama_cpu_problem(PREBUILT_0_3_33_SYSTEM_INFO, lib) is None


def test_single_cpu_backend_without_avx512_passes():
    avx2_only = PREBUILT_0_3_33_SYSTEM_INFO.replace("AVX512 = 1 | ", "")

    assert llama_cpu_problem(avx2_only, PREBUILT_0_3_33_LIB) is None


def test_pinned_version_normalizes_the_distribution_name():
    frozen = "fastapi==0.115.0\nllama_cpp_python==0.3.33\nnumpy==2.1.0\n"

    assert pinned_version(frozen, "llama-cpp-python") == "0.3.33"
    with pytest.raises(BuildError, match="sherpa-onnx"):
        pinned_version(frozen, "sherpa-onnx")


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


def plan(tmp_path, **kwargs):
    return plan_bundle(ROOT / "config.yaml", models_dir=tmp_path, **kwargs)


def test_default_only_requires_active_models(tmp_path):
    populate(tmp_path, skip="nllb-200-distilled-600M-ct2")
    selected = plan(tmp_path)
    assert {model.spec.name for model in selected.models} == {"reazonspeech", "hy-mt2"}
    assert selected.total_bytes == 7
    assert all(".cache" not in str(file.relative) for model in selected.models for file in model.files)


def test_default_hotwords_ship_with_the_code(tmp_path):
    """既定の教科用語は server/ 配下にあり、copy_code で app へ運ばれる。"""
    populate(tmp_path)
    config = yaml.safe_load(plan(tmp_path).config_yaml)
    hotwords = config["asr"]["sherpa"]["hotwords_file"]
    assert hotwords.startswith("server/")
    copy_code(tmp_path / "app", tmp_path)
    assert (tmp_path / "app" / hotwords).is_file()


def test_include_nllb_adds_tokenizer(tmp_path):
    populate(tmp_path)
    selected = plan(tmp_path, include=("nllb",))
    assert {model.spec.name for model in selected.models} == {"reazonspeech", "hy-mt2", "nllb", "nllb-tokenizer"}
    with pytest.raises(BuildError, match="nllb.*依存"):
        plan(tmp_path, include=("nllb",), exclude=("nllb-tokenizer",))


@pytest.mark.parametrize(("missing", "spec_name"), [
    ("reazonspeech-k2-v2", "reazonspeech"), ("hy-mt2", "hy-mt2"),
    ("nllb-200-distilled-600M-ct2", "nllb"), ("nllb-tokenizer", "nllb-tokenizer"),
])
def test_missing_selected_model_reports_download(tmp_path, missing, spec_name):
    populate(tmp_path, skip=missing)
    with pytest.raises(BuildError, match=f"scripts/download_models.py --only {spec_name}"):
        plan(tmp_path, include=("nllb",))


def test_partially_downloaded_model_names_missing_file(tmp_path):
    populate(tmp_path)
    (tmp_path / "reazonspeech-k2-v2" / "tokens.txt").unlink()
    with pytest.raises(BuildError, match="tokens.txt"):
        plan(tmp_path)


@pytest.mark.parametrize("kwargs, message", [
    ({"exclude": ("hy-mt2",)}, "mt.hy_mt2.gguf_path"),
    ({"include": ("unknown",)}, "未知"),
    ({"include": ("nllb",), "exclude": ("nllb",)}, "include/exclude"),
])
def test_invalid_selection_fails_before_model_lookup(tmp_path, kwargs, message):
    with pytest.raises(BuildError, match=message):
        plan(tmp_path, **kwargs)


def test_config_snapshot_override_and_exact_gguf(tmp_path):
    import yaml
    populate(tmp_path)
    override = tmp_path / "override.yaml"
    override.write_text("vad: {pre_roll_ms: 480}\nasr: {engine: faster-whisper, model: kotoba-whisper-v2.0-faster}\n", encoding="utf-8")
    model = tmp_path / "kotoba-whisper-v2.0-faster" / "model.bin"
    model.parent.mkdir()
    model.write_bytes(b"kotoba")
    model.with_name("config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "hy-mt2" / "aaa-Q4_K_M.gguf").write_bytes(b"wrong")
    selected = plan(tmp_path, override=override)
    config = yaml.safe_load(selected.config_yaml)
    assert config["vad"]["pre_roll_ms"] == 480
    assert config["models"]["dir"] == "models/"
    assert {m.spec.name for m in selected.models} == {"kotoba", "hy-mt2"}
    assert next(m for m in selected.models if m.spec.name == "hy-mt2").files[0].source.name == "Hy-MT2-1.8B-Q4_K_M.gguf"


def test_nllb_active_selects_dependencies(tmp_path):
    populate(tmp_path, skip="hy-mt2")
    override = tmp_path / "override.yaml"
    override.write_text("mt: {engine: nllb}\n", encoding="utf-8")
    selected = plan(tmp_path, override=override)
    assert {m.spec.name for m in selected.models} == {"reazonspeech", "nllb", "nllb-tokenizer"}


@pytest.mark.parametrize("text", [
    "asr: {engine: fake}", "mt: {engine: fake}",
    "asr: {sherpa: {model: ../outside}}", "mt: {hy_mt2: {gguf_path: C:/outside.gguf}}",
    "asr: {sherpa: {hotwords_file: custom.txt}}",
    "asr: {sherpa: {hotwords_file: server/asr/hotwords/missing.txt}}",
])
def test_nonportable_config_is_rejected(tmp_path, text):
    override = tmp_path / "override.yaml"
    override.write_text(text, encoding="utf-8")
    with pytest.raises(BuildError):
        plan(tmp_path, override=override)


def test_explicit_missing_override_is_error(tmp_path):
    with pytest.raises(BuildError, match="設定ファイルがありません"):
        plan(tmp_path, override=tmp_path / "missing.yaml")


def test_config_manifest_version_and_payload_share_plan(tmp_path):
    import hashlib
    import json
    from datetime import datetime, timezone
    populate(tmp_path)
    selected = plan(tmp_path)
    app = tmp_path / "app"
    app.mkdir()
    copy_models(selected, app / "models")
    write_distribution_config(selected, app / "config.yaml")
    version = write_version(app, datetime.now(timezone.utc), "abc", False, selected)
    manifest = json.loads((app / "models" / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == set(version["models"]) == {m.spec.name for m in selected.models}
    assert version["model_bytes"] == sum(m["bytes"] for m in manifest.values()) == selected.total_bytes
    expected = {str(f.relative) for m in selected.models for f in m.files}
    assert expected == {str(p.relative_to(app / "models").as_posix()) for p in (app / "models").rglob("*") if p.is_file() and p.name != "manifest.json"}
    assert (app / "config.yaml").read_text(encoding="utf-8") == selected.config_yaml
    assert hashlib.sha256((app / "config.yaml").read_bytes()).hexdigest() == version["config_sha256"]


@pytest.mark.parametrize(("name", "subdir"), [
    ("kotoba", "kotoba-whisper-v2.0-faster"),
    ("whisper-small", "faster-whisper-small"),
    ("nllb", "nllb-200-distilled-600M-ct2"),
])
@pytest.mark.parametrize("missing", ["model.bin", "config.json"])
def test_incomplete_optional_snapshot_is_rejected(tmp_path, name, subdir, missing):
    populate(tmp_path)
    directory = tmp_path / subdir
    directory.mkdir(exist_ok=True)
    for filename in ("model.bin", "config.json"):
        (directory / filename).write_bytes(b"x")
    (directory / missing).unlink()
    with pytest.raises(BuildError, match=missing):
        plan(tmp_path, include=(name,))


def test_signature_check_uses_windows_powershell_modules(tmp_path, monkeypatch):
    import subprocess
    from scripts import build_package as package
    monkeypatch.setenv("PSModulePath", "PowerShell7-only-modules")
    monkeypatch.setenv("BUILD_TEST_KEEP", "preserved")

    def check_signature(command, **kwargs):
        assert not any(key.upper() == "PSMODULEPATH" for key in kwargs["env"])
        assert kwargs["env"]["BUILD_TEST_KEEP"] == "preserved"
        return subprocess.CompletedProcess(command, 0, "Valid|O=Python Software Foundation", "")

    monkeypatch.setattr(package.subprocess, "run", check_signature)
    package._require_signature(tmp_path / "python.exe", "Python Software Foundation")


def test_copy_rejects_changed_source_size(tmp_path):
    populate(tmp_path)
    selected = plan(tmp_path)
    selected.models[0].files[0].source.write_bytes(b"changed")
    with pytest.raises(BuildError, match="サイズが変わりました"):
        copy_models(selected, tmp_path / "out")


def test_plan_cli_has_no_build_network_or_writes(tmp_path, monkeypatch, capsys):
    import sys
    from scripts import build_package as package
    populate(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    def forbidden(*args, **kwargs):
        pytest.fail("plan attempted build/network/write")
    monkeypatch.setattr(package, "build", forbidden)
    monkeypatch.setattr(package.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(sys, "argv", ["build_package.py", "--models-dir", str(tmp_path), "--plan"])
    assert package.main() == 0
    assert "7 bytes" in capsys.readouterr().out
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}



def test_ambiguous_inactive_gguf_is_rejected(tmp_path):
    populate(tmp_path)
    (tmp_path / "hy-mt2" / "other-Q4_K_M.gguf").write_bytes(b"x")
    override = tmp_path / "override.yaml"
    override.write_text("mt: {engine: nllb}", encoding="utf-8")
    with pytest.raises(BuildError, match="候補が複数"):
        plan(tmp_path, override=override, include=("hy-mt2",))


def test_model_links_are_rejected(tmp_path, monkeypatch):
    populate(tmp_path)
    original = Path.is_symlink
    target = tmp_path / "reazonspeech-k2-v2" / "tokens.txt"
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == target or original(self))
    with pytest.raises(BuildError, match="リンク"):
        plan(tmp_path)


def test_copy_rejects_extra_files(tmp_path):
    populate(tmp_path)
    selected = plan(tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "unexpected.bin").write_bytes(b"x")
    with pytest.raises(BuildError, match="ファイル集合/サイズ"):
        copy_models(selected, dest)


def test_package_carries_every_script_the_launcher_dot_sources_or_starts():
    # run.ps1 → launcher.ps1、初回処理の first_run_admin.ps1 → os_setup.ps1（#47）
    for relative in ("scripts/run.ps1", "scripts/launcher.ps1", "scripts/first_run_admin.ps1",
                     "scripts/os_setup.ps1", "scripts/make_cert.py"):
        assert relative in CODE_FILES
        assert (ROOT / relative).is_file()
