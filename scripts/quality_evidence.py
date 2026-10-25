#!/usr/bin/env python3
"""Validate the structured QUAL-01 30-sentence TSV.

This module validates only the TSV's schema and deterministic aggregates.  It
cannot attest that the rows came from a real lesson or that human reviewers are
qualified.  Directory-wide SHA-256 completeness also remains the responsibility
of Phase 16 in ``scripts/field-verification.sh`` so the manifest is generated
after every field artifact has been collected.
"""

from __future__ import annotations

import argparse
import ctypes
import csv
from dataclasses import dataclass
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import unicodedata
import wave
from typing import Any, Mapping, Sequence, TypeAlias


class _EarlyWindowsFileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", ctypes.c_ulong),
    ]


def _early_change_time_ns(path: Path) -> int:
    if os.name != "nt":
        return path.lstat().st_ctime_ns
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    get_information.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    value = os.path.abspath(path)
    if not value.startswith("\\\\?\\"):
        value = (
            "\\\\?\\UNC\\" + value[2:]
            if value.startswith("\\\\")
            else "\\\\?\\" + value
        )
    handle = create_file(
        value,
        0x0080,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00200000 | 0x02000000,
        None,
    )
    if handle in {None, ctypes.c_void_p(-1).value}:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = _EarlyWindowsFileBasicInfo()
        if not get_information(
            handle,
            0,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(information.ChangeTime) * 100
    finally:
        close_handle(handle)


def _early_revision(
    path: Path,
) -> tuple[os.stat_result, tuple[int, int, int, int, int, int]]:
    def value(
        identity: os.stat_result, change_time_ns: int
    ) -> tuple[int, int, int, int, int, int]:
        return (
            identity.st_dev,
            identity.st_ino,
            identity.st_size,
            identity.st_mtime_ns,
            identity.st_ctime_ns,
            change_time_ns,
        )

    before = path.lstat()
    before_revision = value(before, _early_change_time_ns(path))
    after = path.lstat()
    after_revision = value(after, _early_change_time_ns(path))
    if before_revision != after_revision:
        raise RuntimeError(f"import source revision was unstable: {path}")
    return after, after_revision


def _early_import_source_state(
    path: Path, *, root: Path
) -> tuple[str, tuple[int, int, int, int, int, int]]:
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"import source is outside repository: {path}") from exc
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    parents = [root]
    current = root
    for part in relative.parts[:-1]:
        current /= part
        parents.append(current)
    parent_identities: list[tuple[Path, tuple[int, int]]] = []
    reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for parent in parents:
        identity = parent.lstat()
        if (
            stat.S_ISLNK(identity.st_mode)
            or bool(getattr(identity, "st_file_attributes", 0) & reparse_mask)
            or bool(is_junction(parent))
            or not stat.S_ISDIR(identity.st_mode)
        ):
            raise RuntimeError(f"import source has a linked parent: {parent}")
        parent_identities.append((parent, (identity.st_dev, identity.st_ino)))
    lexical, before_revision = _early_revision(path)
    if (
        stat.S_ISLNK(lexical.st_mode)
        or bool(getattr(lexical, "st_file_attributes", 0) & reparse_mask)
        or bool(is_junction(path))
        or not stat.S_ISREG(lexical.st_mode)
    ):
        raise RuntimeError(f"import source is not a real regular file: {path}")
    content = path.read_bytes()
    after, after_revision = _early_revision(path)
    if before_revision != after_revision or len(content) != after.st_size:
        raise RuntimeError(f"import source changed while read: {path}")
    for parent, expected in parent_identities:
        identity = parent.lstat()
        if (
            parent.is_symlink()
            or bool(getattr(identity, "st_file_attributes", 0) & reparse_mask)
            or bool(is_junction(parent))
            or not stat.S_ISDIR(identity.st_mode)
            or (identity.st_dev, identity.st_ino) != expected
        ):
            raise RuntimeError(f"import source parent changed while read: {parent}")
    return hashlib.sha256(content).hexdigest(), after_revision


_SELF_PATH = Path(os.path.abspath(__file__))
_IMPORT_ROOT = _SELF_PATH.parent.parent
_IMPORTED_VALIDATOR_PATHS = (
    _SELF_PATH,
    _IMPORT_ROOT / "scripts" / "qa_evidence_manifest.py",
)
_EARLY_IMPORTED_VALIDATOR_STATE = {
    path: _early_import_source_state(path, root=_IMPORT_ROOT)
    for path in _IMPORTED_VALIDATOR_PATHS
}
_IMPORT_PYCACHE = tempfile.TemporaryDirectory(
    prefix="linguabridge-quality-import-pycache-"
)
_IMPORT_PYCACHE_WAS_EMPTY = not any(Path(_IMPORT_PYCACHE.name).iterdir())
if not _IMPORT_PYCACHE_WAS_EMPTY:  # pragma: no cover - freshly created directory
    raise RuntimeError("quality import bytecode cache was not empty")
sys.pycache_prefix = _IMPORT_PYCACHE.name
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))

from scripts import qa_evidence_manifest  # noqa: E402
from scripts.qa_evidence_manifest import (  # noqa: E402
    EvidenceManifestError,
    SourceHashSnapshot,
)

if Path(os.path.abspath(qa_evidence_manifest.__file__)) != _IMPORTED_VALIDATOR_PATHS[1]:
    raise RuntimeError("quality imported an unexpected QA evidence helper")


def _snapshot_imported_quality_sources() -> SourceHashSnapshot:
    hashes: dict[str, str] = {}
    revisions: dict[str, qa_evidence_manifest.SourceRevision] = {}
    for path in _IMPORTED_VALIDATOR_PATHS:
        data, revision = qa_evidence_manifest._read_stable_regular_file_with_revision(
            path, description=f"imported quality validator source {path.name}"
        )
        relative = path.relative_to(_IMPORT_ROOT).as_posix()
        hashes[relative] = hashlib.sha256(data).hexdigest()
        revisions[relative] = revision
    return SourceHashSnapshot(hashes, revisions)


try:
    _IMPORTED_VALIDATOR_SOURCE_SNAPSHOT = _snapshot_imported_quality_sources()
except EvidenceManifestError as exc:  # pragma: no cover - import cannot continue safely
    raise RuntimeError("cannot bind imported quality validator sources") from exc
if (
    dict(_IMPORTED_VALIDATOR_SOURCE_SNAPSHOT)
    != {
        path.relative_to(_IMPORT_ROOT).as_posix(): state[0]
        for path, state in _EARLY_IMPORTED_VALIDATOR_STATE.items()
    }
    or dict(_IMPORTED_VALIDATOR_SOURCE_SNAPSHOT.revisions)
    != {
        path.relative_to(_IMPORT_ROOT).as_posix(): state[1]
        for path, state in _EARLY_IMPORTED_VALIDATOR_STATE.items()
    }
):
    raise RuntimeError("quality validator source changed during import")


EXPECTED_HEADER = [
    "No",
    "領域",
    "音響条件",
    "正解日本語",
    "ASR日本語",
    "英語訳",
    "英語評点1-5",
    "中国語訳",
    "中国語評点1-5",
    "所見",
]
REQUIRED_DOMAINS = ("理科", "数学", "行事")
REQUIRED_ACOUSTICS = ("後方", "雑音", "チャイム")
REQUIRED_TEXT_COLUMNS = (0, 1, 2, 3, 4, 5, 7)
VALID_RATINGS = {str(value): value for value in range(1, 6)}

