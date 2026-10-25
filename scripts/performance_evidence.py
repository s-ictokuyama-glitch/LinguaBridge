"""Performance-run provenance shared by the replay, scale, and tracker tools.

The manifest is intentionally independent from the measured report.  A run is
adoptable only when the tracker can recompute the exact source/input snapshot,
verify the effective configs and report bytes, and see that the snapshot did
not change while the run was in progress.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from datetime import datetime
from email.parser import BytesParser
from email.policy import compat32
import hashlib
from importlib import metadata, util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

_IMPORT_PYCACHE_DIRECTORY: tempfile.TemporaryDirectory[str] | None = None
_DirectoryChainIdentity = tuple[tuple[str, int, int], ...]
_CachePrefixIdentity = tuple[
    _DirectoryChainIdentity,
    tuple[int, int, int, int, int, int],
]


class _WindowsFileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", ctypes.c_ulong),
    ]


def _path_change_time_ns(path: Path) -> int:
    """Return Windows ChangeTime, not CPython's creation-time ``st_ctime``."""

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
        0x0080,  # FILE_READ_ATTRIBUTES
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x02000000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    if handle in {None, ctypes.c_void_p(-1).value}:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = _WindowsFileBasicInfo()
        if not get_information(
            handle,
            0,  # FileBasicInfo
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(information.ChangeTime) * 100
    finally:
        close_handle(handle)


def _directory_chain_identity(path: Path) -> _DirectoryChainIdentity | None:
    """Snapshot every path component, rejecting all pathname indirection."""

    absolute = Path(os.path.abspath(path))
    parts = absolute.parts
    if not parts:
        return None
    components: list[Path] = [Path(parts[0])]
    current = components[0]
    for part in parts[1:]:
        current /= part
        components.append(current)
    def snapshot() -> _DirectoryChainIdentity | None:
        result: list[tuple[str, int, int]] = []
        try:
            for component in components:
                state = component.lstat()
                attributes = getattr(state, "st_file_attributes", 0)
                if (
                    not stat.S_ISDIR(state.st_mode)
                    or stat.S_ISLNK(state.st_mode)
                    or bool(
                        attributes
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    )
                    or bool(
                        getattr(os.path, "isjunction", lambda _path: False)(
                            component
                        )
                    )
                ):
                    return None
                # Ancestor membership is expected to change in shared locations
                # such as %TEMP%.  Bind pathname identity, not mutable metadata.
                result.append((str(component), state.st_dev, state.st_ino))
        except OSError:
            return None
        return tuple(result)

    before = snapshot()
    return before if before is not None and snapshot() == before else None


def _cache_prefix_identity(path: Path) -> _CachePrefixIdentity | None:
    chain_before = _directory_chain_identity(path)
    if chain_before is None:
        return None
    try:
        before = path.lstat()
        before_leaf = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            _path_change_time_ns(path),
        )
        if any(path.iterdir()):
            return None
        after = path.lstat()
        after_leaf = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            _path_change_time_ns(path),
        )
    except OSError:
        return None
    chain_after = _directory_chain_identity(path)
    if (
        chain_after is None
        or chain_after != chain_before
        or after_leaf != before_leaf
    ):
        return None
    return chain_after, after_leaf


def _safe_empty_cache_prefix(
    value: object,
    *,
    expected_identity: _CachePrefixIdentity | None = None,
) -> Path | None:
    """Return an existing safe cache prefix without taking ownership of it."""

    if not isinstance(value, str) or not value:
        return None
    path = Path(os.path.abspath(value))
    identity = _cache_prefix_identity(path)
    if identity is None or (
        expected_identity is not None and identity != expected_identity
    ):
        return None
    return path


_IMPORT_PYCACHE_ROOT = (
    _safe_empty_cache_prefix(sys.pycache_prefix)
    if sys.dont_write_bytecode
    else None
)
if _IMPORT_PYCACHE_ROOT is None:
    _IMPORT_PYCACHE_DIRECTORY = tempfile.TemporaryDirectory(
        prefix="linguabridge-performance-import-pycache-"
    )
    _IMPORT_PYCACHE_ROOT = _safe_empty_cache_prefix(
        _IMPORT_PYCACHE_DIRECTORY.name
    )
    if _IMPORT_PYCACHE_ROOT is None:
        raise RuntimeError(
            "performance import pycache prefix is not a fresh real directory"
        )
    sys.pycache_prefix = str(_IMPORT_PYCACHE_ROOT)
_IMPORT_PYCACHE_IDENTITY = _cache_prefix_identity(_IMPORT_PYCACHE_ROOT)
if _IMPORT_PYCACHE_IDENTITY is None:
    raise RuntimeError("performance import pycache prefix identity is unstable")
sys.dont_write_bytecode = True


def _venv_site_packages() -> Path:
    """Resolve the sole project-venv site directory without invoking ``site``."""

    executable = Path(os.path.abspath(sys.executable))
    venv_root = executable.parent.parent
    config = venv_root / "pyvenv.cfg"
    if not config.is_file():
        raise RuntimeError("qualified performance producer requires a virtualenv")
    try:
        values = {
            key.strip().lower(): value.strip().lower()
            for line in config.read_text(encoding="utf-8").splitlines()
            if "=" in line
            for key, value in (line.split("=", 1),)
        }
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("virtualenv configuration cannot be audited") from exc
    if values.get("include-system-site-packages", "false") != "false":
        raise RuntimeError(
            "qualified performance producer forbids system site-packages"
        )
    candidates = (
        venv_root / "Lib" / "site-packages",
        venv_root
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages",
    )
    existing = [path for path in candidates if path.is_dir()]
    if len(existing) != 1:
        raise RuntimeError("virtualenv site-packages is missing or ambiguous")
    site_packages = Path(os.path.abspath(existing[0]))
    if _directory_chain_identity(site_packages) is None:
        raise RuntimeError(
            "virtualenv site-packages and its ancestors must be real directories"
        )
    return site_packages


def _isolated_base_paths() -> tuple[Path, ...]:
    """Return CPython's exact Windows ``-I -S`` standard-library path."""

    base = Path(os.path.abspath(sys.base_prefix))
    if os.name != "nt":
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        return (
            base / "lib" / f"{version.replace('.', '')}.zip",
            base / "lib" / version,
            base / "lib" / version / "lib-dynload",
        )
    return (
        base / f"python{sys.version_info.major}{sys.version_info.minor}.zip",
        base / "DLLs",
        base / "Lib",
        base,
    )


def _expected_qualified_sys_path(root: Path) -> tuple[str, ...]:
    return tuple(
        str(path)
        for path in (
            *_isolated_base_paths(),
            _venv_site_packages(),
            Path(os.path.abspath(root)),
        )
    )


_PERFORMANCE_ROOT = Path(os.path.abspath(Path(__file__).parent.parent))
_QUALIFIED_FLAGS = bool(
    sys.flags.isolated
    and sys.flags.no_site
    and sys.flags.ignore_environment
    and sys.flags.safe_path
    and sys.dont_write_bytecode
)
_QUALIFIED_PATH_IDENTITIES: tuple[
    _DirectoryChainIdentity, _DirectoryChainIdentity
] | None = None
if _QUALIFIED_FLAGS:
    _qualified_site = _venv_site_packages()
    _qualified_site_identity = _directory_chain_identity(_qualified_site)
    _qualified_root_identity = _directory_chain_identity(_PERFORMANCE_ROOT)
    if (
        _qualified_site_identity is not None
        and _qualified_root_identity is not None
    ):
        _QUALIFIED_PATH_IDENTITIES = (
            _qualified_site_identity,
            _qualified_root_identity,
        )
_QUALIFIED_PRODUCER_BOOTSTRAP = bool(
    _QUALIFIED_FLAGS
    and _QUALIFIED_PATH_IDENTITIES is not None
    and _safe_empty_cache_prefix(
        sys.pycache_prefix,
        expected_identity=_IMPORT_PYCACHE_IDENTITY,
    )
    is not None
    and tuple(sys.path) == _expected_qualified_sys_path(_PERFORMANCE_ROOT)
)
_PREIMPORT_MODULE_NAMES = frozenset(sys.modules)
_PERFORMANCE_CRITICAL_PREFIXES = (
    "annotated_types",
    "packaging",
    "pydantic",
    "pydantic_core",
    "server",
    "typing_extensions",
    "typing_inspection",
    "yaml",
)
_PRELOADED_CRITICAL_MODULES = frozenset(
    name
    for name in _PREIMPORT_MODULE_NAMES
    if any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in _PERFORMANCE_CRITICAL_PREFIXES
    )
)


def require_qualified_producer_bootstrap() -> None:
    """Reject evidence production from a process exposed to startup imports."""

    if not _QUALIFIED_PRODUCER_BOOTSTRAP or _PRELOADED_CRITICAL_MODULES:
        raise PerformanceEvidenceError(
            "performance evidence production requires a fresh -I -S interpreter "
            "with no preloaded runtime modules"
        )
    if _safe_empty_cache_prefix(
        sys.pycache_prefix,
        expected_identity=_IMPORT_PYCACHE_IDENTITY,
    ) is None:
        raise PerformanceEvidenceError(
            "performance evidence import cache changed after bootstrap"
        )
    if _QUALIFIED_PATH_IDENTITIES is not None:
        site_identity = _directory_chain_identity(_venv_site_packages())
        root_identity = _directory_chain_identity(_PERFORMANCE_ROOT)
        if (
            (site_identity, root_identity) != _QUALIFIED_PATH_IDENTITIES
            or tuple(sys.path) != _expected_qualified_sys_path(_PERFORMANCE_ROOT)
        ):
            raise PerformanceEvidenceError(
                "performance evidence import paths changed after bootstrap"
            )

import yaml  # noqa: E402
from packaging.markers import default_environment  # noqa: E402
from packaging.requirements import InvalidRequirement, Requirement  # noqa: E402
from packaging.version import InvalidVersion, Version  # noqa: E402

import server.config as server_config_module  # noqa: E402
from server.config import AppConfig, load_config  # noqa: E402


MANIFEST_NAME = "source-manifest.json"
MANIFEST_SCHEMA = 5
PERFORMANCE_KINDS = {"soak", "scale"}

_COMMON_DISTRIBUTIONS = {
    "anyio",
    "fastapi",
    "numpy",
    "packaging",
    "psutil",
    "pydantic",
    "PyYAML",
    "starlette",
    "uvicorn",
    "websockets",
}
_DISTRIBUTION_EXTRAS = {"uvicorn": {"standard"}}
_ASR_DISTRIBUTIONS = {
    "fake": set(),
    "faster-whisper": {"ctranslate2", "faster-whisper", "onnxruntime"},
    "sherpa": {"onnxruntime", "sherpa-onnx", "sherpa-onnx-core"},
}
_MT_DISTRIBUTIONS = {
    "fake": set(),
    "hy-mt2": {"llama-cpp-python"},
    "nllb": {"ctranslate2", "sentencepiece", "tokenizers", "transformers"},
}


