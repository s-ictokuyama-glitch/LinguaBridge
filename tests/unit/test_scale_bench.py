"""スケーリングベンチ（`scripts/scale_bench.py`・#32）のユニットテスト。

このファイルが存在する理由は具体的で、**同じ失敗を二度と40分かけないため**。
`Results` のプロパティを1つ整理したとき、行列16点（1点2分）が全部
「計測は終わったが結果を組み立てる行で AttributeError」で落ちた。
サーバーを立てないと1点も走らない構造だったので、壊れていることに
40分かけて気づいた。結果の組み立てを純粋関数（`summarize`）に切り出し、
ここで固定する。

固定するのは3点:

    1. `summarize` は `Results` の実際の形で動く（属性を消したらここが赤くなる）
    2. `failed_point` は `summarize` と**同じキー**を返す
       （レポートの表は両方を同じ列に流し込むので、キーがずれると
         成功した点ではなく失敗した点の描画で落ちる）
    3. 実効言語数は `min(生徒数, 言語数)` — 生徒1名×5言語は実質1言語であり、
       この点を言語軸の主張に使ってはいけない
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

import numpy as np
import pytest

import scripts.performance_evidence as performance_evidence
import scripts.replay_client as replay
import scripts.scale_bench as scale
from scripts.performance_evidence import PerformanceEvidenceError
from scripts.replay_client import Results
from scripts.scale_bench import failed_point, summarize


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
    monkeypatch.setattr(scale, "_QUALIFIED_BOOTSTRAP", True)
    monkeypatch.setattr(
        scale, "_PRELOADED_SCALE_CRITICAL_MODULES", frozenset()
    )


def test_scale_guard_rejects_a_preloaded_critical_module(monkeypatch) -> None:
    monkeypatch.setattr(
        scale,
        "_PRELOADED_SCALE_CRITICAL_MODULES",
        frozenset({"scripts.replay_client"}),
    )

    with pytest.raises(PerformanceEvidenceError, match="fresh -I -S"):
        scale._assert_producer_modules_current()


def test_scale_cli_ignores_a_valid_stale_timestamp_pyc(tmp_path: Path) -> None:
    isolated_root = tmp_path / "isolated-scale"
    scripts = isolated_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(scale.__file__, scripts / "scale_bench.py")

    dependency = scripts / "performance_evidence.py"
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
        [sys.executable, str(scripts / "scale_bench.py")],
        cwd=isolated_root,
        env=environment,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert result.stderr.rstrip().endswith(b"RuntimeError: FRESH")


def test_scale_producer_guard_accepts_a_fresh_isolated_interpreter(
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
import scripts.scale_bench as scale
scale._assert_producer_modules_current()
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
            str(scale._scale_venv_site_packages()),
            str(scale.ROOT),
        ],
        cwd=scale.ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "qualified"


def test_scale_server_spawns_use_distinct_fresh_pycache_environments(
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
    config = Path(scale.ROOT) / "config.yaml"
    monkeypatch.setattr(scale, "ROOT", child_root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source_manifest = tmp_path / "source-manifest.json"
    spawn_fences: list[Path] = []
    monkeypatch.setattr(
        scale,
        "recheck_performance_run_state",
        lambda path: spawn_fences.append(path),
    )
    prefixes: list[Path] = []
    for index in range(2):
        output = tmp_path / f"child-{index}.json"
        monkeypatch.setenv("LINGUABRIDGE_CHILD_PROBE", str(output))
        with pytest.raises(RuntimeError, match="サーバーが起動前に終了"):
            scale.run_point(
                langs=["en"],
                students=1,
                minutes=0.1,
                corpus="ja",
                mt_engine="fake",
                port=19180 + index,
                scratch=scratch,
                drain_s=0,
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


def make_results(*, captions: dict | None = None, stats: dict | None = None) -> Results:
    results = Results()
    results.captions = captions if captions is not None else {(1, "en"): 500, (2, "en"): 1500}
    results.measurement_students = 10
    results.measurement_langs = {"en": 5, "zh": 5}
    results.measurement_seconds = 120.0
    results.ran_seconds = 140.0
    results.audio_bytes_planned = 3_840_000
    results.audio_bytes_sent = 3_840_000
    results.published_seqs = {1, 2}
    results.student_seqs = {f"s{index}": {1, 2} for index in range(10)}
    results.stats_samples = [
        {
            "at_s": 138.0,
            "students": 10,
            "langs": {"en": 5, "zh": 5},
            "queue_depth": 0,
            "audio_queue_seconds": 0.0,
            "mt_queue_depth": 0,
            "asr_calls": 7,
            "mt_calls": 5,
            "mt_cache_hits": 2,
            "tasks": 31,
            "audio_frames_rejected": 0,
            "asr_dropped_segments": 0,
            "asr_dropped_seconds": 0.0,
            **(stats or {}),
        }
    ]
    return results


def test_sleep_at_least_rechecks_an_early_windows_timer_wakeup(monkeypatch) -> None:
    moments = iter([100.0, 100.0, 119.985, 120.0])
    waits: list[float] = []

    async def early_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(replay, "_monotonic", lambda: next(moments))
    monkeypatch.setattr(replay.asyncio, "sleep", early_sleep)

    asyncio.run(replay.sleep_at_least(20.0))

    assert waits == pytest.approx([20.0, 0.015])


def test_teacher_pacing_guards_the_final_audio_chunk_against_early_wakeup(
    monkeypatch,
) -> None:
    waits: list[float] = []

    async def guarded_sleep(seconds: float) -> None:
        waits.append(seconds)

    class FakeSocket:
        async def send(self, _message) -> None:
            pass

        async def recv(self) -> str:
            return '{"type":"joined","seq_head":0,"history_from":1,"languages":[],"session_state":"idle"}'

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class Connection:
        async def __aenter__(self):
            return FakeSocket()

        async def __aexit__(self, *_args) -> None:
            pass

    monkeypatch.setattr(replay, "sleep_at_least", guarded_sleep)
    monkeypatch.setattr(
        replay,
        "websockets",
        SimpleNamespace(connect=lambda *_args, **_kwargs: Connection()),
    )
    pcm = np.zeros(replay.CHUNK_BYTES // 2, dtype=np.int16)

    asyncio.run(replay.run_teacher(18435, "4831", pcm, Results(), drain_s=2.0))

    assert len(waits) == 2
    assert waits[0] == pytest.approx(0.1, abs=0.01)
    assert waits[1] == 2.0


def test_summarize_reads_the_real_results_shape() -> None:
    """`Results` の属性が消えたらここが落ちる（サーバーを立てずに）。"""
    row = summarize(
        make_results(), langs=["en", "zh"], students=10, mt_engine="hy-mt2", asr_engine="sherpa"
    )
    assert row["asr_calls"] == 7
    assert row["mt_calls"] == 5
    assert row["dropped_segments"] == 0
    assert row["frames_rejected"] == 0
    assert row["captions"] == 2
    assert row["median_delay_s"] == 1.5  # sorted([500,1500])[1] / 1000
    assert row["max_delay_s"] == 1.5
    assert row["asr_engine"] == "sherpa"
    assert row["measurement_seconds"] == 120.0
    assert row["ran_seconds"] == 140.0
    assert row["counter_snapshot"]["at_s"] == 138.0
    assert row["counter_snapshot"]["queue_depth"] == 0


def test_summarize_survives_a_point_that_produced_nothing() -> None:
    """caption も stats も 0 件でも行は作れる（None として出る）。"""
    row = summarize(
        Results(), langs=["en"], students=1, mt_engine="fake", asr_engine=None
    )
    assert row["captions"] == 0
    assert row["median_delay_s"] is None
    assert row["asr_calls"] == 0
    assert row["dropped_segments"] == 0


def test_failed_point_has_the_same_keys_as_a_measured_point() -> None:
    """失敗した点も同じ列に流し込まれるので、キーがずれてはいけない。"""
    ok = summarize(
        make_results(), langs=["en", "zh"], students=10, mt_engine="hy-mt2", asr_engine="sherpa"
    )
    bad = failed_point(["en", "zh"], 10, "hy-mt2", "TimeoutError: /ready が来ない")
    assert set(ok) <= set(bad), f"失敗点に足りないキー: {set(ok) - set(bad)}"
    assert bad["failed"]


def test_active_langs_is_capped_by_the_student_count() -> None:
    """生徒1名 × 5言語は実効1言語。言語軸の主張にこの点は使えない。"""
    row = summarize(
        make_results(),
        langs=["en", "zh", "pt", "vi", "ko"],
        students=1,
        mt_engine="fake",
        asr_engine="sherpa",
    )
    assert row["lang_count"] == 5
    assert row["active_langs"] == 1

    many = summarize(
        make_results(),
        langs=["en", "zh", "pt", "vi", "ko"],
        students=40,
        mt_engine="fake",
        asr_engine="sherpa",
    )
    assert many["active_langs"] == 5


def _stub_scale_main(monkeypatch, tmp_path, events: list[str]) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scale_bench.py",
            "--students",
            "1",
            "--lang-counts",
            "1",
            "--out-dir",
            str(tmp_path / "run"),
        ],
    )
    monkeypatch.setattr(scale, "ensure_new_performance_run", lambda _path: None)
    monkeypatch.setattr(
        scale,
        "write_config",
        lambda _engine, _port, scratch, **_kwargs: scratch / "effective.yaml",
    )

    def begin(*_args, **_kwargs):
        events.append("begin")
        return tmp_path / "run" / "source-manifest.json"

    def complete(*_args, **_kwargs):
        events.append("complete")
        return {}

    monkeypatch.setattr(scale, "begin_performance_run", begin)
    monkeypatch.setattr(scale, "complete_performance_run", complete)
    monkeypatch.setattr(scale, "system_info", lambda: {"date": "2026-09-09"})
    monkeypatch.setattr(
        scale,
        "run_point",
        lambda **_kwargs: {
            "asr_calls": 1,
            "mt_calls": 1,
            "captions": 1,
            "median_delay_s": 0.1,
        },
    )
    monkeypatch.setattr(scale, "build_report", lambda *_args: "report")
    monkeypatch.setattr(scale.time, "sleep", lambda _seconds: None)


def test_scale_main_checks_imported_code_before_begin_and_complete(
    tmp_path, monkeypatch
) -> None:
    events: list[str] = []
    _stub_scale_main(monkeypatch, tmp_path, events)
    monkeypatch.setattr(
        scale,
        "_assert_producer_modules_current",
        lambda: events.append("guard"),
    )

    assert scale.main() == 0

    assert events == ["guard", "begin", "guard", "complete"]


def test_scale_main_does_not_seal_when_imported_code_drifts_during_run(
    tmp_path, monkeypatch
) -> None:
    events: list[str] = []
    _stub_scale_main(monkeypatch, tmp_path, events)
    guard_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        events.append("guard")
        if guard_calls == 2:
            raise PerformanceEvidenceError("loaded code changed")

    monkeypatch.setattr(scale, "_assert_producer_modules_current", guard)

    with pytest.raises(PerformanceEvidenceError, match="loaded code changed"):
        scale.main()

    assert events == ["guard", "begin", "guard"]


def test_scale_producer_guard_rejects_stable_disk_b_after_imported_a(
    tmp_path, monkeypatch
) -> None:
    loaded = tmp_path / "loaded-scale-dependency.py"
    loaded.write_text("state A imported", encoding="utf-8")
    state = performance_evidence._capture_imported_module_state(
        loaded,
        description="simulated scale dependency",
    )
    monkeypatch.setattr(scale, "_PRODUCER_MODULE_STATE", (state,))
    loaded.write_text("state B stable on disk", encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match="changed"):
        scale._assert_producer_modules_current()
