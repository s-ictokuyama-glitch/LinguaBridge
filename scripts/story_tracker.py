"""Build the single-sheet QA tracker and sync immutable test-run evidence.

OfficeCLI owns all workbook writes. The catalog is the reviewed story definition;
the workbook is the status register. Sync changes only rows tested in that run,
resolves rows by stable ID (so Excel sorting is safe), and backs up the workbook.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from collections import Counter
import ctypes
import csv
from dataclasses import dataclass
from datetime import datetime
import hashlib
from html.parser import HTMLParser
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from zipfile import ZipFile


@dataclass(frozen=True)
class CodeBinding:
    """Exact source bytes and ordinary revision state used by this process."""

    path: Path
    revision: tuple[int, int, int, int, int, int]
    ancestor_identities: tuple[tuple[Path, tuple[int, int]], ...]
    sha256: str


class _WindowsFileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", ctypes.c_ulong),
    ]


def _code_change_time_ns(path: Path) -> int:
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
        information = _WindowsFileBasicInfo()
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


def _code_file_revision(
    value: os.stat_result, *, change_time_ns: int
) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        change_time_ns,
    )


def _snapshot_code_revision(
    path: Path,
) -> tuple[os.stat_result, tuple[int, int, int, int, int, int]]:
    """Capture stat and Windows ChangeTime at one stable observation point."""

    before = path.lstat()
    before_change = _code_change_time_ns(path)
    after = path.lstat()
    after_change = _code_change_time_ns(path)
    before_revision = _code_file_revision(
        before, change_time_ns=before_change
    )
    after_revision = _code_file_revision(after, change_time_ns=after_change)
    if before_revision != after_revision:
        raise OSError(f"repository file changed while revision was captured: {path}")
    return after, after_revision


def _read_bound_repository_file(
    path: Path, *, root: Path
) -> tuple[bytes, CodeBinding]:
    """Read one repository file without resolving a linked path component."""

    absolute_root = Path(os.path.abspath(root))
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(absolute_root)
    except ValueError as exc:
        raise OSError(f"imported repository module is outside the root: {path}") from exc
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    ancestors: list[Path] = [absolute_root]
    current = absolute_root
    for part in relative.parts[:-1]:
        current /= part
        ancestors.append(current)
    ancestor_before: list[tuple[Path, tuple[int, int]]] = []
    for ancestor in ancestors:
        identity = ancestor.lstat()
        file_attributes = getattr(identity, "st_file_attributes", 0)
        reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(identity.st_mode)
            or bool(file_attributes & reparse_mask)
            or bool(is_junction(ancestor))
            or not stat.S_ISDIR(identity.st_mode)
        ):
            raise OSError(
                f"imported repository module has a linked ancestor: {ancestor}"
            )
        ancestor_before.append(
            (ancestor, (identity.st_dev, identity.st_ino))
        )
    if absolute.is_symlink() or bool(is_junction(absolute)):
        raise OSError(f"imported repository module is a link or junction: {absolute}")
    before, before_revision = _snapshot_code_revision(absolute)
    if not stat.S_ISREG(before.st_mode):
        raise OSError(f"imported repository module is not a regular file: {absolute}")
    content = absolute.read_bytes()
    after, after_revision = _snapshot_code_revision(absolute)
    if before_revision != after_revision:
        raise OSError(f"imported repository module changed while read: {absolute}")
    for ancestor, expected in ancestor_before:
        current_identity = ancestor.lstat()
        if (
            ancestor.is_symlink()
            or bool(is_junction(ancestor))
            or not stat.S_ISDIR(current_identity.st_mode)
            or (current_identity.st_dev, current_identity.st_ino) != expected
        ):
            raise OSError(
                f"imported repository module ancestor changed while read: {ancestor}"
            )
    binding = CodeBinding(
        path=absolute,
        revision=after_revision,
        ancestor_identities=tuple(ancestor_before),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    return content, binding


def _capture_code_binding(path: Path, *, root: Path) -> CodeBinding:
    """Capture a stable repository-file validation witness."""

    return _read_bound_repository_file(path, root=root)[1]


_STORY_TRACKER_PATH = Path(os.path.abspath(__file__))
ROOT = _STORY_TRACKER_PATH.parent.parent
_IMPORT_ROOT = ROOT
_EARLY_IMPORT_CODE_PATHS = (
    _STORY_TRACKER_PATH,
    ROOT / "scripts/qa_evidence_manifest.py",
    ROOT / "scripts/field_story_evidence.py",
    ROOT / "scripts/performance_evidence.py",
)
_EARLY_IMPORT_CODE_BINDINGS = {
    path: _capture_code_binding(path, root=_IMPORT_ROOT)
    for path in _EARLY_IMPORT_CODE_PATHS
}
_STORY_TRACKER_IMPORT_BINDING = _EARLY_IMPORT_CODE_BINDINGS[
    _STORY_TRACKER_PATH
]
# A restored source mtime can make CPython accept an old same-size ``.pyc``.
# Force every repository import below to compile into a fresh, process-private
# cache tree instead of consulting the worktree's pre-existing bytecode.
_IMPORT_PYCACHE = tempfile.TemporaryDirectory(
    prefix="linguabridge-story-import-pycache-"
)
sys.pycache_prefix = _IMPORT_PYCACHE.name
sys.dont_write_bytecode = True
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.performance_evidence as performance_evidence_module  # noqa: E402
import scripts.field_story_evidence as field_story_evidence_module  # noqa: E402
import scripts.qa_evidence_manifest as qa_evidence_manifest_module  # noqa: E402
from scripts.performance_evidence import (  # noqa: E402
    MANIFEST_NAME as PERFORMANCE_MANIFEST_NAME,
    PerformanceEvidenceError,
    validate_performance_manifest,
)
from scripts.field_story_evidence import (  # noqa: E402
    FIELD_STORIES,
    FieldEvidenceError,
    load_strict_json as load_strict_field_json,
    validate_field_bundle,
)
from scripts.qa_evidence_manifest import (  # noqa: E402
    EvidenceManifestError,
    SealedBrowserEvidence,
    SealedPytestEvidence,
    SourceHashSnapshot,
    complete_pytest_evidence,
    load_sealed_browser_evidence,
    load_sealed_pytest_evidence,
    publish_pytest_evidence,
    snapshot_supplied_source_hashes,
    snapshot_source_hashes,
    validate_output_parent,
)

# Repository validators are also executable entry points and may install their
# own private cache prefix while they are imported.  Restore the tracker's
# already-created empty prefix before any later local import or proof check;
# every validator import above was protected by either this prefix or the
# validator's newer private prefix, and the long-running tracker now owns the
# process-wide setting again.
sys.pycache_prefix = _IMPORT_PYCACHE.name
sys.dont_write_bytecode = True


def _module_source_path(module: object) -> Path:
    value = getattr(module, "__file__", None)
    if not isinstance(value, str) or not value:
        raise OSError("imported repository module has no source path")
    path = Path(os.path.abspath(value))
    if path.suffix == ".pyc" and path.with_suffix("").suffix == ".py":
        path = path.with_suffix("")
    return path


if _capture_code_binding(
    _STORY_TRACKER_PATH, root=_IMPORT_ROOT
) != _STORY_TRACKER_IMPORT_BINDING:
    raise RuntimeError("story_tracker.py changed while repository modules were imported")

_IMPORTED_MODULE_PATHS = (
    _STORY_TRACKER_PATH,
    _module_source_path(qa_evidence_manifest_module),
    _module_source_path(field_story_evidence_module),
    _module_source_path(performance_evidence_module),
)
_IMPORT_CODE_BINDINGS = {
    path: _capture_code_binding(path, root=_IMPORT_ROOT)
    for path in _IMPORTED_MODULE_PATHS
}
if (
    set(_IMPORT_CODE_BINDINGS) != set(_EARLY_IMPORT_CODE_BINDINGS)
    or any(
        _IMPORT_CODE_BINDINGS[path] != early
        for path, early in _EARLY_IMPORT_CODE_BINDINGS.items()
    )
):
    raise RuntimeError(
        "repository audit code changed while local modules were imported"
    )


def _recheck_code_binding_revision(binding: CodeBinding) -> None:
    # On Windows a same-size overwrite followed by os.utime can preserve every
    # exposed stat field.  Re-read stable bytes as well as checking identity;
    # revision-only fences would otherwise accept an ABA content replacement.
    root = (
        binding.ancestor_identities[0][0]
        if binding.ancestor_identities
        else binding.path.parent
    )
    observed = _capture_code_binding(binding.path, root=root)
    if observed != binding:
        raise OSError(f"bound repository file changed: {binding.path}")


def _import_cache_is_fresh_real_directory(prefix: object) -> bool:
    """Accept only an empty absolute cache path with no linked component."""

    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    try:
        if not isinstance(prefix, str) or not prefix:
            return False
        cache_root = Path(prefix)
        if not cache_root.is_absolute():
            return False
        reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        identities: list[tuple[Path, tuple[int, int]]] = []
        current = Path(cache_root.anchor)
        for part in cache_root.parts[1:]:
            current /= part
            identity = current.lstat()
            if (
                stat.S_ISLNK(identity.st_mode)
                or not stat.S_ISDIR(identity.st_mode)
                or bool(
                    getattr(identity, "st_file_attributes", 0) & reparse_mask
                )
                or bool(is_junction(current))
            ):
                return False
            identities.append((current, (identity.st_dev, identity.st_ino)))
        if not identities or any(cache_root.iterdir()):
            return False
        for path, expected in identities:
            identity = path.lstat()
            if (
                stat.S_ISLNK(identity.st_mode)
                or not stat.S_ISDIR(identity.st_mode)
                or bool(
                    getattr(identity, "st_file_attributes", 0) & reparse_mask
                )
                or bool(is_junction(path))
                or (identity.st_dev, identity.st_ino) != expected
            ):
                return False
        return not any(cache_root.iterdir())
    except OSError:
        return False


def _validated_import_code_bindings() -> tuple[CodeBinding, ...]:
    """Re-read every local module and return its latest validation witnesses."""

    prefix = sys.pycache_prefix
    # Another audited module may install its own fresh private prefix after the
    # tracker imports.  Prefix ownership is irrelevant; an empty real cache
    # with bytecode writes disabled preserves the same source-only guarantee.
    if not sys.dont_write_bytecode or not _import_cache_is_fresh_real_directory(
        prefix
    ):
        raise OSError("repository bytecode cache isolation was lost")
    current: list[CodeBinding] = []
    for path, imported in _IMPORT_CODE_BINDINGS.items():
        observed = _capture_code_binding(path, root=_IMPORT_ROOT)
        if observed != imported:
            raise OSError(f"imported code differs from current disk state: {path}")
        current.append(observed)
    for binding in current:
        _recheck_code_binding_revision(binding)
    if (
        sys.pycache_prefix != prefix
        or not sys.dont_write_bytecode
        or not _import_cache_is_fresh_real_directory(prefix)
    ):
        raise OSError("repository bytecode cache isolation changed during validation")
    return tuple(current)


def _import_code_is_current() -> bool:
    try:
        _validated_import_code_bindings()
        return True
    except OSError:
        return False


def _require_import_code_current(*, checkpoint: str) -> tuple[CodeBinding, ...]:
    try:
        return _validated_import_code_bindings()
    except OSError as exc:
        raise SystemExit(
            f"imported audit code changed before {checkpoint}; rerun the command: {exc}"
        ) from exc

QA = ROOT / "docs" / "qa"
CATALOG = QA / "stories.tsv"
BOOK = QA / "feature-status.xlsx"
SHEET = "Stories"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
HEADERS = ["ID", "領域", "ユーザーストーリー", "期待する挙動・合格条件", "実装状態", "検証状態", "検証方式", "前提", "操作・テスト手順", "要件", "実装根拠", "自動テスト候補", "今回の証跡", "残作業・注意", "確認日時"]
STATES = ["未実施", "合格", "不合格", "部分確認", "実行不可", "未実装"]
# Audited against the complete acceptance text, not just matching test filenames.
# Expand only after reviewing the actual assertions covering every clause.
AUDITED_AUTOMATIC = {"OPS-04", "OPS-05", "OPS-07", "OPS-11", "JOIN-04", "JOIN-05", "JOIN-06", "ASR-02", "ASR-03", "ASR-04",
                     "MT-01", "MT-03", "REC-01", "REC-04", "REC-06",
                     "CONN-07", "SAFE-05", "SAFE-06", "SAFE-07",
                     "ASR-05", "TURN-01", "TURN-02", "TURN-03", "TURN-04", "MT-02", "MT-04", "CONN-02", "MON-03",
                     "REC-03", "SAFE-01", "SAFE-02", "SAFE-03", "SAFE-04", "SAFE-09",
                     "OPS-06", "ASR-01", "ASR-06"}
# These contracts end at the rendered DOM/settings behavior. Controlled input
# events are sufficient; no claim about real transport or physical devices.
AUDITED_BROWSER = {
    "JOIN-02": {"qr_query_prefills_code"},
    "JOIN-03": {"wrong_code_then_retry"},
    "STU-02": {"follow_pause_and_latest"},
    "STU-01": {"cards_order_dedup_plain_text"},
    "STU-03": {"original_toggle_and_persistence"},
    "STU-04": {"font_sizes_and_persistence"},
    "STU-05": {"storage_denied_student", "storage_denied_teacher"},
    "STU-08": {"session_banners"},
    "STU-09": {"delay_boundary_and_replay"},
    "MON-04": {"native_join_change_leave_stats"},
    "REC-02": {"native_recording_all_screens"},
    "TEACH-03": {"native_pause_flush_gate_resume"},
    "TEACH-04": {"native_end_live_flush_and_gate", "native_end_paused_and_gate"},
    "STU-06": {"native_language_switch_and_rejection", "native_language_switch_during_replay"},
    "MON-01": {"native_transcript_last_50_and_follow"},
    "MON-05": {"native_latency_breakdown_wait_active_drain"},
    "MON-06": {"native_cache_stats_keep_real_inference_median"},
    "MON-07": {"native_silence_warning_lifecycle_and_swap"},
    "MON-08": {"native_overload_audio_and_mt_then_clear"},
    "MON-02": {"native_partial_on_replaces_final_pause", "native_partial_off_is_default"},
    "CONN-06": {"native_teacher_disconnect_flush_and_resume"},
    "STU-10": {"native_speaking_indicator_lifecycle", "native_speaking_after_capture_gap"},
    "MIC-02": {"microphone_persistence_and_missing_fallback"},
    "MIC-01": {"microphone_list_and_selection"},
    "MIC-03": {"microphone_switch_failure_preserves_old_track"},
    "MIC-04": {"microphone_meter"},
    "CONN-01": {"student_retries_multiple_failures"},
    "CONN-03": {"continuous_watermark_and_history_floor"},
    "TEACH-02": {"native_mic_denied_same_tab_retry_to_caption"},
}

# These stories cross evidence types. Neither half may independently earn a
# pass: the browser proof and the pytest proof must both be current and green.
AUDITED_COMPOSITE = {
    "OPS-09": {
        "browser": {"native_http_audio_worklet"},
        "pytest": {
            "tests/integration/test_ops_certificates.py::test_startup_without_certificate_prints_local_http_teacher_url",
            "tests/integration/test_ops_certificates.py::test_serve_without_certificate_is_http_only_and_opens_local_teacher_url",
        },
    },
    "TEACH-01": {
        "browser": {"native_audio_to_caption"},
        "pytest": {
            "qa_checks/test_physical_windows_evidence.py::test_teach01_physical_microphone_sends_pcm_frames_over_loopback_ws_without_persistence",
            "tests/integration/test_ws_boundary.py::TestCaptionFlow::test_teacher_speech_reaches_student_in_selected_lang",
        },
    },
    "SAFE-08": {
        "browser": {"native_local_only_lesson_path"},
        "pytest": {
            "qa_checks/test_real_model_story_e2e.py::test_asr01_real_japanese_audio_reaches_selected_languages_locally",
        },
    },
}
# Performance reports are neither pytest nor browser evidence.  Their audited
# protocol includes source/input provenance and minimum-duration/matrix gates.
AUDITED_PERFORMANCE = {
    "PERF-03": "soak",
    "PERF-04": "scale",
    "PERF-05": "soak",
}
AUDITED_FIELD = set(FIELD_STORIES)
FIELD_IMPORT_SCHEMA = 2


@dataclass(frozen=True)
class FieldProofSnapshot:
    approved: frozenset[str]
    receipt_sha256: str
    receipt_witness: CodeBinding | None
    validation_witness: field_story_evidence_module.FieldValidationWitness | None

    def __getitem__(self, index: int) -> object:
        # Compatibility for the established cache callers while names make
        # the security witnesses explicit.
        return (self.approved, self.receipt_sha256)[index]


FieldProofCache = dict[str, FieldProofSnapshot]
_STRICT_FIELD_VALIDATOR = validate_field_bundle
_STRICT_PERFORMANCE_VALIDATOR = validate_performance_manifest


@dataclass(frozen=True)
class PerformanceProofState:
    semantic: tuple[object, object, object, object]
    validation_witness: performance_evidence_module.ValidationWitness | None


@dataclass(frozen=True)
class PerformanceRunWitness:
    manifest_validation_witness: performance_evidence_module.ValidationWitness | None
    evidence_witness: CodeBinding


def _performance_semantic(
    manifest: dict[str, object],
) -> tuple[object, object, object, object]:
    """Return the stable subset whose equality carries proof meaning."""

    return (
        manifest.get("source_hashes"),
        manifest.get("input_hashes"),
        manifest.get("runtime_asset_hashes"),
        manifest.get("runtime"),
    )


@dataclass(frozen=True)
class ProofDependency:
    """A proof whose truth contributed to a planned workbook pass."""

    kind: str
    story: str
    evidence: str
    row: int
    required: frozenset[str] | None = None


class AuditProofCache:
    """One audit pass's immutable reads and proof decisions."""

    def __init__(self) -> None:
        self.import_witnesses: tuple[CodeBinding, ...] = ()
        self.json_payloads: dict[Path, object] = {}
        self.file_fingerprints: dict[Path, str] = {}
        self.file_witnesses: dict[Path, CodeBinding] = {}
        self.source_snapshots: dict[tuple[str, bool], SourceHashSnapshot] = {}
        self.browser_runs: dict[Path, SealedBrowserEvidence] = {}
        self.pytest_runs: dict[Path, SealedPytestEvidence] = {}
        self.performance_results: dict[
            tuple[Path, str, Path], frozenset[str]
        ] = {}
        self.performance_states: dict[
            tuple[Path, str, Path], PerformanceProofState
        ] = {}
        self.catalog_rows: dict[str, dict[str, str]] | None = None
        self.catalog_sha256: str | None = None
        self.field: FieldProofCache = {}

    def freshness_state(self) -> tuple[dict, dict, dict, dict, str | None]:
        return (
            self.file_fingerprints,
            self.file_witnesses,
            self.source_snapshots,
            self.performance_states,
            self.catalog_sha256,
        )


@dataclass(frozen=True)
class VerifiedWorkbookState:
    """A proof-audited live workbook state bound to exact file revision."""

    file_revision: tuple[int, int, int, int, int]
    parent_identity: tuple[int, int]
    sha256: str
    size: int
    proof_cache: AuditProofCache


SCALE_LANGUAGES = ["en", "zh", "pt", "vi", "ko"]
PERFORMANCE_DRIFT_WINDOW_S = 300.0
PERFORMANCE_RESOURCE_INTERVAL_S = 2.0
CATALOG_HEADERS = (
    "ID",
    "領域",
    "ユーザーストーリー",
    "期待する挙動・合格条件",
    "前提",
    "操作・テスト手順",
    "要件",
    "実装根拠",
    "実装状態",
    "検証方式",
    "自動テスト候補",
    "残作業・注意",
)


