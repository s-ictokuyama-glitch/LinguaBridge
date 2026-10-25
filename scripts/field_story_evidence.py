#!/usr/bin/env python3
"""Create and re-audit machine-readable field-story evidence.

The field wizard records human observations, but its Markdown report is a
mutable presentation artifact.  This module writes the raw gate inputs to a
JSON sidecar inside the date-scoped evidence directory, seals that sidecar in
the directory-wide SHA-256 manifest, and later replays every deterministic
gate before the tracker may use a human review decision.
"""

from __future__ import annotations

import argparse
import contextvars
import ctypes
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import stat
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence, TypeVar


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
ROOT = _SELF_PATH.parent.parent
_IMPORTED_VALIDATOR_PATHS = (
    _SELF_PATH,
    ROOT / "scripts" / "perf01_evidence.py",
    ROOT / "scripts" / "qa_evidence_manifest.py",
    ROOT / "scripts" / "quality_evidence.py",
)
_EARLY_IMPORTED_VALIDATOR_STATE = {
    path: _early_import_source_state(path, root=ROOT)
    for path in _IMPORTED_VALIDATOR_PATHS
}
_IMPORT_PYCACHE = tempfile.TemporaryDirectory(
    prefix="linguabridge-field-import-pycache-"
)
_IMPORT_PYCACHE_WAS_EMPTY = not any(Path(_IMPORT_PYCACHE.name).iterdir())
if not _IMPORT_PYCACHE_WAS_EMPTY:  # pragma: no cover - freshly created directory
    raise RuntimeError("field import bytecode cache was not empty")
sys.pycache_prefix = _IMPORT_PYCACHE.name
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import perf01_evidence, qa_evidence_manifest, quality_evidence  # noqa: E402
from scripts.qa_evidence_manifest import (  # noqa: E402
    EvidenceManifestError,
    SourceHashSnapshot,
    snapshot_supplied_source_hashes,
)

if (
    Path(os.path.abspath(perf01_evidence.__file__)) != _IMPORTED_VALIDATOR_PATHS[1]
    or Path(os.path.abspath(qa_evidence_manifest.__file__))
    != _IMPORTED_VALIDATOR_PATHS[2]
    or Path(os.path.abspath(quality_evidence.__file__))
    != _IMPORTED_VALIDATOR_PATHS[3]
):
    raise RuntimeError("field validator imported an unexpected local module")
try:
    _IMPORTED_VALIDATOR_SOURCE_SNAPSHOT = snapshot_supplied_source_hashes(
        ROOT, lambda: _IMPORTED_VALIDATOR_PATHS
    )
except EvidenceManifestError as exc:  # pragma: no cover - import cannot continue safely
    raise RuntimeError("cannot bind imported field validator sources") from exc
if (
    dict(_IMPORTED_VALIDATOR_SOURCE_SNAPSHOT)
    != {
        path.relative_to(ROOT).as_posix(): state[0]
        for path, state in _EARLY_IMPORTED_VALIDATOR_STATE.items()
    }
    or dict(_IMPORTED_VALIDATOR_SOURCE_SNAPSHOT.revisions)
    != {
        path.relative_to(ROOT).as_posix(): state[1]
        for path, state in _EARLY_IMPORTED_VALIDATOR_STATE.items()
    }
):
    raise RuntimeError("field validator source changed during import")


