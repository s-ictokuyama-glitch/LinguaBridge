from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from types import SimpleNamespace

import pytest

from scripts import qa_evidence_manifest as evidence


BROWSER_BYTES = b'{"results":[]}\n'
PYTEST_BYTES = b"<testsuite />\n"
PYTEST_LOG_BYTES = b"1 passed\n"
RUN_BYTES = b'{"exit_code":0,"finished_at":"2026-09-08T12:00:00Z"}\n'
STORY_EVIDENCE_BYTES = (
    b'{"cases":[{"nodeid":"qa_checks/test_stories.py::test_story_contract[S001]"}]}\n'
)


def _edit_and_restore_source(source: Path) -> None:
    """Model an editor safe-save followed by an undo to identical bytes."""

    original = source.read_bytes()
    source.write_bytes(b"transient edit\n")
    restore = source.with_name(f".{source.name}.restore")
    restore.write_bytes(original)
    restore.replace(source)


def _edit_in_place_and_restore_exposed_times(source: Path) -> None:
    """Restore bytes/size/mtime without replacing the file identity."""

    original = source.read_bytes()
    before = source.stat()
    changed = bytes([original[0] ^ 1]) + original[1:]
    with source.open("r+b") as handle:
        handle.write(changed)
        handle.flush()
        os.fsync(handle.fileno())
        handle.seek(0)
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))


def _published_incomplete_pytest_run(
    tmp_path: Path,
) -> tuple[Path, evidence.SealedPytestEvidence]:
    root = tmp_path / "repo"
    source = root / "scripts" / "story_tracker.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-pytest"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])
    assert evidence.publish_pytest_evidence(
        out,
        root=root,
        source_files=lambda: [source],
        source_hashes_at_start=start,
        pytest_bytes=PYTEST_BYTES,
        log_bytes=PYTEST_LOG_BYTES,
        run_bytes=RUN_BYTES,
    )
    return out, evidence.load_sealed_pytest_evidence(
        out, expected_complete=False
    )


def test_snapshot_source_hashes_rejects_an_empty_source_set(tmp_path: Path) -> None:
    with pytest.raises(evidence.EvidenceManifestError, match="must not be empty"):
        evidence.snapshot_source_hashes(tmp_path, [])


@pytest.mark.parametrize("source_kind", ["missing", "directory"])
def test_snapshot_source_hashes_requires_every_entry_to_be_a_regular_file(
    tmp_path: Path, source_kind: str,
) -> None:
    source = tmp_path / ("missing.py" if source_kind == "missing" else "directory")
    if source_kind == "directory":
        source.mkdir()

    with pytest.raises(evidence.EvidenceManifestError, match="regular file"):
        evidence.snapshot_source_hashes(tmp_path, [source])


def test_snapshot_source_hashes_rejects_a_lexically_outside_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"outside\n")

    with pytest.raises(evidence.EvidenceManifestError, match="outside the repository"):
        evidence.snapshot_source_hashes(root, [outside])


def test_snapshot_source_hashes_rejects_a_duplicate_canonical_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_bytes(b"source\n")

    with pytest.raises(evidence.EvidenceManifestError, match="duplicate"):
        evidence.snapshot_source_hashes(tmp_path, [source, source])


@pytest.mark.skipif(os.name != "nt", reason="Windows ChangeTime regression")
def test_snapshot_rejects_in_place_aba_during_the_bound_file_read(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "source.py"
    source.write_bytes(b"original\n")
    real_read = Path.read_bytes
    injected = False

    def read_then_restore_metadata(path: Path) -> bytes:
        nonlocal injected
        value = real_read(path)
        if path == source and not injected:
            injected = True
            _edit_in_place_and_restore_exposed_times(source)
        return value

    monkeypatch.setattr(Path, "read_bytes", read_then_restore_metadata)

    with pytest.raises(evidence.EvidenceManifestError, match="changed"):
        evidence.snapshot_source_hashes(root, [source])

    assert injected


@pytest.mark.parametrize("link_kind", ["file-symlink", "parent-symlink", "parent-junction"])
def test_snapshot_source_hashes_rejects_links_in_the_source_path(
    tmp_path: Path, monkeypatch, link_kind: str,
) -> None:
    parent = tmp_path / "web"
    parent.mkdir()
    source = parent / "student.js"
    source.write_bytes(b"source\n")
    real_is_symlink = Path.is_symlink
    real_is_junction = getattr(evidence.os.path, "isjunction", lambda _path: False)

    if link_kind == "file-symlink":
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda path: path == source or real_is_symlink(path),
        )
    elif link_kind == "parent-symlink":
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda path: path == parent or real_is_symlink(path),
        )
    else:
        monkeypatch.setattr(
            evidence.os.path,
            "isjunction",
            lambda path: Path(path) == parent or real_is_junction(path),
        )

    with pytest.raises(evidence.EvidenceManifestError, match="link or junction"):
        evidence.snapshot_source_hashes(tmp_path, [source])


