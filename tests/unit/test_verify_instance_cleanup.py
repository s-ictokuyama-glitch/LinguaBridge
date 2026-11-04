import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents/skills/verify/scripts/instance.py"


@pytest.fixture
def run_dir():
    evidence = ROOT / "artifacts/verify"
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cleanup-test-", dir=evidence) as directory:
        yield Path(directory)


def sleeper():
    return subprocess.Popen(
        [sys._base_executable, "-c", "import time; time.sleep(60)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.parametrize("fault", ["children_denied", "child_disappears", "legacy_manifest"])
def test_descendant_inspection_does_not_strand_owned_process(monkeypatch, run_dir, fault):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "cleanup", "--run-dir", str(run_dir)])
    module = runpy.run_path(str(SCRIPT), run_name="__main__")
    stop = module["stop_owned"]
    process = sleeper()
    try:
        owned = psutil.Process(process.pid)
        state = {"pid": process.pid, "created": owned.create_time(), "command": owned.cmdline()}
        if fault != "legacy_manifest":
            state["launcher_command"] = state["command"]
        stop.__globals__["state"] = state

        class VanishedChild:
            pid = 12345

            def create_time(self):
                return 0

            def cmdline(self):
                raise psutil.NoSuchProcess(12345)

        if fault == "children_denied":
            def denied(*args, **kwargs):
                raise psutil.AccessDenied(process.pid)
            monkeypatch.setattr(psutil.Process, "children", denied)
        elif fault == "child_disappears":
            monkeypatch.setattr(psutil.Process, "children", lambda *args, **kwargs: [VanishedChild()])
        else:
            console = VanishedChild()
            console.cmdline = lambda: ["console.exe"]
            monkeypatch.setattr(psutil.Process, "children", lambda *args, **kwargs: [console])
        error = None
        try:
            stop()
        except (psutil.Error, KeyError) as exc:
            error = str(exc)
        assert process.poll() is not None, f"Owned process survived cleanup: {fault}: {error}"
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=15)


def test_failed_ownership_snapshot_stops_spawned_handle(monkeypatch, run_dir):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "launch", "--run-dir", str(run_dir)])
    original_popen = subprocess.Popen
    original_process = psutil.Process
    spawned = []

    def launch(*args, **kwargs):
        child = original_popen(
            [sys._base_executable, "-c", "import time; time.sleep(60)"], **kwargs
        )
        spawned.append(child)
        return child

    def denied_snapshot(pid=None):
        if spawned and pid == spawned[0].pid:
            raise psutil.AccessDenied(pid)
        return original_process(pid)

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(psutil, "Process", denied_snapshot)
    try:
        with pytest.raises(SystemExit, match="1"):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        assert spawned
        assert spawned[0].poll() is not None, "Spawned process survived a failed ownership snapshot"
        state = json.loads((run_dir / "instance.json").read_text(encoding="utf-8"))
        assert state["pid"] == spawned[0].pid
        failure = json.loads((run_dir / "failure.json").read_text(encoding="utf-8"))
        assert "error" in failure
    finally:
        for child in spawned:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=15)
