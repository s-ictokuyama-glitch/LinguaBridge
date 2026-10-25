"""切断注入の帳簿（`scripts/replay_client.py`・#35）のユニットテスト。

固定するのは1点だけだが、それが判定の意味を決めている:

    **試験が自分で切ったぶんを「想定外の切断」に数えない。**

数えると「注入すればするほど不合格に近づく」という無意味な判定になる。
#32 の60分レポートは切断0で合格しており、「`disconnects` が 0 でなければ異常」
という読み方がそこに乗っているので、その意味を壊さないこと。

復元そのものの判定（どの seq が届いたか）は `scripts/acceptance.py` の
`restore_gaps` 側にあり、`tests/unit/test_acceptance.py` で固定している。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import scripts.performance_evidence as performance_evidence
import scripts.replay_client as replay
from scripts.performance_evidence import PerformanceEvidenceError
from scripts.replay_client import (
    Results,
    _count_disconnect,
    _finish_injected_restore,
    _timing_coverage,
    continuous_delivery_summary,
    inject_disconnects,
    injection_event_summary,
    sample_rss,
)


@pytest.fixture(autouse=True)
def qualified_producer_test_seam(monkeypatch):
    monkeypatch.setattr(
        performance_evidence, "_QUALIFIED_PRODUCER_BOOTSTRAP", True
    )
    monkeypatch.setattr(
        performance_evidence, "_PRELOADED_CRITICAL_MODULES", frozenset()
    )
    monkeypatch.setattr(
        performance_evidence.sys,
        "pycache_prefix",
        str(performance_evidence._IMPORT_PYCACHE_ROOT),
    )
    monkeypatch.setattr(performance_evidence.sys, "dont_write_bytecode", True)
    monkeypatch.setattr(replay, "_QUALIFIED_BOOTSTRAP", True)
    monkeypatch.setattr(
        replay, "_PRELOADED_REPLAY_CRITICAL_MODULES", frozenset()
    )


def test_replay_guard_rejects_a_preloaded_critical_module(monkeypatch) -> None:
    monkeypatch.setattr(
        replay,
        "_PRELOADED_REPLAY_CRITICAL_MODULES",
        frozenset({"websockets"}),
    )

    with pytest.raises(PerformanceEvidenceError, match="fresh -I -S"):
        replay._assert_producer_modules_current()


def test_replay_cli_isolation_ignores_a_script_directory_numpy_shadow(
    tmp_path: Path,
) -> None:
    isolated_root = tmp_path / "isolated-replay"
    scripts = isolated_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(replay.__file__, scripts / "replay_client.py")

    dependency = scripts / "numpy.py"
    stale = "raise RuntimeError('STALE')\n"
    fresh = "raise RuntimeError('FRESH')\n"
    assert len(stale) == len(fresh)
    timestamp = 1_700_000_000
    dependency.write_text(stale, encoding="utf-8")
    os.utime(dependency, (timestamp, timestamp))
    cache = dependency.parent / "__pycache__" / (
        f"{dependency.stem}.{sys.implementation.cache_tag}.pyc"
    )
    cache.parent.mkdir()
    py_compile.compile(str(dependency), cfile=str(cache), doraise=True)
    dependency.write_text(fresh, encoding="utf-8")
    os.utime(dependency, (timestamp, timestamp))

    environment = os.environ.copy()
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    result = subprocess.run(
        [sys.executable, str(scripts / "replay_client.py")],
        cwd=isolated_root,
        env=environment,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert b"RuntimeError: STALE" not in result.stderr
    assert b"RuntimeError: FRESH" not in result.stderr
    assert b"ModuleNotFoundError" in result.stderr
    assert b"No module named 'scripts." in result.stderr


def test_replay_producer_guard_accepts_a_fresh_isolated_interpreter(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "guard-pycache"
    cache.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PYTHON")
    }
    probe = """import os
import sys
site_packages, root = map(os.path.abspath, sys.argv[1:])
sys.path.extend((site_packages, root))
import scripts.replay_client as replay
replay._assert_producer_modules_current()
print("qualified")
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-X",
            f"pycache_prefix={cache}",
            "-c",
            probe,
            str(replay._replay_venv_site_packages()),
            str(replay.ROOT),
        ],
        cwd=replay.ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "qualified"