def test_validate_output_parent_accepts_real_existing_ancestors(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    output = root / "docs" / "qa" / "runs" / "run-browser"

    evidence.validate_output_parent(root, output)

    assert not output.parent.exists()


def test_validate_output_parent_requires_an_existing_root(tmp_path: Path) -> None:
    root = tmp_path / "missing-repo"

    with pytest.raises(evidence.EvidenceManifestError, match="existing real"):
        evidence.validate_output_parent(root, root / "runs" / "run-browser")


@pytest.mark.parametrize("ancestor_kind", ["symlink", "reparse"])
def test_validate_output_parent_rejects_a_link_like_existing_ancestor(
    tmp_path: Path, monkeypatch, ancestor_kind: str,
) -> None:
    root = tmp_path / "repo"
    ancestor = root / "docs" / "qa"
    ancestor.mkdir(parents=True)
    output = ancestor / "runs" / "run-browser"
    real_lstat = Path.lstat
    reparse_mask = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def marked_lstat(path: Path):
        identity = real_lstat(path)
        if path != ancestor:
            return identity
        mode = identity.st_mode
        attributes = getattr(identity, "st_file_attributes", 0)
        if ancestor_kind == "symlink":
            mode = stat.S_IFLNK | stat.S_IMODE(mode)
        else:
            attributes |= reparse_mask
        return SimpleNamespace(
            st_mode=mode,
            st_dev=identity.st_dev,
            st_ino=identity.st_ino,
            st_file_attributes=attributes,
        )

    monkeypatch.setattr(Path, "lstat", marked_lstat)

    with pytest.raises(evidence.EvidenceManifestError, match="link or reparse"):
        evidence.validate_output_parent(root, output)


def test_snapshot_source_hashes_rejects_a_file_changed_during_read(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_bytes(b"before\n")
    real_read_bytes = Path.read_bytes

    def read_then_change(path: Path) -> bytes:
        value = real_read_bytes(path)
        if path == source:
            path.write_bytes(value + b"changed\n")
        return value

    monkeypatch.setattr(Path, "read_bytes", read_then_change)

    with pytest.raises(evidence.EvidenceManifestError, match="changed while"):
        evidence.snapshot_source_hashes(tmp_path, [source])


def test_snapshot_source_hashes_rechecks_an_earlier_file_after_the_full_scan(
    tmp_path: Path, monkeypatch,
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_bytes(b"first\n")
    second.write_bytes(b"second\n")
    real_read_bytes = Path.read_bytes

    def edit_first_while_reading_second(path: Path) -> bytes:
        value = real_read_bytes(path)
        if path == second:
            first.write_bytes(b"persistent edit\n")
        return value

    monkeypatch.setattr(Path, "read_bytes", edit_first_while_reading_second)

    with pytest.raises(evidence.EvidenceManifestError, match="changed during"):
        evidence.snapshot_source_hashes(tmp_path, [first, second])


def test_snapshot_source_hashes_rechecks_ancestors_after_the_full_scan(
    tmp_path: Path, monkeypatch,
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_bytes(b"first\n")
    second.write_bytes(b"second\n")
    real_read_bytes = Path.read_bytes

    def add_entry_while_reading_second(path: Path) -> bytes:
        value = real_read_bytes(path)
        if path == second:
            (tmp_path / "persistent-added.py").write_bytes(b"added\n")
        return value

    monkeypatch.setattr(Path, "read_bytes", add_entry_while_reading_second)

    with pytest.raises(evidence.EvidenceManifestError, match="source parent changed"):
        evidence.snapshot_source_hashes(tmp_path, [first, second])


def test_snapshot_supplied_source_hashes_rejects_a_changed_enumeration(
    tmp_path: Path,
) -> None:
    source = tmp_path / "first.py"
    source.write_bytes(b"first\n")
    enumerations = 0

    def source_files() -> list[Path]:
        nonlocal enumerations
        enumerations += 1
        if enumerations == 2:
            (tmp_path / "persistent-added.py").write_bytes(b"added\n")
        return sorted(tmp_path.glob("*.py"))

    with pytest.raises(evidence.EvidenceManifestError, match="source set changed"):
        evidence.snapshot_supplied_source_hashes(tmp_path, source_files)


def test_publish_browser_evidence_commits_artifact_before_exact_manifest(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "repo"
    source = root / "web" / "student.js"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"alpha\n")
    out = root / "docs" / "qa" / "runs" / "run-browser"
    out.mkdir(parents=True)
    start = evidence.snapshot_source_hashes(root, [source])
    committed = []
    real_replace = evidence.os.replace

    def recording_replace(source_path, target_path):
        committed.append(Path(target_path).name)
        real_replace(source_path, target_path)

    monkeypatch.setattr(evidence.os, "replace", recording_replace)

    assert evidence.publish_browser_evidence(
        out,
        root=root,
        kind="browser",
        source_files=lambda: (item for item in [source]),
        source_hashes_at_start=start,
        browser_bytes=BROWSER_BYTES,
    )

    assert committed == ["browser.json", "source-hashes.json"]
    assert (out / "browser.json").read_bytes() == BROWSER_BYTES
    assert json.loads((out / "source-hashes.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "kind": "browser",
        "complete": True,
        "source_hashes": {
            "web/student.js": "b6a98d9ce9a2d9149288fa3df42d377c3e42737afdcdaf714e33c0a100b51060"
        },
        "artifacts": {
            "browser.json": "250a97e605ccc127d0b54b8d69907118ccd5d0a3352b27819891e2dc8c5b4046"
        },
    }
    assert not list(out.glob(".*.tmp"))


def test_publish_browser_evidence_keeps_result_unsealed_when_a_source_drifts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    source = root / "scripts" / "qa_browser.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"before\n")
    out = root / "docs" / "qa" / "runs" / "run-browser"
    out.mkdir(parents=True)
    start = evidence.snapshot_source_hashes(root, [source])
    source.write_bytes(b"after\n")

    assert not evidence.publish_browser_evidence(
        out,
        root=root,
        kind="browser",
        source_files=lambda: [source],
        source_hashes_at_start=start,
        browser_bytes=BROWSER_BYTES,
    )

    assert (out / "browser.json").read_bytes() == BROWSER_BYTES
    assert not (out / "source-hashes.json").exists()


def test_publish_browser_evidence_keeps_source_aba_result_unsealed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    source = root / "scripts" / "qa_browser.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original\n")
    out = root / "run-browser"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])

    _edit_and_restore_source(source)
    end = evidence.snapshot_source_hashes(root, [source])

    assert end == start
    assert end.revisions != start.revisions
    assert not evidence.publish_browser_evidence(
        out,
        root=root,
        kind="browser",
        source_files=lambda: [source],
        source_hashes_at_start=start,
        browser_bytes=BROWSER_BYTES,
    )
    assert (out / "browser.json").read_bytes() == BROWSER_BYTES
    assert not (out / "source-hashes.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ChangeTime regression")
@pytest.mark.parametrize("producer_kind", ["browser", "pytest"])
def test_publish_rejects_in_place_source_aba_with_restored_stat_fields(
    tmp_path: Path, producer_kind: str
) -> None:
    root = tmp_path / "repo"
    source = root / "source.py"
    root.mkdir()
    source.write_bytes(b"original\n")
    out = root / f"run-{producer_kind}"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])

    _edit_in_place_and_restore_exposed_times(source)
    end = evidence.snapshot_source_hashes(root, [source])

    assert dict(end) == dict(start)
    # dev/ino/size/mtime/ctime collide in the real Windows failure mode; the
    # non-serialized ChangeTime token is the distinguishing final element.
    assert end.revisions[0][1][:-1] == start.revisions[0][1][:-1]
    assert end.revisions != start.revisions
    if producer_kind == "browser":
        published = evidence.publish_browser_evidence(
            out,
            root=root,
            kind="browser",
            source_files=lambda: [source],
            source_hashes_at_start=start,
            browser_bytes=BROWSER_BYTES,
        )
    else:
        published = evidence.publish_pytest_evidence(
            out,
            root=root,
            source_files=lambda: [source],
            source_hashes_at_start=start,
            pytest_bytes=PYTEST_BYTES,
            log_bytes=PYTEST_LOG_BYTES,
            run_bytes=RUN_BYTES,
        )

    assert published is False
    assert not (out / "source-hashes.json").exists()


