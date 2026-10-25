"""Field-story sidecars remain replayable, sealed, and fail closed."""

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
from typing import Any, Callable
import wave

import pytest

import scripts.field_story_evidence as field_evidence
import scripts.perf01_evidence as perf01_evidence
import scripts.validate_field_performance as field_validation


FIELD_DATE = "2026-09-08"
EVIDENCE_RELATIVE = f"docs/field/evidence/{FIELD_DATE}"
MANIFEST_RELATIVE = f"docs/field/{FIELD_DATE}-evidence-sha256.txt"


def _run_field_with_a_stale_qa_helper_pyc(
    tmp_path: Path,
) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "stale-pyc-repository"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "__init__.py").write_text("", encoding="utf-8")
    for module in (
        field_evidence,
        field_evidence.perf01_evidence,
        field_evidence.quality_evidence,
    ):
        shutil.copyfile(Path(module.__file__), scripts / Path(module.__file__).name)
    helper = scripts / "qa_evidence_manifest.py"
    good = Path(field_evidence.qa_evidence_manifest.__file__).read_bytes()
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
        [sys.executable, str(scripts / "field_story_evidence.py"), "--help"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_field_standalone_import_ignores_a_same_size_stale_qa_helper_pyc(
    tmp_path: Path,
) -> None:
    completed = _run_field_with_a_stale_qa_helper_pyc(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert "STALE PYC EXECUTED" not in completed.stderr


def test_field_import_bootstraps_use_distinct_empty_private_caches() -> None:
    modules = (
        field_evidence,
        field_evidence.perf01_evidence,
        field_evidence.quality_evidence,
    )
    caches = [Path(module._IMPORT_PYCACHE.name) for module in modules]
    assert all(module._IMPORT_PYCACHE_WAS_EMPTY is True for module in modules)
    assert all(cache.is_dir() for cache in caches)
    assert len(set(caches)) == len(caches)
    expected_hashes = {
        path.relative_to(field_evidence.ROOT).as_posix(): state[0]
        for path, state in field_evidence._EARLY_IMPORTED_VALIDATOR_STATE.items()
    }
    expected_revisions = {
        path.relative_to(field_evidence.ROOT).as_posix(): state[1]
        for path, state in field_evidence._EARLY_IMPORTED_VALIDATOR_STATE.items()
    }
    assert dict(field_evidence._IMPORTED_VALIDATOR_SOURCE_SNAPSHOT) == expected_hashes
    assert dict(field_evidence._IMPORTED_VALIDATOR_SOURCE_SNAPSHOT.revisions) == (
        expected_revisions
    )
    source = Path(field_evidence.__file__).read_text(encoding="utf-8")
    assert source.index("sys.pycache_prefix = _IMPORT_PYCACHE.name") < source.index(
        "from scripts import perf01_evidence"
    )


def _freeze_field_evidence_now(
    monkeypatch: pytest.MonkeyPatch,
    when: dt.datetime = dt.datetime(
        2026, 9, 8, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))
    ),
) -> None:
    real_datetime = dt.datetime

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> FrozenDateTime:
            return cls.fromtimestamp(when.timestamp(), tz or when.tzinfo)

    monkeypatch.setattr(field_evidence.dt, "datetime", FrozenDateTime)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(root: Path, relative: str, content: str = "field evidence\n") -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return relative


