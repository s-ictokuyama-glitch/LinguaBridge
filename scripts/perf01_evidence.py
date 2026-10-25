#!/usr/bin/env python3
"""Validate complete, physical-browser evidence for the PERF-01 story.

The run description is deliberately small and immutable.  It points at the
complete server transcript and at one display-latency row for each
``(final sequence, representative language)`` pair.  Validation reads every
referenced file into a hash snapshot, parses the complete JSONL and TSV,
recomputes the 2N relationship and the unrounded latency statistics, then
re-hashes all bytes to detect changes during the audit.

Only the Python standard library is used so the validator can also run on the
receiving PC before the project's virtual environment is installed.
"""

from __future__ import annotations

import argparse
import ctypes
import csv
from dataclasses import dataclass
import datetime as dt
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import wave
from typing import Any, Callable, Mapping, Sequence


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
ROOT = _IMPORT_ROOT
_IMPORTED_VALIDATOR_PATHS = (
    _SELF_PATH,
    _IMPORT_ROOT / "scripts" / "qa_evidence_manifest.py",
)
_EARLY_IMPORTED_VALIDATOR_STATE = {
    path: _early_import_source_state(path, root=_IMPORT_ROOT)
    for path in _IMPORTED_VALIDATOR_PATHS
}
_IMPORT_PYCACHE = tempfile.TemporaryDirectory(
    prefix="linguabridge-perf01-import-pycache-"
)
_IMPORT_PYCACHE_WAS_EMPTY = not any(Path(_IMPORT_PYCACHE.name).iterdir())
if not _IMPORT_PYCACHE_WAS_EMPTY:  # pragma: no cover - freshly created directory
    raise RuntimeError("PERF-01 import bytecode cache was not empty")
sys.pycache_prefix = _IMPORT_PYCACHE.name
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))

from scripts import qa_evidence_manifest  # noqa: E402
from scripts.qa_evidence_manifest import (  # noqa: E402
    EvidenceManifestError,
    SourceHashSnapshot,
)

if Path(os.path.abspath(qa_evidence_manifest.__file__)) != _IMPORTED_VALIDATOR_PATHS[1]:
    raise RuntimeError("PERF-01 imported an unexpected QA evidence helper")


def _snapshot_imported_validator_sources() -> SourceHashSnapshot:
    hashes: dict[str, str] = {}
    revisions: dict[str, qa_evidence_manifest.SourceRevision] = {}
    for path in _IMPORTED_VALIDATOR_PATHS:
        try:
            data, revision = qa_evidence_manifest._read_stable_regular_file_with_revision(
                path, description=f"imported PERF-01 validator source {path.name}"
            )
        except EvidenceManifestError:
            raise
        relative = path.relative_to(_IMPORT_ROOT).as_posix()
        hashes[relative] = hashlib.sha256(data).hexdigest()
        revisions[relative] = revision
    return SourceHashSnapshot(hashes, revisions)


try:
    _IMPORTED_VALIDATOR_SOURCE_SNAPSHOT = _snapshot_imported_validator_sources()
except EvidenceManifestError as exc:  # pragma: no cover - import cannot continue safely
    raise RuntimeError("cannot bind imported PERF-01 validator sources") from exc
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
    raise RuntimeError("PERF-01 validator source changed during import")

RUN_SCHEMA_VERSION = 2
RUN_KIND = "perf01-field-run"
VALIDATION_SCHEMA_VERSION = 2
VALIDATION_KIND = "perf01-evidence-validation"
SOURCE_SNAPSHOT_SCHEMA_VERSION = 2
SOURCE_SNAPSHOT_KIND = "field-source-snapshot"
SOURCE_SNAPSHOT_FILENAME = "school-source-start.json"
MIN_MEASUREMENT_SECONDS = Decimal("2700")
MAX_MEDIAN_SECONDS = Decimal("5")
MAX_SINGLE_SECONDS = Decimal("8")
LANGUAGES = ("en", "zh")
TSV_HEADER = (
    "run_id",
    "seq",
    "lang",
    "representative_device_id",
    "utterance_end_at",
    "dom_display_at",
    "latency_seconds",
)

RUN_KEYS = {
    "schema_version",
    "kind",
    "field_date",
    "field_session_id",
    "host_fingerprint_sha256",
    "source_snapshot_sha256",
    "run_id",
    "clock_id",
    "measurement_window",
    "population",
    "input",
    "artifacts",
}
WINDOW_KEYS = {"started_at", "audio_ended_at", "observation_ended_at"}
POPULATION_KEYS = {
    "observed_from_at",
    "observed_until_at",
    "teacher_reported_students",
    "devices",
}
DEVICE_KEYS = {"device_id", "lang", "representative", "model", "os", "browser"}
INPUT_KEYS = {"kind", "description", "evidence"}
LIVE_INPUT_KIND = "実授業ライブ"
WAV_INPUT_KIND = "実授業WAV"
INPUT_KINDS = {LIVE_INPUT_KIND, WAV_INPUT_KIND}
ARTIFACT_KEYS = {
    "measurements_tsv",
    "finals_jsonl",
    "recordings",
    "commit_status",
    "config_models",
    "runtime_browsers",
}
FILE_REFERENCE_KEYS = {"path", "sha256"}
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
VALIDATION_KEYS = {
    "schema_version",
    "kind",
    "passed",
    "validator_sha256",
    "run_json",
    "artifact_hashes",
    "binding",
    "metrics",
    "checks",
    "scope",
}
CHECK_KEYS = (
    "run_schema_exact",
    "tsv_schema_exact",
    "artifact_hashes_match",
    "final_source_valid",
    "final_sequences_contiguous",
    "all_finals_have_en_and_zh",
    "exactly_two_rows_per_final",
    "rows_match_final_source",
    "latencies_match_timestamps",
    "timestamps_inside_window",
    "input_duration_requirement_met",
    "measurement_at_least_45_minutes",
    "exactly_ten_devices",
    "language_population_valid",
    "representatives_valid",
    "rows_bind_representatives",
    "median_at_most_5_seconds",
    "max_at_most_8_seconds",
)

SHA256_RE = re.compile(r"[0-9a-f]{64}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]*")
DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?")
RFC3339_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?P<fraction>\.[0-9]{1,9})?"
    r"(?P<zone>Z|[+-]\d{2}:\d{2})"
)


class Perf01EvidenceError(ValueError):
    """The supplied files cannot establish a trustworthy PERF-01 result."""