def test_regular_file_final_fence_checks_digest_even_if_revision_collides(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(b"sealed-A\n")
    expected_bytes, _old_revision = (
        evidence._read_stable_regular_file_with_revision(artifact)
    )
    artifact.write_bytes(b"sealed-B\n")
    _replacement_bytes, colliding_revision = (
        evidence._read_stable_regular_file_with_revision(artifact)
    )

    with pytest.raises(evidence.EvidenceManifestError, match="changed"):
        evidence._recheck_regular_file_revision(
            artifact,
            colliding_revision,
            expected_digest=hashlib.sha256(expected_bytes).hexdigest(),
            description="sealed artifact",
        )


def test_publish_browser_evidence_rejects_a_plain_start_hash_dict(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    source = root / "source.py"
    root.mkdir()
    source.write_bytes(b"stable\n")
    out = root / "run-browser"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])

    with pytest.raises(evidence.EvidenceManifestError, match="intact result"):
        evidence.publish_browser_evidence(
            out,
            root=root,
            kind="browser",
        source_files=lambda: [source],
            source_hashes_at_start=dict(start),
            browser_bytes=BROWSER_BYTES,
        )

    assert not any(out.iterdir())


def test_load_browser_evidence_manifest_rejects_a_tampered_result(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source = root / "web" / "student.js"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-browser"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])
    assert evidence.publish_browser_evidence(
        out,
        root=root,
        kind="browser",
        source_files=lambda: [source],
        source_hashes_at_start=start,
        browser_bytes=BROWSER_BYTES,
    )
    (out / "browser.json").write_bytes(b'{"results":[{"status":"pass"}]}\n')

    with pytest.raises(evidence.EvidenceManifestError, match="browser.json digest"):
        evidence.load_browser_evidence_manifest(
            out / "source-hashes.json", expected_kind="browser"
        )


def test_load_sealed_browser_evidence_returns_the_bytes_it_hashed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source = root / "web" / "student.js"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-native-browser"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])
    assert evidence.publish_browser_evidence(
        out,
        root=root,
        kind="native-browser",
        source_files=lambda: [source],
        source_hashes_at_start=start,
        browser_bytes=BROWSER_BYTES,
    )

    loaded = evidence.load_sealed_browser_evidence(
        out, expected_kind="native-browser"
    )

    assert loaded.browser_bytes == BROWSER_BYTES
    assert loaded.browser_payload == {"results": []}
    assert loaded.manifest_bytes == (out / "source-hashes.json").read_bytes()
    assert len(loaded.manifest_sha256) == 64
    assert loaded.manifest["kind"] == "native-browser"


def test_publish_browser_evidence_refuses_to_replace_an_existing_seal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    source = root / "web" / "student.js"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-browser"
    out.mkdir()
    (out / "browser.json").write_bytes(b"old result")
    (out / "source-hashes.json").write_bytes(b"old seal")
    start = evidence.snapshot_source_hashes(root, [source])

    with pytest.raises(evidence.EvidenceManifestError, match="already sealed"):
        evidence.publish_browser_evidence(
            out,
            root=root,
            kind="browser",
        source_files=lambda: [source],
            source_hashes_at_start=start,
            browser_bytes=BROWSER_BYTES,
        )

    assert (out / "browser.json").read_bytes() == b"old result"
    assert (out / "source-hashes.json").read_bytes() == b"old seal"


@pytest.mark.parametrize(
    ("browser_bytes", "message"),
    [
        (b'{"results":[],"results":[]}\n', "duplicate JSON key"),
        (b'{"value":NaN}\n', "invalid JSON constant"),
    ],
)
def test_load_sealed_browser_evidence_strictly_decodes_browser_json(
    tmp_path: Path, browser_bytes: bytes, message: str,
) -> None:
    root = tmp_path / "repo"
    source = root / "web" / "student.js"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-browser"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])
    assert evidence.publish_browser_evidence(
        out,
        root=root,
        kind="browser",
        source_files=lambda: [source],
        source_hashes_at_start=start,
        browser_bytes=browser_bytes,
    )

    with pytest.raises(evidence.EvidenceManifestError, match=message):
        evidence.load_sealed_browser_evidence(out, expected_kind="browser")


def test_publish_browser_evidence_leaves_no_manifest_when_manifest_commit_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "repo"
    source = root / "web" / "student.js"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-browser"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])
    real_replace = evidence.os.replace

    def fail_manifest_replace(source_path, target_path):
        if Path(target_path).name == "source-hashes.json":
            raise OSError("simulated interrupted seal")
        real_replace(source_path, target_path)

    monkeypatch.setattr(evidence.os, "replace", fail_manifest_replace)

    with pytest.raises(OSError, match="interrupted seal"):
        evidence.publish_browser_evidence(
            out,
            root=root,
            kind="browser",
        source_files=lambda: [source],
            source_hashes_at_start=start,
            browser_bytes=BROWSER_BYTES,
        )

    assert (out / "browser.json").read_bytes() == BROWSER_BYTES
    assert not (out / "source-hashes.json").exists()
    assert not list(out.glob(".*.tmp"))


def test_publish_browser_evidence_rejects_an_unknown_runner_kind(tmp_path: Path) -> None:
    out = tmp_path / "run-browser"
    out.mkdir()

    with pytest.raises(evidence.EvidenceManifestError, match="supported browser runner"):
        evidence.publish_browser_evidence(
            out,
            root=tmp_path,
            kind="chromium",
            source_files=lambda: [],
            source_hashes_at_start={},
            browser_bytes=BROWSER_BYTES,
        )

    assert not list(out.iterdir())


@pytest.mark.parametrize("linked_name", ["browser.json", "source-hashes.json"])
def test_load_sealed_browser_evidence_rejects_linked_run_files(
    tmp_path: Path, monkeypatch, linked_name: str,
) -> None:
    root = tmp_path / "repo"
    source = root / "web" / "student.js"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-browser"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])
    assert evidence.publish_browser_evidence(
        out,
        root=root,
        kind="browser",
        source_files=lambda: [source],
        source_hashes_at_start=start,
        browser_bytes=BROWSER_BYTES,
    )
    real_is_symlink = Path.is_symlink

    def mark_run_file_as_link(path: Path) -> bool:
        return path.name == linked_name or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", mark_run_file_as_link)

    with pytest.raises(evidence.EvidenceManifestError, match="run-local regular file"):
        evidence.load_sealed_browser_evidence(out, expected_kind="browser")


def test_load_sealed_browser_evidence_rejects_a_junction_artifact(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "repo"
    source = root / "web" / "student.js"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-browser"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])
    assert evidence.publish_browser_evidence(
        out,
        root=root,
        kind="browser",
        source_files=lambda: [source],
        source_hashes_at_start=start,
        browser_bytes=BROWSER_BYTES,
    )
    real_isjunction = getattr(evidence.os.path, "isjunction", lambda _path: False)

    def mark_artifact_as_junction(path) -> bool:
        return Path(path).name == "browser.json" or real_isjunction(path)

    monkeypatch.setattr(evidence.os.path, "isjunction", mark_artifact_as_junction)

    with pytest.raises(evidence.EvidenceManifestError, match="run-local regular file"):
        evidence.load_sealed_browser_evidence(out, expected_kind="browser")


def test_load_sealed_browser_evidence_rejects_manifest_changed_during_read(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "repo"
    source = root / "web" / "student.js"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-browser"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])
    assert evidence.publish_browser_evidence(
        out,
        root=root,
        kind="browser",
        source_files=lambda: [source],
        source_hashes_at_start=start,
        browser_bytes=BROWSER_BYTES,
    )
    manifest_path = out / "source-hashes.json"
    real_read_bytes = Path.read_bytes

    def read_then_change(path: Path) -> bytes:
        value = real_read_bytes(path)
        if path == manifest_path:
            path.write_bytes(value + b" ")
        return value

    monkeypatch.setattr(Path, "read_bytes", read_then_change)

    with pytest.raises(evidence.EvidenceManifestError, match="changed while"):
        evidence.load_sealed_browser_evidence(out, expected_kind="browser")


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("duplicate_key", "duplicate JSON key"),
        ("unsafe_source_path", "canonical"),
        ("uppercase_digest", "lowercase SHA-256"),
        ("extra_artifact", "exactly browser.json"),
        ("extra_root_key", "exactly five root keys"),
        ("wrong_kind", "expected runner"),
    ],
)
def test_load_browser_evidence_manifest_rejects_noncanonical_envelopes(
    tmp_path: Path, case: str, message: str,
) -> None:
    out = tmp_path / "run-browser"
    out.mkdir()
    (out / "browser.json").write_bytes(BROWSER_BYTES)
    manifest = {
        "schema_version": 1,
        "kind": "browser",
        "complete": True,
        "source_hashes": {
            "web/student.js": "b6a98d9ce9a2d9149288fa3df42d377c3e42737afdcdaf714e33c0a100b51060"
        },
        "artifacts": {
            "browser.json": "250a97e605ccc127d0b54b8d69907118ccd5d0a3352b27819891e2dc8c5b4046"
        },
    }
    if case == "unsafe_source_path":
        manifest["source_hashes"] = {
            "../web/student.js": next(iter(manifest["source_hashes"].values()))
        }
    elif case == "uppercase_digest":
        manifest["artifacts"]["browser.json"] = manifest["artifacts"][
            "browser.json"
        ].upper()
    elif case == "extra_artifact":
        manifest["artifacts"]["screenshot.png"] = "0" * 64
    elif case == "extra_root_key":
        manifest["finished"] = True
    elif case == "wrong_kind":
        manifest["kind"] = "native-browser"
    raw = json.dumps(manifest)
    if case == "duplicate_key":
        raw = raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1)
    (out / "source-hashes.json").write_text(raw, encoding="utf-8")

    with pytest.raises(evidence.EvidenceManifestError, match=message):
        evidence.load_browser_evidence_manifest(
            out / "source-hashes.json", expected_kind="browser"
        )


