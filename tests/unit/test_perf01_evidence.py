from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import wave

import pytest

from scripts import perf01_evidence as perf


FIELD_DATE = "2026-09-08"
BASE = dt.datetime(2026, 9, 8, 9, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
FIELD_SESSION_ID = "b" * 64
HOST_FINGERPRINT_SHA256 = "c" * 64
SOURCE_HASHES = {"scripts/perf01_evidence.py": "d" * 64}
SOURCE_REVISIONS = {
    "files": {
        "scripts/perf01_evidence.py": {
            "dev": 1,
            "ino": 2,
            "size": 3,
            "mtime_ns": 4,
            "ctime_ns": 5,
        }
    },
    "directories": {
        "scripts": {
            "dev": 1,
            "ino": 6,
            "size": 7,
            "mtime_ns": 8,
            "ctime_ns": 9,
        }
    },
}


def _run_perf01_with_a_stale_qa_helper_pyc(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "stale-pyc-repository"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(Path(perf.__file__), scripts / "perf01_evidence.py")
    helper = scripts / "qa_evidence_manifest.py"
    good = Path(perf.qa_evidence_manifest.__file__).read_bytes()
    needle = b"from typing import Any, Callable, Iterable, cast"
    poison = b'raise RuntimeError("STALE PYC EXECUTED")'
    assert needle in good and len(poison) <= len(needle)
    bad = good.replace(needle, poison + b" " * (len(needle) - len(poison)), 1)
    assert len(bad) == len(good)
    fixed_ns = 1_700_000_000_000_000_000
    helper.write_bytes(bad)
    os.utime(helper, ns=(fixed_ns, fixed_ns))
    cache = helper.parent / "__pycache__" / (
        f"{helper.stem}.{sys.implementation.cache_tag}.pyc"
    )
    cache.parent.mkdir()
    py_compile.compile(str(helper), cfile=str(cache), doraise=True)
    helper.write_bytes(good)
    os.utime(helper, ns=(fixed_ns, fixed_ns))
    environment = os.environ.copy()
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    return subprocess.run(
        [sys.executable, str(scripts / "perf01_evidence.py"), "--help"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_standalone_import_ignores_a_same_size_stale_qa_helper_pyc(
    tmp_path: Path,
) -> None:
    completed = _run_perf01_with_a_stale_qa_helper_pyc(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert "STALE PYC EXECUTED" not in completed.stderr


def test_import_bootstrap_uses_an_empty_private_cache_and_pre_post_binding() -> None:
    cache = Path(perf._IMPORT_PYCACHE.name)
    assert perf._IMPORT_PYCACHE_WAS_EMPTY is True
    assert cache.is_dir()
    expected_hashes = {
        path.relative_to(perf._IMPORT_ROOT).as_posix(): state[0]
        for path, state in perf._EARLY_IMPORTED_VALIDATOR_STATE.items()
    }
    expected_revisions = {
        path.relative_to(perf._IMPORT_ROOT).as_posix(): state[1]
        for path, state in perf._EARLY_IMPORTED_VALIDATOR_STATE.items()
    }
    assert dict(perf._IMPORTED_VALIDATOR_SOURCE_SNAPSHOT) == expected_hashes
    assert dict(perf._IMPORTED_VALIDATOR_SOURCE_SNAPSHOT.revisions) == expected_revisions
    source = Path(perf.__file__).read_text(encoding="utf-8")
    assert source.index("sys.pycache_prefix = _IMPORT_PYCACHE.name") < source.index(
        "from scripts import qa_evidence_manifest"
    )
SOURCE_SNAPSHOT_SHA256 = perf._source_snapshot_sha256(
    SOURCE_HASHES, SOURCE_REVISIONS
)


def _stamp(seconds: Decimal | int | str) -> str:
    value = Decimal(seconds)
    microseconds = int(value * Decimal(1_000_000))
    moment = BASE + dt.timedelta(microseconds=microseconds)
    return moment.isoformat(timespec="microseconds" if microseconds % 1_000_000 else "seconds")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n" for value in values),
        encoding="utf-8",
        newline="\n",
    )


def _write_tsv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(perf.TSV_HEADER)
        writer.writerows(rows)


def _reference(path: Path, root: Path) -> dict[str, str]:
    return {"path": path.relative_to(root).as_posix(), "sha256": _sha(path)}


def _write_pcm_wav(path: Path, duration_seconds: Decimal) -> None:
    """Represent a long PCM recording with a safe one-frame-per-second fixture."""
    frames = int(duration_seconds.to_integral_value(rounding=ROUND_CEILING))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(1)
        handle.writeframes(b"\x80" * frames)


def _make_fixture(
    tmp_path: Path,
    *,
    final_count: int = 2,
    first_seq: int = 1,
    latencies: list[Decimal] | None = None,
    audio_seconds: Decimal = Decimal("2700"),
) -> dict:
    root = tmp_path / "repo"
    evidence = root / "docs" / "field" / "evidence" / FIELD_DATE
    evidence.mkdir(parents=True)

    source_snapshot_path = evidence / perf.SOURCE_SNAPSHOT_FILENAME
    _write_json(
        source_snapshot_path,
        {
            "schema_version": perf.SOURCE_SNAPSHOT_SCHEMA_VERSION,
            "kind": perf.SOURCE_SNAPSHOT_KIND,
            "field_date": FIELD_DATE,
            "phase": "start",
            "captured_at": _stamp(-1),
            "host_fingerprint_sha256": HOST_FINGERPRINT_SHA256,
            "session_id": FIELD_SESSION_ID,
            "predecessor_sha256": None,
            "source_hashes": SOURCE_HASHES,
            "source_revisions": SOURCE_REVISIONS,
            "source_snapshot_sha256": SOURCE_SNAPSHOT_SHA256,
        },
    )

    input_path = evidence / "lesson.wav"
    recording_path = evidence / "screen-recording.mp4"
    commit_path = evidence / "commit-status.txt"
    config_path = evidence / "config-models.json"
    runtime_path = evidence / "runtime-browsers.json"
    finals_path = evidence / "transcript.jsonl"
    measurements_path = evidence / "perf01-latency.tsv"
    _write_pcm_wav(input_path, audio_seconds)
    recording_path.write_bytes(b"physical-browser-screen-recording")
    commit_path.write_text("commit deadbeef\nstatus clean\n", encoding="utf-8")
    _write_json(config_path, {"config_sha256": "a" * 64, "models": ["asr", "mt"]})
    _write_json(runtime_path, {"school_pc": "Windows 11", "browsers": ["Chrome", "Safari"]})

    finals = []
    for ordinal in range(final_count):
        seq = first_seq + ordinal
        finals.append(
            {
                "seq": seq,
                "created_at": _stamp((ordinal + 1) * 20 + 1),
                "t_start": ordinal * 20,
                "t_end": (ordinal + 1) * 20,
                "ja": f"発話 {seq}",
                "asr_ms": 400,
                "translations": {
                    "en": {"text": f"English {seq}", "engine": "hy-mt2", "mt_ms": 500},
                    "zh": {"text": f"中文 {seq}", "engine": "hy-mt2", "mt_ms": 600},
                },
            }
        )
    _write_jsonl(finals_path, finals)

    if latencies is None:
        latencies = [Decimal("2"), Decimal("5"), Decimal("5"), Decimal("8")]
        if final_count != 2:
            latencies = [Decimal("2") if index % 2 == 0 else Decimal("3") for index in range(final_count * 2)]
    assert len(latencies) == final_count * 2
    run_id = "perf01-physical-001"
    rows: list[list[str]] = []
    for ordinal in range(final_count):
        seq = first_seq + ordinal
        utterance_end_seconds = Decimal((ordinal + 1) * 20)
        utterance_end = _stamp(utterance_end_seconds)
        for language_index, language in enumerate(perf.LANGUAGES):
            latency = latencies[ordinal * 2 + language_index]
            rows.append(
                [
                    run_id,
                    str(seq),
                    language,
                    "device-en" if language == "en" else "device-zh",
                    utterance_end,
                    _stamp(utterance_end_seconds + latency),
                    perf._canonical_decimal(latency),
                ]
            )
    _write_tsv(measurements_path, rows)

    devices = [
        {
            "device_id": "device-en" if index == 0 else ("device-zh" if index == 5 else f"device-{index}"),
            "lang": "en" if index < 5 else "zh",
            "representative": index in {0, 5},
            "model": "Chromebook" if index < 9 else "iPhone",
            "os": "ChromeOS 140" if index < 9 else "iOS 20",
            "browser": "Chrome 140" if index < 9 else "Safari 20",
        }
        for index in range(10)
    ]
    refs = {
        "input": _reference(input_path, root),
        "measurements": _reference(measurements_path, root),
        "finals": _reference(finals_path, root),
        "recording": _reference(recording_path, root),
        "commit": _reference(commit_path, root),
        "config": _reference(config_path, root),
        "runtime": _reference(runtime_path, root),
    }
    payload = {
        "schema_version": perf.RUN_SCHEMA_VERSION,
        "kind": "perf01-field-run",
        "field_date": FIELD_DATE,
        "field_session_id": FIELD_SESSION_ID,
        "host_fingerprint_sha256": HOST_FINGERPRINT_SHA256,
        "source_snapshot_sha256": SOURCE_SNAPSHOT_SHA256,
        "run_id": run_id,
        "clock_id": "school-pc-clock-and-video-overlay-v1",
        "measurement_window": {
            "started_at": _stamp(0),
            "audio_ended_at": _stamp(audio_seconds),
            "observation_ended_at": _stamp(audio_seconds + Decimal("10")),
        },
        "population": {
            "observed_from_at": _stamp(0),
            "observed_until_at": _stamp(audio_seconds + Decimal("10")),
            "teacher_reported_students": 10,
            "devices": devices,
        },
        "input": {
            "kind": "実授業WAV",
            "description": "学校PCから実時間再生した45分の実授業録音",
            "evidence": refs["input"],
        },
        "artifacts": {
            "measurements_tsv": refs["measurements"],
            "finals_jsonl": refs["finals"],
            "recordings": [refs["recording"]],
            "commit_status": refs["commit"],
            "config_models": refs["config"],
            "runtime_browsers": refs["runtime"],
        },
    }
    run_path = evidence / "perf01-run.json"
    _write_json(run_path, payload)
    return {
        "root": root,
        "evidence": evidence,
        "run_path": run_path,
        "payload": payload,
        "refs": refs,
        "paths": {
            "source_snapshot": source_snapshot_path,
            "input": input_path,
            "measurements": measurements_path,
            "finals": finals_path,
            "recording": recording_path,
            "commit": commit_path,
            "config": config_path,
            "runtime": runtime_path,
        },
        "finals": finals,
        "rows": rows,
    }


def _save_run(fixture: dict) -> None:
    _write_json(fixture["run_path"], fixture["payload"])


def _refresh(fixture: dict, name: str) -> None:
    fixture["refs"][name]["sha256"] = _sha(fixture["paths"][name])
    _save_run(fixture)


def test_complete_run_passes_with_exact_deterministic_contract(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)

    first = perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])
    second = perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])

    assert first == second
    assert set(first) == perf.VALIDATION_KEYS
    assert first["passed"] is True
    assert first["metrics"] == {
        "final_count": 2,
        "expected_rows": 4,
        "matched_rows": 4,
        "latency_median_seconds": "5",
        "latency_max_seconds": "8",
    }
    assert first["binding"] == {
        "field_date": FIELD_DATE,
        "field_session_id": FIELD_SESSION_ID,
        "host_fingerprint_sha256": HOST_FINGERPRINT_SHA256,
        "source_snapshot_sha256": SOURCE_SNAPSHOT_SHA256,
        "source_snapshot_captured_at": _stamp(-1),
        "source_snapshot_artifact_sha256": _sha(
            fixture["evidence"] / perf.SOURCE_SNAPSHOT_FILENAME
        ),
        "run_id": "perf01-physical-001",
        "clock_id": "school-pc-clock-and-video-overlay-v1",
        "measurement_seconds": "2700",
        "student_count": 10,
        "language_counts": {"en": 5, "zh": 5},
        "representatives": {"en": "device-en", "zh": "device-zh"},
        "first_seq": 1,
        "last_seq": 2,
    }
    assert len(first["artifact_hashes"]) == 8
    assert tuple(first["checks"]) == perf.CHECK_KEYS
    assert first["scope"] == {
        "table_structure_arithmetic_and_hashes_checked": True,
        "input_pcm_wav_format_and_duration_checked": True,
        "recording_duration_checked": False,
        "physical_device_identity_checked": False,
        "simultaneous_connection_checked": False,
        "video_annotation_accuracy_checked": False,
        "same_clock_provenance_checked": False,
        "real_lesson_provenance_checked": False,
        "directory_manifest_checked": False,
        "directory_manifest_owner": "scripts.field_story_evidence.validate_outer_manifest",
    }
    assert first["passed"] is all(first["checks"].values())