def _parse_catalog_bytes(value: bytes) -> list[dict[str, str]]:
    try:
        text = value.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("story catalog must be valid UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter="\t", strict=True)
    if tuple(reader.fieldnames or ()) != CATALOG_HEADERS:
        raise ValueError("story catalog has an unexpected header")
    rows = list(reader)
    if any(set(row) != set(CATALOG_HEADERS) or any(v is None for v in row.values()) for row in rows):
        raise ValueError("story catalog contains a malformed TSV row")
    ids = [row["ID"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate story IDs")
    for row in rows:
        if not all(
            row[k]
            for k in [
                "ID",
                "ユーザーストーリー",
                "期待する挙動・合格条件",
                "操作・テスト手順",
                "実装根拠",
            ]
        ):
            raise ValueError(f"story catalog contains an incomplete row: {row!r}")
        for selector in row["自動テスト候補"].split(";"):
            if selector and not (ROOT / selector.split("::")[0]).is_file():
                raise ValueError(f"story catalog selector is missing: {selector}")
    return rows


def _catalog_snapshot(
    cache: AuditProofCache | None = None,
) -> tuple[dict[str, dict[str, str]], str]:
    if cache is not None and cache.catalog_rows is not None:
        if cache.catalog_sha256 is None:
            raise ValueError("cached story catalog digest is missing")
        return cache.catalog_rows, cache.catalog_sha256
    value, witness = _read_bound_repository_file(CATALOG, root=ROOT)
    rows = _parse_catalog_bytes(value)
    by_id = {row["ID"]: row for row in rows}
    digest = hashlib.sha256(value).hexdigest()
    if cache is not None:
        cache.catalog_rows = by_id
        cache.catalog_sha256 = digest
        cache.file_fingerprints[witness.path] = digest
        cache.file_witnesses[witness.path] = witness
    return by_id, digest


def catalog() -> list[dict[str, str]]:
    return list(_catalog_snapshot()[0].values())


_STRICT_CATALOG_READER = catalog


def _proof_catalog_snapshot(
    cache: AuditProofCache | None,
) -> tuple[dict[str, dict[str, str]], str | None]:
    """Load the exact catalog bytes; retain the legacy callable as a test seam."""

    if catalog is not _STRICT_CATALOG_READER:
        rows = catalog()
        return {row["ID"]: row for row in rows}, None
    return _catalog_snapshot(cache)


def validate_audit_configuration() -> None:
    _require_import_code_current(checkpoint="audit planning")
    groups = [
        set(AUDITED_AUTOMATIC),
        set(AUDITED_BROWSER),
        set(AUDITED_COMPOSITE),
        set(AUDITED_PERFORMANCE),
        set(AUDITED_FIELD),
    ]
    overlaps = set()
    for index, group in enumerate(groups):
        for other in groups[index + 1 :]:
            overlaps |= group & other
    assert not overlaps, f"Stories may use only one audit mode: {sorted(overlaps)}"
    rows = {row["ID"]: row for row in catalog()}
    for story, proof in AUDITED_COMPOSITE.items():
        selectors = {value for value in rows[story]["自動テスト候補"].split(";") if value}
        assert selectors == proof["pytest"], f"Composite pytest selectors differ for {story}"
    assert set(AUDITED_PERFORMANCE) <= set(rows), "Performance audit story is absent from catalog"
    assert set(AUDITED_FIELD) <= set(rows), "Field audit story is absent from catalog"


def office(*args: str, stdin: str | None = None) -> str:
    env = {**os.environ, "OFFICECLI_NO_AUTO_RESIDENT": "1"}
    result = subprocess.run(["officecli", *map(str, args)], input=stdin, capture_output=True,
                            encoding="utf-8", errors="replace", env=env, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(f"officecli {args}: {result.stdout}\n{result.stderr}")
    return result.stdout


def _stable_file_snapshot(path: Path) -> tuple[bytes, tuple[object, ...]]:
    """Read stable regular bytes and bind file plus parent identity."""

    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    parent = path.parent
    absolute = Path(os.path.abspath(path))
    if path.resolve(strict=True) != absolute:
        raise OSError(f"path must not contain a link or junction: {path}")
    if (
        path.is_symlink()
        or bool(is_junction(path))
        or parent.is_symlink()
        or bool(is_junction(parent))
    ):
        raise OSError(f"workbook path must not contain a link or junction: {path}")
    before = path.stat(follow_symlinks=False)
    parent_before = parent.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or not stat.S_ISDIR(parent_before.st_mode):
        raise OSError(f"workbook must be a regular file: {path}")
    content = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    parent_after = parent.stat(follow_symlinks=False)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(after) or identity(parent_before) != identity(parent_after):
        raise OSError(f"workbook changed while being read: {path}")
    fingerprint = (
        identity(after),
        identity(parent_after),
        hashlib.sha256(content).hexdigest(),
    )
    return content, fingerprint


def _stable_file_fingerprint(path: Path) -> tuple[object, ...]:
    return _stable_file_snapshot(path)[1]


def _exclusive_workbook_backup(destination: Path) -> None:
    """Create one immutable workbook backup without following destination links."""

    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    parent = destination.parent
    try:
        if parent.is_symlink() or bool(is_junction(parent)) or not parent.is_dir():
            raise OSError("backup parent must be a real directory")
        parent_before = parent.stat(follow_symlinks=False)
        workbook_bytes, workbook_fingerprint = _stable_file_snapshot(BOOK)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise SystemExit(
            f"workbook backup target already exists: {destination}"
        ) from exc
    except OSError as exc:
        raise SystemExit(f"workbook backup cannot be created safely: {exc}") from exc

    created_identity: tuple[int, int] | None = None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("backup descriptor is not a regular file")
        created_identity = (opened.st_dev, opened.st_ino)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(workbook_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.lseek(descriptor, 0, os.SEEK_SET)
        written_bytes = bytearray()
        while len(written_bytes) <= len(workbook_bytes):
            chunk = os.read(descriptor, min(1024 * 1024, len(workbook_bytes) + 1 - len(written_bytes)))
            if not chunk:
                break
            written_bytes.extend(chunk)
        written = destination.stat(follow_symlinks=False)
        if (
            destination.is_symlink()
            or bool(is_junction(destination))
            or not stat.S_ISREG(written.st_mode)
            or (written.st_dev, written.st_ino) != created_identity
            or bytes(written_bytes) != workbook_bytes
        ):
            raise OSError("backup path or bytes changed during creation")
        parent_after = parent.stat(follow_symlinks=False)
        if (parent_after.st_dev, parent_after.st_ino) != (
            parent_before.st_dev,
            parent_before.st_ino,
        ):
            raise OSError("backup parent changed during creation")
        if _stable_file_fingerprint(BOOK) != workbook_fingerprint:
            raise OSError("live workbook changed during backup creation")
        os.close(descriptor)
        descriptor = -1
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            current = destination.stat(follow_symlinks=False)
            if (
                created_identity is not None
                and not destination.is_symlink()
                and not bool(is_junction(destination))
                and (current.st_dev, current.st_ino) == created_identity
            ):
                destination.unlink()
        except OSError:
            pass
        raise


def _exclusive_regular_file_write(
    destination: Path, payload: bytes, *, label: str
) -> None:
    """Create verified bytes through an exclusive descriptor, never a path link."""

    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    parent = destination.parent
    descriptor = -1
    created_identity: tuple[int, int] | None = None
    try:
        if parent.is_symlink() or bool(is_junction(parent)) or not parent.is_dir():
            raise OSError(f"{label} parent must be a real directory")
        parent_before = parent.stat(follow_symlinks=False)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination, flags, 0o600)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"{label} descriptor is not a regular file")
        created_identity = (opened.st_dev, opened.st_ino)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.lseek(descriptor, 0, os.SEEK_SET)
        verified = bytearray()
        while len(verified) <= len(payload):
            chunk = os.read(
                descriptor,
                min(1024 * 1024, len(payload) + 1 - len(verified)),
            )
            if not chunk:
                break
            verified.extend(chunk)
        current = destination.stat(follow_symlinks=False)
        parent_after = parent.stat(follow_symlinks=False)
        if (
            bytes(verified) != payload
            or destination.is_symlink()
            or bool(is_junction(destination))
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != created_identity
            or (parent_after.st_dev, parent_after.st_ino)
            != (parent_before.st_dev, parent_before.st_ino)
        ):
            raise OSError(f"{label} path or bytes changed during creation")
        os.close(descriptor)
        descriptor = -1
    except FileExistsError as exc:
        raise SystemExit(f"{label} target already exists: {destination}") from exc
    except OSError as exc:
        raise SystemExit(f"{label} cannot be created safely: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if descriptor >= 0 and created_identity is not None:
            try:
                current = destination.stat(follow_symlinks=False)
                if (
                    not destination.is_symlink()
                    and not bool(is_junction(destination))
                    and (current.st_dev, current.st_ino) == created_identity
                ):
                    destination.unlink()
            except OSError:
                pass


def _atomic_replace_regular_file(
    destination: Path, payload: bytes, *, label: str
) -> None:
    """Replace a normal file atomically while rejecting links and lost updates."""

    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    parent = destination.parent
    temporary: Path | None = None
    try:
        if parent.is_symlink() or bool(is_junction(parent)) or not parent.is_dir():
            raise OSError(f"{label} parent must be a real directory")
        parent_before = parent.stat(follow_symlinks=False)
        if os.path.lexists(destination):
            if destination.is_symlink() or bool(is_junction(destination)):
                raise OSError(f"{label} target must not be a link or junction")
            _existing_bytes, full_fingerprint = _stable_file_snapshot(destination)
            existing: tuple[object, ...] | None = (
                full_fingerprint[0],
                full_fingerprint[2],
            )
        else:
            existing = None
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if existing is None:
            if os.path.lexists(destination):
                raise OSError(f"{label} target appeared during staging")
        else:
            _current_bytes, full_fingerprint = _stable_file_snapshot(destination)
            current_existing = (full_fingerprint[0], full_fingerprint[2])
            if current_existing != existing:
                raise OSError(f"{label} target changed during staging")
        parent_after = parent.stat(follow_symlinks=False)
        if (parent_after.st_dev, parent_after.st_ino) != (
            parent_before.st_dev,
            parent_before.st_ino,
        ):
            raise OSError(f"{label} parent changed during staging")
        os.replace(temporary, destination)
        temporary = None
        written, _fingerprint = _stable_file_snapshot(destination)
        if written != payload:
            raise OSError(f"{label} bytes changed during replacement")
    except OSError as exc:
        raise SystemExit(f"{label} cannot be replaced safely: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def batch(ops: list[dict]) -> None:
    if not ops:
        return
    with tempfile.TemporaryDirectory(prefix="office-batch-", dir=QA) as scratch:
        try:
            live_before = _stable_file_fingerprint(BOOK)
        except OSError as exc:
            raise SystemExit(f"live workbook cannot be staged safely: {exc}") from exc
        scratch_path = Path(scratch)
        path = scratch_path / "commands.json"
        staged_book = scratch_path / BOOK.name
        shutil.copy2(BOOK, staged_book)
        for start in range(0, len(ops), 40):
            path.write_text(json.dumps(ops[start:start + 40], ensure_ascii=False), encoding="utf-8")
            office("batch", staged_book, "--input", path, "--stop-on-error", "--json")
        before_replace = getattr(ops, "before_replace", None)
        if before_replace is not None:
            before_replace()
        # All chunks must succeed before the live workbook changes.  os.replace
        # is atomic on the workbook's filesystem, so a later chunk failure can
        # never leave a status cell committed without its evidence cells.
        try:
            live_after = _stable_file_fingerprint(BOOK)
        except OSError as exc:
            raise SystemExit(f"live workbook changed while staged: {exc}") from exc
        if live_after != live_before:
            raise SystemExit("live workbook changed while staged; concurrent edit preserved")
        os.replace(staged_book, BOOK)


class _GuardedOperations(list[dict]):
    def __init__(self, ops: list[dict], before_replace: Callable[[], None]) -> None:
        super().__init__(ops)
        self.before_replace = before_replace


def _guarded_batch(ops: list[dict], before_replace: Callable[[], None]) -> None:
    """Run a normal batch with a caller-specific final proof/CAS gate."""

    batch(_GuardedOperations(ops, before_replace))


def _require_safe_output_parent(output: Path, *, label: str) -> None:
    """Translate the shared lexical ancestor check into a CLI failure."""

    try:
        validate_output_parent(ROOT, output)
    except (EvidenceManifestError, OSError, ValueError) as exc:
        raise SystemExit(f"{label} output parent is unsafe: {exc}") from exc


def setcell(cell: str, **props: str) -> dict:
    return {"command": "set", "path": f"/{SHEET}/{cell}", "props": props}


def initialize(*, format_only: bool = False) -> None:
    if BOOK.exists() and not format_only:
        raise SystemExit("Workbook already exists; use sync. Existing status edits are preserved.")
    rows = catalog()
    end = len(rows) + 6
    if not format_only:
        office("create", BOOK)
        office("set", BOOK, "/Sheet1", "--prop", f"name={SHEET}")
    print(office("get", BOOK, f"/{SHEET}", "--depth", "0"))
    payload = io.StringIO(newline="")
    writer = csv.writer(payload, delimiter="\t", lineterminator="\n")
    writer.writerow(HEADERS)
    for row in rows:
        values = {**row, "検証状態": "未実装" if row["実装状態"] == "未実装" else "未実施",
                  "今回の証跡": "", "確認日時": ""}
        writer.writerow([values.get(key, "") for key in HEADERS])
    if not format_only:
        with tempfile.TemporaryDirectory(prefix="office-import-", dir=QA) as scratch:
            source = Path(scratch) / "stories.tsv"
            source.write_text(payload.getvalue(), encoding="utf-8-sig")
            office("import", BOOK, f"/{SHEET}", "--file", source, "--format", "tsv", "--start-cell", "A6")
    ops = [
        setcell(f"A1:O{end}", **{"font.name": "Calibri", "font.size": "11", "wrapText": "true", "valign": "top"}),
        setcell("A1", value="LinguaBridge 全機能・ユーザーストーリー検証台帳", merge="A1:D1", **{"font.size": "18", "font.bold": "true"}),
        setcell("A3", value="合格＝その行の合格条件を検証済み。部分確認＝自動テストの補助証拠のみ。実機・品質・長時間試験は別途必要。", merge="A3:O3"),
        setcell("A4", value="F列で未実施／不合格を抽出 → I列の手順を実行 → M列に証跡 → F・N・O列を更新。自動同期前にはバックアップを作成。", merge="A4:O4"),
        setcell("A6:O6", fill="17365D", **{"font.color": "FFFFFF", "font.bold": "true"}),
        {"command": "set", "path": f"/{SHEET}/row[1]", "props": {"height": "34"}},
        {"command": "set", "path": f"/{SHEET}/row[3]", "props": {"height": "24"}},
        {"command": "set", "path": f"/{SHEET}/row[4]", "props": {"height": "24"}},
        {"command": "set", "path": f"/{SHEET}/row[6]", "props": {"height": "34"}},
        {"command": "set", "path": f"/{SHEET}", "props": {"freeze": "C7", "autoFilter": f"A6:O{end}", "orientation": "landscape", "fitToPage": "1x0", "printTitleRows": "1:6"}},
        {"command": "add", "parent": f"/{SHEET}", "type": "validation", "props": {"sqref": f"F7:F{end}", "type": "list", "formula1": ",".join(STATES), "showError": "true", "errorTitle": "検証状態", "error": "一覧から選択してください"}},
        {"command": "add", "parent": f"/{SHEET}", "type": "validation", "props": {"sqref": f"E7:E{end}", "type": "list", "formula1": "実装あり,一部実装,未実装,要確認", "showError": "true"}},
    ]
    for col, width in zip("ABCDEFGHIJKLMNO", [14, 18, 48, 74, 15, 15, 17, 35, 55, 24, 58, 62, 65, 62, 24]):
        ops.append({"command": "set", "path": f"/{SHEET}/col[{col}]", "props": {"width": str(width)}})
    for number in range(7, end + 1):
        ops.append({"command": "set", "path": f"/{SHEET}/row[{number}]", "props": {"height": "100"}})
        if number % 2 == 0:
            ops.append(setcell(f"A{number}:O{number}", fill="F2F6FA"))
    for cell, label, formula in [("E1", "全件", f"COUNTA(A7:A{end})"), ("G1", "合格", f'COUNTIF(F7:F{end},"合格")'), ("I1", "不合格", f'COUNTIF(F7:F{end},"不合格")'), ("K1", "未実施", f'COUNTIF(F7:F{end},"未実施")'), ("M1", "部分確認", f'COUNTIF(F7:F{end},"部分確認")'), ("E2", "実行不可", f'COUNTIF(F7:F{end},"実行不可")'), ("G2", "未実装", f'COUNTIF(F7:F{end},"未実装")')]:
        ops.append(setcell(cell, value=label, **{"font.bold": "true"}))
        ops.append(setcell(chr(ord(cell[0]) + 1) + cell[1], formula=formula))
    batch(ops)
    office("save", BOOK)
    print(f"Created {BOOK}: {len(rows)} stories, one worksheet")
    check()


def read_cells() -> dict[str, str]:
    with ZipFile(BOOK) as archive:
        wb = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = wb.findall("s:sheets/s:sheet", NS)
        assert len(sheets) == 1 and sheets[0].get("name") == SHEET
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(item.itertext()) for item in ET.fromstring(archive.read("xl/sharedStrings.xml"))]
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        result = {}
        for cell in sheet.findall(".//s:sheetData/s:row/s:c", NS):
            value = cell.findtext("s:v", "", NS)
            if cell.get("t") == "s":
                value = shared[int(value)]
            elif cell.get("t") == "inlineStr":
                value = "".join(cell.find("s:is", NS).itertext())
            result[cell.get("r")] = value
        return result


def _story_row_map(cells: dict[str, str]) -> dict[str, int]:
    pairs = [
        (value, int(key[1:]))
        for key, value in cells.items()
        if re.fullmatch(r"A\d+", key) and int(key[1:]) >= 7 and value
    ]
    rows = dict(pairs)
    if len(rows) != len(pairs):
        raise SystemExit("台帳のstory IDが重複しているため同期しません。")
    return rows


def _assert_workbook_targets_current(
    snapshot: dict[str, str],
    ops: list[dict],
    *,
    watched_cells: set[str] | None = None,
) -> None:
    """Fail when a write target or proof-decision input changed."""
    watched_cells = watched_cells or set()
    if not ops and not watched_cells:
        return
    snapshot_rows = _story_row_map(snapshot)
    story_by_row = {row: story for story, row in snapshot_rows.items()}
    targets: set[tuple[str, str, int]] = set()
    for operation in ops:
        if operation.get("command") != "set":
            continue
        match = re.fullmatch(
            rf"/{re.escape(SHEET)}/([A-Z]+)(\d+)",
            str(operation.get("path", "")),
        )
        if match is None:
            continue
        column, row_text = match.groups()
        row = int(row_text)
        story = story_by_row.get(row)
        if story is None:
            raise SystemExit("台帳の対象行をstory IDへ対応付けられないため同期しません。")
        targets.add((story, column, row))
    for cell in watched_cells:
        match = re.fullmatch(r"([A-Z]+)(\d+)", cell)
        if match is None:
            raise ValueError(f"invalid watched workbook cell: {cell}")
        column, row_text = match.groups()
        row = int(row_text)
        story = story_by_row.get(row)
        if story is None:
            raise SystemExit("台帳の監視行をstory IDへ対応付けられないため同期しません。")
        targets.add((story, column, row))

    current = read_cells()
    current_rows = _story_row_map(current)
    moved = sorted(
        story
        for story, _column, row in targets
        if current_rows.get(story) != row
    )
    changed = sorted(
        f"{column}{row}"
        for story, column, row in targets
        if current_rows.get(story) == row
        and current.get(f"{column}{row}", "")
        != snapshot.get(f"{column}{row}", "")
    )
    if moved or changed:
        raise SystemExit(
            "台帳が同期中に変更されたため書き込みません。"
            f" moved_story_ids={moved}, changed_cells={changed}"
        )


def _assert_audit_read_set_current(
    snapshot: dict[str, str], read_set: set[str]
) -> None:
    """Fail if any workbook cell used to make an audit decision changed."""
    snapshot_rows = _story_row_map(snapshot)
    current = read_cells()
    current_rows = _story_row_map(current)
    audited_stories = {
        snapshot[cell]
        for cell in read_set
        if re.fullmatch(r"A\d+", cell) and snapshot.get(cell)
    }
    moved = sorted(
        story
        for story in audited_stories
        if current_rows.get(story) != snapshot_rows.get(story)
    )
    changed = sorted(
        cell
        for cell in read_set
        if current.get(cell, "") != snapshot.get(cell, "")
    )
    if moved or changed:
        raise SystemExit(
            "台帳が同期中に変更されたため書き込みません。"
            f" moved_story_ids={moved}, changed_cells={changed}"
        )


def compact_evidence(value: str) -> str:
    """Keep the current proof links in-cell; immutable run folders hold history."""
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    latest_pytest: tuple[int, str] | None = None
    browser_passes: list[tuple[int, str]] = []
    latest_browser_proofs: dict[str, tuple[int, str]] = {}
    latest_browser_failure: tuple[int, str] | None = None
    latest_soak: tuple[int, str] | None = None
    latest_scale: tuple[int, str] | None = None
    latest_pending_soak: tuple[int, str] | None = None
    latest_field_proofs: dict[str, tuple[int, str]] = {}
    other: list[tuple[int, str]] = []
    seen_other: set[str] = set()
    for index, line in enumerate(lines):
        if line.startswith("pytest:"):
            latest_pytest = (index, line)
        elif "/browser.json #" in line:
            outcomes = re.findall(r"=([a-z_]+)(?=,|$)", line)
            if outcomes and all(outcome == "pass" for outcome in outcomes):
                browser_passes.append((index, line))
                browser_passes = browser_passes[-2:]
                match = re.search(r"/browser\.json #([^;\s]+);\s*(.*)$", line)
                if match:
                    story, result_text = match.groups()
                    scenarios = {
                        name
                        for name, status in re.findall(
                            r"(?:^|,\s*)([^=,;\s]+)=([a-z_]+)", result_text
                        )
                        if status == "pass"
                    }
                    required = AUDITED_BROWSER.get(story)
                    if story in AUDITED_COMPOSITE:
                        required = AUDITED_COMPOSITE[story]["browser"]
                    if required and required <= scenarios:
                        # Supplemental green runs must not push the latest full
                        # browser/native proof out of the bounded cell history.
                        latest_browser_proofs[story] = (index, line)
            else:
                latest_browser_failure = (index, line)
        elif "story-evidence.json #PERF-04;" in line:
            latest_scale = (index, line)
        elif "-soak/story-evidence.json #" in line:
            latest_soak = (index, line)
        elif line.startswith("field: ") and "/field-story-evidence.json #" in line:
            match = re.search(r"/field-story-evidence\.json #([^;\s]+);", line)
            if match and match.group(1) in AUDITED_FIELD:
                latest_field_proofs[match.group(1)] = (index, line)
            elif line not in seen_other:
                other.append((index, line))
                seen_other.add(line)
        elif "試験実行中:" in line and "-soak" in line:
            latest_pending_soak = (index, line)
        elif line not in seen_other:
            other.append((index, line))
            seen_other.add(line)
    selected = [*other, *browser_passes]
    selected.extend(
        item for item in latest_browser_proofs.values() if item not in selected
    )
    selected.extend(
        item for item in latest_field_proofs.values() if item not in selected
    )
    for item in (latest_browser_failure, latest_pytest, latest_soak, latest_scale):
        if item is not None:
            selected.append(item)
    if latest_soak is None and latest_pending_soak is not None:
        selected.append(latest_pending_soak)
    return "\n".join(line for _, line in sorted(selected))


def merged_evidence(existing: str, new_line: str) -> str:
    return compact_evidence("\n".join(part for part in (existing, new_line) if part))


def check() -> VerifiedWorkbookState:
    validate_audit_configuration()
    _require_safe_output_parent(
        QA / "runs" / ".check-ancestry-probe",
        label="tracker check",
    )
    end = len(catalog()) + 6
    cells_before = read_cells()
    compact_ops = []
    compaction_dependencies: set[ProofDependency] = set()
    compaction_watched_cells: set[str] = set()
    for number in range(7, end + 1):
        cell = f"M{number}"
        original = cells_before.get(cell, "")
        compacted = compact_evidence(original)
        if compacted != original:
            dependencies: set[ProofDependency] = set()
            if cells_before.get(f"F{number}") == "合格":
                story = cells_before.get(f"A{number}", "")
                dependencies = _proof_dependencies_for_story(
                    story, compacted, row=number
                )
                if dependencies and not _proof_dependencies_are_current(
                    dependencies
                ):
                    # Retain the full evidence text when structural compaction
                    # would discard the only still-current proof witness.
                    continue
            compact_ops.append(setcell(cell, value=compacted))
            compaction_dependencies.update(dependencies)
            compaction_watched_cells.update({f"F{number}", cell})
    if compact_ops:
        def compaction_guard() -> None:
            dependency_cache = _require_current_proof_dependencies(
                compaction_dependencies,
                checkpoint="evidence compaction batch",
            )
            _assert_workbook_targets_current(
                cells_before,
                compact_ops,
                watched_cells=compaction_watched_cells,
            )
            if not _recheck_audit_proof_cache_witnesses(dependency_cache):
                raise SystemExit(
                    "proof dependency changed during evidence compaction "
                    "workbook CAS"
                )

        compaction_guard()
        _guarded_batch(compact_ops, compaction_guard)
        office("save", BOOK)
        cells_before = read_cells()
    widths = [14, 18, 48, 74, 15, 15, 17, 35, 55, 24, 58, 62, 65, 62, 24]
    heights = []
    for number in range(7, end + 1):
        line_count = 1
        for col, width in zip("ABCDEFGHIJKLMNO", widths):
            lines = cells_before.get(f"{col}{number}", "").splitlines() or [""]
            wrapped = sum(max(1, math.ceil(sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in line) / (width - 5))) for line in lines)
            line_count = max(line_count, wrapped)
        height = max(100, math.ceil(line_count * 17 + 12))
        assert height <= 409, f"Row {number}: evidence too long; shorten the summary, keep the linked evidence"
        heights.append({"command": "set", "path": f"/{SHEET}/row[{number}]", "props": {"height": str(height)}})
    batch(heights)
    # OfficeCLI invalidates COUNTIF caches when status cells change. Re-enter
    # formulas through OfficeCLI to calculate and persist every derived value.
    formulas = {"F1": f"COUNTA(A7:A{end})"}
    for status, cell in [("合格", "H1"), ("不合格", "J1"), ("未実施", "L1"), ("部分確認", "N1"), ("実行不可", "F2"), ("未実装", "H2")]:
        formulas[cell] = f'COUNTIF(F7:F{end},"{status}")'
    batch([setcell(cell, formula=formula) for cell, formula in formulas.items()])
    print(office("validate", BOOK))
    issues = office("view", BOOK, "issues")
    print(issues)
    assert "Found 0 issue(s)" in issues, issues
    for error in ["#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A"]:
        result = office("query", BOOK, f'cell:contains("{error}")', "--json")
        # Keep the evidence without printing a full sheet.
        print(f"Formula error scan {error}: {result.strip()[:180]}")
        assert json.loads(result)["data"]["matches"] == 0, result
    office("save", BOOK)
    cells = read_cells()
    actual = {v for k, v in cells.items() if re.fullmatch(r"A\d+", k) and re.fullmatch(r"[A-Z]+-\d+", v)}
    assert actual == {r["ID"] for r in catalog()}, "Story coverage differs from catalog"
    by_id = {v: int(k[1:]) for k, v in cells.items() if re.fullmatch(r"A\d+", k)}
    catalog_columns = {
        "B": "領域",
        "C": "ユーザーストーリー",
        "D": "期待する挙動・合格条件",
        "E": "実装状態",
        "G": "検証方式",
        "H": "前提",
        "I": "操作・テスト手順",
        "J": "要件",
        "K": "実装根拠",
        "L": "自動テスト候補",
    }
    metadata_errors = []
    for row in catalog():
        number = by_id[row["ID"]]
        for column, field in catalog_columns.items():
            if cells.get(f"{column}{number}", "") != row[field]:
                metadata_errors.append((row["ID"], field))
    assert not metadata_errors, f"Workbook metadata differs from catalog: {metadata_errors}"
    assert int(cells["F1"]) == len(actual), "Summary count mismatch"
    for status, cell in [("合格", "H1"), ("不合格", "J1"), ("未実施", "L1"), ("部分確認", "N1"), ("実行不可", "F2"), ("未実装", "H2")]:
        count = sum(v == status for key, v in cells.items() if re.fullmatch(r"F\d+", key) and int(key[1:]) >= 7)
        assert int(cells[cell]) == count, (cell, count, cells[cell])
    # Bind the preview and successful return to a workbook whose every pass is
    # supported by a fresh proof audit.  The matching final check below spans
    # HTML rendering/writing, closing the post-batch validation window.
    verified_workbook = _capture_verified_workbook_state()
    preview = office("view", BOOK, "html")
    _atomic_replace_regular_file(
        QA / "feature-status.html",
        preview.encode("utf-8"),
        label="feature-status HTML preview",
    )
    class Cells(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_cell = False
            self.values = []
        def handle_starttag(self, tag, attrs):
            if tag == "td":
                self.in_cell = True
        def handle_endtag(self, tag):
            if tag == "td":
                self.in_cell = False
        def handle_data(self, data):
            if self.in_cell:
                self.values.append(data)
    rendered = Cells()
    rendered.feed(preview)
    for value in rendered.values:
        assert not any(token in value for token in ["###", "<TODO>", "{var}", "$fy$24"]), value
    _require_verified_workbook_state_current(
        verified_workbook, checkpoint="check completion"
    )
    print(f"HTML preview checked: {len(rendered.values)} text fragments; {QA / 'feature-status.html'}")
    print("Workbook IDs and all summary caches verified")
    return verified_workbook


def hash_files(files: list[Path]) -> dict[str, str]:
    return {
        str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
        if path.is_file()
    }


def reviewed_tree_files(root: Path) -> list[Path]:
    """Return reviewed entries while surfacing links to the secure hasher."""

    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    if root.is_symlink() or bool(is_junction(root)) or not root.is_dir():
        return [root]
    return [
        path
        for path in root.rglob("*")
        if not any(
            part.startswith(".") or part == "__pycache__"
            for part in path.relative_to(root).parts
        )
        and (path.is_file() or path.is_symlink() or bool(is_junction(path)))
    ]


def qa_source_files() -> list[Path]:
    """Return reviewed QA sources, excluding hidden per-run scratch trees."""

    return reviewed_tree_files(ROOT / "qa_checks")


def pytest_source_files() -> list[Path]:
    return [
        *reviewed_tree_files(ROOT / "server"),
        *reviewed_tree_files(ROOT / "web"),
        *reviewed_tree_files(ROOT / "tests"),
        *qa_source_files(),
        *reviewed_tree_files(ROOT / "scripts"),
        ROOT / "README.md",
        ROOT / "config.yaml",
        ROOT / "pyproject.toml",
        ROOT / "requirements.txt",
        ROOT / "requirements-bench.txt",
        ROOT / "requirements-dev.txt",
        ROOT / "setup.ps1",
        ROOT / "start.bat",
        CATALOG,
    ]


def browser_source_files(*, native: bool) -> list[Path]:
    files = [
        *reviewed_tree_files(ROOT / "web"),
        ROOT / "scripts/qa_browser.py",
        ROOT / "scripts/qa_evidence_manifest.py",
        ROOT / "scripts/story_tracker.py",
        CATALOG,
    ]
    if native:
        is_junction = getattr(os.path, "isjunction", lambda _path: False)
        files += [
            *(
                path
                for path in reviewed_tree_files(ROOT / "server")
                if path == ROOT / "server"
                or path.suffix == ".py"
                or path.is_symlink()
                or bool(is_junction(path))
            ),
            ROOT / "scripts/qa_native_browser.py",
            ROOT / "tests/conftest.py",
        ]
    return files


def _browser_source_supplier(native: bool) -> Callable[[], list[Path]]:
    """Give source snapshot code a precisely typed, zero-argument supplier."""

    def supply() -> list[Path]:
        return browser_source_files(native=native)

    return supply


def _cached_json(path: Path, cache: AuditProofCache | None) -> object:
    """Read and fingerprint one JSON file once within an audit pass."""
    absolute = Path(os.path.abspath(path))
    if cache is not None and absolute in cache.json_payloads:
        return cache.json_payloads[absolute]
    payload, witness = _read_bound_repository_file(
        absolute, root=_binding_root_for_path(absolute)
    )
    decoded = json.loads(payload)
    if cache is not None:
        cache.file_fingerprints[absolute] = witness.sha256
        cache.file_witnesses[absolute] = witness
        cache.json_payloads[absolute] = decoded
    return decoded


def _remember_file_fingerprint(
    path: Path,
    cache: AuditProofCache | None,
    *,
    digest: str | None = None,
) -> None:
    if cache is None:
        return
    absolute = Path(os.path.abspath(path))
    _content, witness = _read_bound_repository_file(
        absolute, root=_binding_root_for_path(absolute)
    )
    actual = witness.sha256
    if digest is not None and digest != actual:
        raise EvidenceManifestError(
            f"cached evidence digest does not match stable bytes: {absolute}"
        )
    cached = cache.file_fingerprints.get(absolute)
    if cached is not None and cached != actual:
        raise EvidenceManifestError(
            f"cached evidence changed within one audit pass: {absolute}"
        )
    cache.file_fingerprints[absolute] = actual
    cache.file_witnesses[absolute] = witness


def _binding_root_for_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current_root = Path(os.path.abspath(ROOT))
    if absolute.is_relative_to(current_root):
        return current_root
    if absolute.is_relative_to(_IMPORT_ROOT):
        return _IMPORT_ROOT
    raise OSError(f"validated file is outside the repository root: {path}")


def _cached_source_snapshot(
    cache: AuditProofCache | None,
    key: tuple[str, bool],
    file_supplier: Callable[[], list[Path]],
) -> SourceHashSnapshot:
    if cache is None:
        return _stable_supplied_source_snapshot(file_supplier)
    if key not in cache.source_snapshots:
        cache.source_snapshots[key] = _stable_supplied_source_snapshot(
            file_supplier
        )
    return cache.source_snapshots[key]


def _stable_supplied_source_snapshot(
    file_supplier: Callable[[], list[Path]],
) -> SourceHashSnapshot:
    """Use the shared whole-set snapshot/revision CAS for every consumer."""

    return snapshot_supplied_source_hashes(ROOT, file_supplier)


def _require_current_sealed_sources(
    recorded: object,
    file_supplier: Callable[[], list[Path]],
    *,
    message: str,
) -> SourceHashSnapshot:
    """Compare an envelope snapshot with a fresh, link-safe source snapshot."""

    try:
        current = _stable_supplied_source_snapshot(file_supplier)
    except (EvidenceManifestError, OSError, ValueError) as exc:
        raise SystemExit(f"{message}: {exc}") from exc
    if recorded != current:
        raise SystemExit(message)
    return current


def _remember_sealed_browser_fingerprints(
    run: Path,
    sealed: SealedBrowserEvidence,
    cache: AuditProofCache | None,
) -> None:
    if cache is None:
        return
    _remember_file_fingerprint(
        run / "source-hashes.json", cache, digest=sealed.manifest_sha256
    )
    _remember_file_fingerprint(
        run / "browser.json",
        cache,
        digest=hashlib.sha256(sealed.browser_bytes).hexdigest(),
    )


def _remember_sealed_pytest_fingerprints(
    run: Path,
    sealed: SealedPytestEvidence,
    cache: AuditProofCache | None,
) -> None:
    if cache is None:
        return
    _remember_file_fingerprint(
        run / "source-hashes.json", cache, digest=sealed.manifest_sha256
    )
    values = {
        "pytest.xml": sealed.pytest_bytes,
        "pytest.log": sealed.log_bytes,
        "run.json": sealed.run_bytes,
    }
    if sealed.story_evidence_bytes is not None:
        values["story-evidence.json"] = sealed.story_evidence_bytes
    for name, value in values.items():
        _remember_file_fingerprint(
            run / name,
            cache,
            digest=hashlib.sha256(value).hexdigest(),
        )


def _cached_sealed_browser_evidence(
    run: Path,
    cache: AuditProofCache | None,
) -> SealedBrowserEvidence:
    absolute = Path(os.path.abspath(run))
    if cache is not None and absolute in cache.browser_runs:
        return cache.browser_runs[absolute]
    sealed = load_sealed_browser_evidence(absolute)
    _remember_sealed_browser_fingerprints(absolute, sealed, cache)
    if cache is not None:
        cache.browser_runs[absolute] = sealed
    return sealed


def _cached_sealed_pytest_evidence(
    run: Path,
    cache: AuditProofCache | None,
) -> SealedPytestEvidence:
    absolute = Path(os.path.abspath(run))
    if cache is not None and absolute in cache.pytest_runs:
        return cache.pytest_runs[absolute]
    sealed = load_sealed_pytest_evidence(absolute, expected_complete=True)
    _remember_sealed_pytest_fingerprints(absolute, sealed, cache)
    if cache is not None:
        cache.pytest_runs[absolute] = sealed
    return sealed


PYTEST_RUN_SCHEMA = 2
PYTEST_COLLECTION_ENV = "LINGUABRIDGE_PYTEST_COLLECTION_FILE"


def pytest_collection_finish(session) -> None:
    """Pytest plugin hook: publish the exact collected item set for this run.

    ``collect_run`` loads this module only in an isolated collect-only process.
    The actual test process never receives the private output path, so test code
    cannot rewrite the collection witness that the parent already consumed.
    """

    destination = os.environ.get(PYTEST_COLLECTION_ENV)
    if not destination:
        return
    path = Path(destination)
    payload = json.dumps(
        {
            "schema_version": 1,
            "complete": session.testsfailed == 0,
            "nodeids": [item.nodeid.replace("\\", "/") for item in session.items],
        },
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ).encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _validated_pytest_args(value: object) -> list[str]:
    """Allow only positional scopes and the non-selecting timeout option."""

    if not isinstance(value, list):
        raise ValueError("run.json.pytest_args must be a string array")
    result: list[str] = []
    for argument in value:
        if not isinstance(argument, str) or not argument or argument != argument.strip():
            raise ValueError("pytest arguments must be non-empty canonical strings")
        if re.fullmatch(r"--timeout=(?:\d+(?:\.\d+)?)", argument):
            if float(argument.split("=", 1)[1]) <= 0:
                raise ValueError("pytest timeout must be positive")
            result.append(argument)
            continue
        if argument.startswith(("-", "@")):
            raise ValueError(
                "only positional pytest scopes and --timeout=<seconds> are allowed"
            )
        if (
            "\\" in argument
            or "\x00" in argument
            or "\r" in argument
            or "\n" in argument
            or re.match(r"^[A-Za-z]:", argument)
        ):
            raise ValueError("pytest scope must be a canonical repository-relative selector")
        path_part = argument.split("::", 1)[0]
        parts = path_part.split("/")
        if not path_part or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("pytest scope must be a canonical repository-relative selector")
        result.append(argument)
    return result


def _pytest_node_matches_selector(nodeid: str, selector: str) -> bool:
    selector_path, separator, _ = selector.partition("::")
    node_path = nodeid.split("::", 1)[0]
    if separator:
        return (
            nodeid == selector
            or nodeid.startswith(selector + "::")
            or nodeid.startswith(selector + "[")
        )
    if selector_path.endswith(".py"):
        return node_path == selector_path
    return node_path == selector_path or node_path.startswith(selector_path + "/")


def _pytest_scope_covers_selector(pytest_args: list[str], selector: str) -> bool:
    scopes = [argument for argument in pytest_args if not argument.startswith("--")]
    if not scopes:
        return True
    selector_path = selector.split("::", 1)[0]
    for scope in scopes:
        scope_path, separator, _ = scope.partition("::")
        if separator:
            if scope_path != selector_path:
                continue
            if (
                selector == scope
                or selector.startswith(scope + "::")
                or selector.startswith(scope + "[")
            ):
                return True
            continue
        if scope_path.endswith(".py"):
            if scope_path == selector_path:
                return True
        elif selector_path == scope_path or selector_path.startswith(scope_path + "/"):
            return True
    return False


def _pytest_story_selection(
    selectors: list[str] | set[str],
    cases: list[dict],
    meta: dict,
) -> tuple[bool, list[dict], list[str]]:
    """Bind one story to complete collection and JUnit execution evidence."""

    pytest_args = meta["pytest_args"]
    if not all(
        _pytest_scope_covers_selector(pytest_args, selector)
        for selector in selectors
    ):
        selected = [
            case
            for case in cases
            if any(
                _pytest_node_matches_selector(case["nodeid"], selector)
                for selector in selectors
            )
        ]
        return False, selected, list(selectors)
    collected = meta["collected_nodeids"]
    case_by_nodeid = {case["nodeid"]: case for case in cases}
    missing: list[str] = []
    for selector in selectors:
        descendants = {
            nodeid
            for nodeid in collected
            if _pytest_node_matches_selector(nodeid, selector)
        }
        actual = {
            nodeid
            for nodeid in case_by_nodeid
            if _pytest_node_matches_selector(nodeid, selector)
        }
        if not descendants or descendants != actual:
            missing.append(selector)
    selected = [
        case
        for case in cases
        if any(
            _pytest_node_matches_selector(case["nodeid"], selector)
            for selector in selectors
        )
    ]
    return True, selected, missing


def collect_run(pytest_args: list[str]) -> Path:
    _require_import_code_current(checkpoint="pytest collection")
    try:
        pytest_args = _validated_pytest_args(pytest_args)
    except ValueError as exc:
        raise SystemExit(f"unsafe pytest arguments: {exc}") from exc
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    out = QA / "runs" / stamp
    _require_safe_output_parent(out, label="pytest run")
    out.mkdir(parents=True, exist_ok=False)
    source_file_supplier = lambda: list(pytest_source_files())
    source_hashes_at_start = _stable_supplied_source_snapshot(
        source_file_supplier
    )
    with tempfile.TemporaryDirectory(prefix=".pytest-producer-", dir=out) as raw:
        raw_dir = Path(raw)
        raw_xml = raw_dir / "pytest.xml"
        raw_log = raw_dir / "pytest.log"
        raw_collection = raw_dir / "collected-nodeids.json"
        collection_pycache = raw_dir / "collection-pycache"
        execution_pycache = raw_dir / "execution-pycache"
        collection_pycache.mkdir()
        execution_pycache.mkdir()
        fixed_command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-ra",
            "-p",
            "no:cacheprovider",
            "-p",
            "pytest_timeout",
            "-o",
            "addopts=",
            "-c",
            str(ROOT / "pyproject.toml"),
        ]
        collection_command = [
            *fixed_command,
            "-p",
            "scripts.story_tracker",
            "--collect-only",
            *pytest_args,
        ]
        command = [
            *fixed_command,
            f"--junitxml={raw_xml}",
            f"--basetemp={out / 'tmp'}",
            *pytest_args,
        ]
        pytest_env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "PYTEST_ADDOPTS",
                "PYTEST_PLUGINS",
                "PYTHONDONTWRITEBYTECODE",
                "PYTHONPYCACHEPREFIX",
                PYTEST_COLLECTION_ENV,
            }
        }
        pytest_env.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        collection_env = {
            **pytest_env,
            PYTEST_COLLECTION_ENV: str(raw_collection),
            "PYTHONPYCACHEPREFIX": str(collection_pycache),
        }
        execution_env = {
            **pytest_env,
            "PYTHONPYCACHEPREFIX": str(execution_pycache),
        }
        with raw_log.open("w", encoding="utf-8") as log:
            collection_result = subprocess.run(
                collection_command,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=ROOT,
                env=collection_env,
            )
            collection_complete = False
            collected_nodeids: list[str] = []
            try:
                collection = json.loads(raw_collection.read_bytes())
                collection_complete = (
                    collection_result.returncode == 0
                    and isinstance(collection, dict)
                    and collection.get("schema_version") == 1
                    and collection.get("complete") is True
                    and isinstance(collection.get("nodeids"), list)
                    and all(
                        isinstance(nodeid, str) and nodeid
                        for nodeid in collection["nodeids"]
                    )
                    and len(collection["nodeids"])
                    == len(set(collection["nodeids"]))
                )
                if collection_complete:
                    collected_nodeids = collection["nodeids"]
            except (OSError, ValueError, TypeError):
                collection_complete = False
            finally:
                raw_collection.unlink(missing_ok=True)
            log.write("\n--- pytest execution ---\n")
            log.flush()
            if collection_complete:
                result = subprocess.run(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=ROOT,
                    env=execution_env,
                )
            else:
                log.write("collection failed; pytest execution was not started\n")
                result = subprocess.CompletedProcess(command, 2)
        meta = {
            "schema_version": PYTEST_RUN_SCHEMA,
            "command": command,
            "collection_command": collection_command,
            "collection_exit_code": collection_result.returncode,
            "pytest_args": pytest_args,
            "collected_nodeids": collected_nodeids,
            "collection_complete": collection_complete,
            "exit_code": result.returncode if collection_complete else 2,
            "finished_at": datetime.now().astimezone().isoformat(),
            "python": sys.version,
        }
        pytest_bytes = raw_xml.read_bytes() if raw_xml.is_file() else b""
        log_bytes = raw_log.read_bytes()
        run_bytes = json.dumps(
            meta, indent=2, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        published = publish_pytest_evidence(
            out,
            root=ROOT,
            # Re-enumerate so added/removed reviewed files also invalidate the
            # run, not only content changes to the starting path list.
            source_files=source_file_supplier,
            source_hashes_at_start=source_hashes_at_start,
            pytest_bytes=pytest_bytes,
            log_bytes=log_bytes,
            run_bytes=run_bytes,
        )
        print(log_bytes.decode("utf-8", errors="replace")[-6000:])
    if not published:
        print(
            "テスト中に検証対象sourceが変化したため、このrunはsealed evidenceとして採用できません。"
        )
    print(f"Evidence: {out}")
    return out


def junit_cases(source: Path | bytes) -> list[dict]:
    result = []
    root = ET.fromstring(source) if isinstance(source, bytes) else ET.parse(source).getroot()
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        parts = classname.split(".")
        boundary = next((i for i, part in enumerate(parts) if part.startswith("test_")), len(parts) - 1)
        node = "/".join(parts[:boundary + 1]) + ".py"
        node += "::" + "::".join([*parts[boundary + 1:], case.get("name", "")])
        status = "pass"
        detail = ""
        for tag, value in [("skipped", "skip"), ("failure", "fail"), ("error", "error")]:
            child = case.find(tag)
            if child is not None:
                status, detail = value, child.get("message", "")
        result.append({"nodeid": node, "status": status, "detail": detail})
    return result


def _validated_junit_cases(source: Path | bytes) -> list[dict]:
    cases = junit_cases(source)
    if any(
        not isinstance(case, dict)
        or not isinstance(case.get("nodeid"), str)
        or not case["nodeid"]
        or case.get("status") not in {"pass", "fail", "error", "skip"}
        for case in cases
    ):
        raise ValueError("pytest.xml contains an invalid testcase")
    nodeids = [case["nodeid"] for case in cases]
    if len(nodeids) != len(set(nodeids)):
        raise ValueError("pytest.xml contains duplicate testcase nodeids")
    return cases


def _validated_aware_timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be an aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be an aware ISO-8601 timestamp")
    return value


def _validated_pytest_run_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("run.json root must be an object")
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != PYTEST_RUN_SCHEMA:
        raise ValueError(f"run.json.schema_version must equal {PYTEST_RUN_SCHEMA}")
    command = payload.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(value, str) or not value for value in command)
    ):
        raise ValueError("run.json.command must be a non-empty string array")
    if type(payload.get("exit_code")) is not int:
        raise ValueError("run.json.exit_code must be an integer")
    pytest_args = _validated_pytest_args(payload.get("pytest_args"))
    fixed = [
        "-m",
        "pytest",
        "-q",
        "-ra",
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_timeout",
        "-o",
        "addopts=",
        "-c",
        str(ROOT / "pyproject.toml"),
    ]
    command_tail = command[1:]
    if command_tail[: len(fixed)] != fixed:
        raise ValueError("run.json.command does not use the fixed pytest harness")
    remaining = command_tail[len(fixed) :]
    if (
        len(remaining) != 2 + len(pytest_args)
        or not remaining[0].startswith("--junitxml=")
        or not remaining[0].split("=", 1)[1]
        or not remaining[1].startswith("--basetemp=")
        or not remaining[1].split("=", 1)[1]
        or remaining[2:] != pytest_args
    ):
        raise ValueError("run.json.command does not bind the sealed pytest arguments")
    if pytest_args and command[-len(pytest_args):] != pytest_args:
        raise ValueError("run.json.pytest_args must match the command suffix")
    collection_command = payload.get("collection_command")
    if (
        not isinstance(collection_command, list)
        or any(
            not isinstance(value, str) or not value
            for value in collection_command
        )
        or collection_command
        != [
            command[0],
            *fixed,
            "-p",
            "scripts.story_tracker",
            "--collect-only",
            *pytest_args,
        ]
    ):
        raise ValueError("run.json.collection_command is not the fixed collection harness")
    if (
        type(payload.get("collection_exit_code")) is not int
        or payload["collection_exit_code"] != 0
    ):
        raise ValueError("run.json.collection_exit_code must equal zero")
    collected = payload.get("collected_nodeids")
    if (
        not isinstance(collected, list)
        or any(not isinstance(nodeid, str) or not nodeid for nodeid in collected)
        or len(collected) != len(set(collected))
    ):
        raise ValueError("run.json.collected_nodeids must be a unique string array")
    if payload.get("collection_complete") is not True:
        raise ValueError("run.json.collection_complete must be true")
    _validated_aware_timestamp(
        payload.get("finished_at"), field="run.json.finished_at"
    )
    if not isinstance(payload.get("python"), str) or not payload["python"]:
        raise ValueError("run.json.python must be a non-empty string")
    return payload


