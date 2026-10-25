"""Field-performance validation binds a sealed run to its measuring PC."""

from __future__ import annotations

import ast
import ctypes
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts.performance_evidence import performance_source_files
import scripts.validate_field_performance as field_validation


AUDIO_SHA256 = "a" * 64
GIB = 1024**3
VISIBLE_16_GIB_PC_BYTES = 16_398_209_024


def _valid_system(**overrides: object) -> dict[str, object]:
    system: dict[str, object] = {
        "cpu": "Intel(R) Core(TM) i5-8250U",
        "cores": 4,
        "threads": 8,
        "ram_gb": round(VISIBLE_16_GIB_PC_BYTES / 1e9, 1),
        "os": "Windows 11",
        "python": "3.12.10",
        "date": "2026-09-08",
    }
    system.update(overrides)
    return system


def _valid_hardware(**overrides: object) -> dict[str, object]:
    hardware: dict[str, object] = {
        "schema_version": 1,
        "installed_memory_source": "GetPhysicallyInstalledSystemMemory",
        "os_visible_memory_source": "GlobalMemoryStatusEx",
        "os_version_source": "sys.getwindowsversion",
        "processor_source": r"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        "os_release": "11",
        "os_build_number": 26_200,
        "os_product_type": 1,
        "cpu_manufacturer": "GenuineIntel",
        "cpu_name": "Intel(R) Core(TM) i5-8250U",
        "cpu_physical_cores": 4,
        "cpu_logical_processors": 8,
        "memory_installed_bytes": 16 * GIB,
        "memory_os_visible_bytes": VISIBLE_16_GIB_PC_BYTES,
    }
    hardware.update(overrides)
    return hardware


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: str = "SCHOOL-PC",
    bind_host: bool = True,
    bind_out_dir: bool = True,
    cache_mode: str = "on",
    started_at: str = "2026-09-08T09:00:00+09:00",
    finished_at: str = "2026-09-08T09:46:00+09:00",
    collected_at: str = "2026-09-08T09:46:30+09:00",
) -> Path:
    root = tmp_path
    monkeypatch.setattr(field_validation, "ROOT", root)
    monkeypatch.setenv("COMPUTERNAME", host)
    host_hash = hashlib.sha256(host.encode()).hexdigest()
    prefix = host_hash[:16] if bind_host else "0" * 16
    run = root / "docs" / "field" / "evidence" / "2026-09-08" / (
        f"20260908-090000-{prefix}-school-cache-{cache_mode}"
    )
    work = run / "_work"
    work.mkdir(parents=True)
    effective_relative = f"_work/accept-hy-mt2-sherpa-cache-{cache_mode}.yaml"
    (run / effective_relative).write_text("effective", encoding="utf-8")
    source_relatives = {
        "scripts/__init__.py",
        "scripts/acceptance.py",
        "scripts/bench.py",
        "scripts/performance_evidence.py",
        "scripts/replay_client.py",
        "scripts/story_tracker.py",
        "scripts/text_metrics.py",
        "server/main.py",
        "config.yaml",
        "docs/qa/stories.tsv",
    }
    for relative in source_relatives:
        source = root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"fixture source: {relative}\n", encoding="utf-8")
    report = run / "2026-09-08-acceptance.json"
    samples = 1_400
    result = {
        "minutes": 45.0,
        "students": 10,
        "corpus": "ja_ext",
        "langs": ["en", "zh"],
        "measurement_students": 10,
        "measurement_langs": {"en": 5, "zh": 5},
        "measurement_seconds": 2_700.0,
        "ran_seconds": 2_720.0,
        "audio_bytes_planned": 1_000,
        "audio_bytes_sent": 1_000,
        "disconnect_every_s": None,
        "disconnects": 0,
        "reconnect_failures": 0,
        "mt_cache_size": None if cache_mode == "on" else 0,
        "mt_cache": {"size": 20 if cache_mode == "on" else 0},
        "effective_config": effective_relative,
        "engine": "hy-mt2",
        "asr_engine": "sherpa",
        "crashes": 0,
        "errors": [],
        "captions": 50,
        "calls": {"asr": 25, "mt": 50},
        "restore": {"injection_attempts": 0, "injected_disconnects": 0},
        "memory": {"peak_mb": 2_048.0, "increasing": False, "samples": samples},
        "cpu_percent": {"median": 40.0, "samples": samples},
        "resources": [
            {"name": "threads", "samples": samples},
            {"name": "handles", "samples": samples},
            {"name": "tasks", "samples": samples},
        ],
        "resource_observation": {
            "sampler_interval_s": 2.0,
            "stats_interval_s": 2.0,
            "process_samples": samples,
            "rss_samples": samples,
            "thread_samples": samples,
            "handle_samples": samples,
            "stats_samples": samples,
            "process_first_at_s": 1.0,
            "stats_first_at_s": 1.0,
            "process_max_gap_s": 3.0,
            "stats_max_gap_s": 3.0,
            "process_last_at_s": 2_700.0,
            "stats_last_at_s": 2_700.0,
        },
        "final_stats": {
            "students": 10,
            "langs": {"en": 5, "zh": 5},
            "at_s": 2_720.0,
            "queue_depth": 0,
            "audio_queue_seconds": 0,
            "mt_queue_depth": 0,
            "mt_cache_size": 20 if cache_mode == "on" else 0,
        },
        "verdict": {"passed": True},
        "latency": {"median_s": 2.0, "max_s": 4.0},
    }
    report_payload = {"system": _valid_system(), "results": [result]}
    report.write_text(json.dumps(report_payload), encoding="utf-8")
    run_relative = run.relative_to(root).as_posix()
    manifest = {
        "schema_version": 3,
        "kind": "soak",
        "complete": True,
        "started_at": started_at,
        "finished_at": finished_at,
        "arguments": {
            "minutes": 45.0,
            "students": 10,
            "audio": "lesson.wav",
            "corpus": "ja_ext",
            "langs": ["en", "zh"],
            "disconnect_every_s": None,
            "mt_cache_size": None if cache_mode == "on" else 0,
            "engine": "hy-mt2",
            "asr": "sherpa",
            "partial": None,
            "port": 18_431,
            "drain_seconds": 20.0,
            "out_dir": run_relative if bind_out_dir else "docs/field/evidence/wrong",
            "no_config_update": True,
        },
        "configuration": {
            "effective": {effective_relative: _sha256(run / effective_relative)}
        },
        "source_hashes": {
            relative: _sha256(root / relative)
            for relative in sorted(source_relatives)
        },
        "input_hashes": {"lesson.wav": AUDIO_SHA256},
        "runtime_asset_hashes": {"model.bin": "c" * 64},
        "artifacts": {report.name: _sha256(report)},
        "primary_report": report.name,
        "runtime": {"python": "3.12.10"},
    }
    manifest_path = run / field_validation.MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        field_validation,
        "validate_performance_manifest",
        lambda *args, **kwargs: manifest,
    )
    monkeypatch.setattr(
        field_validation, "_collect_windows_hardware", _valid_hardware
    )
    monkeypatch.setattr(
        field_validation,
        "_hardware_collected_at",
        lambda: collected_at,
    )
    return run