@contextmanager
def fresh_python_child_environment() -> Iterator[dict[str, str]]:
    """Yield a sanitized environment plus a private cache prefix for ``-I``."""

    with tempfile.TemporaryDirectory(
        prefix="linguabridge-child-import-pycache-"
    ) as directory:
        prefix = Path(os.path.abspath(directory))
        prefix_state = prefix.stat(follow_symlinks=False)
        if (
            stat.S_ISLNK(prefix_state.st_mode)
            or not stat.S_ISDIR(prefix_state.st_mode)
            or bool(
                getattr(prefix_state, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            or bool(getattr(prefix, "is_junction", lambda: False)())
            or any(prefix.iterdir())
        ):
            raise RuntimeError(
                "child import pycache prefix is not a fresh real directory"
            )
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("PYTHON")
        }
        environment["LINGUABRIDGE_CHILD_PYCACHE_PREFIX"] = str(prefix)
        yield environment


_ISOLATED_MODULE_BOOTSTRAP = """\
import os
import sys

root = os.path.abspath(sys.argv[1])
site_packages = os.path.abspath(sys.argv[2])
prefix_device, prefix_inode = map(int, sys.argv[3:5])
root_device, root_inode = map(int, sys.argv[5:7])
site_device, site_inode = map(int, sys.argv[7:9])
module = sys.argv[9]
module_args = sys.argv[10:]
if not (
    sys.flags.isolated
    and sys.flags.no_site
    and sys.flags.ignore_environment
    and sys.flags.safe_path
    and sys.dont_write_bytecode
    and isinstance(sys.pycache_prefix, str)
    and sys.pycache_prefix
):
    raise RuntimeError("server bootstrap is not isolated")
if any(key.upper().startswith("PYTHON") for key in os.environ):
    raise RuntimeError("server bootstrap inherited a Python injection variable")
base = os.path.abspath(sys.base_prefix)
if os.name == "nt":
    expected_base = (
        os.path.join(base, f"python{sys.version_info.major}{sys.version_info.minor}.zip"),
        os.path.join(base, "DLLs"),
        os.path.join(base, "Lib"),
        base,
    )
else:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    expected_base = (
        os.path.join(base, "lib", version.replace(".", "") + ".zip"),
        os.path.join(base, "lib", version),
        os.path.join(base, "lib", version, "lib-dynload"),
    )
if tuple(sys.path) != expected_base:
    raise RuntimeError("server bootstrap base path is not exact")

def check_real_directory(path, expected_device, expected_inode, require_empty=False):
    absolute = os.path.abspath(path)
    drive, tail = os.path.splitdrive(absolute)
    parts = [part for part in tail.replace("\\\\", "/").split("/") if part]
    current = drive + os.sep if drive else os.sep
    for part in parts:
        current = os.path.join(current, part)
        state = os.lstat(current)
        attributes = getattr(state, "st_file_attributes", 0)
        if (
            state.st_mode & 0o170000 != 0o040000
            or attributes & 0x400
            or getattr(os.path, "isjunction", lambda _path: False)(current)
        ):
            raise RuntimeError("server bootstrap path is indirect")
    before = os.lstat(absolute)
    if (before.st_dev, before.st_ino) != (expected_device, expected_inode):
        raise RuntimeError("server bootstrap path identity changed")
    if require_empty:
        with os.scandir(absolute) as entries:
            if next(entries, None) is not None:
                raise RuntimeError("server bootstrap cache is not empty")
    after = os.lstat(absolute)
    if (after.st_dev, after.st_ino) != (expected_device, expected_inode):
        raise RuntimeError("server bootstrap path identity changed")

prefix = os.path.abspath(sys.pycache_prefix)
if prefix != os.path.abspath(os.environ.get("LINGUABRIDGE_CHILD_PYCACHE_PREFIX", "")):
    raise RuntimeError("server bootstrap cache identity is inconsistent")
check_real_directory(prefix, prefix_device, prefix_inode, True)
check_real_directory(root, root_device, root_inode)
check_real_directory(site_packages, site_device, site_inode)
sys.path.extend((site_packages, root))
sys.argv = [module, *module_args]
import runpy
runpy.run_module(module, run_name="__main__", alter_sys=True)
"""


def isolated_python_module_command(
    module: str,
    *,
    root: Path,
    environment: Mapping[str, str],
    arguments: Iterable[str] = (),
) -> list[str]:
    """Build the only qualified command used for measured Python children."""

    prefix = _safe_empty_cache_prefix(
        environment.get("LINGUABRIDGE_CHILD_PYCACHE_PREFIX")
    )
    if prefix is None:
        raise RuntimeError("child import pycache prefix is not safe")
    selected_root = Path(os.path.abspath(root))
    site_packages = _venv_site_packages()
    if (
        _directory_chain_identity(selected_root) is None
        or _directory_chain_identity(site_packages) is None
    ):
        raise RuntimeError("child import path is not a real directory chain")
    prefix_state = prefix.lstat()
    root_state = selected_root.lstat()
    site_state = site_packages.lstat()
    return [
        sys.executable,
        "-I",
        "-S",
        "-B",
        "-X",
        f"pycache_prefix={prefix}",
        "-c",
        _ISOLATED_MODULE_BOOTSTRAP,
        str(selected_root),
        str(site_packages),
        str(prefix_state.st_dev),
        str(prefix_state.st_ino),
        str(root_state.st_dev),
        str(root_state.st_ino),
        str(site_state.st_dev),
        str(site_state.st_ino),
        module,
        *arguments,
    ]

_COMMON_SOURCES = (
    "scripts/__init__.py",
    "scripts/acceptance.py",
    "scripts/bench.py",
    "scripts/performance_evidence.py",
    "scripts/replay_client.py",
    "scripts/story_tracker.py",
    "scripts/text_metrics.py",
    "config.yaml",
    "docs/qa/stories.tsv",
)
_KIND_SOURCES = {
    "soak": (),
    "scale": ("scripts/scale_bench.py",),
}
# The tracker is read-locked and hashed while a measurement runs, but it does
# not produce the measured report.  Completed reports are deliberately audited
# again by the current tracker, so only this audit-source hash may drift later.
_REAUDITED_SOURCES = {
    "scripts/story_tracker.py",
}
_ARGUMENT_KEYS = {
    "soak": (
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
    ),
    "scale": (
        "minutes",
        "students",
        "lang_counts",
        "corpus",
        "engine",
        "port",
        "drain_seconds",
        "out_dir",
    ),
}


class PerformanceEvidenceError(ValueError):
    """The run cannot be tied to the current source and measured artifacts."""


FileRevision = tuple[int, int, int, int, int, int]


def _absolute_path(path: Path) -> Path:
    """Return an absolute spelling without resolving a link component."""

    return Path(os.path.abspath(os.fspath(path)))


def _file_revision(
    value: os.stat_result, *, change_time_ns: int
) -> FileRevision:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        change_time_ns,
    )


def _snapshot_path_revision(path: Path) -> tuple[os.stat_result, FileRevision]:
    """Capture stat and ChangeTime at one stable observation point."""

    before = path.lstat()
    before_change = _path_change_time_ns(path)
    after = path.lstat()
    after_change = _path_change_time_ns(path)
    before_revision = _file_revision(before, change_time_ns=before_change)
    after_revision = _file_revision(after, change_time_ns=after_change)
    if before_revision != after_revision:
        raise OSError(f"filesystem entry changed while inspected: {path}")
    return after, after_revision


def _is_link_or_reparse(path: Path, value: os.stat_result | None = None) -> bool:
    """Reject pathname indirection, including Windows reparse points."""

    try:
        identity = value or path.lstat()
        is_junction = getattr(os.path, "isjunction", lambda _path: False)
        file_attributes = getattr(identity, "st_file_attributes", 0)
        reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return (
            stat.S_ISLNK(identity.st_mode)
            or bool(is_junction(path))
            or bool(file_attributes & reparse_mask)
        )
    except OSError:
        return True


def _assert_no_link_components(path: Path, *, description: str) -> Path:
    absolute = _absolute_path(path)
    parts = absolute.parts
    if not parts:
        raise PerformanceEvidenceError(f"{description} cannot be inspected")
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        try:
            identity = current.lstat()
        except OSError as exc:
            raise PerformanceEvidenceError(
                f"{description} cannot be inspected: {current}"
            ) from exc
        if _is_link_or_reparse(current, identity):
            raise PerformanceEvidenceError(
                f"{description} must not traverse a link, junction, or reparse point: {current}"
            )
    return absolute


def _direct_membership_digest(path: Path, *, description: str) -> str:
    """Hash the exact direct child names and filesystem entry types."""

    records: list[tuple[str, str]] = []
    try:
        entries = list(path.iterdir())
    except OSError as exc:
        raise PerformanceEvidenceError(
            f"cannot enumerate {description}: {path}"
        ) from exc
    for entry in entries:
        try:
            identity = entry.lstat()
        except OSError as exc:
            raise PerformanceEvidenceError(
                f"{description} changed during validation"
            ) from exc
        if _is_link_or_reparse(entry, identity):
            kind = "link-or-reparse"
        elif stat.S_ISREG(identity.st_mode):
            kind = "file"
        elif stat.S_ISDIR(identity.st_mode):
            kind = "directory"
        else:
            kind = "other"
        records.append((entry.name, kind))
    canonical = json.dumps(
        sorted(records), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _stable_sha256(
    path: Path,
    *,
    description: str,
    expected_revision: FileRevision | None = None,
) -> tuple[str, FileRevision]:
    """Hash one stable regular file without trusting timestamps as content."""

    absolute = _assert_no_link_components(path, description=description)
    try:
        before, before_revision = _snapshot_path_revision(absolute)
        if (
            not stat.S_ISREG(before.st_mode)
            or _is_link_or_reparse(absolute, before)
            or (
                expected_revision is not None
                and before_revision != expected_revision
            )
        ):
            raise PerformanceEvidenceError(
                f"{description} changed during validation"
            )
        digest = hashlib.sha256()
        with absolute.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        after, after_revision = _snapshot_path_revision(absolute)
    except PerformanceEvidenceError:
        raise
    except OSError as exc:
        raise PerformanceEvidenceError(
            f"{description} changed during validation"
        ) from exc
    if (
        before_revision != after_revision
        or not stat.S_ISREG(after.st_mode)
        or _is_link_or_reparse(absolute, after)
        or (
            expected_revision is not None
            and after_revision != expected_revision
        )
    ):
        raise PerformanceEvidenceError(f"{description} changed during validation")
    return digest.hexdigest(), after_revision


def _recheck_directory(
    path: Path,
    expected: FileRevision,
    expected_membership_sha256: str,
    *,
    description: str,
) -> None:
    absolute = _assert_no_link_components(path, description=description)
    try:
        before, before_revision = _snapshot_path_revision(absolute)
        membership_sha256 = _direct_membership_digest(
            absolute, description=description
        )
        current, current_revision = _snapshot_path_revision(absolute)
    except OSError as exc:
        raise PerformanceEvidenceError(
            f"{description} changed during validation"
        ) from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or _is_link_or_reparse(absolute, current)
        or before_revision != expected
        or current_revision != expected
        or membership_sha256 != expected_membership_sha256
    ):
        raise PerformanceEvidenceError(f"{description} changed during validation")


@dataclass(frozen=True)
class _FileWitness:
    path: Path
    revision: FileRevision
    sha256: str
    description: str


@dataclass(frozen=True)
class _DirectoryWitness:
    path: Path
    revision: FileRevision
    membership_sha256: str
    description: str


@dataclass(frozen=True)
class ValidationWitness:
    """Immutable, non-serialized proof that validated bytes are still current."""

    files: tuple[_FileWitness, ...]
    directories: tuple[_DirectoryWitness, ...]

    def recheck(self) -> None:
        for file_item in self.files:
            digest, _ = _stable_sha256(
                file_item.path,
                description=file_item.description,
                expected_revision=file_item.revision,
            )
            if digest != file_item.sha256:
                raise PerformanceEvidenceError(
                    f"{file_item.description} changed during validation"
                )
        _assert_imported_local_modules_current()
        for directory_item in self.directories:
            _recheck_directory(
                directory_item.path,
                directory_item.revision,
                directory_item.membership_sha256,
                description=directory_item.description,
            )


class ValidatedPerformanceManifest(dict[str, Any]):
    """A normal manifest mapping carrying a non-serialized validation fence."""

    __slots__ = ("_validation_witness",)
    _validation_witness: ValidationWitness

    def __init__(self, payload: Mapping[str, Any], witness: ValidationWitness) -> None:
        super().__init__(payload)
        object.__setattr__(self, "_validation_witness", witness)

    @property
    def validation_witness(self) -> ValidationWitness:
        return self._validation_witness

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("validated performance witness is immutable")


def recheck_validation_witness(
    validated: ValidatedPerformanceManifest | ValidationWitness,
) -> None:
    """Lightweight caller-side CAS after downstream report validation."""

    if isinstance(validated, ValidatedPerformanceManifest):
        witness = validated.validation_witness
    elif isinstance(validated, ValidationWitness):
        witness = validated
    else:
        raise PerformanceEvidenceError(
            "a validated performance manifest or witness is required for recheck"
        )
    witness.recheck()


@dataclass(frozen=True)
class _ReadFile:
    value: bytes | None
    revision: FileRevision
    sha256: str
    description: str