PACKAGE_SCHEMA_VERSION = 1
PACKAGE_KIND = "qual-01-provenance"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PACKAGE_ROOT_KEYS = {
    "schema_version",
    "kind",
    "field_date",
    "captured_at",
    "field_session_id",
    "host_fingerprint_sha256",
    "quality_tsv",
    "sources",
    "evaluators",
    "segments",
}
SOURCE_KEYS = {
    "id",
    "domain",
    "audio",
    "transcript",
    "recorded_started_at",
    "recorded_finished_at",
    "processing_started_at",
    "processing_finished_at",
    "lesson_attestation",
}
AUDIO_KEYS = {"path", "sha256", "size_bytes", "duration_ms", "media_type"}
ARTIFACT_KEYS = {"path", "sha256"}
EVALUATOR_KEYS = {
    "id",
    "person_ref",
    "role",
    "language",
    "qualification_kind",
    "qualification_evidence",
    "review_attestation",
    "review_started_at",
    "review_finished_at",
}
SEGMENT_KEYS = {
    "row_no",
    "source_id",
    "start_ms",
    "end_ms",
    "transcript_seq",
    "domain",
    "acoustic_condition",
    "reference_evaluator_id",
    "english_evaluator_id",
    "chinese_evaluator_id",
}
TRANSCRIPT_KEYS = {
    "seq",
    "created_at",
    "t_start",
    "t_end",
    "ja",
    "asr_ms",
    "translations",
}
TRANSLATION_KEYS = {"text", "engine", "mt_ms"}
DOMAIN_LABELS = {
    "science": REQUIRED_DOMAINS[0],
    "mathematics": REQUIRED_DOMAINS[1],
    "event_notice": REQUIRED_DOMAINS[2],
}
ACOUSTIC_LABELS = {
    "rear": REQUIRED_ACOUSTICS[0],
    "noise": REQUIRED_ACOUSTICS[1],
    "chime": REQUIRED_ACOUSTICS[2],
}
ROLE_LANGUAGE_QUALIFICATIONS = {
    ("reference_transcriber", "ja"): {"native_speaker", "licensed_teacher"},
    ("meaning_evaluator", "en"): {
        "native_speaker",
        "language_teacher",
        "certified_proficiency",
    },
    ("meaning_evaluator", "zh"): {"native_speaker", "language_teacher"},
}
SOURCE_SNAPSHOT_SCHEMA_VERSION = 2
SOURCE_SNAPSHOT_KIND = "field-source-snapshot"
SOURCE_SNAPSHOT_KEYS = {
    "schema_version",
    "kind",
    "field_date",
    "phase",
    "captured_at",
    "host_fingerprint_sha256",
    "session_id",
    "predecessor_sha256",
    "source_hashes",
    "source_revisions",
    "source_snapshot_sha256",
}
SOURCE_REVISION_INDEX_KEYS = {"files", "directories"}
SOURCE_REVISION_KEYS = {"dev", "ino", "size", "mtime_ns", "ctime_ns"}
SOURCE_SNAPSHOT_FILENAMES = {
    "start": "school-source-start.json",
    "end": "school-source-end.json",
}
PACKAGE_CHECK_KEYS = (
    "provenance_schema_exact",
    "field_session_and_host_match",
    "artifact_hashes_match",
    "source_wav_metadata_matches",
    "thirty_source_segments_bound",
    "tsv_matches_transcript",
    "ninety_evaluator_assignments_declared_with_evidence",
    "timeline_is_consistent",
    "artifacts_unchanged_during_validation",
)


class QualityEvidenceError(ValueError):
    """The QUAL-01 provenance package is incomplete or not reproducible."""


ArtifactRevision: TypeAlias = tuple[int, int, int, int, int, int]
ArtifactFingerprint = tuple[str, ArtifactRevision]


@dataclass(frozen=True)
class QualityValidationWitness:
    """Immutable, non-serialized exact read-set for a quality package."""

    root: str
    file_sha256: tuple[tuple[str, str], ...]
    file_revisions: tuple[tuple[str, ArtifactRevision], ...]


class ValidatedQualityPackage(dict[str, Any]):
    """dict-compatible package result carrying a final-fence witness."""

    __slots__ = ("_validation_witness",)

    def __init__(
        self, value: Mapping[str, Any], validation_witness: QualityValidationWitness
    ) -> None:
        super().__init__(value)
        self._validation_witness = validation_witness

    @property
    def validation_witness(self) -> QualityValidationWitness:
        return self._validation_witness


def _recheck_imported_quality_modules() -> None:
    try:
        current = _snapshot_imported_quality_sources()
    except EvidenceManifestError as exc:
        raise QualityEvidenceError(
            "cannot recheck imported quality validator sources"
        ) from exc
    if (
        dict(current) != dict(_IMPORTED_VALIDATOR_SOURCE_SNAPSHOT)
        or current.revisions != _IMPORTED_VALIDATOR_SOURCE_SNAPSHOT.revisions
    ):
        raise QualityEvidenceError(
            "an imported quality validator source changed on disk"
        )


def _freeze_quality_witness(
    root: Path, fingerprints: Mapping[Path, ArtifactFingerprint]
) -> QualityValidationWitness:
    return QualityValidationWitness(
        root=str(root),
        file_sha256=tuple(
            sorted(
                (path.relative_to(root).as_posix(), fingerprint[0])
                for path, fingerprint in fingerprints.items()
            )
        ),
        file_revisions=tuple(
            sorted(
                (path.relative_to(root).as_posix(), fingerprint[1])
                for path, fingerprint in fingerprints.items()
            )
        ),
    )


def _quality_witness_fingerprints(
    witness: QualityValidationWitness,
) -> tuple[Path, dict[Path, ArtifactFingerprint]]:
    if type(witness) is not QualityValidationWitness:
        raise QualityEvidenceError("quality validation witness type is invalid")
    root = Path(witness.root)
    if not root.is_absolute() or str(Path(os.path.abspath(root))) != witness.root:
        raise QualityEvidenceError("quality validation witness root is invalid")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise QualityEvidenceError("quality validation witness root is invalid") from exc
    hashes = dict(witness.file_sha256)
    revisions = dict(witness.file_revisions)
    if (
        not hashes
        or not revisions
        or len(hashes) != len(witness.file_sha256)
        or len(revisions) != len(witness.file_revisions)
        or set(hashes) != set(revisions)
    ):
        raise QualityEvidenceError("quality validation witness paths are invalid")
    fingerprints: dict[Path, ArtifactFingerprint] = {}
    canonical_paths: set[str] = set()
    for relative, digest in hashes.items():
        parts = _relative_parts(relative)
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise QualityEvidenceError("quality validation witness hash is invalid")
        revision = revisions[relative]
        if (
            not isinstance(revision, tuple)
            or len(revision) != 6
            or any(type(component) is not int or component < 0 for component in revision)
        ):
            raise QualityEvidenceError("quality validation witness revision is invalid")
        path = root.joinpath(*parts)
        canonical = os.path.normcase(str(path))
        if canonical in canonical_paths:
            raise QualityEvidenceError("quality validation witness path is duplicated")
        canonical_paths.add(canonical)
        _assert_no_link_components(root, path, label="quality validation witness")
        fingerprints[path] = (digest, revision)
    return root, fingerprints


def recheck_quality_validation_witness(witness: QualityValidationWitness) -> None:
    """Recheck all quality-package inputs immediately before publication."""

    _recheck_imported_quality_modules()
    _root, fingerprints = _quality_witness_fingerprints(witness)
    _assert_fingerprints_unchanged(fingerprints)


def _merge_quality_fingerprints(
    *groups: Mapping[Path, ArtifactFingerprint],
) -> dict[Path, ArtifactFingerprint]:
    """Merge read sets while rejecting path or hardlink identity aliases."""

    merged: dict[Path, ArtifactFingerprint] = {}
    for group in groups:
        for path, fingerprint in group.items():
            existing = merged.get(path)
            if existing is not None:
                if existing != fingerprint:
                    raise QualityEvidenceError(
                        "a binding artifact changed during validation"
                    )
                continue
            for prior in merged:
                try:
                    aliased = os.path.samefile(path, prior)
                except OSError as exc:
                    raise QualityEvidenceError(
                        "a binding artifact changed during validation"
                    ) from exc
                if aliased:
                    raise QualityEvidenceError(
                        "quality evidence files must not be hardlink aliases"
                    )
            merged[path] = fingerprint
    return merged


