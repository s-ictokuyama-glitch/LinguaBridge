from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from scripts import baseline, tune_hymt, tune_turn


def write_report(directory: Path, marker: str, filename="2020-01-01-acceptance.json"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(
        json.dumps({"results": [{"marker": marker}]}), encoding="utf-8"
    )


@pytest.fixture(params=[baseline, tune_hymt, tune_turn], ids=lambda module: module.__name__)
def replay(request, tmp_path, monkeypatch):
    module = request.param
    root = tmp_path / "repo"
    root.mkdir()
    config = root / "config.yaml"
    original = "temperature: 0.0\ncache_size: 512\nstrategy: morph\n"
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", root)
    work = tmp_path / "reports"
    write_report(work, "stale")
    for name in ("before", "after"):
        for index in range(1, 4):
            write_report(work / f"{name}-{index}", "stale")

    def invoke(rounds=1):
        if module is baseline:
            return module.run_e2e(0.1, work)
        if module is tune_hymt:
            return module.run_e2e_ab(rounds, work)
        return module.run_e2e_ab(rounds, work, "morph")

    yield module, invoke, config, original
    assert config.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("returncode", [0, 7])
def test_replay_without_new_report_rejects_old_reports(replay, monkeypatch, returncode):
    _, invoke, _, _ = replay
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, returncode)
    )
    assert invoke() is None


def test_failed_replay_rejects_even_new_report(replay, monkeypatch):
    _, invoke, _, _ = replay

    def fail(command, **kwargs):
        out = Path(command[command.index("--out-dir") + 1])
        write_report(out, "incomplete", "2030-01-01-acceptance.json")
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(subprocess, "run", fail)
    assert invoke() is None


def test_repeated_replays_use_only_current_successful_results(replay, monkeypatch):
    module, invoke, config, original = replay
    destinations = []

    def succeed(command, **kwargs):
        out = Path(command[command.index("--out-dir") + 1])
        assert out.is_dir()
        assert not list(out.iterdir())
        assert out not in destinations
        destinations.append(out)
        write_report(out, str(len(destinations)), "2030-01-01-acceptance.json")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", succeed)
    first = invoke(rounds=2)
    assert config.read_text(encoding="utf-8") == original
    second = invoke(rounds=1)
    if module is baseline:
        assert first == {"marker": "1"}
        assert second == {"marker": "2"}
    elif module is tune_hymt:
        assert first == {
            "before": [{"marker": "1"}, {"marker": "3"}],
            "after": [{"marker": "2"}, {"marker": "4"}],
        }
        assert second == {"before": [{"marker": "5"}], "after": [{"marker": "6"}]}
    else:
        assert first == [
            {"label": "simple（#27 以前）", "runs": [{"marker": "1"}, {"marker": "3"}]},
            {"label": "morph", "runs": [{"marker": "2"}, {"marker": "4"}]},
        ]
        assert second == [
            {"label": "simple（#27 以前）", "runs": [{"marker": "5"}]},
            {"label": "morph", "runs": [{"marker": "6"}]},
        ]


def test_replay_exception_restores_config(replay, monkeypatch):
    _, invoke, _, _ = replay

    def fail(command, **kwargs):
        raise OSError("cannot start replay")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(OSError, match="cannot start replay"):
        invoke()


@pytest.mark.parametrize("replay", [tune_hymt, tune_turn], indirect=True, ids=["hymt", "turn"])
@pytest.mark.parametrize("failure", ["nonzero", "missing", "empty"])
def test_incomplete_ab_round_does_not_return_partial_comparison(replay, monkeypatch, failure):
    _, invoke, _, _ = replay
    calls = 0

    def run(command, **kwargs):
        nonlocal calls
        calls += 1
        out = Path(command[command.index("--out-dir") + 1])
        if calls == 3:
            if failure == "nonzero":
                write_report(out, "incomplete", "2030-01-01-acceptance.json")
                return subprocess.CompletedProcess(command, 7)
            if failure == "empty":
                (out / "2030-01-01-acceptance.json").write_text(
                    '{"results": []}', encoding="utf-8"
                )
            return subprocess.CompletedProcess(command, 0)
        write_report(out, str(calls), "2030-01-01-acceptance.json")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert invoke(rounds=2) is None


def test_turn_archive_reader_keeps_existing_directory_support(tmp_path):
    write_report(tmp_path / "before-1", "before")
    write_report(tmp_path / "after-1", "one")
    write_report(tmp_path / "after-2", "two")
    assert tune_turn.load_e2e_runs(tmp_path, "after") == [{"marker": "one"}, {"marker": "two"}]