def test_publish_pytest_evidence_commits_three_artifacts_before_incomplete_manifest(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "repo"
    source = root / "scripts" / "story_tracker.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "docs" / "qa" / "runs" / "run-pytest"
    out.mkdir(parents=True)
    start = evidence.snapshot_source_hashes(root, [source])
    committed = []
    real_replace = evidence.os.replace

    def recording_replace(source_path, target_path):
        committed.append(Path(target_path).name)
        real_replace(source_path, target_path)

    monkeypatch.setattr(evidence.os, "replace", recording_replace)

    assert evidence.publish_pytest_evidence(
        out,
        root=root,
        source_files=lambda: [source],
        source_hashes_at_start=start,
        pytest_bytes=PYTEST_BYTES,
        log_bytes=PYTEST_LOG_BYTES,
        run_bytes=RUN_BYTES,
    )

    assert committed == [
        "pytest.xml",
        "pytest.log",
        "run.json",
        "source-hashes.json",
    ]
    assert json.loads((out / "source-hashes.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "kind": "pytest",
        "complete": False,
        "source_hashes": start,
        "artifacts": {
            "pytest.xml": "160869d23a0759d0bb99b5e846b0e409507f02cbf60f7b58187d67100fddf50b",
            "pytest.log": "a92b7fdcb45e1d22fc2af4c80adc6e7fc1389ff8a694010cf5e6ff0b5ffbf1f6",
            "run.json": "182e56fd9023eaa75527b3760f03d81ba236726ed5fe4b8459be56f4e40b244e",
        },
    }


def test_load_sealed_pytest_evidence_returns_exact_incomplete_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    source = root / "scripts" / "story_tracker.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-pytest"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])
    assert evidence.publish_pytest_evidence(
        out,
        root=root,
        source_files=lambda: [source],
        source_hashes_at_start=start,
        pytest_bytes=PYTEST_BYTES,
        log_bytes=PYTEST_LOG_BYTES,
        run_bytes=RUN_BYTES,
    )

    loaded = evidence.load_sealed_pytest_evidence(out, expected_complete=False)

    assert loaded.pytest_bytes == PYTEST_BYTES
    assert loaded.log_bytes == PYTEST_LOG_BYTES
    assert loaded.run_bytes == RUN_BYTES
    assert loaded.run_payload == {
        "exit_code": 0,
        "finished_at": "2026-09-08T12:00:00Z",
    }
    assert loaded.story_evidence_bytes is None
    assert loaded.story_evidence_payload is None
    assert loaded.manifest_bytes == (out / "source-hashes.json").read_bytes()
    assert len(loaded.manifest_sha256) == 64
    assert loaded.manifest["kind"] == "pytest"
    assert loaded.manifest["complete"] is False


