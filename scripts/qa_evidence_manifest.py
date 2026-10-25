"""Create and validate tamper-evident QA result envelopes."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable, Iterable, cast


_ROOT_KEYS = {
    "schema_version",
    "kind",
    "complete",
    "source_hashes",
    "artifacts",
}
_KINDS = {"browser", "native-browser"}
_PYTEST_INCOMPLETE_ARTIFACTS = ("pytest.xml", "pytest.log", "run.json")
_PYTEST_COMPLETE_ARTIFACTS = (*_PYTEST_INCOMPLETE_ARTIFACTS, "story-evidence.json")
_PYTEST_COMPLETION_LOCK = ".pytest-completion.lock"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class EvidenceManifestError(ValueError):
    """The evidence envelope or one of its sealed artifacts is invalid."""


class _PublicationGuardFailed(RuntimeError):
    """A producer input changed immediately before manifest publication."""


# The final element is Windows FILE_BASIC_INFO.ChangeTime (100 ns ticks,
# normalized to ns).  CPython's st_ctime_ns is the creation time on Windows,
# so a same-size in-place edit followed by os.utime can otherwise reproduce
# the complete exposed stat tuple.  On non-Windows platforms ctime_ns already
# serves as the change token and is repeated in the final position.
SourceRevision = tuple[int, int, int, int, int, int]
SourceFilesSupplier = Callable[[], Iterable[Path]]


class SourceHashSnapshot(dict[str, str]):
    """Content hashes plus a producer-only file revision witness.

    The mapping remains JSON/dict compatible so manifests keep their existing
    schema.  Revisions are deliberately not serialized: they only prove to the
    producer that a source was not edited and restored while a run was active.
    """

    def __init__(
        self,
        hashes: dict[str, str],
        revisions: dict[str, SourceRevision],
    ) -> None:
        super().__init__(hashes)
        self._revisions = tuple(sorted(revisions.items()))

    @property
    def revisions(self) -> tuple[tuple[str, SourceRevision], ...]:
        return self._revisions


@dataclass(frozen=True)
class SealedBrowserEvidence:
    """A validated envelope and the exact artifact bytes checked against it."""

    manifest: dict[str, Any]
    manifest_bytes: bytes
    manifest_sha256: str
    browser_bytes: bytes
    browser_payload: dict[str, Any]


@dataclass(frozen=True)
class SealedPytestEvidence:
    """A validated pytest envelope and the exact artifact bytes it seals."""

    manifest: dict[str, Any]
    manifest_bytes: bytes
    manifest_sha256: str
    pytest_bytes: bytes
    log_bytes: bytes
    run_bytes: bytes
    run_payload: dict[str, Any]
    story_evidence_bytes: bytes | None
    story_evidence_payload: dict[str, Any] | None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def snapshot_source_hashes(
    root: Path, source_files: Iterable[Path]
) -> SourceHashSnapshot:
    """Return content hashes with a non-serialized source revision witness."""

    if not root.is_absolute():
        raise EvidenceManifestError("QA evidence source root must be an absolute path")
    if _is_link_or_junction(root) or not root.is_dir():
        raise EvidenceManifestError(
            "QA evidence source root must not be a link or junction"
        )
    root_identity, root_revision = _snapshot_path_revision(
        root, description="QA evidence source root"
    )
    if not stat.S_ISDIR(root_identity.st_mode):
        raise EvidenceManifestError("QA evidence source root must be a directory")
    hashes: dict[str, str] = {}
    revisions: dict[str, SourceRevision] = {}
    source_paths: dict[str, Path] = {}
    directory_revisions: dict[Path, SourceRevision] = {root: root_revision}
    canonical_paths: set[str] = set()
    for path in source_files:
        if not path.is_absolute():
            raise EvidenceManifestError(
                f"QA evidence source must be an absolute path: {path}"
            )
        try:
            relative_path = path.relative_to(root)
        except ValueError as exc:
            raise EvidenceManifestError(
                f"QA evidence source is outside the repository root: {path}"
            ) from exc
        relative = str(relative_path).replace("\\", "/")
        relative = _require_repo_relative_path(relative)
        canonical = os.path.normcase(relative)
        if canonical in canonical_paths:
            raise EvidenceManifestError(
                f"duplicate QA evidence source path: {relative!r}"
            )
        canonical_paths.add(canonical)

        directory_identities: list[tuple[Path, SourceRevision]] = [
            (root, root_revision)
        ]
        directory = root
        for part in relative_path.parts[:-1]:
            directory /= part
            if _is_link_or_junction(directory):
                raise EvidenceManifestError(
                    f"QA evidence source parent must not be a link or junction: {directory}"
                )
            identity, revision = _snapshot_path_revision(
                directory,
                description=f"QA evidence source parent {directory}",
            )
            if not stat.S_ISDIR(identity.st_mode):
                raise EvidenceManifestError(
                    f"QA evidence source parent is not a directory: {directory}"
                )
            previous_revision = directory_revisions.setdefault(directory, revision)
            if previous_revision != revision:
                raise EvidenceManifestError(
                    f"QA evidence source parent changed during snapshot: {directory}"
                )
            directory_identities.append((directory, revision))

        if _is_link_or_junction(path):
            raise EvidenceManifestError(
                f"QA evidence source must not be a link or junction: {path}"
            )
        value, revision = _read_stable_regular_file_with_revision(
            path, description=f"QA evidence source {relative!r}"
        )
        for directory, before_revision in directory_identities:
            if _is_link_or_junction(directory):
                raise EvidenceManifestError(
                    f"QA evidence source parent changed while it was being read: {directory}"
                )
            after, after_revision = _snapshot_path_revision(
                directory,
                description=f"QA evidence source parent {directory}",
            )
            if (
                not stat.S_ISDIR(after.st_mode)
                or before_revision != after_revision
            ):
                raise EvidenceManifestError(
                    f"QA evidence source parent changed while it was being read: {directory}"
                )
        hashes[relative] = _sha256_bytes(value)
        revisions[relative] = revision
        source_paths[relative] = path
    if not hashes:
        raise EvidenceManifestError("QA evidence source set must not be empty")

    # A file or ancestor read early in a large source set must still be the
    # same revision when the complete snapshot is ready to return.
    for relative, path in source_paths.items():
        _recheck_regular_file_revision(
            path,
            revisions[relative],
            expected_digest=hashes[relative],
            description=f"QA evidence source {relative!r}",
        )
    for directory, revision in directory_revisions.items():
        _recheck_directory_revision(
            directory,
            revision,
            description="QA evidence source ancestor",
        )

    hashes = dict(sorted(hashes.items()))
    return SourceHashSnapshot(
        hashes,
        {relative: revisions[relative] for relative in hashes},
    )


def _require_source_files_supplier(value: Any) -> SourceFilesSupplier:
    if not callable(value):
        raise EvidenceManifestError("source_files must be a callable supplier")
    return cast(SourceFilesSupplier, value)


def _enumerate_source_files(source_files: SourceFilesSupplier) -> tuple[Path, ...]:
    try:
        result = tuple(source_files())
    except EvidenceManifestError:
        raise
    except Exception as exc:
        raise EvidenceManifestError("cannot enumerate QA evidence sources") from exc
    if any(not isinstance(path, Path) for path in result):
        raise EvidenceManifestError("source_files supplier must return Path entries")
    return result


def _source_file_names(root: Path, source_files: tuple[Path, ...]) -> tuple[str, ...]:
    names: list[str] = []
    canonical_paths: set[str] = set()
    for path in source_files:
        if not path.is_absolute():
            raise EvidenceManifestError(
                f"QA evidence source must be an absolute path: {path}"
            )
        try:
            relative_path = path.relative_to(root)
        except ValueError as exc:
            raise EvidenceManifestError(
                f"QA evidence source is outside the repository root: {path}"
            ) from exc
        relative = _require_repo_relative_path(
            str(relative_path).replace("\\", "/")
        )
        canonical = os.path.normcase(relative)
        if canonical in canonical_paths:
            raise EvidenceManifestError(
                f"duplicate QA evidence source path: {relative!r}"
            )
        canonical_paths.add(canonical)
        names.append(relative)
    return tuple(sorted(names))


def snapshot_supplied_source_hashes(
    root: Path, source_files: SourceFilesSupplier
) -> SourceHashSnapshot:
    """Snapshot a freshly supplied set and prove its membership stayed stable."""

    source_files = _require_source_files_supplier(source_files)
    before = _enumerate_source_files(source_files)
    snapshot = snapshot_source_hashes(root, before)
    after = _enumerate_source_files(source_files)
    if _source_file_names(root, before) != _source_file_names(root, after):
        raise EvidenceManifestError(
            "QA evidence source set changed while it was being snapshotted"
        )
    for relative, revision in snapshot.revisions:
        _recheck_regular_file_revision(
            root / Path(relative),
            revision,
            expected_digest=snapshot[relative],
            description=f"QA evidence source {relative!r}",
        )
    return snapshot


def validate_output_parent(root: Path, output: Path) -> None:
    """Require every existing lexical output ancestor to be a real directory.

    Missing trailing parents are allowed so the producer may create them.  The
    repository root and every ancestor that already exists are inspected
    without resolving links, including Windows reparse-point metadata.
    """

    if not root.is_absolute():
        raise EvidenceManifestError("QA evidence output root must be an absolute path")
    if not output.is_absolute():
        raise EvidenceManifestError("QA evidence output must be an absolute path")
    try:
        relative_output = output.relative_to(root)
    except ValueError as exc:
        raise EvidenceManifestError(
            "QA evidence output must be lexically inside the repository root"
        ) from exc
    relative = str(relative_output).replace("\\", "/")
    _require_repo_relative_path(relative)

    inspected: list[tuple[Path, os.stat_result]] = []
    current = root
    candidates = [root]
    for part in relative_output.parts[:-1]:
        current /= part
        candidates.append(current)

    for candidate in candidates:
        try:
            identity = candidate.lstat()
        except FileNotFoundError as exc:
            if candidate == root:
                raise EvidenceManifestError(
                    "QA evidence output root must be an existing real directory"
                ) from exc
            break
        except OSError as exc:
            raise EvidenceManifestError(
                f"cannot inspect QA evidence output ancestor: {candidate}"
            ) from exc
        _require_real_output_directory(candidate, identity)
        inspected.append((candidate, identity))

    for candidate, before in inspected:
        try:
            after = candidate.lstat()
        except OSError as exc:
            raise EvidenceManifestError(
                f"QA evidence output ancestor changed during validation: {candidate}"
            ) from exc
        _require_real_output_directory(candidate, after)
        if not _same_file_identity(before, after):
            raise EvidenceManifestError(
                f"QA evidence output ancestor changed during validation: {candidate}"
            )


def _atomic_write(
    path: Path,
    value: bytes,
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _require_real_output_directory(path: Path, identity: os.stat_result) -> None:
    file_attributes = getattr(identity, "st_file_attributes", 0)
    reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(identity.st_mode)
        or bool(file_attributes & reparse_mask)
        or _is_link_or_junction(path)
    ):
        raise EvidenceManifestError(
            f"QA evidence output ancestor must not be a link or reparse point: {path}"
        )
    if not stat.S_ISDIR(identity.st_mode):
        raise EvidenceManifestError(
            f"QA evidence output ancestor is not a directory: {path}"
        )


def _remove_owned_lock(
    path: Path, *, identity: os.stat_result, marker: bytes
) -> None:
    """Remove a lock only while it is still the regular file we created."""

    try:
        if _is_link_or_junction(path):
            return
        current = path.lstat()
        if not stat.S_ISREG(current.st_mode) or not _same_file_identity(
            current, identity
        ):
            return
        if _read_stable_regular_file(path) != marker:
            return
        current = path.lstat()
        if not stat.S_ISREG(current.st_mode) or not _same_file_identity(
            current, identity
        ):
            return
        path.unlink()
    except (OSError, EvidenceManifestError):
        # Leaving a stale lock is fail-closed.  A later manual audit can remove it.
        return


@contextmanager
def _exclusive_pytest_completion(run_dir: Path):
    """Serialize the one-way incomplete-to-complete pytest transition."""

    if _is_link_or_junction(run_dir) or not run_dir.is_dir():
        raise EvidenceManifestError(
            "pytest evidence run directory must not be a link or junction"
        )
    lock_path = run_dir / _PYTEST_COMPLETION_LOCK
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    marker = os.urandom(32).hex().encode("ascii")
    descriptor: int | None = None
    identity: os.stat_result | None = None
    try:
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except FileExistsError as exc:
            raise EvidenceManifestError(
                "pytest evidence completion lock already exists"
            ) from exc
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            raise EvidenceManifestError(
                "pytest evidence completion lock is not a regular file"
            )
        offset = 0
        while offset < len(marker):
            written = os.write(descriptor, marker[offset:])
            if written <= 0:
                raise OSError("could not write pytest evidence completion lock")
            offset += written
        os.fsync(descriptor)
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if identity is not None:
            _remove_owned_lock(lock_path, identity=identity, marker=marker)


def commit_browser_result(out: Path, browser_bytes: bytes) -> None:
    """Atomically commit an unsealed result, including an incomplete harness run."""

    if type(browser_bytes) is not bytes:
        raise EvidenceManifestError("browser result must be encoded as bytes")
    if _is_link_or_junction(out) or not out.is_dir():
        raise EvidenceManifestError("browser evidence run directory must not be a link or junction")
    manifest = out / "source-hashes.json"
    if manifest.exists() or _is_link_or_junction(manifest):
        raise EvidenceManifestError("browser evidence run is already sealed")
    artifact = out / "browser.json"
    if artifact.exists() or _is_link_or_junction(artifact):
        raise EvidenceManifestError("browser evidence run already has a result")
    _atomic_write(artifact, browser_bytes)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise EvidenceManifestError(f"invalid JSON constant: {value}")


def _require_sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EvidenceManifestError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_repo_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceManifestError("source hash path must be a non-empty string")
    if "\\" in value or ":" in value or value.startswith("/"):
        raise EvidenceManifestError(f"source hash path is not repository-relative: {value!r}")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise EvidenceManifestError(f"source hash path is not canonical: {value!r}")
    if any(ord(character) < 32 for character in value):
        raise EvidenceManifestError(f"source hash path contains a control character: {value!r}")
    return value


def _validate_source_hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise EvidenceManifestError("source_hashes must be an object")
    validated: dict[str, str] = {}
    for source_path, digest in value.items():
        validated[_require_repo_relative_path(source_path)] = _require_sha256(
            digest, field=f"source_hashes[{source_path!r}]"
        )
    return validated


def _validate_source_hash_snapshot(
    value: Any,
) -> tuple[dict[str, str], tuple[tuple[str, SourceRevision], ...]]:
    if not isinstance(value, SourceHashSnapshot):
        raise EvidenceManifestError(
            "source_hashes_at_start must be the intact result of "
            "snapshot_source_hashes"
        )
    hashes = _validate_source_hashes(value)
    if {relative for relative, _revision in value.revisions} != set(hashes):
        raise EvidenceManifestError(
            "source hash snapshot revision paths do not match its hashes"
        )
    return hashes, value.revisions


def _decode_strict_json(value: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except EvidenceManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceManifestError(f"cannot decode {label}: {exc}") from exc


def _load_manifest_json(value: bytes) -> dict[str, Any]:
    envelope = _decode_strict_json(value, label="evidence manifest")
    if not isinstance(envelope, dict) or set(envelope) != _ROOT_KEYS:
        raise EvidenceManifestError("evidence manifest must have exactly five root keys")
    return envelope


def _is_link_or_junction(path: Path) -> bool:
    try:
        is_junction = getattr(os.path, "isjunction", lambda _path: False)
        return path.is_symlink() or bool(is_junction(path))
    except OSError:
        return True


class _WindowsFileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", ctypes.c_ulong),
    ]


def _windows_extended_path(path: Path) -> str:
    value = os.path.abspath(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _windows_change_time_ns(path: Path) -> int:
    """Return the non-user-settable NTFS change time for one lexical path."""

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
    handle = create_file(
        _windows_extended_path(path),
        0x0080,  # FILE_READ_ATTRIBUTES
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x02000000,  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
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


def _file_revision(
    value: os.stat_result, *, change_time_ns: int
) -> SourceRevision:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        change_time_ns,
    )


def _snapshot_path_revision(
    path: Path, *, description: str
) -> tuple[os.stat_result, SourceRevision]:
    """Capture stat plus ChangeTime at one stable point."""

    try:
        before = path.lstat()
        before_change = _windows_change_time_ns(path)
        after = path.lstat()
        after_change = _windows_change_time_ns(path)
    except OSError as exc:
        raise EvidenceManifestError(
            f"cannot inspect {description}: {exc}"
        ) from exc
    before_revision = _file_revision(
        before, change_time_ns=before_change
    )
    after_revision = _file_revision(after, change_time_ns=after_change)
    if before_revision != after_revision:
        raise EvidenceManifestError(
            f"{description} changed while its revision was captured"
        )
    return after, after_revision


def _recheck_regular_file_revision(
    path: Path,
    expected: SourceRevision,
    *,
    expected_digest: str,
    description: str,
) -> None:
    try:
        value, revision = _read_stable_regular_file_with_revision(
            path, description=description
        )
    except (OSError, EvidenceManifestError) as exc:
        raise EvidenceManifestError(
            f"cannot recheck {description}: {exc}"
        ) from exc
    if (
        revision != expected
        or _sha256_bytes(value) != expected_digest
    ):
        raise EvidenceManifestError(f"{description} changed during validation")


def _recheck_directory_revision(
    path: Path,
    expected: SourceRevision,
    *,
    description: str,
) -> None:
    if _is_link_or_junction(path):
        raise EvidenceManifestError(f"{description} changed during validation")
    identity, revision = _snapshot_path_revision(
        path, description=description
    )
    if (
        not stat.S_ISDIR(identity.st_mode)
        or revision != expected
        or _is_link_or_junction(path)
    ):
        raise EvidenceManifestError(f"{description} changed during validation")


def _snapshot_real_directory_revision(path: Path, *, description: str) -> SourceRevision:
    if _is_link_or_junction(path):
        raise EvidenceManifestError(f"{description} must not be a link or junction")
    identity, revision = _snapshot_path_revision(
        path, description=description
    )
    if not stat.S_ISDIR(identity.st_mode) or _is_link_or_junction(path):
        raise EvidenceManifestError(f"{description} must be a real directory")
    return revision


def _recheck_whole_envelope(
    run_dir: Path,
    run_revision: SourceRevision,
    file_revisions: dict[Path, tuple[SourceRevision, str, str]],
) -> None:
    """Re-read every envelope file and prove its exact bytes and revision."""

    for path, (revision, description, digest) in file_revisions.items():
        _recheck_regular_file_revision(
            path,
            revision,
            expected_digest=digest,
            description=description,
        )
    _recheck_directory_revision(
        run_dir,
        run_revision,
        description="QA evidence run directory",
    )


def _read_stable_regular_file_with_revision(
    path: Path, *, description: str | None = None
) -> tuple[bytes, SourceRevision]:
    display = description or path.name
    if _is_link_or_junction(path) or not path.is_file():
        location = "run-local " if description is None else ""
        raise EvidenceManifestError(
            f"{display} must be a {location}regular file: {path.name}"
        )
    try:
        before, identity_before = _snapshot_path_revision(
            path, description=display
        )
        value = path.read_bytes()
        after, identity_after = _snapshot_path_revision(
            path, description=display
        )
    except (OSError, EvidenceManifestError) as exc:
        raise EvidenceManifestError(f"cannot read {display}: {exc}") from exc
    if _is_link_or_junction(path) or identity_before != identity_after:
        raise EvidenceManifestError(
            f"{display} changed while it was being validated"
        )
    return value, identity_after


def _read_stable_regular_file(
    path: Path, *, description: str | None = None
) -> bytes:
    value, _revision = _read_stable_regular_file_with_revision(
        path, description=description
    )
    return value


def load_sealed_browser_evidence(
    run_dir: Path, *, expected_kind: str | None = None
) -> SealedBrowserEvidence:
    """Validate a run-local envelope and return the exact result bytes it seals.

    Generic consumers may derive the runner kind from the validated manifest by
    omitting ``expected_kind``.  Runner-specific consumers can still pin the
    expected kind to prevent evidence type confusion.
    """

    if expected_kind is not None and (
        not isinstance(expected_kind, str) or expected_kind not in _KINDS
    ):
        raise EvidenceManifestError("expected_kind must name a supported browser runner")
    run_revision = _snapshot_real_directory_revision(
        run_dir, description="browser evidence run directory"
    )
    path = run_dir / "source-hashes.json"
    artifact_path = run_dir / "browser.json"
    manifest_bytes, manifest_revision = _read_stable_regular_file_with_revision(path)
    envelope = _load_manifest_json(manifest_bytes)
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise EvidenceManifestError("unsupported browser evidence schema_version")
    kind = envelope["kind"]
    if (
        not isinstance(kind, str)
        or kind not in _KINDS
        or (expected_kind is not None and kind != expected_kind)
    ):
        raise EvidenceManifestError("browser evidence kind does not match the expected runner")
    if envelope["complete"] is not True:
        raise EvidenceManifestError("browser evidence manifest is not complete")

    _validate_source_hashes(envelope["source_hashes"])

    artifacts = envelope["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != {"browser.json"}:
        raise EvidenceManifestError("artifacts must contain exactly browser.json")
    expected_digest = _require_sha256(
        artifacts["browser.json"], field="artifacts['browser.json']"
    )
    browser_bytes, browser_revision = _read_stable_regular_file_with_revision(
        artifact_path
    )
    actual_digest = _sha256_bytes(browser_bytes)
    if actual_digest != expected_digest:
        raise EvidenceManifestError("browser.json digest does not match the manifest")
    browser_payload = _decode_strict_json(browser_bytes, label="browser.json")
    if not isinstance(browser_payload, dict):
        raise EvidenceManifestError("browser.json root must be an object")
    _recheck_whole_envelope(
        run_dir,
        run_revision,
        {
            path: (
                manifest_revision,
                "browser evidence manifest",
                _sha256_bytes(manifest_bytes),
            ),
            artifact_path: (browser_revision, "browser.json", actual_digest),
        },
    )
    return SealedBrowserEvidence(
        manifest=envelope,
        manifest_bytes=manifest_bytes,
        manifest_sha256=_sha256_bytes(manifest_bytes),
        browser_bytes=browser_bytes,
        browser_payload=browser_payload,
    )


def _load_sealed_pytest_evidence(
    run_dir: Path,
    *,
    expected_complete: bool,
    allow_unsealed_story: bool = False,
) -> SealedPytestEvidence:
    if type(expected_complete) is not bool:
        raise EvidenceManifestError("expected_complete must be a boolean")
    run_revision = _snapshot_real_directory_revision(
        run_dir, description="pytest evidence run directory"
    )

    manifest_path = run_dir / "source-hashes.json"
    manifest_bytes, manifest_revision = _read_stable_regular_file_with_revision(
        manifest_path
    )
    envelope = _load_manifest_json(manifest_bytes)
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise EvidenceManifestError("unsupported pytest evidence schema_version")
    if envelope["kind"] != "pytest":
        raise EvidenceManifestError("pytest evidence kind must be pytest")
    complete = envelope["complete"]
    if type(complete) is not bool:
        raise EvidenceManifestError("pytest evidence complete must be a boolean")
    if complete is not expected_complete:
        if complete:
            raise EvidenceManifestError("pytest evidence is already complete")
        raise EvidenceManifestError("pytest evidence manifest is not complete")
    _validate_source_hashes(envelope["source_hashes"])

    expected_names = (
        _PYTEST_COMPLETE_ARTIFACTS
        if expected_complete
        else _PYTEST_INCOMPLETE_ARTIFACTS
    )
    artifacts = envelope["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != set(expected_names):
        count = "four" if expected_complete else "three"
        raise EvidenceManifestError(
            f"pytest artifacts must contain exactly the {count} expected files"
        )

    artifact_bytes: dict[str, bytes] = {}
    artifact_revisions: dict[Path, tuple[SourceRevision, str, str]] = {}
    for name in expected_names:
        expected_digest = _require_sha256(
            artifacts[name], field=f"artifacts[{name!r}]"
        )
        artifact_path = run_dir / name
        value, revision = _read_stable_regular_file_with_revision(artifact_path)
        if _sha256_bytes(value) != expected_digest:
            raise EvidenceManifestError(f"{name} digest does not match the manifest")
        artifact_bytes[name] = value
        artifact_revisions[artifact_path] = (
            revision,
            name,
            _sha256_bytes(value),
        )

    story_path = run_dir / "story-evidence.json"
    if (
        not expected_complete
        and not allow_unsealed_story
        and (story_path.exists() or _is_link_or_junction(story_path))
    ):
        raise EvidenceManifestError(
            "incomplete pytest evidence has an unsealed story-evidence.json"
        )

    run_payload = _decode_strict_json(artifact_bytes["run.json"], label="run.json")
    if not isinstance(run_payload, dict):
        raise EvidenceManifestError("run.json root must be an object")
    story_bytes = artifact_bytes.get("story-evidence.json")
    story_payload = None
    if story_bytes is not None:
        story_payload = _decode_strict_json(
            story_bytes, label="story-evidence.json"
        )
        if not isinstance(story_payload, dict):
            raise EvidenceManifestError("story-evidence.json root must be an object")
    _recheck_whole_envelope(
        run_dir,
        run_revision,
        {
            manifest_path: (
                manifest_revision,
                "pytest evidence manifest",
                _sha256_bytes(manifest_bytes),
            ),
            **artifact_revisions,
        },
    )
    return SealedPytestEvidence(
        manifest=envelope,
        manifest_bytes=manifest_bytes,
        manifest_sha256=_sha256_bytes(manifest_bytes),
        pytest_bytes=artifact_bytes["pytest.xml"],
        log_bytes=artifact_bytes["pytest.log"],
        run_bytes=artifact_bytes["run.json"],
        run_payload=run_payload,
        story_evidence_bytes=story_bytes,
        story_evidence_payload=story_payload,
    )


def load_sealed_pytest_evidence(
    run_dir: Path, *, expected_complete: bool
) -> SealedPytestEvidence:
    """Validate a pytest seal and return the exact artifact bytes it covers."""

    return _load_sealed_pytest_evidence(
        run_dir, expected_complete=expected_complete
    )


def load_browser_evidence_manifest(
    path: Path, *, expected_kind: str
) -> dict[str, Any]:
    """Compatibility wrapper returning only a validated envelope."""

    if path.name != "source-hashes.json":
        raise EvidenceManifestError("manifest must be the run-local source-hashes.json")
    return load_sealed_browser_evidence(
        path.parent, expected_kind=expected_kind
    ).manifest


def _guard_unsealed_publication(
    *,
    out: Path,
    root: Path,
    manifest_path: Path,
    source_files: SourceFilesSupplier,
    source_hashes_at_start: dict[str, str],
    source_revisions_at_start: tuple[tuple[str, SourceRevision], ...],
    artifact_bytes: dict[str, bytes],
) -> None:
    """Recheck every publication input in an atomic-write pre-replace callback."""

    try:
        validate_output_parent(root, manifest_path)
        if _is_link_or_junction(out) or not out.is_dir():
            raise EvidenceManifestError(
                "QA evidence run directory must be a real directory"
            )
        if manifest_path.exists() or _is_link_or_junction(manifest_path):
            raise EvidenceManifestError(
                "QA evidence manifest appeared before publication"
            )
        run_revision = _snapshot_real_directory_revision(
            out, description="QA evidence run directory"
        )
        committed: dict[str, bytes] = {}
        committed_revisions: dict[
            Path, tuple[SourceRevision, str, str]
        ] = {}
        for name in artifact_bytes:
            artifact_path = out / name
            value, revision = _read_stable_regular_file_with_revision(
                artifact_path
            )
            committed[name] = value
            committed_revisions[artifact_path] = (
                revision,
                name,
                _sha256_bytes(value),
            )
        sources_at_publish = snapshot_supplied_source_hashes(root, source_files)

        # Source collection can be long.  Recheck every artifact only after it
        # completes, so an early artifact cannot be persistently changed while
        # later sources are being hashed.
        _recheck_whole_envelope(out, run_revision, committed_revisions)
        # Conversely, an ordinary source edit may land while the compact
        # artifact set is being re-read.  Take one final whole-source snapshot,
        # then bind the artifacts once more.  Only the unavoidable last
        # callback-to-replace interval remains outside these stable reads.
        final_sources_at_publish = snapshot_supplied_source_hashes(
            root, source_files
        )
        _recheck_whole_envelope(out, run_revision, committed_revisions)

        # Recheck the output path after the file reads so an ancestor change
        # during the guard cannot be followed by a manifest publication.
        validate_output_parent(root, manifest_path)
        if _is_link_or_junction(out) or not out.is_dir():
            raise EvidenceManifestError(
                "QA evidence run directory changed before publication"
            )
        if manifest_path.exists() or _is_link_or_junction(manifest_path):
            raise EvidenceManifestError(
                "QA evidence manifest appeared before publication"
            )
    except (OSError, EvidenceManifestError) as exc:
        raise _PublicationGuardFailed from exc

    if (
        not isinstance(sources_at_publish, SourceHashSnapshot)
        or sources_at_publish != source_hashes_at_start
        or sources_at_publish.revisions != source_revisions_at_start
        or not isinstance(final_sources_at_publish, SourceHashSnapshot)
        or final_sources_at_publish != source_hashes_at_start
        or final_sources_at_publish.revisions != source_revisions_at_start
        or committed != artifact_bytes
    ):
        raise _PublicationGuardFailed


def publish_browser_evidence(
    out: Path,
    *,
    root: Path,
    kind: str,
    source_files: SourceFilesSupplier,
    source_hashes_at_start: SourceHashSnapshot,
    browser_bytes: bytes,
) -> bool:
    """Commit the result first and its envelope last when sources stayed stable."""

    if not isinstance(kind, str) or kind not in _KINDS:
        raise EvidenceManifestError("kind must name a supported browser runner")
    source_files = _require_source_files_supplier(source_files)
    validated_source_hashes_at_start, source_revisions_at_start = (
        _validate_source_hash_snapshot(source_hashes_at_start)
    )
    artifact = out / "browser.json"
    manifest = out / "source-hashes.json"
    if manifest.exists() or _is_link_or_junction(manifest):
        raise EvidenceManifestError("browser evidence run is already sealed")
    commit_browser_result(out, browser_bytes)
    try:
        sources_at_end = snapshot_supplied_source_hashes(root, source_files)
    except (OSError, EvidenceManifestError):
        return False
    if (
        not isinstance(sources_at_end, SourceHashSnapshot)
        or sources_at_end != validated_source_hashes_at_start
        or sources_at_end.revisions != source_revisions_at_start
    ):
        return False
    try:
        committed_bytes = _read_stable_regular_file(artifact)
    except (OSError, EvidenceManifestError):
        return False
    if committed_bytes != browser_bytes:
        return False
    envelope = {
        "schema_version": 1,
        "kind": kind,
        "complete": True,
        "source_hashes": validated_source_hashes_at_start,
        "artifacts": {"browser.json": _sha256_bytes(browser_bytes)},
    }
    manifest_bytes = json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        _atomic_write(
            manifest,
            manifest_bytes,
            before_replace=lambda: _guard_unsealed_publication(
                out=out,
                root=root,
                manifest_path=manifest,
                source_files=source_files,
                source_hashes_at_start=validated_source_hashes_at_start,
                source_revisions_at_start=source_revisions_at_start,
                artifact_bytes={"browser.json": browser_bytes},
            ),
        )
    except _PublicationGuardFailed:
        return False
    return True


def publish_pytest_evidence(
    out: Path,
    *,
    root: Path,
    source_files: SourceFilesSupplier,
    source_hashes_at_start: SourceHashSnapshot,
    pytest_bytes: bytes,
    log_bytes: bytes,
    run_bytes: bytes,
) -> bool:
    """Commit pytest artifacts and seal them incomplete when sources stayed stable."""

    artifacts = {
        "pytest.xml": pytest_bytes,
        "pytest.log": log_bytes,
        "run.json": run_bytes,
    }
    if any(type(value) is not bytes for value in artifacts.values()):
        raise EvidenceManifestError("pytest artifacts must be encoded as bytes")
    if _is_link_or_junction(out) or not out.is_dir():
        raise EvidenceManifestError("pytest evidence run directory must not be a link or junction")
    manifest_path = out / "source-hashes.json"
    if manifest_path.exists() or _is_link_or_junction(manifest_path):
        raise EvidenceManifestError("pytest evidence run is already sealed")
    for name in _PYTEST_COMPLETE_ARTIFACTS:
        artifact_path = out / name
        if artifact_path.exists() or _is_link_or_junction(artifact_path):
            raise EvidenceManifestError(f"pytest evidence run already has {name}")

    source_files = _require_source_files_supplier(source_files)
    validated_source_hashes_at_start, source_revisions_at_start = (
        _validate_source_hash_snapshot(source_hashes_at_start)
    )
    for name in _PYTEST_INCOMPLETE_ARTIFACTS:
        _atomic_write(out / name, artifacts[name])

    try:
        sources_at_end = snapshot_supplied_source_hashes(root, source_files)
        committed = {
            name: _read_stable_regular_file(out / name)
            for name in _PYTEST_INCOMPLETE_ARTIFACTS
        }
    except (OSError, EvidenceManifestError):
        return False
    if (
        not isinstance(sources_at_end, SourceHashSnapshot)
        or sources_at_end != validated_source_hashes_at_start
        or sources_at_end.revisions != source_revisions_at_start
        or committed != artifacts
    ):
        return False

    envelope = {
        "schema_version": 1,
        "kind": "pytest",
        "complete": False,
        "source_hashes": validated_source_hashes_at_start,
        "artifacts": {
            name: _sha256_bytes(artifacts[name])
            for name in _PYTEST_INCOMPLETE_ARTIFACTS
        },
    }
    manifest_bytes = json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        _atomic_write(
            manifest_path,
            manifest_bytes,
            before_replace=lambda: _guard_unsealed_publication(
                out=out,
                root=root,
                manifest_path=manifest_path,
                source_files=source_files,
                source_hashes_at_start=validated_source_hashes_at_start,
                source_revisions_at_start=source_revisions_at_start,
                artifact_bytes=artifacts,
            ),
        )
    except _PublicationGuardFailed:
        return False
    return True


def complete_pytest_evidence(
    run_dir: Path,
    *,
    story_evidence_bytes: bytes,
    expected_manifest_sha256: str,
) -> SealedPytestEvidence:
    """Add story evidence and transition an incomplete pytest seal exactly once."""

    if type(story_evidence_bytes) is not bytes:
        raise EvidenceManifestError("story evidence must be encoded as bytes")
    story_payload = _decode_strict_json(
        story_evidence_bytes, label="story-evidence.json"
    )
    if not isinstance(story_payload, dict):
        raise EvidenceManifestError("story-evidence.json root must be an object")
    expected_manifest_sha256 = _require_sha256(
        expected_manifest_sha256, field="expected_manifest_sha256"
    )

    with _exclusive_pytest_completion(run_dir):
        return _complete_pytest_evidence_locked(
            run_dir,
            story_evidence_bytes=story_evidence_bytes,
            story_payload=story_payload,
            expected_manifest_sha256=expected_manifest_sha256,
        )


def _complete_pytest_evidence_locked(
    run_dir: Path,
    *,
    story_evidence_bytes: bytes,
    story_payload: dict[str, Any],
    expected_manifest_sha256: str,
) -> SealedPytestEvidence:
    """Complete pytest evidence while the run-local exclusive lock is held."""

    incomplete = _load_sealed_pytest_evidence(
        run_dir, expected_complete=False
    )
    if incomplete.manifest_sha256 != expected_manifest_sha256:
        raise EvidenceManifestError(
            "pytest evidence manifest changed before completion"
        )

    story_path = run_dir / "story-evidence.json"

    def revalidate_before_story_commit() -> None:
        if story_path.exists() or _is_link_or_junction(story_path):
            raise EvidenceManifestError(
                "pytest evidence run already has unsealed story evidence"
            )
        current = _load_sealed_pytest_evidence(
            run_dir, expected_complete=False
        )
        if (
            current.manifest_sha256 != expected_manifest_sha256
            or current.manifest_bytes != incomplete.manifest_bytes
            or current.pytest_bytes != incomplete.pytest_bytes
            or current.log_bytes != incomplete.log_bytes
            or current.run_bytes != incomplete.run_bytes
        ):
            raise EvidenceManifestError(
                "pytest evidence changed before story evidence was committed"
            )

    _atomic_write(
        story_path,
        story_evidence_bytes,
        before_replace=revalidate_before_story_commit,
    )

    complete_manifest = {
        "schema_version": 1,
        "kind": "pytest",
        "complete": True,
        "source_hashes": dict(incomplete.manifest["source_hashes"]),
        "artifacts": {
            **dict(incomplete.manifest["artifacts"]),
            "story-evidence.json": _sha256_bytes(story_evidence_bytes),
        },
    }
    complete_manifest_bytes = json.dumps(
        complete_manifest, ensure_ascii=False, indent=2
    ).encode("utf-8")
    manifest_path = run_dir / "source-hashes.json"

    def revalidate_incomplete_seal() -> None:
        current = _load_sealed_pytest_evidence(
            run_dir,
            expected_complete=False,
            allow_unsealed_story=True,
        )
        if (
            current.manifest_sha256 != expected_manifest_sha256
            or current.manifest_bytes != incomplete.manifest_bytes
            or current.pytest_bytes != incomplete.pytest_bytes
            or current.log_bytes != incomplete.log_bytes
            or current.run_bytes != incomplete.run_bytes
        ):
            raise EvidenceManifestError(
                "pytest evidence changed while it was being completed"
            )
        if _read_stable_regular_file(story_path) != story_evidence_bytes:
            raise EvidenceManifestError(
                "story-evidence.json changed while it was being sealed"
            )

    _atomic_write(
        manifest_path,
        complete_manifest_bytes,
        before_replace=revalidate_incomplete_seal,
    )
    return SealedPytestEvidence(
        manifest=complete_manifest,
        manifest_bytes=complete_manifest_bytes,
        manifest_sha256=_sha256_bytes(complete_manifest_bytes),
        pytest_bytes=incomplete.pytest_bytes,
        log_bytes=incomplete.log_bytes,
        run_bytes=incomplete.run_bytes,
        run_payload=incomplete.run_payload,
        story_evidence_bytes=story_evidence_bytes,
        story_evidence_payload=story_payload,
    )