def test_replay_server_spawns_use_distinct_fresh_pycache_environments(
    tmp_path: Path, monkeypatch,
) -> None:
    child_root = tmp_path / "child-root"
    server = child_root / "server"
    server.mkdir(parents=True)
    (server / "__init__.py").write_text("", encoding="utf-8")
    (server / "main.py").write_text(
        """from pathlib import Path
import json
import os
import sys

prefix = Path(sys.pycache_prefix)
payload = {
    "prefix": str(prefix),
    "is_dir": prefix.is_dir() and not prefix.is_symlink(),
    "empty_before_import": list(prefix.iterdir()) == [],
    "dont_write": sys.dont_write_bytecode,
    "isolated": sys.flags.isolated,
    "no_site": sys.flags.no_site,
    "python_env": sorted(key for key in os.environ if key.startswith("PYTHON")),
}
import child_dependency
payload["value"] = child_dependency.VALUE
Path(os.environ["LINGUABRIDGE_CHILD_PROBE"]).write_text(
    json.dumps(payload), encoding="utf-8"
)
""",
        encoding="utf-8",
    )
    dependency = child_root / "child_dependency.py"
    stale = "VALUE = 'STALE'\n"
    fresh = "VALUE = 'FRESH'\n"
    timestamp = 1_700_000_000
    dependency.write_text(stale, encoding="utf-8")
    os.utime(dependency, (timestamp, timestamp))
    cache = dependency.parent / "__pycache__" / (
        f"{dependency.stem}.{sys.implementation.cache_tag}.pyc"
    )
    cache.parent.mkdir()
    py_compile.compile(str(dependency), cfile=str(cache), doraise=True)
    dependency.write_text(fresh, encoding="utf-8")
    os.utime(dependency, (timestamp, timestamp))

    inherited = tmp_path / "inherited-prefix"
    inherited.mkdir()
    (inherited / "untrusted").write_text("not empty", encoding="utf-8")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(inherited))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    config = Path(replay.ROOT) / "config.yaml"
    monkeypatch.setattr(replay, "ROOT", child_root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source_manifest = tmp_path / "source-manifest.json"
    spawn_fences: list[Path] = []
    monkeypatch.setattr(
        replay,
        "recheck_performance_run_state",
        lambda path: spawn_fences.append(path),
    )
    prefixes: list[Path] = []
    for index in range(2):
        output = tmp_path / f"child-{index}.json"
        monkeypatch.setenv("LINGUABRIDGE_CHILD_PROBE", str(output))
        with pytest.raises(RuntimeError, match="サーバーが起動前に終了"):
            replay.run_engine_test(
                "fake",
                SimpleNamespace(port=19080 + index),
                scratch,
                cfg=config,
                source_manifest=source_manifest,
            )
        payload = json.loads(output.read_text(encoding="utf-8"))
        prefix = Path(payload["prefix"])
        prefixes.append(prefix)
        assert prefix != inherited
        assert payload == {
            "prefix": str(prefix),
            "is_dir": True,
            "empty_before_import": True,
            "dont_write": True,
            "isolated": 1,
            "no_site": 1,
            "python_env": [],
            "value": "FRESH",
        }
        assert not prefix.exists()

    assert prefixes[0] != prefixes[1]
    assert spawn_fences == [source_manifest, source_manifest]


def test_injected_disconnect_is_not_counted_as_unexpected() -> None:
    results = Results()
    results.injected_pending.add("s3")

    _count_disconnect(results, "s3")

    assert results.disconnects == 0, "注入した切断が想定外の切断に混ざった"
    assert "s3" not in results.injected_pending, "1回の切断で1回だけ相殺されるべき"


def test_unexpected_disconnect_is_counted() -> None:
    results = Results()

    _count_disconnect(results, "s3")

    assert results.disconnects == 1


def test_only_the_injected_student_is_excused() -> None:
    """注入したのが s3 なら、同時に落ちた s4 は想定外として数える。"""
    results = Results()
    results.injected_pending.add("s3")

    _count_disconnect(results, "s4")
    _count_disconnect(results, "s3")

    assert results.disconnects == 1
    assert results.injected_pending == set()


def test_a_second_disconnect_after_injection_is_unexpected() -> None:
    """注入で1回切られた生徒が**もう一度**落ちたら、2回目は想定外。

    相殺を使い切らずに残すと、注入した生徒はその後どれだけ落ちても
    見逃されることになる。
    """
    results = Results()
    results.injected_pending.add("s3")

    _count_disconnect(results, "s3")  # 注入ぶん
    _count_disconnect(results, "s3")  # 本当に落ちた

    assert results.disconnects == 1


def test_restore_completes_only_after_every_joined_range_caption_arrives() -> None:
    results = Results()
    event = {
        "student": "s3",
        "close_succeeded": True,
        "joined": True,
        "expected_seqs": [2, 3],
        "restore_complete": False,
    }
    results.injection_events.append(event)
    results.pending_injections["s3"] = event
    results.student_seqs["s3"] = {1, 2}
    results.injected_at["s3"] = 10.0

    with patch("scripts.replay_client.time.monotonic", return_value=12.0):
        _finish_injected_restore(results, "s3")
        assert event["restore_complete"] is False
        results.student_seqs["s3"].add(3)
        _finish_injected_restore(results, "s3")

    assert event["restore_complete"] is True
    assert event["restore_seconds"] == 2.0
    assert results.restored_by_student == {"s3": 1}
    assert results.pending_injections == {}


def test_event_summary_keeps_an_old_missing_range_visible() -> None:
    results = Results()
    results.student_seqs["s3"] = {100}
    results.injection_events.append({
        "student": "s3",
        "close_succeeded": True,
        "joined": True,
        "expected_seqs": [2, 3],
        "restore_complete": False,
    })

    summary = injection_event_summary(results)

    assert summary[0]["missing_seqs"] == [2, 3]
    assert summary[0]["restore_complete"] is False


def test_continuous_delivery_keeps_old_non_target_loss_visible() -> None:
    results = Results()
    results.published_seqs = set(range(1, 101))
    results.student_seqs = {
        "target": set(range(1, 101)),
        "unaffected": set(range(2, 101)),
    }
    results.injection_events.append({
        "student": "target",
        "close_succeeded": True,
        "published_head_at_close": 100,
        "beyond_history_seqs": [],
    })

    summary = continuous_delivery_summary(results)

    assert summary["target"]["gaps"] == 0
    assert summary["unaffected"]["missing_seqs"] == [1]


def test_own_injection_does_not_excuse_a_gap_published_before_close() -> None:
    results = Results()
    results.published_seqs = set(range(1, 111))
    results.student_seqs = {"target": set(range(2, 101))}
    results.injection_events.append({
        "student": "target",
        "close_succeeded": True,
        "published_head_at_close": 100,
        # 1 is a pre-close loss; 101..110 were published while disconnected.
        "beyond_history_seqs": [1, *range(101, 111)],
    })

    summary = continuous_delivery_summary(results)

    assert summary["target"]["exempt_own_history"] == list(range(101, 111))
    assert summary["target"]["missing_seqs"] == [1]


def test_close_failure_is_not_counted_as_a_successful_injection() -> None:
    results = Results()
    results.student_langs["s3"] = "en"

    class FailingSocket:
        async def close(self, **_kwargs):
            results.stop.set()
            raise RuntimeError("close failed")

    results.live_ws["s3"] = FailingSocket()
    asyncio.run(inject_disconnects(results, 0))

    assert results.injected_disconnects == 0
    assert results.injected_by_student == {}
    assert results.injection_close_failures == 1
    assert len(results.injection_events) == 1
    assert results.injection_events[0]["close_succeeded"] is False


def test_delayed_end_caption_is_bucketed_by_turn_close_not_drain_receive_time() -> None:
    results = Results()
    results.audio_started_at = 100.0
    # Received at t=1810s, but delay=20s means the turn closed at t=1790s,
    # inside a 1800s measurement. It must remain in the last drift window.
    with patch("scripts.replay_client.time.monotonic", return_value=1910.0):
        results.record_caption({"seq": 1, "lang": "en", "delay_ms": 20_000})

    assert results.lat_samples == [(1790.0, 20_000)]


def test_timing_coverage_exposes_a_long_middle_or_tail_gap() -> None:
    coverage = _timing_coverage(
        [{"at_s": 0.5}, {"at_s": 2.5}, {"at_s": 4.5}, {"at_s": 120.0}],
        measurement_s=100.0,
    )

    assert coverage["measurement_samples"] == 3
    assert coverage["measurement_last_at_s"] == 4.5
    assert coverage["last_at_s"] == 120.0
    assert coverage["max_gap_s"] == 95.5


def test_resource_sampler_exception_is_reported_and_stops_the_run() -> None:
    results = Results()

    class ProcessStub:
        pid = 123

    with patch("scripts.replay_client.psutil.Process", side_effect=RuntimeError("sampler broke")):
        asyncio.run(sample_rss(ProcessStub(), results))

    assert results.stop.is_set()
    assert results.errors == ["resource sampler失敗: RuntimeError: sampler broke"]


def _stub_replay_main(monkeypatch, tmp_path, events: list[str]) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["replay_client.py", "--out-dir", str(tmp_path / "run"), "--no-config-update"],
    )
    monkeypatch.setattr(replay, "ensure_new_performance_run", lambda _path: None)
    monkeypatch.setattr(
        replay,
        "load_config",
        lambda _path: SimpleNamespace(mt=SimpleNamespace(engine="hy-mt2")),
    )
    monkeypatch.setattr(
        replay,
        "write_config",
        lambda _engine, _port, scratch, *_args, **_kwargs: scratch / "effective.yaml",
    )

    def begin(*_args, **_kwargs):
        events.append("begin")
        return tmp_path / "run" / "source-manifest.json"

    def complete(*_args, **_kwargs):
        events.append("complete")
        return {}

    monkeypatch.setattr(replay, "begin_performance_run", begin)
    monkeypatch.setattr(replay, "complete_performance_run", complete)
    monkeypatch.setattr(replay, "system_info", lambda: {"date": "2026-09-09"})
    monkeypatch.setattr(
        replay,
        "run_engine_test",
        lambda *_args, **_kwargs: {
            "engine": "hy-mt2",
            "verdict": {"passed": True, "reasons": []},
        },
    )
    monkeypatch.setattr(replay, "build_report", lambda *_args: "report")