@pytest.mark.parametrize("producer_kind", ["browser", "pytest"])
@pytest.mark.parametrize("tamper_target", ["earlier-file", "run-directory"])
def test_load_sealed_evidence_rechecks_the_whole_envelope_after_all_reads(
    tmp_path: Path,
    monkeypatch,
    producer_kind: str,
    tamper_target: str,
) -> None:
    if producer_kind == "browser":
        root = tmp_path / "repo"
        source = root / "source.py"
        root.mkdir()
        source.write_bytes(b"stable\n")
        out = root / "run-browser"
        out.mkdir()
        start = evidence.snapshot_source_hashes(root, [source])
        assert evidence.publish_browser_evidence(
            out,
            root=root,
            kind="browser",
            source_files=lambda: [source],
            source_hashes_at_start=start,
            browser_bytes=BROWSER_BYTES,
        )
        last_read = out / "browser.json"
        earlier = out / "source-hashes.json"
    else:
        out, _sealed = _published_incomplete_pytest_run(tmp_path)
        last_read = out / "run.json"
        earlier = out / "pytest.xml"

    real_read = evidence._read_stable_regular_file_with_revision
    tampered = False

    def read_then_tamper(path: Path, *, description=None):
        nonlocal tampered
        result = real_read(path, description=description)
        if path == last_read and not tampered:
            tampered = True
            if tamper_target == "earlier-file":
                earlier.write_bytes(earlier.read_bytes() + b"persistent tamper\n")
            else:
                (out / "persistent-extra-file").write_bytes(b"extra\n")
        return result

    monkeypatch.setattr(
        evidence, "_read_stable_regular_file_with_revision", read_then_tamper
    )

    with pytest.raises(evidence.EvidenceManifestError, match="changed during validation"):
        if producer_kind == "browser":
            evidence.load_sealed_browser_evidence(out)
        else:
            evidence.load_sealed_pytest_evidence(out, expected_complete=False)

    assert tampered


def test_complete_pytest_evidence_adds_story_and_completes_manifest(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "repo"
    source = root / "scripts" / "story_tracker.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-pytest"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])
    assert evidence.publish_pytest_evidence(
        out,
        root=root,
        source_files=lambda: [source],
        source_hashes_at_start=start,
        pytest_bytes=PYTEST_BYTES,
        log_bytes=PYTEST_LOG_BYTES,
        run_bytes=RUN_BYTES,
    )
    incomplete = evidence.load_sealed_pytest_evidence(
        out, expected_complete=False
    )
    committed = []
    real_replace = evidence.os.replace

    def recording_replace(source_path, target_path):
        committed.append(Path(target_path).name)
        real_replace(source_path, target_path)

    monkeypatch.setattr(evidence.os, "replace", recording_replace)
    completed = evidence.complete_pytest_evidence(
        out,
        story_evidence_bytes=STORY_EVIDENCE_BYTES,
        expected_manifest_sha256=incomplete.manifest_sha256,
    )

    assert committed == ["story-evidence.json", "source-hashes.json"]
    assert completed.manifest["complete"] is True
    assert set(completed.manifest["artifacts"]) == {
        "pytest.xml",
        "pytest.log",
        "run.json",
        "story-evidence.json",
    }
    assert completed.story_evidence_bytes == STORY_EVIDENCE_BYTES
    assert completed.story_evidence_payload == {
        "cases": [
            {
                "nodeid": "qa_checks/test_stories.py::test_story_contract[S001]"
            }
        ]
    }


def test_complete_pytest_evidence_serializes_concurrent_completers(
    tmp_path: Path, monkeypatch,
) -> None:
    out, incomplete = _published_incomplete_pytest_run(tmp_path)
    stories = (
        STORY_EVIDENCE_BYTES,
        b'{"cases":[{"nodeid":"qa_checks/test_stories.py::test_story_contract[S002]"}]}\n',
    )
    start = threading.Barrier(3)
    entered_locked_load = threading.Event()
    losing_call_finished = threading.Event()
    release_winner = threading.Event()
    pause_guard = threading.Lock()
    outcome_guard = threading.Lock()
    pause_first = True
    outcomes: list[tuple[bytes, object]] = []
    real_load = evidence._load_sealed_pytest_evidence

    def pause_once_after_lock(*args, **kwargs):
        nonlocal pause_first
        should_pause = False
        with pause_guard:
            if pause_first and kwargs.get("expected_complete") is False:
                pause_first = False
                should_pause = True
        if should_pause:
            entered_locked_load.set()
            if not release_winner.wait(timeout=5):
                raise RuntimeError("timed out waiting to release completion winner")
        return real_load(*args, **kwargs)

    monkeypatch.setattr(evidence, "_load_sealed_pytest_evidence", pause_once_after_lock)

    def complete(story_bytes: bytes) -> None:
        start.wait(timeout=5)
        try:
            result: object = evidence.complete_pytest_evidence(
                out,
                story_evidence_bytes=story_bytes,
                expected_manifest_sha256=incomplete.manifest_sha256,
            )
        except Exception as exc:  # the competing completion must fail closed
            result = exc
            losing_call_finished.set()
        with outcome_guard:
            outcomes.append((story_bytes, result))

    threads = [threading.Thread(target=complete, args=(story,)) for story in stories]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    try:
        assert entered_locked_load.wait(timeout=5)
        assert losing_call_finished.wait(timeout=5)
    finally:
        release_winner.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    successes = [
        (story, result)
        for story, result in outcomes
        if isinstance(result, evidence.SealedPytestEvidence)
    ]
    failures = [result for _story, result in outcomes if isinstance(result, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], evidence.EvidenceManifestError)
    assert "completion lock already exists" in str(failures[0])

    sealed = real_load(out, expected_complete=True)
    winning_story = successes[0][0]
    assert sealed.story_evidence_bytes == winning_story
    assert sealed.manifest["artifacts"]["story-evidence.json"] == hashlib.sha256(
        winning_story
    ).hexdigest()
    assert not (out / evidence._PYTEST_COMPLETION_LOCK).exists()


def test_complete_pytest_evidence_leaves_an_existing_lock_fail_closed(
    tmp_path: Path,
) -> None:
    out, incomplete = _published_incomplete_pytest_run(tmp_path)
    lock_path = out / evidence._PYTEST_COMPLETION_LOCK
    lock_path.write_bytes(b"stale lock requiring manual audit")

    with pytest.raises(evidence.EvidenceManifestError, match="lock already exists"):
        evidence.complete_pytest_evidence(
            out,
            story_evidence_bytes=STORY_EVIDENCE_BYTES,
            expected_manifest_sha256=incomplete.manifest_sha256,
        )

    assert lock_path.read_bytes() == b"stale lock requiring manual audit"
    assert not (out / "story-evidence.json").exists()
    assert evidence.load_sealed_pytest_evidence(
        out, expected_complete=False
    ).manifest_sha256 == incomplete.manifest_sha256


def test_publish_pytest_evidence_refuses_a_preexisting_story_artifact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    source = root / "scripts" / "story_tracker.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-pytest"
    out.mkdir()
    (out / "story-evidence.json").write_bytes(STORY_EVIDENCE_BYTES)
    start = evidence.snapshot_source_hashes(root, [source])

    with pytest.raises(evidence.EvidenceManifestError, match="already has"):
        evidence.publish_pytest_evidence(
            out,
            root=root,
        source_files=lambda: [source],
            source_hashes_at_start=start,
            pytest_bytes=PYTEST_BYTES,
            log_bytes=PYTEST_LOG_BYTES,
            run_bytes=RUN_BYTES,
        )

    assert set(path.name for path in out.iterdir()) == {"story-evidence.json"}