def _write_pcm_wav(
    root: Path, relative: str, duration_seconds: Decimal = Decimal("2700")
) -> str:
    """Create a tiny, real PCM WAV whose one-Hz timeline spans the field run."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(duration_seconds.to_integral_value(rounding=ROUND_CEILING))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(1)
        handle.writeframes(b"\x80" * frames)
    return relative


def _write_quality_tsv(root: Path) -> str:
    relative = f"{EVIDENCE_RELATIVE}/{FIELD_DATE}-quality-30.tsv"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    domains = ("理科", "数学", "行事")
    acoustics = ("後方", "雑音", "チャイム")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(field_evidence.quality_evidence.EXPECTED_HEADER)
        for number in range(1, 31):
            writer.writerow(
                [
                    str(number),
                    domains[(number - 1) % len(domains)],
                    acoustics[(number - 1) % len(acoustics)],
                    f"授業文 {number}",
                    f"認識文 {number}",
                    f"English {number}",
                    "4",
                    f"中文 {number}",
                    "3" if number <= 15 else "4",
                    "",
                ]
            )
    return relative


def _write_quality_package(
    root: Path, tsv_relative: str, start_relative: str, end_relative: str
) -> dict[str, str]:
    evidence = root / EVIDENCE_RELATIVE
    package_dir = evidence / "qual-01"
    package_dir.mkdir(parents=True, exist_ok=True)
    audio = package_dir / "lesson.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\0\0" * 8000 * 30)

    with (root / tsv_relative).open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle, delimiter="\t"))[1:]
    transcript = package_dir / "transcript.jsonl"
    transcript_rows = [
        {
            "seq": number,
            "created_at": f"2026-09-08T02:00:{number - 1:02d}+09:00",
            "t_start": float(number - 1),
            "t_end": float(number - 1) + 0.8,
            "ja": row[4],
            "asr_ms": 100,
            "translations": {
                "en": {"text": row[5], "engine": "hy-mt2", "mt_ms": 50},
                "zh": {"text": row[7], "engine": "hy-mt2", "mt_ms": 60},
            },
        }
        for number, row in enumerate(rows, start=1)
    ]
    transcript.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False) + "\n"
            for record in transcript_rows
        ),
        encoding="utf-8",
        newline="\n",
    )
    lesson_attestation = package_dir / "lesson-attestation.pdf"
    lesson_attestation.write_bytes(b"redacted real lesson attestation")

    evaluators: list[dict[str, Any]] = []
    roles = (
        ("eval-ja", "reference_transcriber", "ja", "native_speaker"),
        ("eval-en", "meaning_evaluator", "en", "language_teacher"),
        ("eval-zh", "meaning_evaluator", "zh", "native_speaker"),
    )
    for evaluator_id, role, language, qualification_kind in roles:
        qualification = package_dir / f"{evaluator_id}-qualification.pdf"
        review = package_dir / f"{evaluator_id}-review.pdf"
        qualification.write_bytes(f"{evaluator_id} qualification".encode())
        review.write_bytes(f"{evaluator_id} review receipt".encode())
        evaluators.append(
            {
                "id": evaluator_id,
                "person_ref": f"person-{language}",
                "role": role,
                "language": language,
                "qualification_kind": qualification_kind,
                "qualification_evidence": {
                    "path": qualification.relative_to(root).as_posix(),
                    "sha256": _sha256(qualification),
                },
                "review_attestation": {
                    "path": review.relative_to(root).as_posix(),
                    "sha256": _sha256(review),
                },
                "review_started_at": "2026-09-08T03:00:00+09:00",
                "review_finished_at": "2026-09-08T03:30:00+09:00",
            }
        )
    evaluators.sort(key=lambda item: str(item["id"]))
    start = json.loads((root / start_relative).read_text(encoding="utf-8"))
    provenance_relative = (
        f"{EVIDENCE_RELATIVE}/{FIELD_DATE}-quality-provenance.json"
    )
    provenance = root / provenance_relative
    payload = {
        "schema_version": field_evidence.quality_evidence.PACKAGE_SCHEMA_VERSION,
        "kind": field_evidence.quality_evidence.PACKAGE_KIND,
        "field_date": FIELD_DATE,
        "captured_at": "2026-09-08T04:00:00+09:00",
        "field_session_id": start["session_id"],
        "host_fingerprint_sha256": start["host_fingerprint_sha256"],
        "quality_tsv": {
            "path": tsv_relative,
            "sha256": _sha256(root / tsv_relative),
        },
        "sources": [
            {
                "id": "lesson-01",
                "domain": "mixed",
                "audio": {
                    "path": audio.relative_to(root).as_posix(),
                    "sha256": _sha256(audio),
                    "size_bytes": audio.stat().st_size,
                    "duration_ms": 30000,
                    "media_type": "audio/wav",
                },
                "transcript": {
                    "path": transcript.relative_to(root).as_posix(),
                    "sha256": _sha256(transcript),
                },
                "recorded_started_at": "2026-09-08T01:00:00+09:00",
                "recorded_finished_at": "2026-09-08T01:00:30+09:00",
                "processing_started_at": "2026-09-08T02:00:00+09:00",
                "processing_finished_at": "2026-09-08T02:01:00+09:00",
                "lesson_attestation": {
                    "path": lesson_attestation.relative_to(root).as_posix(),
                    "sha256": _sha256(lesson_attestation),
                },
            }
        ],
        "evaluators": evaluators,
        "segments": [
            {
                "row_no": number,
                "source_id": "lesson-01",
                "start_ms": (number - 1) * 1000,
                "end_ms": (number - 1) * 1000 + 800,
                "transcript_seq": number,
                "domain": ("science", "mathematics", "event_notice")[
                    (number - 1) % 3
                ],
                "acoustic_condition": ("rear", "noise", "chime")[
                    (number - 1) % 3
                ],
                "reference_evaluator_id": "eval-ja",
                "english_evaluator_id": "eval-en",
                "chinese_evaluator_id": "eval-zh",
            }
            for number in range(1, 31)
        ],
    }
    provenance.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest_files = {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in evidence.rglob("*")
        if path.is_file()
    }
    end = json.loads((root / end_relative).read_text(encoding="utf-8"))
    validation = field_evidence.quality_evidence.validate_quality_package(
        root / tsv_relative,
        provenance,
        root=root,
        field_date=FIELD_DATE,
        expected_session_id=start["session_id"],
        expected_host_fingerprint_sha256=start["host_fingerprint_sha256"],
        source_snapshot_start_at=start["captured_at"],
        source_snapshot_end_at=end["captured_at"],
        field_bundle_captured_at="2099-01-01T00:00:00+09:00",
        manifest_files=manifest_files,
    )
    assert validation["passed"] is True
    metrics = validation["metrics"]
    return {
        "F_C6": "可",
        "F_C6_MEASURED": "可",
        "F_C6_PROVENANCE_PASSED": "true",
        "F_C6_PROVENANCE": provenance_relative,
        "F_C6_PROVENANCE_SHA": str(validation["provenance_sha256"]),
        "F_C6_DATA_PASSED": "true",
        "F_C6_ROWS": str(metrics["rows"]),
        "F_C6_REQUIRED": str(metrics["required_rows"]),
        "F_C6_EN_COUNT": str(metrics["english_rating_count"]),
        "F_C6_EN_SCORE": f"{metrics['english_average']:.2f}",
        "F_C6_ZH_COUNT": str(metrics["chinese_rating_count"]),
        "F_C6_SCORE": f"{metrics['chinese_average']:.2f}",
        "F_C6_NUMBERS": str(metrics["ordered_number_count"]),
        "F_C6_DISTINCT": str(metrics["distinct_reference_count"]),
        "F_C6_DOMAIN_COVERAGE": str(metrics["domain_coverage_count"]),
        "F_C6_ACOUSTIC_COVERAGE": str(metrics["acoustic_coverage_count"]),
        "F_C6_SEGMENTS": str(metrics["mapped_segment_count"]),
        "F_C6_TRANSCRIPT_BOUND": str(metrics["transcript_bound_row_count"]),
        "F_C6_EVALUATOR_ASSIGNMENTS": str(
            metrics["evaluator_assignment_count"]
        ),
        "F_C6_ASR_REVIEW": "可",
        "F_C6_ASR_ERRORS": str(metrics["asr_mismatch_row_count"]),
        "F_C6_EVALUATOR": "英語・中国語担当者",
        "F_C6_SOURCE": lesson_attestation.relative_to(root).as_posix(),
        "F_C6_FILE": tsv_relative,
        "F_C6_SHA": _sha256(root / tsv_relative),
    }


def _create_sources(root: Path) -> None:
    sources = {
        "server/main.py",
        "web/student.js",
        "scripts/acceptance.py",
        "scripts/bench.py",
        "scripts/field-verification.sh",
        "scripts/field_story_evidence.py",
        "scripts/perf01_evidence.py",
        "scripts/performance_evidence.py",
        "scripts/quality_evidence.py",
        "scripts/replay_client.py",
        "scripts/story_tracker.py",
        "scripts/text_metrics.py",
        "scripts/validate_field_performance.py",
        "start.bat",
        "setup.ps1",
        "config.yaml",
        "requirements.txt",
        "requirements-bench.txt",
        "docs/qa/stories.tsv",
        "docs/field-verification-checklist.md",
    }
    for relative in sources:
        _write(root, relative, f"fixture source: {relative}\n")
    validator = root / "scripts" / "validate_field_performance.py"
    validator.write_bytes(Path(field_validation.__file__).read_bytes())
    perf01_validator = root / "scripts" / "perf01_evidence.py"
    perf01_validator.write_bytes(Path(perf01_evidence.__file__).read_bytes())
    quality_validator = root / "scripts" / "quality_evidence.py"
    quality_validator.write_bytes(Path(field_evidence.quality_evidence.__file__).read_bytes())
    _write(root, "scripts/.generated.py")
    _write(root, "server/__pycache__/cached.py")
    _write(root, "web/ignored.pyc")


def _write_source_snapshot_pair(
    root: Path,
    *,
    start_at: str = "2026-09-08T00:00:00+09:00",
    end_at: str = "2026-09-08T10:00:00+09:00",
    hostname: str = "SCHOOL-PC",
) -> tuple[str, str]:
    (root / EVIDENCE_RELATIVE).mkdir(parents=True, exist_ok=True)
    source_snapshot = field_evidence.hash_field_sources(root)
    source_hashes = dict(source_snapshot)
    source_revisions = field_evidence._source_revision_payload(source_snapshot)
    source_sha256 = field_evidence._source_snapshot_sha256(
        source_hashes, source_revisions
    )
    session_id = "b" * 64
    host_sha256 = hashlib.sha256(hostname.encode("utf-8")).hexdigest()
    start_relative = (
        f"{EVIDENCE_RELATIVE}/"
        f"{field_evidence.SOURCE_SNAPSHOT_FILENAMES['start']}"
    )
    end_relative = (
        f"{EVIDENCE_RELATIVE}/"
        f"{field_evidence.SOURCE_SNAPSHOT_FILENAMES['end']}"
    )
    start = {
        "schema_version": field_evidence.SOURCE_SNAPSHOT_SCHEMA_VERSION,
        "kind": field_evidence.SOURCE_SNAPSHOT_KIND,
        "field_date": FIELD_DATE,
        "phase": "start",
        "captured_at": start_at,
        "host_fingerprint_sha256": host_sha256,
        "session_id": session_id,
        "predecessor_sha256": None,
        "source_hashes": source_hashes,
        "source_revisions": source_revisions,
        "source_snapshot_sha256": source_sha256,
    }
    _write(
        root,
        start_relative,
        json.dumps(start, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    end = {
        **start,
        "phase": "end",
        "captured_at": end_at,
        "predecessor_sha256": _sha256(root / start_relative),
    }
    _write(
        root,
        end_relative,
        json.dumps(end, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return start_relative, end_relative


def _write_perf01_run(
    root: Path,
    *,
    final_count: int = 2,
    audio_seconds: Decimal = Decimal("2700"),
    minutes_text: str | None = None,
) -> dict[str, str]:
    base = dt.datetime(
        2026, 9, 8, 9, 0, 0,
        tzinfo=dt.timezone(dt.timedelta(hours=9)),
    )

    def stamp(seconds: Decimal | int) -> str:
        microseconds = int(Decimal(seconds) * Decimal(1_000_000))
        moment = base + dt.timedelta(microseconds=microseconds)
        return moment.isoformat(
            timespec="microseconds" if microseconds % 1_000_000 else "seconds"
        )

    input_relative = _write_pcm_wav(
        root,
        f"{EVIDENCE_RELATIVE}/perf-01/lesson.wav",
        audio_seconds,
    )
    recording_relative = _write(
        root, f"{EVIDENCE_RELATIVE}/perf-01/screen-recording.mp4", "video\n"
    )
    commit_relative = _write(
        root, f"{EVIDENCE_RELATIVE}/perf-01/commit-status.txt", "commit/status\n"
    )
    config_relative = _write(
        root, f"{EVIDENCE_RELATIVE}/perf-01/config-models.json", "{}\n"
    )
    runtime_relative = _write(
        root, f"{EVIDENCE_RELATIVE}/perf-01/runtime-browsers.json", "{}\n"
    )
    finals_relative = f"{EVIDENCE_RELATIVE}/perf-01/transcript.jsonl"
    sequences = tuple(range(1, final_count + 1))
    finals = [
        {
            "seq": sequence,
            "created_at": stamp(sequence * 20 + 1),
            "t_start": (sequence - 1) * 20,
            "t_end": sequence * 20,
            "ja": f"発話 {sequence}",
            "asr_ms": 400,
            "translations": {
                "en": {"text": f"English {sequence}", "engine": "hy-mt2", "mt_ms": 500},
                "zh": {"text": f"中文 {sequence}", "engine": "hy-mt2", "mt_ms": 600},
            },
        }
        for sequence in sequences
    ]
    _write(
        root,
        finals_relative,
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in finals),
    )
    measurements_relative = f"{EVIDENCE_RELATIVE}/perf-01/latency.tsv"
    measurements_path = root / measurements_relative
    latencies = (
        (Decimal("2"), Decimal("5"), Decimal("5"), Decimal("8"))
        if final_count == 2
        else tuple(
            Decimal("2") if index % 2 == 0 else Decimal("3")
            for index in range(final_count * 2)
        )
    )
    with measurements_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(perf01_evidence.TSV_HEADER)
        for ordinal, sequence in enumerate(sequences):
            utterance_end = sequence * 20
            for language_index, language in enumerate(perf01_evidence.LANGUAGES):
                latency = latencies[ordinal * 2 + language_index]
                writer.writerow(
                    [
                        "perf01-physical-001",
                        str(sequence),
                        language,
                        "student-en" if language == "en" else "student-zh",
                        stamp(utterance_end),
                        stamp(utterance_end + int(latency)),
                        perf01_evidence._canonical_decimal(latency),
                    ]
                )

    def reference(relative: str) -> dict[str, str]:
        return {"path": relative, "sha256": _sha256(root / relative)}

    devices = [
        {
            "device_id": (
                "student-en"
                if index == 0
                else "student-zh" if index == 5 else f"student-{index}"
            ),
            "lang": "en" if index < 5 else "zh",
            "representative": index in {0, 5},
            "model": "Chromebook" if index < 9 else "iPhone",
            "os": "ChromeOS 140" if index < 9 else "iOS 20",
            "browser": "Chrome 140" if index < 9 else "Safari 20",
        }
        for index in range(10)
    ]
    run_relative = f"{EVIDENCE_RELATIVE}/{FIELD_DATE}-perf01-run.json"
    snapshot_start = json.loads(
        (
            root
            / EVIDENCE_RELATIVE
            / field_evidence.SOURCE_SNAPSHOT_FILENAMES["start"]
        ).read_text(encoding="utf-8")
    )
    run = {
        "schema_version": perf01_evidence.RUN_SCHEMA_VERSION,
        "kind": perf01_evidence.RUN_KIND,
        "field_date": FIELD_DATE,
        "field_session_id": snapshot_start["session_id"],
        "host_fingerprint_sha256": snapshot_start["host_fingerprint_sha256"],
        "source_snapshot_sha256": snapshot_start["source_snapshot_sha256"],
        "run_id": "perf01-physical-001",
        "clock_id": "school-pc-video-clock-v1",
        "measurement_window": {
            "started_at": stamp(0),
            "audio_ended_at": stamp(audio_seconds),
            "observation_ended_at": stamp(audio_seconds + Decimal("10")),
        },
        "population": {
            "observed_from_at": stamp(0),
            "observed_until_at": stamp(audio_seconds + Decimal("10")),
            "teacher_reported_students": 10,
            "devices": devices,
        },
        "input": {
            "kind": "実授業WAV",
            "description": "学校PCから実時間再生した45分の実授業録音",
            "evidence": reference(input_relative),
        },
        "artifacts": {
            "measurements_tsv": reference(measurements_relative),
            "finals_jsonl": reference(finals_relative),
            "recordings": [reference(recording_relative)],
            "commit_status": reference(commit_relative),
            "config_models": reference(config_relative),
            "runtime_browsers": reference(runtime_relative),
        },
    }
    _write(
        root,
        run_relative,
        json.dumps(run, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )
    validation = perf01_evidence.validate_perf01_run(root / run_relative, root=root)
    validation_relative = f"{EVIDENCE_RELATIVE}/{FIELD_DATE}-perf01-validation.json"
    _write(
        root,
        validation_relative,
        json.dumps(validation, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )
    metrics = validation["metrics"]
    return {
        "F_C4": "可",
        "F_C4_MEASURED": "可",
        "F_C4_VALIDATED": "可",
        "F_C4_RUN": run_relative,
        "F_C4_VALIDATION": validation_relative,
        "F_C4_POPULATION": "可",
        "F_C4_COMPLETE": "可",
        "F_C4_BROWSER_DEVICES": "10 physical browsers",
        "F_C4_COUNT": "10",
        "F_C4_EN_COUNT": "5",
        "F_C4_ZH_COUNT": "5",
        "F_C4_REPS": "en=student-en; zh=student-zh",
        "F_C4_INPUT_KIND": "実授業WAV",
        "F_C4_INPUT": "学校PCから実時間再生した45分の実授業録音",
        "F_C4_INPUT_EVIDENCE": input_relative,
        "F_C4_MINUTES": minutes_text or perf01_evidence._canonical_decimal(
            audio_seconds / Decimal(60)
        ),
        "F_C4_FINALS": str(metrics["final_count"]),
        "F_C4_MATCHED": str(metrics["matched_rows"]),
        "F_C4_EXPECTED": str(metrics["expected_rows"]),
        "F_C4_MED": str(metrics["latency_median_seconds"]),
        "F_C4_MAX": str(metrics["latency_max_seconds"]),
        "F_C4_COMMIT": commit_relative,
        "F_C4_CONFIG": config_relative,
        "F_C4_RUNTIME": runtime_relative,
        "F_C4_EVIDENCE": measurements_relative,
    }


def _valid_environment(root: Path) -> dict[str, str]:
    details = {
        key: ""
        for story in field_evidence.FIELD_STORIES
        for key in field_evidence.STORY_DETAIL_KEYS[story]
    }

    snapshot_start, snapshot_end = _write_source_snapshot_pair(root)
    setup = _write(root, f"{EVIDENCE_RELATIVE}/ops-01/setup.txt")
    retry_before = _write(root, f"{EVIDENCE_RELATIVE}/ops-02/before.txt")
    retry_after = _write(root, f"{EVIDENCE_RELATIVE}/ops-02/after.txt")
    retry_manifests = _write(root, f"{EVIDENCE_RELATIVE}/ops-02/manifests.txt")
    retry_log = _write(root, f"{EVIDENCE_RELATIVE}/ops-02/retry.txt")
    ops10 = _write(root, f"{EVIDENCE_RELATIVE}/ops-10/network.txt")
    c1_devices = _write(root, f"{EVIDENCE_RELATIVE}/ops-12/c1-devices.txt")
    c1_evidence = _write(root, f"{EVIDENCE_RELATIVE}/ops-12/c1-video.txt")
    c2_devices = _write(root, f"{EVIDENCE_RELATIVE}/ops-12/c2-devices.txt")
    c2_evidence = _write(root, f"{EVIDENCE_RELATIVE}/ops-12/c2-video.txt")
    chromebook = _write(root, f"{EVIDENCE_RELATIVE}/stu-11/chromebook.mp4")
    iphone = _write(root, f"{EVIDENCE_RELATIVE}/stu-11/iphone.mp4")
    perf01_details = _write_perf01_run(root)
    audio_evidence = _write(root, f"{EVIDENCE_RELATIVE}/perf-02/audio.txt")
    cache_on_validation = _write(
        root, f"{EVIDENCE_RELATIVE}/cache-on/field-validation.json", "{}\n"
    )
    cache_on_manifest = _write(
        root, f"{EVIDENCE_RELATIVE}/cache-on/source-manifest.json", "{}\n"
    )
    cache_on_report = _write(
        root, f"{EVIDENCE_RELATIVE}/cache-on/report.json", "{}\n"
    )
    cache_off_validation = _write(
        root, f"{EVIDENCE_RELATIVE}/cache-off/field-validation.json", "{}\n"
    )
    cache_off_manifest = _write(
        root, f"{EVIDENCE_RELATIVE}/cache-off/source-manifest.json", "{}\n"
    )
    cache_off_report = _write(
        root, f"{EVIDENCE_RELATIVE}/cache-off/report.json", "{}\n"
    )
    quality_tsv = _write_quality_tsv(root)
    quality_details = _write_quality_package(
        root, quality_tsv, snapshot_start, snapshot_end
    )

    details.update(
        {
            "F_OPS01": "可",
            "F_SETUP_FRESH": "可",
            "F_SETUP_OK": "可",
            "F_SETUP_STOPPED": "可",
            "F_SETUP_ENV": "clean-school-pc",
            "F_SETUP_EVIDENCE": setup,
            "F_OPS02": "可",
            "F_RETRY_EXECUTED": "実施",
            "F_RETRY_ENV": "isolated-school-pc",
            "F_RETRY_DONE_MODEL": "sherpa",
            "F_RETRY_DONE_STATE": retry_before,
            "F_RETRY_DONE_AFTER": retry_after,
            "F_RETRY_ACTIVE": "hy-mt2/model.bin",
            "F_RETRY_MARKER": "False",
            "F_RETRY_SERVER": "可",
            "F_RETRY_REUSED": "可",
            "F_RETRY_PARTIAL": "再開",
            "F_RETRY_RESULT": "可",
            "F_RETRY_MANIFESTS": retry_manifests,
            "F_RETRY_EVIDENCE": retry_log,
            "F_OPS10": "可",
            "F_OPS10_MEASURED": "可",
            "F_OPS10_A": "192.0.2.10 / 8000 / 1234",
            "F_OPS10_B": "192.0.2.11 / 8000 / 5678",
            "F_OPS10_CHANGED": "可",
            "F_OPS10_A_MATCH": "可",
            "F_OPS10_B_MATCH": "可",
            "F_OPS10_RESTORED": "可",
            "F_OPS10_EVIDENCE": ops10,
            "F_C2": "可",
            "F_C2_MEASURED": "可",
            "F_C1": "可",
            "F_C1_COUNT": "10",
            "F_C1_NETWORK": "school-wifi",
            "F_A_CONFIG": "校内Wi-Fi直結",
            "F_C2_NETWORK_MATCH": "可",
            "F_C2_TIME_CHECK": "可",
            "F_C2_ZERO": "可",
            "F_C2_CONNECTED": "10",
            "F_C2_QR_COUNT": "10",
            "F_C2_MANUAL_COUNT": "0",
            "F_C2_LANGS": "en=5, zh=5",
            "F_C2_START": "2026-09-08T09:00:00+09:00",
            "F_C2_END": "2026-09-08T09:01:30+09:00",
            "F_C2_SEC": "90",
            "F_C1_DEVICES": c1_devices,
            "F_C1_EVIDENCE": c1_evidence,
            "F_C2_DEVICES": c2_devices,
            "F_C2_EVIDENCE": c2_evidence,
            "F_STU11": "可",
            "F_STU11_CB": "Chromebook / ChromeOS / Chrome",
            "F_STU11_CB_OK": "可",
            "F_STU11_CB_NOAPP": "可",
            "F_STU11_CB_VIEW": "可",
            "F_STU11_CB_TOUCH": "可",
            "F_STU11_CB_LOCK": "可",
            "F_STU11_CB_EVIDENCE": chromebook,
            "F_STU11_CB_SHA": _sha256(root / chromebook),
            "F_STU11_IOS": "iPhone / iOS / Safari",
            "F_STU11_IOS_OK": "可",
            "F_STU11_IOS_NOAPP": "可",
            "F_STU11_IOS_VIEW": "可",
            "F_STU11_IOS_TOUCH": "可",
            "F_STU11_IOS_LOCK": "可",
            "F_STU11_IOS_EVIDENCE": iphone,
            "F_STU11_IOS_SHA": _sha256(root / iphone),
            **perf01_details,
            "F_PERF02": "可",
            "F_PERF02_MEASURED": "可",
            "F_ACC_VALIDATED": "可",
            "F_ACC_AUDIO_MIN": "45",
            "F_ACC_AUDIO_SHA": "a" * 64,
            "F_ACC_ASR": "sherpa",
            "F_ACC_ENGINE": "hy-mt2",
            "F_ACC_RSS": "2048",
            "F_ACC_CRASHES": "0",
            "F_ACC_ERRORS": "0",
            "F_ACC_CPU": "40",
            "F_ACC_AUDIO_EVIDENCE": audio_evidence,
            "F_ACC_REPORT": cache_on_report,
            "F_ACC_MANIFEST": cache_on_manifest,
            "F_ACC_VALIDATION": cache_on_validation,
            "F_ACC_NOCACHE_OK": "可",
            "F_ACC_NOCACHE_VALIDATED": "可",
            "F_ACC_NOCACHE_REPORT": cache_off_report,
            "F_ACC_NOCACHE_MANIFEST": cache_off_manifest,
            "F_ACC_NOCACHE_VALIDATION": cache_off_validation,
            **quality_details,
        }
    )
    details.update(
        {
            "F_DATE": FIELD_DATE,
            "F_TESTER": "field tester",
            "F_EVIDENCE_TRANSFER": "可",
            "F_EVIDENCE_DIR": EVIDENCE_RELATIVE,
            "F_EVIDENCE_MANIFEST": MANIFEST_RELATIVE,
            "F_SOURCE_SNAPSHOT_START": snapshot_start,
            "F_SOURCE_SNAPSHOT_END": snapshot_end,
            "F_STORIES_COMPLETE": "可",
            "F_FIELD_COMPLETE": "可",
            "F_REHEARSAL_COMPLETE": "可",
            "F_OVERALL": "可",
            "F_OVERALL_COND": "",
        }
    )
    return details


def _valid_system() -> dict[str, object]:
    return {
        "cpu": "Intel(R) Core(TM) i5-8250U",
        "cores": 4,
        "threads": 8,
        "ram_gb": 16.4,
        "os": "Windows 11",
        "python": "3.12.10",
        "date": "2026-09-08",
    }


def _valid_hardware() -> dict[str, object]:
    return {
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
        "memory_installed_bytes": 16 * 1024**3,
        "memory_os_visible_bytes": 16_398_209_024,
    }


def _performance_result(cache_mode: str, effective_relative: str) -> dict[str, object]:
    samples = 1_400
    cache_size = 20 if cache_mode == "on" else 0
    return {
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
        "mt_cache": {"size": cache_size},
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
            "mt_cache_size": cache_size,
        },
        "verdict": {"passed": True},
        "latency": {"median_s": 2.0, "max_s": 4.0},
    }


def _write_transferred_performance_run(
    root: Path, *, cache_mode: str
) -> tuple[str, str, str]:
    host_sha256 = hashlib.sha256(b"SCHOOL-PC").hexdigest()
    started_at, finished_at, collected_at = (
        (
            "2026-09-08T00:10:00+09:00",
            "2026-09-08T00:56:00+09:00",
            "2026-09-08T00:56:30+09:00",
        )
        if cache_mode == "on"
        else (
            "2026-09-08T01:00:00+09:00",
            "2026-09-08T01:46:00+09:00",
            "2026-09-08T01:46:30+09:00",
        )
    )
    run_relative = (
        f"{EVIDENCE_RELATIVE}/20000101-{host_sha256[:16]}-school-cache-{cache_mode}"
    )
    run = root / run_relative
    effective_relative = f"_work/effective-cache-{cache_mode}.yaml"
    effective_path = run / effective_relative
    effective_path.parent.mkdir(parents=True, exist_ok=True)
    effective_path.write_text("effective config\n", encoding="utf-8")
    report_relative_in_run = "report.json"
    report_path = run / report_relative_in_run
    result = _performance_result(cache_mode, effective_relative)
    report = {"system": _valid_system(), "results": [result]}
    report_path.write_text(json.dumps(report), encoding="utf-8")

    source_hashes = field_evidence._performance_source_hashes(root)
    input_hashes = {"external:D:/field-audio/lesson.wav": "a" * 64}
    runtime_asset_hashes = {"external:C:/school-models/model.bin": "c" * 64}
    runtime = {
        "python": "3.12.10 (school fixture)",
        "executable": r"C:\school\.venv\Scripts\python.exe",
        "executable_sha256": "d" * 64,
        "active_distributions": ["psutil"],
        "distributions": {
            "psutil": {
                "version": "6.0.0",
                "metadata_hashes": {"METADATA": "e" * 64},
            }
        },
    }
    effective = {effective_relative: _sha256(effective_path)}
    artifacts = {report_relative_in_run: _sha256(report_path)}
    locked_files = sorted(
        {
            *source_hashes,
            *input_hashes,
            *runtime_asset_hashes,
            f"{run_relative}/{effective_relative}",
            f"{run_relative}/{report_relative_in_run}",
            "external:C:/school/.venv/Scripts/python.exe",
        }
    )
    manifest: dict[str, Any] = {
        "schema_version": field_evidence.PERFORMANCE_MANIFEST_SCHEMA,
        "kind": "soak",
        "complete": True,
        "started_at": started_at,
        "finished_at": finished_at,
        "arguments": {
            "minutes": 45.0,
            "students": 10,
            "audio": r"D:\field-audio\lesson.wav",
            "corpus": "ja_ext",
            "langs": ["en", "zh"],
            "disconnect_every_s": None,
            "mt_cache_size": None if cache_mode == "on" else 0,
            "engine": "hy-mt2",
            "asr": "sherpa",
            "partial": None,
            "port": 18_431,
            "drain_seconds": 20.0,
            "out_dir": run_relative,
            "no_config_update": True,
        },
        "configuration": {
            "base": {
                "path": "config.yaml",
                "sha256": source_hashes["config.yaml"],
            },
            "effective": effective,
        },
        "source_hashes": source_hashes,
        "input_hashes": input_hashes,
        "runtime_asset_hashes": runtime_asset_hashes,
        "artifacts": artifacts,
        "primary_report": report_relative_in_run,
        "runtime": runtime,
        "continuous_lock": {
            "mode": "windows-share-read",
            "acquired_at": started_at,
            "released_at": finished_at,
            "interrupted": False,
            "files": locked_files,
        },
    }
    manifest_relative = f"{run_relative}/{field_evidence.PERFORMANCE_MANIFEST_NAME}"
    manifest_path = root / manifest_relative
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    checks, metrics = field_validation._validate_result(
        result,
        arguments=manifest["arguments"],
        effective_configs=set(effective),
        cache_mode=cache_mode,
        audio_sha256="a" * 64,
    )
    system_checks, system, hardware = field_validation._validated_system(
        report["system"], hardware=_valid_hardware(), started_at=started_at
    )
    checks.update(system_checks)
    checks.update(
        {
            "hardware_collected_immediately_after_run": True,
            "manifest_complete_and_current": True,
            "arguments_at_least_45_minutes": True,
            "arguments_ten_students": True,
            "arguments_out_dir_matches_run": True,
            "run_dir_binds_validation_host": True,
            "real_audio_input": True,
            "real_asr": True,
            "real_mt": True,
            "no_config_update": True,
            "report_engines_match_arguments": True,
            "audio_hash_matches": True,
            "primary_report_hash_matches": True,
        }
    )
    binding = {
        "minutes": 45.0,
        "students": 10,
        "audio_sha256": "a" * 64,
        "corpus": "ja_ext",
        "asr": "sherpa",
        "engine": "hy-mt2",
        "langs": ["en", "zh"],
        "disconnect_every_s": None,
        "partial": None,
        "drain_seconds": 20.0,
        "no_config_update": True,
        "effective_config": effective_relative,
    }
    validation = {
        "schema_version": field_validation.FIELD_VALIDATION_SCHEMA,
        "kind": "field-performance-validation",
        "passed": True,
        "cache_mode": cache_mode,
        "run_dir": run_relative,
        "manifest": manifest_relative,
        "primary_report": f"{run_relative}/{report_relative_in_run}",
        "validator_sha256": _sha256(
            root / "scripts" / "validate_field_performance.py"
        ),
        "source_snapshot_sha256": field_evidence._canonical_sha256(source_hashes),
        "runtime_snapshot_sha256": field_evidence._canonical_sha256(
            {"runtime": runtime, "runtime_asset_hashes": runtime_asset_hashes}
        ),
        "system_snapshot_sha256": field_evidence._canonical_sha256(
            {
                key: system[key]
                for key in ("cpu", "cores", "threads", "ram_gb", "os", "python")
            }
        ),
        "hardware_snapshot_sha256": field_evidence._canonical_sha256(hardware),
        "host_fingerprint_sha256": host_sha256,
        "hardware_collected_at": collected_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "manifest_sha256": _sha256(manifest_path),
        "primary_report_sha256": _sha256(report_path),
        "system": system,
        "hardware": hardware,
        "binding": binding,
        "checks": checks,
        "metrics": metrics,
    }
    validation_relative = f"{run_relative}/field-validation.json"
    (root / validation_relative).write_text(json.dumps(validation), encoding="utf-8")
    return validation_relative, manifest_relative, f"{run_relative}/{report_relative_in_run}"


@pytest.fixture
def field_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, str]]:
    _create_sources(tmp_path)
    environ = _valid_environment(tmp_path)
    field_evidence.rewrite_outer_manifest(tmp_path, FIELD_DATE)

    def fake_performance(
        _details: object,
        mode: str,
        _root: Path,
        _evidence_dir: Path,
        _manifest_files: object,
    ) -> dict[str, str]:
        host_sha256 = hashlib.sha256(b"SCHOOL-PC").hexdigest()
        return {
            "cache_mode": mode,
            "started_at": (
                "2026-09-08T00:10:00+09:00"
                if mode == "on"
                else "2026-09-08T01:00:00+09:00"
            ),
            "finished_at": (
                "2026-09-08T00:56:00+09:00"
                if mode == "on"
                else "2026-09-08T01:46:00+09:00"
            ),
            "host_fingerprint_sha256": host_sha256,
        }

    monkeypatch.setattr(field_evidence, "_validate_saved_performance", fake_performance)
    monkeypatch.setattr(
        field_evidence, "_validate_performance_pair", lambda _on, _off: None
    )
    return tmp_path, environ


@pytest.fixture
def transferred_field_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    _create_sources(tmp_path)
    environ = _valid_environment(tmp_path)
    on_validation, on_manifest, on_report = _write_transferred_performance_run(
        tmp_path, cache_mode="on"
    )
    off_validation, off_manifest, off_report = _write_transferred_performance_run(
        tmp_path, cache_mode="off"
    )
    environ.update(
        {
            "F_ACC_VALIDATION": on_validation,
            "F_ACC_MANIFEST": on_manifest,
            "F_ACC_REPORT": on_report,
            "F_ACC_NOCACHE_VALIDATION": off_validation,
            "F_ACC_NOCACHE_MANIFEST": off_manifest,
            "F_ACC_NOCACHE_REPORT": off_report,
        }
    )
    field_evidence.rewrite_outer_manifest(tmp_path, FIELD_DATE)
    return tmp_path, environ


def _capture(field_repo: tuple[Path, dict[str, str]]) -> tuple[Path, dict[str, str]]:
    root, environ = field_repo
    field_evidence.capture_from_environment(root=root, environ=environ)
    return root, environ


def _rewrite_bundle(root: Path, mutate: Callable[[dict[str, object]], None]) -> Path:
    bundle = root / EVIDENCE_RELATIVE / field_evidence.BUNDLE_NAME
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    mutate(payload)
    bundle.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)
    return bundle


def test_capture_seals_sidecar_and_validate_replays_all_candidates(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = _capture(field_repo)

    result = field_evidence.validate_field_bundle(
        root / EVIDENCE_RELATIVE, root=root
    )
    manifest = field_evidence.validate_outer_manifest(root, FIELD_DATE)
    bundle_relative = f"{EVIDENCE_RELATIVE}/{field_evidence.BUNDLE_NAME}"

    assert result["approved_candidates"] == list(field_evidence.FIELD_STORIES)
    assert result["field_complete"] is True
    assert result["bundle_sha256"] == manifest["files"][bundle_relative]
    assert result["manifest_sha256"] == manifest["manifest_sha256"]
    assert result["payload"]["manual_review_required"] is True
    assert result["payload"]["source_snapshot_start"] == (
        f"{EVIDENCE_RELATIVE}/school-source-start.json"
    )
    assert result["payload"]["source_snapshot_end"] == (
        f"{EVIDENCE_RELATIVE}/school-source-end.json"
    )
    assert result["payload"]["stories"]["PERF-02"]["details"] == {
        "F_PERF02": "可",
        "F_PERF02_MEASURED": "可",
        "F_ACC_VALIDATED": "可",
        "F_ACC_AUDIO_MIN": "45",
        "F_ACC_AUDIO_SHA": "a" * 64,
        "F_ACC_ASR": "sherpa",
        "F_ACC_ENGINE": "hy-mt2",
        "F_ACC_RSS": "2048",
        "F_ACC_CRASHES": "0",
        "F_ACC_ERRORS": "0",
        "F_ACC_CPU": "40",
        "F_ACC_AUDIO_EVIDENCE": f"{EVIDENCE_RELATIVE}/perf-02/audio.txt",
        "F_ACC_REPORT": f"{EVIDENCE_RELATIVE}/cache-on/report.json",
        "F_ACC_MANIFEST": f"{EVIDENCE_RELATIVE}/cache-on/source-manifest.json",
        "F_ACC_VALIDATION": f"{EVIDENCE_RELATIVE}/cache-on/field-validation.json",
        "F_ACC_NOCACHE_OK": "可",
        "F_ACC_NOCACHE_VALIDATED": "可",
        "F_ACC_NOCACHE_REPORT": f"{EVIDENCE_RELATIVE}/cache-off/report.json",
        "F_ACC_NOCACHE_MANIFEST": (
            f"{EVIDENCE_RELATIVE}/cache-off/source-manifest.json"
        ),
        "F_ACC_NOCACHE_VALIDATION": (
            f"{EVIDENCE_RELATIVE}/cache-off/field-validation.json"
        ),
    }
    assert result["payload"]["stories"]["QUAL-01"]["details"]["F_C6_EN_SCORE"] == (
        "4.00"
    )
    assert result["payload"]["stories"]["QUAL-01"]["details"]["F_C6_SCORE"] == (
        "3.50"
    )
    perf01 = result["payload"]["stories"]["PERF-01"]
    assert perf01["details"]["F_C4_VALIDATED"] == "可"
    assert perf01["details"]["F_C4_RUN"] == (
        f"{EVIDENCE_RELATIVE}/{FIELD_DATE}-perf01-run.json"
    )
    assert perf01["details"]["F_C4_VALIDATION"] == (
        f"{EVIDENCE_RELATIVE}/{FIELD_DATE}-perf01-validation.json"
    )
    perf01_validation = json.loads(
        (root / perf01["details"]["F_C4_VALIDATION"]).read_text(encoding="utf-8")
    )
    assert set(perf01_validation["artifact_hashes"]) <= set(manifest["files"])


def test_validate_returns_passed_subset_when_shared_performance_gate_passes(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = field_repo
    environ.update(
        {
            "F_STU11": "否",
            "F_STU11_CB_OK": "否",
            "F_STORIES_COMPLETE": "否",
            "F_FIELD_COMPLETE": "否",
            "F_OVERALL": "否",
            "F_OVERALL_COND": "STU-11 failed",
        }
    )

    result = field_evidence.capture_from_environment(root=root, environ=environ)

    assert tuple(result["payload"]["stories"]) == field_evidence.FIELD_STORIES
    assert result["approved_candidates"] == [
        story for story in field_evidence.FIELD_STORIES if story != "STU-11"
    ]
    assert result["field_complete"] is False


def test_validate_returns_no_candidates_when_shared_cache_off_gate_fails(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = field_repo
    environ.update(
        {
            "F_ACC_NOCACHE_OK": "否",
            "F_ACC_NOCACHE_VALIDATED": "否",
            "F_FIELD_COMPLETE": "否",
            "F_OVERALL": "否",
            "F_OVERALL_COND": "cache-off validation failed",
        }
    )

    result = field_evidence.capture_from_environment(root=root, environ=environ)

    assert result["payload"]["stories"]["PERF-02"]["candidate"] == "可"
    assert result["approved_candidates"] == []
    assert result["field_complete"] is False


def test_validate_returns_no_candidate_for_lone_perf02_without_cache_off(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = field_repo
    for detail_key, status_key, failed_value in (
        ("F_SETUP_OK", "F_OPS01", "否"),
        ("F_RETRY_SERVER", "F_OPS02", "否"),
        ("F_OPS10_CHANGED", "F_OPS10", "否"),
        ("F_C2_ZERO", "F_C2", "否"),
        ("F_STU11_CB_OK", "F_STU11", "否"),
        ("F_C4_COMPLETE", "F_C4", "否"),
        ("F_C6_DATA_PASSED", "F_C6", "false"),
    ):
        environ[detail_key] = failed_value
        environ[status_key] = "否"
    environ.update(
        {
            "F_ACC_NOCACHE_OK": "否",
            "F_ACC_NOCACHE_VALIDATED": "否",
            "F_STORIES_COMPLETE": "否",
            "F_FIELD_COMPLETE": "否",
            "F_OVERALL": "否",
            "F_OVERALL_COND": "only PERF-02 passed without cache-off",
        }
    )

    result = field_evidence.capture_from_environment(root=root, environ=environ)

    passed_entries = [
        story
        for story, entry in result["payload"]["stories"].items()
        if entry["candidate"] == "可"
    ]
    assert passed_entries == ["PERF-02"]
    assert result["approved_candidates"] == []
    assert result["field_complete"] is False


def test_cli_json_exposes_approved_candidates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    approved = [story for story in field_evidence.FIELD_STORIES if story != "STU-11"]
    monkeypatch.setattr(
        field_evidence,
        "capture_from_environment",
        lambda: {
            "bundle_path": field_evidence.ROOT / EVIDENCE_RELATIVE / "field-story-evidence.json",
            "bundle_sha256": "a" * 64,
            "manifest_path": field_evidence.ROOT / MANIFEST_RELATIVE,
            "manifest_sha256": "b" * 64,
            "approved_candidates": approved,
            "field_complete": False,
        },
    )

    assert field_evidence.main(["capture"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["approved_candidates"] == approved


def test_capture_accepts_transferred_runs_without_original_audio_models_or_runtime(
    transferred_field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = transferred_field_repo

    result = field_evidence.capture_from_environment(root=root, environ=environ)

    assert result["field_complete"] is True
    assert not (root / "lesson.wav").exists()
    assert not (root / "school-models" / "model.bin").exists()
    assert result["payload"]["source_snapshot_host_fingerprint_sha256"] == (
        hashlib.sha256(b"SCHOOL-PC").hexdigest()
    )


def test_capture_rejects_a_stale_schema_three_performance_receipt(
    transferred_field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = transferred_field_repo
    manifest_path = root / environ["F_ACC_MANIFEST"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 3
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    with pytest.raises(field_evidence.FieldEvidenceError, match="schema"):
        field_evidence.capture_from_environment(root=root, environ=environ)

    assert not (root / EVIDENCE_RELATIVE / field_evidence.BUNDLE_NAME).exists()


def test_capture_rejects_start_and_end_both_taken_after_field_tests(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = field_repo
    start_path = root / environ["F_SOURCE_SNAPSHOT_START"]
    end_path = root / environ["F_SOURCE_SNAPSHOT_END"]
    start = json.loads(start_path.read_text(encoding="utf-8"))
    end = json.loads(end_path.read_text(encoding="utf-8"))
    start["captured_at"] = "2026-09-08T11:00:00+09:00"
    end["captured_at"] = "2026-09-08T11:01:00+09:00"
    start_path.write_text(
        json.dumps(start, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    end["predecessor_sha256"] = _sha256(start_path)
    end_path.write_text(
        json.dumps(end, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    with pytest.raises(
        field_evidence.FieldEvidenceError, match="全試験終了後|package validator"
    ):
        field_evidence.capture_from_environment(root=root, environ=environ)

    assert not (root / EVIDENCE_RELATIVE / field_evidence.BUNDLE_NAME).exists()


def test_capture_rejects_snapshots_from_a_different_pc_than_performance_runs(
    transferred_field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = transferred_field_repo
    start_path = root / environ["F_SOURCE_SNAPSHOT_START"]
    end_path = root / environ["F_SOURCE_SNAPSHOT_END"]
    start = json.loads(start_path.read_text(encoding="utf-8"))
    end = json.loads(end_path.read_text(encoding="utf-8"))
    hand_pc_sha256 = hashlib.sha256(b"HAND-PC").hexdigest()
    start["host_fingerprint_sha256"] = hand_pc_sha256
    end["host_fingerprint_sha256"] = hand_pc_sha256
    start_path.write_text(
        json.dumps(start, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    end["predecessor_sha256"] = _sha256(start_path)
    end_path.write_text(
        json.dumps(end, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    with pytest.raises(
        field_evidence.FieldEvidenceError, match="学校PC binding|package validator"
    ):
        field_evidence.capture_from_environment(root=root, environ=environ)


def test_end_snapshot_rejects_source_changed_since_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sources(tmp_path)
    monkeypatch.setenv("COMPUTERNAME", "SCHOOL-PC")
    _freeze_field_evidence_now(monkeypatch)
    field_evidence.write_field_source_snapshot(
        root=tmp_path, field_date=FIELD_DATE, phase="start"
    )
    (tmp_path / "web" / "student.js").write_text(
        "changed during field verification\n", encoding="utf-8"
    )

    with pytest.raises(field_evidence.FieldEvidenceError, match="sourceまたは学校PC"):
        field_evidence.write_field_source_snapshot(
            root=tmp_path, field_date=FIELD_DATE, phase="end"
        )

    assert not (
        tmp_path
        / EVIDENCE_RELATIVE
        / field_evidence.SOURCE_SNAPSHOT_FILENAMES["end"]
    ).exists()


def test_end_snapshot_rejects_source_aba_restored_to_starting_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sources(tmp_path)
    monkeypatch.setenv("COMPUTERNAME", "SCHOOL-PC")
    real_datetime = dt.datetime
    local_zone = dt.timezone(dt.timedelta(hours=9))

    class MutableDateTime(real_datetime):
        current = real_datetime(2026, 9, 8, 8, 0, tzinfo=local_zone)

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> MutableDateTime:
            return cls.fromtimestamp(cls.current.timestamp(), tz or cls.current.tzinfo)

    monkeypatch.setattr(field_evidence.dt, "datetime", MutableDateTime)
    field_evidence.write_field_source_snapshot(
        root=tmp_path, field_date=FIELD_DATE, phase="start"
    )
    source = tmp_path / "web" / "student.js"
    original = source.read_bytes()
    changed = source.with_name("student.changed")
    changed.write_bytes(b"changed during field verification\n")
    os.replace(changed, source)
    restored = source.with_name("student.restored")
    restored.write_bytes(original)
    os.replace(restored, source)
    MutableDateTime.current = real_datetime(2026, 9, 8, 9, 0, tzinfo=local_zone)

    with pytest.raises(field_evidence.FieldEvidenceError, match="source"):
        field_evidence.write_field_source_snapshot(
            root=tmp_path, field_date=FIELD_DATE, phase="end"
        )

    assert not (
        tmp_path
        / EVIDENCE_RELATIVE
        / field_evidence.SOURCE_SNAPSHOT_FILENAMES["end"]
    ).exists()


def test_end_snapshot_rejects_in_place_source_aba_with_restored_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sources(tmp_path)
    monkeypatch.setenv("COMPUTERNAME", "SCHOOL-PC")
    real_datetime = dt.datetime
    local_zone = dt.timezone(dt.timedelta(hours=9))

    class MutableDateTime(real_datetime):
        current = real_datetime(2026, 9, 8, 8, 0, tzinfo=local_zone)

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> MutableDateTime:
            return cls.fromtimestamp(cls.current.timestamp(), tz or cls.current.tzinfo)

    monkeypatch.setattr(field_evidence.dt, "datetime", MutableDateTime)
    field_evidence.write_field_source_snapshot(
        root=tmp_path, field_date=FIELD_DATE, phase="start"
    )
    source = tmp_path / "web" / "student.js"
    before = source.stat()
    original = source.read_bytes()
    changed = bytes(byte ^ 1 for byte in original)
    source.write_bytes(changed)
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    source.write_bytes(original)
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    MutableDateTime.current = real_datetime(2026, 9, 8, 9, 0, tzinfo=local_zone)

    with pytest.raises(field_evidence.FieldEvidenceError, match="source"):
        field_evidence.write_field_source_snapshot(
            root=tmp_path, field_date=FIELD_DATE, phase="end"
        )

    assert not (
        tmp_path
        / EVIDENCE_RELATIVE
        / field_evidence.SOURCE_SNAPSHOT_FILENAMES["end"]
    ).exists()


def test_source_snapshot_rejects_a_symlinked_source_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sources(tmp_path)
    monkeypatch.setenv("COMPUTERNAME", "SCHOOL-PC")
    _freeze_field_evidence_now(monkeypatch)
    source = tmp_path / "server" / "main.py"
    target = tmp_path / "server" / "real-main.py"
    source.replace(target)
    try:
        source.symlink_to(target)
    except OSError as exc:
        target.replace(source)
        pytest.skip(f"file symlinks are unavailable: {exc}")

    with pytest.raises(field_evidence.FieldEvidenceError, match="link|junction"):
        field_evidence.write_field_source_snapshot(
            root=tmp_path, field_date=FIELD_DATE, phase="start"
        )


def test_source_snapshot_rejects_a_linked_source_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sources(tmp_path)
    monkeypatch.setenv("COMPUTERNAME", "SCHOOL-PC")
    _freeze_field_evidence_now(monkeypatch)
    source_tree = tmp_path / "server"
    target_tree = tmp_path / "real-server"
    source_tree.replace(target_tree)
    try:
        source_tree.symlink_to(target_tree, target_is_directory=True)
    except OSError as exc:
        target_tree.replace(source_tree)
        pytest.skip(f"directory reparse points are unavailable: {exc}")

    with pytest.raises(field_evidence.FieldEvidenceError, match="link|junction"):
        field_evidence.write_field_source_snapshot(
            root=tmp_path, field_date=FIELD_DATE, phase="start"
        )


def test_source_snapshot_rejects_a_source_set_mutation_during_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sources(tmp_path)
    real_reviewed_tree_files = field_evidence._reviewed_tree_files
    mutated = False

    def mutate_after_server_enumeration(tree: Path) -> list[Path]:
        nonlocal mutated
        files = real_reviewed_tree_files(tree)
        if tree == tmp_path / "server" and not mutated:
            mutated = True
            (tree / "added-during-scan.py").write_text(
                "changed during enumeration\n", encoding="utf-8"
            )
        return files

    monkeypatch.setattr(
        field_evidence, "_reviewed_tree_files", mutate_after_server_enumeration
    )

    with pytest.raises(field_evidence.FieldEvidenceError, match="source set|source"):
        field_evidence.hash_field_sources(tmp_path)


@pytest.mark.parametrize("linked_kind", ("file", "directory"))
def test_source_snapshot_deterministically_rejects_simulated_reparse_points(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked_kind: str,
) -> None:
    _create_sources(tmp_path)
    linked_path = (
        tmp_path / "server" / "main.py"
        if linked_kind == "file"
        else tmp_path / "server"
    )
    real_is_link_or_junction = field_evidence._is_link_or_junction

    def simulated_reparse(path: Path) -> bool:
        return Path(os.path.abspath(path)) == Path(os.path.abspath(linked_path)) or (
            real_is_link_or_junction(path)
        )

    monkeypatch.setattr(
        field_evidence, "_is_link_or_junction", simulated_reparse
    )

    with pytest.raises(field_evidence.FieldEvidenceError, match="source|link/junction"):
        field_evidence.hash_field_sources(tmp_path)


def test_end_snapshot_rejects_a_source_file_added_then_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sources(tmp_path)
    monkeypatch.setenv("COMPUTERNAME", "SCHOOL-PC")
    real_datetime = dt.datetime
    local_zone = dt.timezone(dt.timedelta(hours=9))

    class MutableDateTime(real_datetime):
        current = real_datetime(2026, 9, 8, 8, 0, tzinfo=local_zone)

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> MutableDateTime:
            return cls.fromtimestamp(cls.current.timestamp(), tz or cls.current.tzinfo)

    monkeypatch.setattr(field_evidence.dt, "datetime", MutableDateTime)
    field_evidence.write_field_source_snapshot(
        root=tmp_path, field_date=FIELD_DATE, phase="start"
    )
    transient = tmp_path / "server" / "transient-source.py"
    transient.write_text("temporary source\n", encoding="utf-8")
    transient.unlink()
    MutableDateTime.current = real_datetime(2026, 9, 8, 9, 0, tzinfo=local_zone)

    with pytest.raises(field_evidence.FieldEvidenceError, match="source"):
        field_evidence.write_field_source_snapshot(
            root=tmp_path, field_date=FIELD_DATE, phase="end"
        )


def test_source_snapshot_action_accepts_end_on_the_next_local_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sources(tmp_path)
    monkeypatch.setenv("COMPUTERNAME", "SCHOOL-PC")
    real_datetime = dt.datetime
    local_zone = dt.timezone(dt.timedelta(hours=9))

    class MutableDateTime(dt.datetime):
        current: dt.datetime = real_datetime(
            2026, 9, 8, 8, 0, tzinfo=local_zone
        )

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> MutableDateTime:
            return cls.fromtimestamp(
                cls.current.timestamp(), tz or cls.current.tzinfo
            )

    monkeypatch.setattr(field_evidence.dt, "datetime", MutableDateTime)
    field_date = MutableDateTime.current.date().isoformat()
    start_result = field_evidence.write_field_source_snapshot(
        root=tmp_path, field_date=field_date, phase="start"
    )

    MutableDateTime.current = real_datetime(2026, 9, 9, 17, 0, tzinfo=local_zone)
    end_result = field_evidence.write_field_source_snapshot(
        root=tmp_path, field_date=field_date, phase="end"
    )

    start_path = Path(start_result["snapshot_path"])
    end_path = Path(end_result["snapshot_path"])
    start = json.loads(start_path.read_text(encoding="utf-8"))
    end = json.loads(end_path.read_text(encoding="utf-8"))
    assert end["field_date"] == field_date
    assert end["captured_at"].startswith("2026-09-09T17:00:00")
    assert end["session_id"] == start["session_id"]
    assert end["host_fingerprint_sha256"] == start["host_fingerprint_sha256"]
    assert end["source_hashes"] == start["source_hashes"]
    assert end["source_revisions"] == start["source_revisions"]
    assert set(start["source_revisions"]) == {"files", "directories"}
    assert set(start["source_revisions"]["files"]) == set(start["source_hashes"])
    assert start["source_revisions"]["directories"]
    assert all(
        set(revision) == field_evidence.SOURCE_REVISION_KEYS
        for index in start["source_revisions"].values()
        for revision in index.values()
    )
    assert start["source_snapshot_sha256"] == field_evidence._source_snapshot_sha256(
        start["source_hashes"], start["source_revisions"]
    )
    assert end["predecessor_sha256"] == _sha256(start_path)

    field_evidence.rewrite_outer_manifest(tmp_path, field_date)
    outer = field_evidence.validate_outer_manifest(tmp_path, field_date)
    validated = field_evidence._validate_source_snapshot_pair(
        root=tmp_path,
        field_date=field_date,
        evidence_dir=tmp_path / "docs" / "field" / "evidence" / field_date,
        manifest_files=outer["files"],
        start_relative=start_path.relative_to(tmp_path).as_posix(),
        end_relative=end_path.relative_to(tmp_path).as_posix(),
        current_sources=field_evidence.hash_field_sources(tmp_path),
    )
    assert validated["start_captured_at"] == start["captured_at"]
    assert validated["end_captured_at"] == end["captured_at"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("session", "start/end source snapshot"),
        ("predecessor", "start/end source snapshot"),
        ("chronology", "start/end source snapshot"),
    ],
)
def test_capture_rejects_semantically_broken_source_snapshot_pair_after_reseal(
    field_repo: tuple[Path, dict[str, str]], mutation: str, message: str
) -> None:
    root, environ = field_repo
    start_path = root / environ["F_SOURCE_SNAPSHOT_START"]
    end_path = root / environ["F_SOURCE_SNAPSHOT_END"]
    start = json.loads(start_path.read_text(encoding="utf-8"))
    end = json.loads(end_path.read_text(encoding="utf-8"))
    if mutation == "session":
        end["session_id"] = "c" * 64
    elif mutation == "predecessor":
        end["predecessor_sha256"] = "d" * 64
    else:
        end["captured_at"] = start["captured_at"]
    end_path.write_text(
        json.dumps(end, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    with pytest.raises(field_evidence.FieldEvidenceError, match=message):
        field_evidence.capture_from_environment(root=root, environ=environ)


def test_source_snapshot_pair_rejects_a_nonfixed_reference(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = field_repo
    outer = field_evidence.validate_outer_manifest(root, FIELD_DATE)

    with pytest.raises(field_evidence.FieldEvidenceError, match="固定path"):
        field_evidence._validate_source_snapshot_pair(
            root=root,
            field_date=FIELD_DATE,
            evidence_dir=root / EVIDENCE_RELATIVE,
            manifest_files=outer["files"],
            start_relative=f"{EVIDENCE_RELATIVE}/renamed-start.json",
            end_relative=environ["F_SOURCE_SNAPSHOT_END"],
            current_sources=field_evidence.hash_field_sources(root),
        )


def test_source_snapshot_parser_rejects_legacy_schema_one(
    tmp_path: Path,
) -> None:
    _create_sources(tmp_path)
    start_relative, _end_relative = _write_source_snapshot_pair(tmp_path)
    start_path = tmp_path / start_relative
    start = json.loads(start_path.read_text(encoding="utf-8"))
    start["schema_version"] = 1
    start.pop("source_revisions")
    start["source_snapshot_sha256"] = field_evidence._canonical_sha256(
        start["source_hashes"]
    )
    start_path.write_text(
        json.dumps(start, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(field_evidence.FieldEvidenceError, match="schema"):
        field_evidence._parse_source_snapshot(
            start_path, field_date=FIELD_DATE, phase="start"
        )


@pytest.mark.parametrize(
    "mutation",
    ("missing_group", "extra_component", "boolean_component", "wrong_file_set"),
)
def test_source_snapshot_parser_rejects_untrusted_revision_shapes(
    tmp_path: Path, mutation: str
) -> None:
    _create_sources(tmp_path)
    start_relative, _end_relative = _write_source_snapshot_pair(tmp_path)
    start_path = tmp_path / start_relative
    start = json.loads(start_path.read_text(encoding="utf-8"))
    revisions = start["source_revisions"]
    first_file = next(iter(revisions["files"]))
    if mutation == "missing_group":
        revisions.pop("directories")
    elif mutation == "extra_component":
        revisions["files"][first_file]["generation"] = 1
    elif mutation == "boolean_component":
        revisions["files"][first_file]["mtime_ns"] = True
    else:
        revisions["files"].pop(first_file)
    start_path.write_text(
        json.dumps(start, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(field_evidence.FieldEvidenceError, match="revision"):
        field_evidence._parse_source_snapshot(
            start_path, field_date=FIELD_DATE, phase="start"
        )


def test_source_snapshot_digest_binds_valid_revision_values(tmp_path: Path) -> None:
    _create_sources(tmp_path)
    start_relative, _end_relative = _write_source_snapshot_pair(tmp_path)
    start_path = tmp_path / start_relative
    start = json.loads(start_path.read_text(encoding="utf-8"))
    first_file = next(iter(start["source_revisions"]["files"]))
    start["source_revisions"]["files"][first_file]["mtime_ns"] += 1
    start_path.write_text(
        json.dumps(start, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(field_evidence.FieldEvidenceError, match="binding"):
        field_evidence._parse_source_snapshot(
            start_path, field_date=FIELD_DATE, phase="start"
        )


def test_source_snapshot_pair_rejects_plain_current_hash_mapping(
    tmp_path: Path,
) -> None:
    _create_sources(tmp_path)
    start_relative, end_relative = _write_source_snapshot_pair(tmp_path)
    field_evidence.rewrite_outer_manifest(tmp_path, FIELD_DATE)
    outer = field_evidence.validate_outer_manifest(tmp_path, FIELD_DATE)
    current = field_evidence.hash_field_sources(tmp_path)

    with pytest.raises(field_evidence.FieldEvidenceError, match="intact"):
        field_evidence._validate_source_snapshot_pair(
            root=tmp_path,
            field_date=FIELD_DATE,
            evidence_dir=tmp_path / EVIDENCE_RELATIVE,
            manifest_files=outer["files"],
            start_relative=start_relative,
            end_relative=end_relative,
            current_sources=dict(current),
        )


def test_quality_loader_accepts_schema_two_source_revision_binding(
    tmp_path: Path,
) -> None:
    _create_sources(tmp_path)
    start_relative, end_relative = _write_source_snapshot_pair(tmp_path)

    binding, fingerprints = field_evidence.quality_evidence._load_source_snapshot_pair(
        root=tmp_path,
        field_date=FIELD_DATE,
        start_path=tmp_path / start_relative,
        end_path=tmp_path / end_relative,
        manifest_files=None,
    )

    assert binding["session_id"] == "b" * 64
    assert len(fingerprints) == 2


@pytest.mark.parametrize("mutation", ("legacy", "boolean_revision", "extra_revision"))
def test_quality_loader_rejects_legacy_or_untrusted_source_revision_binding(
    tmp_path: Path, mutation: str
) -> None:
    _create_sources(tmp_path)
    start_relative, end_relative = _write_source_snapshot_pair(tmp_path)
    start_path = tmp_path / start_relative
    start = json.loads(start_path.read_text(encoding="utf-8"))
    if mutation == "legacy":
        start["schema_version"] = 1
        start.pop("source_revisions")
        start["source_snapshot_sha256"] = field_evidence._canonical_sha256(
            start["source_hashes"]
        )
    else:
        first_file = next(iter(start["source_revisions"]["files"]))
        if mutation == "boolean_revision":
            start["source_revisions"]["files"][first_file]["ctime_ns"] = True
        else:
            start["source_revisions"]["files"][first_file]["generation"] = 1
    start_path.write_text(
        json.dumps(start, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        field_evidence.quality_evidence.QualityEvidenceError,
        match="schema|revision",
    ):
        field_evidence.quality_evidence._load_source_snapshot_pair(
            root=tmp_path,
            field_date=FIELD_DATE,
            start_path=start_path,
            end_path=tmp_path / end_relative,
            manifest_files=None,
        )


def test_quality_loader_digest_binds_source_revision_values(tmp_path: Path) -> None:
    _create_sources(tmp_path)
    start_relative, end_relative = _write_source_snapshot_pair(tmp_path)
    start_path = tmp_path / start_relative
    start = json.loads(start_path.read_text(encoding="utf-8"))
    first_file = next(iter(start["source_revisions"]["files"]))
    start["source_revisions"]["files"][first_file]["mtime_ns"] += 1
    start_path.write_text(
        json.dumps(start, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        field_evidence.quality_evidence.QualityEvidenceError,
        match="binding is invalid",
    ):
        field_evidence.quality_evidence._load_source_snapshot_pair(
            root=tmp_path,
            field_date=FIELD_DATE,
            start_path=start_path,
            end_path=tmp_path / end_relative,
            manifest_files=None,
        )


def test_source_snapshot_action_refuses_to_overwrite_fixed_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sources(tmp_path)
    monkeypatch.setenv("COMPUTERNAME", "SCHOOL-PC")
    _freeze_field_evidence_now(monkeypatch)
    field_evidence.write_field_source_snapshot(
        root=tmp_path, field_date=FIELD_DATE, phase="start"
    )

    with pytest.raises(field_evidence.FieldEvidenceError, match="上書き"):
        field_evidence.write_field_source_snapshot(
            root=tmp_path, field_date=FIELD_DATE, phase="start"
        )


def test_source_snapshot_action_rejects_a_noncurrent_field_date_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_sources(tmp_path)
    monkeypatch.setenv("COMPUTERNAME", "SCHOOL-PC")
    stale_date = "2026-09-07"

    with pytest.raises(field_evidence.FieldEvidenceError, match="current local date"):
        field_evidence.write_field_source_snapshot(
            root=tmp_path, field_date=stale_date, phase="start"
        )

    assert not (
        tmp_path
        / "docs"
        / "field"
        / "evidence"
        / stale_date
        / field_evidence.SOURCE_SNAPSHOT_FILENAMES["start"]
    ).exists()


def test_capture_rejects_old_source_evidence_relabelled_to_the_field_date(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = field_repo
    start_path = root / environ["F_SOURCE_SNAPSHOT_START"]
    end_path = root / environ["F_SOURCE_SNAPSHOT_END"]
    start = json.loads(start_path.read_text(encoding="utf-8"))
    end = json.loads(end_path.read_text(encoding="utf-8"))
    start["captured_at"] = "2026-09-07T00:00:00+09:00"
    end["captured_at"] = "2026-09-07T10:00:00+09:00"
    start_path.write_text(
        json.dumps(start, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    end["predecessor_sha256"] = _sha256(start_path)
    end_path.write_text(
        json.dumps(end, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    with pytest.raises(field_evidence.FieldEvidenceError, match="source snapshot binding"):
        field_evidence.capture_from_environment(root=root, environ=environ)


def test_validate_rejects_current_source_tampering(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = _capture(field_repo)
    (root / "server/main.py").write_text("changed after capture\n", encoding="utf-8")

    with pytest.raises(field_evidence.FieldEvidenceError, match="source snapshot"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_validate_rechecks_source_after_the_final_outer_manifest_read(
    field_repo: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _capture(field_repo)
    real_validate_outer_manifest = field_evidence.validate_outer_manifest
    calls = 0

    def mutate_source_after_final_outer(
        checked_root: Path, field_date: str
    ) -> dict[str, object]:
        nonlocal calls
        result = real_validate_outer_manifest(checked_root, field_date)
        calls += 1
        if calls == 2:
            (root / "server" / "main.py").write_text(
                "changed after final source snapshot\n", encoding="utf-8"
            )
        return result

    monkeypatch.setattr(
        field_evidence, "validate_outer_manifest", mutate_source_after_final_outer
    )

    with pytest.raises(field_evidence.FieldEvidenceError, match="source|changed"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_validate_rechecks_an_early_outer_file_after_later_manifest_work(
    field_repo: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, environ = _capture(field_repo)
    early_artifact = root / environ["F_SETUP_EVIDENCE"]
    real_parse_manifest = field_evidence._parse_manifest
    calls = 0

    def mutate_early_artifact_after_manifest_parse(
        path: Path, expected_prefix: str
    ) -> dict[str, str]:
        nonlocal calls
        result = real_parse_manifest(path, expected_prefix)
        calls += 1
        if calls == 2:
            early_artifact.write_text(
                "changed after its final hash\n", encoding="utf-8"
            )
        return result

    monkeypatch.setattr(field_evidence, "_parse_manifest", mutate_early_artifact_after_manifest_parse)

    with pytest.raises(field_evidence.FieldEvidenceError, match="changed|manifest"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_validated_bundle_exposes_a_nonserialized_recheckable_witness(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = _capture(field_repo)
    result = field_evidence.validate_field_bundle(
        root / EVIDENCE_RELATIVE, root=root
    )

    assert isinstance(result, field_evidence.ValidatedFieldBundle)
    assert result.validation_witness.file_revisions
    assert result.validation_witness.directory_revisions
    assert "validation_witness" not in result
    assert result == dict(result)
    field_evidence.recheck_validation_witness(result.validation_witness)

    (root / "server" / "main.py").write_text(
        "changed after validation\n", encoding="utf-8"
    )
    with pytest.raises(field_evidence.FieldEvidenceError, match="changed"):
        field_evidence.recheck_validation_witness(result.validation_witness)


def test_bundle_witness_rechecks_the_bundle_receipt_itself(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = _capture(field_repo)
    result = field_evidence.validate_field_bundle(
        root / EVIDENCE_RELATIVE, root=root
    )
    bundle = root / EVIDENCE_RELATIVE / field_evidence.BUNDLE_NAME
    bundle.write_bytes(bundle.read_bytes() + b" ")

    with pytest.raises(field_evidence.FieldEvidenceError, match="changed"):
        field_evidence.recheck_validation_witness(result.validation_witness)


def test_bundle_witness_rechecks_the_current_source_set(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = _capture(field_repo)
    result = field_evidence.validate_field_bundle(
        root / EVIDENCE_RELATIVE, root=root
    )
    (root / "server" / "added-after-validation.py").write_text(
        "new source\n", encoding="utf-8"
    )

    with pytest.raises(field_evidence.FieldEvidenceError, match="directory changed"):
        field_evidence.recheck_validation_witness(result.validation_witness)


def test_bundle_witness_rejects_same_size_source_overwrite_with_restored_mtime(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = _capture(field_repo)
    result = field_evidence.validate_field_bundle(
        root / EVIDENCE_RELATIVE, root=root
    )
    source = root / "server" / "main.py"
    before = source.stat()
    original = source.read_bytes()
    replacement = bytes(byte ^ 1 for byte in original)
    assert len(replacement) == len(original)
    source.write_bytes(replacement)
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

    with pytest.raises(field_evidence.FieldEvidenceError, match="changed"):
        field_evidence.recheck_validation_witness(result.validation_witness)


def test_bundle_witness_rejects_same_size_source_aba_with_restored_mtime(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = _capture(field_repo)
    result = field_evidence.validate_field_bundle(
        root / EVIDENCE_RELATIVE, root=root
    )
    source = root / "server" / "main.py"
    before = source.stat()
    original = source.read_bytes()
    source.write_bytes(bytes(byte ^ 1 for byte in original))
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    source.write_bytes(original)
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

    with pytest.raises(field_evidence.FieldEvidenceError, match="changed"):
        field_evidence.recheck_validation_witness(result.validation_witness)


def test_bundle_witness_rejects_directory_add_with_restored_mtime(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = _capture(field_repo)
    result = field_evidence.validate_field_bundle(
        root / EVIDENCE_RELATIVE, root=root
    )
    source_dir = root / "server"
    before = source_dir.stat()
    (source_dir / "added-with-restored-directory-time.py").write_text(
        "new source\n", encoding="utf-8"
    )
    os.utime(source_dir, ns=(before.st_atime_ns, before.st_mtime_ns))

    with pytest.raises(field_evidence.FieldEvidenceError, match="changed"):
        field_evidence.recheck_validation_witness(result.validation_witness)


def test_validated_outer_manifest_exposes_a_recheckable_witness(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = field_repo
    result = field_evidence.validate_outer_manifest(root, FIELD_DATE)

    assert isinstance(result, field_evidence.ValidatedOuterManifest)
    assert "validation_witness" not in result
    field_evidence.recheck_validation_witness(result.validation_witness)

    (root / environ["F_SETUP_EVIDENCE"]).write_text(
        "changed after outer validation\n", encoding="utf-8"
    )
    with pytest.raises(field_evidence.FieldEvidenceError, match="changed"):
        field_evidence.recheck_validation_witness(result.validation_witness)


def test_outer_manifest_rejects_hardlink_aliases_across_story_evidence(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = field_repo
    first = root / environ["F_SETUP_EVIDENCE"]
    alias = root / environ["F_RETRY_DONE_STATE"]
    alias.unlink()
    try:
        os.link(first, alias)
    except OSError as exc:
        pytest.skip(f"hardlinks are unavailable on this test filesystem: {exc}")
    evidence_dir = root / EVIDENCE_RELATIVE
    files = sorted(
        (path for path in evidence_dir.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix().encode("utf-8"),
    )
    (root / MANIFEST_RELATIVE).write_text(
        "".join(
            f"{_sha256(path)}  {path.relative_to(root).as_posix()}\n"
            for path in files
        ),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(field_evidence.FieldEvidenceError, match="hardlink|alias"):
        field_evidence.validate_outer_manifest(root, FIELD_DATE)


def test_witness_recheck_rejects_an_untrusted_shape() -> None:
    witness = field_evidence.FieldValidationWitness(
        file_revisions=(("relative/file", (1, 2, 3, 4, 5)),),
        directory_revisions=(("relative/directory", (1, 2, 3, 4, 5)),),
    )

    with pytest.raises(field_evidence.FieldEvidenceError, match="entry is invalid"):
        field_evidence.recheck_validation_witness(witness)


def test_imported_validator_source_binding_rejects_aba(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = tuple(tmp_path / f"validator-{index}.py" for index in range(3))
    for path in paths:
        path.write_text("validator source\n", encoding="utf-8")
    baseline = field_evidence.snapshot_supplied_source_hashes(
        tmp_path, lambda: paths
    )
    monkeypatch.setattr(field_evidence, "ROOT", tmp_path)
    monkeypatch.setattr(field_evidence, "_IMPORTED_VALIDATOR_PATHS", paths)
    monkeypatch.setattr(
        field_evidence, "_IMPORTED_VALIDATOR_SOURCE_SNAPSHOT", baseline
    )
    field_evidence._recheck_imported_field_module()
    original = paths[1].read_bytes()
    changed = paths[1].with_suffix(".changed")
    changed.write_bytes(b"changed validator\n")
    os.replace(changed, paths[1])
    restored = paths[1].with_suffix(".restored")
    restored.write_bytes(original)
    os.replace(restored, paths[1])

    with pytest.raises(field_evidence.FieldEvidenceError, match="imported"):
        field_evidence._recheck_imported_field_module()


def test_imported_validator_binding_includes_the_shared_qa_evidence_helper() -> None:
    helper_path = Path(
        os.path.abspath(field_evidence.qa_evidence_manifest.__file__)
    )

    assert helper_path in field_evidence._IMPORTED_VALIDATOR_PATHS


def test_validate_rejects_manifested_artifact_tampering(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = _capture(field_repo)
    (root / environ["F_SETUP_EVIDENCE"]).write_text(
        "changed after manifest\n", encoding="utf-8"
    )

    with pytest.raises(field_evidence.FieldEvidenceError, match="manifestがtreeと一致"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_validate_rejects_perf01_artifact_tampering_even_after_outer_rehash(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = _capture(field_repo)
    (root / environ["F_C4_EVIDENCE"]).write_text(
        "run_id\tseq\nforged\t1\n", encoding="utf-8"
    )
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    with pytest.raises(field_evidence.FieldEvidenceError, match="PERF-01 package validator"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_validate_rejects_modified_saved_perf01_validation(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = _capture(field_repo)
    path = root / environ["F_C4_VALIDATION"]
    validation = json.loads(path.read_text(encoding="utf-8"))
    validation["metrics"]["final_count"] = 30
    path.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    with pytest.raises(field_evidence.FieldEvidenceError, match="保存validation"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_validate_rejects_perf01_run_tampering_even_after_outer_rehash(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = _capture(field_repo)
    path = root / environ["F_C4_RUN"]
    run = json.loads(path.read_text(encoding="utf-8"))
    run["input"]["description"] = "tampered description"
    path.write_text(
        json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    with pytest.raises(field_evidence.FieldEvidenceError, match="保存validation"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


@pytest.mark.parametrize(
    "field",
    ("field_session_id", "host_fingerprint_sha256", "source_snapshot_sha256"),
)
def test_validate_rejects_resealed_perf01_source_binding_tamper(
    field_repo: tuple[Path, dict[str, str]], field: str
) -> None:
    root, environ = _capture(field_repo)
    run_path = root / environ["F_C4_RUN"]
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run[field] = "e" * 64
    run_path.write_text(
        json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    with pytest.raises(field_evidence.FieldEvidenceError, match="package validator"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_validate_rejects_resealed_perf01_window_outside_end_snapshot(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = _capture(field_repo)
    run_path = root / environ["F_C4_RUN"]
    run = json.loads(run_path.read_text(encoding="utf-8"))
    outside = "2026-09-08T10:00:01+09:00"
    run["measurement_window"]["observation_ended_at"] = outside
    run["population"]["observed_until_at"] = outside
    run_path.write_text(
        json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    validation = perf01_evidence.validate_perf01_run(run_path, root=root)
    assert validation["passed"] is True
    (root / environ["F_C4_VALIDATION"]).write_text(
        json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    with pytest.raises(field_evidence.FieldEvidenceError, match="outside the source snapshot"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


@pytest.mark.parametrize(
    ("seconds", "shell_minutes"),
    [
        (Decimal("2701"), "45.0166666666667"),
        (Decimal("2700.123457"), "45.0020576166667"),
    ],
)
def test_capture_accepts_awk_rounded_perf01_minutes(
    field_repo: tuple[Path, dict[str, str]],
    seconds: Decimal,
    shell_minutes: str,
) -> None:
    root, environ = field_repo
    environ.update(
        _write_perf01_run(
            root,
            audio_seconds=seconds,
            minutes_text=shell_minutes,
        )
    )
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    result = field_evidence.capture_from_environment(root=root, environ=environ)

    assert result["field_complete"] is True


def test_capture_rejects_perf01_minutes_outside_rounding_tolerance(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = field_repo
    environ.update(
        _write_perf01_run(
            root,
            audio_seconds=Decimal("2701"),
            minutes_text="45.0166666667",
        )
    )
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    with pytest.raises(field_evidence.FieldEvidenceError, match="sidecar"):
        field_evidence.capture_from_environment(root=root, environ=environ)


def test_validate_rejects_perf01_sidecar_that_changes_31_finals_to_30(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = field_repo
    environ.update(_write_perf01_run(root, final_count=31))
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)
    _capture((root, environ))

    def mutate(payload: dict[str, object]) -> None:
        stories = payload["stories"]
        assert isinstance(stories, dict)
        entry = stories["PERF-01"]
        assert isinstance(entry, dict)
        details = entry["details"]
        assert isinstance(details, dict)
        details["F_C4_FINALS"] = "30"
        details["F_C4_EXPECTED"] = "60"
        details["F_C4_MATCHED"] = "60"

    _rewrite_bundle(root, mutate)

    with pytest.raises(field_evidence.FieldEvidenceError, match="sidecar詳細"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


@pytest.mark.parametrize("invalid", ["duplicate", "nan"])
def test_validate_uses_strict_json_for_duplicate_keys_and_nan(
    field_repo: tuple[Path, dict[str, str]], invalid: str
) -> None:
    root, _ = _capture(field_repo)
    bundle = root / EVIDENCE_RELATIVE / field_evidence.BUNDLE_NAME
    if invalid == "duplicate":
        text = bundle.read_text(encoding="utf-8").replace(
            "{\n", '{\n  "schema_version": 1,\n', 1
        )
    else:
        text = bundle.read_text(encoding="utf-8").replace(
            '"field_complete": true', '"field_complete": NaN', 1
        )
    bundle.write_text(text, encoding="utf-8")

    with pytest.raises(
        field_evidence.FieldEvidenceError,
        match="重複" if invalid == "duplicate" else "非有限",
    ):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_validate_rejects_traversal_in_a_story_reference(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = _capture(field_repo)
    traversal = f"{EVIDENCE_RELATIVE}/../outside.txt"

    def mutate(payload: dict[str, object]) -> None:
        stories = payload["stories"]
        assert isinstance(stories, dict)
        entry = stories["OPS-01"]
        assert isinstance(entry, dict)
        details = entry["details"]
        assert isinstance(details, dict)
        details["F_SETUP_EVIDENCE"] = traversal
        entry["evidence"] = [traversal]

    _rewrite_bundle(root, mutate)

    with pytest.raises(field_evidence.FieldEvidenceError, match="不正なrepo相対path"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_validate_rejects_evidence_list_that_disagrees_with_details(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = _capture(field_repo)

    def mutate(payload: dict[str, object]) -> None:
        stories = payload["stories"]
        assert isinstance(stories, dict)
        entry = stories["OPS-01"]
        assert isinstance(entry, dict)
        entry["evidence"] = [environ["F_OPS10_EVIDENCE"]]

    _rewrite_bundle(root, mutate)

    with pytest.raises(field_evidence.FieldEvidenceError, match="evidence一覧"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("F_OPS01", "否", "OPS-01のshell gate"),
        ("F_STORIES_COMPLETE", "否", "shellの総合集計"),
        ("F_FIELD_COMPLETE", "否", "shellの総合集計"),
    ],
)
def test_capture_rejects_shell_status_that_disagrees_with_recalculation(
    field_repo: tuple[Path, dict[str, str]],
    key: str,
    value: str,
    message: str,
) -> None:
    root, environ = field_repo
    environ[key] = value

    with pytest.raises(field_evidence.FieldEvidenceError, match=message):
        field_evidence.capture_from_environment(root=root, environ=environ)

    assert not (root / EVIDENCE_RELATIVE / field_evidence.BUNDLE_NAME).exists()


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("F_C2_SEC", "9e1", "OPS-12のshell gate"),
        ("F_C4_MED", "5e0", "PERF-01のshell gate"),
        ("F_ACC_RSS", "5000.1", "PERF-02のshell gate"),
        ("F_ACC_CPU", "-1", "cache-off補助gate"),
    ],
)
def test_capture_recomputes_strict_numeric_field_gates(
    field_repo: tuple[Path, dict[str, str]],
    key: str,
    value: str,
    message: str,
) -> None:
    root, environ = field_repo
    environ[key] = value

    with pytest.raises(field_evidence.FieldEvidenceError, match=message):
        field_evidence.capture_from_environment(root=root, environ=environ)

    assert not (root / EVIDENCE_RELATIVE / field_evidence.BUNDLE_NAME).exists()


def test_validate_recomputes_quality_averages_from_the_sealed_tsv(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = _capture(field_repo)

    def mutate(payload: dict[str, object]) -> None:
        stories = payload["stories"]
        assert isinstance(stories, dict)
        entry = stories["QUAL-01"]
        assert isinstance(entry, dict)
        details = entry["details"]
        assert isinstance(details, dict)
        details["F_C6_SCORE"] = "5.00"

    _rewrite_bundle(root, mutate)

    with pytest.raises(field_evidence.FieldEvidenceError, match="集計値"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_validate_rejects_nested_quality_artifact_tamper_after_outer_rehash(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = _capture(field_repo)
    receipt = root / EVIDENCE_RELATIVE / "qual-01/eval-en-review.pdf"
    receipt.write_bytes(b"forged review receipt")
    field_evidence.rewrite_outer_manifest(root, FIELD_DATE)

    with pytest.raises(field_evidence.FieldEvidenceError, match="package validator"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_validate_rejects_spoofed_quality_package_metric_in_sidecar(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = _capture(field_repo)

    def mutate(payload: dict[str, object]) -> None:
        stories = payload["stories"]
        assert isinstance(stories, dict)
        entry = stories["QUAL-01"]
        assert isinstance(entry, dict)
        details = entry["details"]
        assert isinstance(details, dict)
        details["F_C6_ASR_ERRORS"] = "29"

    _rewrite_bundle(root, mutate)

    with pytest.raises(field_evidence.FieldEvidenceError, match="集計値"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_validate_rejects_stale_quality_review_receipt_timeline(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, environ = _capture(field_repo)
    provenance_path = root / environ["F_C6_PROVENANCE"]
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    evaluator = next(
        item for item in provenance["evaluators"] if item["id"] == "eval-en"
    )
    evaluator["review_started_at"] = "2026-09-08T01:59:00+09:00"
    evaluator["review_finished_at"] = "2026-09-08T01:59:30+09:00"
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    new_sha256 = _sha256(provenance_path)

    def mutate(payload: dict[str, object]) -> None:
        stories = payload["stories"]
        assert isinstance(stories, dict)
        entry = stories["QUAL-01"]
        assert isinstance(entry, dict)
        details = entry["details"]
        assert isinstance(details, dict)
        details["F_C6_PROVENANCE_SHA"] = new_sha256

    _rewrite_bundle(root, mutate)

    with pytest.raises(field_evidence.FieldEvidenceError, match="review timeline"):
        field_evidence.validate_field_bundle(root / EVIDENCE_RELATIVE, root=root)


def test_field_source_files_cover_product_and_evidence_gates_but_skip_scratch(
    field_repo: tuple[Path, dict[str, str]],
) -> None:
    root, _ = field_repo
    relative = {
        path.relative_to(root).as_posix()
        for path in field_evidence.field_source_files(root)
    }

    assert {
        "server/main.py",
        "web/student.js",
        "scripts/field-verification.sh",
        "scripts/field_story_evidence.py",
        "scripts/perf01_evidence.py",
        "scripts/quality_evidence.py",
        "scripts/validate_field_performance.py",
        "start.bat",
        "setup.ps1",
        "config.yaml",
        "requirements.txt",
        "requirements-bench.txt",
        "docs/qa/stories.tsv",
        "docs/field-verification-checklist.md",
    } <= relative
    assert "scripts/.generated.py" not in relative
    assert "server/__pycache__/cached.py" not in relative
    assert "web/ignored.pyc" not in relative


def test_wizard_perf01_uses_validator_summary_instead_of_manual_aggregation() -> None:
    script = Path("scripts/field-verification.sh").read_text(encoding="utf-8")
    phase = script.split(
        'stage "Phase C-4 — 実ブラウザ10台で45分の表示遅延を全件測る"', 1
    )[1].split('stage "Phase C-5', 1)[0]

    assert "perf01_validation_summary" in phase
    assert "${F_DATE}-perf01-run.json" in phase
    assert "${F_DATE}-perf01-validation.json" in phase
    assert "${#PERF01_SUMMARY_FIELDS[@]} -eq 10" in script
    assert "command_status != 0" in script
    assert "last_byte" in script
    assert "ask F_C4_FINALS" not in phase
    assert "ask F_C4_MATCHED" not in phase
    assert "ask F_C4_MED" not in phase
    assert "ask F_C4_MAX" not in phase
    assert "F_C4_EXPECTED=$((" not in phase
    assert "awk -v minutes=" not in phase


def test_wizard_quality_package_uses_fail_closed_fifteen_field_contract() -> None:
    script = Path("scripts/field-verification.sh").read_text(encoding="utf-8")
    stage16 = script.split('stage "総合判定とレポート生成"', 1)[1]

    assert "quality_package_summary" in stage16
    assert "--source-snapshot-start" in script
    assert "--source-snapshot-end" in script
    assert "--manifest" in script
    assert "${#QUALITY_PACKAGE_FIELDS[@]} -eq 15" in script
    assert "command_status != 0" in script
    assert "last_byte" in script
    assert 'F_C6_ASR_ERRORS="$QUALITY_PACKAGE_ASR_ERRORS"' in stage16
    assert 'F_C6_MEASURED="可"' in stage16
    assert 'F_C6_PROVENANCE_PASSED="true"' in stage16


def _bash_executable() -> str:
    candidates = [
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(found) if (found := shutil.which("bash")) else None,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return str(candidate)
    pytest.skip("Bash is unavailable for the wizard parser integration test")


def _write_fake_python(root: Path) -> None:
    fake = root / "fake-bin" / "python3"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        """#!/usr/bin/env bash