def _cell(row: Sequence[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def _normalize_whitespace(value: str) -> str:
    """Collapse all Unicode whitespace for reference-sentence uniqueness."""
    return " ".join(value.split())


def _rating(value: str) -> int | None:
    """Return a valid integer 1-5 rating; reject decimals and padded values."""
    return VALID_RATINGS.get(value)


def _average(values: Sequence[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _coverage(rows: Sequence[Sequence[str]], column: int, labels: Sequence[str]) -> set[str]:
    normalized_cells = {
        _normalize_whitespace(_cell(row, column)) for row in rows
    }
    return {
        label
        for label in labels
        if label in normalized_cells
    }


def _scope() -> dict[str, Any]:
    return {
        "tsv_structure_and_aggregates_checked": True,
        "real_lesson_provenance_checked": False,
        "human_reviewer_qualification_checked": False,
        "directory_hash_completeness_checked": False,
        "directory_hash_completeness_owner": "scripts/field-verification.sh Phase 16",
    }


def _validate_quality_records(records: Sequence[Sequence[str]]) -> dict[str, Any]:
    header = records[0] if records else []
    rows = records[1:] if records else []
    english_ratings = [
        rating
        for row in rows
        if (rating := _rating(_cell(row, 6))) is not None
    ]
    chinese_ratings = [
        rating
        for row in rows
        if (rating := _rating(_cell(row, 8))) is not None
    ]
    required_rows = sum(
        len(row) == len(EXPECTED_HEADER)
        and all(_cell(row, index).strip() for index in REQUIRED_TEXT_COLUMNS)
        and _rating(_cell(row, 6)) is not None
        and _rating(_cell(row, 8)) is not None
        for row in rows
    )
    ordered_numbers = sum(
        _cell(row, 0) == str(expected)
        for expected, row in enumerate(rows, start=1)
    )
    distinct_references = {
        reference
        for row in rows
        if (reference := _normalize_whitespace(_cell(row, 3)))
    }
    domains = _coverage(rows, 1, REQUIRED_DOMAINS)
    acoustics = _coverage(rows, 2, REQUIRED_ACOUSTICS)
    english_average = _average(english_ratings)
    chinese_average = _average(chinese_ratings)

    metrics = {
        "rows": len(rows),
        "required_rows": required_rows,
        "english_rating_count": len(english_ratings),
        "english_average": english_average,
        "chinese_rating_count": len(chinese_ratings),
        "chinese_average": chinese_average,
        "ordered_number_count": ordered_numbers,
        "distinct_reference_count": len(distinct_references),
        "domain_coverage_count": len(domains),
        "acoustic_coverage_count": len(acoustics),
    }
    checks = {
        "header_exact": header == EXPECTED_HEADER,
        "exactly_30_rows": len(rows) == 30,
        "exactly_10_columns": len(header) == len(EXPECTED_HEADER)
        and all(len(row) == len(EXPECTED_HEADER) for row in rows),
        "numbers_1_through_30_in_order": ordered_numbers == 30
        and len(rows) == 30,
        "thirty_distinct_normalized_references": len(distinct_references) == 30
        and len(rows) == 30,
        "required_fields_complete": required_rows == 30 and len(rows) == 30,
        "thirty_valid_english_ratings": len(english_ratings) == 30
        and len(rows) == 30,
        "thirty_valid_chinese_ratings": len(chinese_ratings) == 30
        and len(rows) == 30,
        "required_domains_covered": domains == set(REQUIRED_DOMAINS),
        "required_acoustics_covered": acoustics == set(REQUIRED_ACOUSTICS),
        "chinese_average_at_least_3_5": chinese_average is not None
        and chinese_average >= 3.5,
    }
    return {
        "schema_version": 1,
        "kind": "quality-evidence-validation",
        "passed": all(checks.values()),
        "checks": checks,
        "metrics": metrics,
        "scope": _scope(),
    }


def _quality_records_from_bytes(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig")
    with io.StringIO(text, newline="") as handle:
        return list(csv.reader(handle, delimiter="\t", strict=True))


def validate_quality_tsv(path: Path) -> dict[str, Any]:
    """Return named checks and metrics for one QUAL-01 TSV."""
    return _validate_quality_records(_quality_records_from_bytes(path.read_bytes()))


def _failed_result(exc: BaseException) -> dict[str, Any]:
    checks = {
        "header_exact": False,
        "exactly_30_rows": False,
        "exactly_10_columns": False,
        "numbers_1_through_30_in_order": False,
        "thirty_distinct_normalized_references": False,
        "required_fields_complete": False,
        "thirty_valid_english_ratings": False,
        "thirty_valid_chinese_ratings": False,
        "required_domains_covered": False,
        "required_acoustics_covered": False,
        "chinese_average_at_least_3_5": False,
    }
    return {
        "schema_version": 1,
        "kind": "quality-evidence-validation",
        "passed": False,
        "checks": checks,
        "metrics": {
            "rows": 0,
            "required_rows": 0,
            "english_rating_count": 0,
            "english_average": None,
            "chinese_rating_count": 0,
            "chinese_average": None,
            "ordered_number_count": 0,
            "distinct_reference_count": 0,
            "domain_coverage_count": 0,
            "acoustic_coverage_count": 0,
        },
        "scope": _scope(),
        "error": f"{type(exc).__name__}: {exc}",
    }


def _package_scope(*, manifest_checked: bool) -> dict[str, Any]:
    """Separate deterministic bindings from facts that still need human review."""
    return {
        "tsv_structure_and_aggregates_checked": True,
        "artifact_hashes_checked": True,
        "manifest_references_checked": manifest_checked,
        "source_media_format_and_duration_checked": True,
        "source_segment_bounds_and_uniqueness_checked": True,
        "tsv_to_transcript_content_binding_checked": True,
        "evaluator_assignment_and_evidence_presence_checked": True,
        "field_snapshot_authenticity_checked": False,
        "real_lesson_origin_checked": False,
        "transcript_generated_by_audited_run_checked": False,
        "source_segment_semantic_correspondence_checked": False,
        "evaluator_identity_checked": False,
        "evaluator_qualification_authenticity_checked": False,
        "human_meaning_judgment_checked": False,
    }


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QualityEvidenceError(f"JSON key is duplicated: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise QualityEvidenceError(f"non-finite JSON number is forbidden: {value}")


def _load_strict_json_bytes(data: bytes, *, label: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise QualityEvidenceError(f"{label} must be UTF-8") from exc
    if text.startswith("\ufeff"):
        raise QualityEvidenceError(f"{label} must not contain a BOM")
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise QualityEvidenceError(f"{label} is not strict JSON") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_file_content(
    path: Path, *, capture_bytes: bool
) -> tuple[bytes | None, str, ArtifactRevision]:
    """Hash one exact regular-file revision, optionally retaining those bytes."""

    path = Path(os.path.abspath(path))
    if _is_link_or_junction(path):
        raise QualityEvidenceError(f"artifact must not be a link or junction: {path}")
    try:
        lexical_before, revision_before = qa_evidence_manifest._snapshot_path_revision(
            path, description=f"quality artifact {path.name}"
        )
    except (OSError, EvidenceManifestError) as exc:
        raise QualityEvidenceError(f"artifact cannot be inspected: {path}") from exc
    if not stat.S_ISREG(lexical_before.st_mode):
        raise QualityEvidenceError(f"artifact must be a regular file: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise QualityEvidenceError(f"artifact cannot be opened: {path}") from exc
    digest = hashlib.sha256()
    captured = bytearray() if capture_bytes else None
    byte_count = 0
    try:
        descriptor_before = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_before.st_mode):
            raise QualityEvidenceError(f"artifact must be a regular file: {path}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            byte_count += len(chunk)
            if captured is not None:
                captured.extend(chunk)
        descriptor_after = os.fstat(descriptor)
    except OSError as exc:
        raise QualityEvidenceError(f"artifact cannot be read: {path}") from exc
    finally:
        os.close(descriptor)

    try:
        lexical_after, revision_after = qa_evidence_manifest._snapshot_path_revision(
            path, description=f"quality artifact {path.name}"
        )
    except (OSError, EvidenceManifestError) as exc:
        raise QualityEvidenceError(f"artifact cannot be rechecked: {path}") from exc
    descriptor_identity_before = (
        descriptor_before.st_dev,
        descriptor_before.st_ino,
        descriptor_before.st_size,
        descriptor_before.st_mtime_ns,
    )
    descriptor_identity_after = (
        descriptor_after.st_dev,
        descriptor_after.st_ino,
        descriptor_after.st_size,
        descriptor_after.st_mtime_ns,
    )
    lexical_identity_after = (
        lexical_after.st_dev,
        lexical_after.st_ino,
        lexical_after.st_size,
        lexical_after.st_mtime_ns,
    )
    if (
        _is_link_or_junction(path)
        or revision_before != revision_after
        or descriptor_identity_before != descriptor_identity_after
        or descriptor_identity_after != lexical_identity_after
        or byte_count != descriptor_after.st_size
    ):
        raise QualityEvidenceError(f"artifact changed while being read: {path}")
    return (
        bytes(captured) if captured is not None else None,
        digest.hexdigest(),
        revision_after,
    )


def _stable_sha256(path: Path) -> ArtifactFingerprint:
    _data, digest, revision = _stable_file_content(path, capture_bytes=False)
    return digest, revision


def _stable_read_bytes(
    path: Path,
) -> tuple[bytes, str, ArtifactRevision]:
    """Read and hash the same bytes, rejecting a concurrent path mutation."""
    path = Path(os.path.abspath(path))
    try:
        data, revision = qa_evidence_manifest._read_stable_regular_file_with_revision(
            path, description=f"quality artifact {path.name}"
        )
    except (OSError, EvidenceManifestError) as exc:
        raise QualityEvidenceError(f"artifact changed while being read: {path}") from exc
    if len(data) != revision[2]:
        raise QualityEvidenceError(f"artifact changed while being read: {path}")
    return data, hashlib.sha256(data).hexdigest(), revision


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _aware_time(value: Any, *, label: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise QualityEvidenceError(f"{label} must be timezone-aware ISO 8601") from exc
    else:
        raise QualityEvidenceError(f"{label} must be timezone-aware ISO 8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QualityEvidenceError(f"{label} must be timezone-aware ISO 8601")
    return parsed


def _valid_field_date(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise QualityEvidenceError("field_date must use YYYY-MM-DD")
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise QualityEvidenceError("field_date is not a real date") from exc
    return value


def _relative_parts(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise QualityEvidenceError("artifact path must be a canonical POSIX relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or pure.as_posix() != value or any(
        part in {"", ".", ".."} for part in pure.parts
    ):
        raise QualityEvidenceError("artifact path must be a canonical POSIX relative path")
    if not pure.parts or any(":" in part for part in pure.parts):
        raise QualityEvidenceError("artifact path must be a canonical POSIX relative path")
    return pure.parts


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    return path.is_symlink() or bool(is_junction(path))


def _assert_no_link_components(root: Path, path: Path, *, label: str) -> None:
    root_absolute = Path(os.path.abspath(root))
    path_absolute = Path(os.path.abspath(path))
    try:
        relative = path_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise QualityEvidenceError(f"{label} must stay inside the repository") from exc
    current = root_absolute
    if _is_link_or_junction(current):
        raise QualityEvidenceError("repository root must not be a link or junction")
    for part in relative.parts:
        current /= part
        if current.exists() and _is_link_or_junction(current):
            raise QualityEvidenceError(f"{label} must not traverse a link or junction")


def _resolve_artifact(
    root: Path, evidence_dir: Path, relative: Any, *, label: str
) -> tuple[str, Path]:
    parts = _relative_parts(relative)
    evidence_resolved = evidence_dir.resolve(strict=True)
    path = root.joinpath(*parts)
    _assert_no_link_components(root, path, label=label)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(evidence_resolved)
    except (OSError, ValueError) as exc:
        raise QualityEvidenceError(f"{label} must stay inside the field evidence directory") from exc
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise QualityEvidenceError(f"{label} must reference a non-empty regular file")
    return str(relative), resolved


def _validated_manifest(value: Mapping[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise QualityEvidenceError("manifest_files must be a mapping")
    result: dict[str, str] = {}
    for relative, digest in value.items():
        _relative_parts(relative)
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise QualityEvidenceError(f"manifest digest is invalid: {relative}")
        if relative in result:
            raise QualityEvidenceError(f"manifest path is duplicated: {relative}")
        result[relative] = digest
    return result


def _record_artifact(
    *,
    root: Path,
    evidence_dir: Path,
    relative: Any,
    expected_sha256: Any,
    label: str,
    manifest_files: Mapping[str, str] | None,
    fingerprints: dict[Path, ArtifactFingerprint],
    captured_bytes: dict[Path, bytes] | None = None,
) -> Path:
    if not isinstance(expected_sha256, str) or SHA256_RE.fullmatch(expected_sha256) is None:
        raise QualityEvidenceError(f"{label} SHA-256 is invalid")
    canonical, path = _resolve_artifact(root, evidence_dir, relative, label=label)
    if any(os.path.samefile(path, existing_path) for existing_path in fingerprints):
        raise QualityEvidenceError(f"{label} aliases a previously bound artifact")
    if captured_bytes is None:
        digest, identity = _stable_sha256(path)
    else:
        data, digest, identity = _stable_read_bytes(path)
        captured_bytes[path] = data
    if digest != expected_sha256:
        raise QualityEvidenceError(f"{label} SHA-256 does not match")
    if manifest_files is not None and manifest_files.get(canonical) != digest:
        raise QualityEvidenceError(f"{label} is missing or stale in the outer manifest")
    fingerprints[path] = (digest, identity)
    return path


def _artifact_object(
    value: Any,
    *,
    root: Path,
    evidence_dir: Path,
    label: str,
    manifest_files: Mapping[str, str] | None,
    fingerprints: dict[Path, ArtifactFingerprint],
    captured_bytes: dict[Path, bytes] | None = None,
) -> Path:
    if not isinstance(value, dict) or set(value) != ARTIFACT_KEYS:
        raise QualityEvidenceError(f"{label} schema is invalid")
    return _record_artifact(
        root=root,
        evidence_dir=evidence_dir,
        relative=value.get("path"),
        expected_sha256=value.get("sha256"),
        label=label,
        manifest_files=manifest_files,
        fingerprints=fingerprints,
        captured_bytes=captured_bytes,
    )


def _stable_wav_metadata(
    path: Path,
) -> tuple[str, ArtifactRevision, int, int]:
    path = Path(os.path.abspath(path))
    if _is_link_or_junction(path):
        raise QualityEvidenceError("source audio must not be a link or junction")
    try:
        lexical_before, revision_before = qa_evidence_manifest._snapshot_path_revision(
            path, description=f"quality source audio {path.name}"
        )
        if not stat.S_ISREG(lexical_before.st_mode):
            raise QualityEvidenceError("source audio must be a regular file")
        with path.open("rb") as raw:
            before = os.fstat(raw.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise QualityEvidenceError("source audio must be a regular file")
            digest = hashlib.sha256()
            byte_count = 0
            for chunk in iter(lambda: raw.read(1024 * 1024), b""):
                digest.update(chunk)
                byte_count += len(chunk)
            raw.seek(0)
            with wave.open(raw, "rb") as handle:
                channels = handle.getnchannels()
                sample_width = handle.getsampwidth()
                frames = handle.getnframes()
                rate = handle.getframerate()
                if (
                    handle.getcomptype() != "NONE"
                    or channels <= 0
                    or sample_width not in {1, 2, 3, 4}
                    or rate <= 0
                    or frames <= 0
                ):
                    raise QualityEvidenceError("source audio must be non-empty PCM WAV")
                remaining = frames
                while remaining:
                    requested = min(remaining, 64 * 1024)
                    if len(handle.readframes(requested)) != requested * channels * sample_width:
                        raise QualityEvidenceError("source audio PCM data is truncated")
                    remaining -= requested
            after = os.fstat(raw.fileno())
        lexical_after, revision_after = qa_evidence_manifest._snapshot_path_revision(
            path, description=f"quality source audio {path.name}"
        )
    except (EOFError, wave.Error) as exc:
        raise QualityEvidenceError("source audio must be a valid PCM WAV") from exc
    except EvidenceManifestError as exc:
        raise QualityEvidenceError("source audio changed while validating") from exc
    descriptor_identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    descriptor_identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    lexical_identity_after = (
        lexical_after.st_dev,
        lexical_after.st_ino,
        lexical_after.st_size,
        lexical_after.st_mtime_ns,
    )
    if (
        _is_link_or_junction(path)
        or revision_before != revision_after
        or descriptor_identity_before != descriptor_identity_after
        or descriptor_identity_after != lexical_identity_after
        or byte_count != after.st_size
    ):
        raise QualityEvidenceError("source audio changed while validating")
    duration_ms = int(
        (Decimal(frames) * 1000 / Decimal(rate)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    return digest.hexdigest(), revision_after, after.st_size, duration_ms


def _number(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QualityEvidenceError(f"{label} must be a finite JSON number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise QualityEvidenceError(f"{label} must be a finite JSON number") from exc
    if not result.is_finite():
        raise QualityEvidenceError(f"{label} must be a finite JSON number")
    return result


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise QualityEvidenceError(f"{label} must be an integer >= {minimum}")
    return value


def _canonical_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualityEvidenceError(f"{label} must be non-empty text")
    return unicodedata.normalize("NFC", value).strip()


def _load_transcript_bytes(data: bytes) -> dict[int, dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise QualityEvidenceError("transcript must be UTF-8 JSONL") from exc
    if text.startswith("\ufeff") or "\r" in text or not text.endswith("\n"):
        raise QualityEvidenceError("transcript must be BOM-free UTF-8 JSONL ending in LF")
    result: dict[int, dict[str, Any]] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise QualityEvidenceError("transcript must not contain blank JSONL records")
        record = _load_strict_json_bytes(line.encode("utf-8"), label="transcript record")
        if not isinstance(record, dict) or set(record) != TRANSCRIPT_KEYS:
            raise QualityEvidenceError(f"transcript record {line_number} schema is invalid")
        seq = _integer(record.get("seq"), label="transcript seq", minimum=1)
        if seq in result:
            raise QualityEvidenceError(f"transcript seq is duplicated: {seq}")
        _aware_time(record.get("created_at"), label="transcript created_at")
        start = _number(record.get("t_start"), label="transcript t_start")
        end = _number(record.get("t_end"), label="transcript t_end")
        if start < 0 or end <= start:
            raise QualityEvidenceError("transcript interval is invalid")
        _canonical_text(record.get("ja"), label="transcript ja")
        _integer(record.get("asr_ms"), label="transcript asr_ms")
        translations = record.get("translations")
        if not isinstance(translations, dict) or not translations or not set(translations) <= {"en", "zh"}:
            raise QualityEvidenceError("transcript translations schema is invalid")
        for language, translation in translations.items():
            if not isinstance(translation, dict) or set(translation) != TRANSLATION_KEYS:
                raise QualityEvidenceError(f"transcript {language} translation schema is invalid")
            _canonical_text(translation.get("text"), label=f"transcript {language} text")
            _canonical_text(translation.get("engine"), label=f"transcript {language} engine")
            _integer(translation.get("mt_ms"), label=f"transcript {language} mt_ms")
        result[seq] = record
    if not result:
        raise QualityEvidenceError("transcript must contain at least one record")
    return result


def _failed_package_result(
    exc: BaseException, *, manifest_checked: bool
) -> dict[str, Any]:
    base = _failed_result(exc)
    return {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "kind": "quality-evidence-package-validation",
        "passed": False,
        "checks": {
            **base["checks"],
            **{key: False for key in PACKAGE_CHECK_KEYS},
        },
        "metrics": {
            **base["metrics"],
            "source_count": 0,
            "mapped_segment_count": 0,
            "transcript_bound_row_count": 0,
            "evaluator_count": 0,
            "evaluator_assignment_count": 0,
            "asr_mismatch_row_count": 0,
        },
        "scope": _package_scope(manifest_checked=manifest_checked),
        "provenance_sha256": None,
        "error": f"{type(exc).__name__}: {exc}",
    }


def _validate_source_hashes(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise QualityEvidenceError(f"{label} source_hashes must be a non-empty object")
    result: dict[str, str] = {}
    for relative, digest in value.items():
        _relative_parts(relative)
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise QualityEvidenceError(f"{label} source hash is invalid: {relative}")
        result[relative] = digest
    return result


def _validate_source_revisions(
    value: Any, source_hashes: Mapping[str, str], *, label: str
) -> dict[str, dict[str, dict[str, int]]]:
    if not isinstance(value, dict) or set(value) != SOURCE_REVISION_INDEX_KEYS:
        raise QualityEvidenceError(f"{label} source_revisions schema is invalid")
    result: dict[str, dict[str, dict[str, int]]] = {}
    for group in ("files", "directories"):
        raw_index = value.get(group)
        if not isinstance(raw_index, dict):
            raise QualityEvidenceError(f"{label} source_revisions index is invalid")
        normalized: dict[str, dict[str, int]] = {}
        for relative, revision in raw_index.items():
            if relative != "." or group != "directories":
                _relative_parts(relative)
            if not isinstance(revision, dict) or set(revision) != SOURCE_REVISION_KEYS:
                raise QualityEvidenceError(f"{label} source revision shape is invalid")
            if any(
                type(revision[key]) is not int or revision[key] < 0
                for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")
            ):
                raise QualityEvidenceError(f"{label} source revision value is invalid")
            normalized[relative] = {
                key: revision[key]
                for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")
            }
        result[group] = dict(sorted(normalized.items()))
    if set(result["files"]) != set(source_hashes) or not result["directories"]:
        raise QualityEvidenceError(f"{label} source revision paths are invalid")
    return result


def _source_snapshot_sha256(
    source_hashes: Mapping[str, str], source_revisions: Mapping[str, Any]
) -> str:
    return _canonical_sha256(
        {
            "source_hashes": dict(source_hashes),
            "source_revisions": dict(source_revisions),
        }
    )


def _load_source_snapshot(
    path: Path,
    *,
    root: Path,
    evidence_dir: Path,
    field_date: str,
    phase: str,
) -> tuple[dict[str, Any], Path, ArtifactFingerprint]:
    expected = evidence_dir / SOURCE_SNAPSHOT_FILENAMES[phase]
    if Path(os.path.abspath(path)) != Path(os.path.abspath(expected)):
        raise QualityEvidenceError(f"{phase} source snapshot must use its fixed path")
    relative, resolved = _resolve_artifact(
        root,
        evidence_dir,
        expected.relative_to(root).as_posix(),
        label=f"{phase} source snapshot",
    )
    del relative
    data, digest, identity = _stable_read_bytes(resolved)
    payload = _load_strict_json_bytes(data, label=f"{phase} source snapshot")
    if not isinstance(payload, dict) or set(payload) != SOURCE_SNAPSHOT_KEYS:
        raise QualityEvidenceError(f"{phase} source snapshot schema is invalid")
    source_hashes = _validate_source_hashes(payload.get("source_hashes"), label=phase)
    source_revisions = _validate_source_revisions(
        payload.get("source_revisions"), source_hashes, label=phase
    )
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != SOURCE_SNAPSHOT_SCHEMA_VERSION
        or payload.get("kind") != SOURCE_SNAPSHOT_KIND
        or payload.get("field_date") != field_date
        or payload.get("phase") != phase
        or not isinstance(payload.get("session_id"), str)
        or SHA256_RE.fullmatch(payload["session_id"]) is None
        or not isinstance(payload.get("host_fingerprint_sha256"), str)
        or SHA256_RE.fullmatch(payload["host_fingerprint_sha256"]) is None
        or payload.get("source_snapshot_sha256")
        != _source_snapshot_sha256(source_hashes, source_revisions)
    ):
        raise QualityEvidenceError(f"{phase} source snapshot binding is invalid")
    _aware_time(payload.get("captured_at"), label=f"{phase} source snapshot captured_at")
    predecessor = payload.get("predecessor_sha256")
    if (phase == "start" and predecessor is not None) or (
        phase == "end"
        and (
            not isinstance(predecessor, str)
            or SHA256_RE.fullmatch(predecessor) is None
        )
    ):
        raise QualityEvidenceError(f"{phase} source snapshot predecessor is invalid")
    return payload, resolved, (digest, identity)


def _load_source_snapshot_pair(
    *,
    root: Path,
    field_date: str,
    start_path: Path,
    end_path: Path,
    manifest_files: Mapping[str, str] | None,
) -> tuple[dict[str, Any], dict[Path, ArtifactFingerprint]]:
    evidence_dir = root / "docs" / "field" / "evidence" / field_date
    start, start_resolved, start_fingerprint = _load_source_snapshot(
        start_path,
        root=root,
        evidence_dir=evidence_dir,
        field_date=field_date,
        phase="start",
    )
    end, end_resolved, end_fingerprint = _load_source_snapshot(
        end_path,
        root=root,
        evidence_dir=evidence_dir,
        field_date=field_date,
        phase="end",
    )
    start_time = _aware_time(start["captured_at"], label="start source snapshot captured_at")
    end_time = _aware_time(end["captured_at"], label="end source snapshot captured_at")
    if (
        start_time >= end_time
        or end["session_id"] != start["session_id"]
        or end["host_fingerprint_sha256"] != start["host_fingerprint_sha256"]
        or end["source_hashes"] != start["source_hashes"]
        or end["source_revisions"] != start["source_revisions"]
        or end["source_snapshot_sha256"] != start["source_snapshot_sha256"]
        or end["predecessor_sha256"] != start_fingerprint[0]
    ):
        raise QualityEvidenceError("start/end source snapshot chain is inconsistent")
    if manifest_files is not None:
        for snapshot, fingerprint in (
            (start_resolved, start_fingerprint),
            (end_resolved, end_fingerprint),
        ):
            relative = snapshot.relative_to(root).as_posix()
            if manifest_files.get(relative) != fingerprint[0]:
                raise QualityEvidenceError(
                    "source snapshot is missing or stale in the outer manifest"
                )
    return (
        {
            "session_id": start["session_id"],
            "host_fingerprint_sha256": start["host_fingerprint_sha256"],
            "start_captured_at": start["captured_at"],
            "end_captured_at": end["captured_at"],
        },
        {
            start_resolved: start_fingerprint,
            end_resolved: end_fingerprint,
        },
    )


def _load_outer_manifest(
    path: Path, *, root: Path, field_date: str
) -> tuple[dict[str, str], Path, ArtifactFingerprint]:
    expected = root / "docs" / "field" / f"{field_date}-evidence-sha256.txt"
    if Path(os.path.abspath(path)) != Path(os.path.abspath(expected)):
        raise QualityEvidenceError("outer manifest must use its fixed date-scoped path")
    _assert_no_link_components(root, expected, label="outer manifest")
    try:
        resolved = expected.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise QualityEvidenceError("outer manifest must stay inside the repository") from exc
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise QualityEvidenceError("outer manifest must be a non-empty regular file")
    data, digest, identity = _stable_read_bytes(resolved)
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise QualityEvidenceError("outer manifest must be UTF-8") from exc
    if text.startswith("\ufeff") or "\r" in text or not text.endswith("\n"):
        raise QualityEvidenceError("outer manifest must be BOM-free UTF-8/LF")
    expected_prefix = f"docs/field/evidence/{field_date}/"
    files: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise QualityEvidenceError("outer manifest is not strict GNU sha256sum format")
        artifact_digest, relative = match.groups()
        _relative_parts(relative)
        if not relative.startswith(expected_prefix) or relative in files:
            raise QualityEvidenceError("outer manifest path is outside scope or duplicated")
        files[relative] = artifact_digest
    if not files:
        raise QualityEvidenceError("outer manifest must contain evidence files")
    return files, resolved, (digest, identity)


def _assert_fingerprints_unchanged(
    fingerprints: Mapping[Path, ArtifactFingerprint]
) -> None:
    for path, expected in fingerprints.items():
        if _stable_sha256(path) != expected:
            raise QualityEvidenceError("a binding artifact changed during validation")


def validate_quality_package(
    tsv_path: Path,
    provenance_path: Path,
    *,
    root: Path,
    field_date: str,
    expected_session_id: str,
    expected_host_fingerprint_sha256: str,
    source_snapshot_start_at: str | dt.datetime,
    source_snapshot_end_at: str | dt.datetime | None = None,
    field_bundle_captured_at: str | dt.datetime | None = None,
    manifest_files: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate one sealed QUAL-01 TSV and its source/evaluator provenance.

    The deterministic gate proves artifact integrity, segment bounds, transcript
    binding, assignments, and chronology.  Whether the audio really came from a
    lesson and whether the named people are qualified remain explicit human
    review obligations in ``scope``.
    """
    manifest_checked = manifest_files is not None
    try:
        _recheck_imported_quality_modules()
        root = root.resolve(strict=True)
        field_date = _valid_field_date(field_date)
        evidence_dir = root / "docs" / "field" / "evidence" / field_date
        if not evidence_dir.is_dir():
            raise QualityEvidenceError("the fixed field evidence directory is missing")
        expected_tsv = evidence_dir / f"{field_date}-quality-30.tsv"
        expected_provenance = evidence_dir / f"{field_date}-quality-provenance.json"
        if Path(os.path.abspath(tsv_path)) != Path(os.path.abspath(expected_tsv)):
            raise QualityEvidenceError("quality TSV must use its fixed date-scoped path")
        if Path(os.path.abspath(provenance_path)) != Path(os.path.abspath(expected_provenance)):
            raise QualityEvidenceError("quality provenance must use its fixed date-scoped path")
        if not isinstance(expected_session_id, str) or SHA256_RE.fullmatch(expected_session_id) is None:
            raise QualityEvidenceError("expected field session id is invalid")
        if (
            not isinstance(expected_host_fingerprint_sha256, str)
            or SHA256_RE.fullmatch(expected_host_fingerprint_sha256) is None
        ):
            raise QualityEvidenceError("expected field host fingerprint is invalid")
        start_snapshot_time = _aware_time(
            source_snapshot_start_at, label="source snapshot start"
        )
        if start_snapshot_time.date().isoformat() != field_date:
            raise QualityEvidenceError(
                "source snapshot start local date does not match field_date"
            )
        end_snapshot_time = (
            _aware_time(source_snapshot_end_at, label="source snapshot end")
            if source_snapshot_end_at is not None
            else None
        )
        bundle_time = (
            _aware_time(field_bundle_captured_at, label="field bundle captured_at")
            if field_bundle_captured_at is not None
            else None
        )
        if end_snapshot_time is not None and start_snapshot_time >= end_snapshot_time:
            raise QualityEvidenceError("source snapshot chronology is invalid")
        if end_snapshot_time is not None and bundle_time is not None and end_snapshot_time > bundle_time:
            raise QualityEvidenceError("field bundle predates the end source snapshot")

        manifest = _validated_manifest(manifest_files)
        fingerprints: dict[Path, ArtifactFingerprint] = {}
        tsv_relative, tsv = _resolve_artifact(root, evidence_dir, expected_tsv.relative_to(root).as_posix(), label="quality TSV")
        provenance_relative, provenance = _resolve_artifact(
            root,
            evidence_dir,
            expected_provenance.relative_to(root).as_posix(),
            label="quality provenance",
        )
        if os.path.samefile(tsv, provenance):
            raise QualityEvidenceError("quality TSV and provenance must be distinct files")
        tsv_bytes, tsv_digest, tsv_identity = _stable_read_bytes(tsv)
        provenance_bytes, provenance_digest, provenance_identity = _stable_read_bytes(
            provenance
        )
        if manifest is not None:
            if manifest.get(tsv_relative) != tsv_digest or manifest.get(provenance_relative) != provenance_digest:
                raise QualityEvidenceError("TSV or provenance is missing or stale in the outer manifest")
        fingerprints[tsv] = (tsv_digest, tsv_identity)
        fingerprints[provenance] = (provenance_digest, provenance_identity)

        payload = _load_strict_json_bytes(
            provenance_bytes, label="quality provenance"
        )
        if not isinstance(payload, dict) or set(payload) != PACKAGE_ROOT_KEYS:
            raise QualityEvidenceError("quality provenance root schema is invalid")
        if type(payload.get("schema_version")) is not int or payload.get("schema_version") != PACKAGE_SCHEMA_VERSION or payload.get("kind") != PACKAGE_KIND:
            raise QualityEvidenceError("quality provenance schema_version/kind is invalid")
        if payload.get("field_date") != field_date:
            raise QualityEvidenceError("quality provenance field_date does not match")
        captured_at = _aware_time(payload.get("captured_at"), label="provenance captured_at")
        if payload.get("field_session_id") != expected_session_id:
            raise QualityEvidenceError("quality provenance field session does not match")
        if payload.get("host_fingerprint_sha256") != expected_host_fingerprint_sha256:
            raise QualityEvidenceError("quality provenance host fingerprint does not match")
        if end_snapshot_time is not None and captured_at > end_snapshot_time:
            raise QualityEvidenceError("quality provenance was captured after the end source snapshot")
        if bundle_time is not None and captured_at > bundle_time:
            raise QualityEvidenceError("field bundle predates quality provenance")

        quality_tsv = payload.get("quality_tsv")
        if not isinstance(quality_tsv, dict) or set(quality_tsv) != ARTIFACT_KEYS:
            raise QualityEvidenceError("quality_tsv binding schema is invalid")
        if quality_tsv.get("path") != tsv_relative or quality_tsv.get("sha256") != tsv_digest:
            raise QualityEvidenceError("quality_tsv binding does not match the fixed TSV")

        quality_records = _quality_records_from_bytes(tsv_bytes)
        tsv_result = _validate_quality_records(quality_records)
        if tsv_result.get("passed") is not True:
            raise QualityEvidenceError("quality TSV deterministic checks failed")
        rows = quality_records[1:] if quality_records else []
        if len(rows) != 30:
            raise QualityEvidenceError("quality TSV must contain exactly 30 rows")

        source_values = payload.get("sources")
        if not isinstance(source_values, list) or not source_values:
            raise QualityEvidenceError("sources must be a non-empty array")
        source_ids: list[str] = []
        for item in source_values:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise QualityEvidenceError("sources must be sorted by unique id")
            source_ids.append(item["id"])
        if source_ids != sorted(source_ids):
            raise QualityEvidenceError("sources must be sorted by unique id")
        if len(source_ids) != len(set(source_ids)):
            raise QualityEvidenceError("source id is duplicated")
        sources: dict[str, dict[str, Any]] = {}
        core_paths = {tsv, provenance}
        latest_processing_finish = start_snapshot_time
        for index, source in enumerate(source_values):
            if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
                raise QualityEvidenceError(f"source {index} schema is invalid")
            source_id = source.get("id")
            if not isinstance(source_id, str) or ID_RE.fullmatch(source_id) is None:
                raise QualityEvidenceError("source id is invalid")
            if source.get("domain") not in {*DOMAIN_LABELS, "mixed"}:
                raise QualityEvidenceError("source domain is invalid")
            audio_value = source.get("audio")
            if not isinstance(audio_value, dict) or set(audio_value) != AUDIO_KEYS:
                raise QualityEvidenceError("source audio schema is invalid")
            if audio_value.get("media_type") != "audio/wav":
                raise QualityEvidenceError("source audio media_type must be audio/wav")
            audio = _record_artifact(
                root=root,
                evidence_dir=evidence_dir,
                relative=audio_value.get("path"),
                expected_sha256=audio_value.get("sha256"),
                label=f"source {source_id} audio",
                manifest_files=manifest,
                fingerprints=fingerprints,
            )
            size = _integer(audio_value.get("size_bytes"), label="audio size_bytes", minimum=1)
            duration = _integer(audio_value.get("duration_ms"), label="audio duration_ms", minimum=1)
            wav_digest, wav_identity, wav_size, wav_duration = _stable_wav_metadata(audio)
            if (
                fingerprints[audio] != (wav_digest, wav_identity)
                or wav_size != size
                or wav_duration != duration
            ):
                raise QualityEvidenceError("source audio size or duration does not match")
            transcript_bytes: dict[Path, bytes] = {}
            transcript = _artifact_object(
                source.get("transcript"),
                root=root,
                evidence_dir=evidence_dir,
                label=f"source {source_id} transcript",
                manifest_files=manifest,
                fingerprints=fingerprints,
                captured_bytes=transcript_bytes,
            )
            lesson_attestation = _artifact_object(
                source.get("lesson_attestation"),
                root=root,
                evidence_dir=evidence_dir,
                label=f"source {source_id} lesson attestation",
                manifest_files=manifest,
                fingerprints=fingerprints,
            )
            if any(path in core_paths for path in (audio, transcript, lesson_attestation)) or len({audio, transcript, lesson_attestation}) != 3:
                raise QualityEvidenceError("source core artifacts must be distinct files")
            core_paths.update((audio, transcript, lesson_attestation))
            recorded_start = _aware_time(source.get("recorded_started_at"), label="recorded_started_at")
            recorded_finish = _aware_time(source.get("recorded_finished_at"), label="recorded_finished_at")
            processing_start = _aware_time(source.get("processing_started_at"), label="processing_started_at")
            processing_finish = _aware_time(source.get("processing_finished_at"), label="processing_finished_at")
            recorded_ms = Decimal(str((recorded_finish - recorded_start).total_seconds())) * 1000
            if (
                not start_snapshot_time
                <= recorded_start
                < recorded_finish
                <= processing_start
                or abs(recorded_ms - Decimal(duration)) > 1000
            ):
                raise QualityEvidenceError("source recording chronology does not match WAV duration")
            if not (start_snapshot_time < processing_start < processing_finish <= captured_at):
                raise QualityEvidenceError("source processing is outside the field/provenance timeline")
            transcript_entries = _load_transcript_bytes(transcript_bytes[transcript])
            for entry in transcript_entries.values():
                created_at = _aware_time(entry.get("created_at"), label="transcript created_at")
                if not processing_start <= created_at <= processing_finish:
                    raise QualityEvidenceError("transcript entry is outside its processing interval")
            latest_processing_finish = max(latest_processing_finish, processing_finish)
            sources[source_id] = {
                "domain": source["domain"],
                "duration_ms": duration,
                "transcript": transcript_entries,
            }

        evaluator_values = payload.get("evaluators")
        if not isinstance(evaluator_values, list) or not evaluator_values:
            raise QualityEvidenceError("evaluators must be a non-empty array")
        evaluator_ids: list[str] = []
        for item in evaluator_values:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise QualityEvidenceError("evaluators must be sorted by unique id")
            evaluator_ids.append(item["id"])
        if evaluator_ids != sorted(evaluator_ids):
            raise QualityEvidenceError("evaluators must be sorted by unique id")
        if len(evaluator_ids) != len(set(evaluator_ids)):
            raise QualityEvidenceError("evaluator id is duplicated")
        evaluators: dict[str, tuple[str, str]] = {}
        for index, evaluator in enumerate(evaluator_values):
            if not isinstance(evaluator, dict) or set(evaluator) != EVALUATOR_KEYS:
                raise QualityEvidenceError(f"evaluator {index} schema is invalid")
            evaluator_id = evaluator.get("id")
            person_ref = evaluator.get("person_ref")
            if not isinstance(evaluator_id, str) or ID_RE.fullmatch(evaluator_id) is None or not isinstance(person_ref, str) or ID_RE.fullmatch(person_ref) is None:
                raise QualityEvidenceError("evaluator id/person_ref is invalid")
            role = evaluator.get("role")
            language = evaluator.get("language")
            if not isinstance(role, str) or not isinstance(language, str):
                raise QualityEvidenceError("evaluator role/language/qualification is invalid")
            allowed = ROLE_LANGUAGE_QUALIFICATIONS.get((role, language))
            if allowed is None or evaluator.get("qualification_kind") not in allowed:
                raise QualityEvidenceError("evaluator role/language/qualification is invalid")
            qualification = _artifact_object(
                evaluator.get("qualification_evidence"),
                root=root,
                evidence_dir=evidence_dir,
                label=f"evaluator {evaluator_id} qualification",
                manifest_files=manifest,
                fingerprints=fingerprints,
            )
            attestation = _artifact_object(
                evaluator.get("review_attestation"),
                root=root,
                evidence_dir=evidence_dir,
                label=f"evaluator {evaluator_id} review attestation",
                manifest_files=manifest,
                fingerprints=fingerprints,
            )
            if qualification == attestation or qualification in core_paths or attestation in core_paths:
                raise QualityEvidenceError("evaluator evidence must be distinct from core artifacts")
            review_start = _aware_time(evaluator.get("review_started_at"), label="review_started_at")
            review_finish = _aware_time(evaluator.get("review_finished_at"), label="review_finished_at")
            if not (latest_processing_finish <= review_start < review_finish <= captured_at):
                raise QualityEvidenceError("evaluator review timeline is invalid")
            evaluators[evaluator_id] = (str(role), str(language))

        segment_values = payload.get("segments")
        if not isinstance(segment_values, list) or len(segment_values) != 30:
            raise QualityEvidenceError("segments must contain exactly 30 records")
        used_evaluators: set[str] = set()
        used_sources: set[str] = set()
        used_transcript_entries: set[tuple[str, int]] = set()
        intervals: dict[str, list[tuple[int, int]]] = {source_id: [] for source_id in sources}
        transcript_bound = 0
        assignments = 0
        asr_mismatches = 0
        for expected_row, segment in enumerate(segment_values, start=1):
            if not isinstance(segment, dict) or set(segment) != SEGMENT_KEYS:
                raise QualityEvidenceError(f"segment {expected_row} schema is invalid")
            if segment.get("row_no") != expected_row:
                raise QualityEvidenceError("segment row_no must be exactly 1 through 30 in order")
            source_id = segment.get("source_id")
            if not isinstance(source_id, str) or source_id not in sources:
                raise QualityEvidenceError("segment references an unknown source")
            used_sources.add(source_id)
            start_ms = _integer(segment.get("start_ms"), label="segment start_ms")
            end_ms = _integer(segment.get("end_ms"), label="segment end_ms", minimum=1)
            if start_ms >= end_ms or end_ms > sources[source_id]["duration_ms"]:
                raise QualityEvidenceError("segment interval is outside source duration")
            intervals[source_id].append((start_ms, end_ms))
            transcript_seq = _integer(segment.get("transcript_seq"), label="transcript_seq", minimum=1)
            entry_key = (str(source_id), transcript_seq)
            if entry_key in used_transcript_entries:
                raise QualityEvidenceError("a transcript entry is assigned to more than one row")
            used_transcript_entries.add(entry_key)
            entry = sources[source_id]["transcript"].get(transcript_seq)
            if entry is None:
                raise QualityEvidenceError("segment transcript_seq does not exist")
            entry_start_ms = int((Decimal(str(entry["t_start"])) * 1000).to_integral_value(rounding=ROUND_HALF_UP))
            entry_end_ms = int((Decimal(str(entry["t_end"])) * 1000).to_integral_value(rounding=ROUND_HALF_UP))
            if (start_ms, end_ms) != (entry_start_ms, entry_end_ms):
                raise QualityEvidenceError("segment interval does not match transcript")
            row = rows[expected_row - 1]
            domain = segment.get("domain")
            acoustic = segment.get("acoustic_condition")
            if domain not in DOMAIN_LABELS or DOMAIN_LABELS[domain] != row[1]:
                raise QualityEvidenceError("segment domain does not match the TSV")
            if acoustic not in ACOUSTIC_LABELS or ACOUSTIC_LABELS[acoustic] != row[2]:
                raise QualityEvidenceError("segment acoustic condition does not match the TSV")
            source_domain = sources[source_id]["domain"]
            if source_domain != "mixed" and source_domain != domain:
                raise QualityEvidenceError("segment domain does not match its source declaration")
            translations = entry["translations"]
            if set(translations) != {"en", "zh"}:
                raise QualityEvidenceError("selected transcript entries require both en and zh")
            if _canonical_text(entry["ja"], label="transcript ja") != _canonical_text(row[4], label="TSV ASR") or _canonical_text(translations["en"]["text"], label="transcript en") != _canonical_text(row[5], label="TSV English") or _canonical_text(translations["zh"]["text"], label="transcript zh") != _canonical_text(row[7], label="TSV Chinese"):
                raise QualityEvidenceError("TSV text does not match the sealed transcript")
            evaluator_fields = (
                ("reference_evaluator_id", ("reference_transcriber", "ja")),
                ("english_evaluator_id", ("meaning_evaluator", "en")),
                ("chinese_evaluator_id", ("meaning_evaluator", "zh")),
            )
            for field, expected_role in evaluator_fields:
                evaluator_id = segment.get(field)
                if (
                    not isinstance(evaluator_id, str)
                    or evaluators.get(evaluator_id) != expected_role
                ):
                    raise QualityEvidenceError(
                        f"{field} declaration does not match its role/language assignment"
                    )
                used_evaluators.add(evaluator_id)
                assignments += 1
            if _normalize_whitespace(row[3]) != _normalize_whitespace(row[4]):
                asr_mismatches += 1
            transcript_bound += 1
        for source_id, source_intervals in intervals.items():
            ordered = sorted(source_intervals)
            if any(previous[1] > current[0] for previous, current in zip(ordered, ordered[1:])):
                raise QualityEvidenceError(f"source segments overlap: {source_id}")
        if used_evaluators != set(evaluators):
            raise QualityEvidenceError("every evaluator record must be used by at least one row")
        if used_sources != set(sources):
            raise QualityEvidenceError("every source record must be used by at least one row")

        _assert_fingerprints_unchanged(fingerprints)

        checks = {
            **tsv_result["checks"],
            "provenance_schema_exact": True,
            "field_session_and_host_match": True,
            "artifact_hashes_match": True,
            "source_wav_metadata_matches": True,
            "thirty_source_segments_bound": True,
            "tsv_matches_transcript": True,
            "ninety_evaluator_assignments_declared_with_evidence": True,
            "timeline_is_consistent": True,
            "artifacts_unchanged_during_validation": True,
        }
        result = {
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "kind": "quality-evidence-package-validation",
            "passed": all(checks.values()),
            "checks": checks,
            "metrics": {
                **tsv_result["metrics"],
                "source_count": len(sources),
                "mapped_segment_count": len(segment_values),
                "transcript_bound_row_count": transcript_bound,
                "evaluator_count": len(evaluators),
                "evaluator_assignment_count": assignments,
                "asr_mismatch_row_count": asr_mismatches,
            },
            "scope": _package_scope(manifest_checked=manifest_checked),
            "provenance_sha256": provenance_digest,
        }
        witness = _freeze_quality_witness(root, fingerprints)
        recheck_quality_validation_witness(witness)
        return ValidatedQualityPackage(result, witness)
    except (
        QualityEvidenceError,
        OSError,
        UnicodeError,
        csv.Error,
        InvalidOperation,
        OverflowError,
        ValueError,
        TypeError,
    ) as exc:
        return _failed_package_result(exc, manifest_checked=manifest_checked)


def _formatted_average(value: Any) -> str:
    return f"{value:.2f}" if isinstance(value, (int, float)) else "0.00"


def _shell_summary(result: dict[str, Any]) -> str:
    metrics = result["metrics"]
    fields = [
        "true" if result.get("passed") is True else "false",
        str(metrics["rows"]),
        str(metrics["required_rows"]),
        str(metrics["english_rating_count"]),
        _formatted_average(metrics["english_average"]),
        str(metrics["chinese_rating_count"]),
        _formatted_average(metrics["chinese_average"]),
        str(metrics["ordered_number_count"]),
        str(metrics["distinct_reference_count"]),
        str(metrics["domain_coverage_count"]),
        str(metrics["acoustic_coverage_count"]),
    ]
    return "\t".join(fields)


def _package_shell_summary(result: dict[str, Any]) -> str:
    metrics = result["metrics"]
    return "\t".join(
        [
            _shell_summary(result),
            str(metrics["mapped_segment_count"]),
            str(metrics["transcript_bound_row_count"]),
            str(metrics["evaluator_assignment_count"]),
            str(result.get("provenance_sha256") or ""),
        ]
    )


def _package_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="quality_evidence.py package",
        description="Validate a sealed QUAL-01 provenance package.",
    )
    parser.add_argument("tsv", type=Path)
    parser.add_argument("provenance", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--field-date", required=True)
    parser.add_argument("--source-snapshot-start", type=Path, required=True)
    parser.add_argument("--source-snapshot-end", type=Path, required=True)
    parser.add_argument("--field-bundle-captured-at")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--format", choices=("json", "shell"), default="json")
    args = parser.parse_args(argv)

    manifest_checked = args.manifest is not None
    binding_fingerprints: dict[Path, ArtifactFingerprint] = {}
    publication_witness: QualityValidationWitness | None = None
    try:
        _recheck_imported_quality_modules()
        root = args.root.resolve(strict=True)
        field_date = _valid_field_date(args.field_date)
        manifest_files: dict[str, str] | None = None
        if args.manifest is not None:
            manifest_files, manifest_path, manifest_fingerprint = _load_outer_manifest(
                args.manifest, root=root, field_date=field_date
            )
            binding_fingerprints[manifest_path] = manifest_fingerprint
        binding, snapshot_fingerprints = _load_source_snapshot_pair(
            root=root,
            field_date=field_date,
            start_path=args.source_snapshot_start,
            end_path=args.source_snapshot_end,
            manifest_files=manifest_files,
        )
        binding_fingerprints.update(snapshot_fingerprints)
        result = validate_quality_package(
            args.tsv,
            args.provenance,
            root=root,
            field_date=field_date,
            expected_session_id=binding["session_id"],
            expected_host_fingerprint_sha256=binding["host_fingerprint_sha256"],
            source_snapshot_start_at=binding["start_captured_at"],
            source_snapshot_end_at=binding["end_captured_at"],
            field_bundle_captured_at=args.field_bundle_captured_at,
            manifest_files=manifest_files,
        )
        _assert_fingerprints_unchanged(binding_fingerprints)
        if isinstance(result, ValidatedQualityPackage):
            witness_root, package_fingerprints = _quality_witness_fingerprints(
                result.validation_witness
            )
            if witness_root != root:
                raise QualityEvidenceError(
                    "quality package witness repository root changed"
                )
            publication_fingerprints = _merge_quality_fingerprints(
                binding_fingerprints, package_fingerprints
            )
            publication_witness = _freeze_quality_witness(
                root, publication_fingerprints
            )
            recheck_quality_validation_witness(publication_witness)
    except (
        QualityEvidenceError,
        OSError,
        UnicodeError,
        csv.Error,
        InvalidOperation,
        OverflowError,
        ValueError,
        TypeError,
    ) as exc:
        result = _failed_package_result(exc, manifest_checked=manifest_checked)

    rendered = (
        _package_shell_summary(result)
        if args.format == "shell"
        else json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    )
    try:
        if publication_witness is not None:
            recheck_quality_validation_witness(publication_witness)
        else:
            _recheck_imported_quality_modules()
            _assert_fingerprints_unchanged(binding_fingerprints)
    except (QualityEvidenceError, OSError) as exc:
        print(
            f"QUALITY_EVIDENCE_INVALID: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(rendered)
    return 0 if result["passed"] is True else 1


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if raw_argv and raw_argv[0] == "package":
        return _package_main(raw_argv[1:])
    parser = argparse.ArgumentParser(
        description="Validate a QUAL-01 30-sentence field-evidence TSV."
    )
    parser.add_argument("tsv", type=Path)
    parser.add_argument("--format", choices=("json", "shell"), default="json")
    args = parser.parse_args(raw_argv)
    try:
        result = validate_quality_tsv(args.tsv)
    except (OSError, UnicodeError, csv.Error) as exc:
        result = _failed_result(exc)

    if args.format == "shell":
        print(_shell_summary(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