def test_browser_loader_derives_kind(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source = root / "source.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"stable\n")
    out = root / "run-native-browser"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])
    assert evidence.publish_browser_evidence(
        out,
        root=root,
        kind="native-browser",
        source_files=lambda: [source],
        source_hashes_at_start=start,
        browser_bytes=BROWSER_BYTES,
    )

    loaded = evidence.load_sealed_browser_evidence(out)

    assert loaded.manifest["kind"] == "native-browser"


def test_publish_pytest_evidence_keeps_three_artifacts_unsealed_when_source_drifts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    source = root / "scripts" / "story_tracker.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"before\n")
    out = root / "run-pytest"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])
    source.write_bytes(b"after\n")

    assert not evidence.publish_pytest_evidence(
        out,
        root=root,
        source_files=lambda: [source],
        source_hashes_at_start=start,
        pytest_bytes=PYTEST_BYTES,
        log_bytes=PYTEST_LOG_BYTES,
        run_bytes=RUN_BYTES,
    )

    assert {path.name for path in out.iterdir()} == {
        "pytest.xml",
        "pytest.log",
        "run.json",
    }
    assert not (out / "source-hashes.json").exists()


def test_publish_pytest_evidence_keeps_source_aba_result_unsealed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    source = root / "scripts" / "story_tracker.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original\n")
    out = root / "run-pytest"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])

    _edit_and_restore_source(source)
    end = evidence.snapshot_source_hashes(root, [source])

    assert end == start
    assert end.revisions != start.revisions
    assert not evidence.publish_pytest_evidence(
        out,
        root=root,
        source_files=lambda: [source],
        source_hashes_at_start=start,
        pytest_bytes=PYTEST_BYTES,
        log_bytes=PYTEST_LOG_BYTES,
        run_bytes=RUN_BYTES,
    )
    assert {path.name for path in out.iterdir()} == {
        "pytest.xml",
        "pytest.log",
        "run.json",
    }
    assert not (out / "source-hashes.json").exists()


def test_publish_pytest_evidence_rejects_a_plain_start_hash_dict(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    source = root / "source.py"
    root.mkdir()
    source.write_bytes(b"stable\n")
    out = root / "run-pytest"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])

    with pytest.raises(evidence.EvidenceManifestError, match="intact result"):
        evidence.publish_pytest_evidence(
            out,
            root=root,
        source_files=lambda: [source],
            source_hashes_at_start=dict(start),
            pytest_bytes=PYTEST_BYTES,
            log_bytes=PYTEST_LOG_BYTES,
            run_bytes=RUN_BYTES,
        )

    assert not any(out.iterdir())