@dataclass(frozen=True)
class Perf01ValidationWitness:
    """Immutable, non-serialized exact read-set for one PERF-01 validation."""

    root: str
    file_sha256: tuple[tuple[str, str], ...]
    file_revisions: tuple[tuple[str, tuple[int, ...]], ...]


class ValidatedPerf01Result(dict[str, Any]):
    """dict-compatible result carrying a publication-time read witness."""

    __slots__ = ("_validation_witness",)

    def __init__(
        self, value: Mapping[str, Any], validation_witness: Perf01ValidationWitness
    ) -> None:
        super().__init__(value)
        self._validation_witness = validation_witness

    @property
    def validation_witness(self) -> Perf01ValidationWitness:
        return self._validation_witness


def _recheck_imported_perf01_modules() -> None:
    try:
        current = _snapshot_imported_validator_sources()
    except EvidenceManifestError as exc:
        raise Perf01EvidenceError(
            "cannot recheck imported PERF-01 validator sources"
        ) from exc
    if (
        dict(current) != dict(_IMPORTED_VALIDATOR_SOURCE_SNAPSHOT)
        or current.revisions != _IMPORTED_VALIDATOR_SOURCE_SNAPSHOT.revisions
    ):
        raise Perf01EvidenceError(
            "an imported PERF-01 validator source changed on disk"
        )


def _freeze_perf01_witness(
    root: Path, snapshot: Mapping[Path, tuple[str, tuple[int, ...]]]
) -> Perf01ValidationWitness:
    return Perf01ValidationWitness(
        root=str(root),
        file_sha256=tuple(
            sorted(
                (path.relative_to(root).as_posix(), digest)
                for path, (digest, _revision) in snapshot.items()
            )
        ),
        file_revisions=tuple(
            sorted(
                (path.relative_to(root).as_posix(), revision)
                for path, (_digest, revision) in snapshot.items()
            )
        ),
    )


def recheck_perf01_validation_witness(witness: Perf01ValidationWitness) -> None:
    """Recheck a PERF-01 validation immediately before publishing its JSON."""

    if type(witness) is not Perf01ValidationWitness:
        raise Perf01EvidenceError("PERF-01 validation witness type is invalid")
    _recheck_imported_perf01_modules()
    root = Path(witness.root)
    if not root.is_absolute() or str(Path(os.path.abspath(root))) != witness.root:
        raise Perf01EvidenceError("PERF-01 validation witness root is invalid")
    root = _root_path(root)
    if not witness.file_sha256 or not witness.file_revisions:
        raise Perf01EvidenceError("PERF-01 validation witness is empty")
    hashes = dict(witness.file_sha256)
    revisions = dict(witness.file_revisions)
    if (
        len(hashes) != len(witness.file_sha256)
        or len(revisions) != len(witness.file_revisions)
        or set(hashes) != set(revisions)
    ):
        raise Perf01EvidenceError("PERF-01 validation witness paths are invalid")
    paths: dict[str, Path] = {}
    for relative, digest in hashes.items():
        validated = _source_hash_index({relative: digest})
        if validated[relative] != digest:
            raise Perf01EvidenceError("PERF-01 validation witness hash is invalid")
        revision = revisions[relative]
        if (
            not isinstance(revision, tuple)
            or len(revision) != 6
            or any(type(component) is not int or component < 0 for component in revision)
        ):
            raise Perf01EvidenceError(
                "PERF-01 validation witness revision is invalid"
            )
        path = root.joinpath(*PurePosixPath(relative).parts)
        _assert_no_link_components(root, path)
        paths[relative] = path
    for relative, path in paths.items():
        digest, revision = _stable_file_hash_revision(path)
        if digest != hashes[relative] or revision != revisions[relative]:
            raise Perf01EvidenceError("PERF-01 validation read-set changed")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_file_hash_revision(path: Path) -> tuple[str, tuple[int, ...]]:
    path = Path(os.path.abspath(path))
    if _is_link_or_junction(path):
        raise Perf01EvidenceError(f"validation read-set path is a link/junction: {path}")
    try:
        lexical_before, revision_before = (
            qa_evidence_manifest._snapshot_path_revision(
                path, description=f"PERF-01 validation read-set {path.name}"
            )
        )
    except (OSError, EvidenceManifestError) as exc:
        raise Perf01EvidenceError(
            f"validation read-set path cannot be inspected: {path}"
        ) from exc
    if not stat.S_ISREG(lexical_before.st_mode):
        raise Perf01EvidenceError(
            f"validation read-set path is not a regular file: {path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise Perf01EvidenceError(
            f"validation read-set path cannot be opened: {path}"
        ) from exc
    digest = hashlib.sha256()
    try:
        descriptor_before = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_before.st_mode):
            raise Perf01EvidenceError(
                f"validation read-set path is not a regular file: {path}"
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        descriptor_after = os.fstat(descriptor)
    except OSError as exc:
        raise Perf01EvidenceError(
            f"validation read-set path cannot be read: {path}"
        ) from exc
    finally:
        os.close(descriptor)
    try:
        lexical_after, revision_after = (
            qa_evidence_manifest._snapshot_path_revision(
                path, description=f"PERF-01 validation read-set {path.name}"
            )
        )
    except (OSError, EvidenceManifestError) as exc:
        raise Perf01EvidenceError(
            f"validation read-set path cannot be rechecked: {path}"
        ) from exc
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
        or descriptor_identity_before != descriptor_identity_after
        or descriptor_identity_after != lexical_identity_after
        or revision_before != revision_after
        or not stat.S_ISREG(lexical_after.st_mode)
    ):
        raise Perf01EvidenceError(f"validation read-set path changed: {path}")
    return digest.hexdigest(), tuple(revision_after)


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Perf01EvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise Perf01EvidenceError(f"non-finite JSON number: {value}")


def _decode_json(data: bytes, label: str) -> Any:
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except Perf01EvidenceError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Perf01EvidenceError(f"{label} is not strict UTF-8 JSON: {exc}") from exc


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise Perf01EvidenceError(f"{label} schema mismatch: {actual}")
    return value


def _text(value: Any, label: str, *, identifier: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.isprintable()
        or "\x00" in value
    ):
        raise Perf01EvidenceError(f"{label} must be non-empty trimmed text")
    if identifier and not IDENTIFIER_RE.fullmatch(value):
        raise Perf01EvidenceError(f"{label} is not a portable identifier")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise Perf01EvidenceError(f"{label} must be an integer >= {minimum}")
    return value