def test_replay_main_checks_imported_code_before_begin_and_complete(
    tmp_path, monkeypatch
) -> None:
    events: list[str] = []
    _stub_replay_main(monkeypatch, tmp_path, events)
    monkeypatch.setattr(
        replay,
        "_assert_producer_modules_current",
        lambda: events.append("guard"),
    )

    assert replay.main() == 0

    assert events == ["guard", "begin", "guard", "complete"]


def test_replay_main_does_not_seal_when_imported_code_drifts_during_run(
    tmp_path, monkeypatch
) -> None:
    events: list[str] = []
    _stub_replay_main(monkeypatch, tmp_path, events)
    guard_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        events.append("guard")
        if guard_calls == 2:
            raise PerformanceEvidenceError("loaded code changed")

    monkeypatch.setattr(replay, "_assert_producer_modules_current", guard)

    with pytest.raises(PerformanceEvidenceError, match="loaded code changed"):
        replay.main()

    assert events == ["guard", "begin", "guard"]


def test_replay_producer_guard_rejects_stable_disk_b_after_imported_a(
    tmp_path, monkeypatch
) -> None:
    loaded = tmp_path / "loaded-replay-dependency.py"
    loaded.write_text("state A imported", encoding="utf-8")
    state = performance_evidence._capture_imported_module_state(
        loaded,
        description="simulated replay dependency",
    )
    monkeypatch.setattr(replay, "_PRODUCER_MODULE_STATE", (state,))
    loaded.write_text("state B stable on disk", encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match="changed"):
        replay._assert_producer_modules_current()