class ValidationReadSet:
    """Bind all bytes and directory enumerations used by one validation."""

    def __init__(self) -> None:
        self._files: dict[Path, _ReadFile] = {}
        self._directories: dict[Path, _DirectoryWitness] = {}

    def watch_directory(self, path: Path, *, description: str) -> Path:
        absolute = _assert_no_link_components(path, description=description)
        previous = self._directories.get(absolute)
        if previous is not None:
            _recheck_directory(
                absolute,
                previous.revision,
                previous.membership_sha256,
                description=previous.description,
            )
            return absolute
        try:
            identity, revision = _snapshot_path_revision(absolute)
        except OSError as exc:
            raise PerformanceEvidenceError(
                f"{description} cannot be inspected"
            ) from exc
        if not stat.S_ISDIR(identity.st_mode) or _is_link_or_reparse(absolute, identity):
            raise PerformanceEvidenceError(
                f"{description} must be a real directory, not a link, junction, or reparse point"
            )
        membership_sha256 = _direct_membership_digest(
            absolute, description=description
        )
        try:
            after, after_revision = _snapshot_path_revision(absolute)
        except OSError as exc:
            raise PerformanceEvidenceError(
                f"{description} changed during validation"
            ) from exc
        if after_revision != revision:
            raise PerformanceEvidenceError(
                f"{description} changed during validation"
            )
        self._directories[absolute] = _DirectoryWitness(
            absolute,
            revision,
            membership_sha256,
            description,
        )
        return absolute

    def directory_membership_sha256(
        self, path: Path, *, description: str
    ) -> str:
        absolute = self.watch_directory(path, description=description)
        return self._directories[absolute].membership_sha256

    def read_bytes(self, path: Path, *, description: str) -> bytes:
        absolute = _absolute_path(path)
        self.watch_directory(
            absolute.parent,
            description=f"{description} parent directory",
        )
        previous = self._files.get(absolute)
        if previous is not None:
            digest, _ = _stable_sha256(
                absolute,
                description=previous.description,
                expected_revision=previous.revision,
            )
            if digest != previous.sha256:
                raise PerformanceEvidenceError(
                    f"{previous.description} changed during validation"
                )
            if previous.value is None:
                value = absolute.read_bytes()
                digest_after, _ = _stable_sha256(
                    absolute,
                    description=previous.description,
                    expected_revision=previous.revision,
                )
                if (
                    hashlib.sha256(value).hexdigest() != digest_after
                    or digest_after != previous.sha256
                ):
                    raise PerformanceEvidenceError(
                        f"{previous.description} changed during validation"
                    )
                self._files[absolute] = _ReadFile(
                    value,
                    previous.revision,
                    previous.sha256,
                    previous.description,
                )
                return value
            return previous.value
        absolute = _assert_no_link_components(absolute, description=description)
        try:
            before, before_revision = _snapshot_path_revision(absolute)
            if not stat.S_ISREG(before.st_mode) or _is_link_or_reparse(absolute, before):
                raise PerformanceEvidenceError(
                    f"{description} must be a regular file, not a link, junction, or reparse point"
                )
            value = absolute.read_bytes()
            after, after_revision = _snapshot_path_revision(absolute)
        except PerformanceEvidenceError:
            raise
        except OSError as exc:
            raise PerformanceEvidenceError(f"cannot read {description}") from exc
        if (
            before_revision != after_revision
            or not stat.S_ISREG(after.st_mode)
            or _is_link_or_reparse(absolute, after)
        ):
            raise PerformanceEvidenceError(
                f"{description} changed during validation"
            )
        digest = hashlib.sha256(value).hexdigest()
        self._files[absolute] = _ReadFile(
            value,
            after_revision,
            digest,
            description,
        )
        return value

    def sha256(self, path: Path, *, description: str) -> str:
        absolute = _absolute_path(path)
        self.watch_directory(
            absolute.parent,
            description=f"{description} parent directory",
        )
        previous = self._files.get(absolute)
        if previous is not None:
            digest, _ = _stable_sha256(
                absolute,
                description=previous.description,
                expected_revision=previous.revision,
            )
            if digest != previous.sha256:
                raise PerformanceEvidenceError(
                    f"{previous.description} changed during validation"
                )
            return digest
        digest, revision = _stable_sha256(
            absolute, description=description
        )
        self._files[absolute] = _ReadFile(
            None,
            revision,
            digest,
            description,
        )
        return digest

    def walk_regular_files(self, path: Path, *, description: str) -> list[Path]:
        root = self.watch_directory(path, description=description)
        files: list[Path] = []
        pending = [root]
        while pending:
            directory = pending.pop()
            self.watch_directory(directory, description=f"{description} directory")
            try:
                entries = list(directory.iterdir())
            except OSError as exc:
                raise PerformanceEvidenceError(
                    f"cannot enumerate {description}: {directory}"
                ) from exc
            for entry in entries:
                absolute = _assert_no_link_components(entry, description=description)
                identity = absolute.lstat()
                if _is_link_or_reparse(absolute, identity):
                    raise PerformanceEvidenceError(
                        f"{description} must not contain a link, junction, or reparse point"
                    )
                if stat.S_ISDIR(identity.st_mode):
                    self.watch_directory(
                        absolute,
                        description=f"{description} directory",
                    )
                    pending.append(absolute)
                elif stat.S_ISREG(identity.st_mode):
                    files.append(absolute)
                else:
                    raise PerformanceEvidenceError(
                        f"{description} contains a non-regular filesystem entry: {absolute}"
                    )
            current = self._directories[directory]
            _recheck_directory(
                directory,
                current.revision,
                current.membership_sha256,
                description=current.description,
            )
        return sorted(files, key=lambda candidate: candidate.as_posix())

    def freeze(self) -> ValidationWitness:
        witness = ValidationWitness(
            files=tuple(
                _FileWitness(
                    path,
                    item.revision,
                    item.sha256,
                    item.description,
                )
                for path, item in sorted(
                    self._files.items(), key=lambda pair: pair[0].as_posix()
                )
            ),
            directories=tuple(
                item
                for _, item in sorted(
                    self._directories.items(), key=lambda pair: pair[0].as_posix()
                )
            ),
        )
        witness.recheck()
        return witness


def _capture_imported_module_state(path: Path, *, description: str) -> _FileWitness:
    absolute = _absolute_path(path)
    digest, revision = _stable_sha256(absolute, description=description)
    return _FileWitness(absolute, revision, digest, description)


def capture_imported_module_state(*modules: object) -> tuple[_FileWitness, ...]:
    """Fingerprint already-imported module files for a producer entrypoint."""

    result: list[_FileWitness] = []
    seen: set[Path] = set()
    for module in modules:
        raw_path = getattr(module, "__file__", None)
        module_name = getattr(module, "__name__", type(module).__name__)
        if not isinstance(raw_path, str) or not raw_path:
            raise PerformanceEvidenceError(
                f"loaded module has no source file: {module_name}"
            )
        path = _absolute_path(Path(raw_path))
        if path in seen:
            continue
        seen.add(path)
        result.append(
            _capture_imported_module_state(
                path,
                description=f"loaded module {module_name}",
            )
        )
    return tuple(result)


def recheck_imported_module_state(state: Iterable[_FileWitness]) -> None:
    """Require an import-time producer code snapshot to remain current."""

    for item in tuple(state):
        if not isinstance(item, _FileWitness):
            raise PerformanceEvidenceError("loaded module state is invalid")
        digest, _ = _stable_sha256(
            item.path,
            description=item.description,
            expected_revision=item.revision,
        )
        if digest != item.sha256:
            raise PerformanceEvidenceError(
                f"{item.description} changed after import"
            )


_expected_scripts_package_path = _absolute_path(
    _PERFORMANCE_ROOT / "scripts" / "__init__.py"
)
_scripts_package = sys.modules.get("scripts")
_scripts_package_path = getattr(_scripts_package, "__file__", None)
_scripts_package_spec = getattr(_scripts_package, "__spec__", None)
_scripts_package_origin = getattr(_scripts_package_spec, "origin", None)
_scripts_package_search_path = getattr(_scripts_package, "__path__", None)
if (
    not isinstance(_scripts_package_path, str)
    or _absolute_path(Path(_scripts_package_path)) != _expected_scripts_package_path
    or not isinstance(_scripts_package_origin, str)
    or _absolute_path(Path(_scripts_package_origin)) != _expected_scripts_package_path
    or _scripts_package_search_path is None
    or tuple(_absolute_path(Path(item)) for item in _scripts_package_search_path)
    != (_expected_scripts_package_path.parent,)
):
    raise PerformanceEvidenceError(
        "loaded scripts package does not match the repository package"
    )

_IMPORTED_LOCAL_PATHS: list[tuple[Path, str]] = [
    (Path(__file__), "loaded performance evidence module"),
    (Path(server_config_module.__file__), "loaded server config module"),
    (_expected_scripts_package_path, "loaded scripts package"),
]
_IMPORTED_LOCAL_MODULE_STATE = tuple(
    _capture_imported_module_state(path, description=description)
    for path, description in _IMPORTED_LOCAL_PATHS
)


def _assert_imported_local_modules_current() -> None:
    """Refuse decisions made by stale already-imported repository code."""

    recheck_imported_module_state(_IMPORTED_LOCAL_MODULE_STATE)


# Locks intentionally live in the harness process for the whole measurement.
# A crashed process closes its OS handles, but the incomplete manifest cannot be
# completed by a later process because that process has no entry in this table.
_RUN_LOCKS: dict[Path, list[tuple[Path, Any]]] = {}
_RUN_DIRECTORY_WITNESSES: dict[Path, tuple[_DirectoryWitness, ...]] = {}
_RUN_MANIFEST_WITNESSES: dict[Path, _FileWitness] = {}


def _lock_mode() -> str:
    return "windows-share-read" if os.name == "nt" else "posix-advisory"


def _require_qualified_lock_mode(mode: str) -> None:
    if mode != "windows-share-read":
        raise PerformanceEvidenceError(
            "qualified performance evidence requires Windows mandatory read locks; "
            f"{mode} cannot prove measurement-time immutability"
        )


def _open_read_lock(path: Path) -> Any:
    """Open one file so Windows rejects writes, deletes, and renames.

    ``FILE_SHARE_READ`` still lets the model/server open the bytes for loading,
    while deliberately omitting WRITE and DELETE sharing.  POSIX has no
    equivalent mandatory share mode, so the fallback is accurately labelled as
    advisory in the manifest rather than over-claiming continuous immutability.
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001,  # FILE_SHARE_READ (no write/delete sharing)
            None,
            3,  # OPEN_EXISTING
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            error = ctypes.get_last_error()
            raise OSError(error, f"性能run入力をread-lockできません: {path}")
        return int(handle)

    file = path.open("rb")
    try:
        import fcntl

        fcntl.flock(  # type: ignore[attr-defined]
            file.fileno(),
            fcntl.LOCK_SH | fcntl.LOCK_NB,  # type: ignore[attr-defined]
        )
    except Exception:
        file.close()
        raise
    return file


def _close_read_lock(lock: Any) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        close_handle(lock)
    else:
        lock.close()


def _acquire_run_locks(manifest_path: Path, files: Iterable[Path]) -> None:
    key = manifest_path.resolve()
    held = _RUN_LOCKS.setdefault(key, [])
    already = {path for path, _ in held}
    try:
        for path in sorted({item.resolve() for item in files}, key=lambda item: item.as_posix()):
            if path in already:
                continue
            if not path.is_file():
                raise PerformanceEvidenceError(f"read-lock対象がありません: {path}")
            held.append((path, _open_read_lock(path)))
            already.add(path)
    except Exception:
        _release_run_locks(manifest_path)
        raise


def _release_run_locks(manifest_path: Path) -> None:
    for _, lock in reversed(_RUN_LOCKS.pop(manifest_path.resolve(), [])):
        _close_read_lock(lock)
    _RUN_DIRECTORY_WITNESSES.pop(manifest_path.resolve(), None)
    _RUN_MANIFEST_WITNESSES.pop(manifest_path.resolve(), None)


def _recheck_run_directory_witnesses(manifest_path: Path) -> None:
    witnesses = _RUN_DIRECTORY_WITNESSES.get(manifest_path.resolve())
    if not witnesses:
        raise PerformanceEvidenceError(
            "performance run has no implementation directory witness"
        )
    for item in witnesses:
        _recheck_directory(
            item.path,
            item.revision,
            item.membership_sha256,
            description=item.description,
        )


def _recheck_run_manifest_witness(manifest_path: Path) -> None:
    witness = _RUN_MANIFEST_WITNESSES.get(manifest_path.resolve())
    if witness is None:
        raise PerformanceEvidenceError(
            "performance run has no immutable start-manifest witness"
        )
    digest, _ = _stable_sha256(
        witness.path,
        description=witness.description,
        expected_revision=witness.revision,
    )
    if digest != witness.sha256:
        raise PerformanceEvidenceError(
            "performance start manifest changed during measurement"
        )


def recheck_performance_run_state(manifest_path: Path) -> None:
    """Fence all measured directory enumerations immediately before a child."""

    require_qualified_producer_bootstrap()
    _assert_imported_local_modules_current()
    if manifest_path.resolve() not in _RUN_LOCKS:
        raise PerformanceEvidenceError(
            "performance run has no continuous read-lock"
        )
    _recheck_run_manifest_witness(manifest_path)
    _recheck_run_directory_witnesses(manifest_path)


def _locked_paths(manifest_path: Path) -> list[Path]:
    return [path for path, _ in _RUN_LOCKS.get(manifest_path.resolve(), [])]


def _aware_now() -> str:
    return datetime.now().astimezone().isoformat()


def _sha256(path: Path) -> str:
    digest, _ = _stable_sha256(
        path, description=f"performance evidence file: {path}"
    )
    return digest


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise PerformanceEvidenceError(f"manifestへ保存できない引数型です: {type(value).__name__}")


def normalize_arguments(kind: str, arguments: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Return the reviewed CLI surface in a stable, JSON-safe representation."""
    if kind not in PERFORMANCE_KINDS:
        raise PerformanceEvidenceError(f"未知の性能run種別です: {kind}")
    values = arguments if isinstance(arguments, Mapping) else vars(arguments)
    missing = [key for key in _ARGUMENT_KEYS[kind] if key not in values]
    if missing:
        raise PerformanceEvidenceError(f"manifest引数が不足しています: {', '.join(missing)}")
    result = {key: _json_value(values[key]) for key in _ARGUMENT_KEYS[kind]}
    if kind == "scale":
        for key in ("students", "lang_counts"):
            raw = result[key]
            if isinstance(raw, str):
                try:
                    result[key] = [int(part.strip()) for part in raw.split(",") if part.strip()]
                except ValueError as exc:
                    raise PerformanceEvidenceError(f"{key} は整数のカンマ区切りが必要です") from exc
            elif isinstance(raw, list):
                result[key] = [int(value) for value in raw]
            else:
                raise PerformanceEvidenceError(f"{key} の形式が不正です")
    return result