def test_valid_run_is_bound_to_current_hostname_and_out_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _make_run(tmp_path, monkeypatch)

    payload = field_validation.validate_run(
        run, cache_mode="on", audio_sha256=AUDIO_SHA256
    )

    assert payload["passed"] is True
    assert payload["schema_version"] == 2
    assert payload["checks"]["run_dir_binds_validation_host"] is True
    assert payload["checks"]["arguments_out_dir_matches_run"] is True
    assert payload["checks"]["hardware_collected_immediately_after_run"] is True
    assert payload["hardware"] == _valid_hardware()
    expected_hardware_hash = hashlib.sha256(
        json.dumps(
            _valid_hardware(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    assert payload["hardware_snapshot_sha256"] == expected_hardware_hash


@pytest.mark.parametrize(
    ("bind_host", "bind_out_dir", "failed_check"),
    [
        (False, True, "run_dir_binds_validation_host"),
        (True, False, "arguments_out_dir_matches_run"),
    ],
)
def test_run_binding_mismatch_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bind_host: bool,
    bind_out_dir: bool,
    failed_check: str,
) -> None:
    run = _make_run(
        tmp_path,
        monkeypatch,
        bind_host=bind_host,
        bind_out_dir=bind_out_dir,
    )

    payload = field_validation.validate_run(
        run, cache_mode="on", audio_sha256=AUDIO_SHA256
    )

    assert payload["passed"] is False
    assert payload["checks"][failed_check] is False


def test_run_rejects_hardware_collection_outside_immediate_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _make_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        field_validation,
        "_hardware_collected_at",
        lambda: "2026-09-08T10:01:01+09:00",
    )

    payload = field_validation.validate_run(
        run, cache_mode="on", audio_sha256=AUDIO_SHA256
    )

    assert payload["passed"] is False
    assert payload["checks"]["hardware_collected_immediately_after_run"] is False


def test_number_rejects_an_integer_too_large_for_float() -> None:
    assert field_validation._number(10**10_000) is None


def _validate_system(
    *,
    system: dict[str, object] | None = None,
    hardware: dict[str, object] | None = None,
) -> tuple[dict[str, bool], dict[str, object], dict[str, object]]:
    return field_validation._validated_system(
        _valid_system() if system is None else system,
        hardware=_valid_hardware() if hardware is None else hardware,
        started_at="2026-09-08T09:00:00+09:00",
    )


@pytest.mark.parametrize(
    ("system", "hardware"),
    [
        (
            _valid_system(ram_gb=7.5),
            _valid_hardware(
                memory_installed_bytes=8 * GIB,
                memory_os_visible_bytes=7_500_000_000,
            ),
        ),
        (_valid_system(), _valid_hardware()),
        (
            _valid_system(ram_gb=17.2, os="Windows 11 Pro"),
            _valid_hardware(
                memory_installed_bytes=16 * GIB,
                memory_os_visible_bytes=16 * GIB,
            ),
        ),
        (
            _valid_system(
                cpu="11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz",
                ram_gb=12.5,
            ),
            _valid_hardware(
                cpu_name="11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz",
                memory_installed_bytes=12 * GIB,
                memory_os_visible_bytes=12_500_000_000,
            ),
        ),
    ],
)
def test_n05_hardware_gate_accepts_supported_school_pc(
    system: dict[str, object], hardware: dict[str, object]
) -> None:
    checks, normalized_system, normalized_hardware = _validate_system(
        system=system, hardware=hardware
    )

    assert checks == {
        "windows_hardware_snapshot": True,
        "school_hardware_supported": True,
        "report_matches_hardware": True,
        "windows_system_metadata": True,
    }
    assert normalized_system == system
    assert normalized_hardware == hardware


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("os_release", "10"),
        ("os_build_number", 21_999),
        ("os_product_type", 3),
        ("cpu_manufacturer", "AuthenticAMD"),
        ("cpu_name", "Intel(R) Core(TM) i7-1165G7"),
        ("cpu_name", "Intel(R) Core(TM) Ultra 5 125U"),
        ("cpu_physical_cores", 0),
        ("cpu_physical_cores", None),
        ("cpu_logical_processors", 0),
        ("memory_installed_bytes", 8 * GIB - 1),
        ("memory_installed_bytes", 16 * GIB + 1),
        ("memory_installed_bytes", 16.0 * GIB),
        ("memory_installed_bytes", True),
        ("memory_installed_bytes", str(16 * GIB)),
        ("memory_os_visible_bytes", 0),
        ("memory_os_visible_bytes", 16 * GIB + 1),
    ],
)
def test_n05_hardware_gate_rejects_unsupported_raw_hardware(
    field: str, value: object
) -> None:
    checks, _, _ = _validate_system(hardware=_valid_hardware(**{field: value}))

    assert checks["windows_system_metadata"] is False


