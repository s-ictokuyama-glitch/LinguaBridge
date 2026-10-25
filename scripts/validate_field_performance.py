#!/usr/bin/env python3
"""Validate one field-performance run before its evidence leaves the school PC."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.performance_evidence import (  # noqa: E402
    MANIFEST_NAME,
    PerformanceEvidenceError,
    validate_performance_manifest,
)


REAL_ASR_ENGINES = {"sherpa", "faster-whisper"}
REAL_MT_ENGINES = {"hy-mt2", "nllb"}
RESOURCE_INTERVAL_SECONDS = 2.0
FIELD_VALIDATION_SCHEMA = 2
GIB = 1024**3
MIN_INSTALLED_MEMORY_BYTES = 8 * GIB
MAX_INSTALLED_MEMORY_BYTES = 16 * GIB
HARDWARE_COLLECTION_MAX_DELAY_SECONDS = 15 * 60
HARDWARE_SNAPSHOT_KEYS = (
    "schema_version",
    "installed_memory_source",
    "os_visible_memory_source",
    "os_version_source",
    "processor_source",
    "os_release",
    "os_build_number",
    "os_product_type",
    "cpu_manufacturer",
    "cpu_name",
    "cpu_physical_cores",
    "cpu_logical_processors",
    "memory_installed_bytes",
    "memory_os_visible_bytes",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _host_fingerprint_sha256() -> str:
    hostname = (os.environ.get("COMPUTERNAME") or platform.node()).strip()
    if not hostname:
        raise PerformanceEvidenceError("Windows hostnameを取得できません")
    return hashlib.sha256(hostname.encode("utf-8")).hexdigest()


def _windows_version() -> Any:
    getter = getattr(sys, "getwindowsversion", None)
    if getter is None:
        raise PerformanceEvidenceError("Windows version APIを利用できません")
    return getter()


def _windows_memory_bytes() -> tuple[int, int]:
    """Return SMBIOS-installed and OS-visible physical memory as exact bytes."""
    import ctypes
    from ctypes import wintypes

    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise PerformanceEvidenceError("Windows memory APIを利用できません")
    kernel32: Any = loader("kernel32", use_last_error=True)

    installed_kib = ctypes.c_ulonglong()
    get_installed = kernel32.GetPhysicallyInstalledSystemMemory
    get_installed.argtypes = (ctypes.POINTER(ctypes.c_ulonglong),)
    get_installed.restype = wintypes.BOOL
    if not get_installed(ctypes.byref(installed_kib)):
        error = getattr(ctypes, "get_last_error", lambda: 0)()
        raise OSError(error, "搭載物理メモリを取得できません")

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = (
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        )

    visible = MemoryStatusEx()
    visible.dwLength = ctypes.sizeof(MemoryStatusEx)
    get_visible = kernel32.GlobalMemoryStatusEx
    get_visible.argtypes = (ctypes.POINTER(MemoryStatusEx),)
    get_visible.restype = wintypes.BOOL
    if not get_visible(ctypes.byref(visible)):
        error = getattr(ctypes, "get_last_error", lambda: 0)()
        raise OSError(error, "OS可視物理メモリを取得できません")

    return int(installed_kib.value) * 1024, int(visible.ullTotalPhys)


def _windows_cpu_identity() -> tuple[str, str]:
    import winreg

    key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
        manufacturer = winreg.QueryValueEx(key, "VendorIdentifier")[0]
        name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
    if not isinstance(manufacturer, str) or not isinstance(name, str):
        raise PerformanceEvidenceError("Windows CPU識別情報が文字列ではありません")
    return " ".join(manufacturer.split()), " ".join(name.split())


def _collect_windows_hardware() -> dict[str, Any]:
    """Capture the live Windows machine independently of the sealed report."""
    if platform.system() != "Windows":
        raise PerformanceEvidenceError("現地性能validatorはWindows上でのみ実行できます")
    version = _windows_version()
    installed_bytes, visible_bytes = _windows_memory_bytes()
    manufacturer, cpu_name = _windows_cpu_identity()
    return {
        "schema_version": 1,
        "installed_memory_source": "GetPhysicallyInstalledSystemMemory",
        "os_visible_memory_source": "GlobalMemoryStatusEx",
        "os_version_source": "sys.getwindowsversion",
        "processor_source": r"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        "os_release": platform.release(),
        "os_build_number": getattr(version, "build", None),
        "os_product_type": getattr(version, "product_type", None),
        "cpu_manufacturer": manufacturer,
        "cpu_name": cpu_name,
        "cpu_physical_cores": psutil.cpu_count(logical=False),
        "cpu_logical_processors": psutil.cpu_count(logical=True),
        "memory_installed_bytes": installed_bytes,
        "memory_os_visible_bytes": visible_bytes,
    }


def _hardware_collected_at() -> str:
    return datetime.datetime.now().astimezone().isoformat()


def _hardware_collection_timing_ok(*, finished_at: Any, collected_at: Any) -> bool:
    """Require a timezone-aware hardware capture immediately after the run."""
    if not isinstance(finished_at, str) or not isinstance(collected_at, str):
        return False
    try:
        finished = datetime.datetime.fromisoformat(finished_at)
        collected = datetime.datetime.fromisoformat(collected_at)
        if finished.utcoffset() is None or collected.utcoffset() is None:
            return False
        delay = (collected - finished).total_seconds()
    except (OverflowError, ValueError):
        return False
    return 0 <= delay <= HARDWARE_COLLECTION_MAX_DELAY_SECONDS


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _same_number(left: Any, right: Any) -> bool:
    left_number = _number(left)
    right_number = _number(right)
    return left_number is not None and left_number == right_number


def _at_least(value: Any, minimum: float) -> bool:
    number = _number(value)
    return number is not None and number >= minimum


def _at_most(value: Any, maximum: float) -> bool:
    number = _number(value)
    return number is not None and number <= maximum


def _coverage_ok(result: dict[str, Any], target_seconds: float) -> bool:
    observation = result.get("resource_observation")
    if not isinstance(observation, dict):
        return False
    required = int(target_seconds / RESOURCE_INTERVAL_SECONDS * 0.8)
    resources = result.get("resources")
    by_name = {
        item.get("name"): item
        for item in (resources if isinstance(resources, list) else [])
        if isinstance(item, dict)
    }
    measurement = _number(result.get("measurement_seconds"))
    memory = result.get("memory")
    cpu = result.get("cpu_percent")
    if measurement is None or not isinstance(memory, dict) or not isinstance(cpu, dict):
        return False
    return bool(
        observation.get("sampler_interval_s") == RESOURCE_INTERVAL_SECONDS
        and observation.get("stats_interval_s") == RESOURCE_INTERVAL_SECONDS
        and _at_least(observation.get("process_samples"), required)
        and _at_least(observation.get("rss_samples"), required)
        and _at_least(observation.get("thread_samples"), required)
        and _at_least(observation.get("handle_samples"), required)
        and _at_least(observation.get("stats_samples"), required)
        and _at_most(observation.get("process_first_at_s"), 10.0)
        and _at_most(observation.get("stats_first_at_s"), 10.0)
        and _at_most(observation.get("process_max_gap_s"), 10.0)
        and _at_most(observation.get("stats_max_gap_s"), 10.0)
        and _at_least(observation.get("process_last_at_s"), measurement)
        and _at_least(observation.get("stats_last_at_s"), measurement)
        and by_name.get("threads", {}).get("samples")
        == observation.get("thread_samples")
        and by_name.get("handles", {}).get("samples")
        == observation.get("handle_samples")
        and by_name.get("tasks", {}).get("samples")
        == observation.get("stats_samples")
        and memory.get("samples") == observation.get("rss_samples")
        and cpu.get("samples") == observation.get("process_samples")
    )


def _load_sealed_report(
    report_path: Path, manifest: dict[str, Any], run_dir: Path
) -> tuple[dict[str, Any], str]:
    primary = manifest.get("primary_report")
    artifacts = manifest.get("artifacts")
    if not isinstance(primary, str) or not isinstance(artifacts, dict):
        raise PerformanceEvidenceError("manifestのartifact索引が不正です")
    if (run_dir / primary).resolve() != report_path.resolve():
        raise PerformanceEvidenceError("manifestのprimary report参照が一致しません")
    expected_hash = artifacts.get(primary)
    if not isinstance(expected_hash, str):
        raise PerformanceEvidenceError("manifestにprimary report hashがありません")
    payload = report_path.read_bytes()
    actual_hash = hashlib.sha256(payload).hexdigest()
    if actual_hash != expected_hash:
        raise PerformanceEvidenceError("監査直前のprimary report hashが一致しません")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PerformanceEvidenceError("primary report JSONを読めません") from exc
    if not isinstance(decoded, dict):
        raise PerformanceEvidenceError("reportのrootがobjectではありません")
    return decoded, actual_hash


def _validated_system(
    system: Any, *, hardware: Any, started_at: Any
) -> tuple[dict[str, bool], dict[str, Any], dict[str, Any]]:
    if not isinstance(system, dict):
        failed = {
            "windows_hardware_snapshot": False,
            "school_hardware_supported": False,
            "report_matches_hardware": False,
            "windows_system_metadata": False,
        }
        return failed, {}, {}
    normalized = {
        key: system.get(key)
        for key in ("cpu", "cores", "threads", "ram_gb", "os", "python", "date")
    }
    hardware_mapping = hardware if isinstance(hardware, dict) else {}
    normalized_hardware = {
        key: hardware_mapping.get(key) for key in HARDWARE_SNAPSHOT_KEYS
    }
    cores = system.get("cores")
    threads = system.get("threads")
    ram_gb = _number(system.get("ram_gb"))
    os_name = system.get("os")
    cpu_name = system.get("cpu")
    build = normalized_hardware["os_build_number"]
    product_type = normalized_hardware["os_product_type"]
    physical_cores = normalized_hardware["cpu_physical_cores"]
    logical_processors = normalized_hardware["cpu_logical_processors"]
    installed_bytes = normalized_hardware["memory_installed_bytes"]
    visible_bytes = normalized_hardware["memory_os_visible_bytes"]

    def positive_integer(value: Any) -> int | None:
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
            else None
        )

    def normalized_text(value: Any) -> str:
        return " ".join(value.split()).casefold() if isinstance(value, str) else ""

    release_text = (
        normalized_hardware["os_release"].strip()
        if isinstance(normalized_hardware["os_release"], str)
        else ""
    )
    build_int = positive_integer(build)
    product_type_int = positive_integer(product_type)
    physical_cores_int = positive_integer(physical_cores)
    logical_processors_int = positive_integer(logical_processors)
    installed_bytes_int = positive_integer(installed_bytes)
    visible_bytes_int = positive_integer(visible_bytes)
    report_cores = positive_integer(cores)
    report_threads = positive_integer(threads)

    valid_source_metadata = (
        positive_integer(normalized_hardware["schema_version"]) == 1
        and normalized_hardware["installed_memory_source"]
        == "GetPhysicallyInstalledSystemMemory"
        and normalized_hardware["os_visible_memory_source"] == "GlobalMemoryStatusEx"
        and normalized_hardware["os_version_source"] == "sys.getwindowsversion"
        and normalized_hardware["processor_source"]
        == r"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    )
    hardware_complete = (
        valid_source_metadata
        and bool(release_text)
        and build_int is not None
        and product_type_int is not None
        and isinstance(normalized_hardware["cpu_manufacturer"], str)
        and bool(normalized_hardware["cpu_manufacturer"].strip())
        and isinstance(normalized_hardware["cpu_name"], str)
        and bool(normalized_hardware["cpu_name"].strip())
        and physical_cores_int is not None
        and logical_processors_int is not None
        and logical_processors_int >= physical_cores_int
        and installed_bytes_int is not None
        and visible_bytes_int is not None
        and visible_bytes_int <= installed_bytes_int
    )
    supported_os = (
        hardware_complete
        and release_text == "11"
        and build_int is not None
        and build_int >= 22_000
        and product_type_int == 1
    )
    live_cpu_name = normalized_hardware["cpu_name"]
    supported_cpu = (
        hardware_complete
        and normalized_text(normalized_hardware["cpu_manufacturer"]) == "genuineintel"
        and "intel" in normalized_text(live_cpu_name)
        and "core" in normalized_text(live_cpu_name)
        and bool(
            re.search(
                r"(?<![a-z0-9])i5(?:[-\s]|$)",
                str(live_cpu_name),
                flags=re.IGNORECASE,
            )
        )
    )
    supported_memory = (
        hardware_complete
        and installed_bytes_int is not None
        and MIN_INSTALLED_MEMORY_BYTES
        <= installed_bytes_int
        <= MAX_INSTALLED_MEMORY_BYTES
    )
    try:
        system_date = datetime.date.fromisoformat(str(system.get("date")))
        started_date = datetime.datetime.fromisoformat(str(started_at)).date()
        date_matches = system_date == started_date
    except ValueError:
        date_matches = False
    report_metadata_valid = (
        isinstance(cpu_name, str)
        and bool(cpu_name.strip())
        and isinstance(os_name, str)
        and bool(re.match(r"^Windows 11(?:\b|$)", os_name, flags=re.IGNORECASE))
        and report_cores is not None
        and report_threads is not None
        and report_threads >= report_cores
        and ram_gb is not None
        and isinstance(system.get("python"), str)
        and bool(system["python"].strip())
        and date_matches
    )
    report_matches_hardware = (
        report_metadata_valid
        and hardware_complete
        and normalized_text(cpu_name) == normalized_text(live_cpu_name)
        and report_cores == physical_cores_int
        and report_threads == logical_processors_int
        and visible_bytes_int is not None
        and ram_gb == round(visible_bytes_int / 1e9, 1)
    )
    school_hardware_supported = bool(
        hardware_complete and supported_os and supported_cpu and supported_memory
    )
    checks = {
        "windows_hardware_snapshot": bool(hardware_complete),
        "school_hardware_supported": school_hardware_supported,
        "report_matches_hardware": bool(report_matches_hardware),
        "windows_system_metadata": bool(
            hardware_complete
            and school_hardware_supported
            and report_matches_hardware
        ),
    }
    return checks, normalized, normalized_hardware


def _validate_result(
    result: Any,
    *,
    arguments: dict[str, Any],
    effective_configs: set[str],
    cache_mode: str,
    audio_sha256: str,
) -> tuple[dict[str, bool], dict[str, Any]]:
    if not isinstance(result, dict):
        return {"result_object": False}, {}

    measurement = _number(result.get("measurement_seconds"))
    memory = result.get("memory")
    peak_rss = _number(memory.get("peak_mb")) if isinstance(memory, dict) else None
    cpu = result.get("cpu_percent")
    cpu_median = _number(cpu.get("median")) if isinstance(cpu, dict) else None
    measurement_langs = result.get("measurement_langs")
    langs = result.get("langs")
    students = result.get("students")
    expected_langs: dict[str, int] = {}
    if isinstance(students, int) and not isinstance(students, bool) and isinstance(langs, list) and langs:
        for index in range(students):
            lang = langs[index % len(langs)]
            if isinstance(lang, str):
                expected_langs[lang] = expected_langs.get(lang, 0) + 1
    planned = result.get("audio_bytes_planned")
    sent = result.get("audio_bytes_sent")
    errors = result.get("errors")
    verdict = result.get("verdict")
    latency = result.get("latency")
    final_stats = result.get("final_stats")
    mt_cache = result.get("mt_cache")
    calls = result.get("calls")
    restore = result.get("restore")
    minutes = _number(arguments.get("minutes"))
    drain_seconds = _number(arguments.get("drain_seconds"))
    target_seconds = minutes * 60 if minutes is not None else 0.0
    measurement_complete = (
        minutes is not None
        and minutes >= 45
        and measurement is not None
        and measurement >= target_seconds
        and drain_seconds is not None
        and _at_least(result.get("ran_seconds"), measurement + drain_seconds)
        and isinstance(planned, int)
        and not isinstance(planned, bool)
        and planned > 0
        and sent == planned
    )
    population_complete = (
        students == 10
        and result.get("measurement_students") == students
        and measurement_langs == expected_langs
        and set(expected_langs) == {"en", "zh"}
        and all(count > 0 for count in expected_langs.values())
        and isinstance(final_stats, dict)
        and final_stats.get("students") == students
        and final_stats.get("langs") == expected_langs
        and _at_least(
            final_stats.get("at_s"),
            (_number(result.get("ran_seconds")) or 0) - RESOURCE_INTERVAL_SECONDS * 2,
        )
        and final_stats.get("queue_depth") == 0
        and final_stats.get("audio_queue_seconds") == 0
        and final_stats.get("mt_queue_depth") == 0
    )
    if cache_mode == "off":
        cache_mode_matches = (
            arguments.get("mt_cache_size") == 0
            and result.get("mt_cache_size") == 0
            and isinstance(mt_cache, dict)
            and mt_cache.get("size") == 0
            and isinstance(final_stats, dict)
            and final_stats.get("mt_cache_size") == 0
        )
    else:
        cache_mode_matches = (
            arguments.get("mt_cache_size") is None
            and result.get("mt_cache_size") is None
            and isinstance(mt_cache, dict)
            and _at_least(mt_cache.get("size"), 1)
            and isinstance(final_stats, dict)
            and _at_least(final_stats.get("mt_cache_size"), 1)
        )

    checks = {
        "measurement_and_drain_complete": measurement_complete,
        "ten_students_in_english_and_chinese": population_complete,
        "continuous_resource_coverage": _coverage_ok(result, target_seconds),
        "no_crash_or_error": result.get("crashes") == 0 and errors == [],
        "standard_load_without_disconnect_injection": (
            arguments.get("disconnect_every_s") is None
            and arguments.get("partial") is None
            and result.get("disconnect_every_s") is None
            and result.get("disconnects") == 0
            and result.get("reconnect_failures") == 0
            and isinstance(restore, dict)
            and restore.get("injection_attempts") == 0
            and restore.get("injected_disconnects") == 0
        ),
        "real_asr_and_mt_work_observed": (
            isinstance(calls, dict)
            and _at_least(calls.get("asr"), 1)
            and _at_least(calls.get("mt"), 1)
            and _at_least(result.get("captions"), 1)
        ),
        "cache_mode_effective": cache_mode_matches,
        "report_arguments_match_manifest": (
            _same_number(result.get("minutes"), arguments.get("minutes"))
            and students == arguments.get("students")
            and result.get("corpus") == arguments.get("corpus")
            and result.get("disconnect_every_s")
            == arguments.get("disconnect_every_s")
            and result.get("mt_cache_size") == arguments.get("mt_cache_size")
            and (
                arguments.get("langs") is None
                or result.get("langs") == arguments.get("langs")
            )
        ),
        "effective_config_matches_manifest": (
            len(effective_configs) == 1
            and result.get("effective_config") in effective_configs
        ),
    }
    if cache_mode == "on":
        checks.update(
            {
                "rss_within_5000_mb": peak_rss is not None
                and 0 <= peak_rss <= 5000,
                "memory_not_increasing": isinstance(memory, dict)
                and memory.get("increasing") is False,
            }
        )
    else:
        checks["cpu_observed"] = (
            cpu_median is not None
            and cpu_median >= 0
            and isinstance(cpu, dict)
            and _at_least(cpu.get("samples"), 1)
        )
    metrics = {
        "audio_sha256": audio_sha256,
        "measurement_seconds": measurement,
        "peak_rss_mb": peak_rss,
        "cpu_median_percent": cpu_median,
        "measurement_langs": measurement_langs,
        "crashes": result.get("crashes"),
        "errors": errors,
        "acceptance_verdict": verdict,
        "latency_median_seconds": (
            latency.get("median_s") if isinstance(latency, dict) else None
        ),
        "latency_max_seconds": (
            latency.get("max_s") if isinstance(latency, dict) else None
        ),
    }
    return checks, metrics


def validate_run(run_dir: Path, *, cache_mode: str, audio_sha256: str) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    run_dir.relative_to(ROOT)
    manifest_path = run_dir / MANIFEST_NAME
    initial_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(initial_manifest, dict):
        raise PerformanceEvidenceError("manifestのrootがobjectではありません")
    primary_relative = initial_manifest.get("primary_report")
    if not isinstance(primary_relative, str):
        raise PerformanceEvidenceError("manifestにprimary_reportがありません")
    primary_report = (run_dir / primary_relative).resolve()
    primary_report.relative_to(run_dir)
    validated_manifest = validate_performance_manifest(
        run_dir, kind="soak", primary_report=primary_report, root=ROOT
    )
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    try:
        current_manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PerformanceEvidenceError("検証直後のmanifestを読めません") from exc
    if current_manifest != validated_manifest:
        raise PerformanceEvidenceError("検証中にmanifestが変更されました")
    report, report_sha256 = _load_sealed_report(
        primary_report, validated_manifest, run_dir
    )
    results = report.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise PerformanceEvidenceError("固定ASR/MTの単一結果ではありません")
    result = results[0]
    if not isinstance(result, dict):
        raise PerformanceEvidenceError("固定ASR/MTの結果がobjectではありません")

    arguments = validated_manifest.get("arguments")
    if not isinstance(arguments, dict):
        raise PerformanceEvidenceError("manifest argumentsがobjectではありません")
    normalized_audio_sha = audio_sha256.strip().lower()
    host_fingerprint_sha256 = _host_fingerprint_sha256()
    input_hashes = validated_manifest.get("input_hashes")
    configuration = validated_manifest.get("configuration")
    effective = configuration.get("effective") if isinstance(configuration, dict) else None
    effective_configs = set(effective) if isinstance(effective, dict) else set()
    checks, metrics = _validate_result(
        result,
        arguments=arguments,
        effective_configs=effective_configs,
        cache_mode=cache_mode,
        audio_sha256=normalized_audio_sha,
    )
    hardware = _collect_windows_hardware()
    hardware_collected_at = _hardware_collected_at()
    system_checks, system, hardware = _validated_system(
        report.get("system"),
        hardware=hardware,
        started_at=validated_manifest.get("started_at"),
    )
    checks.update(system_checks)
    checks.update(
        {
            "hardware_collected_immediately_after_run": _hardware_collection_timing_ok(
                finished_at=validated_manifest.get("finished_at"),
                collected_at=hardware_collected_at,
            ),
            "manifest_complete_and_current": True,
            "arguments_at_least_45_minutes": _number(arguments.get("minutes"))
            is not None
            and float(arguments["minutes"]) >= 45,
            "arguments_ten_students": arguments.get("students") == 10,
            "arguments_out_dir_matches_run": (
                isinstance(arguments.get("out_dir"), str)
                and (
                    Path(arguments["out_dir"]).resolve()
                    if Path(arguments["out_dir"]).is_absolute()
                    else (ROOT / arguments["out_dir"]).resolve()
                )
                == run_dir
            ),
            "run_dir_binds_validation_host": run_dir.name.endswith(
                f"-{host_fingerprint_sha256[:16]}-school-cache-{cache_mode}"
            ),
            "real_audio_input": isinstance(arguments.get("audio"), str)
            and bool(arguments["audio"].strip()),
            "real_asr": arguments.get("asr") in REAL_ASR_ENGINES,
            "real_mt": arguments.get("engine") in REAL_MT_ENGINES,
            "no_config_update": arguments.get("no_config_update") is True,
            "report_engines_match_arguments": result.get("engine")
            == arguments.get("engine")
            and result.get("asr_engine") == arguments.get("asr"),
            "audio_hash_matches": len(normalized_audio_sha) == 64
            and isinstance(input_hashes, dict)
            and list(input_hashes.values()) == [normalized_audio_sha],
            "primary_report_hash_matches": validated_manifest.get("artifacts", {}).get(
                primary_relative
            )
            == _sha256(primary_report),
        }
    )
    passed = all(checks.values())
    source_snapshot = json.dumps(
        validated_manifest.get("source_hashes"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    runtime_snapshot = json.dumps(
        {
            "runtime": validated_manifest.get("runtime"),
            "runtime_asset_hashes": validated_manifest.get("runtime_asset_hashes"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    system_snapshot = json.dumps(
        {
            key: system.get(key)
            for key in ("cpu", "cores", "threads", "ram_gb", "os", "python")
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    hardware_snapshot = json.dumps(
        hardware, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "schema_version": FIELD_VALIDATION_SCHEMA,
        "kind": "field-performance-validation",
        "passed": passed,
        "cache_mode": cache_mode,
        "run_dir": run_dir.relative_to(ROOT).as_posix(),
        "manifest": manifest_path.relative_to(ROOT).as_posix(),
        "primary_report": primary_report.relative_to(ROOT).as_posix(),
        "validator_sha256": _sha256(Path(__file__)),
        "source_snapshot_sha256": hashlib.sha256(source_snapshot).hexdigest(),
        "runtime_snapshot_sha256": hashlib.sha256(runtime_snapshot).hexdigest(),
        "system_snapshot_sha256": hashlib.sha256(system_snapshot).hexdigest(),
        "hardware_snapshot_sha256": hashlib.sha256(hardware_snapshot).hexdigest(),
        "host_fingerprint_sha256": host_fingerprint_sha256,
        "hardware_collected_at": hardware_collected_at,
        "started_at": validated_manifest.get("started_at"),
        "finished_at": validated_manifest.get("finished_at"),
        "manifest_sha256": manifest_sha256,
        "primary_report_sha256": report_sha256,
        "system": system,
        "hardware": hardware,
        "binding": {
            "minutes": arguments.get("minutes"),
            "students": arguments.get("students"),
            "audio_sha256": normalized_audio_sha,
            "corpus": arguments.get("corpus"),
            "asr": arguments.get("asr"),
            "engine": arguments.get("engine"),
            "langs": result.get("langs"),
            "disconnect_every_s": arguments.get("disconnect_every_s"),
            "partial": arguments.get("partial"),
            "drain_seconds": arguments.get("drain_seconds"),
            "no_config_update": arguments.get("no_config_update"),
            "effective_config": result.get("effective_config"),
        },
        "checks": checks,
        "metrics": metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--cache-mode", choices=("on", "off"), required=True)
    parser.add_argument("--audio-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    expected_output = args.run_dir.resolve() / "field-validation.json"
    if args.output.resolve() != expected_output:
        parser.error("--output は <run_dir>/field-validation.json 固定です")
    try:
        payload = validate_run(
            args.run_dir, cache_mode=args.cache_mode, audio_sha256=args.audio_sha256
        )
    except (
        OSError,
        OverflowError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        PerformanceEvidenceError,
    ) as exc:
        payload = {
            "schema_version": FIELD_VALIDATION_SCHEMA,
            "kind": "field-performance-validation",
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("passed") is True else 1


if __name__ == "__main__":
    sys.exit(main())