def performance_source_files(
    root: Path,
    kind: str,
    *,
    read_set: ValidationReadSet | None = None,
) -> list[Path]:
    """Sources whose current bytes define the meaning of a performance run."""
    if kind not in PERFORMANCE_KINDS:
        raise PerformanceEvidenceError(f"未知の性能run種別です: {kind}")
    source_root = _absolute_path(root) if read_set is not None else root
    if read_set is None:
        files = [*source_root.glob("server/**/*.py")]
    else:
        read_set.watch_directory(source_root, description="performance source root")
        files = [
            path
            for path in read_set.walk_regular_files(
                source_root / "server",
                description="performance server source tree",
            )
            if path.suffix == ".py"
        ]
    files.extend(
        source_root / relative
        for relative in (*_COMMON_SOURCES, *_KIND_SOURCES[kind])
    )
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise PerformanceEvidenceError(
            "性能runの必須ソースがありません: " + ", ".join(str(path) for path in missing)
        )
    return sorted(set(files), key=lambda path: path.as_posix())


def _resolve_input(
    root: Path,
    value: str,
    *,
    read_set: ValidationReadSet | None = None,
) -> Path:
    path = Path(value)
    selected = path if path.is_absolute() else root / path
    return _absolute_path(selected) if read_set is not None else selected.resolve()


def performance_input_files(
    root: Path,
    kind: str,
    arguments: Mapping[str, Any] | Any,
    *,
    read_set: ValidationReadSet | None = None,
) -> list[Path]:
    """Resolve the exact audio/corpus files fed to the run."""
    args = normalize_arguments(kind, arguments)
    if kind == "soak" and args["audio"]:
        files = [_resolve_input(root, str(args["audio"]), read_set=read_set)]
    elif args["corpus"] == "ja":
        fixture_dir = root / "tests" / "fixtures" / "ja"
        if read_set is None:
            files = sorted(fixture_dir.glob("*.wav"))
        else:
            fixture_dir = _absolute_path(fixture_dir)
            files = [
                path
                for path in read_set.walk_regular_files(
                    fixture_dir,
                    description="performance input corpus",
                )
                if path.parent == fixture_dir and path.suffix == ".wav"
            ]
    elif args["corpus"] == "ja_ext":
        index_path = root / "tests" / "fixtures" / "ja_ext" / "index.json"
        try:
            if read_set is None:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            else:
                index_path = _absolute_path(index_path)
                index = json.loads(
                    read_set.read_bytes(
                        index_path,
                        description="performance input corpus index",
                    ).decode("utf-8")
                )
            files = [
                index_path,
                *(
                    _resolve_input(
                        root,
                        clip["path"],
                        read_set=read_set,
                    )
                    for clip in index["clips"]
                ),
            ]
        except PerformanceEvidenceError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PerformanceEvidenceError(f"ja_ext indexを解決できません: {index_path}") from exc
    else:
        raise PerformanceEvidenceError(f"未知の性能入力corpusです: {args['corpus']}")
    if not files:
        raise PerformanceEvidenceError("性能runへ与える音源がありません")
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise PerformanceEvidenceError(
            "性能runの入力がありません: " + ", ".join(str(path) for path in missing)
        )
    return sorted(set(files), key=lambda path: path.as_posix())