@pytest.mark.parametrize("schema_version", [True, 1.0, "1"])
def test_n05_hardware_gate_requires_integer_hardware_schema(
    schema_version: object,
) -> None:
    checks, _, _ = _validate_system(
        hardware=_valid_hardware(schema_version=schema_version)
    )

    assert checks["windows_hardware_snapshot"] is False
    assert checks["windows_system_metadata"] is False


@pytest.mark.parametrize(
    ("finished_at", "collected_at", "expected"),
    [
        (
            "2026-09-08T09:46:00+09:00",
            "2026-09-08T09:46:00+09:00",
            True,
        ),
        (
            "2026-09-08T09:46:00+09:00",
            "2026-09-08T10:01:00+09:00",
            True,
        ),
        (
            "2026-09-08T09:46:00+09:00",
            "2026-09-08T09:45:59+09:00",
            False,
        ),
        (
            "2026-09-08T09:46:00+09:00",
            "2026-09-08T10:01:01+09:00",
            False,
        ),
        (
            "2026-09-08T09:46:00",
            "2026-09-08T09:46:01+09:00",
            False,
        ),
        (
            "2026-09-08T09:46:00+09:00",
            "2026-09-08T09:46:01",
            False,
        ),
        ("not-a-time", "2026-09-08T09:46:01+09:00", False),
    ],
)
def test_hardware_collection_timing_is_timezone_aware_and_immediate(
    finished_at: object, collected_at: object, expected: bool
) -> None:
    assert (
        field_validation._hardware_collection_timing_ok(
            finished_at=finished_at,
            collected_at=collected_at,
        )
        is expected
    )