def _validated_browser_results(payload: object, *, require_nonempty: bool) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("browser.json root must be an object")
    _validated_aware_timestamp(
        payload.get("finished_at"), field="browser.json.finished_at"
    )
    results = payload.get("results")
    if not isinstance(results, list) or (require_nonempty and not results):
        raise ValueError("browser.json.results must be a non-empty array")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("story"), str)
        or not item["story"]
        or not isinstance(item.get("scenario"), str)
        or not item["scenario"]
        or item.get("status") not in {"pass", "fail"}
        for item in results
    ):
        raise ValueError("browser.json.results contains an invalid result")
    environment_skips = payload.get("environment_skips", [])
    if not isinstance(environment_skips, list) or any(
        not isinstance(item, dict) for item in environment_skips
    ):
        raise ValueError("browser.json.environment_skips must be an object array")
    return results


def current_browser_proof(
    story: str,
    evidence: str,
    *,
    required: set[str] | None = None,
    include_harness: bool = True,
    cache: AuditProofCache | None = None,
) -> bool:
    """Find a current green browser proof in a complete sealed run."""
    if not _import_code_is_current():
        return False
    required = required if required is not None else AUDITED_BROWSER.get(story)
    if not required:
        return False
    required_native = any(name.startswith("native_") for name in required)
    paths = re.findall(r"docs/qa/runs/[^\s;]+/browser\.json", evidence)
    for relative in reversed(paths):
        path = _safe_qa_run_reference(relative, "browser.json")
        if path is None:
            continue
        try:
            sealed = _cached_sealed_browser_evidence(path.parent, cache)
            kind = sealed.manifest["kind"]
            native = kind == "native-browser"
            # The trusted envelope, not scenario names inside browser.json,
            # selects the source scope.  A browser seal therefore cannot be
            # relabelled with native_* scenarios to omit server-side sources.
            if native is not required_native:
                continue
            current_state = _cached_source_snapshot(
                cache,
                ("browser", native),
                lambda: browser_source_files(native=native),
            )
            if sealed.manifest["source_hashes"] != current_state:
                continue
            hashes = sealed.manifest["source_hashes"]
            required_harness = {
                "scripts/story_tracker.py",
                "scripts/qa_browser.py",
                "scripts/qa_evidence_manifest.py",
            }
            if native:
                required_harness |= {
                    "scripts/qa_native_browser.py",
                    "tests/conftest.py",
                }
            if not required_harness <= hashes.keys() or "web/student.js" not in hashes:
                continue
            if native and "server/pipeline.py" not in hashes:
                continue
            results = _validated_browser_results(
                sealed.browser_payload, require_nonempty=True
            )
            selected = [
                item
                for item in results
                if isinstance(item, dict) and item.get("story") == story
            ]
            if not required <= {
                item.get("scenario")
                for item in selected
                if isinstance(item.get("scenario"), str)
            }:
                continue
            if any(item.get("status") != "pass" for item in selected):
                continue
            return True
        except (EvidenceManifestError, OSError, ValueError, KeyError, TypeError):
            continue
    return False