def _key(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return f"external:{resolved.as_posix()}"


def hash_files(
    files: Iterable[Path],
    root: Path,
    *,
    read_set: ValidationReadSet | None = None,
    description: str = "performance evidence file",
) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in files:
        digest = (
            read_set.sha256(path, description=f"{description}: {path}")
            if read_set is not None
            else _sha256(path)
        )
        result[_key(path, root)] = digest
    return result


def _tree_files(
    path: Path,
    label: str,
    *,
    read_set: ValidationReadSet | None = None,
) -> list[Path]:
    path = _absolute_path(path) if read_set is not None else path.resolve()
    if read_set is not None:
        try:
            identity = path.lstat()
        except OSError as exc:
            raise PerformanceEvidenceError(f"{label} is missing: {path}") from exc
        if stat.S_ISREG(identity.st_mode):
            _assert_no_link_components(path, description=label)
            return [path]
        if not stat.S_ISDIR(identity.st_mode):
            raise PerformanceEvidenceError(f"{label} is missing: {path}")
        files = read_set.walk_regular_files(path, description=label)
        if not files:
            raise PerformanceEvidenceError(f"{label} contains no files: {path}")
        return files
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise PerformanceEvidenceError(f"{label}がありません: {path}")
    files = sorted(
        (candidate.resolve() for candidate in path.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.as_posix(),
    )
    if not files:
        raise PerformanceEvidenceError(f"{label}にファイルがありません: {path}")
    return files


def _watch_directory_or_absence(
    path: Path,
    label: str,
    *,
    read_set: ValidationReadSet | None,
) -> None:
    """Bind a runtime directory even when its final path does not yet exist."""

    if read_set is None:
        return
    absolute = _absolute_path(path)

    def nearest_existing_directory() -> Path:
        candidate = absolute
        while True:
            try:
                identity = candidate.lstat()
            except FileNotFoundError:
                parent = candidate.parent
                if parent == candidate:
                    raise PerformanceEvidenceError(
                        f"{label} absence cannot be witnessed: {absolute}"
                    )
                candidate = parent
                continue
            except OSError as exc:
                raise PerformanceEvidenceError(
                    f"{label} cannot be inspected: {candidate}"
                ) from exc
            if not stat.S_ISDIR(identity.st_mode) or _is_link_or_reparse(
                candidate, identity
            ):
                raise PerformanceEvidenceError(
                    f"{label} must be a real directory or absent below one: {absolute}"
                )
            return candidate

    while True:
        candidate = nearest_existing_directory()
        description = (
            label
            if candidate == absolute
            else f"{label} nearest existing ancestor"
        )
        read_set.watch_directory(candidate, description=description)
        # A missing child may have appeared immediately before the parent
        # snapshot.  In that case the parent baseline already contains it, so
        # descend and witness the new deepest directory instead of treating the
        # stale absence observation as proof.
        if nearest_existing_directory() != candidate:
            continue
        read_set.watch_directory(candidate, description=description)
        if nearest_existing_directory() == candidate:
            return


def _silero_model_file(
    *, read_set: ValidationReadSet | None = None
) -> Path:
    spec = util.find_spec("faster_whisper")
    if spec is None or spec.origin is None:
        raise PerformanceEvidenceError("Silero VADを提供するfaster-whisperがありません")
    origin = (
        _absolute_path(Path(spec.origin))
        if read_set is not None
        else Path(spec.origin).resolve()
    )
    path = origin.parent / "assets" / "silero_vad_v6.onnx"
    if not path.is_file():
        raise PerformanceEvidenceError(f"Silero VADモデルがありません: {path}")
    return path


def _runtime_assets(
    root: Path,
    config: Any,
    *,
    asr_engine: str | None = None,
    mt_engine: str | None = None,
    read_set: ValidationReadSet | None = None,
) -> tuple[list[Path], set[str]]:
    """Resolve model/tokenizer bytes and packages used by one effective config."""
    asr_engine = asr_engine or config.asr.engine
    mt_engine = mt_engine or config.mt.engine
    if asr_engine not in _ASR_DISTRIBUTIONS:
        raise PerformanceEvidenceError(f"未監査のASRエンジンです: {asr_engine}")
    if mt_engine not in _MT_DISTRIBUTIONS:
        raise PerformanceEvidenceError(f"未監査のMTエンジンです: {mt_engine}")

    files: list[Path] = []
    distributions = {
        *_COMMON_DISTRIBUTIONS,
        *_ASR_DISTRIBUTIONS[asr_engine],
        *_MT_DISTRIBUTIONS[mt_engine],
    }
    if config.vad.engine == "silero":
        files.append(_silero_model_file(read_set=read_set))
        distributions |= {"faster-whisper", "onnxruntime"}
    elif config.vad.engine != "energy":
        raise PerformanceEvidenceError(f"未監査のVADエンジンです: {config.vad.engine}")

    cert_dir = Path(config.server.cert_dir)
    cert_dir = cert_dir if cert_dir.is_absolute() else root / cert_dir
    _watch_directory_or_absence(
        cert_dir,
        "TLS certificate directory",
        read_set=read_set,
    )
    cert_path = cert_dir / config.server.cert_file
    key_path = cert_dir / config.server.key_file
    if cert_path.is_file() and key_path.is_file():
        files.extend(
            (
                _absolute_path(cert_path) if read_set is not None else cert_path.resolve(),
                _absolute_path(key_path) if read_set is not None else key_path.resolve(),
            )
        )
        distributions.add("cryptography")

    if asr_engine == "faster-whisper":
        files.extend(
            _tree_files(
                config.models.resolve(config.asr.model),
                "ASRモデル",
                read_set=read_set,
            )
        )
    elif asr_engine == "sherpa":
        files.extend(
            _tree_files(
                config.models.resolve(config.asr.sherpa.model),
                "Sherpa ASRモデル",
                read_set=read_set,
            )
        )
        if config.asr.sherpa.hotwords_file:
            hotwords = Path(config.asr.sherpa.hotwords_file)
            hotwords = hotwords if hotwords.is_absolute() else root / hotwords
            files.extend(_tree_files(hotwords, "Sherpa hotwords", read_set=read_set))

    if mt_engine == "hy-mt2":
        files.extend(
            _tree_files(
                config.models.resolve(config.mt.hy_mt2.gguf_path),
                "Hy-MT2モデル",
                read_set=read_set,
            )
        )
    elif mt_engine == "nllb":
        files.extend(
            _tree_files(
                config.models.resolve(config.mt.nllb.model_dir),
                "NLLBモデル",
                read_set=read_set,
            )
        )
        files.extend(
            _tree_files(
                config.models.resolve(config.mt.nllb.tokenizer_dir),
                "NLLB tokenizer",
                read_set=read_set,
            )
        )
    return sorted(set(files), key=lambda path: path.as_posix()), distributions


def _effective_runtime_assets(
    root: Path,
    config_paths: Iterable[Path],
    *,
    read_set: ValidationReadSet | None = None,
    asset_read_set: ValidationReadSet | None = None,
) -> tuple[list[Path], set[str]]:
    files: list[Path] = []
    distributions: set[str] = set()
    for path in config_paths:
        try:
            if read_set is None:
                config = load_config(path)
            else:
                config_bytes = read_set.read_bytes(
                    path,
                    description=f"effective performance config: {path}",
                )
                config = AppConfig.model_validate(
                    yaml.safe_load(config_bytes.decode("utf-8")) or {}
                )
        except PerformanceEvidenceError:
            raise
        except (OSError, ValueError) as exc:
            raise PerformanceEvidenceError(f"実効configを解決できません: {path}") from exc
        selected_files, selected_distributions = _runtime_assets(
            root,
            config,
            read_set=(asset_read_set if asset_read_set is not None else read_set),
        )
        files.extend(selected_files)
        distributions |= selected_distributions
    return sorted(set(files), key=lambda path: path.as_posix()), distributions


def _canonical_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _distribution_catalog(
    *, required: Iterable[str] = (),
) -> dict[str, Any]:
    required_names = {_canonical_distribution(name) for name in required}
    catalog: dict[str, Any] = {}
    for distribution in metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            continue
        name = _canonical_distribution(raw_name)
        if name in catalog:
            raise PerformanceEvidenceError(
                f"duplicate Python distribution is ambiguous: {name}"
            )
        catalog[name] = distribution
    missing = sorted(required_names - catalog.keys())
    if missing:
        raise PerformanceEvidenceError(
            "performance Python distributions are missing: " + ", ".join(missing)
        )
    return catalog


def _read_discovery_bytes(
    path: Path,
    *,
    description: str,
    read_set: ValidationReadSet | None,
) -> bytes:
    if read_set is not None:
        return read_set.read_bytes(path, description=description)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PerformanceEvidenceError(f"cannot read {description}: {path}") from exc


def _distribution_site_root(distribution: Any, *, name: str) -> Path:
    try:
        root = _absolute_path(Path(str(distribution.locate_file(Path()))))
        identity = root.lstat()
    except OSError as exc:
        raise PerformanceEvidenceError(
            f"Python distribution site directory is missing: {name}"
        ) from exc
    if not stat.S_ISDIR(identity.st_mode) or _is_link_or_reparse(root, identity):
        raise PerformanceEvidenceError(
            f"Python distribution site directory is not real: {name}: {root}"
        )
    return _assert_no_link_components(
        root, description=f"Python distribution site directory {name}"
    )


def _distribution_metadata_message(
    distribution: Any,
    *,
    name: str,
    read_set: ValidationReadSet | None,
) -> Any:
    entries = [
        entry
        for entry in distribution.files or ()
        if Path(str(entry)).name == "METADATA"
        and len(PurePosixPath(str(entry).replace("\\", "/")).parts) == 2
    ]
    if len(entries) != 1:
        raise PerformanceEvidenceError(
            f"Python distribution METADATA is missing or ambiguous: {name}"
        )
    path = _absolute_path(Path(str(distribution.locate_file(entries[0]))))
    value = _read_discovery_bytes(
        path,
        description=f"Python distribution METADATA {name}",
        read_set=read_set,
    )
    try:
        message = BytesParser(policy=compat32).parsebytes(value)
    except (TypeError, ValueError) as exc:
        raise PerformanceEvidenceError(
            f"Python distribution METADATA is invalid: {name}"
        ) from exc
    recorded_name = message.get("Name")
    recorded_version = message.get("Version")
    if (
        not isinstance(recorded_name, str)
        or _canonical_distribution(recorded_name) != name
        or not isinstance(recorded_version, str)
        or not recorded_version
    ):
        raise PerformanceEvidenceError(
            f"Python distribution METADATA identity is invalid: {name}"
        )
    return message


def _dependency_closure(
    distributions: Iterable[str],
    *,
    catalog: Mapping[str, Any],
    read_set: ValidationReadSet | None,
) -> set[str]:
    """Resolve mandatory and selected-extra Requires-Dist edges to a fixpoint."""

    extras: dict[str, set[str]] = {
        _canonical_distribution(name): set(
            _DISTRIBUTION_EXTRAS.get(_canonical_distribution(name), set())
        )
        for name in distributions
    }
    pending = list(extras)
    processed: dict[str, frozenset[str]] = {}
    marker_environment = {
        key: str(value) for key, value in default_environment().items()
    }
    while pending:
        name = pending.pop()
        distribution = catalog.get(name)
        if distribution is None:
            raise PerformanceEvidenceError(
                f"performance Python distribution is missing: {name}"
            )
        enabled_extras = frozenset(extras.get(name, set()))
        if processed.get(name) == enabled_extras:
            continue
        processed[name] = enabled_extras
        message = _distribution_metadata_message(
            distribution,
            name=name,
            read_set=read_set,
        )
        version_text = str(message.get("Version"))
        try:
            installed_version = Version(version_text)
        except InvalidVersion as exc:
            raise PerformanceEvidenceError(
                f"Python distribution version is invalid: {name}: {version_text}"
            ) from exc
        for requirement_text in message.get_all("Requires-Dist", []) or []:
            try:
                requirement = Requirement(requirement_text)
                # Base requirements remain active when extras are selected.  A
                # marker may distinguish ``extra == ''`` from one or more
                # selected extras, so evaluate both contexts to match install
                # semantics instead of replacing the base context.
                contexts = frozenset({"", *enabled_extras})
                enabled = requirement.marker is None or any(
                    requirement.marker.evaluate(
                        {**marker_environment, "extra": selected_extra}
                    )
                    for selected_extra in contexts
                )
            except (InvalidRequirement, KeyError, TypeError, ValueError) as exc:
                raise PerformanceEvidenceError(
                    f"Python dependency marker is invalid: {name}: {requirement_text}"
                ) from exc
            if not enabled:
                continue
            dependency = _canonical_distribution(requirement.name)
            dependency_distribution = catalog.get(dependency)
            if dependency_distribution is None:
                raise PerformanceEvidenceError(
                    f"required Python distribution is missing: {name} -> {dependency}"
                )
            dependency_message = _distribution_metadata_message(
                dependency_distribution,
                name=dependency,
                read_set=read_set,
            )
            dependency_version_text = str(dependency_message.get("Version"))
            try:
                dependency_version = Version(dependency_version_text)
            except InvalidVersion as exc:
                raise PerformanceEvidenceError(
                    f"Python distribution version is invalid: {dependency}: "
                    f"{dependency_version_text}"
                ) from exc
            if requirement.specifier and not requirement.specifier.contains(
                dependency_version, prereleases=True
            ):
                raise PerformanceEvidenceError(
                    f"Python dependency version does not satisfy metadata: "
                    f"{name} -> {requirement} (installed {dependency_version})"
                )
            before = set(extras.get(dependency, set()))
            extras.setdefault(dependency, set()).update(requirement.extras)
            if dependency not in processed or extras[dependency] != before:
                pending.append(dependency)
    return set(extras)


_DISTUTILS_PRECEDENCE_AST = ast.dump(
    ast.parse(
        "import os; var = 'SETUPTOOLS_USE_DISTUTILS'; "
        "enabled = os.environ.get(var, 'local') == 'local'; "
        "enabled and __import__('_distutils_hack').add_shim();"
    ),
    include_attributes=False,
)


def _executable_pth_imports(line: str, *, path: Path) -> set[str]:
    """Statically resolve the tiny executable subset accepted from ``.pth``."""

    try:
        tree = ast.parse(line)
    except SyntaxError as exc:
        raise PerformanceEvidenceError(
            f"startup .pth executable line is invalid: {path}"
        ) from exc
    imports: set[str] = set()
    if all(isinstance(node, (ast.Import, ast.ImportFrom)) for node in tree.body):
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
        return imports
    if ast.dump(tree, include_attributes=False) == _DISTUTILS_PRECEDENCE_AST:
        return {"os", "_distutils_hack"}
    raise PerformanceEvidenceError(
        f"startup .pth executable line cannot be resolved safely: {path}"
    )


def _referenced_path_files(
    path: Path,
    *,
    description: str,
    read_set: ValidationReadSet | None,
    directories_out: set[Path] | None = None,
) -> list[Path]:
    candidate = _absolute_path(path)
    try:
        identity = candidate.lstat()
    except OSError:
        return []
    if _is_link_or_reparse(candidate, identity):
        raise PerformanceEvidenceError(
            f"{description} must not be a link, junction, or reparse point: {candidate}"
        )
    _assert_no_link_components(candidate, description=description)
    if stat.S_ISREG(identity.st_mode):
        if read_set is not None:
            read_set.sha256(candidate, description=description)
        if directories_out is not None:
            directories_out.add(candidate.parent)
        return [candidate]
    if not stat.S_ISDIR(identity.st_mode):
        raise PerformanceEvidenceError(
            f"{description} is not a regular file or directory: {candidate}"
        )
    if read_set is not None:
        discovered_files = read_set.walk_regular_files(
            candidate, description=description
        )
        if directories_out is not None:
            directories_out.update(
                path
                for path in read_set._directories
                if path == candidate or candidate in path.parents
            )
        return discovered_files
    files: list[Path] = []
    pending = [candidate]
    while pending:
        directory = pending.pop()
        if directories_out is not None:
            directories_out.add(directory)
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            raise PerformanceEvidenceError(
                f"cannot enumerate {description}: {directory}"
            ) from exc
        for entry in entries:
            absolute = _assert_no_link_components(entry, description=description)
            try:
                entry_identity = absolute.lstat()
            except OSError as exc:
                raise PerformanceEvidenceError(
                    f"{description} changed during discovery"
                ) from exc
            if _is_link_or_reparse(absolute, entry_identity):
                raise PerformanceEvidenceError(
                    f"{description} must not contain a link, junction, or reparse point"
                )
            if stat.S_ISDIR(entry_identity.st_mode):
                pending.append(absolute)
            elif stat.S_ISREG(entry_identity.st_mode):
                files.append(absolute)
            else:
                raise PerformanceEvidenceError(
                    f"{description} contains a non-regular filesystem entry"
                )
    return sorted(files, key=lambda item: item.as_posix())


def _entry_top_level(entry: object) -> str | None:
    pure = PurePosixPath(str(entry).replace("\\", "/"))
    if not pure.parts or pure.parts[0] in {"", ".", ".."}:
        return None
    first = pure.parts[0]
    if first.endswith(".py"):
        first = first[:-3]
    return first if first.isidentifier() else None


def _startup_pth_bindings(
    catalog: Mapping[str, Any],
    *,
    read_set: ValidationReadSet | None,
    directories_out: set[Path],
    site_directories_out: set[Path],
) -> tuple[set[str], dict[str, set[Path]]]:
    """Bind every startup ``.pth`` and resolve every executable import owner."""

    path_owners: dict[Path, set[str]] = {}
    top_level_owners: dict[str, set[str]] = {}
    site_roots: set[Path] = set()
    for name, distribution in catalog.items():
        site_root = _distribution_site_root(distribution, name=name)
        site_roots.add(site_root)
        site_directories_out.add(site_root)
        directories_out.add(site_root)
        for entry in distribution.files or ():
            path = _absolute_path(Path(str(distribution.locate_file(entry))))
            path_owners.setdefault(path, set()).add(name)
            top_level = _entry_top_level(entry)
            if top_level:
                top_level_owners.setdefault(top_level, set()).add(name)

    seeds: set[str] = set()
    extra_files: dict[str, set[Path]] = {}
    for site_root in sorted(site_roots, key=lambda path: path.as_posix()):
        if read_set is not None:
            read_set.watch_directory(
                site_root, description="Python startup site directory"
            )
        try:
            entries = list(site_root.iterdir())
        except OSError as exc:
            raise PerformanceEvidenceError(
                f"cannot enumerate Python startup site directory: {site_root}"
            ) from exc
        for path in sorted(entries, key=lambda item: item.name):
            if path.suffix.lower() != ".pth":
                continue
            absolute = _absolute_path(path)
            owners = path_owners.get(absolute, set())
            if len(owners) != 1:
                raise PerformanceEvidenceError(
                    f"startup .pth owner is missing or ambiguous: {absolute}"
                )
            owner = next(iter(owners))
            seeds.add(owner)
            extra_files.setdefault(owner, set()).add(absolute)
            try:
                lines = _read_discovery_bytes(
                    absolute,
                    description=f"startup .pth {owner}",
                    read_set=read_set,
                ).decode("utf-8-sig").splitlines()
            except UnicodeError as exc:
                raise PerformanceEvidenceError(
                    f"startup .pth is not UTF-8: {absolute}"
                ) from exc
            for raw_line in lines:
                line = raw_line.rstrip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(("import ", "import\t")):
                    imported = _executable_pth_imports(line, path=absolute)
                    for module in imported:
                        if module in sys.stdlib_module_names:
                            continue
                        module_owners = top_level_owners.get(module, set())
                        if len(module_owners) != 1:
                            raise PerformanceEvidenceError(
                                f"startup .pth import owner is missing or ambiguous: "
                                f"{absolute}: {module}"
                            )
                        seeds.update(module_owners)
                    continue
                referenced = _referenced_path_files(
                    absolute.parent / line,
                    description=f"startup .pth target {owner}",
                    read_set=read_set,
                    directories_out=directories_out,
                )
                extra_files.setdefault(owner, set()).update(referenced)
    return seeds, extra_files


def _active_distribution_files(
    distributions: Iterable[str],
    *,
    read_set: ValidationReadSet | None = None,
    directories_out: set[Path] | None = None,
    site_directories_out: set[Path] | None = None,
) -> dict[str, tuple[Path, ...]]:
    """Resolve the dependency closure plus startup and editable implementation."""

    roots = {_canonical_distribution(name) for name in distributions}
    catalog = _distribution_catalog(required=roots)
    selected_directories = directories_out if directories_out is not None else set()
    selected_sites = (
        site_directories_out if site_directories_out is not None else set()
    )
    startup_seeds, startup_files = _startup_pth_bindings(
        catalog,
        read_set=read_set,
        directories_out=selected_directories,
        site_directories_out=selected_sites,
    )
    active = sorted(
        _dependency_closure(
            roots | startup_seeds,
            catalog=catalog,
            read_set=read_set,
        )
    )
    result: dict[str, tuple[Path, ...]] = {}
    for name in active:
        distribution = catalog[name]
        site_root = _distribution_site_root(distribution, name=name)
        entries = tuple(distribution.files or ())
        if not entries:
            raise PerformanceEvidenceError(
                f"active Python distribution has no auditable file index: {name}"
            )
        files: set[Path] = set(startup_files.get(name, set()))
        entry_paths: dict[str, Path] = {}
        top_level_directories: set[Path] = set()
        for entry in entries:
            entry_name = Path(str(entry)).as_posix()
            path = _absolute_path(Path(str(distribution.locate_file(entry))))
            try:
                identity = path.lstat()
            except OSError as exc:
                raise PerformanceEvidenceError(
                    f"active Python distribution file is missing: {name}: {entry_name}"
                ) from exc
            if not stat.S_ISREG(identity.st_mode) or _is_link_or_reparse(path, identity):
                raise PerformanceEvidenceError(
                    f"active Python distribution file is missing: {name}: {entry_name}"
                )
            _assert_no_link_components(
                path,
                description=f"active Python distribution file {name}: {entry_name}",
            )
            if read_set is not None:
                read_set.sha256(
                    path,
                    description=f"active Python distribution file {name}: {entry_name}",
                )
            files.add(path)
            entry_paths[entry_name] = path
            selected_directories.add(path.parent)
            pure = PurePosixPath(str(entry).replace("\\", "/"))
            if pure.parts and pure.parts[0] not in {"", ".", ".."}:
                candidate = site_root / pure.parts[0]
                if candidate.is_dir():
                    top_level_directories.add(candidate)

        # RECORD is an index, not an import sandbox.  Walk each owned top-level
        # directory so unindexed native modules or namespace members are still
        # hashed, locked, and covered by directory membership witnesses.
        for top_level in sorted(
            top_level_directories, key=lambda path: path.as_posix()
        ):
            files.update(
                _referenced_path_files(
                    top_level,
                    description=f"active Python distribution tree {name}",
                    read_set=read_set,
                    directories_out=selected_directories,
                )
            )

        for entry_name, path in tuple(entry_paths.items()):
            if path.suffix.lower() != ".pth":
                continue
            try:
                lines = _read_discovery_bytes(
                    path,
                    description=f"active Python distribution .pth {name}",
                    read_set=read_set,
                ).decode("utf-8-sig").splitlines()
            except UnicodeError as exc:
                raise PerformanceEvidenceError(
                    f"active Python distribution .pth is not UTF-8: {name}: {entry_name}"
                ) from exc
            for raw_line in lines:
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith(("import ", "import\t")):
                    continue
                files.update(
                    _referenced_path_files(
                        path.parent / line,
                        description=f"active Python distribution .pth target {name}",
                        read_set=read_set,
                        directories_out=selected_directories,
                    )
                )

        direct_urls = [
            path
            for entry_name, path in entry_paths.items()
            if Path(entry_name).name == "direct_url.json"
        ]
        top_levels = [
            path
            for entry_name, path in entry_paths.items()
            if Path(entry_name).name == "top_level.txt"
        ]
        for direct_url in direct_urls:
            try:
                direct_payload = json.loads(
                    _read_discovery_bytes(
                        direct_url,
                        description=f"active Python distribution direct URL {name}",
                        read_set=read_set,
                    ).decode("utf-8")
                )
            except (UnicodeError, ValueError) as exc:
                raise PerformanceEvidenceError(
                    f"active Python distribution direct URL is invalid: {name}"
                ) from exc
            if not (
                isinstance(direct_payload, dict)
                and isinstance(direct_payload.get("dir_info"), dict)
                and direct_payload["dir_info"].get("editable") is True
            ):
                continue
            parsed = urlsplit(str(direct_payload.get("url", "")))
            if parsed.scheme != "file":
                raise PerformanceEvidenceError(
                    f"editable Python distribution URL is not local: {name}"
                )
            checkout = Path(url2pathname(unquote(parsed.path)))
            if parsed.netloc:
                checkout = Path(f"//{parsed.netloc}{checkout}")
            checkout = _absolute_path(checkout)
            declared: set[str] = set()
            for top_level in top_levels:
                try:
                    text = _read_discovery_bytes(
                        top_level,
                        description=f"active Python distribution top-level index {name}",
                        read_set=read_set,
                    ).decode("utf-8")
                except UnicodeError as exc:
                    raise PerformanceEvidenceError(
                        f"active Python distribution top-level index is invalid: {name}"
                    ) from exc
                declared.update(
                    line.strip()
                    for line in text.splitlines()
                    if line.strip() and line.strip().isidentifier()
                )
            bound_editable = False
            for package in sorted(declared):
                for candidate in (
                    checkout / package,
                    checkout / f"{package}.py",
                    checkout / "src" / package,
                    checkout / "src" / f"{package}.py",
                ):
                    selected = _referenced_path_files(
                        candidate,
                        description=f"editable Python distribution source {name}",
                        read_set=read_set,
                        directories_out=selected_directories,
                    )
                    if selected:
                        bound_editable = True
                        files.update(selected)
            if not bound_editable:
                raise PerformanceEvidenceError(
                    f"editable Python distribution source cannot be bound: {name}"
                )
        selected_directories.update(path.parent for path in files)
        result[name] = tuple(sorted(files, key=lambda path: path.as_posix()))
    return result


def _installed_distributions(
    *,
    active_files: Mapping[str, tuple[Path, ...]],
    read_set: ValidationReadSet | None = None,
    metadata_files_out: set[Path] | None = None,
    implementation_files_out: set[Path] | None = None,
) -> dict[str, dict[str, Any]]:
    """Snapshot environment metadata plus all active implementation bytes."""
    installed: dict[str, dict[str, Any]] = {}
    metadata_names = {"METADATA", "WHEEL", "RECORD", "direct_url.json"}
    for distribution in metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            continue
        name = _canonical_distribution(raw_name)
        metadata_hashes: dict[str, str] = {}
        for entry in distribution.files or ():
            if Path(str(entry)).name not in metadata_names:
                continue
            raw_path = Path(str(distribution.locate_file(entry)))
            path = _absolute_path(raw_path)
            if path.is_file():
                if metadata_files_out is not None:
                    metadata_files_out.add(path)
                metadata_hashes[Path(str(entry)).as_posix()] = (
                    read_set.sha256(
                        path,
                        description=f"Python distribution metadata: {path}",
                    )
                    if read_set is not None
                    else _sha256(path)
                )
        value = {
            "version": distribution.version,
            "metadata_hashes": dict(sorted(metadata_hashes.items())),
        }
        if name in active_files:
            implementation_hashes: dict[str, str] = {}
            for path in active_files[name]:
                if implementation_files_out is not None:
                    implementation_files_out.add(path)
                implementation_hashes[path.as_posix()] = (
                    read_set.sha256(
                        path,
                        description=(
                            f"active Python distribution implementation {name}: {path}"
                        ),
                    )
                    if read_set is not None
                    else _sha256(path)
                )
            value["implementation_hashes"] = dict(
                sorted(implementation_hashes.items())
            )
        previous = installed.get(name)
        if previous is not None and previous != value:
            raise PerformanceEvidenceError(f"同名Python依存が複数あります: {name}")
        installed[name] = value
    return dict(sorted(installed.items()))


def _installed_distribution_metadata_files(
    *, read_set: ValidationReadSet | None = None
) -> list[Path]:
    """Return the metadata indexes whose hashes represent installed packages."""
    metadata_names = {"METADATA", "WHEEL", "RECORD", "direct_url.json"}
    files: set[Path] = set()
    for distribution in metadata.distributions():
        for entry in distribution.files or ():
            if Path(str(entry)).name not in metadata_names:
                continue
            raw_path = Path(str(distribution.locate_file(entry)))
            path = _absolute_path(raw_path)
            if path.is_file():
                files.add(path)
    result = sorted(files, key=lambda path: path.as_posix())
    if read_set is not None:
        for path in result:
            read_set.watch_directory(
                path.parent,
                description="Python distribution metadata directory",
            )
            if path.parent.parent != path.parent:
                read_set.watch_directory(
                    path.parent.parent,
                    description="Python distribution search directory",
                )
            read_set.sha256(
                path,
                description=f"Python distribution metadata: {path}",
            )
    return result


def _stable_directory_membership(
    path: Path,
    *,
    description: str,
) -> tuple[str, _DirectoryWitness]:
    absolute = _assert_no_link_components(path, description=description)
    try:
        identity, revision = _snapshot_path_revision(absolute)
        if not stat.S_ISDIR(identity.st_mode) or _is_link_or_reparse(
            absolute, identity
        ):
            raise PerformanceEvidenceError(
                f"{description} must be a real directory"
            )
        digest = _direct_membership_digest(absolute, description=description)
        _, after_revision = _snapshot_path_revision(absolute)
    except PerformanceEvidenceError:
        raise
    except OSError as exc:
        raise PerformanceEvidenceError(
            f"{description} changed during discovery"
        ) from exc
    if revision != after_revision:
        raise PerformanceEvidenceError(f"{description} changed during discovery")
    return digest, _DirectoryWitness(absolute, revision, digest, description)


def _directory_memberships(
    directories: Iterable[Path],
    *,
    description: str,
    read_set: ValidationReadSet | None,
    witnesses_out: list[_DirectoryWitness] | None = None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted({_absolute_path(path) for path in directories}, key=lambda p: p.as_posix()):
        item_description = f"{description}: {path}"
        if read_set is not None:
            digest = read_set.directory_membership_sha256(
                path, description=item_description
            )
        else:
            digest, witness = _stable_directory_membership(
                path, description=item_description
            )
            if witnesses_out is not None:
                witnesses_out.append(witness)
        result[path.as_posix()] = digest
    return result


def _runtime_snapshot(
    distributions: Iterable[str],
    *,
    active_distribution_files: Mapping[str, tuple[Path, ...]] | None = None,
    active_distribution_directories: set[Path] | None = None,
    site_directories: set[Path] | None = None,
    read_set: ValidationReadSet | None = None,
    metadata_files_out: set[Path] | None = None,
    implementation_files_out: set[Path] | None = None,
    directory_witnesses_out: list[_DirectoryWitness] | None = None,
) -> dict[str, Any]:
    selected_directories = (
        active_distribution_directories
        if active_distribution_directories is not None
        else set()
    )
    selected_sites = site_directories if site_directories is not None else set()
    selected_files = dict(active_distribution_files) if active_distribution_files is not None else _active_distribution_files(
        distributions,
        read_set=read_set,
        directories_out=selected_directories,
        site_directories_out=selected_sites,
    )
    active = sorted(selected_files)
    installed = _installed_distributions(
        active_files=selected_files,
        read_set=read_set,
        metadata_files_out=metadata_files_out,
        implementation_files_out=implementation_files_out,
    )
    missing = [name for name in active if name not in installed]
    if missing:
        raise PerformanceEvidenceError(
            "性能runのPython依存がありません: " + ", ".join(missing)
        )
    return {
        "python": sys.version,
        "executable": sys.executable,
        "executable_sha256": (
            read_set.sha256(
                Path(sys.executable),
                description="Python executable",
            )
            if read_set is not None
            else _sha256(Path(sys.executable))
        ),
        "active_distributions": active,
        "site_directory_memberships": _directory_memberships(
            selected_sites,
            description="Python site directory membership",
            read_set=read_set,
            witnesses_out=directory_witnesses_out,
        ),
        "implementation_directory_memberships": _directory_memberships(
            selected_directories,
            description="Python implementation directory membership",
            read_set=read_set,
            witnesses_out=directory_witnesses_out,
        ),
        "distributions": installed,
    }


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    )
    temporary.replace(path)


def _capture_run_manifest_witness(
    path: Path, payload: Mapping[str, Any]
) -> None:
    witness = _capture_imported_module_state(
        path, description="performance start manifest"
    )
    expected = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    if witness.sha256 != hashlib.sha256(expected).hexdigest():
        raise PerformanceEvidenceError(
            "performance start manifest changed while it was written"
        )
    _RUN_MANIFEST_WITNESSES[path.resolve()] = witness


def ensure_new_performance_run(
    run_dir: Path, *, allowed_files: Iterable[Path] = ()
) -> None:
    """Refuse to reuse a non-empty run, except for just-created configs."""
    run_dir.mkdir(parents=True, exist_ok=True)
    allowed = {path.resolve() for path in allowed_files}
    existing = {
        path.resolve() for path in run_dir.rglob("*") if path.is_file()
    }
    unexpected = existing - allowed
    if unexpected:
        raise PerformanceEvidenceError(
            "既存runは上書きできません。新しい --out-dir を指定してください: "
            + ", ".join(str(path) for path in sorted(unexpected))
        )


def begin_performance_run(
    run_dir: Path,
    *,
    kind: str,
    arguments: Mapping[str, Any] | Any,
    effective_configs: Iterable[Path],
    root: Path,
    started_at: str | None = None,
) -> Path:
    """Write an incomplete start snapshot before any measurement is launched."""
    require_qualified_producer_bootstrap()
    _assert_imported_local_modules_current()
    lock_mode = _lock_mode()
    _require_qualified_lock_mode(lock_mode)
    config_paths = {config.resolve() for config in effective_configs}
    ensure_new_performance_run(run_dir, allowed_files=config_paths)
    path = run_dir / MANIFEST_NAME
    normalized = normalize_arguments(kind, arguments)
    if not config_paths or any(not config.is_file() for config in config_paths):
        raise PerformanceEvidenceError("開始前の実効configを1件以上保存する必要があります")
    started_value = started_at or _aware_now()
    _parse_time(started_value, "started_at")
    source_files = performance_source_files(root, kind)
    input_files = performance_input_files(root, kind, normalized)
    # Lock configs before parsing them, then extend the lock set with every
    # model/tokenizer/TLS and package-metadata file that parsing selected.
    _acquire_run_locks(
        path,
        [*source_files, *input_files, *config_paths, Path(sys.executable)],
    )
    try:
        runtime_files, distributions = _effective_runtime_assets(root, sorted(config_paths))
        if not runtime_files:
            raise PerformanceEvidenceError("性能runで使う実モデル資産を解決できません")
        active_distribution_directories: set[Path] = set()
        site_directories: set[Path] = set()
        active_distribution_files = _active_distribution_files(
            distributions,
            directories_out=active_distribution_directories,
            site_directories_out=site_directories,
        )
        distribution_files = {
            distribution_path
            for files in active_distribution_files.values()
            for distribution_path in files
        }
        _acquire_run_locks(
            path,
            [
                *runtime_files,
                *_installed_distribution_metadata_files(),
                *distribution_files,
            ],
        )
        rechecked_directories: set[Path] = set()
        rechecked_sites: set[Path] = set()
        if (
            _active_distribution_files(
                distributions,
                directories_out=rechecked_directories,
                site_directories_out=rechecked_sites,
            )
            != active_distribution_files
            or rechecked_directories != active_distribution_directories
            or rechecked_sites != site_directories
        ):
            raise PerformanceEvidenceError(
                "active Python distribution file set changed before measurement"
            )
        discovery_read_set = ValidationReadSet()
        discovered_sources = performance_source_files(
            root, kind, read_set=discovery_read_set
        )
        discovered_inputs = performance_input_files(
            root, kind, normalized, read_set=discovery_read_set
        )
        discovered_runtime_files, discovered_distributions = (
            _effective_runtime_assets(
                root,
                sorted(config_paths),
                asset_read_set=discovery_read_set,
            )
        )
        if (
            discovered_sources != source_files
            or discovered_inputs != input_files
            or discovered_runtime_files != runtime_files
            or discovered_distributions != distributions
        ):
            raise PerformanceEvidenceError(
                "source, input, or runtime discovery changed before measurement"
            )
        source_hashes = hash_files(source_files, root)
        input_hashes = hash_files(input_files, root)
        config_hashes = {
            _run_relative(config, run_dir): _sha256(config) for config in sorted(config_paths)
        }
        config_hash = source_hashes.get("config.yaml")
        if config_hash is None:
            raise PerformanceEvidenceError("config.yamlがsource snapshotに含まれていません")
        directory_witnesses: list[_DirectoryWitness] = []
        runtime_snapshot = _runtime_snapshot(
            distributions,
            active_distribution_files=active_distribution_files,
            active_distribution_directories=active_distribution_directories,
            site_directories=site_directories,
            directory_witnesses_out=directory_witnesses,
        )
        discovery_witness = discovery_read_set.freeze()
        merged_witnesses: dict[Path, _DirectoryWitness] = {}
        for item in (*directory_witnesses, *discovery_witness.directories):
            previous = merged_witnesses.get(item.path)
            if previous is not None and (
                previous.revision != item.revision
                or previous.membership_sha256 != item.membership_sha256
            ):
                raise PerformanceEvidenceError(
                    "performance directory changed while start witnesses were merged"
                )
            merged_witnesses[item.path] = item
        _RUN_DIRECTORY_WITNESSES[path.resolve()] = tuple(
            item
            for _, item in sorted(
                merged_witnesses.items(), key=lambda pair: pair[0].as_posix()
            )
        )
        payload = {
            "schema_version": MANIFEST_SCHEMA,
            "kind": kind,
            "complete": False,
            "started_at": started_value,
            "finished_at": None,
            "arguments": normalized,
            "configuration": {
                "base": {"path": "config.yaml", "sha256": config_hash},
                "effective": config_hashes,
            },
            "source_hashes": source_hashes,
            "input_hashes": input_hashes,
            "runtime_asset_hashes": hash_files(runtime_files, root),
            "artifacts": {},
            "primary_report": None,
            "runtime": runtime_snapshot,
            "continuous_lock": {
                "mode": lock_mode,
                "acquired_at": started_value,
                "released_at": None,
                "interrupted": False,
                "files": sorted(_key(item, root) for item in _locked_paths(path)),
            },
        }
        _recheck_run_directory_witnesses(path)
        _write_manifest(path, payload)
        _capture_run_manifest_witness(path, payload)
        _recheck_run_manifest_witness(path)
        _recheck_run_directory_witnesses(path)
    except Exception:
        _release_run_locks(path)
        raise
    return path


def _run_relative(path: Path, run_dir: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(run_dir.resolve()).as_posix()
    except ValueError as exc:
        raise PerformanceEvidenceError(f"run外のartifactは採用できません: {path}") from exc


def _reject_changed_snapshot(
    manifest_path: Path,
    payload: dict[str, Any],
    changed: list[str],
    finished_at: str | None,
) -> None:
    payload["finished_at"] = finished_at or _aware_now()
    payload["snapshot_changed_during_run"] = changed
    payload["complete"] = False
    _write_manifest(manifest_path, payload)
    raise PerformanceEvidenceError(
        "性能run中にsnapshotが変化しました: " + ", ".join(changed)
    )


def _complete_performance_run_locked(
    manifest_path: Path,
    *,
    primary_report: Path,
    artifacts: Iterable[Path],
    effective_configs: Iterable[Path],
    root: Path,
    finished_at: str | None = None,
) -> dict[str, Any]:
    """Seal report/config hashes, failing if source or input changed mid-run."""
    _recheck_run_manifest_witness(manifest_path)
    _recheck_run_directory_witnesses(manifest_path)
    lock_mode = _lock_mode()
    _require_qualified_lock_mode(lock_mode)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PerformanceEvidenceError(f"開始manifestを読めません: {manifest_path}") from exc
    if payload.get("schema_version") != MANIFEST_SCHEMA:
        raise PerformanceEvidenceError("開始manifestのschemaが一致しません")
    if payload.get("complete") is not False:
        # A completed run is immutable.  In particular, do not let a caller
        # replace the report and call ``complete`` again to bless new bytes.
        raise PerformanceEvidenceError("完了済みの性能runは再封印できません")
    _parse_time(payload.get("started_at"), "started_at")
    continuous_lock = payload.get("continuous_lock")
    if (
        not isinstance(continuous_lock, dict)
        or continuous_lock.get("mode") != lock_mode
        or continuous_lock.get("acquired_at") != payload.get("started_at")
        or continuous_lock.get("released_at") is not None
        or continuous_lock.get("interrupted") is not False
        or continuous_lock.get("files")
        != sorted(_key(item, root) for item in _locked_paths(manifest_path))
    ):
        raise PerformanceEvidenceError("性能runの連続read-lock記録が開始時と一致しません")
    kind = payload.get("kind")
    args = normalize_arguments(str(kind), payload.get("arguments", {}))
    current_sources = hash_files(performance_source_files(root, str(kind)), root)
    current_inputs = hash_files(performance_input_files(root, str(kind), args), root)
    changed: list[str] = []
    if current_sources != payload.get("source_hashes"):
        changed.append("source")
    if current_inputs != payload.get("input_hashes"):
        changed.append("input")
    if changed:
        _reject_changed_snapshot(manifest_path, payload, changed, finished_at)

    config_paths = {path.resolve() for path in effective_configs}
    if not config_paths or any(not path.is_file() for path in config_paths):
        raise PerformanceEvidenceError("実行時の有効configを1件以上保存する必要があります")
    runtime_files, distributions = _effective_runtime_assets(root, sorted(config_paths))
    active_distribution_directories: set[Path] = set()
    site_directories: set[Path] = set()
    active_distribution_files = _active_distribution_files(
        distributions,
        directories_out=active_distribution_directories,
        site_directories_out=site_directories,
    )
    current_assets = hash_files(runtime_files, root)
    current_runtime = _runtime_snapshot(
        distributions,
        active_distribution_files=active_distribution_files,
        active_distribution_directories=active_distribution_directories,
        site_directories=site_directories,
    )
    _recheck_run_manifest_witness(manifest_path)
    _recheck_run_directory_witnesses(manifest_path)
    started_assets = payload.get("runtime_asset_hashes")
    started_runtime = payload.get("runtime")
    changed = []
    if current_assets != started_assets:
        changed.append("runtime_asset")
    started_configuration = payload.get("configuration")
    current_config_hashes = {
        _run_relative(path, manifest_path.parent): _sha256(path)
        for path in sorted(config_paths)
    }
    if (
        not isinstance(started_configuration, dict)
        or current_config_hashes != started_configuration.get("effective")
    ):
        changed.append("effective_config")
    if current_runtime != started_runtime:
        changed.append("runtime")
    if changed:
        _reject_changed_snapshot(manifest_path, payload, changed, finished_at)

    run_dir = manifest_path.parent
    artifact_paths = {path.resolve() for path in artifacts}
    artifact_paths.add(primary_report.resolve())
    missing = [path for path in artifact_paths if not path.is_file()]
    if missing:
        raise PerformanceEvidenceError(
            "性能runのartifactがありません: " + ", ".join(str(path) for path in missing)
        )
    # Hold reports too: otherwise another process can replace report bytes
    # after hashing but before the completed manifest is atomically written.
    _acquire_run_locks(manifest_path, artifact_paths)
    payload["continuous_lock"]["files"] = sorted(
        _key(item, root) for item in _locked_paths(manifest_path)
    )
    payload["artifacts"] = {
        _run_relative(path, run_dir): _sha256(path) for path in sorted(artifact_paths)
    }
    payload["primary_report"] = _run_relative(primary_report, run_dir)
    payload["configuration"]["effective"] = current_config_hashes
    # Drop unused fallback assets/dependencies, retaining the hashes captured at
    # start for exactly the effective configurations that produced the report.
    payload["runtime_asset_hashes"] = current_assets
    payload["runtime"] = current_runtime
    payload["finished_at"] = finished_at or _aware_now()
    payload["complete"] = True
    payload.pop("snapshot_changed_during_run", None)
    return payload


def _record_lock_release(
    manifest_path: Path, *, interrupted: bool, released_at: str | None = None
) -> None:
    """Best-effort update of lock state without masking the original failure."""
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        lock = payload.get("continuous_lock")
        if not isinstance(lock, dict):
            return
        timestamp = released_at or _aware_now()
        lock["released_at"] = timestamp
        lock["interrupted"] = interrupted
        if interrupted:
            payload["complete"] = False
            payload["finished_at"] = payload.get("finished_at") or timestamp
        _write_manifest(manifest_path, payload)
    except (OSError, ValueError, TypeError):
        pass


def release_performance_run_locks(manifest_path: Path) -> None:
    """Abort a run and explicitly release its read locks.

    This is intentionally not a pause/resume mechanism: once released, the
    manifest is marked interrupted and cannot be sealed.  It exists so a caller
    handling an aborted run need not wait for process exit before editing files.
    """
    if manifest_path.resolve() not in _RUN_LOCKS:
        return
    _record_lock_release(manifest_path, interrupted=True)
    _release_run_locks(manifest_path)


def complete_performance_run(
    manifest_path: Path,
    *,
    primary_report: Path,
    artifacts: Iterable[Path],
    effective_configs: Iterable[Path],
    root: Path,
    finished_at: str | None = None,
) -> dict[str, Any]:
    """Seal a run while its start snapshot remains continuously read-locked."""
    require_qualified_producer_bootstrap()
    _assert_imported_local_modules_current()
    if manifest_path.resolve() in _RUN_LOCKS:
        try:
            _recheck_run_manifest_witness(manifest_path)
        except Exception:
            _record_lock_release(manifest_path, interrupted=True)
            _release_run_locks(manifest_path)
            raise
    try:
        initial = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PerformanceEvidenceError(f"開始manifestを読めません: {manifest_path}") from exc
    if initial.get("complete") is True:
        raise PerformanceEvidenceError("完了済みの性能runは再封印できません")
    if manifest_path.resolve() not in _RUN_LOCKS:
        raise PerformanceEvidenceError(
            "性能runの連続read-lockがありません。別processや中断後のrunは封印できません"
        )
    try:
        payload = _complete_performance_run_locked(
            manifest_path,
            primary_report=primary_report,
            artifacts=artifacts,
            effective_configs=effective_configs,
            root=root,
            finished_at=finished_at,
        )
        lock = payload.get("continuous_lock")
        if not isinstance(lock, dict) or lock.get("released_at") is not None:
            raise PerformanceEvidenceError("性能runの連続read-lock記録が不正です")
        lock["released_at"] = payload["finished_at"]
        lock["interrupted"] = False
        _recheck_run_manifest_witness(manifest_path)
        _recheck_run_directory_witnesses(manifest_path)
        _write_manifest(manifest_path, payload)
        _recheck_run_directory_witnesses(manifest_path)
        return payload
    except Exception:
        _record_lock_release(manifest_path, interrupted=True)
        raise
    finally:
        _release_run_locks(manifest_path)


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise PerformanceEvidenceError(f"{field}がありません")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PerformanceEvidenceError(f"{field}がISO日時ではありません") from exc
    if parsed.tzinfo is None:
        raise PerformanceEvidenceError(f"{field}にtimezoneがありません")
    return parsed


def _normalized_run_hash_paths(
    run_dir: Path,
    values: Any,
    field: str,
) -> dict[str, Path]:
    if not isinstance(values, dict) or not values:
        raise PerformanceEvidenceError(f"{field}が空です")
    run_root = _absolute_path(run_dir)
    result: dict[str, Path] = {}
    for relative, expected in values.items():
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or not relative.isprintable()
        ):
            raise PerformanceEvidenceError(f"{field}のpathが不正です")
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or pure.as_posix() != relative
            or any(part in {"", ".", ".."} or ":" in part for part in pure.parts)
        ):
            raise PerformanceEvidenceError(f"{field}のpathが不正です: {relative}")
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise PerformanceEvidenceError(f"{field}のhashが不正です: {relative}")
        path = _absolute_path(run_root.joinpath(*pure.parts))
        try:
            path.relative_to(run_root)
        except ValueError as exc:
            raise PerformanceEvidenceError(f"{field}がrun外を参照しています: {relative}") from exc
        if path in result.values():
            raise PerformanceEvidenceError(f"{field}のpathが重複しています: {relative}")
        result[relative] = path
    return result


def _verify_run_hashes(
    run_dir: Path,
    values: Any,
    field: str,
    *,
    read_set: ValidationReadSet | None = None,
) -> dict[str, Path]:
    paths = _normalized_run_hash_paths(run_dir, values, field)
    for relative, path in paths.items():
        expected = values[relative]
        digest = (
            read_set.sha256(path, description=f"{field}: {relative}")
            if read_set is not None
            else _sha256(path)
        )
        if digest != expected:
            raise PerformanceEvidenceError(f"{field}のhashが一致しません: {relative}")
    return paths


def _completed_source_snapshot_matches(
    recorded: Any, current: dict[str, str]
) -> bool:
    """Compare a sealed measurement while allowing current audit rules."""

    if not isinstance(recorded, dict) or recorded.keys() != current.keys():
        return False
    return all(
        recorded[path] == digest
        for path, digest in current.items()
        if path not in _REAUDITED_SOURCES
    )


def validate_performance_manifest(
    run_dir: Path, *, kind: str, primary_report: Path, root: Path
) -> ValidatedPerformanceManifest:
    """Recompute every trusted field before the tracker touches the workbook."""
    _assert_imported_local_modules_current()
    lock_mode = _lock_mode()
    _require_qualified_lock_mode(lock_mode)
    read_set = ValidationReadSet()
    root = read_set.watch_directory(root, description="performance source root")
    run_dir = read_set.watch_directory(run_dir, description="performance run directory")
    try:
        run_dir.relative_to(root)
    except ValueError as exc:
        raise PerformanceEvidenceError("performance run directory is outside the source root") from exc

    manifest_path = run_dir / MANIFEST_NAME
    try:
        manifest_bytes = read_set.read_bytes(
            manifest_path,
            description="performance source manifest",
        )
        payload = json.loads(manifest_bytes.decode("utf-8"))
    except PerformanceEvidenceError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise PerformanceEvidenceError(f"性能source manifestを読めません: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise PerformanceEvidenceError("性能source manifestのrootがobjectではありません")
    if payload.get("schema_version") != MANIFEST_SCHEMA or payload.get("kind") != kind:
        raise PerformanceEvidenceError("性能source manifestのschema/kindが一致しません")
    if payload.get("complete") is not True:
        raise PerformanceEvidenceError("未完了の性能runは採用できません")
    started = _parse_time(payload.get("started_at"), "started_at")
    finished = _parse_time(payload.get("finished_at"), "finished_at")
    if finished < started:
        raise PerformanceEvidenceError("finished_atがstarted_atより前です")

    args = normalize_arguments(kind, payload.get("arguments", {}))
    source_files = performance_source_files(root, kind, read_set=read_set)
    input_files = performance_input_files(root, kind, args, read_set=read_set)
    source_set = tuple(source_files)
    input_set = tuple(input_files)
    expected_sources = hash_files(
        source_files,
        root,
        read_set=read_set,
        description="performance source",
    )
    expected_inputs = hash_files(
        input_files,
        root,
        read_set=read_set,
        description="performance input",
    )
    if not _completed_source_snapshot_matches(
        payload.get("source_hashes"), expected_sources
    ):
        raise PerformanceEvidenceError("性能runのsource snapshotが現行状態と一致しません")
    if payload.get("input_hashes") != expected_inputs:
        raise PerformanceEvidenceError("性能runの入力snapshotが現行状態と一致しません")

    configuration = payload.get("configuration")
    if not isinstance(configuration, dict):
        raise PerformanceEvidenceError("性能runのconfigurationが不正です")
    base = configuration.get("base")
    if base != {"path": "config.yaml", "sha256": expected_sources.get("config.yaml")}:
        raise PerformanceEvidenceError("性能runのbase configがsource snapshotと一致しません")
    effective_paths = _verify_run_hashes(
        run_dir,
        configuration.get("effective"),
        "effective config",
        read_set=read_set,
    )
    config_paths = list(effective_paths.values())
    config_set = tuple(sorted(config_paths, key=lambda path: path.as_posix()))
    runtime_files, distributions = _effective_runtime_assets(
        root,
        config_paths,
        read_set=read_set,
    )
    runtime_set = tuple(runtime_files)
    expected_assets = hash_files(
        runtime_files,
        root,
        read_set=read_set,
        description="performance runtime asset",
    )
    if not expected_assets or payload.get("runtime_asset_hashes") != expected_assets:
        raise PerformanceEvidenceError("性能runのモデル/TLS資産snapshotが現行状態と一致しません")

    distribution_metadata_files = _installed_distribution_metadata_files(
        read_set=read_set
    )
    distribution_metadata_set = tuple(distribution_metadata_files)
    active_distribution_directories: set[Path] = set()
    site_directories: set[Path] = set()
    active_distribution_files = _active_distribution_files(
        distributions,
        read_set=read_set,
        directories_out=active_distribution_directories,
        site_directories_out=site_directories,
    )
    active_distribution_file_set = {
        path for files in active_distribution_files.values() for path in files
    }
    snapshot_metadata_files: set[Path] = set()
    snapshot_implementation_files: set[Path] = set()
    runtime_snapshot = _runtime_snapshot(
        distributions,
        active_distribution_files=active_distribution_files,
        active_distribution_directories=active_distribution_directories,
        site_directories=site_directories,
        read_set=read_set,
        metadata_files_out=snapshot_metadata_files,
        implementation_files_out=snapshot_implementation_files,
    )
    if not snapshot_metadata_files.issubset(set(distribution_metadata_files)):
        raise PerformanceEvidenceError(
            "Python distribution metadata set changed during validation"
        )
    if snapshot_implementation_files != active_distribution_file_set:
        raise PerformanceEvidenceError(
            "active Python distribution implementation set changed during validation"
        )
    if payload.get("runtime") != runtime_snapshot:
        raise PerformanceEvidenceError("性能runのPython依存snapshotが現行環境と一致しません")

    continuous_lock = payload.get("continuous_lock")
    artifact_paths = _normalized_run_hash_paths(
        run_dir,
        payload.get("artifacts"),
        "artifact",
    )
    artifact_files = set(artifact_paths.values())
    artifact_set = tuple(sorted(artifact_files, key=lambda path: path.as_posix()))
    expected_locked_files = sorted(
        _key(path, root)
        for path in {
            *source_files,
            *input_files,
            *config_paths,
            *runtime_files,
            *distribution_metadata_files,
            *active_distribution_file_set,
            _absolute_path(Path(sys.executable)),
            *artifact_files,
        }
    )
    if (
        not isinstance(continuous_lock, dict)
        or continuous_lock.get("mode") != lock_mode
        or continuous_lock.get("files") != expected_locked_files
        or continuous_lock.get("interrupted") is not False
        or continuous_lock.get("acquired_at") != payload.get("started_at")
        or continuous_lock.get("released_at") != payload.get("finished_at")
    ):
        raise PerformanceEvidenceError("性能runの連続read-lock記録が不正です")
    _parse_time(continuous_lock.get("acquired_at"), "continuous_lock.acquired_at")
    _parse_time(continuous_lock.get("released_at"), "continuous_lock.released_at")
    _verify_run_hashes(
        run_dir,
        payload.get("artifacts"),
        "artifact",
        read_set=read_set,
    )
    primary_report = _absolute_path(primary_report)
    try:
        expected_report = primary_report.relative_to(run_dir).as_posix()
    except ValueError as exc:
        raise PerformanceEvidenceError("primary reportがrun外を参照しています") from exc
    if payload.get("primary_report") != expected_report:
        raise PerformanceEvidenceError("primary reportが一致しません")
    primary_digest = read_set.sha256(
        primary_report,
        description="performance primary report",
    )
    if payload["artifacts"].get(expected_report) != primary_digest:
        raise PerformanceEvidenceError("primary reportのhashが一致しません")

    # Re-enumerate every dynamic set after all expensive parsing/hashing.  The
    # directory revisions catch transient membership changes; these exact set
    # comparisons also reject persistent additions/removals deterministically.
    final_sources = performance_source_files(root, kind, read_set=read_set)
    final_inputs = performance_input_files(root, kind, args, read_set=read_set)
    final_effective = _normalized_run_hash_paths(
        run_dir,
        configuration.get("effective"),
        "effective config",
    )
    final_runtime, final_distributions = _effective_runtime_assets(
        root,
        final_effective.values(),
        read_set=read_set,
    )
    final_distribution_metadata = _installed_distribution_metadata_files(
        read_set=read_set
    )
    final_active_distribution_directories: set[Path] = set()
    final_site_directories: set[Path] = set()
    final_active_distribution_files = _active_distribution_files(
        final_distributions,
        read_set=read_set,
        directories_out=final_active_distribution_directories,
        site_directories_out=final_site_directories,
    )
    final_artifacts = _normalized_run_hash_paths(
        run_dir,
        payload.get("artifacts"),
        "artifact",
    )
    if (
        tuple(final_sources) != source_set
        or tuple(final_inputs) != input_set
        or tuple(sorted(final_effective.values(), key=lambda path: path.as_posix()))
        != config_set
        or tuple(final_runtime) != runtime_set
        or final_distributions != distributions
        or tuple(final_distribution_metadata) != distribution_metadata_set
        or final_active_distribution_files != active_distribution_files
        or final_active_distribution_directories
        != active_distribution_directories
        or final_site_directories != site_directories
        or tuple(sorted(final_artifacts.values(), key=lambda path: path.as_posix()))
        != artifact_set
    ):
        raise PerformanceEvidenceError(
            "performance evidence file set changed during validation"
        )
    final_runtime_snapshot = _runtime_snapshot(
        final_distributions,
        active_distribution_files=final_active_distribution_files,
        active_distribution_directories=final_active_distribution_directories,
        site_directories=final_site_directories,
        read_set=read_set,
    )
    if final_runtime_snapshot != runtime_snapshot:
        raise PerformanceEvidenceError(
            "Python distribution state changed during validation"
        )

    witness = read_set.freeze()
    return ValidatedPerformanceManifest(payload, witness)