def test_n05_hardware_gate_rejects_missing_raw_hardware() -> None:
    hardware = _valid_hardware()
    del hardware["memory_installed_bytes"]

    checks, _, normalized_hardware = _validate_system(hardware=hardware)

    assert checks["windows_hardware_snapshot"] is False
    assert checks["windows_system_metadata"] is False
    assert normalized_hardware["memory_installed_bytes"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cpu", "Intel(R) Core(TM) i5-1235U"),
        ("cores", 6),
        ("threads", 12),
        ("ram_gb", 16.3),
        ("os", "Windows 10"),
        ("date", "2026-09-07"),
    ],
)
def test_n05_hardware_gate_rejects_report_that_does_not_match_live_hardware(
    field: str, value: object
) -> None:
    checks, _, _ = _validate_system(system=_valid_system(**{field: value}))

    assert checks["report_matches_hardware"] is False
    assert checks["windows_system_metadata"] is False


def test_collect_windows_hardware_preserves_raw_integer_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(field_validation.platform, "system", lambda: "Windows")
    monkeypatch.setattr(field_validation.platform, "release", lambda: "11")
    monkeypatch.setattr(
        field_validation,
        "_windows_version",
        lambda: SimpleNamespace(build=26_200, product_type=1),
    )
    monkeypatch.setattr(
        field_validation,
        "_windows_memory_bytes",
        lambda: (16 * GIB, VISIBLE_16_GIB_PC_BYTES),
    )
    monkeypatch.setattr(
        field_validation,
        "_windows_cpu_identity",
        lambda: ("GenuineIntel", "Intel(R) Core(TM) i5-8250U"),
    )
    monkeypatch.setattr(
        field_validation.psutil,
        "cpu_count",
        lambda *, logical: 8 if logical else 4,
    )

    hardware = field_validation._collect_windows_hardware()

    assert hardware == _valid_hardware()
    assert isinstance(hardware["memory_installed_bytes"], int)
    assert isinstance(hardware["memory_os_visible_bytes"], int)


def test_collect_windows_hardware_fails_closed_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(field_validation.platform, "system", lambda: "Linux")

    with pytest.raises(field_validation.PerformanceEvidenceError):
        field_validation._collect_windows_hardware()


class _FakeWindowsFunction:
    def __init__(self, callback: object, *, succeeds: bool = True) -> None:
        self.callback = callback
        self.succeeds = succeeds
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, pointer: object) -> bool:
        if self.succeeds:
            self.callback(pointer)  # type: ignore[operator]
        return self.succeeds


def _fake_kernel32(*, installed_ok: bool = True, visible_ok: bool = True) -> object:
    def set_installed(pointer: object) -> None:
        pointer._obj.value = 16 * GIB // 1024  # type: ignore[attr-defined]

    def set_visible(pointer: object) -> None:
        pointer._obj.ullTotalPhys = VISIBLE_16_GIB_PC_BYTES  # type: ignore[attr-defined]

    return SimpleNamespace(
        GetPhysicallyInstalledSystemMemory=_FakeWindowsFunction(
            set_installed, succeeds=installed_ok
        ),
        GlobalMemoryStatusEx=_FakeWindowsFunction(
            set_visible, succeeds=visible_ok
        ),
    )