def current_pytest_proof(
    story: str,
    evidence: str,
    *,
    cache: AuditProofCache | None = None,
) -> bool:
    """Find a current green proof reconstructed from sealed JUnit bytes."""
    if not _import_code_is_current():
        return False
    required = AUDITED_COMPOSITE.get(story, {}).get("pytest")
    catalog_digest: str | None = None
    if not required and story in AUDITED_AUTOMATIC:
        try:
            catalog_rows, catalog_digest = _proof_catalog_snapshot(cache)
        except (OSError, ValueError, KeyError, TypeError):
            return False
        row = catalog_rows.get(story)
        required = {value for value in row["自動テスト候補"].split(";") if value} if row else None
    if not required:
        return False
    current_state = None
    paths = re.findall(r"docs/qa/runs/[^\s;]+/story-evidence\.json", evidence)
    for relative in reversed(paths):
        path = _safe_qa_run_reference(relative, "story-evidence.json")
        if path is None:
            continue
        try:
            sealed = _cached_sealed_pytest_evidence(path.parent, cache)
            meta = _validated_pytest_run_payload(sealed.run_payload)
            if (
                sealed.story_evidence_payload is None
                or story not in sealed.story_evidence_payload
                or not isinstance(sealed.story_evidence_payload[story], dict)
            ):
                continue
            if current_state is None:
                current_state = _cached_source_snapshot(
                    cache, ("pytest", False), pytest_source_files
                )
            if sealed.manifest["source_hashes"] != current_state:
                continue
            if catalog_digest is not None and (
                sealed.manifest["source_hashes"].get("docs/qa/stories.tsv")
                != catalog_digest
                or current_state.get("docs/qa/stories.tsv") != catalog_digest
            ):
                continue
            cases = _validated_junit_cases(sealed.pytest_bytes)
            scope_complete, selected, missing = _pytest_story_selection(
                required, cases, meta
            )
            if not scope_complete or missing:
                continue
            if any(case["status"] != "pass" for case in selected):
                continue
            hashes = sealed.manifest["source_hashes"]
            required_hashes = {
                "config.yaml",
                "server/main.py",
                "scripts/story_tracker.py",
                *(selector.split("::", 1)[0] for selector in required),
            }
            if not required_hashes <= hashes.keys():
                continue
            return True
        except (
            EvidenceManifestError,
            ET.ParseError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ):
            continue
    return False


def current_performance_proof(
    story: str,
    evidence: str,
    *,
    cache: AuditProofCache | None = None,
) -> bool:
    """Re-audit a saved performance report against its current source manifest.

    ``story-evidence.json`` is only an index written by the tracker, not trusted
    measurement output.  Resolve its report inside the same immutable run, have
    the manifest validator recompute every source/input/artifact hash, then run
    today's qualification rules over the sealed report bytes.
    """
    if not _import_code_is_current():
        return False
    kind = AUDITED_PERFORMANCE.get(story)
    if kind is None:
        return False
    # The performance manifest intentionally permits the current auditor code
    # to differ from the code recorded by an old measurement.  Bind the exact
    # auditor bytes used by this process so an edit during re-evaluation cannot
    # be ignored by that compatibility rule.
    for audit_source in (
        Path(__file__),
        Path(performance_evidence_module.__file__),
    ):
        _remember_file_fingerprint(audit_source, cache)
    paths = re.findall(r"docs/qa/runs/[^\s;]+/story-evidence\.json", evidence)
    for relative in reversed(paths):
        evidence_path = _safe_qa_run_reference(
            relative, "story-evidence.json"
        )
        if evidence_path is None:
            continue
        cache_key: tuple[Path, str, Path] | None = None
        try:
            index = _cached_json(evidence_path, cache)
            if not isinstance(index, dict):
                continue
            report_reference = index["source_report"]
            if not isinstance(report_reference, str):
                continue
            report_name = Path(report_reference).name
            report_path = _safe_qa_run_reference(
                report_reference, report_name
            )
            if report_path is None:
                continue
            if report_path.parent != evidence_path.parent:
                continue
            cache_key = (evidence_path.parent, kind, report_path)
            if cache is not None and cache_key in cache.performance_results:
                if story in cache.performance_results[cache_key]:
                    return True
                continue
            _remember_file_fingerprint(
                evidence_path.parent / PERFORMANCE_MANIFEST_NAME, cache
            )
            _remember_file_fingerprint(report_path, cache)
            manifest = validate_performance_manifest(
                evidence_path.parent,
                kind=kind,
                primary_report=report_path,
                root=ROOT,
            )
            report = _load_sealed_performance_json(report_path, manifest)
            candidates = {
                candidate
                for candidate, candidate_kind in AUDITED_PERFORMANCE.items()
                if candidate_kind == kind
            }
            passing: set[str] = set()
            if kind == "soak":
                audited_windows = _audit_soak_report(report, manifest)
                if audited_windows:
                    passing = {
                        candidate
                        for candidate in candidates
                        if all(
                            item["qualified"].get(candidate) is True
                            and item["evaluations"].get(candidate) is True
                            for item in audited_windows
                        )
                    }
            elif kind == "scale":
                audited_scale = _audit_scale_report(report, manifest)
                if (
                    audited_scale["qualified"] is True
                    and audited_scale["passed"] is True
                ):
                    passing = candidates
            if cache is not None:
                cache.performance_results[cache_key] = frozenset(passing)
                validation_witness = getattr(
                    manifest, "validation_witness", None
                )
                if (
                    validation_witness is None
                    and validate_performance_manifest
                    is _STRICT_PERFORMANCE_VALIDATOR
                ):
                    raise PerformanceEvidenceError(
                        "performance validation witness is missing"
                    )
                cache.performance_states[cache_key] = PerformanceProofState(
                    semantic=_performance_semantic(manifest),
                    validation_witness=validation_witness,
                )
            if story in passing:
                return True
        except (OSError, ValueError, KeyError, TypeError, SystemExit):
            if cache is not None and cache_key is not None:
                cache.performance_results[cache_key] = frozenset()
            continue
    return False


def _safe_repo_regular_reference(
    relative: str, *, filename: str | None = None
) -> Path | None:
    """Return one lexical repository file without traversal or linked parents."""

    if (
        not isinstance(relative, str)
        or "\\" in relative
        or "\x00" in relative
        or "\r" in relative
        or "\n" in relative
    ):
        return None
    parts = relative.split("/")
    if (
        any(part in {"", ".", ".."} for part in parts)
        or (filename is not None and parts[-1] != filename)
    ):
        return None
    path = Path(os.path.abspath(ROOT.joinpath(*parts)))
    try:
        validate_output_parent(Path(os.path.abspath(ROOT)), path)
        _capture_code_binding(path, root=ROOT)
    except (OSError, ValueError):
        return None
    return path


def _safe_qa_run_reference(relative: str, filename: str) -> Path | None:
    """Return one lexical QA receipt without accepting traversal or links."""

    path = _safe_repo_regular_reference(relative, filename=filename)
    if path is None or not path.is_relative_to(
        Path(os.path.abspath(QA / "runs"))
    ):
        return None
    return path


def _field_candidate_tuple(value: object) -> tuple[str, ...] | None:
    """Return a nonempty, unique subset in canonical FIELD_STORIES order."""
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(story, str) for story in value):
        return None
    candidates = tuple(value)
    candidate_set = set(candidates)
    canonical = tuple(story for story in FIELD_STORIES if story in candidate_set)
    if candidates != canonical:
        return None
    return candidates


def _field_import_fingerprint(
    validation: dict, candidates: tuple[str, ...]
) -> tuple[Path, str, Path, str, tuple[str, ...]]:
    """Bind a review decision to exact bundle, manifest, and candidate state."""
    return (
        Path(os.path.abspath(validation["bundle_path"])),
        validation["bundle_sha256"],
        Path(os.path.abspath(validation["manifest_path"])),
        validation["manifest_sha256"],
        candidates,
    )


def _empty_field_proof_snapshot() -> FieldProofSnapshot:
    return FieldProofSnapshot(frozenset(), "", None, None)


