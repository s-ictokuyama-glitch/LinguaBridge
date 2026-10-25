"""Verify launches freeze the selected profile before isolating writable paths."""

import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

import pytest

from server.config import load_config

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents/skills/verify/scripts/instance.py"


class LaunchCaptured(BaseException):
    pass


@pytest.fixture
def run_dir():
    evidence = ROOT / "artifacts/verify"
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="config-test-", dir=evidence) as directory:
        yield Path(directory)


@pytest.mark.parametrize("with_profile", [False, True])
def test_launch_freezes_selected_config_and_isolates_outputs(monkeypatch, run_dir, with_profile):
    profile = run_dir / "profile.json"
    profile.write_text(json.dumps({
        "asr": {"engine": "faster-whisper", "model": "kotoba-whisper-v2.0-faster"},
        "server": {"http_port": 12345, "cert_dir": str(ROOT / "certs")},
        "recording": {"default_on": True, "out_dir": str(ROOT / "sessions")},
    }), encoding="utf-8")
    models = run_dir / "models"
    command = [str(SCRIPT), "launch", "--run-dir", str(run_dir), "--models-dir", str(models)]
    if with_profile:
        command.extend(["--config-override", str(profile)])
    monkeypatch.setattr(sys, "argv", command)

    def capture_launch(*args, **kwargs):
        raise LaunchCaptured

    monkeypatch.setattr(subprocess, "Popen", capture_launch)
    with pytest.raises(LaunchCaptured):
        runpy.run_path(str(SCRIPT), run_name="__main__")

    effective = json.loads((run_dir / "effective-config.json").read_text(encoding="utf-8"))
    expected = load_config(ROOT / "config.yaml", override=profile if with_profile else None)
    assert effective["asr"] == expected.asr.model_dump()
    assert effective["server"]["http_port"] != 12345
    assert effective["server"]["cert_dir"] == str(run_dir / "tmp/data/certs")
    assert effective["recording"] == {"default_on": False, "out_dir": str(run_dir / "tmp/data/sessions")}
    assert effective["models"]["dir"] == str(models)
    frozen = load_config(ROOT / "config.yaml", override=run_dir / "override.json")
    assert frozen.asr == expected.asr


def test_explicit_missing_profile_fails_before_launch(monkeypatch, run_dir):
    monkeypatch.setattr(sys, "argv", [
        str(SCRIPT), "launch", "--run-dir", str(run_dir),
        "--config-override", str(run_dir / "missing.yaml"),
    ])
    with pytest.raises(SystemExit, match="1"):
        runpy.run_path(str(SCRIPT), run_name="__main__")
    failure = json.loads((run_dir / "failure.json").read_text(encoding="utf-8"))
    assert "Config override does not exist" in failure["error"]
    assert not (run_dir / "tmp").exists()