if [[ " $* " == *" --format shell "* ]]; then
  printf '%s' "${FAKE_SHELL_OUTPUT-}"
  exit "${FAKE_SHELL_STATUS-0}"
fi
if [[ " $* " == *" --format json "* ]]; then
  printf '%s' "${FAKE_JSON_OUTPUT-{}}"
  exit "${FAKE_JSON_STATUS-0}"
fi
printf '%s\n' "${FAKE_ASR_OUTPUT-0}"
exit "${FAKE_ASR_STATUS-0}"
""",
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(fake, 0o755)


def _run_wizard_preflight(
    tmp_path: Path, *, python_status: int = 0
) -> subprocess.CompletedProcess[str]:
    script = Path("scripts/field-verification.sh").read_text(encoding="utf-8")
    function_start = "field_verification_preflight() {"
    function = (
        function_start
        + script.split(function_start, 1)[1].split("\n}\n", 1)[0]
        + "\n}\n"
    )
    _write_fake_python(tmp_path)
    harness = tmp_path / "preflight-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        "warn() { printf 'WARN: %s\\n' \"$1\"; }\n"
        "note() { printf 'NOTE: %s\\n' \"$1\"; }\n"
        'if command -v cygpath >/dev/null 2>&1; then\n'
        '  LB_ROOT="$(cygpath -u "$1")"\n'
        "else\n"
        '  LB_ROOT="$1"\n'
        "fi\n"
        'PATH="$LB_ROOT/fake-bin:$PATH"\n'
        + function
        + "if field_verification_preflight; then\n"
        + "  printf 'accepted\\n'\n"
        + "  exit 0\n"
        + "fi\n"
        + "printf 'rejected\\n'\n"
        + "exit 7\n",
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(harness, 0o755)
    environment = os.environ.copy()
    environment["FAKE_ASR_STATUS"] = str(python_status)
    return subprocess.run(
        [_bash_executable(), str(harness), tmp_path.as_posix()],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )


def test_wizard_preflight_rejects_missing_benchmark_requirements(
    tmp_path: Path,
) -> None:
    result = _run_wizard_preflight(tmp_path)

    assert result.returncode == 7, result.stderr
    assert "requirements-bench.txt がありません" in result.stdout
    assert "完全な checkout または配布物を配置し直して" in result.stdout
    assert result.stdout.endswith("rejected\n")


def test_wizard_preflight_rejects_missing_psutil_with_install_command(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements-bench.txt").write_text(
        "psutil>=5.9\n", encoding="utf-8"
    )
    result = _run_wizard_preflight(tmp_path, python_status=1)

    assert result.returncode == 7, result.stderr
    assert "必須依存 psutil がありません" in result.stdout
    assert (
        r".\.venv\Scripts\python.exe -m pip install -r .\requirements-bench.txt"
        in result.stdout
    )
    assert "学校PCでも性能run前に" in result.stdout
    assert result.stdout.endswith("rejected\n")


def test_wizard_preflight_accepts_complete_benchmark_dependencies(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements-bench.txt").write_text(
        "psutil>=5.9\n", encoding="utf-8"
    )
    result = _run_wizard_preflight(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "accepted\n"


def test_wizard_invokes_dependency_preflight_before_first_prompt() -> None:
    script = Path("scripts/field-verification.sh").read_text(encoding="utf-8")

    preflight_call = script.index("if ! field_verification_preflight; then")
    first_prompt = script.index('banner "LinguaBridge')
    assert preflight_call < first_prompt
    assert "exit 2" in script[preflight_call:first_prompt]


def _run_wizard_parser(
    tmp_path: Path,
    *,
    block_start: str,
    block_end: str,
    invocation: str,
    shell_output: str,
    shell_status: int = 0,
    quality: bool = False,
    asr_output: str = "0",
) -> subprocess.CompletedProcess[str]:
    script = Path("scripts/field-verification.sh").read_text(encoding="utf-8")
    block = block_start + script.split(block_start, 1)[1].split(block_end, 1)[0]
    _write_fake_python(tmp_path)
    evidence = tmp_path / "docs" / "field" / "evidence" / FIELD_DATE
    evidence.mkdir(parents=True)
    manifest = tmp_path / "docs" / "field" / f"{FIELD_DATE}-evidence-sha256.txt"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("sealed manifest\n", encoding="utf-8")
    setup = """