@pytest.mark.parametrize("producer_kind", ["browser", "pytest"])
@pytest.mark.parametrize(
    "mutation_kind",
    [
        "source-edit",
        "source-aba",
        "source-added",
        "artifact-tamper",
        "manifest-appears",
        "run-junction",
    ],
)
def test_publish_manifest_guard_rechecks_immediately_before_replace(
    tmp_path: Path,
    monkeypatch,
    producer_kind: str,
    mutation_kind: str,
) -> None:
    root = tmp_path / "repo"
    source = root / "source.py"
    root.mkdir()
    source.write_bytes(b"original\n")
    out = root / f"run-{producer_kind}"
    out.mkdir()
    manifest = out / "source-hashes.json"
    artifact = out / (
        "browser.json" if producer_kind == "browser" else "pytest.log"
    )
    source_files = lambda: sorted(root.glob("*.py"))
    start = evidence.snapshot_supplied_source_hashes(root, source_files)
    real_atomic_write = evidence._atomic_write
    real_is_junction = getattr(evidence.os.path, "isjunction", lambda _path: False)
    mark_run_as_junction = False
    injected = False

    monkeypatch.setattr(
        evidence.os.path,
        "isjunction",
        lambda path: (
            (mark_run_as_junction and Path(path) == out)
            or real_is_junction(path)
        ),
    )

    def mutate_publication_input() -> None:
        nonlocal mark_run_as_junction
        if mutation_kind == "source-edit":
            source.write_bytes(b"edited before publication\n")
        elif mutation_kind == "source-aba":
            _edit_and_restore_source(source)
        elif mutation_kind == "source-added":
            (root / "persistent-added.py").write_bytes(b"added\n")
        elif mutation_kind == "artifact-tamper":
            artifact.write_bytes(b"tampered before publication\n")
        elif mutation_kind == "manifest-appears":
            manifest.write_bytes(b"racing manifest\n")
        else:
            mark_run_as_junction = True

    def inject_before_guard(path, value, *, before_replace=None) -> None:
        nonlocal injected
        if Path(path) != manifest:
            real_atomic_write(path, value, before_replace=before_replace)
            return
        assert before_replace is not None

        def mutate_then_guard() -> None:
            nonlocal injected
            injected = True
            mutate_publication_input()
            before_replace()

        real_atomic_write(path, value, before_replace=mutate_then_guard)

    monkeypatch.setattr(evidence, "_atomic_write", inject_before_guard)

    if producer_kind == "browser":
        published = evidence.publish_browser_evidence(
            out,
            root=root,
            kind="browser",
            source_files=source_files,
            source_hashes_at_start=start,
            browser_bytes=BROWSER_BYTES,
        )
    else:
        published = evidence.publish_pytest_evidence(
            out,
            root=root,
            source_files=source_files,
            source_hashes_at_start=start,
            pytest_bytes=PYTEST_BYTES,
            log_bytes=PYTEST_LOG_BYTES,
            run_bytes=RUN_BYTES,
        )

    assert published is False
    assert injected
    if mutation_kind == "manifest-appears":
        assert manifest.read_bytes() == b"racing manifest\n"
    else:
        assert not manifest.exists()
    assert not list(out.glob(".source-hashes.json.*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows metadata collision regression")
@pytest.mark.parametrize("producer_kind", ["browser", "pytest"])
def test_publish_guard_rehashes_artifact_changed_during_source_scan(
    tmp_path: Path, monkeypatch, producer_kind: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "source.py"
    source.write_bytes(b"stable\n")
    out = root / f"run-{producer_kind}"
    out.mkdir()
    source_files = lambda: [source]
    # Simulate the observed Windows stat collision and independently prove
    # that the final exact-byte comparison, not metadata alone, rejects it.
    monkeypatch.setattr(evidence, "_windows_change_time_ns", lambda _path: 7)
    start = evidence.snapshot_supplied_source_hashes(root, source_files)
    real_snapshot = evidence.snapshot_supplied_source_hashes
    scans = 0
    mutated = False

    def scan_then_mutate_artifact(root_path, supplier):
        nonlocal scans, mutated
        result = real_snapshot(root_path, supplier)
        scans += 1
        # First scan is the producer's end check; second is inside the
        # manifest before_replace guard after it captured artifact bytes.
        if scans == 2:
            artifact = out / (
                "browser.json" if producer_kind == "browser" else "pytest.xml"
            )
            before = artifact.stat()
            value = artifact.read_bytes()
            replacement = bytes([value[0] ^ 1]) + value[1:]
            artifact.write_bytes(replacement)
            os.utime(
                artifact, ns=(before.st_atime_ns, before.st_mtime_ns)
            )
            mutated = True
        return result

    monkeypatch.setattr(
        evidence, "snapshot_supplied_source_hashes", scan_then_mutate_artifact
    )
    if producer_kind == "browser":
        published = evidence.publish_browser_evidence(
            out,
            root=root,
            kind="browser",
            source_files=source_files,
            source_hashes_at_start=start,
            browser_bytes=BROWSER_BYTES,
        )
    else:
        published = evidence.publish_pytest_evidence(
            out,
            root=root,
            source_files=source_files,
            source_hashes_at_start=start,
            pytest_bytes=PYTEST_BYTES,
            log_bytes=PYTEST_LOG_BYTES,
            run_bytes=RUN_BYTES,
        )

    assert mutated
    assert published is False
    assert not (out / "source-hashes.json").exists()


@pytest.mark.parametrize("producer_kind", ["browser", "pytest"])
def test_publish_manifest_guard_rechecks_artifacts_after_the_source_scan(
    tmp_path: Path, producer_kind: str,
) -> None:
    root = tmp_path / "repo"
    source = root / "source.py"
    root.mkdir()
    source.write_bytes(b"stable\n")
    out = root / f"run-{producer_kind}"
    out.mkdir()
    artifact = out / (
        "browser.json" if producer_kind == "browser" else "pytest.log"
    )
    start = evidence.snapshot_source_hashes(root, [source])
    supplier_calls = 0

    def source_files() -> list[Path]:
        nonlocal supplier_calls
        supplier_calls += 1
        # Calls 1-2 are the publisher's first check.  The guard reads the
        # artifacts and only then makes call 3 to begin its source scan.
        if supplier_calls == 3:
            artifact.write_bytes(b"persistent tamper during source scan\n")
        return [source]

    if producer_kind == "browser":
        published = evidence.publish_browser_evidence(
            out,
            root=root,
            kind="browser",
            source_files=source_files,
            source_hashes_at_start=start,
            browser_bytes=BROWSER_BYTES,
        )
    else:
        published = evidence.publish_pytest_evidence(
            out,
            root=root,
            source_files=source_files,
            source_hashes_at_start=start,
            pytest_bytes=PYTEST_BYTES,
            log_bytes=PYTEST_LOG_BYTES,
            run_bytes=RUN_BYTES,
        )

    assert published is False
    assert supplier_calls == 4
    assert artifact.read_bytes() == b"persistent tamper during source scan\n"
    assert not (out / "source-hashes.json").exists()


@pytest.mark.parametrize("producer_kind", ["browser", "pytest"])
def test_publish_evidence_requires_a_callable_source_supplier(
    tmp_path: Path, producer_kind: str,
) -> None:
    root = tmp_path / "repo"
    source = root / "source.py"
    root.mkdir()
    source.write_bytes(b"stable\n")
    out = root / f"run-{producer_kind}"
    out.mkdir()
    start = evidence.snapshot_source_hashes(root, [source])

    with pytest.raises(evidence.EvidenceManifestError, match="callable supplier"):
        if producer_kind == "browser":
            evidence.publish_browser_evidence(
                out,
                root=root,
                kind="browser",
                source_files=[source],
                source_hashes_at_start=start,
                browser_bytes=BROWSER_BYTES,
            )
        else:
            evidence.publish_pytest_evidence(
                out,
                root=root,
                source_files=[source],
                source_hashes_at_start=start,
                pytest_bytes=PYTEST_BYTES,
                log_bytes=PYTEST_LOG_BYTES,
                run_bytes=RUN_BYTES,
            )

    assert not any(out.iterdir())


def test_complete_pytest_evidence_wrong_expected_manifest_leaves_no_story(
    tmp_path: Path,
) -> None:
    out, _ = _published_incomplete_pytest_run(tmp_path)

    with pytest.raises(
        evidence.EvidenceManifestError, match="manifest changed before completion"
    ):
        evidence.complete_pytest_evidence(
            out,
            story_evidence_bytes=STORY_EVIDENCE_BYTES,
            expected_manifest_sha256="0" * 64,
        )

    assert not (out / "story-evidence.json").exists()
    assert evidence.load_sealed_pytest_evidence(
        out, expected_complete=False
    ).manifest["complete"] is False


def test_complete_pytest_evidence_refuses_to_reseal_a_complete_run(
    tmp_path: Path,
) -> None:
    out, incomplete = _published_incomplete_pytest_run(tmp_path)
    completed = evidence.complete_pytest_evidence(
        out,
        story_evidence_bytes=STORY_EVIDENCE_BYTES,
        expected_manifest_sha256=incomplete.manifest_sha256,
    )

    with pytest.raises(evidence.EvidenceManifestError, match="already complete"):
        evidence.complete_pytest_evidence(
            out,
            story_evidence_bytes=STORY_EVIDENCE_BYTES,
            expected_manifest_sha256=completed.manifest_sha256,
        )

    loaded = evidence.load_sealed_pytest_evidence(out, expected_complete=True)
    assert loaded.manifest_sha256 == completed.manifest_sha256
    assert loaded.story_evidence_bytes == STORY_EVIDENCE_BYTES


@pytest.mark.parametrize("artifact", ["pytest.xml", "pytest.log", "run.json"])
def test_complete_pytest_evidence_rejects_tampered_raw_artifacts_before_story_write(
    tmp_path: Path, artifact: str,
) -> None:
    out, incomplete = _published_incomplete_pytest_run(tmp_path)
    (out / artifact).write_bytes((out / artifact).read_bytes() + b"tampered")

    with pytest.raises(
        evidence.EvidenceManifestError, match=rf"{re.escape(artifact)} digest"
    ):
        evidence.complete_pytest_evidence(
            out,
            story_evidence_bytes=STORY_EVIDENCE_BYTES,
            expected_manifest_sha256=incomplete.manifest_sha256,
        )

    assert not (out / "story-evidence.json").exists()
    manifest = json.loads((out / "source-hashes.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is False


def test_complete_pytest_evidence_rechecks_raw_artifacts_before_true_manifest(
    tmp_path: Path, monkeypatch,
) -> None:
    out, incomplete = _published_incomplete_pytest_run(tmp_path)
    real_replace = evidence.os.replace

    def tamper_after_story_commit(source_path, target_path):
        real_replace(source_path, target_path)
        if Path(target_path).name == "story-evidence.json":
            (out / "pytest.xml").write_bytes(PYTEST_BYTES + b"tampered")

    monkeypatch.setattr(evidence.os, "replace", tamper_after_story_commit)

    with pytest.raises(evidence.EvidenceManifestError, match="pytest.xml digest"):
        evidence.complete_pytest_evidence(
            out,
            story_evidence_bytes=STORY_EVIDENCE_BYTES,
            expected_manifest_sha256=incomplete.manifest_sha256,
        )

    manifest = json.loads((out / "source-hashes.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is False
    assert (out / "story-evidence.json").read_bytes() == STORY_EVIDENCE_BYTES


def test_complete_pytest_evidence_rechecks_manifest_before_story_commit(
    tmp_path: Path, monkeypatch,
) -> None:
    out, incomplete = _published_incomplete_pytest_run(tmp_path)
    story_path = out / "story-evidence.json"
    manifest_path = out / "source-hashes.json"
    original_manifest = manifest_path.read_bytes()
    real_exists = Path.exists
    changed = False

    def change_manifest_at_story_cas(path: Path) -> bool:
        nonlocal changed
        if path == story_path and not changed:
            changed = True
            manifest_path.write_bytes(original_manifest + b"\n")
        return real_exists(path)

    monkeypatch.setattr(Path, "exists", change_manifest_at_story_cas)

    with pytest.raises(
        evidence.EvidenceManifestError,
        match="changed (?:before story|during validation)",
    ):
        evidence.complete_pytest_evidence(
            out,
            story_evidence_bytes=STORY_EVIDENCE_BYTES,
            expected_manifest_sha256=incomplete.manifest_sha256,
        )

    assert changed
    assert not story_path.exists()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("old-flat", "exactly five root keys"),
        ("wrong-kind", "kind must be pytest"),
        ("non-boolean-complete", "complete must be a boolean"),
    ],
)
def test_load_sealed_pytest_evidence_rejects_noncanonical_manifests(
    tmp_path: Path, case: str, message: str,
) -> None:
    out, _ = _published_incomplete_pytest_run(tmp_path)
    manifest_path = out / "source-hashes.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if case == "old-flat":
        manifest = dict(manifest["source_hashes"])
    elif case == "wrong-kind":
        manifest["kind"] = "browser"
    elif case == "non-boolean-complete":
        manifest["complete"] = 0
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(evidence.EvidenceManifestError, match=message):
        evidence.load_sealed_pytest_evidence(out, expected_complete=False)


@pytest.mark.parametrize("complete", [False, True], ids=["incomplete", "complete"])
def test_load_sealed_pytest_evidence_requires_the_exact_artifact_set(
    tmp_path: Path, complete: bool,
) -> None:
    out, incomplete = _published_incomplete_pytest_run(tmp_path)
    if complete:
        evidence.complete_pytest_evidence(
            out,
            story_evidence_bytes=STORY_EVIDENCE_BYTES,
            expected_manifest_sha256=incomplete.manifest_sha256,
        )
    manifest_path = out / "source-hashes.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["extra.json"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    count = "four" if complete else "three"
    with pytest.raises(
        evidence.EvidenceManifestError,
        match=rf"exactly the {count} expected files",
    ):
        evidence.load_sealed_pytest_evidence(
            out, expected_complete=complete
        )


def test_load_sealed_pytest_evidence_requires_the_expected_completion_state(
    tmp_path: Path,
) -> None:
    out, incomplete = _published_incomplete_pytest_run(tmp_path)

    with pytest.raises(evidence.EvidenceManifestError, match="not complete"):
        evidence.load_sealed_pytest_evidence(out, expected_complete=True)

    evidence.complete_pytest_evidence(
        out,
        story_evidence_bytes=STORY_EVIDENCE_BYTES,
        expected_manifest_sha256=incomplete.manifest_sha256,
    )
    with pytest.raises(evidence.EvidenceManifestError, match="already complete"):
        evidence.load_sealed_pytest_evidence(out, expected_complete=False)


@pytest.mark.parametrize(
    ("run_bytes", "message"),
    [
        (b'{"exit_code":0,"exit_code":1}\n', "duplicate JSON key"),
        (b'{"exit_code":NaN}\n', "invalid JSON constant"),
    ],
)
def test_load_sealed_pytest_evidence_strictly_decodes_run_json(
    tmp_path: Path, run_bytes: bytes, message: str,
) -> None:
    out, _ = _published_incomplete_pytest_run(tmp_path)
    run_path = out / "run.json"
    run_path.write_bytes(run_bytes)
    manifest_path = out / "source-hashes.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["run.json"] = hashlib.sha256(run_bytes).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(evidence.EvidenceManifestError, match=message):
        evidence.load_sealed_pytest_evidence(out, expected_complete=False)


@pytest.mark.parametrize(
    ("story_bytes", "message"),
    [
        (b'{"cases":[],"cases":[]}\n', "duplicate JSON key"),
        (b'{"duration":NaN}\n', "invalid JSON constant"),
    ],
)
def test_complete_pytest_evidence_strictly_decodes_story_json_before_writing(
    tmp_path: Path, story_bytes: bytes, message: str,
) -> None:
    out, incomplete = _published_incomplete_pytest_run(tmp_path)

    with pytest.raises(evidence.EvidenceManifestError, match=message):
        evidence.complete_pytest_evidence(
            out,
            story_evidence_bytes=story_bytes,
            expected_manifest_sha256=incomplete.manifest_sha256,
        )

    assert not (out / "story-evidence.json").exists()
    assert evidence.load_sealed_pytest_evidence(
        out, expected_complete=False
    ).manifest["complete"] is False


def test_complete_pytest_evidence_rechecks_story_before_true_manifest(
    tmp_path: Path, monkeypatch,
) -> None:
    out, incomplete = _published_incomplete_pytest_run(tmp_path)
    story_path = out / "story-evidence.json"
    real_replace = evidence.os.replace

    def tamper_after_story_commit(source_path, target_path):
        real_replace(source_path, target_path)
        if Path(target_path) == story_path:
            story_path.write_bytes(STORY_EVIDENCE_BYTES + b"tampered")

    monkeypatch.setattr(evidence.os, "replace", tamper_after_story_commit)

    with pytest.raises(
        evidence.EvidenceManifestError, match="story-evidence.json changed"
    ):
        evidence.complete_pytest_evidence(
            out,
            story_evidence_bytes=STORY_EVIDENCE_BYTES,
            expected_manifest_sha256=incomplete.manifest_sha256,
        )

    manifest = json.loads((out / "source-hashes.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is False
