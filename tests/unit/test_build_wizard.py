from pathlib import Path
import sys

import pytest
import yaml

from scripts import build_package as package


def populate_model(root, spec):
    names = spec.allow_patterns or ("model.bin", "config.json")
    if spec.kind == "gguf":
        names = ("Hy-MT2-1.8B-Q4_K_M.gguf",)
    for name in names:
        path = root / spec.subdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")


@pytest.fixture
def models(tmp_path):
    root = tmp_path / "モデル"
    for spec in package.SPECS:
        populate_model(root, spec)
    return root


@pytest.fixture
def built(monkeypatch):
    calls = []

    def build(work_dir, out_dir, plan):
        calls.append((work_dir, out_dir, plan))
        result = out_dir / "bundle.zip"
        result.write_bytes(b"test bundle")
        return result

    monkeypatch.setattr(package, "build", build)
    return calls


def run_wizard(monkeypatch, tmp_path, models, answers, *flags):
    inputs = iter(answers)

    def answer(prompt):
        try:
            value = next(inputs)
        except StopIteration:
            raise EOFError from None
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(sys, "argv", ["build_package.py", "--interactive",
                        "--models-dir", str(models), "--work-dir", str(tmp_path / "work"),
                        "--out-dir", str(tmp_path / "出力 フォルダー"), *flags])
    return package.main()


def names(plan):
    return {model.spec.name for model in plan.models}


@pytest.mark.parametrize(("asr", "mt", "expected", "engines"), [
    ("", "", {"reazonspeech", "hy-mt2"}, ("sherpa", "hy-mt2")),
    ("1", "1", {"reazonspeech", "hy-mt2"}, ("sherpa", "hy-mt2")),
    ("2", "1", {"kotoba", "hy-mt2"}, ("faster-whisper", "hy-mt2")),
    ("3", "2", {"whisper-small", "nllb", "nllb-tokenizer"}, ("faster-whisper", "nllb")),
])
def test_selected_engines_match_payload(monkeypatch, tmp_path, models, built, asr, mt, expected, engines):
    assert run_wizard(monkeypatch, tmp_path, models, [asr, mt, "", "", "y"]) == 0
    plan = built[0][2]
    config = yaml.safe_load(plan.config_yaml)
    assert names(plan) == expected
    assert (config["asr"]["engine"], config["mt"]["engine"]) == engines
    assert config["models"]["dir"] == "models/"
    if asr == "2":
        profile = package._load_build_config(package.ROOT / "config.yaml", package.ROOT / "config.accuracy.yaml")
        assert config["vad"]["pre_roll_ms"] == profile.vad.pre_roll_ms


def test_defaults_preserve_custom_config_and_exact_gguf(monkeypatch, tmp_path, models, built):
    override = tmp_path / "custom.yaml"
    override.write_text("vad: {pre_roll_ms: 361}\nmt: {hy_mt2: {gguf_path: hy-mt2/custom.gguf, threads: 3}}\n", encoding="utf-8")
    (models / "hy-mt2" / "custom.gguf").write_bytes(b"custom")
    before = override.read_bytes()
    assert run_wizard(monkeypatch, tmp_path, models, ["", "", "", "", "y"],
                      "--config-override", str(override)) == 0
    plan = built[0][2]
    config = yaml.safe_load(plan.config_yaml)
    assert config["vad"]["pre_roll_ms"] == 361
    assert config["mt"]["hy_mt2"]["threads"] == 3
    assert next(m for m in plan.models if m.spec.name == "hy-mt2").files[0].source.name == "custom.gguf"
    assert override.read_bytes() == before


def test_invalid_choices_retry_and_extras_keep_active_engines(monkeypatch, tmp_path, models, built, capsys):
    assert run_wizard(monkeypatch, tmp_path, models,
                      ["99", "bad", "", "1,2", "", "0", "1,99", "1,3,3", "", "maybe", "y"]) == 0
    plan = built[0][2]
    config = yaml.safe_load(plan.config_yaml)
    assert names(plan) == {"reazonspeech", "hy-mt2", "kotoba", "nllb", "nllb-tokenizer"}
    assert (config["asr"]["engine"], config["mt"]["engine"]) == ("sherpa", "hy-mt2")
    output = capsys.readouterr().out
    assert "#53" in output and "CC-BY-NC-4.0" in output
    assert "reazonspeech（起動設定で同梱必須）" in output
    assert "依存 nllb-tokenizer は自動追加" in output
    assert f"{plan.total_bytes} bytes" in output
    assert "ZIP全体の容量ではありません" in output


def test_unicode_output_and_two_runs_use_distinct_directories(monkeypatch, tmp_path, models, built):
    out = tmp_path / "選択した 出力"
    for _ in range(2):
        assert run_wizard(monkeypatch, tmp_path, models, ["", "", "", str(out), "y"]) == 0
    assert built[0][1].parent == out == built[1][1].parent
    assert built[0][1] != built[1][1]
    assert all((call[1] / "bundle.zip").read_bytes() == b"test bundle" for call in built)


@pytest.mark.parametrize("answers", [["q"], [""], [KeyboardInterrupt()],
                                    ["", "", "q"], ["", "", "", "q"],
                                    ["", "", "", "", "n"], ["", "", "", "", ""]])