def test_validation_witness_rejects_same_size_artifact_aba_with_restored_mtime(
    tmp_path: Path,
) -> None:
    fixture = _make_fixture(tmp_path)
    result = perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])
    assert isinstance(result, perf.ValidatedPerf01Result)
    target = fixture["paths"]["commit"]
    before = target.stat()
    original = target.read_bytes()
    replacement = bytes(byte ^ 1 for byte in original)
    target.write_bytes(replacement)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    target.write_bytes(original)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    with pytest.raises(perf.Perf01EvidenceError, match="changed"):
        perf.recheck_perf01_validation_witness(result.validation_witness)


def test_exact_latency_boundaries_median_5_and_max_8_pass(tmp_path: Path) -> None:
    fixture = _make_fixture(
        tmp_path,
        latencies=[Decimal("2"), Decimal("5"), Decimal("5"), Decimal("8")],
    )
    result = perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])
    assert result["metrics"]["latency_median_seconds"] == "5"
    assert result["metrics"]["latency_max_seconds"] == "8"
    assert result["passed"] is True


def test_positive_non_one_first_seq_is_preserved_through_tsv_and_binding(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path, first_seq=41)

    result = perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])

    assert [final["seq"] for final in fixture["finals"]] == [41, 42]
    assert [row[1] for row in fixture["rows"]] == ["41", "41", "42", "42"]
    assert result["binding"]["first_seq"] == 41
    assert result["binding"]["last_seq"] == 42
    assert result["passed"] is True