def _json_decimal(value: Any, label: str, *, minimum: Decimal = Decimal("0")) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise Perf01EvidenceError(f"{label} must be a finite JSON number")
    result = Decimal(value)
    if not result.is_finite() or result < minimum:
        raise Perf01EvidenceError(f"{label} must be >= {minimum}")
    return result


def _decimal_text(value: str, label: str) -> Decimal:
    if not isinstance(value, str) or not DECIMAL_RE.fullmatch(value):
        raise Perf01EvidenceError(f"{label} must be a non-negative plain decimal")
    result = Decimal(value)
    if not result.is_finite():
        raise Perf01EvidenceError(f"{label} must be finite")
    return result


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _instant(value: Any, label: str) -> Decimal:
    if not isinstance(value, str):
        raise Perf01EvidenceError(f"{label} must be an RFC 3339 string")
    match = RFC3339_RE.fullmatch(value)
    if match is None:
        raise Perf01EvidenceError(f"{label} must be timezone-aware RFC 3339")
    try:
        date = dt.date.fromisoformat(match.group("date"))
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        second = int(match.group("second"))
        if hour > 23 or minute > 59 or second > 59:
            raise ValueError("invalid clock time")
        zone = match.group("zone")
        if zone == "Z":
            offset = 0
        else:
            zone_hour = int(zone[1:3])
            zone_minute = int(zone[4:6])
            if zone_hour > 23 or zone_minute > 59:
                raise ValueError("invalid UTC offset")
            offset = zone_hour * 3600 + zone_minute * 60
            if zone[0] == "-":
                offset = -offset
        fraction_text = match.group("fraction")
        fraction = Decimal(f"0{fraction_text}") if fraction_text else Decimal("0")
    except (ValueError, ArithmeticError) as exc:
        raise Perf01EvidenceError(f"{label} has an invalid RFC 3339 value") from exc
    epoch_days = date.toordinal() - dt.date(1970, 1, 1).toordinal()
    return Decimal(epoch_days * 86400 + hour * 3600 + minute * 60 + second - offset) + fraction


def _is_link_or_junction(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise Perf01EvidenceError(f"path cannot be inspected: {path}") from exc
    is_junction = getattr(os.path, "isjunction", None)
    junction = bool(is_junction(path)) if is_junction is not None else False
    if is_junction is None and os.name == "nt":
        # IO_REPARSE_TAG_MOUNT_POINT.  Do not reject every reparse tag: OneDrive
        # Files On-Demand uses non-link reparse points for otherwise normal files.
        junction = getattr(info, "st_reparse_tag", 0) == 0xA0000003
    return path.is_symlink() or junction


def _assert_no_link_components(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise Perf01EvidenceError(f"path escapes repository root: {target}") from exc
    current = root
    for component in relative.parts:
        current = current / component
        if not current.exists() and not current.is_symlink():
            raise Perf01EvidenceError(f"referenced path does not exist: {current}")
        if _is_link_or_junction(current):
            raise Perf01EvidenceError(f"links and junctions are forbidden: {current}")


def _root_path(root: Path) -> Path:
    absolute = Path(os.path.abspath(root))
    if not absolute.is_dir():
        raise Perf01EvidenceError(f"repository root is not a directory: {absolute}")
    if _is_link_or_junction(absolute):
        raise Perf01EvidenceError(f"repository root cannot be a link or junction: {absolute}")
    return absolute


def _run_path(run_json: Path, root: Path) -> Path:
    candidate = run_json if run_json.is_absolute() else root / run_json
    absolute = Path(os.path.abspath(candidate))
    try:
        absolute.relative_to(root)
    except ValueError as exc:
        raise Perf01EvidenceError("run JSON must be inside repository root") from exc
    _assert_no_link_components(root, absolute)
    if not stat.S_ISREG(absolute.lstat().st_mode):
        raise Perf01EvidenceError("run JSON must be a regular file")
    return absolute


def _relative_reference(value: Any, field_date: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or not value.isprintable():
        raise Perf01EvidenceError(f"{label}.path must be a printable POSIX path")
    pure = PurePosixPath(value)
    expected_prefix = ("docs", "field", "evidence", field_date)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or len(pure.parts) <= len(expected_prefix)
        or pure.parts[: len(expected_prefix)] != expected_prefix
        or any(part in {"", ".", ".."} or ":" in part for part in pure.parts)
    ):
        raise Perf01EvidenceError(
            f"{label}.path must be canonical and inside docs/field/evidence/{field_date}"
        )
    return value


def _file_reference(value: Any, field_date: str, label: str) -> tuple[str, str]:
    ref = _object(value, FILE_REFERENCE_KEYS, label)
    relative = _relative_reference(ref["path"], field_date, label)
    digest = ref["sha256"]
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise Perf01EvidenceError(f"{label}.sha256 must be canonical lowercase SHA-256")
    return relative, digest


def _source_hash_index(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise Perf01EvidenceError("start source snapshot source_hashes must be non-empty")
    result: dict[str, str] = {}
    for relative, digest in value.items():
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or not relative.isprintable()
        ):
            raise Perf01EvidenceError("start source snapshot source path is invalid")
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or pure.as_posix() != relative
            or any(part in {"", ".", ".."} or ":" in part for part in pure.parts)
        ):
            raise Perf01EvidenceError("start source snapshot source path is invalid")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise Perf01EvidenceError("start source snapshot source hash is invalid")
        result[relative] = digest
    return result


def _source_revision_index(
    value: Any, source_hashes: Mapping[str, str]
) -> dict[str, dict[str, dict[str, int]]]:
    if not isinstance(value, dict) or set(value) != SOURCE_REVISION_INDEX_KEYS:
        raise Perf01EvidenceError("start source snapshot source_revisions schema is invalid")
    result: dict[str, dict[str, dict[str, int]]] = {}
    for group in ("files", "directories"):
        raw_index = value.get(group)
        if not isinstance(raw_index, dict):
            raise Perf01EvidenceError(
                "start source snapshot source_revisions index is invalid"
            )
        normalized: dict[str, dict[str, int]] = {}
        for relative, revision in raw_index.items():
            if relative != "." or group != "directories":
                _source_hash_index({relative: "0" * 64})
            if not isinstance(revision, dict) or set(revision) != SOURCE_REVISION_KEYS:
                raise Perf01EvidenceError(
                    "start source snapshot source revision shape is invalid"
                )
            if any(
                type(revision[key]) is not int or revision[key] < 0
                for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")
            ):
                raise Perf01EvidenceError(
                    "start source snapshot source revision value is invalid"
                )
            normalized[relative] = {
                key: revision[key]
                for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")
            }
        result[group] = dict(sorted(normalized.items()))
    if set(result["files"]) != set(source_hashes) or not result["directories"]:
        raise Perf01EvidenceError(
            "start source snapshot source revision paths are invalid"
        )
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


def _load_start_source_snapshot(
    *,
    root: Path,
    field_date: str,
    run: Mapping[str, Any],
    run_path: Path,
    validator_path: Path,
    snapshots: dict[Path, tuple[str, bytes | None]],
) -> tuple[dict[str, Any], str, Path, Decimal]:
    path = root / "docs" / "field" / "evidence" / field_date / SOURCE_SNAPSHOT_FILENAME
    _assert_no_link_components(root, path)
    if not stat.S_ISREG(path.lstat().st_mode) or path.lstat().st_size <= 0:
        raise Perf01EvidenceError("fixed start source snapshot is missing or empty")
    for prior in (run_path, validator_path):
        if os.path.samefile(path, prior):
            raise Perf01EvidenceError(
                "start source snapshot must not hardlink-alias the run or validator"
            )
    data = _read_bytes(path)
    digest = _sha256_bytes(data)
    payload = _object(
        _decode_json(data, "start source snapshot"),
        SOURCE_SNAPSHOT_KEYS,
        "start source snapshot",
    )
    source_hashes = _source_hash_index(payload["source_hashes"])
    source_revisions = _source_revision_index(
        payload["source_revisions"], source_hashes
    )
    captured_at = _instant(
        payload["captured_at"], "start source snapshot captured_at"
    )
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != SOURCE_SNAPSHOT_SCHEMA_VERSION
        or payload["kind"] != SOURCE_SNAPSHOT_KIND
        or payload["field_date"] != field_date
        or payload["phase"] != "start"
        or payload["predecessor_sha256"] is not None
        or not isinstance(payload["session_id"], str)
        or SHA256_RE.fullmatch(payload["session_id"]) is None
        or not isinstance(payload["host_fingerprint_sha256"], str)
        or SHA256_RE.fullmatch(payload["host_fingerprint_sha256"]) is None
        or payload["source_snapshot_sha256"]
        != _source_snapshot_sha256(source_hashes, source_revisions)
        or str(payload["captured_at"])[:10] != field_date
    ):
        raise Perf01EvidenceError("start source snapshot binding is invalid")
    for field in (
        "field_session_id",
        "host_fingerprint_sha256",
        "source_snapshot_sha256",
    ):
        snapshot_field = "session_id" if field == "field_session_id" else field
        if run[field] != payload[snapshot_field]:
            raise Perf01EvidenceError(
                f"run {field} does not match the fixed start source snapshot"
            )
    snapshots[path] = (digest, data)
    return payload, digest, path, captured_at