def test_windows_memory_api_converts_installed_kib_to_exact_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *args, **kwargs: _fake_kernel32(),
        raising=False,
    )

    installed, visible = field_validation._windows_memory_bytes()

    assert installed == 16 * GIB
    assert visible == VISIBLE_16_GIB_PC_BYTES


@pytest.mark.parametrize(
    ("installed_ok", "visible_ok"), [(False, True), (True, False)]
)
def test_windows_memory_api_failures_are_not_replaced_with_estimates(
    monkeypatch: pytest.MonkeyPatch, installed_ok: bool, visible_ok: bool
) -> None:
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *args, **kwargs: _fake_kernel32(
            installed_ok=installed_ok, visible_ok=visible_ok
        ),
        raising=False,
    )

    with pytest.raises(OSError):
        field_validation._windows_memory_bytes()


def test_receiving_gate_requires_the_complete_current_soak_source_set() -> None:
    wizard = (field_validation.ROOT / "scripts/field-verification.sh").read_text(
        encoding="utf-8"
    )
    blocks = re.findall(r"<<'PY'\r?\n(.*?)\r?\nPY", wizard, flags=re.S)
    parsed_blocks = [ast.parse(block) for block in blocks]
    tree = parsed_blocks[0]
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "expected_soak_source_keys"
    )
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<gate>", "exec"), namespace)
    actual = namespace["expected_soak_source_keys"](field_validation.ROOT)  # type: ignore[operator]
    expected = {
        path.resolve().relative_to(field_validation.ROOT).as_posix()
        for path in performance_source_files(field_validation.ROOT, "soak")
    }

    assert actual == expected
    assert "set(source_hashes) == expected_soak_source_keys(root)" in blocks[0]
    assert 'payload.get("schema_version") == 2' in blocks[0]
    assert 'payload.get("hardware_snapshot_sha256") == canonical_sha256(hardware)' in blocks[0]
    assert '8 * 1024**3 <= hardware["memory_installed_bytes"] <= 16 * 1024**3' in blocks[0]
    assert 'hardware_matches_report' in blocks[0]
    assert 'type(hardware.get("schema_version")) is int' in blocks[0]
    assert '0 <= hardware_delay.total_seconds() <= 15 * 60' in blocks[0]
    assert '"hardware_collected_immediately_after_run"' in blocks[0]
    assert any("same_hardware" in block for block in blocks[1:])
    assert any("on_collected <= off_started" in block for block in blocks[1:])


def _field_python_blocks(root: Path) -> list[str]:
    wizard = (root / "scripts/field-verification.sh").read_text(
        encoding="utf-8"
    )
    return re.findall(r"<<'PY'\r?\n(.*?)\r?\nPY", wizard, flags=re.S)