F_SOURCE_SNAPSHOT_START="docs/field/evidence/$F_DATE/school-source-start.json"
F_SOURCE_SNAPSHOT_END="docs/field/evidence/$F_DATE/school-source-end.json"
F_EVIDENCE_MANIFEST="docs/field/${F_DATE}-evidence-sha256.txt"
all_evidence_paths_ok() { return 0; }
""" if quality else "evidence_path_ok() { return 0; }\n"
    harness = tmp_path / "parser-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        f'F_DATE="{FIELD_DATE}"\n'
        'if command -v cygpath >/dev/null 2>&1; then\n'
        '  LB_ROOT="$(cygpath -u "$1")"\n'
        'else\n'
        '  LB_ROOT="$1"\n'
        'fi\n'
        'PATH="$LB_ROOT/fake-bin:$PATH"\n'
        + setup
        + block
        + "\n"
        + invocation,
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(harness, 0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "FAKE_SHELL_OUTPUT": shell_output,
            "FAKE_SHELL_STATUS": str(shell_status),
            "FAKE_JSON_OUTPUT": '{"passed":true}',
            "FAKE_JSON_STATUS": "0",
            "FAKE_ASR_OUTPUT": asr_output,
            "FAKE_ASR_STATUS": "0",
        }
    )
    return subprocess.run(
        [_bash_executable(), str(harness), tmp_path.as_posix()],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize(
    ("shell_output", "shell_status", "accepted"),
    [
        ("true\t2\t4\t4\t2701\t10\t5\t5\t5\t8\n", 0, True),
        ("true\t2\t4\t4\t2701\t10\t5\t5\t5\t8", 0, False),
        ("true\t2\t4\t4\t2701\t10\t5\t5\t5\t8\r\n", 0, False),
        ("true\t2\t4\t4\t2701\t10\t5\t5\t5\t8\nextra\n", 0, False),
        ("true\t2\t4\t4\t2701\t10\t5\t5\t5\n", 0, False),
        ("false\t2\t4\t4\t2701\t10\t5\t5\t5\t8\n", 0, False),
        ("true\t2\t4\t4\t2.701e3\t10\t5\t5\t5\t8\n", 0, False),
        ("true\t2\t4\t4\t2701\t10\t5\t5\t5\t8\n", 3, False),
    ],
)
def test_wizard_perf01_parser_dynamically_rejects_malformed_validator_output(
    tmp_path: Path, shell_output: str, shell_status: int, accepted: bool
) -> None:
    result = _run_wizard_parser(
        tmp_path,
        block_start="PERF01_SUMMARY_FIELDS=()",
        block_end="# quality_package_summary",
        invocation="""