SCHEMA_VERSION = 5
BUNDLE_NAME = "field-story-evidence.json"
KIND = "field-story-candidates"
SOURCE_SNAPSHOT_SCHEMA_VERSION = 2
SOURCE_SNAPSHOT_KIND = "field-source-snapshot"
SOURCE_SNAPSHOT_FILENAMES = {
    "start": "school-source-start.json",
    "end": "school-source-end.json",
}
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
SOURCE_TREE_RELATIVES = ("server", "web", "scripts")
SOURCE_PARENT_RELATIVES = (".", "docs", "docs/qa")
PERFORMANCE_MANIFEST_NAME = "source-manifest.json"
PERFORMANCE_MANIFEST_SCHEMA = 4
FIELD_STORIES = (
    "OPS-01",
    "OPS-02",
    "OPS-10",
    "OPS-12",
    "STU-11",
    "PERF-01",
    "PERF-02",
    "QUAL-01",
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SHELL_DECIMAL_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
FIELD_TIME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
PERFORMANCE_MANIFEST_KEYS = {
    "schema_version",
    "kind",
    "complete",
    "started_at",
    "finished_at",
    "arguments",
    "configuration",
    "source_hashes",
    "input_hashes",
    "runtime_asset_hashes",
    "artifacts",
    "primary_report",
    "runtime",
    "continuous_lock",
}
PERFORMANCE_ARGUMENT_KEYS = {
    "minutes",
    "students",
    "audio",
    "corpus",
    "langs",
    "disconnect_every_s",
    "mt_cache_size",
    "engine",
    "asr",
    "partial",
    "port",
    "drain_seconds",
    "out_dir",
    "no_config_update",
}
PERFORMANCE_SOURCE_RELATIVES = {
    "scripts/acceptance.py",
    "scripts/bench.py",
    "scripts/performance_evidence.py",
    "scripts/replay_client.py",
    "scripts/story_tracker.py",
    "scripts/text_metrics.py",
    "config.yaml",
    "docs/qa/stories.tsv",
}
PERFORMANCE_VALIDATION_KEYS = {
    "schema_version",
    "kind",
    "passed",
    "cache_mode",
    "run_dir",
    "manifest",
    "primary_report",
    "validator_sha256",
    "source_snapshot_sha256",
    "runtime_snapshot_sha256",
    "system_snapshot_sha256",
    "hardware_snapshot_sha256",
    "host_fingerprint_sha256",
    "hardware_collected_at",
    "started_at",
    "finished_at",
    "manifest_sha256",
    "primary_report_sha256",
    "system",
    "hardware",
    "binding",
    "checks",
    "metrics",
}
PERFORMANCE_BINDING_KEYS = {
    "minutes",
    "students",
    "audio_sha256",
    "corpus",
    "asr",
    "engine",
    "langs",
    "disconnect_every_s",
    "partial",
    "drain_seconds",
    "no_config_update",
    "effective_config",
}
PERFORMANCE_METRIC_KEYS = {
    "audio_sha256",
    "measurement_seconds",
    "peak_rss_mb",
    "cpu_median_percent",
    "measurement_langs",
    "crashes",
    "errors",
    "acceptance_verdict",
    "latency_median_seconds",
    "latency_max_seconds",
}
PERFORMANCE_CHECK_KEYS = {
    "measurement_and_drain_complete",
    "ten_students_in_english_and_chinese",
    "continuous_resource_coverage",
    "no_crash_or_error",
    "standard_load_without_disconnect_injection",
    "real_asr_and_mt_work_observed",
    "cache_mode_effective",
    "report_arguments_match_manifest",
    "effective_config_matches_manifest",
    "windows_system_metadata",
    "manifest_complete_and_current",
    "arguments_at_least_45_minutes",
    "arguments_ten_students",
    "real_audio_input",
    "real_asr",
    "real_mt",
    "no_config_update",
    "report_engines_match_arguments",
    "audio_hash_matches",
    "primary_report_hash_matches",
    "arguments_out_dir_matches_run",
    "run_dir_binds_validation_host",
    "windows_hardware_snapshot",
    "school_hardware_supported",
    "report_matches_hardware",
    "hardware_collected_immediately_after_run",
}
PERFORMANCE_RUNTIME_KEYS = {
    "python",
    "executable",
    "executable_sha256",
    "active_distributions",
    "distributions",
}
PERFORMANCE_LOCK_KEYS = {
    "mode",
    "acquired_at",
    "released_at",
    "interrupted",
    "files",
}


class FieldEvidenceError(ValueError):
    """A field bundle is incomplete, stale, malformed, or outside its root."""


SourceRevision = tuple[int, int, int, int, int]
ValidationRevision = tuple[int, int, int, int, int, int]
_T = TypeVar("_T")


class FieldSourceHashSnapshot(SourceHashSnapshot):
    """Field source hashes plus file and source-tree directory revisions."""

    def __init__(
        self,
        hashes: dict[str, str],
        revisions: Mapping[str, tuple[int, ...]],
        directory_revisions: dict[str, SourceRevision],
    ) -> None:
        persisted_revisions: dict[str, SourceRevision] = {}
        validation_revisions: dict[str, ValidationRevision] = {}
        for relative, revision in revisions.items():
            if len(revision) not in {5, 6}:
                raise FieldEvidenceError(
                    f"field source revision is incomplete: {relative}"
                )
            change_time_ns = revision[5] if len(revision) >= 6 else revision[4]
            validation_revisions[relative] = (
                revision[0],
                revision[1],
                revision[2],
                revision[3],
                revision[4],
                change_time_ns,
            )
            persisted_revisions[relative] = (
                revision[0],
                revision[1],
                revision[2],
                revision[3],
                change_time_ns,
            )
        super().__init__(hashes, validation_revisions)
        self._persisted_revisions = tuple(sorted(persisted_revisions.items()))
        self._validation_revisions = tuple(sorted(validation_revisions.items()))
        self._directory_revisions = tuple(sorted(directory_revisions.items()))

    @property
    def persisted_revisions(self) -> tuple[tuple[str, SourceRevision], ...]:
        return self._persisted_revisions

    @property
    def validation_revisions(
        self,
    ) -> tuple[tuple[str, ValidationRevision], ...]:
        return self.revisions

    @property
    def directory_revisions(self) -> tuple[tuple[str, SourceRevision], ...]:
        return self._directory_revisions


@dataclass
class _ValidationReadSet:
    """Revisions read by one validation transaction."""

    file_revisions: dict[Path, ValidationRevision] = field(default_factory=dict)
    file_sha256: dict[Path, str] = field(default_factory=dict)
    directory_revisions: dict[Path, ValidationRevision] = field(default_factory=dict)
    directory_membership_sha256: dict[Path, str] = field(default_factory=dict)

    def record_file(
        self, path: Path, revision: ValidationRevision, sha256: str
    ) -> None:
        absolute = Path(os.path.abspath(path))
        previous = self.file_revisions.setdefault(absolute, revision)
        previous_sha256 = self.file_sha256.setdefault(absolute, sha256)
        if previous != revision or previous_sha256 != sha256:
            raise FieldEvidenceError(
                f"field validation file changed between reads: {absolute}"
            )

    def record_directory(
        self, path: Path, revision: ValidationRevision, membership_sha256: str
    ) -> None:
        absolute = Path(os.path.abspath(path))
        previous = self.directory_revisions.setdefault(absolute, revision)
        previous_membership = self.directory_membership_sha256.setdefault(
            absolute, membership_sha256
        )
        if previous != revision or previous_membership != membership_sha256:
            raise FieldEvidenceError(
                f"field validation directory changed between reads: {absolute}"
            )

    def recheck(self) -> None:
        recheck_validation_witness(self.freeze())

    def freeze(self) -> FieldValidationWitness:
        return FieldValidationWitness(
            file_revisions=tuple(
                sorted((str(path), revision) for path, revision in self.file_revisions.items())
            ),
            directory_revisions=tuple(
                sorted(
                    (str(path), revision)
                    for path, revision in self.directory_revisions.items()
                )
            ),
            file_sha256=tuple(
                sorted((str(path), digest) for path, digest in self.file_sha256.items())
            ),
            directory_membership_sha256=tuple(
                sorted(
                    (str(path), digest)
                    for path, digest in self.directory_membership_sha256.items()
                )
            ),
        )


@dataclass(frozen=True)
class FieldValidationWitness:
    """Immutable, non-serialized proof of the exact validation read-set."""

    file_revisions: tuple[tuple[str, ValidationRevision], ...]
    directory_revisions: tuple[tuple[str, ValidationRevision], ...]
    file_sha256: tuple[tuple[str, str], ...] = ()
    directory_membership_sha256: tuple[tuple[str, str], ...] = ()


class _WitnessedFieldResult(dict[str, Any]):
    __slots__ = ("_validation_witness",)

    def __init__(
        self, value: Mapping[str, Any], validation_witness: FieldValidationWitness
    ) -> None:
        super().__init__(value)
        self._validation_witness = validation_witness

    @property
    def validation_witness(self) -> FieldValidationWitness:
        return self._validation_witness


class ValidatedFieldBundle(_WitnessedFieldResult):
    """dict-compatible validated field bundle with a non-serialized witness."""


class ValidatedOuterManifest(_WitnessedFieldResult):
    """dict-compatible outer-manifest result with a non-serialized witness."""


_ACTIVE_READ_SET: contextvars.ContextVar[_ValidationReadSet | None] = (
    contextvars.ContextVar("field_validation_read_set", default=None)
)


STORY_DETAIL_KEYS: dict[str, tuple[str, ...]] = {
    "OPS-01": (
        "F_OPS01",
        "F_SETUP_FRESH",
        "F_SETUP_OK",
        "F_SETUP_STOPPED",
        "F_SETUP_ENV",
        "F_SETUP_EVIDENCE",
    ),
    "OPS-02": (
        "F_OPS02",
        "F_RETRY_EXECUTED",
        "F_RETRY_ENV",
        "F_RETRY_DONE_MODEL",
        "F_RETRY_DONE_STATE",
        "F_RETRY_DONE_AFTER",
        "F_RETRY_ACTIVE",
        "F_RETRY_MARKER",
        "F_RETRY_SERVER",
        "F_RETRY_REUSED",
        "F_RETRY_PARTIAL",
        "F_RETRY_RESULT",
        "F_RETRY_MANIFESTS",
        "F_RETRY_EVIDENCE",
    ),
    "OPS-10": (
        "F_OPS10",
        "F_OPS10_MEASURED",
        "F_OPS10_A",
        "F_OPS10_B",
        "F_OPS10_CHANGED",
        "F_OPS10_A_MATCH",
        "F_OPS10_B_MATCH",
        "F_OPS10_RESTORED",
        "F_OPS10_EVIDENCE",
    ),
    "OPS-12": (
        "F_C2",
        "F_C2_MEASURED",
        "F_C1",
        "F_C1_COUNT",
        "F_C1_NETWORK",
        "F_A_CONFIG",
        "F_OPS10_RESTORED",
        "F_C2_NETWORK_MATCH",
        "F_C2_TIME_CHECK",
        "F_C2_ZERO",
        "F_C2_CONNECTED",
        "F_C2_QR_COUNT",
        "F_C2_MANUAL_COUNT",
        "F_C2_LANGS",
        "F_C2_START",
        "F_C2_END",
        "F_C2_SEC",
        "F_C1_DEVICES",
        "F_C1_EVIDENCE",
        "F_C2_DEVICES",
        "F_C2_EVIDENCE",
    ),
    "STU-11": (
        "F_STU11",
        "F_STU11_CB",
        "F_STU11_CB_OK",
        "F_STU11_CB_NOAPP",
        "F_STU11_CB_VIEW",
        "F_STU11_CB_TOUCH",
        "F_STU11_CB_LOCK",
        "F_STU11_CB_EVIDENCE",
        "F_STU11_CB_SHA",
        "F_STU11_IOS",
        "F_STU11_IOS_OK",
        "F_STU11_IOS_NOAPP",
        "F_STU11_IOS_VIEW",
        "F_STU11_IOS_TOUCH",
        "F_STU11_IOS_LOCK",
        "F_STU11_IOS_EVIDENCE",
        "F_STU11_IOS_SHA",
    ),
    "PERF-01": (
        "F_C4",
        "F_C4_MEASURED",
        "F_C4_VALIDATED",
        "F_C4_RUN",
        "F_C4_VALIDATION",
        "F_C1",
        "F_C1_COUNT",
        "F_C4_POPULATION",
        "F_C4_COMPLETE",
        "F_C4_BROWSER_DEVICES",
        "F_C4_COUNT",
        "F_C4_EN_COUNT",
        "F_C4_ZH_COUNT",
        "F_C4_REPS",
        "F_C4_INPUT_KIND",
        "F_C4_INPUT",
        "F_C4_INPUT_EVIDENCE",
        "F_C4_MINUTES",
        "F_C4_FINALS",
        "F_C4_MATCHED",
        "F_C4_EXPECTED",
        "F_C4_MED",
        "F_C4_MAX",
        "F_C4_COMMIT",
        "F_C4_CONFIG",
        "F_C4_RUNTIME",
        "F_C4_EVIDENCE",
    ),
    "PERF-02": (
        "F_PERF02",
        "F_PERF02_MEASURED",
        "F_ACC_VALIDATED",
        "F_ACC_AUDIO_MIN",
        "F_ACC_AUDIO_SHA",
        "F_ACC_ASR",
        "F_ACC_ENGINE",
        "F_ACC_RSS",
        "F_ACC_CRASHES",
        "F_ACC_ERRORS",
        "F_ACC_CPU",
        "F_ACC_AUDIO_EVIDENCE",
        "F_ACC_REPORT",
        "F_ACC_MANIFEST",
        "F_ACC_VALIDATION",
        "F_ACC_NOCACHE_OK",
        "F_ACC_NOCACHE_VALIDATED",
        "F_ACC_NOCACHE_REPORT",
        "F_ACC_NOCACHE_MANIFEST",
        "F_ACC_NOCACHE_VALIDATION",
    ),
    "QUAL-01": (
        "F_C6",
        "F_C6_MEASURED",
        "F_C6_PROVENANCE_PASSED",
        "F_C6_PROVENANCE",
        "F_C6_PROVENANCE_SHA",
        "F_C6_DATA_PASSED",
        "F_C6_ROWS",
        "F_C6_REQUIRED",
        "F_C6_EN_COUNT",
        "F_C6_EN_SCORE",
        "F_C6_ZH_COUNT",
        "F_C6_SCORE",
        "F_C6_NUMBERS",
        "F_C6_DISTINCT",
        "F_C6_DOMAIN_COVERAGE",
        "F_C6_ACOUSTIC_COVERAGE",
        "F_C6_SEGMENTS",
        "F_C6_TRANSCRIPT_BOUND",
        "F_C6_EVALUATOR_ASSIGNMENTS",
        "F_C6_ASR_REVIEW",
        "F_C6_ASR_ERRORS",
        "F_C6_EVALUATOR",
        "F_C6_SOURCE",
        "F_C6_FILE",
        "F_C6_SHA",
    ),
}

STORY_EVIDENCE_KEYS: dict[str, tuple[str, ...]] = {
    "OPS-01": ("F_SETUP_EVIDENCE",),
    "OPS-02": (
        "F_RETRY_DONE_STATE",
        "F_RETRY_DONE_AFTER",
        "F_RETRY_MANIFESTS",
        "F_RETRY_EVIDENCE",
    ),
    "OPS-10": ("F_OPS10_EVIDENCE",),
    "OPS-12": (
        "F_C1_DEVICES",
        "F_C1_EVIDENCE",
        "F_C2_DEVICES",
        "F_C2_EVIDENCE",
    ),
    "STU-11": ("F_STU11_CB_EVIDENCE", "F_STU11_IOS_EVIDENCE"),
    "PERF-01": (
        "F_C4_RUN",
        "F_C4_VALIDATION",
        "F_C4_INPUT_EVIDENCE",
        "F_C4_COMMIT",
        "F_C4_CONFIG",
        "F_C4_RUNTIME",
        "F_C4_EVIDENCE",
    ),
    "PERF-02": (
        "F_ACC_AUDIO_EVIDENCE",
        "F_ACC_REPORT",
        "F_ACC_MANIFEST",
        "F_ACC_VALIDATION",
        "F_ACC_NOCACHE_REPORT",
        "F_ACC_NOCACHE_MANIFEST",
        "F_ACC_NOCACHE_VALIDATION",
    ),
    "QUAL-01": ("F_C6_FILE", "F_C6_PROVENANCE"),
}

STORY_STATUS_KEYS = {
    "OPS-01": "F_OPS01",
    "OPS-02": "F_OPS02",
    "OPS-10": "F_OPS10",
    "OPS-12": "F_C2",
    "STU-11": "F_STU11",
    "PERF-01": "F_C4",
    "PERF-02": "F_PERF02",
    "QUAL-01": "F_C6",
}


def _file_revision(value: os.stat_result) -> SourceRevision:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _persisted_revision(value: ValidationRevision) -> SourceRevision:
    """Keep schema-2 shape while using NTFS ChangeTime as its change token."""

    return (value[0], value[1], value[2], value[3], value[5])


def _read_stable_regular_file(path: Path) -> tuple[bytes, ValidationRevision]:
    """Read exact bytes while rejecting links and identity/content revisions."""

    path = Path(os.path.abspath(path))
    if _is_link_or_junction(path):
        raise FieldEvidenceError(f"link/junction file is not valid evidence: {path}")
    try:
        lexical_before, revision_before = qa_evidence_manifest._snapshot_path_revision(
            path, description=f"field validation file {path.name}"
        )
    except (OSError, EvidenceManifestError) as exc:
        raise FieldEvidenceError(f"field validation file cannot be inspected: {path}") from exc
    if not stat.S_ISREG(lexical_before.st_mode):
        raise FieldEvidenceError(f"field validation path is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FieldEvidenceError(f"field validation file cannot be opened: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise FieldEvidenceError(f"field validation path is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise FieldEvidenceError(f"field validation file cannot be read: {path}") from exc
    finally:
        os.close(descriptor)
    try:
        lexical_after, revision_after = qa_evidence_manifest._snapshot_path_revision(
            path, description=f"field validation file {path.name}"
        )
    except (OSError, EvidenceManifestError) as exc:
        raise FieldEvidenceError(f"field validation file cannot be rechecked: {path}") from exc
    descriptor_revision = _file_revision(before)
    if (
        _is_link_or_junction(path)
        or descriptor_revision != _file_revision(after)
        or revision_before != revision_after
        or not stat.S_ISREG(lexical_after.st_mode)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (
            lexical_after.st_dev,
            lexical_after.st_ino,
            lexical_after.st_size,
            lexical_after.st_mtime_ns,
        )
    ):
        raise FieldEvidenceError(f"field validation file changed while read: {path}")
    data = b"".join(chunks)
    digest = hashlib.sha256(data).hexdigest()
    read_set = _ACTIVE_READ_SET.get()
    if read_set is not None:
        read_set.record_file(path, revision_after, digest)
    return data, revision_after


def _recheck_regular_file_revision(
    path: Path, expected: ValidationRevision
) -> None:
    if _is_link_or_junction(path):
        raise FieldEvidenceError(f"field validation file changed: {path}")
    try:
        current, revision = qa_evidence_manifest._snapshot_path_revision(
            Path(os.path.abspath(path)),
            description=f"field validation file {path.name}",
        )
    except (OSError, EvidenceManifestError) as exc:
        raise FieldEvidenceError(f"field validation file cannot be rechecked: {path}") from exc
    if (
        _is_link_or_junction(path)
        or not stat.S_ISREG(current.st_mode)
        or revision != expected
    ):
        raise FieldEvidenceError(f"field validation file changed: {path}")


def _directory_members(path: Path) -> tuple[tuple[str, str, int, int], ...]:
    members: list[tuple[str, str, int, int]] = []
    canonical_names: set[str] = set()
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                canonical_name = os.path.normcase(entry.name)
                if canonical_name in canonical_names:
                    raise FieldEvidenceError(
                        f"field validation directory has duplicate canonical members: {path}"
                    )
                canonical_names.add(canonical_name)
                member_path = path / entry.name
                identity = entry.stat(follow_symlinks=False)
                if _is_link_or_junction(member_path):
                    kind = "link-or-junction"
                elif stat.S_ISREG(identity.st_mode):
                    kind = "file"
                elif stat.S_ISDIR(identity.st_mode):
                    kind = "directory"
                else:
                    kind = f"other-{stat.S_IFMT(identity.st_mode)}"
                members.append(
                    (canonical_name, kind, identity.st_dev, identity.st_ino)
                )
    except FieldEvidenceError:
        raise
    except OSError as exc:
        raise FieldEvidenceError(
            f"field validation directory members cannot be read: {path}"
        ) from exc
    return tuple(sorted(members, key=lambda item: item[0].encode("utf-8")))


def _snapshot_directory_state(path: Path) -> tuple[ValidationRevision, str]:
    path = Path(os.path.abspath(path))
    if _is_link_or_junction(path):
        raise FieldEvidenceError(f"link/junction directory is not valid evidence: {path}")
    try:
        current, revision = qa_evidence_manifest._snapshot_path_revision(
            path, description=f"field validation directory {path.name}"
        )
    except (OSError, EvidenceManifestError) as exc:
        raise FieldEvidenceError(f"field validation directory cannot be read: {path}") from exc
    if not stat.S_ISDIR(current.st_mode) or _is_link_or_junction(path):
        raise FieldEvidenceError(f"field validation path is not a real directory: {path}")
    members_before = _directory_members(path)
    try:
        middle, middle_revision = qa_evidence_manifest._snapshot_path_revision(
            path, description=f"field validation directory {path.name}"
        )
    except (OSError, EvidenceManifestError) as exc:
        raise FieldEvidenceError(
            f"field validation directory cannot be rechecked: {path}"
        ) from exc
    members_after = _directory_members(path)
    try:
        after, after_revision = qa_evidence_manifest._snapshot_path_revision(
            path, description=f"field validation directory {path.name}"
        )
    except (OSError, EvidenceManifestError) as exc:
        raise FieldEvidenceError(
            f"field validation directory cannot be rechecked: {path}"
        ) from exc
    if (
        _is_link_or_junction(path)
        or not stat.S_ISDIR(middle.st_mode)
        or not stat.S_ISDIR(after.st_mode)
        or revision != middle_revision
        or revision != after_revision
        or members_before != members_after
    ):
        raise FieldEvidenceError(
            f"field validation directory changed while read: {path}"
        )
    membership_sha256 = _canonical_sha256(members_before)
    read_set = _ACTIVE_READ_SET.get()
    if read_set is not None:
        read_set.record_directory(path, revision, membership_sha256)
    return revision, membership_sha256


def _snapshot_directory_revision(path: Path) -> SourceRevision:
    return _persisted_revision(_snapshot_directory_state(path)[0])


def _recheck_directory_revision(path: Path, expected: SourceRevision) -> None:
    if _is_link_or_junction(path):
        raise FieldEvidenceError(f"field validation directory changed: {path}")
    try:
        current, revision = qa_evidence_manifest._snapshot_path_revision(
            Path(os.path.abspath(path)),
            description=f"field validation directory {path.name}",
        )
    except (OSError, EvidenceManifestError) as exc:
        raise FieldEvidenceError(
            f"field validation directory cannot be rechecked: {path}"
        ) from exc
    if (
        _is_link_or_junction(path)
        or not stat.S_ISDIR(current.st_mode)
        or _persisted_revision(revision) != expected
    ):
        raise FieldEvidenceError(f"field validation directory changed: {path}")


def _recheck_imported_field_module() -> None:
    try:
        current = snapshot_supplied_source_hashes(
            ROOT, lambda: _IMPORTED_VALIDATOR_PATHS
        )
    except EvidenceManifestError as exc:
        raise FieldEvidenceError("cannot recheck imported field validator source") from exc
    if (
        dict(current) != dict(_IMPORTED_VALIDATOR_SOURCE_SNAPSHOT)
        or current.revisions != _IMPORTED_VALIDATOR_SOURCE_SNAPSHOT.revisions
    ):
        raise FieldEvidenceError("an imported field validator source changed on disk")


def recheck_validation_witness(witness: FieldValidationWitness) -> None:
    """Revalidate a prior field result immediately before a caller commits."""

    if type(witness) is not FieldValidationWitness:
        raise FieldEvidenceError("field validation witness type is invalid")
    _recheck_imported_field_module()
    revision_indexes: dict[str, dict[str, ValidationRevision]] = {}
    for revision_entries, label in (
        (witness.file_revisions, "file"),
        (witness.directory_revisions, "directory"),
    ):
        if not isinstance(revision_entries, tuple) or not revision_entries:
            raise FieldEvidenceError(f"field validation witness {label} set is invalid")
        revisions: dict[str, ValidationRevision] = {}
        revision_canonical_paths: set[str] = set()
        for revision_entry in revision_entries:
            if (
                not isinstance(revision_entry, tuple)
                or len(revision_entry) != 2
                or not isinstance(revision_entry[0], str)
                or not Path(revision_entry[0]).is_absolute()
            ):
                raise FieldEvidenceError(
                    f"field validation witness {label} entry is invalid"
                )
            path, raw_revision = revision_entry
            canonical_path = os.path.normcase(os.path.abspath(path))
            if canonical_path in revision_canonical_paths:
                raise FieldEvidenceError(
                    f"field validation witness {label} path is duplicated"
                )
            revision_canonical_paths.add(canonical_path)
            revisions[path] = _validated_validation_revision(
                raw_revision, f"field validation witness {label}"
            )
        revision_indexes[label] = revisions

    digest_indexes: dict[str, dict[str, str]] = {}
    for digest_entries, label in (
        (witness.file_sha256, "file"),
        (witness.directory_membership_sha256, "directory"),
    ):
        if not isinstance(digest_entries, tuple) or not digest_entries:
            raise FieldEvidenceError(
                f"field validation witness {label} digest set is invalid"
            )
        digests: dict[str, str] = {}
        digest_canonical_paths: set[str] = set()
        for digest_entry in digest_entries:
            if (
                not isinstance(digest_entry, tuple)
                or len(digest_entry) != 2
                or not isinstance(digest_entry[0], str)
                or not Path(digest_entry[0]).is_absolute()
                or not isinstance(digest_entry[1], str)
                or SHA256_RE.fullmatch(digest_entry[1]) is None
            ):
                raise FieldEvidenceError(
                    f"field validation witness {label} digest entry is invalid"
                )
            path, digest = digest_entry
            canonical_path = os.path.normcase(os.path.abspath(path))
            if canonical_path in digest_canonical_paths:
                raise FieldEvidenceError(
                    f"field validation witness {label} digest path is duplicated"
                )
            digest_canonical_paths.add(canonical_path)
            digests[path] = digest
        digest_indexes[label] = digests

    for label in ("file", "directory"):
        if set(revision_indexes[label]) != set(digest_indexes[label]):
            raise FieldEvidenceError(
                f"field validation witness {label} digest paths are invalid"
            )

    for path, revision in revision_indexes["file"].items():
        digest = _sha256(Path(path))
        if digest != digest_indexes["file"][path]:
            raise FieldEvidenceError(f"field validation file changed: {path}")
        _recheck_regular_file_revision(Path(path), revision)
    for path, revision in revision_indexes["directory"].items():
        current_revision, membership_sha256 = _snapshot_directory_state(Path(path))
        if (
            current_revision != revision
            or membership_sha256 != digest_indexes["directory"][path]
        ):
            raise FieldEvidenceError(f"field validation directory changed: {path}")


def _run_with_validation_read_set(
    operation: Callable[[], _T],
) -> tuple[_T, FieldValidationWitness | None]:
    existing = _ACTIVE_READ_SET.get()
    if existing is not None:
        return operation(), None
    read_set = _ValidationReadSet()
    token = _ACTIVE_READ_SET.set(read_set)
    try:
        result = operation()
        witness = read_set.freeze()
        recheck_validation_witness(witness)
        return result, witness
    finally:
        _ACTIVE_READ_SET.reset(token)


def _sha256(path: Path) -> str:
    """Hash a stable regular file without buffering large field artifacts."""

    path = Path(os.path.abspath(path))
    if _is_link_or_junction(path):
        raise FieldEvidenceError(f"link/junction file is not valid evidence: {path}")
    try:
        lexical_before, revision_before = qa_evidence_manifest._snapshot_path_revision(
            path, description=f"field validation file {path.name}"
        )
    except (OSError, EvidenceManifestError) as exc:
        raise FieldEvidenceError(f"field validation file cannot be inspected: {path}") from exc
    if not stat.S_ISREG(lexical_before.st_mode):
        raise FieldEvidenceError(f"field validation path is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FieldEvidenceError(f"field validation file cannot be opened: {path}") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise FieldEvidenceError(f"field validation path is not a regular file: {path}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise FieldEvidenceError(f"field validation file cannot be read: {path}") from exc
    finally:
        os.close(descriptor)
    try:
        lexical_after, revision_after = qa_evidence_manifest._snapshot_path_revision(
            path, description=f"field validation file {path.name}"
        )
    except (OSError, EvidenceManifestError) as exc:
        raise FieldEvidenceError(f"field validation file cannot be rechecked: {path}") from exc
    descriptor_revision = _file_revision(before)
    if (
        _is_link_or_junction(path)
        or descriptor_revision != _file_revision(after)
        or revision_before != revision_after
        or not stat.S_ISREG(lexical_after.st_mode)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (
            lexical_after.st_dev,
            lexical_after.st_ino,
            lexical_after.st_size,
            lexical_after.st_mtime_ns,
        )
    ):
        raise FieldEvidenceError(f"field validation file changed while hashed: {path}")
    hexdigest = digest.hexdigest()
    read_set = _ACTIVE_READ_SET.get()
    if read_set is not None:
        read_set.record_file(path, revision_after, hexdigest)
    return hexdigest


def _canonical_sha256(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FieldEvidenceError(f"JSON keyが重複しています: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise FieldEvidenceError(f"JSONの非有限数を許可しません: {value}")


def _decode_strict_json(data: bytes, path: Path) -> Any:
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FieldEvidenceError(f"JSONを安全に読めません: {path}") from exc


def _load_strict_json_snapshot(path: Path) -> tuple[Any, str]:
    try:
        data, _revision = _read_stable_regular_file(path)
    except FieldEvidenceError as exc:
        raise FieldEvidenceError(f"JSONを安全に読めません: {path}") from exc
    return _decode_strict_json(data, path), hashlib.sha256(data).hexdigest()


def load_strict_json(path: Path) -> Any:
    return _load_strict_json_snapshot(path)[0]


def _is_link_or_junction(path: Path) -> bool:
    try:
        is_junction = getattr(os.path, "isjunction", lambda _path: False)
        return path.is_symlink() or bool(is_junction(path))
    except OSError:
        return True


def _assert_no_link_components(root: Path, path: Path) -> None:
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise FieldEvidenceError(f"repo外pathを拒否します: {path}") from exc
    current = root
    if _is_link_or_junction(current):
        raise FieldEvidenceError(f"repo rootがlink/junctionです: {root}")
    for part in relative.parts:
        current /= part
        if current.exists() and _is_link_or_junction(current):
            raise FieldEvidenceError(f"link/junctionを拒否します: {current}")


def _relative_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise FieldEvidenceError(f"不正なrepo相対pathです: {value!r}")
    if "\r" in value or "\n" in value:
        raise FieldEvidenceError("改行を含むpathを拒否します")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise FieldEvidenceError(f"不正なrepo相対pathです: {value!r}")
    if pure.as_posix() != value:
        raise FieldEvidenceError(f"正規化されていないpathです: {value!r}")
    return pure.parts


def _resolve_repo_path(root: Path, value: str, *, must_exist: bool = True) -> Path:
    parts = _relative_parts(value)
    root = Path(os.path.abspath(root))
    path = root.joinpath(*parts)
    _assert_no_link_components(root, path)
    if must_exist and not path.exists():
        raise FieldEvidenceError(f"証拠pathが存在しません: {value}")
    try:
        resolved = path.resolve(strict=must_exist)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise FieldEvidenceError(f"repo外pathを拒否します: {value}") from exc
    return path


def _validated_field_date(value: str) -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise FieldEvidenceError("field dateはYYYY-MM-DD形式が必要です")
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise FieldEvidenceError("field dateが実在日ではありません") from exc
    return value


def _field_date_from_run_dir(run_dir: Path, root: Path) -> tuple[str, Path]:
    root_abs = Path(os.path.abspath(root))
    raw_parts = run_dir.parts
    if ".." in raw_parts:
        raise FieldEvidenceError("run-dirに '..' は使えません")
    absolute = Path(os.path.abspath(run_dir if run_dir.is_absolute() else root_abs / run_dir))
    _assert_no_link_components(root_abs, absolute)
    try:
        relative = absolute.relative_to(root_abs)
    except ValueError as exc:
        raise FieldEvidenceError("field run-dirはrepo内である必要があります") from exc
    if len(relative.parts) != 4 or relative.parts[:3] != ("docs", "field", "evidence"):
        raise FieldEvidenceError("field run-dirはdocs/field/evidence/YYYY-MM-DD固定です")
    field_date = relative.parts[3]
    _validated_field_date(field_date)
    if not absolute.is_dir():
        raise FieldEvidenceError("field run-dirが存在しません")
    return field_date, absolute


def _walk_evidence_files(evidence_dir: Path, root: Path) -> list[Path]:
    _assert_no_link_components(root, evidence_dir)
    files: list[Path] = []
    file_identities: dict[tuple[int, int], Path] = {}
    directory_revisions: dict[Path, SourceRevision] = {}
    for current, directories, names in os.walk(evidence_dir, followlinks=False):
        current_path = Path(current)
        directory_revisions[current_path] = _snapshot_directory_revision(current_path)
        for name in directories:
            directory = current_path / name
            if _is_link_or_junction(directory):
                raise FieldEvidenceError(f"証拠tree内のlink/junctionを拒否します: {directory}")
        for name in names:
            path = current_path / name
            if _is_link_or_junction(path) or not path.is_file():
                raise FieldEvidenceError(f"証拠tree内のlinkを拒否します: {path}")
            try:
                identity = path.lstat()
            except OSError as exc:
                raise FieldEvidenceError(
                    f"cannot inspect field evidence file: {path}"
                ) from exc
            if not stat.S_ISREG(identity.st_mode) or identity.st_size <= 0:
                raise FieldEvidenceError(f"空の証拠fileを拒否します: {path}")
            file_identity = (identity.st_dev, identity.st_ino)
            previous = file_identities.setdefault(file_identity, path)
            if previous != path:
                raise FieldEvidenceError(
                    f"field evidence hardlink alias is not allowed: {previous}, {path}"
                )
            files.append(path)
    for directory, revision in directory_revisions.items():
        _recheck_directory_revision(directory, revision)
    if not files:
        raise FieldEvidenceError("field evidence directoryが空です")
    return sorted(files, key=lambda path: path.relative_to(root).as_posix().encode("utf-8"))


def _parse_manifest(path: Path, expected_prefix: str) -> dict[str, str]:
    try:
        data, _revision = _read_stable_regular_file(path)
        text = data.decode("utf-8")
    except (FieldEvidenceError, UnicodeError) as exc:
        raise FieldEvidenceError("field SHA-256 manifestを読めません") from exc
    if not text or "\r" in text:
        raise FieldEvidenceError("field SHA-256 manifestは非空UTF-8/LF形式が必要です")
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise FieldEvidenceError(f"manifest行がGNU sha256sum形式ではありません: {line!r}")
        digest, relative = match.groups()
        _relative_parts(relative)
        if not relative.startswith(expected_prefix + "/"):
            raise FieldEvidenceError(f"manifestに日付dir外のpathがあります: {relative}")
        if relative in result:
            raise FieldEvidenceError(f"manifest pathが重複しています: {relative}")
        result[relative] = digest
    return result


def _validate_outer_manifest(root: Path, field_date: str) -> dict[str, Any]:
    _recheck_imported_field_module()
    evidence_relative = f"docs/field/evidence/{field_date}"
    manifest_relative = f"docs/field/{field_date}-evidence-sha256.txt"
    evidence_dir = _resolve_repo_path(root, evidence_relative)
    manifest_path = _resolve_repo_path(root, manifest_relative)
    if not evidence_dir.is_dir() or not manifest_path.is_file() or manifest_path.stat().st_size <= 0:
        raise FieldEvidenceError("field evidence dirまたはmanifestが不正です")
    files = _walk_evidence_files(evidence_dir, root)
    expected = {path.relative_to(root).as_posix(): _sha256(path) for path in files}
    recorded = _parse_manifest(manifest_path, evidence_relative)
    if recorded != expected:
        missing = sorted(set(expected) - set(recorded))
        extra = sorted(set(recorded) - set(expected))
        changed = sorted(key for key in set(expected) & set(recorded) if expected[key] != recorded[key])
        raise FieldEvidenceError(
            f"field manifestがtreeと一致しません: missing={missing}, extra={extra}, changed={changed}"
        )
    files_after = _walk_evidence_files(evidence_dir, root)
    before_names = tuple(path.relative_to(root).as_posix() for path in files)
    after_names = tuple(path.relative_to(root).as_posix() for path in files_after)
    if before_names != after_names:
        raise FieldEvidenceError("field evidence file set changed during validation")
    return {
        "evidence_dir": evidence_dir,
        "manifest_path": manifest_path,
        "manifest_sha256": _sha256(manifest_path),
        "files": recorded,
    }


def validate_outer_manifest(root: Path, field_date: str) -> dict[str, Any]:
    result, witness = _run_with_validation_read_set(
        lambda: _validate_outer_manifest(root, field_date)
    )
    if witness is None:
        return result
    return ValidatedOuterManifest(result, witness)


def rewrite_outer_manifest(root: Path, field_date: str) -> Path:
    evidence_relative = f"docs/field/evidence/{field_date}"
    evidence_dir = _resolve_repo_path(root, evidence_relative)
    manifest_path = root / "docs" / "field" / f"{field_date}-evidence-sha256.txt"
    _assert_no_link_components(root, manifest_path)
    files = _walk_evidence_files(evidence_dir, root)
    content = "".join(
        f"{_sha256(path)}  {path.relative_to(root).as_posix()}\n" for path in files
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=manifest_path.parent,
        prefix=f".{manifest_path.name}.", suffix=".tmp",
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.replace(temporary, manifest_path)
    return manifest_path


def _reviewed_tree_files(root: Path) -> list[Path]:
    if _is_link_or_junction(root):
        raise FieldEvidenceError(f"source subtree must not be a link/junction: {root}")
    if not root.is_dir():
        return []
    files: list[Path] = []
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        if _is_link_or_junction(current_path):
            raise FieldEvidenceError(
                f"source subtree must not contain a link/junction: {current_path}"
            )
        retained_directories: list[str] = []
        for name in directories:
            directory = current_path / name
            if _is_link_or_junction(directory):
                raise FieldEvidenceError(
                    f"source subtree must not contain a link/junction: {directory}"
                )
            if not (name.startswith(".") or name == "__pycache__"):
                retained_directories.append(name)
        directories[:] = retained_directories
        for name in names:
            path = current_path / name
            if _is_link_or_junction(path):
                raise FieldEvidenceError(
                    f"source file must not be a link/junction: {path}"
                )
            if name.startswith(".") or path.suffix == ".pyc":
                continue
            try:
                identity = path.lstat()
            except OSError as exc:
                raise FieldEvidenceError(f"cannot inspect source file: {path}") from exc
            if not stat.S_ISREG(identity.st_mode):
                raise FieldEvidenceError(f"source path is not a regular file: {path}")
            files.append(path)
    return files


def field_source_files(root: Path = ROOT) -> list[Path]:
    """Sources whose bytes define the tested product and field evidence gates."""
    candidates = [
        *_reviewed_tree_files(root / "server"),
        *_reviewed_tree_files(root / "web"),
        *_reviewed_tree_files(root / "scripts"),
        root / "start.bat",
        root / "setup.ps1",
        root / "config.yaml",
        root / "requirements.txt",
        root / "requirements-bench.txt",
        root / "docs" / "qa" / "stories.tsv",
        root / "docs" / "field-verification-checklist.md",
    ]
    unique: dict[str, Path] = {}
    for path in candidates:
        if _is_link_or_junction(path):
            raise FieldEvidenceError(f"source file must not be a link/junction: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        canonical = os.path.normcase(relative)
        if canonical in unique:
            raise FieldEvidenceError(f"duplicate source path: {relative}")
        unique[canonical] = path
    return sorted(unique.values(), key=lambda path: path.relative_to(root).as_posix())


def _field_source_directory_revisions(root: Path) -> dict[str, SourceRevision]:
    revisions: dict[str, SourceRevision] = {}
    for parent_relative in SOURCE_PARENT_RELATIVES:
        parent = root / parent_relative
        if _is_link_or_junction(parent):
            raise FieldEvidenceError(
                f"source parent must not be a link/junction: {parent}"
            )
        if not parent.exists():
            continue
        relative = parent.relative_to(root).as_posix()
        revisions[relative] = _snapshot_directory_revision(parent)
    for tree_relative in SOURCE_TREE_RELATIVES:
        tree = root / tree_relative
        if _is_link_or_junction(tree):
            raise FieldEvidenceError(f"source subtree must not be a link/junction: {tree}")
        if not tree.exists():
            continue
        for current, directories, _names in os.walk(tree, followlinks=False):
            current_path = Path(current)
            relative = current_path.relative_to(root).as_posix()
            revisions[relative] = _snapshot_directory_revision(current_path)
            retained_directories: list[str] = []
            for name in directories:
                directory = current_path / name
                if _is_link_or_junction(directory):
                    raise FieldEvidenceError(
                        f"source subtree must not contain a link/junction: {directory}"
                    )
                if not (name.startswith(".") or name == "__pycache__"):
                    retained_directories.append(name)
            directories[:] = retained_directories
    for relative, revision in revisions.items():
        _recheck_directory_revision(root / Path(relative), revision)
    return dict(sorted(revisions.items()))


def _record_source_snapshot(root: Path, snapshot: FieldSourceHashSnapshot) -> None:
    read_set = _ACTIVE_READ_SET.get()
    if read_set is None:
        return
    for relative, revision in snapshot.validation_revisions:
        read_set.record_file(root / Path(relative), revision, snapshot[relative])


def hash_field_sources(root: Path = ROOT) -> FieldSourceHashSnapshot:
    root = Path(os.path.abspath(root))
    directories_before = _field_source_directory_revisions(root)
    try:
        snapshot = snapshot_supplied_source_hashes(
            root, lambda: field_source_files(root)
        )
    except (EvidenceManifestError, OSError) as exc:
        raise FieldEvidenceError(f"cannot snapshot field source set: {exc}") from exc
    directories_after = _field_source_directory_revisions(root)
    if directories_before != directories_after:
        raise FieldEvidenceError("field source directory set changed during snapshot")
    result = FieldSourceHashSnapshot(
        dict(snapshot), dict(snapshot.revisions), directories_before
    )
    _record_source_snapshot(root, result)
    return result


def _validated_repo_hash_index(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise FieldEvidenceError(f"{field}は非空objectである必要があります")
    result: dict[str, str] = {}
    for relative, digest in value.items():
        if not isinstance(relative, str):
            raise FieldEvidenceError(f"{field}のpathは文字列である必要があります")
        _relative_parts(relative)
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise FieldEvidenceError(f"{field}のSHA-256が不正です: {relative}")
        result[relative] = digest
    return result


def _validated_revision_tuple(value: Any, field_name: str) -> SourceRevision:
    if (
        not isinstance(value, tuple)
        or len(value) != 5
        or any(type(component) is not int or component < 0 for component in value)
    ):
        raise FieldEvidenceError(f"{field_name} source revision is invalid")
    return value


def _validated_validation_revision(
    value: Any, field_name: str
) -> ValidationRevision:
    if (
        not isinstance(value, tuple)
        or len(value) != 6
        or any(type(component) is not int or component < 0 for component in value)
    ):
        raise FieldEvidenceError(f"{field_name} revision is invalid")
    return value


def _revision_payload(value: SourceRevision) -> dict[str, int]:
    revision = _validated_revision_tuple(value, "in-memory")
    return dict(zip(("dev", "ino", "size", "mtime_ns", "ctime_ns"), revision))


def _source_revision_payload(
    snapshot: FieldSourceHashSnapshot,
) -> dict[str, dict[str, dict[str, int]]]:
    _require_field_source_snapshot(snapshot, "field source snapshot")
    return {
        "files": {
            relative: _revision_payload(revision)
            for relative, revision in snapshot.persisted_revisions
        },
        "directories": {
            relative: _revision_payload(revision)
            for relative, revision in snapshot.directory_revisions
        },
    }


def _validated_revision_index(
    value: Any, field_name: str, *, allow_repo_root: bool = False
) -> dict[str, SourceRevision]:
    if not isinstance(value, dict):
        raise FieldEvidenceError(f"{field_name} source revisions must be an object")
    result: dict[str, SourceRevision] = {}
    for relative, raw_revision in value.items():
        if not isinstance(relative, str):
            raise FieldEvidenceError(f"{field_name} source revision path is invalid")
        if relative != "." or not allow_repo_root:
            _relative_parts(relative)
        if not isinstance(raw_revision, dict) or set(raw_revision) != SOURCE_REVISION_KEYS:
            raise FieldEvidenceError(f"{field_name} source revision shape is invalid: {relative}")
        components = tuple(
            raw_revision[key]
            for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")
        )
        if any(type(component) is not int or component < 0 for component in components):
            raise FieldEvidenceError(f"{field_name} source revision value is invalid: {relative}")
        result[relative] = components
    return dict(sorted(result.items()))


def _validated_source_revision_payload(
    value: Any, source_hashes: Mapping[str, str], field_name: str
) -> dict[str, dict[str, dict[str, int]]]:
    if not isinstance(value, dict) or set(value) != SOURCE_REVISION_INDEX_KEYS:
        raise FieldEvidenceError(f"{field_name} source revision index schema is invalid")
    file_revisions = _validated_revision_index(value.get("files"), field_name)
    directory_revisions = _validated_revision_index(
        value.get("directories"), field_name, allow_repo_root=True
    )
    if set(file_revisions) != set(source_hashes) or not directory_revisions:
        raise FieldEvidenceError(f"{field_name} source revision paths are invalid")
    return {
        "files": {
            relative: _revision_payload(revision)
            for relative, revision in file_revisions.items()
        },
        "directories": {
            relative: _revision_payload(revision)
            for relative, revision in directory_revisions.items()
        },
    }


def _require_field_source_snapshot(
    value: Any, field_name: str
) -> FieldSourceHashSnapshot:
    if type(value) is not FieldSourceHashSnapshot or not value:
        raise FieldEvidenceError(
            f"{field_name} must be an intact FieldSourceHashSnapshot"
        )
    hashes = _validated_repo_hash_index(value, field_name)
    validation_revisions = dict(value.revisions)
    file_revisions = dict(value.persisted_revisions)
    directory_revisions = dict(value.directory_revisions)
    if (
        set(file_revisions) != set(hashes)
        or set(validation_revisions) != set(hashes)
        or len(file_revisions) != len(value.persisted_revisions)
        or len(validation_revisions) != len(value.revisions)
        or not directory_revisions
        or len(directory_revisions) != len(value.directory_revisions)
    ):
        raise FieldEvidenceError(f"{field_name} revision paths are invalid")
    for relative, revision in file_revisions.items():
        _relative_parts(relative)
        _validated_revision_tuple(revision, field_name)
        _validated_validation_revision(validation_revisions[relative], field_name)
    for relative, revision in directory_revisions.items():
        if relative != ".":
            _relative_parts(relative)
        _validated_revision_tuple(revision, field_name)
    return value


def _source_snapshots_equal(left: Any, right: Any) -> bool:
    try:
        left_snapshot = _require_field_source_snapshot(left, "left source snapshot")
        right_snapshot = _require_field_source_snapshot(right, "right source snapshot")
    except FieldEvidenceError:
        return False
    return (
        dict(left_snapshot) == dict(right_snapshot)
        and left_snapshot.revisions == right_snapshot.revisions
        and left_snapshot.directory_revisions == right_snapshot.directory_revisions
    )


def _source_snapshot_sha256(
    source_hashes: Mapping[str, str],
    source_revisions: Mapping[str, Any],
) -> str:
    return _canonical_sha256(
        {
            "source_hashes": dict(source_hashes),
            "source_revisions": dict(source_revisions),
        }
    )


def _host_fingerprint_sha256() -> str:
    hostname = (os.environ.get("COMPUTERNAME") or platform.node()).strip()
    if not hostname:
        raise FieldEvidenceError("学校PCのhostnameを取得できません")
    return hashlib.sha256(hostname.encode("utf-8")).hexdigest()


def _source_snapshot_relative(field_date: str, phase: str) -> str:
    if phase not in SOURCE_SNAPSHOT_FILENAMES:
        raise FieldEvidenceError(f"source snapshot phaseが不正です: {phase}")
    return (
        f"docs/field/evidence/{_validated_field_date(field_date)}/"
        f"{SOURCE_SNAPSHOT_FILENAMES[phase]}"
    )


def _parse_source_snapshot(
    path: Path, *, field_date: str, phase: str
) -> tuple[dict[str, Any], str]:
    payload, artifact_sha256 = _load_strict_json_snapshot(path)
    if not isinstance(payload, dict) or set(payload) != SOURCE_SNAPSHOT_KEYS:
        raise FieldEvidenceError(f"{phase} source snapshot schemaが不正です")
    source_hashes = _validated_repo_hash_index(
        payload.get("source_hashes"), f"{phase} source snapshot"
    )
    source_revisions = _validated_source_revision_payload(
        payload.get("source_revisions"),
        source_hashes,
        f"{phase} source snapshot",
    )
    captured_at = _aware_time(payload.get("captured_at", ""))
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != SOURCE_SNAPSHOT_SCHEMA_VERSION
        or payload.get("kind") != SOURCE_SNAPSHOT_KIND
        or payload.get("field_date") != field_date
        or payload.get("phase") != phase
        or captured_at is None
        or (phase == "start" and captured_at.date().isoformat() != field_date)
        or not isinstance(payload.get("host_fingerprint_sha256"), str)
        or not SHA256_RE.fullmatch(payload["host_fingerprint_sha256"])
        or not isinstance(payload.get("session_id"), str)
        or not SHA256_RE.fullmatch(payload["session_id"])
        or payload.get("source_snapshot_sha256")
        != _source_snapshot_sha256(source_hashes, source_revisions)
    ):
        raise FieldEvidenceError(f"{phase} source snapshot bindingが不正です")
    predecessor = payload.get("predecessor_sha256")
    if (phase == "start" and predecessor is not None) or (
        phase == "end"
        and (
            not isinstance(predecessor, str)
            or not SHA256_RE.fullmatch(predecessor)
        )
    ):
        raise FieldEvidenceError(f"{phase} source snapshot predecessorが不正です")
    payload["source_hashes"] = source_hashes
    payload["source_revisions"] = source_revisions
    return payload, artifact_sha256


def write_field_source_snapshot(
    *, field_date: str, phase: str, root: Path = ROOT
) -> dict[str, Any]:
    """Write one immutable school-PC source snapshot at its fixed evidence path."""
    _recheck_imported_field_module()
    root = Path(os.path.abspath(root))
    field_date = _validated_field_date(field_date)
    captured_at = dt.datetime.now().astimezone()
    if phase == "start" and field_date != captured_at.date().isoformat():
        raise FieldEvidenceError(
            "start source snapshot field_date must be the current local date"
        )
    relative = _source_snapshot_relative(field_date, phase)
    target = _resolve_repo_path(root, relative, must_exist=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_link_components(root, target)
    if target.exists():
        raise FieldEvidenceError(f"source snapshotは上書きできません: {relative}")

    source_snapshot = hash_field_sources(root)
    source_hashes = dict(source_snapshot)
    source_revisions = _source_revision_payload(source_snapshot)
    if not source_hashes or not _source_snapshots_equal(
        source_snapshot, hash_field_sources(root)
    ):
        raise FieldEvidenceError("source snapshot取得中にsourceが変化しました")
    predecessor_sha256: str | None = None
    if phase == "start":
        session_id = secrets.token_hex(32)
        end_path = _resolve_repo_path(
            root, _source_snapshot_relative(field_date, "end"), must_exist=False
        )
        if end_path.exists():
            raise FieldEvidenceError("end snapshotが既に存在するためstartを作れません")
    elif phase == "end":
        start_relative = _source_snapshot_relative(field_date, "start")
        start_path = _resolve_repo_path(root, start_relative)
        start_payload, predecessor_sha256 = _parse_source_snapshot(
            start_path, field_date=field_date, phase="start"
        )
        if (
            start_payload["source_hashes"] != source_hashes
            or start_payload["source_revisions"] != source_revisions
            or start_payload["host_fingerprint_sha256"]
            != _host_fingerprint_sha256()
        ):
            raise FieldEvidenceError(
                "field開始後にsourceまたは学校PCが変化したためend snapshotを作れません"
            )
        if _sha256(start_path) != predecessor_sha256:
            raise FieldEvidenceError("start source snapshotが検証中に変化しました")
        start_captured_at = _aware_time(start_payload["captured_at"])
        if (
            start_captured_at is None
            or captured_at <= start_captured_at
            or captured_at.date().isoformat() < field_date
        ):
            raise FieldEvidenceError(
                "end source snapshotはstartより後のローカル日時で取得する必要があります"
            )
        session_id = start_payload["session_id"]
    else:
        raise FieldEvidenceError(f"source snapshot phaseが不正です: {phase}")

    payload = {
        "schema_version": SOURCE_SNAPSHOT_SCHEMA_VERSION,
        "kind": SOURCE_SNAPSHOT_KIND,
        "field_date": field_date,
        "phase": phase,
        "captured_at": captured_at.isoformat(),
        "host_fingerprint_sha256": _host_fingerprint_sha256(),
        "session_id": session_id,
        "predecessor_sha256": predecessor_sha256,
        "source_hashes": source_hashes,
        "source_revisions": source_revisions,
        "source_snapshot_sha256": _source_snapshot_sha256(
            source_hashes, source_revisions
        ),
    }
    data = json.dumps(
        payload, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True
    ) + "\n"
    try:
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise FieldEvidenceError(f"source snapshotは上書きできません: {relative}") from exc
    if not _source_snapshots_equal(source_snapshot, hash_field_sources(root)):
        try:
            target.unlink()
        except OSError:
            pass
        raise FieldEvidenceError("source snapshot取得中にsourceが変化しました")
    try:
        _recheck_imported_field_module()
    except FieldEvidenceError:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return {
        "snapshot_path": target,
        "snapshot_sha256": _sha256(target),
        "source_snapshot_sha256": payload["source_snapshot_sha256"],
        "phase": phase,
    }


def _validate_source_snapshot_pair(
    *,
    root: Path,
    field_date: str,
    evidence_dir: Path,
    manifest_files: Mapping[str, str],
    start_relative: str,
    end_relative: str,
    current_sources: Mapping[str, str],
) -> dict[str, Any]:
    expected_start = _source_snapshot_relative(field_date, "start")
    expected_end = _source_snapshot_relative(field_date, "end")
    if start_relative != expected_start or end_relative != expected_end:
        raise FieldEvidenceError("学校PC source snapshot参照が固定pathと一致しません")
    start_path = _validate_evidence_reference(
        root, evidence_dir, start_relative, manifest_files
    )
    end_path = _validate_evidence_reference(
        root, evidence_dir, end_relative, manifest_files
    )
    if not start_path.is_file() or not end_path.is_file() or start_path == end_path:
        raise FieldEvidenceError("学校PC source snapshotは別々の通常fileが必要です")
    start, start_sha256 = _parse_source_snapshot(
        start_path, field_date=field_date, phase="start"
    )
    end, end_sha256 = _parse_source_snapshot(
        end_path, field_date=field_date, phase="end"
    )
    start_time = _aware_time(start["captured_at"])
    end_time = _aware_time(end["captured_at"])
    current_snapshot = _require_field_source_snapshot(
        current_sources, "current field source snapshot"
    )
    current = dict(current_snapshot)
    current_revisions = _source_revision_payload(current_snapshot)
    if (
        start_time is None
        or end_time is None
        or start_time >= end_time
        or end.get("session_id") != start.get("session_id")
        or end.get("host_fingerprint_sha256")
        != start.get("host_fingerprint_sha256")
        or end.get("predecessor_sha256") != start_sha256
        or start.get("source_hashes") != end.get("source_hashes")
        or start.get("source_revisions") != end.get("source_revisions")
        or start.get("source_hashes") != current
        or start.get("source_revisions") != current_revisions
        or start.get("source_snapshot_sha256")
        != _source_snapshot_sha256(current, current_revisions)
    ):
        raise FieldEvidenceError(
            "学校PCのstart/end source snapshotと受領checkoutが一致しません"
        )
    if _sha256(start_path) != start_sha256 or _sha256(end_path) != end_sha256:
        raise FieldEvidenceError("source snapshotが検証中に変化しました")
    return {
        "source_snapshot_sha256": start["source_snapshot_sha256"],
        "session_id": start["session_id"],
        "start_captured_at": start["captured_at"],
        "end_captured_at": end["captured_at"],
        "host_fingerprint_sha256": start["host_fingerprint_sha256"],
    }


def _nonempty(details: Mapping[str, str], *keys: str) -> bool:
    return all(bool(details.get(key, "").strip()) for key in keys)


def _all_equal(details: Mapping[str, str], expected: str, *keys: str) -> bool:
    return all(details.get(key) == expected for key in keys)


def _decimal(value: str) -> Decimal | None:
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _shell_decimal(value: str) -> Decimal | None:
    return _decimal(value) if SHELL_DECIMAL_RE.fullmatch(value) else None


def _same_decimal(left: str, right: Any) -> bool:
    left_number = _decimal(left)
    right_number = _decimal(str(right))
    return (
        left_number is not None
        and right_number is not None
        and left_number == right_number
    )


def _integer(value: str) -> int | None:
    if not re.fullmatch(r"\d+", value):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _aware_time(value: str) -> dt.datetime | None:
    if not isinstance(value, str) or not FIELD_TIME_RE.fullmatch(value):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def recompute_story_candidate(story: str, details: Mapping[str, str]) -> bool:
    if story == "OPS-01":
        return _all_equal(details, "可", "F_SETUP_FRESH", "F_SETUP_OK", "F_SETUP_STOPPED") and _nonempty(
            details, "F_SETUP_ENV", "F_SETUP_EVIDENCE"
        )
    if story == "OPS-02":
        return (
            details.get("F_RETRY_EXECUTED") != "未実施"
            and _nonempty(details, "F_RETRY_ENV", "F_RETRY_DONE_MODEL", "F_RETRY_DONE_STATE", "F_RETRY_DONE_AFTER", "F_RETRY_ACTIVE", "F_RETRY_MANIFESTS", "F_RETRY_EVIDENCE")
            and details.get("F_RETRY_MARKER") == "False"
            and _all_equal(details, "可", "F_RETRY_SERVER", "F_RETRY_REUSED", "F_RETRY_RESULT")
            and details.get("F_RETRY_PARTIAL") in {"先頭から", "再開"}
        )
    if story == "OPS-10":
        return (
            _nonempty(details, "F_OPS10_A", "F_OPS10_B", "F_OPS10_EVIDENCE")
            and _all_equal(details, "可", "F_OPS10_CHANGED", "F_OPS10_A_MATCH", "F_OPS10_B_MATCH", "F_OPS10_RESTORED")
        )
    if story == "OPS-12":
        start = _aware_time(details.get("F_C2_START", ""))
        finish = _aware_time(details.get("F_C2_END", ""))
        seconds = _shell_decimal(details.get("F_C2_SEC", ""))
        time_matches = bool(
            start is not None
            and finish is not None
            and seconds is not None
            and seconds > 0
            and seconds <= 90
            and finish >= start
            and abs(Decimal(str((finish - start).total_seconds())) - seconds) <= 1
        )
        return (
            details.get("F_C1") == "可"
            and details.get("F_C1_COUNT") == "10"
            and _nonempty(details, "F_C1_NETWORK", "F_C2_LANGS", "F_C1_DEVICES", "F_C1_EVIDENCE", "F_C2_DEVICES", "F_C2_EVIDENCE")
            and details.get("F_A_CONFIG") == "校内Wi-Fi直結"
            and _all_equal(details, "可", "F_OPS10_RESTORED", "F_C2_NETWORK_MATCH", "F_C2_TIME_CHECK", "F_C2_ZERO")
            and details.get("F_C2_CONNECTED") == "10"
            and details.get("F_C2_QR_COUNT") == "10"
            and details.get("F_C2_MANUAL_COUNT") == "0"
            and time_matches
        )
    if story == "STU-11":
        return (
            _nonempty(details, "F_STU11_CB", "F_STU11_CB_EVIDENCE", "F_STU11_CB_SHA", "F_STU11_IOS", "F_STU11_IOS_EVIDENCE", "F_STU11_IOS_SHA")
            and _all_equal(
                details,
                "可",
                "F_STU11_CB_OK",
                "F_STU11_CB_NOAPP",
                "F_STU11_CB_VIEW",
                "F_STU11_CB_TOUCH",
                "F_STU11_CB_LOCK",
                "F_STU11_IOS_OK",
                "F_STU11_IOS_NOAPP",
                "F_STU11_IOS_VIEW",
                "F_STU11_IOS_TOUCH",
                "F_STU11_IOS_LOCK",
            )
        )
    if story == "PERF-01":
        count = _integer(details.get("F_C4_COUNT", ""))
        english = _integer(details.get("F_C4_EN_COUNT", ""))
        chinese = _integer(details.get("F_C4_ZH_COUNT", ""))
        finals = _integer(details.get("F_C4_FINALS", ""))
        matched = _integer(details.get("F_C4_MATCHED", ""))
        minutes = _shell_decimal(details.get("F_C4_MINUTES", ""))
        median = _shell_decimal(details.get("F_C4_MED", ""))
        maximum = _shell_decimal(details.get("F_C4_MAX", ""))
        return (
            details.get("F_C4_MEASURED") == "可"
            and details.get("F_C4_VALIDATED") == "可"
            and details.get("F_C1") == "可"
            and details.get("F_C1_COUNT") == "10"
            and details.get("F_C4_POPULATION") == "可"
            and details.get("F_C4_COMPLETE") == "可"
            and count == 10
            and english is not None
            and chinese is not None
            and english > 0
            and chinese > 0
            and english + chinese == 10
            and finals is not None
            and finals > 0
            and matched == finals * 2
            and details.get("F_C4_EXPECTED") == str(finals * 2)
            and details.get("F_C4_INPUT_KIND") in {"実授業ライブ", "実授業WAV"}
            and _nonempty(details, "F_C4_RUN", "F_C4_VALIDATION", "F_C4_REPS", "F_C4_INPUT", "F_C4_INPUT_EVIDENCE", "F_C4_COMMIT", "F_C4_CONFIG", "F_C4_RUNTIME", "F_C4_EVIDENCE")
            and minutes is not None
            and minutes >= 45
            and median is not None
            and median <= 5
            and maximum is not None
            and maximum <= 8
        )
    if story == "PERF-02":
        audio_minutes = _shell_decimal(details.get("F_ACC_AUDIO_MIN", ""))
        rss = _shell_decimal(details.get("F_ACC_RSS", ""))
        return (
            details.get("F_PERF02_MEASURED") == "可"
            and details.get("F_ACC_VALIDATED") == "可"
            and audio_minutes is not None
            and audio_minutes >= 45
            and rss is not None
            and rss <= 5000
            and details.get("F_ACC_CRASHES") == "0"
            and details.get("F_ACC_ERRORS") == "0"
            and bool(SHA256_RE.fullmatch(details.get("F_ACC_AUDIO_SHA", "").lower()))
            and _nonempty(details, "F_ACC_AUDIO_SHA", "F_ACC_ASR", "F_ACC_ENGINE", "F_ACC_AUDIO_EVIDENCE", "F_ACC_REPORT", "F_ACC_MANIFEST", "F_ACC_VALIDATION")
        )
    if story == "QUAL-01":
        asr_errors = _integer(details.get("F_C6_ASR_ERRORS", ""))
        return (
            details.get("F_C6_MEASURED") == "可"
            and details.get("F_C6_PROVENANCE_PASSED") == "true"
            and details.get("F_C6_DATA_PASSED") == "true"
            and details.get("F_C6_ROWS") == "30"
            and details.get("F_C6_REQUIRED") == "30"
            and details.get("F_C6_EN_COUNT") == "30"
            and details.get("F_C6_ZH_COUNT") == "30"
            and details.get("F_C6_NUMBERS") == "30"
            and details.get("F_C6_DISTINCT") == "30"
            and details.get("F_C6_DOMAIN_COVERAGE") == "3"
            and details.get("F_C6_ACOUSTIC_COVERAGE") == "3"
            and details.get("F_C6_SEGMENTS") == "30"
            and details.get("F_C6_TRANSCRIPT_BOUND") == "30"
            and details.get("F_C6_EVALUATOR_ASSIGNMENTS") == "90"
            and asr_errors is not None
            and bool(SHA256_RE.fullmatch(details.get("F_C6_SHA", "").lower()))
            and bool(
                SHA256_RE.fullmatch(
                    details.get("F_C6_PROVENANCE_SHA", "").lower()
                )
            )
            and _nonempty(details, "F_C6_FILE", "F_C6_PROVENANCE")
        )
    raise FieldEvidenceError(f"未知のfield storyです: {story}")


def _validate_evidence_reference(
    root: Path, evidence_dir: Path, relative: str, manifest_files: Mapping[str, str]
) -> Path:
    path = _resolve_repo_path(root, relative)
    try:
        path.resolve(strict=True).relative_to(evidence_dir.resolve(strict=True))
    except ValueError as exc:
        raise FieldEvidenceError(f"実施日dir外の証拠pathです: {relative}") from exc
    if path.is_file():
        if path.stat().st_size <= 0 or manifest_files.get(relative) != _sha256(path):
            raise FieldEvidenceError(f"manifest未収録またはhash不一致です: {relative}")
    elif path.is_dir():
        descendants = [candidate for candidate in _walk_evidence_files(path, root)]
        if not descendants or any(
            manifest_files.get(candidate.relative_to(root).as_posix()) != _sha256(candidate)
            for candidate in descendants
        ):
            raise FieldEvidenceError(f"directory証拠が完全にmanifest収録されていません: {relative}")
    else:
        raise FieldEvidenceError(f"通常file/directoryではない証拠です: {relative}")
    return path


def _performance_paths(details: Mapping[str, str], mode: str) -> tuple[str, str, str]:
    prefix = "F_ACC" if mode == "on" else "F_ACC_NOCACHE"
    return (
        details[f"{prefix}_VALIDATION"],
        details[f"{prefix}_MANIFEST"],
        details[f"{prefix}_REPORT"],
    )


def _recompute_cache_off_candidate(details: Mapping[str, str]) -> bool:
    cpu = _shell_decimal(details.get("F_ACC_CPU", ""))
    return (
        details.get("F_ACC_NOCACHE_VALIDATED") == "可"
        and cpu is not None
        and _nonempty(
            details,
            "F_ACC_NOCACHE_REPORT",
            "F_ACC_NOCACHE_MANIFEST",
            "F_ACC_NOCACHE_VALIDATION",
        )
    )


def _performance_source_hashes(root: Path) -> dict[str, str]:
    paths = {root / relative for relative in PERFORMANCE_SOURCE_RELATIVES}
    paths.update(path for path in root.glob("server/**/*.py") if path.is_file())
    missing = sorted(
        relative
        for relative in PERFORMANCE_SOURCE_RELATIVES
        if not (root / relative).is_file()
    )
    if missing:
        raise FieldEvidenceError(
            "performance sourceが不足しています: " + ", ".join(missing)
        )
    result: dict[str, str] = {}
    for path in sorted(paths, key=lambda item: item.as_posix()):
        _assert_no_link_components(root, path)
        relative = path.relative_to(root).as_posix()
        result[relative] = _sha256(path)
    return result


def _validated_opaque_hash_index(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise FieldEvidenceError(f"{field}は非空objectである必要があります")
    result: dict[str, str] = {}
    for name, digest in value.items():
        if (
            not isinstance(name, str)
            or not name
            or not name.isprintable()
            or "\x00" in name
            or "\r" in name
            or "\n" in name
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
        ):
            raise FieldEvidenceError(f"{field}のhash indexが不正です")
        result[name] = digest
    return result


def _verify_run_hash_index(
    run_dir: Path, value: Any, field: str
) -> dict[str, str]:
    index = _validated_repo_hash_index(value, field)
    for relative, expected_sha256 in index.items():
        path = run_dir.joinpath(*_relative_parts(relative))
        _assert_no_link_components(run_dir, path)
        try:
            path.resolve(strict=True).relative_to(run_dir.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise FieldEvidenceError(f"{field}がrun外を参照しています: {relative}") from exc
        if not path.is_file() or _sha256(path) != expected_sha256:
            raise FieldEvidenceError(f"{field}のhashが一致しません: {relative}")
    return index


def _runtime_snapshot_is_well_formed(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != PERFORMANCE_RUNTIME_KEYS:
        return False
    active = value.get("active_distributions")
    distributions = value.get("distributions")
    if (
        not isinstance(value.get("python"), str)
        or not value["python"]
        or not isinstance(value.get("executable"), str)
        or not value["executable"]
        or not isinstance(value.get("executable_sha256"), str)
        or not SHA256_RE.fullmatch(value["executable_sha256"])
        or not isinstance(active, list)
        or not active
        or not all(isinstance(name, str) and bool(name) for name in active)
        or active != sorted(set(active))
        or not isinstance(distributions, dict)
        or not distributions
        or not set(active) <= set(distributions)
    ):
        return False
    for name, distribution in distributions.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(distribution, dict)
            or set(distribution) != {"version", "metadata_hashes"}
            or not isinstance(distribution.get("version"), str)
            or not distribution["version"]
            or not isinstance(distribution.get("metadata_hashes"), dict)
        ):
            return False
        for metadata_name, digest in distribution["metadata_hashes"].items():
            if (
                metadata_name
                not in {"METADATA", "WHEEL", "RECORD", "direct_url.json"}
                or not isinstance(digest, str)
                or not SHA256_RE.fullmatch(digest)
            ):
                return False
    return True


def _continuous_lock_is_well_formed(
    value: Any,
    *,
    started_at: Any,
    finished_at: Any,
    run_relative: str,
    source_hashes: Mapping[str, str],
    input_hashes: Mapping[str, str],
    runtime_asset_hashes: Mapping[str, str],
    effective: Mapping[str, str],
    artifacts: Mapping[str, str],
) -> bool:
    if not isinstance(value, dict) or set(value) != PERFORMANCE_LOCK_KEYS:
        return False
    files = value.get("files")
    if (
        value.get("mode") != "windows-share-read"
        or value.get("acquired_at") != started_at
        or value.get("released_at") != finished_at
        or value.get("interrupted") is not False
        or not isinstance(files, list)
        or not files
        or not all(
            isinstance(path, str)
            and bool(path)
            and path.isprintable()
            and "\r" not in path
            and "\n" not in path
            for path in files
        )
        or files != sorted(set(files))
    ):
        return False
    required = {
        *source_hashes,
        *input_hashes,
        *runtime_asset_hashes,
        *(f"{run_relative}/{relative}" for relative in effective),
        *(f"{run_relative}/{relative}" for relative in artifacts),
    }
    return bool(required) and required <= set(files)


def _validate_saved_performance(
    details: Mapping[str, str], mode: str, root: Path, evidence_dir: Path,
    manifest_files: Mapping[str, str],
) -> dict[str, Any]:
    try:
        from scripts import validate_field_performance as field_performance
    except ImportError as exc:
        raise FieldEvidenceError(
            "performance受領側validatorの依存を読み込めません"
        ) from exc

    validation_relative, manifest_relative, report_relative = _performance_paths(details, mode)
    validation_path = _validate_evidence_reference(root, evidence_dir, validation_relative, manifest_files)
    manifest_path = _validate_evidence_reference(root, evidence_dir, manifest_relative, manifest_files)
    report_path = _validate_evidence_reference(root, evidence_dir, report_relative, manifest_files)
    if not all(path.is_file() for path in (validation_path, manifest_path, report_path)):
        raise FieldEvidenceError("performance参照は通常fileである必要があります")
    run_dir = manifest_path.parent
    if validation_path.parent != run_dir or report_path.parent != run_dir:
        raise FieldEvidenceError("performance validation/manifest/reportは同一run内である必要があります")
    if (
        validation_path.name != "field-validation.json"
        or manifest_path.name != PERFORMANCE_MANIFEST_NAME
    ):
        raise FieldEvidenceError("performance validation/manifest名が不正です")

    payload, validation_sha256 = _load_strict_json_snapshot(validation_path)
    manifest, manifest_sha256 = _load_strict_json_snapshot(manifest_path)
    report, report_sha256 = _load_strict_json_snapshot(report_path)
    if not isinstance(payload, dict) or not isinstance(manifest, dict) or not isinstance(report, dict):
        raise FieldEvidenceError("performance JSON rootはobjectである必要があります")
    if set(payload) != PERFORMANCE_VALIDATION_KEYS:
        raise FieldEvidenceError("performance validation root schemaが不正です")
    if (
        set(manifest) != PERFORMANCE_MANIFEST_KEYS
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != PERFORMANCE_MANIFEST_SCHEMA
        or manifest.get("kind") != "soak"
        or manifest.get("complete") is not True
    ):
        raise FieldEvidenceError("performance manifest root schemaが不正です")
    started = _aware_time(manifest.get("started_at", ""))
    finished = _aware_time(manifest.get("finished_at", ""))
    if started is None or finished is None or finished < started:
        raise FieldEvidenceError("performance manifest時刻が不正です")

    results = report.get("results")
    arguments = manifest.get("arguments")
    configuration = manifest.get("configuration")
    effective = configuration.get("effective") if isinstance(configuration, dict) else None
    artifacts = manifest.get("artifacts")
    if (
        not isinstance(results, list)
        or len(results) != 1
        or not isinstance(results[0], dict)
        or not isinstance(arguments, dict)
        or set(arguments) != PERFORMANCE_ARGUMENT_KEYS
        or not isinstance(configuration, dict)
        or set(configuration) != {"base", "effective"}
        or not isinstance(effective, dict)
        or not isinstance(artifacts, dict)
    ):
        raise FieldEvidenceError("performance report/manifest構造が不正です")

    current_sources = _performance_source_hashes(root)
    source_hashes = _validated_repo_hash_index(
        manifest.get("source_hashes"), "performance source snapshot"
    )
    if source_hashes != current_sources:
        raise FieldEvidenceError(
            "performance source snapshotが受領checkoutの完全なsource集合と一致しません"
        )
    base = configuration.get("base")
    if base != {"path": "config.yaml", "sha256": current_sources.get("config.yaml")}:
        raise FieldEvidenceError("performance base config bindingが不正です")
    effective = _verify_run_hash_index(run_dir, effective, "effective config")
    artifacts = _verify_run_hash_index(run_dir, artifacts, "performance artifact")
    input_hashes = _validated_opaque_hash_index(
        manifest.get("input_hashes"), "performance input snapshot"
    )
    runtime_asset_hashes = _validated_opaque_hash_index(
        manifest.get("runtime_asset_hashes"), "performance runtime asset snapshot"
    )
    runtime = manifest.get("runtime")
    if not _runtime_snapshot_is_well_formed(runtime):
        raise FieldEvidenceError("performance runtime snapshot構造が不正です")

    result = results[0]
    audio_sha = details.get("F_ACC_AUDIO_SHA", "").lower()
    checks, metrics = field_performance._validate_result(
        result,
        arguments=arguments,
        effective_configs=set(effective),
        cache_mode=mode,
        audio_sha256=audio_sha,
    )
    system_checks, system, hardware = field_performance._validated_system(
        report.get("system"),
        hardware=payload.get("hardware"),
        started_at=manifest.get("started_at"),
    )
    checks.update(system_checks)
    host_hash = payload.get("host_fingerprint_sha256")
    primary_relative = report_path.relative_to(run_dir).as_posix()
    run_relative = run_dir.relative_to(root).as_posix()
    out_dir = arguments.get("out_dir")
    try:
        resolved_out = (
            Path(out_dir).resolve()
            if isinstance(out_dir, str) and Path(out_dir).is_absolute()
            else (root / str(out_dir)).resolve()
        )
    except (OSError, ValueError):
        resolved_out = Path()
    checks.update(
        {
            "hardware_collected_immediately_after_run": field_performance._hardware_collection_timing_ok(
                finished_at=manifest.get("finished_at"),
                collected_at=payload.get("hardware_collected_at"),
            ),
            "manifest_complete_and_current": True,
            "arguments_at_least_45_minutes": field_performance._number(arguments.get("minutes")) is not None
            and float(arguments["minutes"]) >= 45,
            "arguments_ten_students": arguments.get("students") == 10,
            "arguments_out_dir_matches_run": isinstance(out_dir, str) and resolved_out == run_dir.resolve(),
            "run_dir_binds_validation_host": isinstance(host_hash, str)
            and bool(SHA256_RE.fullmatch(host_hash))
            and run_dir.name.endswith(f"-{host_hash[:16]}-school-cache-{mode}"),
            "real_audio_input": isinstance(arguments.get("audio"), str) and bool(arguments["audio"].strip()),
            "real_asr": arguments.get("asr") in field_performance.REAL_ASR_ENGINES,
            "real_mt": arguments.get("engine") in field_performance.REAL_MT_ENGINES,
            "no_config_update": arguments.get("no_config_update") is True,
            "report_engines_match_arguments": result.get("engine") == arguments.get("engine")
            and result.get("asr_engine") == arguments.get("asr"),
            "audio_hash_matches": bool(SHA256_RE.fullmatch(audio_sha))
            and len(input_hashes) == 1
            and next(iter(input_hashes.values())) == audio_sha,
            "primary_report_hash_matches": artifacts.get(primary_relative)
            == report_sha256,
        }
    )
    expected_check_keys = set(PERFORMANCE_CHECK_KEYS)
    expected_check_keys.add("rss_within_5000_mb" if mode == "on" else "cpu_observed")
    if mode == "on":
        expected_check_keys.add("memory_not_increasing")
    if set(checks) != expected_check_keys or set(metrics) != PERFORMANCE_METRIC_KEYS:
        raise FieldEvidenceError("performance validator check/metric schemaが不正です")

    if not _continuous_lock_is_well_formed(
        manifest.get("continuous_lock"),
        started_at=manifest.get("started_at"),
        finished_at=manifest.get("finished_at"),
        run_relative=run_relative,
        source_hashes=source_hashes,
        input_hashes=input_hashes,
        runtime_asset_hashes=runtime_asset_hashes,
        effective=effective,
        artifacts=artifacts,
    ):
        raise FieldEvidenceError("performance continuous lock記録が不正です")
    if manifest.get("primary_report") != primary_relative:
        raise FieldEvidenceError("performance primary report参照が一致しません")

    source_snapshot = source_hashes
    runtime_snapshot = {
        "runtime": runtime,
        "runtime_asset_hashes": runtime_asset_hashes,
    }
    system_snapshot = {key: system.get(key) for key in ("cpu", "cores", "threads", "ram_gb", "os", "python")}
    expected_binding = {
        "minutes": arguments.get("minutes"),
        "students": arguments.get("students"),
        "audio_sha256": audio_sha,
        "corpus": arguments.get("corpus"),
        "asr": arguments.get("asr"),
        "engine": arguments.get("engine"),
        "langs": result.get("langs"),
        "disconnect_every_s": arguments.get("disconnect_every_s"),
        "partial": arguments.get("partial"),
        "drain_seconds": arguments.get("drain_seconds"),
        "no_config_update": arguments.get("no_config_update"),
        "effective_config": result.get("effective_config"),
    }
    expected_paths = {
        "run_dir": run_relative,
        "manifest": manifest_relative,
        "primary_report": report_relative,
    }
    validator_path = _resolve_repo_path(root, "scripts/validate_field_performance.py")
    if not validator_path.is_file():
        raise FieldEvidenceError("performance validator sourceがありません")
    expected_hashes = {
        "manifest_sha256": manifest_sha256,
        "primary_report_sha256": report_sha256,
        "validator_sha256": _sha256(validator_path),
        "source_snapshot_sha256": _canonical_sha256(source_snapshot),
        "runtime_snapshot_sha256": _canonical_sha256(runtime_snapshot),
        "system_snapshot_sha256": _canonical_sha256(system_snapshot),
        "hardware_snapshot_sha256": _canonical_sha256(hardware),
    }
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != field_performance.FIELD_VALIDATION_SCHEMA
        or payload.get("kind") != "field-performance-validation"
        or payload.get("passed") is not True
        or payload.get("cache_mode") != mode
        or payload.get("system") != system
        or payload.get("hardware") != hardware
        or not isinstance(payload.get("binding"), dict)
        or set(payload["binding"]) != PERFORMANCE_BINDING_KEYS
        or payload.get("binding") != expected_binding
        or payload.get("checks") != checks
        or payload.get("metrics") != metrics
        or not all(value is True for value in checks.values())
        or any(payload.get(key) != value for key, value in expected_paths.items())
        or any(payload.get(key) != value for key, value in expected_hashes.items())
        or payload.get("started_at") != manifest.get("started_at")
        or payload.get("finished_at") != manifest.get("finished_at")
        or details.get("F_ACC_ASR") != arguments.get("asr")
        or details.get("F_ACC_ENGINE") != arguments.get("engine")
        or not _same_decimal(details.get("F_ACC_AUDIO_MIN", ""), arguments.get("minutes"))
        or (
            mode == "on"
            and (
                not _same_decimal(details.get("F_ACC_RSS", ""), metrics.get("peak_rss_mb"))
                or not _same_decimal(
                    details.get("F_ACC_CRASHES", ""), metrics.get("crashes")
                )
                or not isinstance(metrics.get("errors"), list)
                or details.get("F_ACC_ERRORS") != str(len(metrics["errors"]))
            )
        )
        or (
            mode == "off"
            and not _same_decimal(
                details.get("F_ACC_CPU", ""), metrics.get("cpu_median_percent")
            )
        )
    ):
        raise FieldEvidenceError(f"cache-{mode} performance validationの再監査に失敗しました")
    if (
        _sha256(validation_path) != validation_sha256
        or _sha256(manifest_path) != manifest_sha256
        or _sha256(report_path) != report_sha256
        or _verify_run_hash_index(run_dir, effective, "effective config") != effective
        or _verify_run_hash_index(run_dir, artifacts, "performance artifact") != artifacts
    ):
        raise FieldEvidenceError("performance evidenceが検証中に変化しました")
    return payload


def _validate_performance_pair(cache_on: Mapping[str, Any], cache_off: Mapping[str, Any]) -> None:
    on_binding = cache_on.get("binding")
    off_binding = cache_off.get("binding")
    same_binding = (
        isinstance(on_binding, dict)
        and isinstance(off_binding, dict)
        and {key: value for key, value in on_binding.items() if key != "effective_config"}
        == {key: value for key, value in off_binding.items() if key != "effective_config"}
        and on_binding.get("effective_config") != off_binding.get("effective_config")
    )
    distinct_runs = all(
        isinstance(payload.get(key), str) and bool(payload[key])
        for payload in (cache_on, cache_off)
        for key in ("run_dir", "manifest", "primary_report")
    ) and all(cache_on[key] != cache_off[key] for key in ("run_dir", "manifest", "primary_report"))
    same_snapshots = all(
        isinstance(cache_on.get(key), str)
        and bool(SHA256_RE.fullmatch(str(cache_on[key])))
        and cache_on.get(key) == cache_off.get(key)
        for key in (
            "source_snapshot_sha256",
            "runtime_snapshot_sha256",
            "system_snapshot_sha256",
            "hardware_snapshot_sha256",
            "host_fingerprint_sha256",
        )
    )
    same_system = isinstance(cache_on.get("system"), dict) and isinstance(cache_off.get("system"), dict) and all(
        cache_on["system"].get(key) == cache_off["system"].get(key)
        for key in ("cpu", "cores", "threads", "ram_gb", "os", "python")
    )
    same_hardware = isinstance(cache_on.get("hardware"), dict) and cache_on.get("hardware") == cache_off.get("hardware")
    try:
        on_started = dt.datetime.fromisoformat(cache_on["started_at"])
        on_finished = dt.datetime.fromisoformat(cache_on["finished_at"])
        on_collected = dt.datetime.fromisoformat(cache_on["hardware_collected_at"])
        off_started = dt.datetime.fromisoformat(cache_off["started_at"])
        off_finished = dt.datetime.fromisoformat(cache_off["finished_at"])
        off_collected = dt.datetime.fromisoformat(cache_off["hardware_collected_at"])
        sequential = (
            on_started < on_finished <= on_collected <= off_started < off_finished <= off_collected
            and (on_finished - on_started).total_seconds() >= 45 * 60
            and (off_finished - off_started).total_seconds() >= 45 * 60
            and (off_started - on_finished).total_seconds() <= 4 * 60 * 60
        )
    except (KeyError, OverflowError, TypeError, ValueError):
        sequential = False
    if not all((same_binding, distinct_runs, same_snapshots, same_system, same_hardware, sequential)):
        raise FieldEvidenceError("cache-on/off performance pairの再監査に失敗しました")


def _validate_field_timeline(
    *,
    source_snapshots: Mapping[str, Any],
    cache_on: Mapping[str, Any],
    cache_off: Mapping[str, Any],
    ops12_details: Mapping[str, str],
    captured_at: str,
) -> None:
    snapshot_start = _aware_time(str(source_snapshots.get("start_captured_at", "")))
    snapshot_end = _aware_time(str(source_snapshots.get("end_captured_at", "")))
    cache_on_started = _aware_time(str(cache_on.get("started_at", "")))
    cache_off_finished = _aware_time(str(cache_off.get("finished_at", "")))
    ops12_started = _aware_time(ops12_details.get("F_C2_START", ""))
    ops12_finished = _aware_time(ops12_details.get("F_C2_END", ""))
    sidecar_captured = _aware_time(captured_at)
    host = source_snapshots.get("host_fingerprint_sha256")
    if (
        any(
            value is None
            for value in (
                snapshot_start,
                snapshot_end,
                cache_on_started,
                cache_off_finished,
                ops12_started,
                ops12_finished,
                sidecar_captured,
            )
        )
        or not isinstance(host, str)
        or not SHA256_RE.fullmatch(host)
        or cache_on.get("host_fingerprint_sha256") != host
        or cache_off.get("host_fingerprint_sha256") != host
    ):
        raise FieldEvidenceError("field source snapshotの時刻または学校PC bindingが不正です")
    assert snapshot_start is not None
    assert snapshot_end is not None
    assert cache_on_started is not None
    assert cache_off_finished is not None
    assert ops12_started is not None
    assert ops12_finished is not None
    assert sidecar_captured is not None
    if not (
        snapshot_start < cache_on_started
        and snapshot_end >= cache_off_finished
        and snapshot_start <= ops12_started
        and snapshot_end >= ops12_finished
        and snapshot_start < snapshot_end <= sidecar_captured
    ):
        raise FieldEvidenceError(
            "field source snapshotが実地試験の開始前と全試験終了後を囲んでいません"
        )


def _validate_quality_story(
    details: Mapping[str, str], root: Path, evidence_dir: Path,
    manifest_files: Mapping[str, str],
    source_snapshots: Mapping[str, Any], captured_at: str,
) -> None:
    field_date = evidence_dir.name
    tsv_relative = f"docs/field/evidence/{field_date}/{field_date}-quality-30.tsv"
    provenance_relative = (
        f"docs/field/evidence/{field_date}/{field_date}-quality-provenance.json"
    )
    if (
        details.get("F_C6_FILE") != tsv_relative
        or details.get("F_C6_PROVENANCE") != provenance_relative
    ):
        raise FieldEvidenceError("QUAL-01 TSV/provenance参照が日付固定pathと一致しません")
    tsv = _validate_evidence_reference(
        root, evidence_dir, tsv_relative, manifest_files
    )
    provenance = _validate_evidence_reference(
        root, evidence_dir, provenance_relative, manifest_files
    )
    if not tsv.is_file() or not provenance.is_file() or tsv == provenance:
        raise FieldEvidenceError("QUAL-01 TSV/provenanceは別々の通常fileが必要です")
    validation = quality_evidence.validate_quality_package(
        tsv,
        provenance,
        root=root,
        field_date=field_date,
        expected_session_id=str(source_snapshots.get("session_id")),
        expected_host_fingerprint_sha256=str(
            source_snapshots.get("host_fingerprint_sha256")
        ),
        source_snapshot_start_at=str(source_snapshots.get("start_captured_at")),
        source_snapshot_end_at=str(source_snapshots.get("end_captured_at")),
        field_bundle_captured_at=captured_at,
        manifest_files=manifest_files,
    )
    if not isinstance(validation, dict):
        raise FieldEvidenceError("QUAL-01 package validatorの応答が不正です")
    if validation.get("passed") is not True:
        error = validation.get("error")
        raise FieldEvidenceError(
            "QUAL-01 package validatorが不合格です"
            + (f": {error}" if isinstance(error, str) else "")
        )
    metrics = validation.get("metrics")
    scope = validation.get("scope")
    if not isinstance(metrics, dict) or not isinstance(scope, dict):
        raise FieldEvidenceError("QUAL-01 package validatorのmetrics/scopeが不正です")
    english_average = _decimal(str(metrics.get("english_average")))
    chinese_average = _decimal(str(metrics.get("chinese_average")))
    if english_average is None or chinese_average is None:
        raise FieldEvidenceError("QUAL-01 package validatorの平均値が不正です")
    expected = {
        "F_C6_MEASURED": "可",
        "F_C6_PROVENANCE_PASSED": "true",
        "F_C6_PROVENANCE_SHA": str(validation.get("provenance_sha256")),
        "F_C6_DATA_PASSED": "true",
        "F_C6_ROWS": str(metrics.get("rows")),
        "F_C6_REQUIRED": str(metrics.get("required_rows")),
        "F_C6_EN_COUNT": str(metrics.get("english_rating_count")),
        "F_C6_EN_SCORE": f"{english_average:.2f}",
        "F_C6_ZH_COUNT": str(metrics.get("chinese_rating_count")),
        "F_C6_SCORE": f"{chinese_average:.2f}",
        "F_C6_NUMBERS": str(metrics.get("ordered_number_count")),
        "F_C6_DISTINCT": str(metrics.get("distinct_reference_count")),
        "F_C6_DOMAIN_COVERAGE": str(metrics.get("domain_coverage_count")),
        "F_C6_ACOUSTIC_COVERAGE": str(metrics.get("acoustic_coverage_count")),
        "F_C6_SEGMENTS": str(metrics.get("mapped_segment_count")),
        "F_C6_TRANSCRIPT_BOUND": str(
            metrics.get("transcript_bound_row_count")
        ),
        "F_C6_EVALUATOR_ASSIGNMENTS": str(
            metrics.get("evaluator_assignment_count")
        ),
        "F_C6_ASR_ERRORS": str(metrics.get("asr_mismatch_row_count")),
        "F_C6_SHA": _sha256(tsv),
    }
    if (
        scope.get("manifest_references_checked") is not True
        or not isinstance(validation.get("provenance_sha256"), str)
        or not SHA256_RE.fullmatch(validation["provenance_sha256"])
        or _sha256(provenance) != validation["provenance_sha256"]
        or any(details.get(key) != value for key, value in expected.items())
    ):
        raise FieldEvidenceError(
            "QUAL-01 packageの検証値・集計値がsidecarと一致しません"
        )


def _validate_perf01_story(
    details: Mapping[str, str], root: Path, evidence_dir: Path,
    manifest_files: Mapping[str, str],
    source_snapshots: Mapping[str, Any], captured_at: str,
) -> None:
    field_date = evidence_dir.name
    run_relative = (
        f"docs/field/evidence/{field_date}/{field_date}-perf01-run.json"
    )
    validation_relative = (
        f"docs/field/evidence/{field_date}/{field_date}-perf01-validation.json"
    )
    if (
        details.get("F_C4_RUN") != run_relative
        or details.get("F_C4_VALIDATION") != validation_relative
    ):
        raise FieldEvidenceError("PERF-01 run/validation参照が日付固定pathと一致しません")

    run_path = _validate_evidence_reference(
        root, evidence_dir, run_relative, manifest_files
    )
    validation_path = _validate_evidence_reference(
        root, evidence_dir, validation_relative, manifest_files
    )
    if not run_path.is_file() or not validation_path.is_file():
        raise FieldEvidenceError("PERF-01 run/validationは通常fileである必要があります")
    run, run_sha256 = _load_strict_json_snapshot(run_path)
    saved_validation, validation_sha256 = _load_strict_json_snapshot(validation_path)
    if not isinstance(run, dict) or not isinstance(saved_validation, dict):
        raise FieldEvidenceError("PERF-01 run/validation JSON rootが不正です")
    try:
        validation = perf01_evidence.validate_perf01_run(run_path, root=root)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise FieldEvidenceError(
            "PERF-01 package validatorの受領側再実行に失敗しました"
        ) from exc
    if saved_validation != validation or validation.get("passed") is not True:
        raise FieldEvidenceError("PERF-01保存validationが受領側再検証結果と一致しません")
    if _sha256(run_path) != run_sha256 or _sha256(validation_path) != validation_sha256:
        raise FieldEvidenceError("PERF-01 run/validationが検証中に変化しました")

    artifact_hashes = validation.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict) or not artifact_hashes:
        raise FieldEvidenceError("PERF-01 artifact hash indexが不正です")
    for relative, digest in artifact_hashes.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise FieldEvidenceError("PERF-01 artifact hash indexが不正です")
        artifact = _validate_evidence_reference(
            root, evidence_dir, relative, manifest_files
        )
        if not artifact.is_file() or _sha256(artifact) != digest:
            raise FieldEvidenceError(
                f"PERF-01 artifactが外側manifestと一致しません: {relative}"
            )

    binding = validation.get("binding")
    metrics = validation.get("metrics")
    population = run.get("population")
    input_value = run.get("input")
    artifacts = run.get("artifacts")
    measurement_window = run.get("measurement_window")
    if not all(
        isinstance(value, dict)
        for value in (
            binding,
            metrics,
            population,
            input_value,
            artifacts,
            measurement_window,
        )
    ):
        raise FieldEvidenceError("PERF-01 run/validation bindingが不正です")
    assert isinstance(binding, dict)
    assert isinstance(metrics, dict)
    assert isinstance(population, dict)
    assert isinstance(input_value, dict)
    assert isinstance(artifacts, dict)
    assert isinstance(measurement_window, dict)
    expected_source_binding = {
        "field_session_id": source_snapshots.get("session_id"),
        "host_fingerprint_sha256": source_snapshots.get(
            "host_fingerprint_sha256"
        ),
        "source_snapshot_sha256": source_snapshots.get(
            "source_snapshot_sha256"
        ),
    }
    if (
        binding.get("field_date") != field_date
        or any(run.get(key) != value for key, value in expected_source_binding.items())
        or any(
            binding.get(key) != value
            for key, value in expected_source_binding.items()
        )
    ):
        raise FieldEvidenceError(
            "PERF-01 run/validation source session, host, or snapshot binding does not match"
        )
    timeline = [
        _aware_time(str(source_snapshots.get("start_captured_at", ""))),
        _aware_time(str(measurement_window.get("started_at", ""))),
        _aware_time(str(measurement_window.get("audio_ended_at", ""))),
        _aware_time(str(measurement_window.get("observation_ended_at", ""))),
        _aware_time(str(source_snapshots.get("end_captured_at", ""))),
        _aware_time(captured_at),
    ]
    if any(value is None for value in timeline):
        raise FieldEvidenceError("PERF-01 field timeline must use timezone-aware timestamps")
    snapshot_start, measurement_start, audio_end, observation_end, snapshot_end, bundle_time = timeline
    assert snapshot_start is not None
    assert measurement_start is not None
    assert audio_end is not None
    assert observation_end is not None
    assert snapshot_end is not None
    assert bundle_time is not None
    if not (
        snapshot_start
        < measurement_start
        < audio_end
        <= observation_end
        <= snapshot_end
        <= bundle_time
    ):
        raise FieldEvidenceError(
            "PERF-01 measurement is outside the source snapshot/bundle timeline"
        )
    representatives = binding.get("representatives")
    language_counts = binding.get("language_counts")
    if not isinstance(representatives, dict) or not isinstance(language_counts, dict):
        raise FieldEvidenceError("PERF-01言語・代表端末bindingが不正です")
    measurement_seconds = _decimal(str(binding.get("measurement_seconds")))
    recorded_minutes = _shell_decimal(details.get("F_C4_MINUTES", ""))
    expected = {
        "F_C4_MEASURED": "可",
        "F_C4_VALIDATED": "可",
        "F_C4_POPULATION": "可",
        "F_C4_COMPLETE": "可",
        "F_C4_COUNT": str(binding.get("student_count")),
        "F_C4_EN_COUNT": str(language_counts.get("en")),
        "F_C4_ZH_COUNT": str(language_counts.get("zh")),
        "F_C4_REPS": (
            f"en={representatives.get('en')}; zh={representatives.get('zh')}"
        ),
        "F_C4_INPUT_KIND": str(input_value.get("kind")),
        "F_C4_INPUT": str(input_value.get("description")),
        "F_C4_INPUT_EVIDENCE": str(
            input_value.get("evidence", {}).get("path")
            if isinstance(input_value.get("evidence"), dict)
            else None
        ),
        "F_C4_FINALS": str(metrics.get("final_count")),
        "F_C4_EXPECTED": str(metrics.get("expected_rows")),
        "F_C4_MATCHED": str(metrics.get("matched_rows")),
        "F_C4_MED": str(metrics.get("latency_median_seconds")),
        "F_C4_MAX": str(metrics.get("latency_max_seconds")),
        "F_C4_COMMIT": str(
            artifacts.get("commit_status", {}).get("path")
            if isinstance(artifacts.get("commit_status"), dict)
            else None
        ),
        "F_C4_CONFIG": str(
            artifacts.get("config_models", {}).get("path")
            if isinstance(artifacts.get("config_models"), dict)
            else None
        ),
        "F_C4_RUNTIME": str(
            artifacts.get("runtime_browsers", {}).get("path")
            if isinstance(artifacts.get("runtime_browsers"), dict)
            else None
        ),
        "F_C4_EVIDENCE": str(
            artifacts.get("measurements_tsv", {}).get("path")
            if isinstance(artifacts.get("measurements_tsv"), dict)
            else None
        ),
    }
    if (
        measurement_seconds is None
        or recorded_minutes is None
        or abs(recorded_minutes * Decimal(60) - measurement_seconds)
        > Decimal("0.000000001")
        or any(details.get(key) != value for key, value in expected.items())
    ):
        raise FieldEvidenceError("PERF-01 run/validationとsidecar詳細が一致しません")


def _validate_story_entries(
    payload: Mapping[str, Any], root: Path, evidence_dir: Path,
    manifest_files: Mapping[str, str],
    source_snapshots: Mapping[str, Any], captured_at: str,
) -> tuple[dict[str, bool], dict[str, dict[str, Any]]]:
    stories = payload.get("stories")
    if not isinstance(stories, dict) or tuple(stories) != FIELD_STORIES:
        raise FieldEvidenceError("field storyは規定8件を固定順で過不足なく含める必要があります")
    candidates: dict[str, bool] = {}
    entries: dict[str, dict[str, Any]] = {}
    for story in FIELD_STORIES:
        entry = stories.get(story)
        if not isinstance(entry, dict) or set(entry) != {"candidate", "evidence", "details"}:
            raise FieldEvidenceError(f"{story} entryのschemaが不正です")
        details = entry.get("details")
        evidence = entry.get("evidence")
        if not isinstance(details, dict) or tuple(details) != STORY_DETAIL_KEYS[story] or not all(isinstance(value, str) for value in details.values()):
            raise FieldEvidenceError(f"{story} detailsが不正です")
        recomputed = recompute_story_candidate(story, details)
        expected_candidate = "可" if recomputed else "否"
        if entry.get("candidate") != expected_candidate or details[STORY_STATUS_KEYS[story]] != expected_candidate:
            raise FieldEvidenceError(f"{story} candidateを生値から再現できません")
        expected_evidence = [details[key] for key in STORY_EVIDENCE_KEYS[story]] if recomputed else []
        if evidence != expected_evidence or len(expected_evidence) != len(set(expected_evidence)):
            raise FieldEvidenceError(f"{story} evidence一覧が規定値と一致しません")
        for relative in expected_evidence:
            _validate_evidence_reference(root, evidence_dir, relative, manifest_files)
        if recomputed and story == "STU-11":
            for prefix in ("F_STU11_CB", "F_STU11_IOS"):
                path = _resolve_repo_path(root, details[f"{prefix}_EVIDENCE"])
                if not path.is_file() or _sha256(path) != details[f"{prefix}_SHA"].lower():
                    raise FieldEvidenceError(f"{story}の画面証拠SHA-256が一致しません")
        if recomputed and story == "PERF-01":
            _validate_perf01_story(
                details,
                root,
                evidence_dir,
                manifest_files,
                source_snapshots,
                captured_at,
            )
        if recomputed and story == "QUAL-01":
            _validate_quality_story(
                details,
                root,
                evidence_dir,
                manifest_files,
                source_snapshots,
                captured_at,
            )
        if story == "PERF-02":
            expected_cache_off = "可" if _recompute_cache_off_candidate(details) else "否"
            if details.get("F_ACC_NOCACHE_OK") != expected_cache_off:
                raise FieldEvidenceError(
                    "PERF-02 cache-off補助候補を生値から再現できません"
                )
        candidates[story] = recomputed
        entries[story] = entry
    return candidates, entries


def _validate_field_bundle(run_dir: Path, *, root: Path = ROOT) -> dict[str, Any]:
    _recheck_imported_field_module()
    root = Path(os.path.abspath(root))
    field_date, evidence_dir = _field_date_from_run_dir(run_dir, root)
    bundle_path = evidence_dir / BUNDLE_NAME
    _assert_no_link_components(root, bundle_path)
    if not bundle_path.is_file() or bundle_path.stat().st_size <= 0:
        raise FieldEvidenceError(f"{BUNDLE_NAME}がありません")
    bundle_sha_before = _sha256(bundle_path)
    payload = load_strict_json(bundle_path)
    expected_keys = {
        "schema_version",
        "kind",
        "field_date",
        "captured_at",
        "tester",
        "evidence_dir",
        "manifest",
        "manual_review_required",
        "source_hashes",
        "source_snapshot_start",
        "source_snapshot_end",
        "source_snapshot_sha256",
        "source_snapshot_host_fingerprint_sha256",
        "stories",
        "stories_complete",
        "field_complete",
        "rehearsal_complete",
        "overall",
        "overall_condition",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise FieldEvidenceError("field sidecar root schemaが不正です")
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != KIND:
        raise FieldEvidenceError("field sidecar schema/kindが不正です")
    expected_dir = f"docs/field/evidence/{field_date}"
    expected_manifest = f"docs/field/{field_date}-evidence-sha256.txt"
    if payload.get("field_date") != field_date or payload.get("evidence_dir") != expected_dir or payload.get("manifest") != expected_manifest:
        raise FieldEvidenceError("field sidecarの日付/path bindingが不正です")
    tester = payload.get("tester")
    if (
        payload.get("manual_review_required") is not True
        or not isinstance(tester, str)
        or not tester
        or tester != tester.strip()
        or len(tester) > 100
        or not tester.isprintable()
        or ";" in tester
    ):
        raise FieldEvidenceError("field sidecarには実施者とmanual review要件が必要です")
    captured_at = _aware_time(payload.get("captured_at", ""))
    if captured_at is None:
        raise FieldEvidenceError("captured_atはtimezone付きISO 8601が必要です")

    initial_sources = hash_field_sources(root)
    if not initial_sources or payload.get("source_hashes") != initial_sources:
        raise FieldEvidenceError("field source snapshotが現行checkoutと一致しません")
    outer = validate_outer_manifest(root, field_date)
    source_snapshot_start = payload.get("source_snapshot_start")
    source_snapshot_end = payload.get("source_snapshot_end")
    if not isinstance(source_snapshot_start, str) or not isinstance(
        source_snapshot_end, str
    ):
        raise FieldEvidenceError("field sidecarのsource snapshot参照が不正です")
    source_snapshots = _validate_source_snapshot_pair(
        root=root,
        field_date=field_date,
        evidence_dir=evidence_dir,
        manifest_files=outer["files"],
        start_relative=source_snapshot_start,
        end_relative=source_snapshot_end,
        current_sources=initial_sources,
    )
    if (
        payload.get("source_snapshot_sha256")
        != source_snapshots["source_snapshot_sha256"]
        or payload.get("source_snapshot_host_fingerprint_sha256")
        != source_snapshots["host_fingerprint_sha256"]
    ):
        raise FieldEvidenceError("field sidecarのsource snapshot bindingが一致しません")
    candidates, entries = _validate_story_entries(
        payload,
        root,
        evidence_dir,
        outer["files"],
        source_snapshots,
        payload["captured_at"],
    )
    stories_complete = all(candidates.values())
    if payload.get("stories_complete") is not stories_complete:
        raise FieldEvidenceError("stories_completeを8件の再計算結果から再現できません")

    perf_details = entries["PERF-02"]["details"]
    performance_pair_ok = False
    if candidates["PERF-02"]:
        cache_on = _validate_saved_performance(
            perf_details, "on", root, evidence_dir, outer["files"]
        )
        if perf_details.get("F_ACC_NOCACHE_OK") == "可" and perf_details.get("F_ACC_NOCACHE_VALIDATED") == "可":
            cache_off = _validate_saved_performance(
                perf_details, "off", root, evidence_dir, outer["files"]
            )
            _validate_performance_pair(cache_on, cache_off)
            _validate_field_timeline(
                source_snapshots=source_snapshots,
                cache_on=cache_on,
                cache_off=cache_off,
                ops12_details=entries["OPS-12"]["details"],
                captured_at=payload["captured_at"],
            )
            performance_pair_ok = True
    field_complete = stories_complete and performance_pair_ok
    if payload.get("field_complete") is not field_complete:
        raise FieldEvidenceError("field_completeを再計算できません")
    if type(payload.get("rehearsal_complete")) is not bool:
        raise FieldEvidenceError("rehearsal_completeはbooleanが必要です")
    if payload.get("overall") not in {"可", "条件付き可", "否"} or not isinstance(payload.get("overall_condition"), str):
        raise FieldEvidenceError("総合判定が不正です")
    if payload.get("overall") == "可" and (
        not field_complete or payload.get("rehearsal_complete") is not True
    ):
        raise FieldEvidenceError("総合判定=可にはfield証拠と実地リハーサル完了が必要です")

    final_sources = hash_field_sources(root)
    final_outer = validate_outer_manifest(root, field_date)
    if not _source_snapshots_equal(final_sources, initial_sources) or final_outer["manifest_sha256"] != outer["manifest_sha256"] or _sha256(bundle_path) != bundle_sha_before:
        raise FieldEvidenceError("field証拠を監査中にsource/bundle/manifestが変化しました")
    return {
        "payload": payload,
        "bundle_path": bundle_path,
        "bundle_sha256": bundle_sha_before,
        "manifest_path": outer["manifest_path"],
        "manifest_sha256": outer["manifest_sha256"],
        "approved_candidates": (
            [story for story, passed in candidates.items() if passed]
            if performance_pair_ok
            else []
        ),
        "field_complete": field_complete,
    }


def validate_field_bundle(run_dir: Path, *, root: Path = ROOT) -> dict[str, Any]:
    result, witness = _run_with_validation_read_set(
        lambda: _validate_field_bundle(run_dir, root=root)
    )
    if witness is None:
        return result
    return ValidatedFieldBundle(result, witness)


def _payload_from_environment(environ: Mapping[str, str], root: Path) -> dict[str, Any]:
    _recheck_imported_field_module()
    field_date = environ.get("F_DATE", "")
    try:
        field_date = _validated_field_date(field_date)
    except ValueError as exc:
        raise FieldEvidenceError("F_DATEが不正です") from exc
    tester = environ.get("F_TESTER", "").strip()
    if (
        not tester
        or len(tester) > 100
        or not tester.isprintable()
        or ";" in tester
    ):
        raise FieldEvidenceError("F_TESTERが必要です")
    expected_dir = f"docs/field/evidence/{field_date}"
    expected_manifest = f"docs/field/{field_date}-evidence-sha256.txt"
    expected_snapshot_start = _source_snapshot_relative(field_date, "start")
    expected_snapshot_end = _source_snapshot_relative(field_date, "end")
    if (
        environ.get("F_EVIDENCE_TRANSFER") != "可"
        or environ.get("F_EVIDENCE_DIR") != expected_dir
        or environ.get("F_EVIDENCE_MANIFEST") != expected_manifest
        or environ.get("F_SOURCE_SNAPSHOT_START") != expected_snapshot_start
        or environ.get("F_SOURCE_SNAPSHOT_END") != expected_snapshot_end
    ):
        raise FieldEvidenceError("検証済みの日付固定evidence dir/manifestが必要です")
    evidence_dir = _resolve_repo_path(root, expected_dir)
    outer = validate_outer_manifest(root, field_date)
    current_sources = hash_field_sources(root)
    source_snapshots = _validate_source_snapshot_pair(
        root=root,
        field_date=field_date,
        evidence_dir=evidence_dir,
        manifest_files=outer["files"],
        start_relative=expected_snapshot_start,
        end_relative=expected_snapshot_end,
        current_sources=current_sources,
    )
    captured_at = dt.datetime.now().astimezone().isoformat()
    stories: dict[str, Any] = {}
    candidates: dict[str, bool] = {}
    for story in FIELD_STORIES:
        details = {key: environ.get(key, "") for key in STORY_DETAIL_KEYS[story]}
        passed = recompute_story_candidate(story, details)
        expected_candidate = "可" if passed else "否"
        if details[STORY_STATUS_KEYS[story]] != expected_candidate:
            raise FieldEvidenceError(f"{story}のshell gateとsidecar再計算が一致しません")
        if story == "PERF-02":
            expected_cache_off = "可" if _recompute_cache_off_candidate(details) else "否"
            if details.get("F_ACC_NOCACHE_OK") != expected_cache_off:
                raise FieldEvidenceError(
                    "PERF-02 cache-off補助gateとsidecar再計算が一致しません"
                )
        if passed and story == "PERF-01":
            _validate_perf01_story(
                details,
                root,
                evidence_dir,
                outer["files"],
                source_snapshots,
                captured_at,
            )
        if passed and story == "QUAL-01":
            _validate_quality_story(
                details,
                root,
                evidence_dir,
                outer["files"],
                source_snapshots,
                captured_at,
            )
        stories[story] = {
            "candidate": expected_candidate,
            "evidence": [details[key] for key in STORY_EVIDENCE_KEYS[story]] if passed else [],
            "details": details,
        }
        candidates[story] = passed
    stories_complete = all(candidates.values())
    field_complete = stories_complete and environ.get("F_ACC_NOCACHE_OK") == "可" and environ.get("F_ACC_NOCACHE_VALIDATED") == "可"
    if environ.get("F_STORIES_COMPLETE") != ("可" if stories_complete else "否") or environ.get("F_FIELD_COMPLETE") != ("可" if field_complete else "否"):
        raise FieldEvidenceError("shellの総合集計とsidecar再計算が一致しません")
    if field_complete:
        performance_details = stories["PERF-02"]["details"]
        cache_on = _validate_saved_performance(
            performance_details, "on", root, evidence_dir, outer["files"]
        )
        cache_off = _validate_saved_performance(
            performance_details, "off", root, evidence_dir, outer["files"]
        )
        _validate_performance_pair(cache_on, cache_off)
        _validate_field_timeline(
            source_snapshots=source_snapshots,
            cache_on=cache_on,
            cache_off=cache_off,
            ops12_details=stories["OPS-12"]["details"],
            captured_at=captured_at,
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "field_date": field_date,
        "captured_at": captured_at,
        "tester": tester,
        "evidence_dir": expected_dir,
        "manifest": expected_manifest,
        "manual_review_required": True,
        "source_hashes": current_sources,
        "source_snapshot_start": expected_snapshot_start,
        "source_snapshot_end": expected_snapshot_end,
        "source_snapshot_sha256": source_snapshots["source_snapshot_sha256"],
        "source_snapshot_host_fingerprint_sha256": source_snapshots[
            "host_fingerprint_sha256"
        ],
        "stories": stories,
        "stories_complete": stories_complete,
        "field_complete": field_complete,
        "rehearsal_complete": environ.get("F_REHEARSAL_COMPLETE") == "可",
        "overall": environ.get("F_OVERALL", "否"),
        "overall_condition": environ.get("F_OVERALL_COND", ""),
    }


def capture_from_environment(
    *, root: Path = ROOT, environ: Mapping[str, str] = os.environ
) -> dict[str, Any]:
    _recheck_imported_field_module()
    root = Path(os.path.abspath(root))
    payload = _payload_from_environment(environ, root)
    field_date = payload["field_date"]
    # Require a complete pre-capture manifest before adding the derived sidecar.
    validate_outer_manifest(root, field_date)
    evidence_dir = root / "docs" / "field" / "evidence" / field_date
    bundle_path = evidence_dir / BUNDLE_NAME
    data = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=evidence_dir,
        prefix=f".{BUNDLE_NAME}.", suffix=".tmp",
    ) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    os.replace(temporary, bundle_path)
    rewrite_outer_manifest(root, field_date)
    return validate_field_bundle(evidence_dir, root=root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "validate", "snapshot-sources"])
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--field-date")
    parser.add_argument("--phase", choices=tuple(SOURCE_SNAPSHOT_FILENAMES))
    args = parser.parse_args(argv)
    try:
        if args.action == "snapshot-sources":
            if args.field_date is None or args.phase is None:
                parser.error("snapshot-sources requires --field-date and --phase")
            snapshot = write_field_source_snapshot(
                field_date=args.field_date, phase=args.phase
            )
            print(
                json.dumps(
                    {
                        "snapshot": snapshot["snapshot_path"]
                        .relative_to(ROOT)
                        .as_posix(),
                        "snapshot_sha256": snapshot["snapshot_sha256"],
                        "source_snapshot_sha256": snapshot[
                            "source_snapshot_sha256"
                        ],
                        "phase": snapshot["phase"],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.action == "capture":
            result = capture_from_environment()
        else:
            if args.run_dir is None:
                parser.error("validate requires --run-dir")
            result = validate_field_bundle(args.run_dir)
    except (FieldEvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FIELD_EVIDENCE_INVALID: {exc}")
        return 1
    print(
        json.dumps(
            {
                "bundle": result["bundle_path"].relative_to(ROOT).as_posix(),
                "bundle_sha256": result["bundle_sha256"],
                "manifest": result["manifest_path"].relative_to(ROOT).as_posix(),
                "manifest_sha256": result["manifest_sha256"],
                "approved_candidates": result["approved_candidates"],
                "field_complete": result["field_complete"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