def _decode_strict_json_bytes(value: bytes, *, label: str) -> object:
    def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate key {key!r}")
            result[key] = item
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"{label} contains non-finite number {value}")

    try:
        return json.loads(
            value.decode("utf-8", errors="strict"),
            object_pairs_hook=strict_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not strict UTF-8 JSON") from exc


def _validated_field_receipt_snapshot(relative: str) -> FieldProofSnapshot:
    receipt_path = _safe_qa_run_reference(relative, "field-story-evidence.json")
    if receipt_path is None:
        return _empty_field_proof_snapshot()
    try:
        receipt_bytes, receipt_witness = _read_bound_repository_file(
            receipt_path, root=ROOT
        )
        receipt_sha = hashlib.sha256(receipt_bytes).hexdigest()
        receipt = _decode_strict_json_bytes(
            receipt_bytes, label="field import receipt"
        )
        expected_keys = {
            "schema_version",
            "kind",
            "imported_at",
            "reviewer",
            "review_confirmation",
            "bundle",
            "bundle_sha256",
            "manifest",
            "manifest_sha256",
            "stories",
        }
        if not isinstance(receipt, dict) or set(receipt) != expected_keys:
            return _empty_field_proof_snapshot()
        if (
            type(receipt.get("schema_version")) is not int
            or receipt.get("schema_version") != FIELD_IMPORT_SCHEMA
            or receipt.get("kind") != "field-story-import"
            or receipt.get("review_confirmation")
            != "reviewer inspected every referenced artifact before approval"
            or not isinstance(receipt.get("reviewer"), str)
            or not receipt["reviewer"]
            or receipt["reviewer"] != receipt["reviewer"].strip()
            or len(receipt["reviewer"]) > 100
            or not receipt["reviewer"].isprintable()
            or ";" in receipt["reviewer"]
        ):
            return _empty_field_proof_snapshot()
        imported_at = datetime.fromisoformat(str(receipt.get("imported_at")))
        if imported_at.tzinfo is None or imported_at.utcoffset() is None:
            return _empty_field_proof_snapshot()
        bundle_relative = receipt.get("bundle")
        if not isinstance(bundle_relative, str):
            return _empty_field_proof_snapshot()
        bundle_path = _safe_repo_regular_reference(bundle_relative)
        if bundle_path is None:
            return _empty_field_proof_snapshot()
        receipt_manifest_relative = receipt.get("manifest")
        if not isinstance(receipt_manifest_relative, str):
            return _empty_field_proof_snapshot()
        receipt_manifest_path = _safe_repo_regular_reference(
            receipt_manifest_relative
        )
        if receipt_manifest_path is None:
            return _empty_field_proof_snapshot()
        validation = validate_field_bundle(bundle_path.parent, root=ROOT)
        validation_witness = getattr(validation, "validation_witness", None)
        if (
            validation_witness is None
            and validate_field_bundle is _STRICT_FIELD_VALIDATOR
        ):
            return _empty_field_proof_snapshot()
        candidates = _field_candidate_tuple(
            validation.get("approved_candidates")
        )
        if candidates is None:
            return _empty_field_proof_snapshot()
        manifest_path = Path(os.path.abspath(validation["manifest_path"]))
        manifest_relative = manifest_path.relative_to(
            Path(os.path.abspath(ROOT))
        ).as_posix()
        if (
            Path(os.path.abspath(validation["bundle_path"])) != bundle_path
            or manifest_path != receipt_manifest_path
            or Path(os.path.abspath(validation["bundle_path"])).relative_to(
                Path(os.path.abspath(ROOT))
            ).as_posix()
            != bundle_relative
            or validation["bundle_sha256"] != receipt.get("bundle_sha256")
            or manifest_relative != receipt_manifest_relative
            or validation["manifest_sha256"] != receipt.get("manifest_sha256")
        ):
            return _empty_field_proof_snapshot()
        stories = receipt.get("stories")
        if not isinstance(stories, dict) or tuple(stories) != candidates:
            return _empty_field_proof_snapshot()
        approved = set()
        for story, decision in stories.items():
            if (
                not isinstance(decision, dict)
                or set(decision) != {"candidate", "review_decision", "reason"}
                or decision.get("candidate") != "可"
                or decision.get("review_decision") != "合格"
                or not isinstance(decision.get("reason"), str)
                or not decision["reason"].strip()
            ):
                return _empty_field_proof_snapshot()
            approved.add(story)
        if approved != set(candidates):
            return _empty_field_proof_snapshot()
        _recheck_code_binding_revision(receipt_witness)
        if validation_witness is not None:
            field_story_evidence_module.recheck_validation_witness(
                validation_witness
            )
        return FieldProofSnapshot(
            frozenset(approved),
            receipt_sha,
            receipt_witness,
            validation_witness,
        )
    except (
        AttributeError,
        FieldEvidenceError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ):
        return _empty_field_proof_snapshot()


def _approved_field_stories_from_receipt(relative: str) -> set[str]:
    return set(_validated_field_receipt_snapshot(relative).approved)


def _field_proof_snapshot(relative: str) -> FieldProofSnapshot:
    """Bind an approval result to the exact receipt bytes just re-audited."""
    return _validated_field_receipt_snapshot(relative)


def _field_proof_cache_is_current(cache: FieldProofCache) -> bool:
    """Re-audit every cached receipt immediately before workbook mutation."""
    latest: dict[str, FieldProofSnapshot] = {}
    for relative, snapshot in tuple(cache.items()):
        current = _field_proof_snapshot(relative)
        if current != snapshot:
            return False
        latest[relative] = current
    if not _recheck_field_snapshots(list(latest.values())):
        return False
    # Keep the witnesses returned by the last full validation.  Commit guards
    # can now perform one final revision-only fence after the workbook CAS.
    cache.update(latest)
    return True


def _recheck_field_snapshots(
    snapshots: list[FieldProofSnapshot] | tuple[FieldProofSnapshot, ...],
) -> bool:
    try:
        for current in snapshots:
            if current.receipt_witness is not None:
                _recheck_code_binding_revision(current.receipt_witness)
            if current.validation_witness is not None:
                field_story_evidence_module.recheck_validation_witness(
                    current.validation_witness
                )
        return True
    except (AttributeError, FieldEvidenceError, OSError, ValueError, TypeError):
        return False


def _recheck_field_cache_witnesses(cache: FieldProofCache) -> bool:
    return _recheck_field_snapshots(list(cache.values()))


def current_field_proof(
    story: str,
    evidence: str,
    *,
    cache: FieldProofCache | None = None,
) -> bool:
    """Re-audit a reviewed field receipt and its sealed source/evidence bundle."""
    if not _import_code_is_current():
        return False
    if story not in AUDITED_FIELD:
        return False
    references = re.findall(
        r"(?m)^field: (docs/qa/runs/[^\s;]+/field-story-evidence\.json) "
        r"#([^;\s]+);(?: [^\r\n]*)?$",
        evidence,
    )
    local_cache = cache if cache is not None else {}
    for relative, referenced_story in reversed(references):
        if referenced_story != story:
            continue
        if relative not in local_cache:
            local_cache[relative] = _field_proof_snapshot(relative)
        if story in local_cache[relative].approved:
            return True
    return False


def _proof_dependency_is_current(
    dependency: ProofDependency, *, cache: AuditProofCache | None = None
) -> bool:
    """Re-run exactly one recorded proof without an audit cache."""

    if dependency.kind == "browser":
        required = (
            set(dependency.required)
            if dependency.required is not None
            else None
        )
        return current_browser_proof(
            dependency.story,
            dependency.evidence,
            required=required,
            include_harness=True,
            cache=cache,
        )
    if dependency.kind == "pytest":
        return current_pytest_proof(
            dependency.story, dependency.evidence, cache=cache
        )
    if dependency.kind == "performance":
        return current_performance_proof(
            dependency.story, dependency.evidence, cache=cache
        )
    if dependency.kind == "field":
        return current_field_proof(
            dependency.story,
            dependency.evidence,
            cache=cache.field if cache is not None else None,
        )
    raise ValueError(f"unsupported proof dependency kind: {dependency.kind}")


def _proof_dependency_workbook_cells(
    dependencies: set[ProofDependency],
) -> set[str]:
    """Return workbook inputs that selected the captured proof references."""

    return {
        f"{column}{dependency.row}"
        for dependency in dependencies
        for column in ("F", "M")
    }


def _field_proof_workbook_cells(cells: dict[str, str]) -> set[str]:
    """Return inputs for existing field passes re-audited during this sync."""

    rows = _story_row_map(cells)
    return {
        f"{column}{number}"
        for story in AUDITED_FIELD
        if (number := rows.get(story)) is not None
        and cells.get(f"F{number}") == "合格"
        for column in ("F", "M")
    }


def _nonfield_proof_workbook_cells(cells: dict[str, str]) -> set[str]:
    """Return F/M inputs for every existing audited non-field pass."""

    audited = (
        set(AUDITED_AUTOMATIC)
        | set(AUDITED_BROWSER)
        | set(AUDITED_COMPOSITE)
        | set(AUDITED_PERFORMANCE)
    )
    rows = _story_row_map(cells)
    return {
        f"{column}{number}"
        for story in audited
        if (number := rows.get(story)) is not None
        and cells.get(f"F{number}") == "合格"
        for column in ("F", "M")
    }


def _proof_dependencies_for_story(
    story: str, evidence: str, *, row: int
) -> set[ProofDependency]:
    """Describe the complete proof predicate for one audited story."""

    if story in AUDITED_FIELD:
        return {
            ProofDependency(
                kind="field", story=story, evidence=evidence, row=row
            )
        }
    if story in AUDITED_PERFORMANCE:
        return {
            ProofDependency(
                kind="performance", story=story, evidence=evidence, row=row
            )
        }
    if story in AUDITED_AUTOMATIC:
        return {
            ProofDependency(
                kind="pytest", story=story, evidence=evidence, row=row
            )
        }
    if story in AUDITED_BROWSER:
        return {
            ProofDependency(
                kind="browser", story=story, evidence=evidence, row=row
            )
        }
    if story in AUDITED_COMPOSITE:
        return {
            ProofDependency(
                kind="pytest", story=story, evidence=evidence, row=row
            ),
            ProofDependency(
                kind="browser",
                story=story,
                evidence=evidence,
                row=row,
                required=frozenset(AUDITED_COMPOSITE[story]["browser"]),
            ),
        }
    return set()


def _proof_dependencies_with_evidence(
    dependencies: set[ProofDependency], evidence: str
) -> set[ProofDependency]:
    return {
        ProofDependency(
            kind=dependency.kind,
            story=dependency.story,
            evidence=evidence,
            row=dependency.row,
            required=dependency.required,
        )
        for dependency in dependencies
    }


def _proof_dependencies_are_current(
    dependencies: set[ProofDependency],
) -> bool:
    if not dependencies:
        return True
    try:
        return all(
            _proof_dependency_is_current(dependency)
            for dependency in dependencies
        )
    except (EvidenceManifestError, OSError, ValueError, KeyError, TypeError):
        return False


def _proof_preserving_merged_evidence(
    existing: str,
    new_line: str,
    dependencies: set[ProofDependency],
) -> str:
    """Compact a planned pass only when its complete proof remains resolvable."""

    combined = "\n".join(part for part in (existing, new_line) if part)
    compacted = compact_evidence(combined)
    compacted_dependencies = _proof_dependencies_with_evidence(
        dependencies, compacted
    )
    if compacted_dependencies and not _proof_dependencies_are_current(
        compacted_dependencies
    ):
        return combined
    return compacted


def _require_current_proof_dependencies(
    dependencies: set[ProofDependency], *, checkpoint: str
) -> AuditProofCache:
    """Fail closed when any proof used to plan a pass is no longer current."""

    cache = AuditProofCache()
    for dependency in sorted(
        dependencies,
        key=lambda item: (
            item.kind,
            item.story,
            item.row,
            item.evidence,
            tuple(sorted(item.required or ())),
        ),
    ):
        try:
            current = _proof_dependency_is_current(dependency, cache=cache)
        except (EvidenceManifestError, OSError, ValueError, KeyError, TypeError) as exc:
            raise SystemExit(
                "proof dependency could not be revalidated before "
                f"{checkpoint}: {dependency.kind} {dependency.story}: {exc}"
            ) from exc
        if not current:
            raise SystemExit(
                "proof dependency is no longer current before "
                f"{checkpoint}: {dependency.kind} {dependency.story}"
            )
    # Predicates above may involve several runs and source trees.  Recheck the
    # revisions captured by the shared cache after the last predicate so an
    # early dependency cannot change during a later, slower validation.
    if not _audit_proof_cache_is_current(cache):
        raise SystemExit(
            f"proof dependency state changed during {checkpoint}"
        )
    return cache


def _planned_status(ops: list[dict], number: int) -> str | None:
    """Return the last status one pending batch would write for a story row."""
    target = f"/{SHEET}/F{number}"
    for operation in reversed(ops):
        if operation.get("command") != "set" or operation.get("path") != target:
            continue
        props = operation.get("props")
        if isinstance(props, dict) and isinstance(props.get("value"), str):
            return props["value"]
    return None


def _planned_cell_value(
    cells: dict[str, str], ops: list[dict], column: str, number: int
) -> str:
    target = f"/{SHEET}/{column}{number}"
    for operation in reversed(ops):
        if operation.get("command") != "set" or operation.get("path") != target:
            continue
        props = operation.get("props")
        if isinstance(props, dict) and isinstance(props.get("value"), str):
            return props["value"]
    return cells.get(f"{column}{number}", "")


def _all_planned_pass_dependencies(
    cells: dict[str, str],
    ops: list[dict],
    *,
    exclude_stories: set[str] | frozenset[str] = frozenset(),
) -> set[ProofDependency]:
    """Describe every audited pass that will exist after ``ops`` commits."""

    dependencies: set[ProofDependency] = set()
    for story, number in _story_row_map(cells).items():
        if story in exclude_stories:
            continue
        if _planned_cell_value(cells, ops, "F", number) != "合格":
            continue
        dependencies.update(
            _proof_dependencies_for_story(
                story,
                _planned_cell_value(cells, ops, "M", number),
                row=number,
            )
        )
    return dependencies


def _reaudit_all_field_passes(
    cells: dict[str, str], ops: list[dict], cache: FieldProofCache
) -> list[str]:
    """Keep only current field passes, including rows untouched by this run.

    A full pytest or browser run does not necessarily contain a selector for
    every physical-device story (OPS-12 has none). Re-audit all existing field
    passes before any workbook mutation so a stale receipt cannot survive merely
    because the supplemental run did not mention that story. A newly observed
    explicit failure always wins.
    """
    rows = _story_row_map(cells)
    downgraded: list[str] = []
    stale_note = "現地証拠を現行source/manifestで再監査できないため部分確認。"
    for story in sorted(AUDITED_FIELD):
        number = rows.get(story)
        if number is None or cells.get(f"F{number}") != "合格":
            continue
        planned = _planned_status(ops, number)
        if planned == "不合格":
            continue
        if planned == "合格":
            continue
        current = current_field_proof(
            story, cells.get(f"M{number}", ""), cache=cache
        )
        if current:
            if planned not in {None, "合格"}:
                ops.append(setcell(f"F{number}", value="合格"))
            continue
        remaining = cells.get(f"N{number}", "").strip()
        if stale_note not in remaining.splitlines():
            remaining = "\n".join(part for part in (remaining, stale_note) if part)
        ops.extend(
            [
                setcell(f"F{number}", value="部分確認"),
                setcell(f"N{number}", value=remaining),
            ]
        )
        downgraded.append(story)
    return downgraded


def _reaudit_all_nonfield_passes(
    cells: dict[str, str],
    ops: list[dict],
    cache: AuditProofCache,
    dependencies: set[ProofDependency],
) -> list[str]:
    """Re-audit existing non-field passes not decided by the current run."""

    audited = sorted(
        set(AUDITED_AUTOMATIC)
        | set(AUDITED_BROWSER)
        | set(AUDITED_COMPOSITE)
        | set(AUDITED_PERFORMANCE)
    )
    rows = _story_row_map(cells)
    downgraded: list[str] = []
    stale_note = "既存の自動証拠を現行source/manifestで再監査できないため部分確認。"
    for story in audited:
        number = rows.get(story)
        if number is None or cells.get(f"F{number}") != "合格":
            continue
        if any(
            dependency.story == story and dependency.row == number
            for dependency in dependencies
        ):
            # The current run already evaluated and recorded the exact proof
            # that preserves this row, even when preservation needs no write.
            continue
        # A row explicitly decided by this run (including a new failure or a
        # freshly sealed pass) is authoritative.  Only untouched/preserved
        # rows need the prior-proof audit below.
        if _planned_status(ops, number) is not None:
            continue
        evidence = cells.get(f"M{number}", "")
        if story in AUDITED_AUTOMATIC:
            current = current_pytest_proof(story, evidence, cache=cache)
        elif story in AUDITED_BROWSER:
            current = current_browser_proof(story, evidence, cache=cache)
        elif story in AUDITED_COMPOSITE:
            current = current_pytest_proof(
                story, evidence, cache=cache
            ) and current_browser_proof(
                story,
                evidence,
                required=AUDITED_COMPOSITE[story]["browser"],
                include_harness=True,
                cache=cache,
            )
        else:
            current = current_performance_proof(story, evidence, cache=cache)
        if current:
            dependencies.update(
                _proof_dependencies_for_story(story, evidence, row=number)
            )
            continue
        remaining = cells.get(f"N{number}", "").strip()
        if stale_note not in remaining.splitlines():
            remaining = "\n".join(
                part for part in (remaining, stale_note) if part
            )
        ops.extend(
            [
                setcell(f"F{number}", value="部分確認"),
                setcell(f"N{number}", value=remaining),
            ]
        )
        downgraded.append(story)
    return downgraded


def sync_field(run: Path, *, reviewer: str, approve_candidates: bool) -> Path:
    """Import one sealed nonempty candidate subset after explicit review."""
    _require_import_code_current(checkpoint="field sync")
    reviewer = reviewer.strip()
    if (
        not approve_candidates
        or not reviewer
        or len(reviewer) > 100
        or not reviewer.isprintable()
        or ";" in reviewer
    ):
        raise SystemExit(
            "現地証拠の取込には --reviewer と --approve-field-candidates が必要です。"
            "承認前にsidecarが参照する全artifactを目視レビューしてください。"
        )
    try:
        initial = validate_field_bundle(run, root=ROOT)
    except (FieldEvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"field bundleを再監査できないため台帳を変更しません: {exc}") from exc
    candidates = _field_candidate_tuple(initial.get("approved_candidates"))
    if candidates is None:
        raise SystemExit(
            "field approved_candidatesが空、未知、重複、または順序不正のため"
            "台帳を変更しません。"
        )
    initial_fingerprint = _field_import_fingerprint(initial, candidates)
    validate_audit_configuration()
    cells = read_cells()
    by_id = {
        value: int(key[1:])
        for key, value in cells.items()
        if re.fullmatch(r"A\d+", key)
    }
    if not set(candidates) <= set(by_id):
        raise SystemExit("台帳にfield候補storyが揃っていません。")
    try:
        final = validate_field_bundle(run, root=ROOT)
    except (FieldEvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"field bundleが台帳更新直前に変化しました: {exc}") from exc
    final_candidates = _field_candidate_tuple(final.get("approved_candidates"))
    if (
        final_candidates is None
        or _field_import_fingerprint(final, final_candidates)
        != initial_fingerprint
    ):
        raise SystemExit(
            "field bundle/manifest/candidatesがレビュー中に変化したため"
            "台帳を変更しません。"
        )

    imported_at = datetime.now().astimezone().isoformat()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f-field-import")
    receipt_dir = QA / "runs" / stamp
    _require_safe_output_parent(receipt_dir, label="field import receipt")
    receipt_dir.mkdir(parents=True, exist_ok=False)
    receipt_path = receipt_dir / "field-story-evidence.json"
    bundle_relative = final["bundle_path"].relative_to(ROOT).as_posix()
    manifest_relative = final["manifest_path"].relative_to(ROOT).as_posix()
    receipt = {
        "schema_version": FIELD_IMPORT_SCHEMA,
        "kind": "field-story-import",
        "imported_at": imported_at,
        "reviewer": reviewer,
        "review_confirmation": "reviewer inspected every referenced artifact before approval",
        "bundle": bundle_relative,
        "bundle_sha256": final["bundle_sha256"],
        "manifest": manifest_relative,
        "manifest_sha256": final["manifest_sha256"],
        "stories": {
            story: {
                "candidate": "可",
                "review_decision": "合格",
                "reason": "候補条件と全参照証拠をレビューし、合格条件を満たすと承認",
            }
            for story in candidates
        },
    }
    receipt_bytes = (
        json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    _exclusive_regular_file_write(
        receipt_path, receipt_bytes, label="field import receipt"
    )
    reference = receipt_path.relative_to(ROOT).as_posix()
    expected_receipt_snapshot = _field_proof_snapshot(reference)
    if expected_receipt_snapshot.approved != frozenset(candidates):
        raise SystemExit("new field receipt could not be revalidated")
    _exclusive_workbook_backup(
        receipt_dir / "feature-status-before-sync.xlsx"
    )
    captured_at = final["payload"]["captured_at"]
    ops = []
    for story in candidates:
        number = by_id[story]
        existing_status = cells.get(f"F{number}", "")
        status = "不合格" if existing_status == "不合格" else "合格"
        note = (
            f"field: {reference} #{story}; review=pass; "
            f"bundle_sha256={final['bundle_sha256']}"
        )
        evidence = merged_evidence(cells.get(f"M{number}", ""), note)
        if status == "合格":
            remaining = (
                "現地実機証拠をレビュー済み。機械可読sidecar、全artifact manifest、"
                f"生成時source snapshotを再監査: {reference} #{story}"
            )
        else:
            prior = cells.get(f"N{number}", "").strip()
            suffix = (
                "現地証拠は合格レビュー済みだが、既存の不合格findingは自動解除しない: "
                f"{reference} #{story}"
            )
            remaining = "\n".join(part for part in (prior, suffix) if part)
        ops += [
            setcell(f"F{number}", value=status),
            setcell(f"M{number}", value=evidence),
            setcell(f"N{number}", value=remaining),
            setcell(f"O{number}", value=captured_at),
        ]
    field_proof_cache: FieldProofCache = {}
    nonfield_proof_cache = AuditProofCache()
    proof_dependencies: set[ProofDependency] = set()
    _reaudit_all_nonfield_passes(
        cells, ops, nonfield_proof_cache, proof_dependencies
    )
    _reaudit_all_field_passes(cells, ops, field_proof_cache)
    proof_dependencies.update(
        _all_planned_pass_dependencies(
            cells, ops, exclude_stories=set(candidates)
        )
    )
    watched_cells = (
        _proof_dependency_workbook_cells(proof_dependencies)
        | _nonfield_proof_workbook_cells(cells)
        | _field_proof_workbook_cells(cells)
    )

    def field_commit_guard() -> None:
        current_receipt_snapshot = _field_proof_snapshot(reference)
        if current_receipt_snapshot != expected_receipt_snapshot:
            raise SystemExit(
                "field receipt/source/manifest changed before workbook replace"
            )
        if not _field_proof_cache_is_current(field_proof_cache):
            raise SystemExit("field proof changed before workbook replace")
        dependency_cache = _require_current_proof_dependencies(
            proof_dependencies, checkpoint="field staged workbook replace"
        )
        _assert_workbook_targets_current(
            cells, ops, watched_cells=watched_cells
        )
        _require_final_witness_fence(
            checkpoint="field staged workbook replace",
            proof_caches=(dependency_cache,),
            field_caches=(field_proof_cache,),
            field_snapshots=(current_receipt_snapshot,),
        )

    field_commit_guard()
    _guarded_batch(ops, field_commit_guard)
    office("save", BOOK)
    check()
    print(
        f"Reviewed field evidence imported for {len(candidates)} stories: {receipt_path}"
    )
    return receipt_dir


def sync(run: Path) -> None:
    _require_import_code_current(checkpoint="pytest sync")
    run = Path(os.path.abspath(run))
    _require_safe_output_parent(run / "story-evidence.json", label="pytest sync")
    try:
        sealed = load_sealed_pytest_evidence(run, expected_complete=False)
    except (EvidenceManifestError, OSError, ValueError) as exc:
        raise SystemExit(
            "pytest evidenceの未完了sealを検証できないため台帳を変更しません。"
            f"現行状態で再実行してください: {exc}"
        ) from exc
    _require_current_sealed_sources(
        sealed.manifest["source_hashes"],
        pytest_source_files,
        message=(
            "テスト後に検証対象または証跡ハーネスが変わったため台帳を変更しません。"
            "現行状態で再実行してください。"
        ),
    )
    validate_audit_configuration()
    try:
        cases = _validated_junit_cases(sealed.pytest_bytes)
        meta = _validated_pytest_run_payload(sealed.run_payload)
    except (ET.ParseError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"sealed pytest evidenceが不正なため台帳を変更しません: {exc}") from exc
    cells = read_cells()
    by_id = {value: int(key[1:]) for key, value in cells.items() if re.fullmatch(r"A\d+", key)}
    backup = run / "feature-status-before-sync.xlsx"
    if os.path.lexists(backup):
        raise SystemExit("This run was already synced; preserve its original backup.")
    ops, evidence = [], {}
    field_proof_cache: FieldProofCache = {}
    nonfield_proof_cache = AuditProofCache()
    proof_dependencies: set[ProofDependency] = set()
    for row in catalog():
        selectors = [s for s in row["自動テスト候補"].split(";") if s]
        if not selectors:
            continue
        scope_complete, selected, missing = _pytest_story_selection(
            selectors, cases, meta
        )
        if not selected:
            continue  # A deliberately excluded suite is not an execution failure.
        counts = {state: sum(c["status"] == state for c in selected) for state in ["pass", "fail", "error", "skip"]}
        number = by_id[row["ID"]]
        existing_evidence = cells.get(f"M{number}", "")
        preserve_existing_proof = False
        row_dependencies: set[ProofDependency] = set()
        status = "部分確認"
        if counts["fail"] or counts["error"]:
            status = "不合格"
        elif row["ID"] in AUDITED_COMPOSITE and (missing or counts["skip"]):
            browser_required = AUDITED_COMPOSITE[row["ID"]]["browser"]
            browser_current = False
            pytest_current = False
            if cells.get(f"F{number}") == "合格":
                browser_current = current_browser_proof(
                    row["ID"], existing_evidence,
                    required=browser_required, include_harness=True,
                )
                if browser_current:
                    pytest_current = current_pytest_proof(
                        row["ID"], existing_evidence
                    )
            preserve_existing_proof = browser_current and pytest_current
            if preserve_existing_proof:
                status = "合格"
                row_dependencies.update(
                    {
                        ProofDependency(
                            kind="browser",
                            story=row["ID"],
                            evidence=existing_evidence,
                            row=number,
                            required=frozenset(browser_required),
                        ),
                        ProofDependency(
                            kind="pytest",
                            story=row["ID"],
                            evidence=existing_evidence,
                            row=number,
                        ),
                    }
                )
            elif counts["skip"] == len(selected):
                status = "実行不可"
        elif row["ID"] in AUDITED_AUTOMATIC and (missing or counts["skip"]):
            pytest_current = (
                current_pytest_proof(row["ID"], existing_evidence)
                if cells.get(f"F{number}") == "合格"
                else False
            )
            preserve_existing_proof = pytest_current
            if preserve_existing_proof:
                status = "合格"
                row_dependencies.add(
                    ProofDependency(
                        kind="pytest",
                        story=row["ID"],
                        evidence=existing_evidence,
                        row=number,
                    )
                )
            elif counts["skip"] == len(selected):
                status = "実行不可"
        elif row["ID"] in AUDITED_PERFORMANCE:
            # Pytest selectors exercise the harness, not the duration/matrix
            # acceptance criterion.  A currently valid sealed report is
            # re-audited and may also restore a row conservatively downgraded
            # after audit-only code changed; a prior failure is guarded below.
            current_performance = current_performance_proof(
                row["ID"], existing_evidence
            )
            preserve_existing_proof = (
                cells.get(f"F{number}") == "合格" and current_performance
            )
            if current_performance:
                status = "合格"
                row_dependencies.add(
                    ProofDependency(
                        kind="performance",
                        story=row["ID"],
                        evidence=existing_evidence,
                        row=number,
                    )
                )
            elif counts["skip"] == len(selected):
                status = "実行不可"
        elif row["ID"] in AUDITED_FIELD:
            current_field = current_field_proof(
                row["ID"], existing_evidence, cache=field_proof_cache
            )
            preserve_existing_proof = (
                cells.get(f"F{number}") == "合格" and current_field
            )
            if current_field:
                status = "合格"
                row_dependencies.add(
                    ProofDependency(
                        kind="field",
                        story=row["ID"],
                        evidence=existing_evidence,
                        row=number,
                    )
                )
            elif counts["skip"] == len(selected):
                status = "実行不可"
        elif counts["skip"] == len(selected):
            status = "実行不可"
        # The audit allow-list is the authority for completeness. Some fully
        # automated pytest stories deliberately retain a "実モデル" label to
        # make their local-model prerequisite visible in the sheet.
        elif row["ID"] in AUDITED_AUTOMATIC and not counts["skip"] and not missing:
            status = "合格"
        elif row["ID"] in AUDITED_COMPOSITE:
            browser_required = AUDITED_COMPOSITE[row["ID"]]["browser"]
            if current_browser_proof(
                row["ID"], existing_evidence,
                required=browser_required, include_harness=True,
            ):
                status = "合格"
                row_dependencies.add(
                    ProofDependency(
                        kind="browser",
                        story=row["ID"],
                        evidence=existing_evidence,
                        row=number,
                        required=frozenset(browser_required),
                    )
                )
        # A current, complete prior composite proof survives a green subset run.
        if status == "部分確認" and cells.get(f"F{number}") == "合格":
            prior_browser_evidence = cells.get(f"M{number}", "")
            if current_browser_proof(row["ID"], prior_browser_evidence):
                status = "合格"
                row_dependencies.add(
                    ProofDependency(
                        kind="browser",
                        story=row["ID"],
                        evidence=prior_browser_evidence,
                        row=number,
                    )
                )
        if cells.get(f"F{number}") == "不合格" and status != "不合格":
            status = "不合格"  # A different check cannot resolve an existing finding.
        new_note = f"pytest: pass={counts['pass']}, fail={counts['fail']}, error={counts['error']}, skip={counts['skip']}. "
        new_note += str((run / "story-evidence.json").relative_to(ROOT)).replace("\\", "/") + " #" + row["ID"]
        if missing:
            new_note += "; 未検出selector=" + ";".join(missing)
        if status == "合格" and row_dependencies and not preserve_existing_proof:
            note = _proof_preserving_merged_evidence(
                existing_evidence, new_note, row_dependencies
            )
            row_dependencies = _proof_dependencies_with_evidence(
                row_dependencies, note
            )
        else:
            note = merged_evidence(existing_evidence, new_note)
        if status == "合格":
            proof_dependencies.update(row_dependencies)
        evidence[row["ID"]] = {"status": status, "counts": counts, "missing_selectors": missing, "cases": selected,
                               "scope": row["検証方式"], "remaining": row["残作業・注意"],
                               "workbook_preserved": preserve_existing_proof}
        if not preserve_existing_proof:
            ops += [setcell(f"F{number}", value=status), setcell(f"M{number}", value=note), setcell(f"O{number}", value=meta["finished_at"])]
    _reaudit_all_nonfield_passes(
        cells, ops, nonfield_proof_cache, proof_dependencies
    )
    _reaudit_all_field_passes(cells, ops, field_proof_cache)
    proof_workbook_cells = (
        _proof_dependency_workbook_cells(proof_dependencies)
        | _nonfield_proof_workbook_cells(cells)
        | _field_proof_workbook_cells(cells)
    )
    if not _field_proof_cache_is_current(field_proof_cache):
        raise SystemExit(
            "field receipt/source/manifestが同期中に変化したため台帳を変更しません。"
        )
    story_evidence_bytes = json.dumps(
        evidence,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    try:
        fresh_incomplete = load_sealed_pytest_evidence(
            run, expected_complete=False
        )
    except (EvidenceManifestError, OSError, ValueError) as exc:
        raise SystemExit(
            f"pytest evidenceが同期中に変化したため台帳を変更しません: {exc}"
        ) from exc
    if fresh_incomplete != sealed:
        raise SystemExit(
            "pytest evidence/sourceが同期中に変化したため台帳を変更しません。"
        )
    _require_current_sealed_sources(
        fresh_incomplete.manifest["source_hashes"],
        pytest_source_files,
        message="pytest evidence/sourceが同期中に変化したため台帳を変更しません。",
    )
    _assert_workbook_targets_current(
        cells, ops, watched_cells=proof_workbook_cells
    )
    _require_current_proof_dependencies(
        proof_dependencies, checkpoint="pytest story seal"
    )
    try:
        completed = complete_pytest_evidence(
            run,
            story_evidence_bytes=story_evidence_bytes,
            expected_manifest_sha256=sealed.manifest_sha256,
        )
        final_seal = load_sealed_pytest_evidence(run, expected_complete=True)
    except (EvidenceManifestError, OSError, ValueError) as exc:
        raise SystemExit(
            f"pytest story evidenceを安全にsealできないため台帳を変更しません: {exc}"
        ) from exc
    if final_seal != completed:
        raise SystemExit(
            "completed pytest evidence/sourceが台帳更新直前に変化したため書き込みません。"
        )
    _require_current_sealed_sources(
        final_seal.manifest["source_hashes"],
        pytest_source_files,
        message=(
            "completed pytest evidence/sourceが台帳更新直前に変化したため書き込みません。"
        ),
    )
    # The new story index is not a valid proof until the complete seal exists.
    # Once sealed, include every pass that the pending workbook would contain
    # and widen the F/M CAS read-set before backup, batch, and replace.
    proof_dependencies.update(_all_planned_pass_dependencies(cells, ops))
    proof_workbook_cells = (
        _proof_dependency_workbook_cells(proof_dependencies)
        | _nonfield_proof_workbook_cells(cells)
        | _field_proof_workbook_cells(cells)
    )
    if not _field_proof_cache_is_current(field_proof_cache):
        raise SystemExit(
            "field receipt/source/manifestが台帳更新直前に変化したため書き込みません。"
        )
    _assert_workbook_targets_current(
        cells, ops, watched_cells=proof_workbook_cells
    )
    _require_current_proof_dependencies(
        proof_dependencies, checkpoint="pytest workbook backup"
    )
    _exclusive_workbook_backup(backup)
    try:
        before_batch = load_sealed_pytest_evidence(run, expected_complete=True)
    except (EvidenceManifestError, OSError, ValueError) as exc:
        raise SystemExit(
            f"completed pytest evidenceがbatch直前に変化しました: {exc}"
        ) from exc
    if before_batch != completed:
        raise SystemExit(
            "completed pytest evidence/sourceがbatch直前に変化したため書き込みません。"
        )
    _require_current_sealed_sources(
        before_batch.manifest["source_hashes"],
        pytest_source_files,
        message=(
            "completed pytest evidence/sourceがbatch直前に変化したため書き込みません。"
        ),
    )
    if not _field_proof_cache_is_current(field_proof_cache):
        raise SystemExit(
            "field receipt/source/manifestがbatch直前に変化したため書き込みません。"
        )
    _assert_workbook_targets_current(
        cells, ops, watched_cells=proof_workbook_cells
    )
    _require_current_proof_dependencies(
        proof_dependencies, checkpoint="pytest workbook batch"
    )

    def pytest_commit_guard() -> None:
        try:
            committed = load_sealed_pytest_evidence(
                run, expected_complete=True
            )
        except (EvidenceManifestError, OSError, ValueError) as exc:
            raise SystemExit(
                f"completed pytest evidence changed before workbook replace: {exc}"
            ) from exc
        if committed != completed:
            raise SystemExit(
                "completed pytest evidence changed before workbook replace"
            )
        _require_current_sealed_sources(
            committed.manifest["source_hashes"],
            pytest_source_files,
            message="pytest source changed before workbook replace",
        )
        if not _field_proof_cache_is_current(field_proof_cache):
            raise SystemExit("field proof changed before workbook replace")
        dependency_cache = _require_current_proof_dependencies(
            proof_dependencies, checkpoint="pytest staged workbook replace"
        )
        _assert_workbook_targets_current(
            cells, ops, watched_cells=proof_workbook_cells
        )
        _require_final_witness_fence(
            checkpoint="pytest staged workbook replace",
            proof_caches=(dependency_cache,),
            field_caches=(field_proof_cache,),
        )

    _guarded_batch(ops, pytest_commit_guard)
    office("save", BOOK)
    check()
    print(f"Synced evidence for {len(evidence)} stories. Unmatched/manual rows preserved.")


def sync_browser(run: Path) -> None:
    _require_import_code_current(checkpoint="browser sync")
    run = Path(os.path.abspath(run))
    _require_safe_output_parent(
        run / "feature-status-before-sync.xlsx", label="browser sync"
    )
    try:
        sealed = load_sealed_browser_evidence(run)
    except (EvidenceManifestError, OSError, ValueError) as exc:
        raise SystemExit(
            "browser evidenceのcomplete sealを検証できないため台帳を変更しません。"
            f"現行状態で再実行してください: {exc}"
        ) from exc
    meta = sealed.browser_payload
    try:
        results = _validated_browser_results(meta, require_nonempty=True)
    except ValueError as exc:
        message = str(exc)
        if "non-empty array" in message:
            raise SystemExit(
                "browser.jsonに実行結果がないため台帳を変更しません。"
                "環境skipを解消して再実行してください。"
            ) from exc
        raise SystemExit(
            f"sealed browser.jsonの形式が不正なため台帳を変更しません: {exc}"
        ) from exc
    native = sealed.manifest["kind"] == "native-browser"
    _require_current_sealed_sources(
        sealed.manifest["source_hashes"],
        lambda: browser_source_files(native=native),
        message=(
            "ブラウザ検証後に対象または証跡ハーネスが変わったため台帳を変更しません。"
            "現行状態で再実行してください。"
        ),
    )
    validate_audit_configuration()
    cells = read_cells()
    by_id = {value: int(key[1:]) for key, value in cells.items() if re.fullmatch(r"A\d+", key)}
    backup = run / "feature-status-before-sync.xlsx"
    if os.path.lexists(backup):
        raise SystemExit("This browser run was already synced.")
    ops = []
    field_proof_cache: FieldProofCache = {}
    nonfield_proof_cache = AuditProofCache()
    proof_dependencies: set[ProofDependency] = set()
    for story in sorted({result["story"] for result in results}):
        story_results = [r for r in results if r["story"] == story]
        number = by_id[story]
        failed = any(r["status"] != "pass" for r in story_results)
        # Controlled transport can fully cover a DOM-only contract, but not
        # LAN, real audio, or an end-to-end server/client contract.
        status = "不合格" if failed or cells.get(f"F{number}") == "不合格" else "部分確認"
        reference = str((run / "browser.json").relative_to(ROOT)).replace("\\", "/")
        existing_evidence = cells.get(f"M{number}", "")
        combined_reference = "\n".join(
            part for part in (existing_evidence, reference) if part
        )
        row_dependencies: set[ProofDependency] = set()
        if status != "不合格":
            if current_browser_proof(story, reference):
                status = "合格"
                row_dependencies.add(
                    ProofDependency(
                        kind="browser",
                        story=story,
                        evidence=reference,
                        row=number,
                    )
                )
            elif (
                story in AUDITED_BROWSER
                and cells.get(f"F{number}") == "合格"
                and current_browser_proof(story, combined_reference)
            ):
                # Keep a current complete browser proof when this run adds only
                # a green supplemental scenario for the same story.
                status = "合格"
                row_dependencies.add(
                    ProofDependency(
                        kind="browser",
                        story=story,
                        evidence=combined_reference,
                        row=number,
                    )
                )
            elif story in AUDITED_COMPOSITE:
                browser_required = AUDITED_COMPOSITE[story]["browser"]
                browser_current = current_browser_proof(
                    story,
                    combined_reference,
                    required=browser_required,
                    include_harness=True,
                )
                pytest_current = (
                    current_pytest_proof(story, existing_evidence)
                    if browser_current
                    else False
                )
                if browser_current and pytest_current:
                    status = "合格"
                    row_dependencies.update(
                        {
                            ProofDependency(
                                kind="browser",
                                story=story,
                                evidence=combined_reference,
                                row=number,
                                required=frozenset(browser_required),
                            ),
                            ProofDependency(
                                kind="pytest",
                                story=story,
                                evidence=existing_evidence,
                                row=number,
                            ),
                        }
                    )
            elif story in AUDITED_PERFORMANCE and cells.get(f"F{number}") == "合格":
                if current_performance_proof(story, existing_evidence):
                    # Browser scenarios are supplemental for duration/matrix
                    # performance stories.  A green supplemental run must not
                    # replace a still-current, independently re-audited run proof.
                    status = "合格"
                    row_dependencies.add(
                        ProofDependency(
                            kind="performance",
                            story=story,
                            evidence=existing_evidence,
                            row=number,
                        )
                    )
            elif (
                story in AUDITED_FIELD
                and current_field_proof(
                    story, existing_evidence, cache=field_proof_cache
                )
            ):
                # A green browser supplement does not erase a reviewed current
                # physical-device/field proof. Explicit browser failures above
                # remain authoritative findings.
                status = "合格"
                row_dependencies.add(
                    ProofDependency(
                        kind="field",
                        story=story,
                        evidence=existing_evidence,
                        row=number,
                    )
                )
            elif (
                cells.get(f"F{number}") == "合格"
                and current_pytest_proof(story, existing_evidence)
            ):
                # A green supplemental browser run must not erase a current,
                # complete pytest proof for a story audited on that axis.
                status = "合格"
                row_dependencies.add(
                    ProofDependency(
                        kind="pytest",
                        story=story,
                        evidence=existing_evidence,
                        row=number,
                    )
                )
        new_note = str((run / "browser.json").relative_to(ROOT)).replace("\\", "/") + " #" + story
        new_note += "; " + ", ".join(r["scenario"] + "=" + r["status"] for r in story_results)
        if status == "合格" and row_dependencies:
            note = _proof_preserving_merged_evidence(
                existing_evidence, new_note, row_dependencies
            )
            row_dependencies = _proof_dependencies_with_evidence(
                row_dependencies, note
            )
        else:
            note = merged_evidence(existing_evidence, new_note)
        if status == "合格":
            proof_dependencies.update(row_dependencies)
        ops += [setcell(f"F{number}", value=status), setcell(f"M{number}", value=note),
                setcell(f"O{number}", value=meta["finished_at"])]
    _reaudit_all_nonfield_passes(
        cells, ops, nonfield_proof_cache, proof_dependencies
    )
    _reaudit_all_field_passes(cells, ops, field_proof_cache)
    proof_dependencies.update(_all_planned_pass_dependencies(cells, ops))
    proof_workbook_cells = (
        _proof_dependency_workbook_cells(proof_dependencies)
        | _nonfield_proof_workbook_cells(cells)
        | _field_proof_workbook_cells(cells)
    )
    if not _field_proof_cache_is_current(field_proof_cache):
        raise SystemExit(
            "field receipt/source/manifestがブラウザ同期中に変化したため台帳を変更しません。"
        )
    try:
        fresh = load_sealed_browser_evidence(run)
    except (EvidenceManifestError, OSError, ValueError) as exc:
        raise SystemExit(
            f"browser evidenceが同期中に変化したため台帳を変更しません: {exc}"
        ) from exc
    if fresh != sealed:
        raise SystemExit(
            "browser evidence/sourceが台帳更新直前に変化したため書き込みません。"
        )
    _require_current_sealed_sources(
        fresh.manifest["source_hashes"],
        lambda: browser_source_files(native=native),
        message=(
            "browser evidence/sourceが台帳更新直前に変化したため書き込みません。"
        ),
    )
    if not _field_proof_cache_is_current(field_proof_cache):
        raise SystemExit(
            "field receipt/source/manifestが台帳更新直前に変化したため書き込みません。"
        )
    _assert_workbook_targets_current(
        cells, ops, watched_cells=proof_workbook_cells
    )
    _require_current_proof_dependencies(
        proof_dependencies, checkpoint="browser workbook backup"
    )
    _exclusive_workbook_backup(backup)
    try:
        before_batch = load_sealed_browser_evidence(run)
    except (EvidenceManifestError, OSError, ValueError) as exc:
        raise SystemExit(f"browser evidenceがbatch直前に変化しました: {exc}") from exc
    if before_batch != sealed:
        raise SystemExit(
            "browser evidence/sourceがbatch直前に変化したため書き込みません。"
        )
    _require_current_sealed_sources(
        before_batch.manifest["source_hashes"],
        lambda: browser_source_files(native=native),
        message="browser evidence/sourceがbatch直前に変化したため書き込みません。",
    )
    if not _field_proof_cache_is_current(field_proof_cache):
        raise SystemExit(
            "field receipt/source/manifestがbatch直前に変化したため書き込みません。"
        )
    _assert_workbook_targets_current(
        cells, ops, watched_cells=proof_workbook_cells
    )
    _require_current_proof_dependencies(
        proof_dependencies, checkpoint="browser workbook batch"
    )

    def browser_commit_guard() -> None:
        try:
            committed = load_sealed_browser_evidence(run)
        except (EvidenceManifestError, OSError, ValueError) as exc:
            raise SystemExit(
                f"browser evidence changed before workbook replace: {exc}"
            ) from exc
        if committed != sealed:
            raise SystemExit("browser evidence changed before workbook replace")
        _require_current_sealed_sources(
            committed.manifest["source_hashes"],
            lambda: browser_source_files(native=native),
            message="browser source changed before workbook replace",
        )
        if not _field_proof_cache_is_current(field_proof_cache):
            raise SystemExit("field proof changed before workbook replace")
        dependency_cache = _require_current_proof_dependencies(
            proof_dependencies, checkpoint="browser staged workbook replace"
        )
        _assert_workbook_targets_current(
            cells, ops, watched_cells=proof_workbook_cells
        )
        _require_final_witness_fence(
            checkpoint="browser staged workbook replace",
            proof_caches=(dependency_cache,),
            field_caches=(field_proof_cache,),
        )

    _guarded_batch(ops, browser_commit_guard)
    office("save", BOOK)
    check()


def _plan_audit(
    cells: dict[str, str],
    *,
    cache: AuditProofCache | None = None,
) -> tuple[list[str], list[dict], FieldProofCache, set[str]]:
    """Plan one complete evidence audit without mutating the workbook."""
    proof_cache = cache if cache is not None else AuditProofCache()
    changes: list[str] = []
    ops: list[dict] = []
    field_proof_cache = proof_cache.field
    read_set: set[str] = set()
    for cell, story in cells.items():
        if not re.fullmatch(r"A\d+", cell):
            continue
        number = int(cell[1:])
        if number < 7:
            continue
        read_set.update({f"A{number}", f"F{number}"})
        if cells.get(f"F{number}") != "合格":
            continue
        read_set.update({f"M{number}", f"N{number}"})
        if story in AUDITED_FIELD:
            if current_field_proof(
                story, cells.get(f"M{number}", ""), cache=field_proof_cache
            ):
                continue
            changes.append(story)
            note = (
                cells.get(f"N{number}", "")
                + "\n現地証拠を現行source/manifestで再監査できないため部分確認。"
            )
            ops += [
                setcell(f"F{number}", value="部分確認"),
                setcell(f"N{number}", value=note),
            ]
            continue
        if story in AUDITED_PERFORMANCE:
            if current_performance_proof(
                story,
                cells.get(f"M{number}", ""),
                cache=proof_cache,
            ):
                continue
            changes.append(story)
            note = (
                cells.get(f"N{number}", "")
                + "\n性能証拠を現行source/manifestで再監査できないため部分確認。"
            )
            ops += [
                setcell(f"F{number}", value="部分確認"),
                setcell(f"N{number}", value=note),
            ]
            continue
        current_test_proof = None
        if story in AUDITED_AUTOMATIC:
            current_test_proof = current_pytest_proof(
                story,
                cells.get(f"M{number}", ""),
                cache=proof_cache,
            )
        elif story in AUDITED_BROWSER:
            current_test_proof = current_browser_proof(
                story,
                cells.get(f"M{number}", ""),
                cache=proof_cache,
            )
        elif story in AUDITED_COMPOSITE:
            pytest_current = current_pytest_proof(
                story,
                cells.get(f"M{number}", ""),
                cache=proof_cache,
            )
            browser_current = current_browser_proof(
                story,
                cells.get(f"M{number}", ""),
                required=AUDITED_COMPOSITE[story]["browser"],
                include_harness=True,
                cache=proof_cache,
            )
            current_test_proof = pytest_current and browser_current
        if current_test_proof is not None:
            if current_test_proof:
                continue
            changes.append(story)
            note = (
                cells.get(f"N{number}", "")
                + "\nテスト証拠を現行sourceで再監査できないため部分確認。"
            )
            ops += [
                setcell(f"F{number}", value="部分確認"),
                setcell(f"N{number}", value=note),
            ]
            continue
        changes.append(story)
        note = cells.get(f"N{number}", "") + "\n関連テスト成功。合格条件の全節との対応監査が未完了のため部分確認。"
        ops += [setcell(f"F{number}", value="部分確認"), setcell(f"N{number}", value=note)]
    return changes, ops, field_proof_cache, read_set


def _recheck_audit_proof_cache_witnesses(cache: AuditProofCache) -> bool:
    """Fence exact bytes and revisions from the latest full validation."""

    try:
        if not cache.import_witnesses:
            return False
        if set(cache.file_witnesses) != set(cache.file_fingerprints):
            return False
        # Source and validator witnesses may cover large trees.  Check them
        # first, then finish with the small exact-file/import witnesses so an
        # earlier artifact cannot change while a later heavy fence is running.
        for key, snapshot in cache.source_snapshots.items():
            if key == ("pytest", False):
                supplier = pytest_source_files
            elif key[0] == "browser":
                supplier = _browser_source_supplier(key[1])
            else:
                return False
            if not _source_snapshot_revisions_are_current(snapshot, supplier):
                return False
        for state in cache.performance_states.values():
            if state.validation_witness is None:
                if validate_performance_manifest is _STRICT_PERFORMANCE_VALIDATOR:
                    return False
                continue
            performance_evidence_module.recheck_validation_witness(
                state.validation_witness
            )
        if not _recheck_field_cache_witnesses(cache.field):
            return False
        for witness in cache.file_witnesses.values():
            _recheck_code_binding_revision(witness)
        for witness in cache.import_witnesses:
            _recheck_code_binding_revision(witness)
        return True
    except (
        AttributeError,
        EvidenceManifestError,
        FieldEvidenceError,
        PerformanceEvidenceError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ):
        return False


def _audit_proof_cache_is_current(cache: AuditProofCache) -> bool:
    """Fully revalidate a pass and retain witnesses for a final light fence."""

    try:
        import_witnesses = _validated_import_code_bindings()
        latest_file_witnesses: dict[Path, CodeBinding] = {}
        for path, expected_digest in cache.file_fingerprints.items():
            absolute = Path(os.path.abspath(path))
            _content, witness = _read_bound_repository_file(
                absolute, root=_binding_root_for_path(absolute)
            )
            if witness.sha256 != expected_digest:
                return False
            latest_file_witnesses[absolute] = witness
        latest_source_snapshots: dict[
            tuple[str, bool], SourceHashSnapshot
        ] = {}
        for key, expected_snapshot in cache.source_snapshots.items():
            if key == ("pytest", False):
                current_snapshot = _stable_supplied_source_snapshot(
                    pytest_source_files
                )
            elif key[0] == "browser":
                current_snapshot = _stable_supplied_source_snapshot(
                    _browser_source_supplier(key[1])
                )
            else:
                return False
            if current_snapshot != expected_snapshot:
                return False
            latest_source_snapshots[key] = current_snapshot
        latest_performance_states: dict[
            tuple[Path, str, Path], PerformanceProofState
        ] = {}
        for (run, kind, report), expected_performance in tuple(
            cache.performance_states.items()
        ):
            manifest = validate_performance_manifest(
                run, kind=kind, primary_report=report, root=ROOT
            )
            validation_witness = getattr(manifest, "validation_witness", None)
            if (
                validation_witness is None
                and validate_performance_manifest
                is _STRICT_PERFORMANCE_VALIDATOR
            ):
                return False
            current_performance = PerformanceProofState(
                semantic=_performance_semantic(manifest),
                validation_witness=validation_witness,
            )
            # The semantic evidence must remain identical.  Keep the newest
            # validation witness so an intentional sibling write (for example
            # the exclusive workbook backup) can be followed by a new full
            # validation and a final byte-and-revision fence.
            if current_performance.semantic != expected_performance.semantic:
                return False
            latest_performance_states[(run, kind, report)] = current_performance
        if not _field_proof_cache_is_current(cache.field):
            return False

        cache.import_witnesses = import_witnesses
        cache.file_witnesses = latest_file_witnesses
        cache.source_snapshots = latest_source_snapshots
        cache.performance_states = latest_performance_states
        # Every expensive predicate above contributes its newest stable-read
        # witness.  Recheck all of them only after the last predicate so an
        # early browser/pytest/catalog input cannot change during a later,
        # slower field or performance validation.
        return _recheck_audit_proof_cache_witnesses(cache)
    except (
        AttributeError,
        EvidenceManifestError,
        FieldEvidenceError,
        PerformanceEvidenceError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ):
        return False


def _source_snapshot_revisions_are_current(
    snapshot: SourceHashSnapshot,
    supplier: Callable[[], list[Path]],
) -> bool:
    """Recheck exact source membership, stable bytes, and file revisions."""

    if not isinstance(snapshot, SourceHashSnapshot):
        return False
    try:
        current = _stable_supplied_source_snapshot(supplier)
        return current == snapshot and current.revisions == snapshot.revisions
    except (EvidenceManifestError, OSError, ValueError, TypeError):
        return False


def _capture_verified_workbook_state() -> VerifiedWorkbookState:
    """Audit every live pass while the workbook remains one stable revision."""

    try:
        workbook_bytes, before = _stable_file_snapshot(BOOK)
    except OSError as exc:
        raise SystemExit(f"live workbook cannot be audited safely: {exc}") from exc
    cells = read_cells()
    cache = AuditProofCache()
    changes, ops, _field_cache, _read_set = _plan_audit(cells, cache=cache)
    if changes or ops:
        raise SystemExit(
            "live workbook contains pass rows without current complete proof: "
            + ", ".join(changes)
        )
    if not _audit_proof_cache_is_current(cache):
        raise SystemExit("live workbook proof state changed during final audit")
    try:
        _after_bytes, after = _stable_file_snapshot(BOOK)
    except OSError as exc:
        raise SystemExit(f"live workbook changed during final audit: {exc}") from exc
    if after != before:
        raise SystemExit("live workbook changed during final audit")
    if not _recheck_audit_proof_cache_witnesses(cache):
        raise SystemExit(
            "live workbook proof state changed during workbook fingerprint CAS"
        )
    file_revision = before[0]
    parent_revision = before[1]
    if not (
        isinstance(file_revision, tuple)
        and len(file_revision) == 5
        and isinstance(parent_revision, tuple)
        and len(parent_revision) >= 2
    ):
        raise SystemExit("live workbook fingerprint is malformed")
    return VerifiedWorkbookState(
        file_revision=file_revision,
        parent_identity=(parent_revision[0], parent_revision[1]),
        sha256=hashlib.sha256(workbook_bytes).hexdigest(),
        size=len(workbook_bytes),
        proof_cache=cache,
    )


def _require_verified_workbook_state_current(
    expected: VerifiedWorkbookState, *, checkpoint: str
) -> None:
    """Recheck proof dependencies, then CAS the entire workbook revision."""

    if not _audit_proof_cache_is_current(expected.proof_cache):
        raise SystemExit(f"workbook proof state changed before {checkpoint}")
    try:
        workbook_bytes, fingerprint = _stable_file_snapshot(BOOK)
    except OSError as exc:
        raise SystemExit(
            "concurrent workbook edit detected before "
            f"{checkpoint}; external edit preserved; rerun audit: {exc}"
        ) from exc
    current_file = fingerprint[0]
    current_parent = fingerprint[1]
    if (
        not isinstance(current_parent, tuple)
        or current_file != expected.file_revision
        or current_parent[:2] != expected.parent_identity
        or len(workbook_bytes) != expected.size
        or hashlib.sha256(workbook_bytes).hexdigest() != expected.sha256
    ):
        raise SystemExit(
            "concurrent workbook edit detected before "
            f"{checkpoint}; external edit preserved; rerun audit"
        )
    if not _recheck_audit_proof_cache_witnesses(expected.proof_cache):
        raise SystemExit(f"workbook proof state changed during {checkpoint}")


def audit() -> None:
    _require_import_code_current(checkpoint="coverage audit")
    cells = read_cells()
    initial_cache = AuditProofCache()
    initial = _plan_audit(cells, cache=initial_cache)
    initial_changes, initial_ops, initial_field_cache, initial_read_set = initial
    if not _field_proof_cache_is_current(initial_field_cache):
        raise SystemExit(
            "field receipt/source/manifestが監査中に変化したため台帳を変更しません。"
        )
    _assert_audit_read_set_current(cells, initial_read_set)

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    runs = QA / "runs"
    _require_safe_output_parent(
        runs / f".{stamp}-coverage-audit-placeholder",
        label="coverage audit",
    )
    staging = Path(
        tempfile.mkdtemp(prefix=f".{stamp}-coverage-audit-", dir=runs)
    )
    out = runs / f"{stamp}-coverage-audit"
    mutation_started = False
    finalized = False
    try:
        _require_safe_output_parent(
            staging / "feature-status-before-sync.xlsx",
            label="coverage audit backup",
        )
        _exclusive_workbook_backup(
            staging / "feature-status-before-sync.xlsx"
        )

        fresh_cache = AuditProofCache()
        fresh = _plan_audit(cells, cache=fresh_cache)
        changes, ops, fresh_field_cache, fresh_read_set = fresh
        if (
            (initial_changes, initial_ops, initial_read_set)
            != (changes, ops, fresh_read_set)
            or initial_cache.freshness_state()
            != fresh_cache.freshness_state()
        ):
            raise SystemExit(
                "audit evidence changed before workbook mutation; "
                "the workbook was not changed."
            )
        if not (
            _field_proof_cache_is_current(initial_field_cache)
            and _field_proof_cache_is_current(fresh_field_cache)
        ):
            raise SystemExit(
                "field receipt/source/manifestが監査中に変化したため台帳を変更しません。"
            )
        _assert_audit_read_set_current(cells, fresh_read_set)

        def audit_commit_guard() -> None:
            if not _audit_proof_cache_is_current(fresh_cache):
                raise SystemExit(
                    "audit proof state changed before workbook replace"
                )
            _assert_audit_read_set_current(cells, fresh_read_set)
            if not _recheck_audit_proof_cache_witnesses(fresh_cache):
                raise SystemExit(
                    "audit proof state changed during workbook read-set CAS"
                )

        audit_commit_guard()

        if ops:
            mutation_started = True
            _guarded_batch(ops, audit_commit_guard)
        # check() also performs workbook formatting/formula/compaction writes.
        # Preserve the staged backup if any of those writes or validations fail.
        mutation_started = True
        verified_workbook = check()
        if not isinstance(verified_workbook, VerifiedWorkbookState):
            raise SystemExit("check did not return a verified workbook state")
        if not _audit_proof_cache_is_current(fresh_cache):
            raise SystemExit("audit proof state changed before finalization")
        audit_bytes = (
            json.dumps(
                {
                    "downgraded_to_partial": changes,
                    "audited_automatic": sorted(AUDITED_AUTOMATIC),
                    "audited_browser": sorted(AUDITED_BROWSER),
                    "audited_composite": sorted(AUDITED_COMPOSITE),
                    "audited_performance": dict(
                        sorted(AUDITED_PERFORMANCE.items())
                    ),
                    "audited_field": sorted(AUDITED_FIELD),
                    "workbook": {
                        "sha256": verified_workbook.sha256,
                        "size": verified_workbook.size,
                        "file_revision": list(verified_workbook.file_revision),
                        "parent_identity": list(
                            verified_workbook.parent_identity
                        ),
                    },
                },
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        _require_safe_output_parent(
            staging / "audit.json", label="coverage audit result"
        )
        _exclusive_regular_file_write(
            staging / "audit.json", audit_bytes, label="coverage audit result"
        )
        _require_verified_workbook_state_current(
            verified_workbook, checkpoint="coverage audit publication"
        )
        _require_safe_output_parent(out, label="coverage audit publication")
        staging.rename(out)
        finalized = True
    except BaseException:
        if mutation_started and staging.exists():
            failed = runs / f".{stamp}-coverage-audit-failed"
            staging.rename(failed)
        raise
    finally:
        if not finalized and staging.exists():
            shutil.rmtree(staging)


def _performance_manifest(run: Path, kind: str, report: Path) -> dict:
    try:
        return validate_performance_manifest(run, kind=kind, primary_report=report, root=ROOT)
    except PerformanceEvidenceError as exc:
        raise SystemExit(
            f"性能runのsource manifestが採用条件を満たしません。現行状態で再実行してください: {exc}"
        ) from exc


def _performance_auditor_state() -> tuple[tuple[object, ...], ...]:
    """Fingerprint the in-process rules that reinterpret old measurements."""

    return tuple(
        _stable_file_fingerprint(path)
        for path in (Path(__file__), Path(performance_evidence_module.__file__))
    )


def _load_sealed_performance_json(report: Path, manifest: dict) -> dict:
    """Read once, verify those exact bytes, then parse them.

    ``validate_performance_manifest`` validates current disk state, but opening
    the report again without checking the captured bytes would leave a TOCTOU
    window.  This helper binds the JSON object passed to audit rules to the
    artifact hash already sealed in the manifest.
    """
    primary = manifest.get("primary_report")
    artifacts = manifest.get("artifacts")
    if not isinstance(primary, str) or not isinstance(artifacts, dict):
        raise PerformanceEvidenceError("性能manifestのartifact索引が不正です")
    expected_path = (report.parent / primary).resolve()
    if expected_path != report.resolve():
        raise PerformanceEvidenceError("性能manifestのprimary report参照が一致しません")
    expected_hash = artifacts.get(primary)
    if not isinstance(expected_hash, str):
        raise PerformanceEvidenceError("性能manifestにprimary report hashがありません")
    payload = report.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise PerformanceEvidenceError("監査直前のprimary report hashが一致しません")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PerformanceEvidenceError("primary report JSONを読めません") from exc
    if not isinstance(decoded, dict):
        raise PerformanceEvidenceError("primary report JSONの形式が不正です")
    return decoded


def _require_current_performance_run(
    *,
    run: Path,
    kind: str,
    report: Path,
    expected_manifest: dict,
    expected_audit: object,
    evidence_path: Path,
    evidence_bytes: bytes,
    expected_auditor_state: tuple[tuple[object, ...], ...],
    checkpoint: str,
) -> PerformanceRunWitness:
    """Revalidate a newly imported performance run and its exact index bytes."""

    try:
        manifest = _performance_manifest(run, kind, report)
        payload = _load_sealed_performance_json(report, manifest)
        if kind == "soak":
            audited: object = _audit_soak_report(payload, manifest)
        elif kind == "scale":
            audited = _audit_scale_report(payload, manifest)
        else:
            raise ValueError(f"unsupported performance evidence kind: {kind}")
        current_evidence, evidence_witness = _read_bound_repository_file(
            evidence_path, root=_binding_root_for_path(evidence_path)
        )
        auditor_state = _performance_auditor_state()
        validation_witness = getattr(manifest, "validation_witness", None)
        if (
            validation_witness is None
            and validate_performance_manifest
            is _STRICT_PERFORMANCE_VALIDATOR
        ):
            raise PerformanceEvidenceError(
                "performance validation witness is missing"
            )
    except (OSError, ValueError, KeyError, TypeError, SystemExit) as exc:
        raise SystemExit(
            f"performance evidence changed before {checkpoint}: {exc}"
        ) from exc
    if (
        manifest != expected_manifest
        or audited != expected_audit
        or current_evidence != evidence_bytes
        or auditor_state != expected_auditor_state
    ):
        raise SystemExit(f"performance evidence changed before {checkpoint}")
    try:
        if validation_witness is not None:
            performance_evidence_module.recheck_validation_witness(
                validation_witness
            )
        _recheck_code_binding_revision(evidence_witness)
    except (PerformanceEvidenceError, OSError, ValueError, TypeError) as exc:
        raise SystemExit(
            f"performance evidence changed during {checkpoint}: {exc}"
        ) from exc
    return PerformanceRunWitness(validation_witness, evidence_witness)


def _recheck_performance_run_witness(witness: PerformanceRunWitness) -> None:
    try:
        if witness.manifest_validation_witness is not None:
            performance_evidence_module.recheck_validation_witness(
                witness.manifest_validation_witness
            )
        _recheck_code_binding_revision(witness.evidence_witness)
    except (PerformanceEvidenceError, OSError, ValueError, TypeError) as exc:
        raise SystemExit(f"performance evidence changed during final fence: {exc}") from exc


def _require_final_witness_fence(
    *,
    checkpoint: str,
    proof_caches: tuple[AuditProofCache, ...] = (),
    field_caches: tuple[FieldProofCache, ...] = (),
    field_snapshots: tuple[FieldProofSnapshot, ...] = (),
    performance_runs: tuple[PerformanceRunWitness, ...] = (),
) -> None:
    """Aggregate byte-and-revision fence run after the live workbook CAS.

    Every argument must have come from the immediately preceding full
    validation.  It avoids semantic parsing, but rehashes bound bytes because
    Windows does not guarantee that the exposed stat tuple detects an ABA
    same-size overwrite whose timestamps were restored.
    """

    for witness in performance_runs:
        _recheck_performance_run_witness(witness)
    for field_cache in field_caches:
        if not _recheck_field_cache_witnesses(field_cache):
            raise SystemExit(f"field proof changed during {checkpoint}")
    if field_snapshots and not _recheck_field_snapshots(field_snapshots):
        raise SystemExit(f"field receipt changed during {checkpoint}")
    for proof_cache in proof_caches:
        if not _recheck_audit_proof_cache_witnesses(proof_cache):
            raise SystemExit(f"proof dependency changed during {checkpoint}")


def _same_number(left: object, right: object) -> bool:
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return False


def _at_least(value: object, minimum: object) -> bool:
    try:
        actual = float(value)
        threshold = float(minimum)
        return actual >= threshold or math.isclose(
            actual, threshold, rel_tol=1e-12, abs_tol=1e-9
        )
    except (TypeError, ValueError):
        return False


def _at_most(value: object, maximum: object) -> bool:
    try:
        return float(value) <= float(maximum)
    except (TypeError, ValueError):
        return False


def _number(value: object) -> float:
    if isinstance(value, bool):
        return math.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _expected_student_langs(point: dict) -> dict[str, int] | None:
    langs = point.get("langs")
    students = point.get("students")
    if (
        not isinstance(langs, list)
        or not langs
        or not isinstance(students, int)
        or isinstance(students, bool)
        or students < 0
    ):
        return None
    return dict(Counter(langs[index % len(langs)] for index in range(students)))


def _scale_delivery_complete(point: dict) -> bool:
    published = point.get("published_seqs")
    students = point.get("students")
    active_langs = point.get("active_langs")
    delivery = point.get("continuous_delivery_by_student")
    return (
        isinstance(published, int)
        and not isinstance(published, bool)
        and published > 0
        and isinstance(students, int)
        and not isinstance(students, bool)
        and isinstance(active_langs, int)
        and not isinstance(active_langs, bool)
        and point.get("captions") == published * active_langs
        and isinstance(delivery, dict)
        and len(delivery) == students
        and all(
            isinstance(detail, dict)
            and detail.get("published") == published
            and detail.get("expected") == published
            and detail.get("received") == published
            and detail.get("gaps") == 0
            and detail.get("missing_seqs") == []
            and detail.get("exempt_own_history") == []
            for detail in delivery.values()
        )
    )


def _audit_soak_report(report: dict, manifest: dict) -> list[dict]:
    results = report.get("results") if isinstance(report, dict) else None
    if not isinstance(results, list) or not results:
        raise SystemExit("acceptance reportに完了結果がありません。")
    args = manifest["arguments"]
    single_fixed_engine = len(results) == 1 and args.get("engine") is not None
    audited = []
    for result in results:
        if not isinstance(result, dict) or "verdict" not in result:
            raise SystemExit("acceptance reportの結果形式が不正です。")
        if not (
            _same_number(result.get("minutes"), args.get("minutes"))
            and result.get("students") == args.get("students")
            and result.get("corpus") == args.get("corpus")
            and result.get("disconnect_every_s") == args.get("disconnect_every_s")
            and result.get("mt_cache_size") == args.get("mt_cache_size")
        ):
            raise SystemExit("acceptance reportとmanifest引数が一致しません。")
        if args.get("engine") is not None and result.get("engine") != args["engine"]:
            raise SystemExit("acceptance reportのengineがmanifest引数と一致しません。")
        if args.get("langs") is not None and result.get("langs") != args["langs"]:
            raise SystemExit("acceptance reportのlangsがmanifest引数と一致しません。")
        if args.get("asr") is not None and result.get("asr_engine") != args["asr"]:
            raise SystemExit("acceptance reportのASR engineがmanifest引数と一致しません。")
        real_asr = result.get("asr_engine") in {"sherpa", "faster-whisper"}

        minutes = float(result["minutes"])
        target_s = minutes * 60
        drift = result.get("drift") if isinstance(result.get("drift"), dict) else None
        windows = drift.get("windows", []) if drift else []
        usable_windows = []
        trailing_windows = []
        for window in windows:
            if not isinstance(window, dict):
                continue
            try:
                start_s = float(window.get("start_s"))
            except (TypeError, ValueError):
                continue
            (usable_windows if start_s < target_s else trailing_windows).append(window)
        usable_windows.sort(key=lambda window: float(window["start_s"]))
        expected_window_count = math.ceil(target_s / PERFORMANCE_DRIFT_WINDOW_S)
        drift_windows_complete = (
            bool(drift)
            and drift.get("window_s") == PERFORMANCE_DRIFT_WINDOW_S
            and drift.get("empty_windows") == 0
            and len(usable_windows) == expected_window_count
            and all(
                window.get("index") == index
                and _same_number(window.get("start_s"), index * PERFORMANCE_DRIFT_WINDOW_S)
                and isinstance(window.get("count"), int)
                and not isinstance(window.get("count"), bool)
                and window["count"] > 0
                and isinstance(window.get("median_s"), (int, float))
                and not isinstance(window.get("median_s"), bool)
                for index, window in enumerate(usable_windows)
            )
        )
        final_window_observed = (
            drift_windows_complete
            and any(
                target_s - PERFORMANCE_DRIFT_WINDOW_S
                <= float(window.get("start_s", -1))
                < target_s
                and int(window.get("count", 0)) > 0
                for window in usable_windows
            )
        ) if drift else False
        drift_increase = None
        drifting = False
        if len(usable_windows) >= 2:
            first = float(usable_windows[0]["median_s"])
            last = float(usable_windows[-1]["median_s"])
            drift_increase = round(last - first, 2)
            drifting = drift_increase > 1.0 and drift_increase > first * 0.5

        resources = result.get("resources") if isinstance(result.get("resources"), list) else []
        resource_names = {item.get("name") for item in resources if isinstance(item, dict)}
        resources_growing = [
            str(item["name"])
            for item in resources
            if isinstance(item, dict) and item.get("growing")
        ]
        resource_observation = (
            result.get("resource_observation")
            if isinstance(result.get("resource_observation"), dict)
            else {}
        )
        sampler_interval = resource_observation.get("sampler_interval_s")
        stats_interval = resource_observation.get("stats_interval_s")
        process_required = int(target_s / PERFORMANCE_RESOURCE_INTERVAL_S * 0.8)
        stats_required = int(target_s / PERFORMANCE_RESOURCE_INTERVAL_S * 0.8)
        resources_by_name = {
            item.get("name"): item for item in resources if isinstance(item, dict)
        }
        resource_coverage_complete = (
            sampler_interval == PERFORMANCE_RESOURCE_INTERVAL_S
            and stats_interval == PERFORMANCE_RESOURCE_INTERVAL_S
            and _at_least(resource_observation.get("process_samples"), process_required)
            and _at_least(resource_observation.get("rss_samples"), process_required)
            and _at_least(resource_observation.get("thread_samples"), process_required)
            and _at_least(resource_observation.get("handle_samples"), process_required)
            and _at_least(resource_observation.get("stats_samples"), stats_required)
            and _at_most(
                resource_observation.get("process_first_at_s"),
                PERFORMANCE_RESOURCE_INTERVAL_S * 5,
            )
            and _at_most(
                resource_observation.get("stats_first_at_s"),
                PERFORMANCE_RESOURCE_INTERVAL_S * 5,
            )
            and _at_most(
                resource_observation.get("process_max_gap_s"),
                PERFORMANCE_RESOURCE_INTERVAL_S * 5,
            )
            and _at_most(
                resource_observation.get("stats_max_gap_s"),
                PERFORMANCE_RESOURCE_INTERVAL_S * 5,
            )
            and _at_least(
                resource_observation.get("process_last_at_s"), result.get("measurement_seconds")
            )
            and _at_least(
                resource_observation.get("stats_last_at_s"), result.get("measurement_seconds")
            )
            and resources_by_name.get("threads", {}).get("samples")
            == resource_observation.get("thread_samples")
            and resources_by_name.get("handles", {}).get("samples")
            == resource_observation.get("handle_samples")
            and resources_by_name.get("tasks", {}).get("samples")
            == resource_observation.get("stats_samples")
        )
        dropped = result.get("dropped") if isinstance(result.get("dropped"), dict) else {}
        notices = dropped.get("notices") if isinstance(dropped.get("notices"), dict) else {}
        latency = result.get("latency") if isinstance(result.get("latency"), dict) else {}
        memory = result.get("memory") if isinstance(result.get("memory"), dict) else {}
        restore = result.get("restore") if isinstance(result.get("restore"), dict) else {}
        by_student = restore.get("by_student") if isinstance(restore.get("by_student"), dict) else {}
        injected_by_student = (
            restore.get("injected_by_student")
            if isinstance(restore.get("injected_by_student"), dict)
            else {}
        )
        restored_by_student = (
            restore.get("restored_by_student")
            if isinstance(restore.get("restored_by_student"), dict)
            else {}
        )
        injection_events = (
            restore.get("injection_events")
            if isinstance(restore.get("injection_events"), list)
            else []
        )
        continuous_delivery = (
            restore.get("continuous_delivery_by_student")
            if isinstance(restore.get("continuous_delivery_by_student"), dict)
            else {}
        )
        students = int(result["students"])
        expected_langs = dict(
            Counter(result["langs"][index % len(result["langs"])] for index in range(students))
        ) if isinstance(result.get("langs"), list) and result["langs"] else {}
        measurement_complete = (
            _at_least(result.get("measurement_seconds"), target_s)
            and _at_least(
                result.get("ran_seconds"),
                _number(result.get("measurement_seconds")) + _number(args.get("drain_seconds")),
            )
            and isinstance(result.get("audio_bytes_planned"), int)
            and not isinstance(result.get("audio_bytes_planned"), bool)
            and result["audio_bytes_planned"] > 0
            and result.get("audio_bytes_sent") == result["audio_bytes_planned"]
        )
        final_stats = result.get("final_stats") if isinstance(result.get("final_stats"), dict) else {}
        population_observed = (
            result.get("measurement_students") == students
            and result.get("measurement_langs") == expected_langs
            and final_stats.get("students") == students
            and final_stats.get("langs") == expected_langs
            and _at_least(
                final_stats.get("at_s"),
                _number(result.get("ran_seconds"))
                - PERFORMANCE_RESOURCE_INTERVAL_S * 2,
            )
            and final_stats.get("queue_depth") == 0
            and final_stats.get("audio_queue_seconds") == 0
            and final_stats.get("mt_queue_depth") == 0
        )
        injected_values_are_positive = bool(injected_by_student) and all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in injected_by_student.values()
        )
        injection_totals_match = (
            isinstance(restore.get("injected_disconnects"), int)
            and not isinstance(restore.get("injected_disconnects"), bool)
            and sum(injected_by_student.values()) == restore["injected_disconnects"]
        ) if injected_values_are_positive else False
        restored_counts_match = (
            set(restored_by_student) == set(injected_by_student)
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in restored_by_student.values()
            )
            and restored_by_student == injected_by_student
        )
        event_audit_complete = (
            bool(injection_events)
            and restore.get("injection_attempts") == len(injection_events)
            and restore.get("injection_close_failures") == 0
            and len(injection_events) == restore.get("injected_disconnects")
            and all(
                isinstance(event, dict)
                and event.get("student") in injected_by_student
                and event.get("close_succeeded") is True
                and event.get("joined") is True
                and event.get("restore_complete") is True
                and isinstance(event.get("requested_last_seq"), int)
                and isinstance(event.get("history_from"), int)
                and isinstance(event.get("seq_head"), int)
                and isinstance(event.get("published_head_at_close"), int)
                and not isinstance(event.get("published_head_at_close"), bool)
                and event["published_head_at_close"] >= 0
                and isinstance(event.get("published_count_at_close"), int)
                and not isinstance(event.get("published_count_at_close"), bool)
                and event["published_count_at_close"] >= 0
                and isinstance(event.get("received_head_at_close"), int)
                and not isinstance(event.get("received_head_at_close"), bool)
                and event["received_head_at_close"] >= 0
                and isinstance(event.get("received_count_at_close"), int)
                and not isinstance(event.get("received_count_at_close"), bool)
                and event["received_count_at_close"] >= 0
                and event.get("expected_seqs") == list(range(
                    max(event["requested_last_seq"] + 1, event["history_from"]),
                    event["seq_head"] + 1,
                ))
                and event.get("beyond_history_seqs") == list(range(
                    max(
                        event["requested_last_seq"] + 1,
                        event["published_head_at_close"] + 1,
                    ),
                    min(event["history_from"], event["seq_head"] + 1),
                ))
                and event.get("missing_seqs") == []
                and _at_least(event.get("restore_seconds"), 0.0)
                for event in injection_events
            )
            and {
                event["student"]
                for event in injection_events
                if event.get("expected_seqs")
            } == set(injected_by_student)
        )
        continuous_delivery_complete = (
            len(by_student) == students
            and set(continuous_delivery) == set(by_student)
            and all(
                isinstance(detail, dict)
                and detail.get("gaps") == 0
                and detail.get("missing_seqs") == []
                and isinstance(detail.get("expected"), int)
                and detail["expected"] > 0
                and detail.get("published") == detail["expected"]
                and detail.get("received") == detail["published"]
                and detail.get("exempt_own_history") == []
                for detail in continuous_delivery.values()
            )
        )
        student_detail_complete = (
            restore.get("students") == students
            and len(by_student) == students
            and set(by_student) == set(injected_by_student)
            and injected_values_are_positive
            and injection_totals_match
            and restored_counts_match
            and event_audit_complete
            and continuous_delivery_complete
            and all(
                isinstance(detail, dict)
                and detail.get("published", 0) > 0
                and detail.get("recoverable", 0) > 0
                and "gaps" in detail
                for detail in by_student.values()
            )
        )
        every_s = result.get("disconnect_every_s")
        event_times = [
            _number(event.get("attempted_at_s"))
            for event in injection_events
            if isinstance(event, dict)
        ]
        cadence_tolerance_s = 10.0
        event_times_valid = bool(event_times) and all(
            math.isfinite(at_s)
            and 0 <= at_s <= _number(result.get("measurement_seconds"))
            for at_s in event_times
        )
        cadence_complete = (
            isinstance(every_s, (int, float))
            and not isinstance(every_s, bool)
            and float(every_s) > 0
            and len(event_times) == len(injection_events)
            and event_times_valid
            and event_times == sorted(event_times)
            and event_times[0] <= float(every_s) + cadence_tolerance_s
            and all(
                later - earlier <= float(every_s) + cadence_tolerance_s
                for earlier, later in zip(event_times, event_times[1:])
            )
            and event_times[-1]
            >= _number(result.get("measurement_seconds"))
            - float(every_s)
            - cadence_tolerance_s
        )
        qualified = {
            "PERF-03": (
                single_fixed_engine
                and real_asr
                and minutes >= 90
                and measurement_complete
                and population_observed
                and continuous_delivery_complete
                and drift_windows_complete
                and final_window_observed
                and {"threads", "handles", "tasks"} <= resource_names
                and resource_coverage_complete
            ),
            "PERF-05": (
                single_fixed_engine
                and real_asr
                and minutes >= 45
                and students >= 10
                and isinstance(every_s, (int, float))
                and 0 < float(every_s) <= 60
                and measurement_complete
                and population_observed
                and student_detail_complete
                and cadence_complete
            ),
        }
        evaluations = {
            "PERF-01": (
                bool(latency)
                and latency.get("median_s", math.inf) <= 5
                and latency.get("max_s", math.inf) <= 8
            ),
            "PERF-02": (
                bool(memory)
                and memory.get("peak_mb", math.inf) <= 5000
                and not memory.get("increasing", True)
            ),
            "PERF-03": (
                qualified["PERF-03"]
                and result.get("crashes") == 0
                and result.get("reconnect_failures") == 0
                and not result.get("errors")
                and not memory.get("increasing", True)
                and not drifting
                and not resources_growing
                and dropped.get("segments") == 0
                and dropped.get("frames_rejected") == 0
                and not any(notices.get(code, 0) for code in ("asr_timeout", "mt_timeout"))
            ),
            "PERF-05": (
                qualified["PERF-05"]
                and restore.get("injected_disconnects", 0) > 0
                and restore.get("gaps") == 0
                and all(detail.get("gaps") == 0 for detail in by_student.values())
                and result.get("disconnects") == 0
                and result.get("reconnect_failures") == 0
                and result.get("crashes") == 0
                and not result.get("errors")
            ),
        }
        audited.append({
            "engine": result.get("engine"),
            "minutes": minutes,
            "students": students,
            "scope": "開発機・入力音源・Python WSクライアント",
            "qualified": qualified,
            "evaluations": evaluations,
            "student_restore": {
                "complete_collection": student_detail_complete,
                "by_student": by_student,
                "injected_by_student": injected_by_student,
                "restored_by_student": restored_by_student,
                "injection_events": injection_events,
                "continuous_delivery_by_student": continuous_delivery,
                "cadence_complete": cadence_complete,
                "cadence_tolerance_s": cadence_tolerance_s,
                "first_injection_at_s": event_times[0] if event_times else None,
                "last_injection_at_s": event_times[-1] if event_times else None,
                "max_injection_gap_s": (
                    max(later - earlier for earlier, later in zip(event_times, event_times[1:]))
                    if event_times_valid and len(event_times) > 1
                    else None
                ),
            },
            "drift": {
                "usable_first_median_s": usable_windows[0]["median_s"] if usable_windows else None,
                "usable_last_median_s": usable_windows[-1]["median_s"] if usable_windows else None,
                "increase_s": drift_increase,
                "drifting": drifting,
                "excluded_post_duration_windows": trailing_windows,
                "final_window_observed": final_window_observed,
                "all_measurement_windows_observed": drift_windows_complete,
                "reason": "目標試験時間以後のdrainは定常運転のドリフト判定から除外",
            },
            "resources_growing": resources_growing,
            "resource_coverage_complete": resource_coverage_complete,
            "original_overall_verdict": result["verdict"],
        })
    return audited


def sync_soak(run: Path) -> None:
    _require_import_code_current(checkpoint="soak sync")
    _require_safe_output_parent(run / "story-evidence.json", label="soak sync")
    auditor_state = _performance_auditor_state()
    reports = list(run.glob("*-acceptance.json"))
    if len(reports) != 1:
        raise SystemExit("完了したacceptance.jsonが1つ必要です。実行中は同期しません。")
    manifest = _performance_manifest(run, "soak", reports[0])
    try:
        report = _load_sealed_performance_json(reports[0], manifest)
    except (OSError, ValueError) as exc:
        raise SystemExit("acceptance reportを読めません。") from exc
    audited = _audit_soak_report(report, manifest)

    backup = run / "feature-status-before-sync.xlsx"
    if os.path.lexists(backup):
        raise SystemExit("This soak run was already synced.")
    cells = read_cells()
    by_id = {value: int(key[1:]) for key, value in cells.items() if re.fullmatch(r"A\d+", key)}
    evidence_path = run / "story-evidence.json"
    evidence_bytes = json.dumps({
        "source_report": str(reports[0].relative_to(ROOT)).replace("\\", "/"),
        "source_manifest": str((run / PERFORMANCE_MANIFEST_NAME).relative_to(ROOT)).replace("\\", "/"),
        "audit_rule": (
            "source/input/report/config hashが現行一致する単一engine runのみ採用。"
            "PERF-03は90分、PERF-05は45分・10人・60秒以下の全員切断注入と"
            "生徒別復元集合を要求。PERF-01/02は現行のreview済みfield証拠があれば"
            "green補助run後も合格を保持し、反例は不合格。"
        ),
        "results": audited,
    }, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    _exclusive_regular_file_write(
        evidence_path, evidence_bytes, label="soak story evidence"
    )
    reference = str(evidence_path.relative_to(ROOT)).replace("\\", "/")
    ops = []
    field_proof_cache: FieldProofCache = {}
    nonfield_proof_cache = AuditProofCache()
    proof_dependencies: set[ProofDependency] = set()
    for story in ["PERF-01", "PERF-02", "PERF-03", "PERF-05"]:
        number = by_id[story]
        observed_pass = all(item["evaluations"][story] for item in audited)
        if story in AUDITED_PERFORMANCE:
            qualified = all(item["qualified"][story] for item in audited)
            # A complete current soak is the authoritative check for these
            # requirements. Keep prior findings in evidence, but let a later
            # sealed rerun update the status.
            if not qualified:
                status = "部分確認"
            else:
                status = "合格" if observed_pass else "不合格"
        else:
            if not observed_pass or cells.get(f"F{number}") == "不合格":
                status = "不合格"
            elif current_field_proof(
                story, cells.get(f"M{number}", ""), cache=field_proof_cache
            ):
                status = "合格"
            else:
                status = "部分確認"
        summary = "; ".join(
            f"{item['engine']} {item['minutes']}分 {item['students']}人: "
            f"{'監査条件内成功' if item['evaluations'][story] else '条件不足/反例あり'}"
            for item in audited
        )
        note = f"{reference} #{story}; {summary}; source manifest current。"
        if story == "PERF-01":
            note += " 学校i5/実授業音源/物理ブラウザ10台は未検証。"
        elif story == "PERF-02":
            note += (
                " 学校Windows 11/Core i5/8〜16GB級/実授業音源は未検証。"
                " 物理ブラウザはPERF-02条件外。現地validator v2は搭載RAM/OS可視RAMの"
                "raw bytes、Windows build/product type、CPUを別採取してレポートと照合する。"
            )
        if any(item["drift"]["excluded_post_duration_windows"] for item in audited):
            note += " 目標時間後drain窓を除外して再監査。"
        ops += [setcell(f"F{number}", value=status),
                setcell(f"M{number}", value=merged_evidence(cells.get(f"M{number}", ""), note)),
                setcell(f"O{number}", value=manifest["finished_at"])]
    _reaudit_all_field_passes(cells, ops, field_proof_cache)
    _reaudit_all_nonfield_passes(
        cells, ops, nonfield_proof_cache, proof_dependencies
    )
    proof_dependencies.update(
        _all_planned_pass_dependencies(
            cells,
            ops,
            exclude_stories={
                story
                for story, kind in AUDITED_PERFORMANCE.items()
                if kind == "soak"
            },
        )
    )
    watched_cells = (
        _proof_dependency_workbook_cells(proof_dependencies)
        | _nonfield_proof_workbook_cells(cells)
        | _field_proof_workbook_cells(cells)
    )
    if not _field_proof_cache_is_current(field_proof_cache):
        raise SystemExit(
            "field receipt/source/manifestがsoak同期中に変化したため台帳を変更しません。"
        )
    _require_current_performance_run(
        run=run,
        kind="soak",
        report=reports[0],
        expected_manifest=manifest,
        expected_audit=audited,
        evidence_path=evidence_path,
        evidence_bytes=evidence_bytes,
        expected_auditor_state=auditor_state,
        checkpoint="soak workbook backup",
    )
    _exclusive_workbook_backup(backup)

    def soak_commit_guard() -> None:
        current_run_witness = _require_current_performance_run(
            run=run,
            kind="soak",
            report=reports[0],
            expected_manifest=manifest,
            expected_audit=audited,
            evidence_path=evidence_path,
            evidence_bytes=evidence_bytes,
            expected_auditor_state=auditor_state,
            checkpoint="soak workbook replace",
        )
        if not _field_proof_cache_is_current(field_proof_cache):
            raise SystemExit("field proof changed before soak workbook replace")
        dependency_cache = _require_current_proof_dependencies(
            proof_dependencies, checkpoint="soak staged workbook replace"
        )
        _assert_workbook_targets_current(
            cells, ops, watched_cells=watched_cells
        )
        _require_final_witness_fence(
            checkpoint="soak staged workbook replace",
            proof_caches=(dependency_cache,),
            field_caches=(field_proof_cache,),
            performance_runs=(current_run_witness,),
        )

    soak_commit_guard()
    _guarded_batch(ops, soak_commit_guard)
    check()


def _audit_scale_report(report: dict, manifest: dict) -> dict:
    points = report.get("points") if isinstance(report, dict) else None
    if not isinstance(points, list) or not points:
        raise SystemExit("scaling reportに計測点がありません。")
    args = manifest["arguments"]
    if not (
        _same_number(report.get("minutes"), args.get("minutes"))
        and report.get("corpus") == args.get("corpus")
    ):
        raise SystemExit("scaling reportとmanifest引数が一致しません。")
    try:
        by_pair = {(int(point["students"]), int(point["lang_count"])): point for point in points}
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit("scaling reportの計測点形式が不正です。") from exc
    requested = {(student, langs) for student in args["students"] for langs in args["lang_counts"]}
    if set(by_pair) != requested or len(by_pair) != len(points):
        raise SystemExit("scaling reportの計測点がmanifestの行列と一致しません。")

    required_students = {1, 10, 20, 40}
    required_langs = {1, 2, 3, 5}
    required = {(student, langs) for student in required_students for langs in required_langs}
    qualified = (
        float(args["minutes"]) >= 2
        and required_students <= set(args["students"])
        and required_langs <= set(args["lang_counts"])
    )
    required_points = [by_pair[pair] for pair in sorted(required)] if required <= set(by_pair) else []
    configured_engine = args["engine"]
    modes_ok = bool(required_points) and all(
        point.get("mt_engine") == (configured_engine if point["lang_count"] <= 2 else "fake")
        and point.get("langs") == SCALE_LANGUAGES[: int(point["lang_count"])]
        for point in required_points
    )
    asr_engines = {point.get("asr_engine") for point in required_points}
    healthy = bool(required_points) and all(
        (
            not point.get("failed")
            and isinstance(point.get("asr_calls"), int) and point["asr_calls"] > 0
            and isinstance(point.get("mt_calls"), int) and point["mt_calls"] > 0
            and point.get("captions", 0) > 0
            and _scale_delivery_complete(point)
            and point.get("dropped_segments") == 0
            and point.get("frames_rejected") == 0
            and point.get("disconnects") == 0
            and point.get("reconnect_failures") == 0
            and point.get("crashes") == 0
            and not point.get("errors")
            and isinstance(point.get("langs"), list)
            and len(point["langs"]) == int(point["lang_count"])
            and point.get("active_langs") == min(int(point["students"]), int(point["lang_count"]))
            and _at_least(point.get("measurement_seconds"), float(args["minutes"]) * 60)
            and _at_least(
                point.get("ran_seconds"),
                _number(point.get("measurement_seconds")) + _number(args.get("drain_seconds")),
            )
            and isinstance(point.get("audio_bytes_planned"), int)
            and not isinstance(point.get("audio_bytes_planned"), bool)
            and point["audio_bytes_planned"] > 0
            and point.get("audio_bytes_sent") == point["audio_bytes_planned"]
            and point.get("measurement_students") == int(point["students"])
            and _expected_student_langs(point) is not None
            and point.get("measurement_langs") == _expected_student_langs(point)
            and isinstance(point.get("counter_snapshot"), dict)
            and point.get("counter_snapshot_at_s") == point["counter_snapshot"].get("at_s")
            and _at_least(
                point["counter_snapshot"].get("at_s"),
                _number(point.get("ran_seconds"))
                - PERFORMANCE_RESOURCE_INTERVAL_S * 2,
            )
            and point["counter_snapshot"].get("students") == int(point["students"])
            and point["counter_snapshot"].get("langs") == point.get("measurement_langs")
            and point["counter_snapshot"].get("queue_depth") == 0
            and point["counter_snapshot"].get("audio_queue_seconds") == 0
            and point["counter_snapshot"].get("mt_queue_depth") == 0
            and point["counter_snapshot"].get("asr_calls") == point.get("asr_calls")
            and point["counter_snapshot"].get("mt_calls") == point.get("mt_calls")
            and point["counter_snapshot"].get("audio_frames_rejected")
            == point.get("frames_rejected")
            and point["counter_snapshot"].get("asr_dropped_segments")
            == point.get("dropped_segments")
        )
        for point in required_points
    )
    asr_constant = bool(required_points) and all(
        len({by_pair[(student, langs)]["asr_calls"] for student in required_students}) == 1
        for langs in required_langs
    )
    mt_constant = bool(required_points) and all(
        len({
            by_pair[(student, langs)]["mt_calls"]
            for student in required_students
            if student >= langs
        }) == 1
        for langs in required_langs
    )
    asr_real = len(asr_engines) == 1 and asr_engines <= {"sherpa", "faster-whisper"}
    passed = qualified and modes_ok and asr_real and healthy and asr_constant and mt_constant
    return {
        "qualified": qualified,
        "passed": passed,
        "minutes_per_point": args["minutes"],
        "required_matrix": {"students": sorted(required_students), "lang_counts": sorted(required_langs)},
        "modes_ok": modes_ok,
        "asr_engines": sorted(str(value) for value in asr_engines),
        "asr_real": asr_real,
        "healthy": healthy,
        "asr_constant_by_student_count": asr_constant,
        "mt_constant_by_student_count": mt_constant,
    }


def sync_scale(run: Path) -> None:
    _require_import_code_current(checkpoint="scale sync")
    _require_safe_output_parent(run / "story-evidence.json", label="scale sync")
    auditor_state = _performance_auditor_state()
    reports = list(run.glob("*-scaling.json"))
    if len(reports) != 1:
        raise SystemExit("完了したscaling.jsonが1つ必要です。実行中は同期しません。")
    manifest = _performance_manifest(run, "scale", reports[0])
    try:
        report = _load_sealed_performance_json(reports[0], manifest)
    except (OSError, ValueError) as exc:
        raise SystemExit("scaling reportを読めません。") from exc
    audit_result = _audit_scale_report(report, manifest)

    backup = run / "feature-status-before-sync.xlsx"
    if os.path.lexists(backup):
        raise SystemExit("This scale run was already synced.")
    cells = read_cells()
    by_id = {value: int(key[1:]) for key, value in cells.items() if re.fullmatch(r"A\d+", key)}
    number = by_id["PERF-04"]
    # The newest complete, current and sealed scale matrix is authoritative;
    # merged_evidence below retains the prior failed-run history.
    if not audit_result["qualified"]:
        status = "部分確認"
    else:
        status = "合格" if audit_result["passed"] else "不合格"
    evidence_path = run / "story-evidence.json"
    evidence_bytes = json.dumps({
        "source_report": str(reports[0].relative_to(ROOT)).replace("\\", "/"),
        "source_manifest": str((run / PERFORMANCE_MANIFEST_NAME).relative_to(ROOT)).replace("\\", "/"),
        "audit_rule": (
            "source/input/report/config hashが現行一致し、2分以上×"
            "1/10/20/40人×1/2/3/5言語の16点が揃う場合だけPERF-04を昇格。"
            "1/2言語は指定実MT、3/5言語はfakeとして回数だけを監査。"
        ),
        "result": audit_result,
    }, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    _exclusive_regular_file_write(
        evidence_path, evidence_bytes, label="scale story evidence"
    )
    reference = str(evidence_path.relative_to(ROOT)).replace("\\", "/")
    note = (
        f"{reference} #PERF-04; {args_summary(manifest['arguments'])}; "
        f"{'監査条件内成功' if audit_result['passed'] else '条件不足/反例あり'}; source manifest current。"
    )
    ops = [setcell(f"F{number}", value=status),
           setcell(f"M{number}", value=merged_evidence(cells.get(f"M{number}", ""), note)),
           setcell(f"O{number}", value=manifest["finished_at"])]
    field_proof_cache: FieldProofCache = {}
    nonfield_proof_cache = AuditProofCache()
    proof_dependencies: set[ProofDependency] = set()
    _reaudit_all_field_passes(cells, ops, field_proof_cache)
    _reaudit_all_nonfield_passes(
        cells, ops, nonfield_proof_cache, proof_dependencies
    )
    proof_dependencies.update(
        _all_planned_pass_dependencies(
            cells,
            ops,
            exclude_stories={
                story
                for story, kind in AUDITED_PERFORMANCE.items()
                if kind == "scale"
            },
        )
    )
    watched_cells = (
        _proof_dependency_workbook_cells(proof_dependencies)
        | _nonfield_proof_workbook_cells(cells)
        | _field_proof_workbook_cells(cells)
    )
    if not _field_proof_cache_is_current(field_proof_cache):
        raise SystemExit(
            "field receipt/source/manifestがscale同期中に変化したため台帳を変更しません。"
        )
    _require_current_performance_run(
        run=run,
        kind="scale",
        report=reports[0],
        expected_manifest=manifest,
        expected_audit=audit_result,
        evidence_path=evidence_path,
        evidence_bytes=evidence_bytes,
        expected_auditor_state=auditor_state,
        checkpoint="scale workbook backup",
    )
    _exclusive_workbook_backup(backup)

    def scale_commit_guard() -> None:
        current_run_witness = _require_current_performance_run(
            run=run,
            kind="scale",
            report=reports[0],
            expected_manifest=manifest,
            expected_audit=audit_result,
            evidence_path=evidence_path,
            evidence_bytes=evidence_bytes,
            expected_auditor_state=auditor_state,
            checkpoint="scale workbook replace",
        )
        if not _field_proof_cache_is_current(field_proof_cache):
            raise SystemExit("field proof changed before scale workbook replace")
        dependency_cache = _require_current_proof_dependencies(
            proof_dependencies, checkpoint="scale staged workbook replace"
        )
        _assert_workbook_targets_current(
            cells, ops, watched_cells=watched_cells
        )
        _require_final_witness_fence(
            checkpoint="scale staged workbook replace",
            proof_caches=(dependency_cache,),
            field_caches=(field_proof_cache,),
            performance_runs=(current_run_witness,),
        )

    scale_commit_guard()
    _guarded_batch(ops, scale_commit_guard)
    check()


def args_summary(args: dict) -> str:
    return (
        f"{args['minutes']}分/点; students={','.join(map(str, args['students']))}; "
        f"langs={','.join(map(str, args['lang_counts']))}; engine={args['engine']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "init", "format", "run", "sync", "sync-browser", "sync-field", "sync-soak", "sync-scale",
            "audit", "check",
        ],
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--pytest-arg", action="append", default=[])
    parser.add_argument("--no-sync", action="store_true", help="Save evidence without concurrent workbook writes")
    parser.add_argument("--reviewer")
    parser.add_argument(
        "--approve-field-candidates",
        action="store_true",
        help="Confirm that the named reviewer inspected every field artifact",
    )
    args = parser.parse_args()
    if args.action == "init":
        initialize()
    elif args.action == "format":
        initialize(format_only=True)
    elif args.action == "run":
        if not BOOK.exists():
            raise SystemExit("Create the full story sheet before starting the test loop.")
        run = collect_run(args.pytest_arg)
        if not args.no_sync:
            sync(run)
        try:
            sealed = load_sealed_pytest_evidence(
                run, expected_complete=not args.no_sync
            )
            meta = _validated_pytest_run_payload(sealed.run_payload)
        except (EvidenceManifestError, OSError, ValueError, KeyError, TypeError) as exc:
            print(f"pytest producer evidence is not safely sealed: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        raise SystemExit(meta["exit_code"])
    elif args.action == "sync":
        assert args.run_dir is not None, "--run-dir required"
        sync(Path(os.path.abspath(args.run_dir)))
    elif args.action == "sync-browser":
        assert args.run_dir is not None, "--run-dir required"
        sync_browser(Path(os.path.abspath(args.run_dir)))
    elif args.action == "sync-field":
        assert args.run_dir is not None, "--run-dir required"
        sync_field(
            args.run_dir,
            reviewer=args.reviewer or "",
            approve_candidates=args.approve_field_candidates,
        )
    elif args.action == "audit":
        audit()
    elif args.action == "sync-soak":
        assert args.run_dir is not None, "--run-dir required"
        sync_soak(Path(os.path.abspath(args.run_dir)))
    elif args.action == "sync-scale":
        assert args.run_dir is not None, "--run-dir required"
        sync_scale(Path(os.path.abspath(args.run_dir)))
    else:
        check()


if __name__ == "__main__":
    main()