def _stable_pcm_wav_duration(path: Path) -> tuple[str, Decimal]:
    """Hash and parse one PCM WAV through the same stable file handle."""
    try:
        with path.open("rb") as raw:
            before = os.fstat(raw.fileno())
            digest = hashlib.sha256()
            for chunk in iter(lambda: raw.read(1024 * 1024), b""):
                digest.update(chunk)
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
                    or frames <= 0
                    or rate <= 0
                ):
                    raise Perf01EvidenceError(
                        "input.evidence must be a non-empty PCM WAV"
                    )
                remaining = frames
                while remaining:
                    requested = min(remaining, 64 * 1024)
                    if (
                        len(handle.readframes(requested))
                        != requested * channels * sample_width
                    ):
                        raise Perf01EvidenceError(
                            "input.evidence PCM data is truncated"
                        )
                    remaining -= requested
            after = os.fstat(raw.fileno())
            before_identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            after_identity = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
            if before_identity != after_identity:
                raise Perf01EvidenceError(
                    "input.evidence changed while validating the PCM WAV"
                )
    except Perf01EvidenceError:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise Perf01EvidenceError(
            "input.evidence must be a valid PCM WAV"
        ) from exc
    return digest.hexdigest(), Decimal(frames) / Decimal(rate)


def _snapshot_reference(
    *,
    root: Path,
    field_date: str,
    label: str,
    reference: Any,
    snapshots: dict[Path, tuple[str, bytes | None]],
    paths_by_relative: dict[str, Path],
    wav_minimum_seconds: Decimal | None = None,
) -> tuple[str, bytes]:
    relative, expected_hash = _file_reference(reference, field_date, label)
    if relative in paths_by_relative:
        raise Perf01EvidenceError(f"duplicate referenced artifact path: {relative}")
    target = root.joinpath(*PurePosixPath(relative).parts)
    _assert_no_link_components(root, target)
    try:
        target.resolve(strict=True).relative_to(
            (root / "docs" / "field" / "evidence" / field_date).resolve(strict=True)
        )
    except (OSError, ValueError) as exc:
        raise Perf01EvidenceError(f"{label} resolves outside the field-date directory") from exc
    if not stat.S_ISREG(target.lstat().st_mode):
        raise Perf01EvidenceError(f"{label} must reference a regular file")
    if target.lstat().st_size <= 0:
        raise Perf01EvidenceError(f"{label} must reference a non-empty file")
    for prior in snapshots:
        try:
            aliased = os.path.samefile(target, prior)
        except OSError as exc:
            raise Perf01EvidenceError(f"{label} identity cannot be inspected") from exc
        if aliased:
            raise Perf01EvidenceError(
                f"{label} must not hardlink-alias a prior artifact or the run JSON"
            )
    if wav_minimum_seconds is not None:
        actual_hash, wav_duration = _stable_pcm_wav_duration(target)
        if wav_duration < wav_minimum_seconds:
            raise Perf01EvidenceError(
                "input.evidence PCM WAV is shorter than the measurement window"
            )
        data = None
    else:
        capture_bytes = label in {
            "artifacts.measurements_tsv",
            "artifacts.finals_jsonl",
        }
        data = _read_bytes(target) if capture_bytes else None
        actual_hash = _sha256_bytes(data) if data is not None else _sha256_file(target)
    if actual_hash != expected_hash:
        raise Perf01EvidenceError(f"{label} SHA-256 mismatch")
    snapshots[target] = (actual_hash, data)
    paths_by_relative[relative] = target
    return relative, data or b""