def test_unrounded_median_above_5_fails_without_becoming_structural_error(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path, latencies=[Decimal("5.000001")] * 4)
    result = perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])
    assert result["passed"] is False
    assert result["metrics"]["latency_median_seconds"] == "5.000001"
    assert result["checks"]["median_at_most_5_seconds"] is False
    assert result["checks"]["max_at_most_8_seconds"] is True


def test_unrounded_max_above_8_fails(tmp_path: Path) -> None:
    fixture = _make_fixture(
        tmp_path,
        latencies=[Decimal("1"), Decimal("1"), Decimal("1"), Decimal("8.000001")],
    )
    result = perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])
    assert result["passed"] is False
    assert result["metrics"]["latency_max_seconds"] == "8.000001"
    assert result["checks"]["max_at_most_8_seconds"] is False


def test_44_minutes_59_999_seconds_fails_duration_check(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path, audio_seconds=Decimal("2699.999"))
    result = perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])
    assert result["binding"]["measurement_seconds"] == "2699.999"
    assert result["checks"]["measurement_at_least_45_minutes"] is False
    assert result["passed"] is False


def test_missing_one_of_2n_measurement_rows_is_rejected(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["rows"].pop()
    _write_tsv(fixture["paths"]["measurements"], fixture["rows"])
    _refresh(fixture, "measurements")
    with pytest.raises(perf.Perf01EvidenceError, match="all 2N rows"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_thirty_row_extract_from_31_finals_cannot_substitute_for_all_finals(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path, final_count=31)
    fixture["rows"] = fixture["rows"][:60]
    _write_tsv(fixture["paths"]["measurements"], fixture["rows"])
    _refresh(fixture, "measurements")
    with pytest.raises(perf.Perf01EvidenceError, match=r"62.*got 60"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_dom_timestamp_change_is_rejected_even_when_artifact_hash_is_updated(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["rows"][0][5] = _stamp(Decimal("22.000001"))
    _write_tsv(fixture["paths"]["measurements"], fixture["rows"])
    _refresh(fixture, "measurements")
    with pytest.raises(perf.Perf01EvidenceError, match="latency mismatch"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_supplied_decimal_change_is_rejected_without_rounding(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["rows"][0][6] = "2.000001"
    _write_tsv(fixture["paths"]["measurements"], fixture["rows"])
    _refresh(fixture, "measurements")
    with pytest.raises(perf.Perf01EvidenceError, match="latency mismatch"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


@pytest.mark.parametrize("device_count", [9, 11])
def test_population_requires_exactly_ten_devices(tmp_path: Path, device_count: int) -> None:
    fixture = _make_fixture(tmp_path)
    devices = fixture["payload"]["population"]["devices"]
    if device_count == 9:
        devices.pop(4)
    else:
        devices.append(
            {
                "device_id": "device-extra",
                "lang": "en",
                "representative": False,
                "model": "Chromebook",
                "os": "ChromeOS 140",
                "browser": "Chrome 140",
            }
        )
    _save_run(fixture)
    with pytest.raises(perf.Perf01EvidenceError, match="exactly 10 devices"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "integer"])
def test_representatives_must_be_exact_boolean_one_per_language(tmp_path: Path, mutation: str) -> None:
    fixture = _make_fixture(tmp_path)
    devices = fixture["payload"]["population"]["devices"]
    if mutation == "missing":
        devices[0]["representative"] = False
    elif mutation == "duplicate":
        devices[1]["representative"] = True
    else:
        devices[0]["representative"] = 1
    _save_run(fixture)
    with pytest.raises(perf.Perf01EvidenceError, match="representative"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_both_languages_must_be_present(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    for index, device in enumerate(fixture["payload"]["population"]["devices"]):
        device["lang"] = "en"
        device["representative"] = index == 0
    _save_run(fixture)
    with pytest.raises(perf.Perf01EvidenceError, match="both en and zh"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_population_interval_must_exactly_match_measurement_window(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["payload"]["population"]["observed_from_at"] = _stamp(Decimal("0.001"))
    _save_run(fixture)
    with pytest.raises(perf.Perf01EvidenceError, match="complete window"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_transcript_seq_must_be_contiguous_and_complete(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["finals"][1]["seq"] = 3
    _write_jsonl(fixture["paths"]["finals"], fixture["finals"])
    _refresh(fixture, "finals")
    with pytest.raises(perf.Perf01EvidenceError, match="contiguous"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_every_final_requires_nonempty_en_and_zh_translations(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["finals"][0]["translations"]["zh"]["text"] = ""
    _write_jsonl(fixture["paths"]["finals"], fixture["finals"])
    _refresh(fixture, "finals")
    with pytest.raises(perf.Perf01EvidenceError, match="non-empty"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_tsv_order_is_seq_then_en_then_zh(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["rows"][0], fixture["rows"][1] = fixture["rows"][1], fixture["rows"][0]
    _write_tsv(fixture["paths"]["measurements"], fixture["rows"])
    _refresh(fixture, "measurements")
    with pytest.raises(perf.Perf01EvidenceError, match="en then zh"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_tsv_header_is_exact(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    with fixture["paths"]["measurements"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow([*perf.TSV_HEADER[:-1], "latency_s"])
        writer.writerows(fixture["rows"])
    _refresh(fixture, "measurements")
    with pytest.raises(perf.Perf01EvidenceError, match="header mismatch"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_transcript_created_at_requires_timezone_and_window_membership(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["finals"][0]["created_at"] = "2026-09-08T09:00:25"
    _write_jsonl(fixture["paths"]["finals"], fixture["finals"])
    _refresh(fixture, "finals")
    with pytest.raises(perf.Perf01EvidenceError, match="timezone-aware"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])

    fixture = _make_fixture(tmp_path / "outside")
    fixture["finals"][0]["created_at"] = _stamp(Decimal("2711"))
    _write_jsonl(fixture["paths"]["finals"], fixture["finals"])
    _refresh(fixture, "finals")
    with pytest.raises(perf.Perf01EvidenceError, match="outside the observation window"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_each_final_created_at_is_between_utterance_end_and_both_dom_displays(
    tmp_path: Path,
) -> None:
    fixture = _make_fixture(tmp_path)
    # seq 1 displays at +2s (en) and +5s (zh); +3s is therefore too late for en.
    fixture["finals"][0]["created_at"] = _stamp(Decimal("23"))
    _write_jsonl(fixture["paths"]["finals"], fixture["finals"])
    _refresh(fixture, "finals")
    with pytest.raises(perf.Perf01EvidenceError, match="between utterance_end_at and both DOM"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_exact_run_schema_rejects_unknown_keys(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["payload"]["unexpected"] = True
    _save_run(fixture)
    with pytest.raises(perf.Perf01EvidenceError, match="schema mismatch"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_nested_schema_rejects_unknown_keys(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["payload"]["artifacts"]["measurements_tsv"]["size"] = 123
    _save_run(fixture)
    with pytest.raises(perf.Perf01EvidenceError, match="schema mismatch"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    raw = fixture["run_path"].read_text(encoding="utf-8")
    raw = raw.replace('  "kind": "perf01-field-run",', '  "kind": "perf01-field-run",\n  "kind": "perf01-field-run",', 1)
    fixture["run_path"].write_text(raw, encoding="utf-8")
    with pytest.raises(perf.Perf01EvidenceError, match="duplicate JSON key"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_nan_and_bool_as_integer_are_rejected(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    raw = fixture["run_path"].read_text(encoding="utf-8").replace(
        '"teacher_reported_students": 10', '"teacher_reported_students": NaN'
    )
    fixture["run_path"].write_text(raw, encoding="utf-8")
    with pytest.raises(perf.Perf01EvidenceError, match="non-finite"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])

    fixture = _make_fixture(tmp_path / "bool")
    fixture["payload"]["population"]["teacher_reported_students"] = True
    _save_run(fixture)
    with pytest.raises(perf.Perf01EvidenceError, match="integer"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


@pytest.mark.parametrize(
    "bad_path",
    [
        "docs/field/evidence/2026-09-08/../outside.tsv",
        "docs\\field\\evidence\\2026-09-08\\perf01-latency.tsv",
        "C:/outside/perf01-latency.tsv",
    ],
)
def test_references_must_be_canonical_posix_paths_inside_field_date(tmp_path: Path, bad_path: str) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["refs"]["measurements"]["path"] = bad_path
    _save_run(fixture)
    with pytest.raises(perf.Perf01EvidenceError, match="canonical|POSIX"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_run_json_itself_must_be_in_field_date_directory(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    moved = fixture["root"] / "perf01-run.json"
    moved.write_bytes(fixture["run_path"].read_bytes())
    with pytest.raises(perf.Perf01EvidenceError, match="field-date directory"):
        perf.validate_perf01_run(moved, root=fixture["root"])


def test_symlinked_artifact_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _make_fixture(tmp_path)
    target = fixture["paths"]["measurements"]
    link = fixture["evidence"] / "linked-measurements.tsv"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        # Windows commonly reserves symlink creation for Developer Mode or an
        # elevated token.  Exercise the same guard deterministically there.
        link.write_bytes(target.read_bytes())
        real_check = perf._is_link_or_junction

        def simulated_link(path: Path) -> bool:
            return Path(path) == link or real_check(path)

        monkeypatch.setattr(perf, "_is_link_or_junction", simulated_link)
    fixture["refs"]["measurements"]["path"] = link.relative_to(fixture["root"]).as_posix()
    fixture["refs"]["measurements"]["sha256"] = _sha(link)
    _save_run(fixture)
    with pytest.raises(perf.Perf01EvidenceError, match="links and junctions"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_artifact_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["paths"]["config"].write_bytes(b"tampered after run JSON was sealed")
    with pytest.raises(perf.Perf01EvidenceError, match="SHA-256 mismatch"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_hash_must_be_canonical_lowercase_and_artifact_nonempty(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["refs"]["config"]["sha256"] = fixture["refs"]["config"]["sha256"].upper()
    _save_run(fixture)
    with pytest.raises(perf.Perf01EvidenceError, match="canonical lowercase"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])

    fixture = _make_fixture(tmp_path / "empty")
    fixture["paths"]["commit"].write_bytes(b"")
    _refresh(fixture, "commit")
    with pytest.raises(perf.Perf01EvidenceError, match="non-empty file"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


@pytest.mark.parametrize(
    "field",
    ("field_session_id", "host_fingerprint_sha256", "source_snapshot_sha256"),
)
def test_run_binding_hashes_must_be_canonical_lowercase(
    tmp_path: Path, field: str
) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["payload"][field] = "A" * 64
    _save_run(fixture)

    with pytest.raises(perf.Perf01EvidenceError, match=field):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


@pytest.mark.parametrize(
    "field",
    ("field_session_id", "host_fingerprint_sha256", "source_snapshot_sha256"),
)
def test_run_binding_must_match_the_fixed_start_snapshot(
    tmp_path: Path, field: str
) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["payload"][field] = "e" * 64
    _save_run(fixture)

    with pytest.raises(
        perf.Perf01EvidenceError, match="does not match the fixed start source snapshot"
    ):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("unknown_key", "schema mismatch"),
        ("phase", "binding is invalid"),
        ("predecessor", "binding is invalid"),
        ("captured_date", "binding is invalid"),
        ("source_hashes", "binding is invalid"),
    ),
)
def test_fixed_start_snapshot_contract_is_validated_independently(
    tmp_path: Path, mutation: str, message: str
) -> None:
    fixture = _make_fixture(tmp_path)
    snapshot_path = fixture["paths"]["source_snapshot"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if mutation == "unknown_key":
        snapshot["unexpected"] = True
    elif mutation == "phase":
        snapshot["phase"] = "end"
    elif mutation == "predecessor":
        snapshot["predecessor_sha256"] = "e" * 64
    elif mutation == "captured_date":
        snapshot["captured_at"] = "2026-09-07T23:59:59+09:00"
    else:
        snapshot["source_hashes"]["scripts/perf01_evidence.py"] = "e" * 64
    _write_json(snapshot_path, snapshot)

    with pytest.raises(perf.Perf01EvidenceError, match=message):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_fixed_start_snapshot_rejects_legacy_schema_one(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    snapshot_path = fixture["paths"]["source_snapshot"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["schema_version"] = 1
    snapshot.pop("source_revisions")
    snapshot["source_snapshot_sha256"] = perf._canonical_sha256(
        snapshot["source_hashes"]
    )
    _write_json(snapshot_path, snapshot)

    with pytest.raises(perf.Perf01EvidenceError, match="schema mismatch"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


@pytest.mark.parametrize(
    "mutation", ("missing_group", "extra_component", "boolean_component", "wrong_files")
)
def test_fixed_start_snapshot_rejects_untrusted_source_revisions(
    tmp_path: Path, mutation: str
) -> None:
    fixture = _make_fixture(tmp_path)
    snapshot_path = fixture["paths"]["source_snapshot"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    revisions = snapshot["source_revisions"]
    file_path = next(iter(revisions["files"]))
    if mutation == "missing_group":
        revisions.pop("directories")
    elif mutation == "extra_component":
        revisions["files"][file_path]["generation"] = 1
    elif mutation == "boolean_component":
        revisions["files"][file_path]["ctime_ns"] = True
    else:
        revisions["files"].pop(file_path)
    _write_json(snapshot_path, snapshot)

    with pytest.raises(perf.Perf01EvidenceError, match="revision"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_fixed_start_snapshot_digest_binds_source_revision_values(
    tmp_path: Path,
) -> None:
    fixture = _make_fixture(tmp_path)
    snapshot_path = fixture["paths"]["source_snapshot"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    file_path = next(iter(snapshot["source_revisions"]["files"]))
    snapshot["source_revisions"]["files"][file_path]["mtime_ns"] += 1
    _write_json(snapshot_path, snapshot)

    with pytest.raises(perf.Perf01EvidenceError, match="binding is invalid"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_run_schema_v1_is_rejected(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["payload"]["schema_version"] = 1
    _save_run(fixture)

    with pytest.raises(perf.Perf01EvidenceError, match="schema_version"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


@pytest.mark.parametrize("mutation", ("text", "truncated", "short"))
def test_wav_input_must_be_complete_pcm_covering_the_measurement(
    tmp_path: Path, mutation: str
) -> None:
    fixture = _make_fixture(tmp_path)
    input_path = fixture["paths"]["input"]
    if mutation == "text":
        input_path.write_bytes(b"RIFF-looking but not a WAV")
    elif mutation == "truncated":
        input_path.write_bytes(input_path.read_bytes()[:-1])
    else:
        _write_pcm_wav(input_path, Decimal("2699"))
    _refresh(fixture, "input")

    with pytest.raises(
        perf.Perf01EvidenceError,
        match="PCM WAV|valid PCM WAV|PCM data is truncated|shorter than the measurement",
    ):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_live_input_is_exempt_from_pcm_parsing_and_keeps_manual_scope(
    tmp_path: Path,
) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["paths"]["input"].write_bytes(b"sealed live-lesson input attestation")
    fixture["payload"]["input"]["kind"] = perf.LIVE_INPUT_KIND
    _refresh(fixture, "input")

    result = perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])

    assert result["passed"] is True
    assert result["scope"]["input_pcm_wav_format_and_duration_checked"] is False
    assert result["scope"]["recording_duration_checked"] is False
    assert result["scope"]["physical_device_identity_checked"] is False
    assert result["scope"]["simultaneous_connection_checked"] is False
    assert result["scope"]["same_clock_provenance_checked"] is False
    assert result["scope"]["real_lesson_provenance_checked"] is False


def test_opaque_recording_is_stream_hashed_not_loaded_into_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _make_fixture(tmp_path)
    real_read = perf._read_bytes
    opaque = {fixture["paths"]["recording"].resolve()}

    def guarded_read(path: Path) -> bytes:
        if Path(path).resolve() in opaque:
            raise AssertionError("opaque media must not be loaded with read_bytes")
        return real_read(path)

    monkeypatch.setattr(perf, "_read_bytes", guarded_read)
    assert perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])["passed"] is True


def test_duplicate_artifact_path_is_rejected(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["payload"]["artifacts"]["runtime_browsers"] = dict(fixture["refs"]["config"])
    _save_run(fixture)
    with pytest.raises(perf.Perf01EvidenceError, match="duplicate referenced artifact"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


@pytest.mark.parametrize("alias_target", ("artifact", "run", "snapshot_run"))
def test_hardlink_alias_between_evidence_files_is_rejected(
    tmp_path: Path, alias_target: str
) -> None:
    fixture = _make_fixture(tmp_path)
    if alias_target == "artifact":
        target = fixture["paths"]["runtime"]
        source = fixture["paths"]["config"]
    elif alias_target == "run":
        target = fixture["paths"]["input"]
        source = fixture["run_path"]
    else:
        target = fixture["paths"]["source_snapshot"]
        source = fixture["run_path"]
    target.unlink()
    try:
        os.link(source, target)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")
    if alias_target == "artifact":
        fixture["refs"]["runtime"]["sha256"] = _sha(source)
        _save_run(fixture)

    with pytest.raises(perf.Perf01EvidenceError, match="hardlink.alias"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_stream_hashed_opaque_artifact_change_during_validation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _make_fixture(tmp_path)
    target = fixture["paths"]["recording"].resolve()
    real_hash = perf._sha256_file
    reads = 0

    def changing_hash(path: Path) -> str:
        nonlocal reads
        digest = real_hash(path)
        if Path(path).resolve() == target:
            reads += 1
            if reads >= 2:
                return "0" * 64
        return digest

    monkeypatch.setattr(perf, "_sha256_file", changing_hash)
    with pytest.raises(perf.Perf01EvidenceError, match="changed during validation"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


@pytest.mark.parametrize(
    "changed_name", ["run", "source_snapshot", "measurements", "validator"]
)
def test_byte_snapshot_change_during_validation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_name: str
) -> None:
    fixture = _make_fixture(tmp_path)
    if changed_name == "run":
        target = fixture["run_path"]
    elif changed_name == "source_snapshot":
        target = fixture["paths"]["source_snapshot"]
    elif changed_name == "validator":
        target = Path(perf.__file__).resolve()
    else:
        target = fixture["paths"]["measurements"]
    real_read = perf._read_bytes
    reads = 0

    def changing_read(path: Path) -> bytes:
        nonlocal reads
        data = real_read(path)
        if Path(path).resolve() == target.resolve():
            reads += 1
            if reads >= 2:
                return data + b"concurrent-change"
        return data

    monkeypatch.setattr(perf, "_read_bytes", changing_read)
    with pytest.raises(perf.Perf01EvidenceError, match="changed during validation"):
        perf.validate_perf01_run(fixture["run_path"], root=fixture["root"])


def test_cli_writes_atomic_json_and_shell_has_exactly_ten_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _make_fixture(tmp_path)
    output = fixture["evidence"] / f"{FIELD_DATE}-perf01-validation.json"
    replaces: list[tuple[Path, Path]] = []
    real_replace = perf.os.replace

    def recording_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        replaces.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(perf, "ROOT", fixture["root"])
    monkeypatch.setattr(perf.os, "replace", recording_replace)
    exit_code = perf.main(
        ["validate", str(fixture["run_path"]), "--output", str(output), "--format", "shell"]
    )
    captured = capsys.readouterr()
    first_bytes = output.read_bytes()

    assert exit_code == 0
    assert captured.err == ""
    assert len(captured.out.rstrip("\n").split("\t")) == 10
    assert captured.out.startswith("true\t2\t4\t4\t2700\t10\t5\t5\t5\t8")
    assert json.loads(first_bytes)["passed"] is True
    assert replaces and replaces[0][1] == output
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))

    assert perf.main(
        ["validate", str(fixture["run_path"]), "--output", str(output), "--format", "json"]
    ) == 0
    capsys.readouterr()
    assert output.read_bytes() == first_bytes


def test_cli_rechecks_the_exact_read_set_immediately_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _make_fixture(tmp_path)
    output = fixture["evidence"] / f"{FIELD_DATE}-perf01-validation.json"
    target = fixture["paths"]["commit"]
    real_atomic_write = perf._atomic_write

    def tampering_atomic_write(
        path: Path,
        content: str,
        *,
        before_replace=None,
    ) -> None:
        before = target.stat()
        original = target.read_bytes()
        target.write_bytes(bytes(byte ^ 1 for byte in original))
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        target.write_bytes(original)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        real_atomic_write(path, content, before_replace=before_replace)

    monkeypatch.setattr(perf, "ROOT", fixture["root"])
    monkeypatch.setattr(perf, "_atomic_write", tampering_atomic_write)

    assert perf.main(
        ["validate", str(fixture["run_path"]), "--output", str(output)]
    ) == 2
    assert not output.exists()
    assert "changed" in capsys.readouterr().err


def test_cli_structural_failure_still_writes_exact_failed_validation_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _make_fixture(tmp_path)
    fixture["payload"]["unexpected"] = True
    _save_run(fixture)
    output = fixture["evidence"] / f"{FIELD_DATE}-perf01-validation.json"
    monkeypatch.setattr(perf, "ROOT", fixture["root"])

    exit_code = perf.main(
        ["validate", str(fixture["run_path"]), "--output", str(output), "--format", "shell"]
    )
    captured = capsys.readouterr()
    result = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert "PERF01_EVIDENCE_INVALID" in captured.err
    assert len(captured.out.rstrip("\n").split("\t")) == 10
    assert set(result) == perf.VALIDATION_KEYS
    assert result["passed"] is False
    assert result["passed"] is all(result["checks"].values())


def test_cli_refuses_to_overwrite_a_source_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _make_fixture(tmp_path)
    target = fixture["paths"]["measurements"]
    before = target.read_bytes()
    monkeypatch.setattr(perf, "ROOT", fixture["root"])
    assert perf.main(
        ["validate", str(fixture["run_path"]), "--output", str(target), "--format", "shell"]
    ) == 2
    assert target.read_bytes() == before