def _run_receiving_gate(
    block: str, *, run: Path, payload: dict[str, object], cache_mode: str = "on"
) -> subprocess.CompletedProcess[str]:
    validation = run / "field-validation.json"
    validation.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-",
            str(validation),
            cache_mode,
            str(payload["validator_sha256"]),
            str(field_validation.ROOT),
            str(payload["manifest"]),
            str(payload["primary_report"]),
            AUDIO_SHA256,
            "sherpa",
            "hy-mt2",
        ],
        input=block,
        text=True,
        capture_output=True,
        check=False,
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_receiving_gate_executes_against_a_complete_valid_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_root = field_validation.ROOT
    block = _field_python_blocks(original_root)[0]
    run = _make_run(tmp_path, monkeypatch)
    payload = field_validation.validate_run(
        run, cache_mode="on", audio_sha256=AUDIO_SHA256
    )

    completed = _run_receiving_gate(block, run=run, payload=payload)

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "schema_bool",
        "hardware_without_hash",
        "hash_without_hardware",
        "cpu_report_mismatch",
        "before_run_finished",
        "after_immediate_window",
    ],
)
def test_receiving_gate_executes_fail_closed_for_hardware_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    original_root = field_validation.ROOT
    block = _field_python_blocks(original_root)[0]
    run = _make_run(tmp_path, monkeypatch)
    payload = field_validation.validate_run(
        run, cache_mode="on", audio_sha256=AUDIO_SHA256
    )
    hardware = payload["hardware"]
    assert isinstance(hardware, dict)
    if mutation == "schema_bool":
        hardware["schema_version"] = True
        payload["hardware_snapshot_sha256"] = _canonical_sha256(hardware)
    elif mutation == "hardware_without_hash":
        hardware["memory_installed_bytes"] = 12 * GIB
    elif mutation == "hash_without_hardware":
        payload["hardware_snapshot_sha256"] = "0" * 64
    elif mutation == "cpu_report_mismatch":
        hardware["cpu_name"] = "Intel(R) Core(TM) i5-1235U"
        payload["hardware_snapshot_sha256"] = _canonical_sha256(hardware)
    elif mutation == "before_run_finished":
        payload["hardware_collected_at"] = "2026-09-08T09:45:59+09:00"
    else:
        payload["hardware_collected_at"] = "2026-09-08T10:01:01+09:00"

    completed = _run_receiving_gate(block, run=run, payload=payload)

    assert completed.returncode == 1, completed.stderr


def _write_validation(run: Path, payload: dict[str, object]) -> Path:
    path = run / "field-validation.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run_pair_gate(
    block: str, *, cache_on: Path, cache_off: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-", str(cache_on), str(cache_off)],
        input=block,
        text=True,
        capture_output=True,
        check=False,
    )


def _valid_validation_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
    cache_on_run = _make_run(tmp_path, monkeypatch)
    cache_on = field_validation.validate_run(
        cache_on_run, cache_mode="on", audio_sha256=AUDIO_SHA256
    )
    cache_off_run = _make_run(
        tmp_path,
        monkeypatch,
        cache_mode="off",
        started_at="2026-09-08T10:00:00+09:00",
        finished_at="2026-09-08T10:46:00+09:00",
        collected_at="2026-09-08T10:46:30+09:00",
    )
    cache_off = field_validation.validate_run(
        cache_off_run, cache_mode="off", audio_sha256=AUDIO_SHA256
    )
    assert cache_on["passed"] is True
    assert cache_off["passed"] is True
    return (
        _write_validation(cache_on_run, cache_on),
        _write_validation(cache_off_run, cache_off),
        cache_on,
        cache_off,
    )


def test_pair_gate_executes_against_two_sequential_valid_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_root = field_validation.ROOT
    pair_block = next(
        block
        for block in _field_python_blocks(original_root)
        if "same_hardware =" in block
    )
    cache_on_path, cache_off_path, _, _ = _valid_validation_pair(
        tmp_path, monkeypatch
    )

    completed = _run_pair_gate(
        pair_block, cache_on=cache_on_path, cache_off=cache_off_path
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("mutation", ["hardware", "host", "overlapping_capture"])
def test_pair_gate_executes_fail_closed_for_cross_run_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    original_root = field_validation.ROOT
    pair_block = next(
        block
        for block in _field_python_blocks(original_root)
        if "same_hardware =" in block
    )
    cache_on_path, cache_off_path, cache_on, cache_off = _valid_validation_pair(
        tmp_path, monkeypatch
    )
    if mutation == "hardware":
        hardware = cache_off["hardware"]
        assert isinstance(hardware, dict)
        hardware["memory_installed_bytes"] = 12 * GIB
        cache_off["hardware_snapshot_sha256"] = _canonical_sha256(hardware)
        _write_validation(cache_off_path.parent, cache_off)
    elif mutation == "host":
        cache_off["host_fingerprint_sha256"] = "f" * 64
        _write_validation(cache_off_path.parent, cache_off)
    else:
        cache_on["hardware_collected_at"] = "2026-09-08T10:00:01+09:00"
        _write_validation(cache_on_path.parent, cache_on)

    completed = _run_pair_gate(
        pair_block, cache_on=cache_on_path, cache_off=cache_off_path
    )

    assert completed.returncode == 1, completed.stderr