if perf01_validation_summary \\
    "docs/field/evidence/$F_DATE/${F_DATE}-perf01-run.json" \\
    "docs/field/evidence/$F_DATE/${F_DATE}-perf01-validation.json"; then
  printf 'accepted:%s:%s\n' "${#PERF01_SUMMARY_FIELDS[@]}" "${PERF01_SUMMARY_FIELDS[4]}"
  exit 0
fi
printf 'rejected\n'
exit 7
""",
        shell_output=shell_output,
        shell_status=shell_status,
    )

    assert (result.returncode == 0) is accepted, result.stderr
    assert (result.stdout == "accepted:10:2701\n") is accepted


@pytest.mark.parametrize(
    ("shell_output", "shell_status", "asr_output", "accepted"),
    [
        (
            "true\t30\t30\t30\t4.00\t30\t3.50\t30\t30\t3\t3\t30\t30\t90\t"
            + "a" * 64
            + "\n",
            0,
            "0",
            True,
        ),
        (
            "true\t30\t30\t30\t4\t30\t3.50\t30\t30\t3\t3\t30\t30\t90\t"
            + "a" * 64
            + "\n",
            0,
            "0",
            False,
        ),
        (
            "true\t30\t30\t30\t4.00\t30\t3.50\t30\t30\t3\t3\t30\t30\t90\t"
            + "A" * 64
            + "\n",
            0,
            "0",
            False,
        ),
        (
            "true\t30\t30\t30\t4.00\t30\t3.50\t30\t30\t3\t3\t30\t30\t90\t"
            + "a" * 64
            + "\r\n",
            0,
            "0",
            False,
        ),
        (
            "true\t30\t30\t30\t4.00\t30\t3.50\t30\t30\t3\t3\t30\t30\t90\t"
            + "a" * 64
            + "\n",
            2,
            "0",
            False,
        ),
        (
            "true\t30\t30\t30\t4.00\t30\t3.50\t30\t30\t3\t3\t30\t30\t90\t"
            + "a" * 64
            + "\n",
            0,
            "not-an-integer",
            False,
        ),
    ],
)
def test_wizard_quality_parser_dynamically_rejects_malformed_validator_output(
    tmp_path: Path,
    shell_output: str,
    shell_status: int,
    asr_output: str,
    accepted: bool,
) -> None:
    result = _run_wizard_parser(
        tmp_path,
        block_start="QUALITY_PACKAGE_FIELDS=()",
        block_end="validation_json_passed()",
        invocation="""