def _parse_run(payload: Any) -> dict[str, Any]:
    run = _object(payload, RUN_KEYS, "run JSON")
    if type(run["schema_version"]) is not int or run["schema_version"] != RUN_SCHEMA_VERSION:
        raise Perf01EvidenceError("unsupported run schema_version")
    if run["kind"] != RUN_KIND:
        raise Perf01EvidenceError("run kind mismatch")
    field_date = run["field_date"]
    if not isinstance(field_date, str) or DATE_RE.fullmatch(field_date) is None:
        raise Perf01EvidenceError("field_date must be YYYY-MM-DD")
    try:
        dt.date.fromisoformat(field_date)
    except ValueError as exc:
        raise Perf01EvidenceError("field_date is not a calendar date") from exc
    for field in (
        "field_session_id",
        "host_fingerprint_sha256",
        "source_snapshot_sha256",
    ):
        if not isinstance(run[field], str) or SHA256_RE.fullmatch(run[field]) is None:
            raise Perf01EvidenceError(
                f"{field} must be canonical lowercase SHA-256"
            )
    _text(run["run_id"], "run_id", identifier=True)
    _text(run["clock_id"], "clock_id")
    _object(run["measurement_window"], WINDOW_KEYS, "measurement_window")
    _object(run["population"], POPULATION_KEYS, "population")
    input_value = _object(run["input"], INPUT_KEYS, "input")
    if input_value["kind"] not in INPUT_KINDS:
        raise Perf01EvidenceError("input.kind must identify live lesson audio or a lesson WAV")
    _text(input_value["description"], "input.description")
    artifacts = _object(run["artifacts"], ARTIFACT_KEYS, "artifacts")
    recordings = artifacts["recordings"]
    if not isinstance(recordings, list) or not recordings:
        raise Perf01EvidenceError("artifacts.recordings must be a non-empty array")
    return run


def _validate_window(run: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, Decimal], Decimal]:
    raw = run["measurement_window"]
    assert isinstance(raw, dict)
    values = {key: _text(raw[key], f"measurement_window.{key}") for key in WINDOW_KEYS}
    instants = {key: _instant(values[key], f"measurement_window.{key}") for key in WINDOW_KEYS}
    if values["started_at"][:10] != run["field_date"]:
        raise Perf01EvidenceError("measurement start date does not match field_date")
    if not (
        instants["started_at"] < instants["audio_ended_at"]
        <= instants["observation_ended_at"]
    ):
        raise Perf01EvidenceError("measurement window is not ordered")
    measurement_seconds = instants["audio_ended_at"] - instants["started_at"]
    return values, instants, measurement_seconds


def _validate_population(
    run: Mapping[str, Any], window_raw: Mapping[str, str]
) -> tuple[dict[str, int], dict[str, str]]:
    population = run["population"]
    assert isinstance(population, dict)
    if (
        population["observed_from_at"] != window_raw["started_at"]
        or population["observed_until_at"] != window_raw["observation_ended_at"]
    ):
        raise Perf01EvidenceError("population observation must cover the complete window")
    _instant(population["observed_from_at"], "population.observed_from_at")
    _instant(population["observed_until_at"], "population.observed_until_at")
    if _integer(population["teacher_reported_students"], "teacher_reported_students") != 10:
        raise Perf01EvidenceError("teacher_reported_students must equal 10")
    devices = population["devices"]
    if not isinstance(devices, list) or len(devices) != 10:
        raise Perf01EvidenceError("population.devices must contain exactly 10 devices")
    seen: set[str] = set()
    counts = {language: 0 for language in LANGUAGES}
    representatives: dict[str, str] = {}
    for index, raw_device in enumerate(devices):
        device = _object(raw_device, DEVICE_KEYS, f"population.devices[{index}]")
        device_id = _text(device["device_id"], f"devices[{index}].device_id", identifier=True)
        if device_id in seen:
            raise Perf01EvidenceError(f"duplicate device_id: {device_id}")
        seen.add(device_id)
        language = device["lang"]
        if language not in LANGUAGES:
            raise Perf01EvidenceError(f"devices[{index}].lang must be en or zh")
        counts[language] += 1
        if type(device["representative"]) is not bool:
            raise Perf01EvidenceError(f"devices[{index}].representative must be boolean")
        for key in ("model", "os", "browser"):
            _text(device[key], f"devices[{index}].{key}")
        if device["representative"]:
            if language in representatives:
                raise Perf01EvidenceError(f"multiple {language} representatives")
            representatives[language] = device_id
    if any(counts[language] < 1 for language in LANGUAGES):
        raise Perf01EvidenceError("both en and zh devices are required")
    if set(representatives) != set(LANGUAGES):
        raise Perf01EvidenceError("exactly one representative is required for en and zh")
    return counts, representatives


def _parse_transcript(
    data: bytes, *, start: Decimal, observation_end: Decimal
) -> tuple[list[dict[str, Any]], dict[int, Decimal]]:
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise Perf01EvidenceError("finals_jsonl is not UTF-8") from exc
    if not text.endswith("\n"):
        raise Perf01EvidenceError("finals_jsonl must end with a newline")
    lines = text.splitlines()
    if not lines or any(not line for line in lines):
        raise Perf01EvidenceError("finals_jsonl must contain one non-empty JSON object per line")
    finals: list[dict[str, Any]] = []
    created_by_seq: dict[int, Decimal] = {}
    previous_created: Decimal | None = None
    first_seq: int | None = None
    for index, line in enumerate(lines, start=1):
        final = _object(
            _decode_json(line.encode("utf-8"), f"finals_jsonl line {index}"),
            TRANSCRIPT_KEYS,
            f"finals_jsonl line {index}",
        )
        sequence = _integer(final["seq"], f"finals_jsonl[{index}].seq", minimum=1)
        if first_seq is None:
            first_seq = sequence
        if sequence != first_seq + index - 1:
            raise Perf01EvidenceError(
                "transcript seq values must be contiguous from the positive first value"
            )
        created = _instant(final["created_at"], f"finals_jsonl[{index}].created_at")
        if not start <= created <= observation_end:
            raise Perf01EvidenceError("transcript created_at falls outside the observation window")
        if previous_created is not None and created < previous_created:
            raise Perf01EvidenceError("transcript created_at values must be nondecreasing")
        previous_created = created
        created_by_seq[sequence] = created
        t_start = _json_decimal(final["t_start"], f"finals_jsonl[{index}].t_start")
        t_end = _json_decimal(final["t_end"], f"finals_jsonl[{index}].t_end")
        if t_end < t_start:
            raise Perf01EvidenceError("transcript t_end precedes t_start")
        _text(final["ja"], f"finals_jsonl[{index}].ja")
        _integer(final["asr_ms"], f"finals_jsonl[{index}].asr_ms")
        translations = final["translations"]
        if not isinstance(translations, dict) or set(translations) != set(LANGUAGES):
            raise Perf01EvidenceError("every final must contain exactly en and zh translations")
        for language in LANGUAGES:
            translation = _object(
                translations[language],
                TRANSLATION_KEYS,
                f"finals_jsonl[{index}].translations.{language}",
            )
            _text(translation["text"], f"translations.{language}.text")
            _text(translation["engine"], f"translations.{language}.engine")
            _integer(translation["mt_ms"], f"translations.{language}.mt_ms")
        finals.append(final)
    return finals, created_by_seq