def test_cancellation_never_builds(monkeypatch, tmp_path, models, built, capsys, answers):
    assert run_wizard(monkeypatch, tmp_path, models, answers) == 0
    assert built == []
    assert not (tmp_path / "出力 フォルダー").exists()
    output = capsys.readouterr().out
    assert "中止しました" in output and "完了:" not in output


@pytest.mark.parametrize("missing", [False, True])
def test_preview_never_downloads_or_writes(monkeypatch, tmp_path, models, built, missing):
    if missing:
        (models / "hy-mt2" / "Hy-MT2-1.8B-Q4_K_M.gguf").unlink()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("preview attempted network or writes")

    monkeypatch.setattr(package, "download_one", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    assert run_wizard(monkeypatch, tmp_path, models, ["2", "", "3", ""], "--plan") == int(missing)
    assert built == []
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_download_only_missing_selected_dependencies_then_replan(monkeypatch, tmp_path, models, built):
    for name in ("nllb", "nllb-tokenizer"):
        for path in (models / package.SPECS_BY_NAME[name].subdir).iterdir():
            path.unlink()
    downloads = []

    def download(spec, root):
        downloads.append(spec.name)
        populate_model(root, spec)

    monkeypatch.setattr(package, "download_one", download)
    assert run_wizard(monkeypatch, tmp_path, models, ["", "", "3", "", "y", "y"]) == 0
    assert set(downloads) == {"nllb", "nllb-tokenizer"}
    assert len(downloads) == 2
    assert names(built[0][2]) == {"reazonspeech", "hy-mt2", "nllb", "nllb-tokenizer"}
    assert not (models / "manifest.json").exists()


@pytest.mark.parametrize("answer", ["n", "q", "", EOFError(), KeyboardInterrupt()])
def test_declining_download_does_not_download_or_build(monkeypatch, tmp_path, built, answer):
    def forbidden(*args):
        pytest.fail("download without consent")

    monkeypatch.setattr(package, "download_one", forbidden)
    assert run_wizard(monkeypatch, tmp_path, tmp_path / "missing", ["", "", "", "", answer]) == 0
    assert built == []


def test_download_failure_returns_nonzero(monkeypatch, tmp_path, built, capsys):
    def fail(*args):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(package, "download_one", fail)
    assert run_wizard(monkeypatch, tmp_path, tmp_path / "missing", ["", "", "", "", "y"]) == 1
    assert built == []
    assert "network unavailable" in capsys.readouterr().err


def test_download_must_supply_exact_configured_gguf(monkeypatch, tmp_path, models, built, capsys):
    override = tmp_path / "custom.yaml"
    override.write_text("mt: {hy_mt2: {gguf_path: hy-mt2/missing-custom.gguf}}", encoding="utf-8")
    downloads = []

    def download(spec, root):
        downloads.append(spec.name)
        populate_model(root, spec)

    monkeypatch.setattr(package, "download_one", download)
    assert run_wizard(monkeypatch, tmp_path, models, ["", "", "", "", "y"],
                      "--config-override", str(override)) == 1
    assert downloads == ["hy-mt2"]
    assert built == []
    assert "missing-custom.gguf" in capsys.readouterr().err


@pytest.mark.parametrize("danger", ["file", "root"])
def test_links_rejected_before_missing_model_download(monkeypatch, tmp_path, models, built, danger):
    (models / "hy-mt2" / "Hy-MT2-1.8B-Q4_K_M.gguf").unlink()
    target = models if danger == "root" else models / "reazonspeech-k2-v2" / "tokens.txt"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or original(path))

    def forbidden(*args):
        pytest.fail("unsafe models triggered download")

    monkeypatch.setattr(package, "download_one", forbidden)
    assert run_wizard(monkeypatch, tmp_path, models, ["", "", "", "", "y"]) == 1
    assert built == []


def test_required_exclusion_stops_wizard(monkeypatch, tmp_path, models, built):
    assert run_wizard(monkeypatch, tmp_path, models, ["", "", "", "", "y"],
                      "--exclude-model", "hy-mt2") == 1
    assert built == []


def test_build_failure_is_not_success(monkeypatch, tmp_path, models, capsys):
    def fail(*args):
        raise package.BuildError("native check failed")

    monkeypatch.setattr(package, "build", fail)
    assert run_wizard(monkeypatch, tmp_path, models, ["", "", "", "", "y"]) == 1
    output = capsys.readouterr()
    assert "native check failed" in output.err
    assert "完了:" not in output.out


def test_work_lock_rejects_second_owner_and_releases(tmp_path):
    with package._interactive_work_lock(tmp_path / "work"):
        with pytest.raises(package.BuildError, match="実行中"):
            with package._interactive_work_lock(tmp_path / "work"):
                pytest.fail("second lock owner")
    with package._interactive_work_lock(tmp_path / "work"):
        pass


def test_inspection_does_not_mutate_validated_config(models):
    config = package._load_build_config(package.ROOT / "config.yaml", None)
    before = config.model_dump()
    package._inspect_bundle(config, models_dir=models)
    assert config.model_dump() == before