if quality_package_summary \\
    "docs/field/evidence/$F_DATE/${F_DATE}-quality-30.tsv" \\
    "docs/field/evidence/$F_DATE/${F_DATE}-quality-provenance.json"; then
  printf 'accepted:%s:%s\n' "${#QUALITY_PACKAGE_FIELDS[@]}" "$QUALITY_PACKAGE_ASR_ERRORS"
  exit 0
fi
printf 'rejected\n'
exit 7
""",
        shell_output=shell_output,
        shell_status=shell_status,
        quality=True,
        asr_output=asr_output,
    )

    assert (result.returncode == 0) is accepted, result.stderr
    assert (result.stdout == "accepted:15:0\n") is accepted


def _run_wizard_sync_block(
    tmp_path: Path,
    *,
    stu11: str,
    cache_off: str,
    cache_off_validated: str = "可",
    approved_candidates: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    script = Path("scripts/field-verification.sh").read_text(encoding="utf-8")
    block_start = 'F_TRACKER_UPDATED="未実施"'
    block = block_start + script.split(block_start, 1)[1].split("F_PENDING=", 1)[0]
    if approved_candidates is None:
        approved_candidates = (
            [story for story in field_evidence.FIELD_STORIES if story != "STU-11"]
            if stu11 == "否"
            and cache_off == "可"
            and cache_off_validated == "可"
            else list(field_evidence.FIELD_STORIES)
            if cache_off == "可" and cache_off_validated == "可"
            else []
        )
    approved_array = " ".join(f'"{story}"' for story in approved_candidates)
    harness = tmp_path / "sync-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        'F_FIELD_BUNDLE="docs/field/evidence/run/field-story-evidence.json"\n'
        f'F_ACC_NOCACHE_OK="{cache_off}"\n'
        f'F_ACC_NOCACHE_VALIDATED="{cache_off_validated}"\n'
        'F_OPS01="可"\n'
        'F_OPS02="可"\n'
        'F_OPS10="可"\n'
        'F_C2="可"\n'
        f'F_STU11="{stu11}"\n'
        'F_C4="可"\n'
        'F_PERF02="可"\n'
        'F_C6="可"\n'
        'F_FIELD_COMPLETE="否"\n'
        'LB_ROOT="/repo"\n'
        'EXPECTED_EVIDENCE_DIR="/evidence"\n'
        'F_TESTER="reviewer"\n'
        'FIELD_PYTHON="record_sync"\n'
        f"FIELD_APPROVED_CANDIDATE_IDS=({approved_array})\n"
        "SKIPPED=()\n"
        "pause() { :; }\n"
        "confirm() { return 0; }\n"
        "note() { printf 'NOTE:%s\\n' \"$1\"; }\n"
        "warn() { printf 'WARN:%s\\n' \"$1\"; }\n"
        "record_sync() { printf 'SYNC:%s\\n' \"$*\"; }\n"
        + block
        + "\n"
        + "printf 'CANDIDATES:%s\\n' \"${F_FIELD_CANDIDATES-undefined}\"\n"
        + "printf 'UPDATED:%s\\n' \"$F_TRACKER_UPDATED\"\n",
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(harness, 0o755)
    return subprocess.run(
        [_bash_executable(), str(harness)],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )


def test_wizard_syncs_exact_passed_subset_when_stu11_fails(tmp_path: Path) -> None:
    result = _run_wizard_sync_block(tmp_path, stu11="否", cache_off="可")
    expected = "OPS-01 OPS-02 OPS-10 OPS-12 PERF-01 PERF-02 QUAL-01"

    assert result.returncode == 0, result.stderr
    assert f"NOTE:台帳取り込み候補ID: {expected}\n" in result.stdout
    assert f"CANDIDATES:{expected}\n" in result.stdout
    assert "SYNC:/repo/scripts/story_tracker.py sync-field" in result.stdout
    assert "UPDATED:可\n" in result.stdout


def test_wizard_does_not_sync_without_shared_cache_off_gate(tmp_path: Path) -> None:
    result = _run_wizard_sync_block(tmp_path, stu11="可", cache_off="否")

    assert result.returncode == 0, result.stderr
    assert "NOTE:台帳取り込み候補ID: なし\n" in result.stdout
    assert "CANDIDATES:なし\n" in result.stdout
    assert "SYNC:" not in result.stdout
    assert "UPDATED:未実施\n" in result.stdout


def test_wizard_does_not_sync_when_cache_off_was_not_validated(
    tmp_path: Path,
) -> None:
    result = _run_wizard_sync_block(
        tmp_path,
        stu11="可",
        cache_off="可",
        cache_off_validated="否",
    )

    assert result.returncode == 0, result.stderr
    assert "NOTE:台帳取り込み候補ID: なし\n" in result.stdout
    assert "SYNC:" not in result.stdout
    assert "UPDATED:未実施\n" in result.stdout


def test_wizard_does_not_sync_when_shell_subset_disagrees_with_validator(
    tmp_path: Path,
) -> None:
    result = _run_wizard_sync_block(
        tmp_path,
        stu11="可",
        cache_off="可",
        approved_candidates=["OPS-01"],
    )

    assert result.returncode == 0, result.stderr
    assert "NOTE:台帳取り込み候補ID: なし\n" in result.stdout
    assert "SYNC:" not in result.stdout
    assert "UPDATED:未実施\n" in result.stdout


def _run_wizard_capture_json_parser(
    tmp_path: Path, capture_json: str
) -> subprocess.CompletedProcess[str]:
    script = Path("scripts/field-verification.sh").read_text(encoding="utf-8")
    block_start = (
        'if [[ -n "$FIELD_PYTHON" && "$F_EVIDENCE_TRANSFER" == "可" ]]; then'
    )
    block = block_start + script.split(block_start, 1)[1].split('\nsay ""', 1)[0]
    fake_cli = tmp_path / "scripts" / "field_story_evidence.py"
    fake_cli.parent.mkdir(parents=True)
    fake_cli.write_text(
        "import os, sys\n"
        'sys.stdout.write(os.environ["FAKE_CAPTURE_JSON"])\n',
        encoding="utf-8",
        newline="\n",
    )
    bundle = tmp_path / EVIDENCE_RELATIVE / field_evidence.BUNDLE_NAME
    bundle.parent.mkdir(parents=True)
    bundle.write_text("sealed bundle\n", encoding="utf-8")
    manifest = tmp_path / MANIFEST_RELATIVE
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("sealed manifest\n", encoding="utf-8")
    harness = tmp_path / "capture-json-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        'if command -v cygpath >/dev/null 2>&1; then\n'
        '  LB_ROOT="$(cygpath -u "$1")"\n'
        '  FIELD_PYTHON="$(cygpath -u "$2")"\n'
        "else\n"
        '  LB_ROOT="$1"\n'
        '  FIELD_PYTHON="$2"\n'
        "fi\n"
        f'EXPECTED_EVIDENCE_REL="{EVIDENCE_RELATIVE}"\n'
        'EXPECTED_EVIDENCE_DIR="$LB_ROOT/$EXPECTED_EVIDENCE_REL"\n'
        f'F_EVIDENCE_MANIFEST="{MANIFEST_RELATIVE}"\n'
        'EXPECTED_MANIFEST="$LB_ROOT/$F_EVIDENCE_MANIFEST"\n'
        'F_EVIDENCE_TRANSFER="可"\n'
        'F_FIELD_BUNDLE="未生成"\n'
        'F_FIELD_BUNDLE_SHA="未確認"\n'
        'F_EVIDENCE_MANIFEST_SHA="未確認"\n'
        'FIELD_CAPTURE_JSON=""\n'
        'FIELD_CAPTURE_CANDIDATES=""\n'
        "FIELD_APPROVED_CANDIDATE_IDS=()\n"
        "SKIPPED=()\n"
        "warn() { printf 'WARN:%s\\n' \"$1\"; }\n"
        + block
        + "\n"
        + "printf 'BUNDLE:%s\\n' \"$F_FIELD_BUNDLE\"\n"
        + "printf 'APPROVED:%s\\n' \"${FIELD_APPROVED_CANDIDATE_IDS[*]}\"\n",
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(harness, 0o755)
    environment = os.environ.copy()
    environment["FAKE_CAPTURE_JSON"] = capture_json
    return subprocess.run(
        [_bash_executable(), str(harness), str(tmp_path), sys.executable],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )


def _capture_cli_payload(approved_candidates: list[str]) -> dict[str, object]:
    return {
        "bundle": f"{EVIDENCE_RELATIVE}/{field_evidence.BUNDLE_NAME}",
        "bundle_sha256": "a" * 64,
        "manifest": MANIFEST_RELATIVE,
        "manifest_sha256": "b" * 64,
        "approved_candidates": approved_candidates,
        "field_complete": approved_candidates == list(field_evidence.FIELD_STORIES),
    }


def test_wizard_strictly_receives_cli_approved_candidates(tmp_path: Path) -> None:
    approved = [story for story in field_evidence.FIELD_STORIES if story != "STU-11"]
    result = _run_wizard_capture_json_parser(
        tmp_path,
        json.dumps(_capture_cli_payload(approved)),
    )

    assert result.returncode == 0, result.stderr
    assert f"BUNDLE:{EVIDENCE_RELATIVE}/{field_evidence.BUNDLE_NAME}\n" in result.stdout
    assert f"APPROVED:{' '.join(approved)}\n" in result.stdout


def test_wizard_rejects_duplicate_keys_in_capture_json(tmp_path: Path) -> None:
    valid = json.dumps(_capture_cli_payload(list(field_evidence.FIELD_STORIES)))
    duplicate = valid.replace(
        '"bundle":',
        '"bundle": "spoofed", "bundle":',
        1,
    )
    result = _run_wizard_capture_json_parser(tmp_path, duplicate)

    assert result.returncode == 0, result.stderr
    assert "BUNDLE:未生成\n" in result.stdout
    assert "APPROVED:\n" in result.stdout
    assert "capture JSON契約を検証できません" in result.stdout
    assert "field capture JSON contract error" in result.stderr