def _parse_measurements(
    data: bytes,
    *,
    run_id: str,
    final_sequences: Sequence[int],
    final_created_at: Mapping[int, Decimal],
    representatives: Mapping[str, str],
    start: Decimal,
    audio_end: Decimal,
    observation_end: Decimal,
) -> list[Decimal]:
    try:
        text = data.decode("utf-8-sig")
        records = list(csv.reader(io.StringIO(text, newline=""), delimiter="\t", strict=True))
    except (UnicodeError, csv.Error) as exc:
        raise Perf01EvidenceError("measurements_tsv is not strict UTF-8 TSV") from exc
    if not records or tuple(records[0]) != TSV_HEADER:
        raise Perf01EvidenceError("measurements_tsv header mismatch")
    rows = records[1:]
    expected_rows = len(final_sequences) * len(LANGUAGES)
    if len(rows) != expected_rows:
        raise Perf01EvidenceError(
            f"measurements_tsv must contain all 2N rows ({expected_rows}), got {len(rows)}"
        )
    latencies: list[Decimal] = []
    utterance_ends: dict[int, str] = {}
    for row_index, row in enumerate(rows, start=1):
        if len(row) != len(TSV_HEADER) or any("\x00" in cell for cell in row):
            raise Perf01EvidenceError(f"measurements_tsv row {row_index} has invalid columns")
        expected_seq = final_sequences[(row_index - 1) // len(LANGUAGES)]
        expected_language = LANGUAGES[(row_index - 1) % len(LANGUAGES)]
        if row[0] != run_id:
            raise Perf01EvidenceError(f"measurements_tsv row {row_index} run_id mismatch")
        if POSITIVE_INTEGER_RE.fullmatch(row[1]) is None or int(row[1]) != expected_seq:
            raise Perf01EvidenceError(
                "measurement rows must follow the transcript seq values, en then zh"
            )
        if row[2] != expected_language:
            raise Perf01EvidenceError("measurement rows must be ordered en then zh for every seq")
        if row[3] != representatives[expected_language]:
            raise Perf01EvidenceError("measurement representative_device_id mismatch")
        utterance_end = _instant(row[4], f"measurements_tsv[{row_index}].utterance_end_at")
        display_at = _instant(row[5], f"measurements_tsv[{row_index}].dom_display_at")
        if not start <= utterance_end <= audio_end:
            raise Perf01EvidenceError("utterance_end_at falls outside the audio window")
        if not utterance_end <= display_at <= observation_end:
            raise Perf01EvidenceError("dom_display_at falls outside the observation window")
        created_at = final_created_at[expected_seq]
        if not utterance_end <= created_at <= display_at:
            raise Perf01EvidenceError(
                "each transcript created_at must be between utterance_end_at and both DOM displays"
            )
        prior_end = utterance_ends.setdefault(expected_seq, row[4])
        if prior_end != row[4]:
            raise Perf01EvidenceError("en and zh rows must use the same utterance_end_at")
        supplied = _decimal_text(row[6], f"measurements_tsv[{row_index}].latency_seconds")
        recomputed = display_at - utterance_end
        if supplied != recomputed:
            raise Perf01EvidenceError(
                f"measurement latency mismatch at seq={expected_seq} lang={expected_language}"
            )
        latencies.append(recomputed)
    if not latencies:
        raise Perf01EvidenceError("at least one final and two measurement rows are required")
    return latencies


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _scope(*, wav_input_checked: bool) -> dict[str, Any]:
    return {
        "table_structure_arithmetic_and_hashes_checked": True,
        "input_pcm_wav_format_and_duration_checked": wav_input_checked,
        "recording_duration_checked": False,
        "physical_device_identity_checked": False,
        "simultaneous_connection_checked": False,
        "video_annotation_accuracy_checked": False,
        "same_clock_provenance_checked": False,
        "real_lesson_provenance_checked": False,
        "directory_manifest_checked": False,
        "directory_manifest_owner": "scripts.field_story_evidence.validate_outer_manifest",
    }


def _artifact_references(run: Mapping[str, Any]) -> list[tuple[str, Any]]:
    artifacts = run["artifacts"]
    assert isinstance(artifacts, dict)
    references: list[tuple[str, Any]] = [("input.evidence", run["input"]["evidence"])]
    references.extend(
        (f"artifacts.{key}", artifacts[key])
        for key in (
            "measurements_tsv",
            "finals_jsonl",
            "commit_status",
            "config_models",
            "runtime_browsers",
        )
    )
    references.extend(
        (f"artifacts.recordings[{index}]", reference)
        for index, reference in enumerate(artifacts["recordings"])
    )
    return references


def _assert_unchanged(
    snapshots: Mapping[Path, tuple[str, bytes | None]], root: Path
) -> dict[Path, tuple[str, tuple[int, ...]]]:
    paths = list(snapshots)
    for index, path in enumerate(paths):
        for prior in paths[:index]:
            try:
                aliased = os.path.samefile(path, prior)
            except OSError as exc:
                raise Perf01EvidenceError(
                    f"evidence identity changed during validation: {path}"
                ) from exc
            if aliased:
                raise Perf01EvidenceError(
                    "evidence files must not become hardlink aliases during validation"
                )
    for path, (original_hash, original_bytes) in snapshots.items():
        if path == Path(__file__).resolve():
            if _read_bytes(path) != original_bytes:
                raise Perf01EvidenceError("validator changed during validation")
            continue
        _assert_no_link_components(root, path)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise Perf01EvidenceError(f"evidence changed during validation: {path}")
        unchanged = (
            _read_bytes(path) == original_bytes
            if original_bytes is not None
            else _sha256_file(path) == original_hash
        )
        if not unchanged:
            raise Perf01EvidenceError(f"evidence changed during validation: {path}")
    evidence_paths = tuple(
        path for path in paths if path != Path(__file__).resolve()
    )
    current: dict[Path, tuple[str, tuple[int, ...]]] = {}
    for path in evidence_paths:
        digest, revision = _stable_file_hash_revision(path)
        if digest != snapshots[path][0]:
            raise Perf01EvidenceError("evidence changed during validation")
        current[path] = (digest, revision)
    return current


def validate_perf01_run(run_json: Path, *, root: Path) -> dict[str, Any]:
    """Validate one complete PERF-01 run and return deterministic JSON data.

    Malformed, incomplete, path-unsafe, hash-mismatched, or concurrently changed
    evidence raises :class:`Perf01EvidenceError`.  A structurally trustworthy run
    that misses only a latency threshold returns a normal result with
    ``passed == False`` and the corresponding threshold check set to false.
    """

    _recheck_imported_perf01_modules()
    root = _root_path(root)
    run_path = _run_path(Path(run_json), root)
    validator_path = Path(__file__).resolve()
    run_bytes = _read_bytes(run_path)
    if not run_bytes:
        raise Perf01EvidenceError("run JSON must not be empty")
    validator_bytes = _read_bytes(validator_path)
    snapshots: dict[Path, tuple[str, bytes | None]] = {
        run_path: (_sha256_bytes(run_bytes), run_bytes),
        validator_path: (_sha256_bytes(validator_bytes), validator_bytes),
    }
    run = _parse_run(_decode_json(run_bytes, "run JSON"))
    field_date = run["field_date"]
    expected_run_dir = root / "docs" / "field" / "evidence" / field_date
    try:
        run_path.resolve(strict=True).relative_to(expected_run_dir.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise Perf01EvidenceError("run JSON is outside its field-date directory") from exc

    source_snapshot, source_snapshot_artifact_sha256, source_snapshot_path, snapshot_start = (
        _load_start_source_snapshot(
            root=root,
            field_date=field_date,
            run=run,
            run_path=run_path,
            validator_path=validator_path,
            snapshots=snapshots,
        )
    )
    window_raw, window, measurement_seconds = _validate_window(run)
    if snapshot_start >= window["started_at"]:
        raise Perf01EvidenceError(
            "measurement must start after the fixed start source snapshot"
        )
    language_counts, representatives = _validate_population(run, window_raw)

    source_snapshot_relative = source_snapshot_path.relative_to(root).as_posix()
    paths_by_relative: dict[str, Path] = {
        source_snapshot_relative: source_snapshot_path
    }
    bytes_by_label: dict[str, bytes] = {}
    for label, reference in _artifact_references(run):
        _, data = _snapshot_reference(
            root=root,
            field_date=field_date,
            label=label,
            reference=reference,
            snapshots=snapshots,
            paths_by_relative=paths_by_relative,
            wav_minimum_seconds=(
                measurement_seconds
                if label == "input.evidence"
                and run["input"]["kind"] == WAV_INPUT_KIND
                else None
            ),
        )
        bytes_by_label[label] = data

    finals, final_created_at = _parse_transcript(
        bytes_by_label["artifacts.finals_jsonl"],
        start=window["started_at"],
        observation_end=window["observation_ended_at"],
    )
    latencies = _parse_measurements(
        bytes_by_label["artifacts.measurements_tsv"],
        run_id=run["run_id"],
        final_sequences=[final["seq"] for final in finals],
        final_created_at=final_created_at,
        representatives=representatives,
        start=window["started_at"],
        audio_end=window["audio_ended_at"],
        observation_end=window["observation_ended_at"],
    )
    median = _median(latencies)
    maximum = max(latencies)

    exact_read_snapshot = _assert_unchanged(snapshots, root)

    checks = {
        "run_schema_exact": True,
        "tsv_schema_exact": True,
        "artifact_hashes_match": True,
        "final_source_valid": True,
        "final_sequences_contiguous": True,
        "all_finals_have_en_and_zh": True,
        "exactly_two_rows_per_final": len(latencies) == len(finals) * len(LANGUAGES),
        "rows_match_final_source": True,
        "latencies_match_timestamps": True,
        "timestamps_inside_window": True,
        "input_duration_requirement_met": True,
        "measurement_at_least_45_minutes": measurement_seconds >= MIN_MEASUREMENT_SECONDS,
        "exactly_ten_devices": True,
        "language_population_valid": all(language_counts[lang] >= 1 for lang in LANGUAGES),
        "representatives_valid": set(representatives) == set(LANGUAGES),
        "rows_bind_representatives": True,
        "median_at_most_5_seconds": median <= MAX_MEDIAN_SECONDS,
        "max_at_most_8_seconds": maximum <= MAX_SINGLE_SECONDS,
    }
    if tuple(checks) != CHECK_KEYS:
        raise AssertionError("internal PERF-01 check schema drift")
    run_relative = run_path.relative_to(root).as_posix()
    artifact_hashes = {
        relative: snapshots[path][0]
        for relative, path in sorted(paths_by_relative.items())
    }
    result = {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "kind": VALIDATION_KIND,
        "passed": all(checks.values()),
        "validator_sha256": _sha256_bytes(validator_bytes),
        "run_json": {
            "path": run_relative,
            "sha256": _sha256_bytes(run_bytes),
        },
        "artifact_hashes": artifact_hashes,
        "binding": {
            "field_date": field_date,
            "field_session_id": run["field_session_id"],
            "host_fingerprint_sha256": run["host_fingerprint_sha256"],
            "source_snapshot_sha256": run["source_snapshot_sha256"],
            "source_snapshot_captured_at": source_snapshot["captured_at"],
            "source_snapshot_artifact_sha256": source_snapshot_artifact_sha256,
            "run_id": run["run_id"],
            "clock_id": run["clock_id"],
            "measurement_seconds": _canonical_decimal(measurement_seconds),
            "student_count": 10,
            "language_counts": {
                language: language_counts[language] for language in LANGUAGES
            },
            "representatives": {
                language: representatives[language] for language in LANGUAGES
            },
            "first_seq": finals[0]["seq"],
            "last_seq": finals[-1]["seq"],
        },
        "metrics": {
            "final_count": len(finals),
            "expected_rows": len(finals) * len(LANGUAGES),
            "matched_rows": len(latencies),
            "latency_median_seconds": _canonical_decimal(median),
            "latency_max_seconds": _canonical_decimal(maximum),
        },
        "checks": checks,
        "scope": _scope(wav_input_checked=run["input"]["kind"] == WAV_INPUT_KIND),
    }
    if set(result) != VALIDATION_KEYS or result["passed"] is not all(checks.values()):
        raise AssertionError("internal PERF-01 validation schema drift")
    witness = _freeze_perf01_witness(root, exact_read_snapshot)
    recheck_perf01_validation_witness(witness)
    return ValidatedPerf01Result(result, witness)


def _invalid_result(run_json: Path, root: Path) -> dict[str, Any]:
    try:
        validator_hash = _sha256_bytes(_read_bytes(Path(__file__).resolve()))
    except OSError:
        validator_hash = ""
    try:
        candidate = run_json if run_json.is_absolute() else root / run_json
        candidate = Path(os.path.abspath(candidate))
        relative = candidate.relative_to(root).as_posix()
        _assert_no_link_components(root, candidate)
        run_hash = (
            _sha256_bytes(_read_bytes(candidate))
            if stat.S_ISREG(candidate.lstat().st_mode)
            else ""
        )
    except (OSError, ValueError, Perf01EvidenceError):
        relative = str(run_json).replace("\\", "/")
        run_hash = ""
    checks = {key: False for key in CHECK_KEYS}
    return {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "kind": VALIDATION_KIND,
        "passed": all(checks.values()),
        "validator_sha256": validator_hash,
        "run_json": {"path": relative, "sha256": run_hash},
        "artifact_hashes": {},
        "binding": {
            "field_date": "",
            "field_session_id": "",
            "host_fingerprint_sha256": "",
            "source_snapshot_sha256": "",
            "source_snapshot_captured_at": "",
            "source_snapshot_artifact_sha256": "",
            "run_id": "",
            "clock_id": "",
            "measurement_seconds": "0",
            "student_count": 0,
            "language_counts": {"en": 0, "zh": 0},
            "representatives": {"en": "", "zh": ""},
            "first_seq": 0,
            "last_seq": 0,
        },
        "metrics": {
            "final_count": 0,
            "expected_rows": 0,
            "matched_rows": 0,
            "latency_median_seconds": "",
            "latency_max_seconds": "",
        },
        "checks": checks,
        "scope": _scope(wav_input_checked=False),
    }


def _json_document(result: Mapping[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _shell_summary(result: Mapping[str, Any]) -> str:
    binding = result["binding"]
    metrics = result["metrics"]
    counts = binding["language_counts"]
    fields = (
        "true" if result.get("passed") is True else "false",
        str(metrics["final_count"]),
        str(metrics["expected_rows"]),
        str(metrics["matched_rows"]),
        str(binding["measurement_seconds"]),
        str(binding["student_count"]),
        str(counts["en"]),
        str(counts["zh"]),
        str(metrics["latency_median_seconds"]),
        str(metrics["latency_max_seconds"]),
    )
    return "\t".join(fields)


def _atomic_write(
    path: Path,
    content: str,
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    path = Path(os.path.abspath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _protected_output_paths(result: Mapping[str, Any], root: Path, run_json: Path) -> set[Path]:
    paths = {Path(os.path.abspath(run_json if run_json.is_absolute() else root / run_json))}
    for relative in result.get("artifact_hashes", {}):
        paths.add(Path(os.path.abspath(root.joinpath(*PurePosixPath(relative).parts))))
    paths.add(Path(__file__).resolve())
    return paths


def _fixed_validation_output(run_json: Path, root: Path) -> Path:
    candidate = run_json if run_json.is_absolute() else root / run_json
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise Perf01EvidenceError("run JSON must be inside repository root") from exc
    if (
        len(relative.parts) < 5
        or relative.parts[:3] != ("docs", "field", "evidence")
        or DATE_RE.fullmatch(relative.parts[3]) is None
    ):
        raise Perf01EvidenceError(
            "run JSON must be under docs/field/evidence/<YYYY-MM-DD>"
        )
    field_date = relative.parts[3]
    try:
        dt.date.fromisoformat(field_date)
    except ValueError as exc:
        raise Perf01EvidenceError("run directory does not contain a calendar date") from exc
    output = root / "docs" / "field" / "evidence" / field_date / (
        f"{field_date}-perf01-validation.json"
    )
    _assert_no_link_components(root, output.parent)
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate",))
    parser.add_argument("run_json", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--format", choices=("json", "shell"), default="json")
    args = parser.parse_args(argv)
    try:
        _recheck_imported_perf01_modules()
        root = _root_path(ROOT)
    except (OSError, Perf01EvidenceError) as exc:
        print(f"PERF01_EVIDENCE_INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    try:
        expected_output = _fixed_validation_output(args.run_json, root)
    except Perf01EvidenceError as exc:
        print(f"PERF01_EVIDENCE_INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    output = Path(os.path.abspath(args.output))
    if output != expected_output:
        print(
            f"PERF01_EVIDENCE_INVALID: --output must be {expected_output}",
            file=sys.stderr,
        )
        return 2
    try:
        result = validate_perf01_run(args.run_json, root=root)
    except (OSError, Perf01EvidenceError, ValueError, TypeError, KeyError) as exc:
        result = _invalid_result(args.run_json, root)
        error = f"PERF01_EVIDENCE_INVALID: {type(exc).__name__}: {exc}"
        print(error, file=sys.stderr)
    if output in _protected_output_paths(result, root, args.run_json):
        print("PERF01_EVIDENCE_INVALID: --output would overwrite source evidence", file=sys.stderr)
        return 2
    document = _json_document(result)
    before_replace: Callable[[], None]
    if isinstance(result, ValidatedPerf01Result):
        before_replace = lambda: recheck_perf01_validation_witness(
            result.validation_witness
        )
    else:
        before_replace = _recheck_imported_perf01_modules
    try:
        _atomic_write(output, document, before_replace=before_replace)
    except (OSError, Perf01EvidenceError) as exc:
        print(f"PERF01_EVIDENCE_INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(document.rstrip() if args.format == "json" else _shell_summary(result))
    return 0 if result["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
