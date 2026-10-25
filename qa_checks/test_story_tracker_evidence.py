"""Evidence bookkeeping must fail closed when its source snapshot is stale."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import scripts.qa_evidence_manifest as evidence_manifest
import scripts.story_tracker as tracker


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _catalog_bytes(story: str, selector: str, *, area: str = "test") -> bytes:
    values = {header: "reviewed" for header in tracker.CATALOG_HEADERS}
    values.update(
        {
            "ID": story,
            "領域": area,
            "自動テスト候補": selector,
        }
    )
    return (
        "\t".join(tracker.CATALOG_HEADERS)
        + "\n"
        + "\t".join(values[header] for header in tracker.CATALOG_HEADERS)
        + "\n"
    ).encode("utf-8")


def _write_evidence_envelope(
    run: Path,
    *,
    kind: str,
    complete: bool,
    source_hashes: dict[str, str],
    artifacts: dict[str, bytes],
) -> None:
    for name, payload in artifacts.items():
        (run / name).write_bytes(payload)
    manifest = {
        "schema_version": 1,
        "kind": kind,
        "complete": complete,
        "source_hashes": source_hashes,
        "artifacts": {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in artifacts.items()
        },
    }
    (run / "source-hashes.json").write_bytes(_json_bytes(manifest))


def _write_browser_envelope(
    run: Path,
    *,
    payload: dict,
    source_hashes: dict[str, str],
    kind: str = "browser",
    complete: bool = True,
) -> None:
    _write_evidence_envelope(
        run,
        kind=kind,
        complete=complete,
        source_hashes=source_hashes,
        artifacts={"browser.json": _json_bytes(payload)},
    )


def _write_pytest_envelope(
    run: Path,
    *,
    xml: bytes,
    source_hashes: dict[str, str],
    complete: bool,
    meta: dict | None = None,
    story_evidence: dict | None = None,
    kind: str = "pytest",
) -> None:
    try:
        collected_nodeids = [case["nodeid"] for case in tracker.junit_cases(xml)]
    except Exception:
        collected_nodeids = []
    default_command = [
        "python",
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
        str(tracker.ROOT / "pyproject.toml"),
        "--junitxml=pytest.xml",
        "--basetemp=tmp",
    ]
    default_collection_command = [
        *default_command[:-2],
        "-p",
        "scripts.story_tracker",
        "--collect-only",
    ]
    default_meta = {
        "schema_version": 2,
        "command": default_command,
        "collection_command": default_collection_command,
        "collection_exit_code": 0,
        "pytest_args": [],
        "collected_nodeids": collected_nodeids,
        "collection_complete": True,
        "exit_code": 0,
        "finished_at": "2026-09-08T12:00:00+09:00",
        "python": "test",
    }
    if meta is not None:
        default_meta.update(meta)
        if "pytest_args" in meta and "command" not in meta:
            default_meta["command"] = [
                *default_command,
                *default_meta["pytest_args"],
            ]
            default_meta["collection_command"] = [
                *default_collection_command,
                *default_meta["pytest_args"],
            ]
    artifacts = {
        "pytest.xml": xml,
        "pytest.log": b"pytest output\n",
        "run.json": _json_bytes(default_meta),
    }
    if complete:
        artifacts["story-evidence.json"] = _json_bytes(
            story_evidence if story_evidence is not None else {}
        )
    _write_evidence_envelope(
        run,
        kind=kind,
        complete=complete,
        source_hashes=source_hashes,
        artifacts=artifacts,
    )


def _seal_existing_pytest_run(
    run: Path,
    *,
    source_hashes: dict[str, str] | None = None,
) -> None:
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    meta.pop("command", None)
    meta.setdefault("exit_code", 0)
    meta.setdefault("python", "test")
    _write_pytest_envelope(
        run,
        xml=(run / "pytest.xml").read_bytes(),
        source_hashes=source_hashes or {},
        complete=False,
        meta=meta,
    )


def _seal_existing_browser_run(
    run: Path,
    *,
    source_hashes: dict[str, str] | None = None,
    kind: str = "browser",
) -> None:
    _write_browser_envelope(
        run,
        payload=json.loads((run / "browser.json").read_text(encoding="utf-8")),
        source_hashes=source_hashes or {},
        kind=kind,
    )


def _verified_workbook_state_for_test() -> tracker.VerifiedWorkbookState:
    workbook_bytes, fingerprint = tracker._stable_file_snapshot(tracker.BOOK)
    return tracker.VerifiedWorkbookState(
        file_revision=fingerprint[0],
        parent_identity=fingerprint[1][:2],
        sha256=hashlib.sha256(workbook_bytes).hexdigest(),
        size=len(workbook_bytes),
        proof_cache=tracker.AuditProofCache(),
    )


def test_pytest_snapshot_includes_runtime_evidence_producers() -> None:
    relative = {
        path.relative_to(tracker.ROOT).as_posix()
        for path in tracker.pytest_source_files()
    }

    assert {
        "README.md",
        "config.yaml",
        "docs/qa/stories.tsv",
        "pyproject.toml",
        "requirements.txt",
        "setup.ps1",
        "start.bat",
        "scripts/acceptance.py",
        "scripts/bench.py",
        "scripts/field-verification.sh",
        "scripts/field_story_evidence.py",
        "scripts/perf01_evidence.py",
        "scripts/performance_evidence.py",
        "scripts/quality_evidence.py",
        "scripts/replay_client.py",
        "scripts/run.ps1",
        "scripts/scale_bench.py",
        "scripts/text_metrics.py",
        "scripts/validate_field_performance.py",
        "tests/fixtures/ja/03.wav",
        "tests/fixtures/ja_ext/noise-snr10-rate-fast-b.wav",
    } <= relative


def test_pytest_snapshot_excludes_hidden_test_artifacts(tmp_path, monkeypatch) -> None:
    qa = tmp_path / "qa_checks"
    qa.mkdir()
    source = qa / "test_real_check.py"
    source.write_text("# reviewed QA source\n", encoding="utf-8")
    artifact = qa / ".pytest-perf-root" / "copied" / "test_copy.py"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# generated during a test\n", encoding="utf-8")
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "CATALOG", tmp_path / "docs/qa/stories.tsv")

    relative = {
        path.relative_to(tmp_path).as_posix()
        for path in tracker.pytest_source_files()
        if path.is_file()
    }

    assert "qa_checks/test_real_check.py" in relative
    assert "qa_checks/.pytest-perf-root/copied/test_copy.py" not in relative


def test_consumer_source_snapshot_rejects_a_file_added_after_hash_traversal(
    tmp_path, monkeypatch
) -> None:
    source_dir = tmp_path / "tests"
    source_dir.mkdir()
    source = source_dir / "test_existing.py"
    source.write_text("existing\n", encoding="utf-8")
    added = source_dir / "test_added.py"
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    supplier = lambda: sorted(source_dir.glob("*.py"))
    real_snapshot = evidence_manifest.snapshot_source_hashes

    def snapshot_then_add(root, files):
        result = real_snapshot(root, files)
        added.write_text("added after traversal\n", encoding="utf-8")
        return result

    monkeypatch.setattr(
        evidence_manifest, "snapshot_source_hashes", snapshot_then_add
    )

    with pytest.raises(tracker.EvidenceManifestError, match="source set changed"):
        tracker._stable_supplied_source_snapshot(supplier)


def test_import_code_binding_rejects_a_stable_post_import_edit(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "scripts" / "audit_module.py"
    source.parent.mkdir()
    source.write_bytes(b"imported = True\n")
    imported = tracker._capture_code_binding(source, root=tmp_path)
    monkeypatch.setattr(tracker, "_IMPORT_ROOT", tmp_path)
    monkeypatch.setattr(
        tracker, "_IMPORT_CODE_BINDINGS", {source: imported}
    )
    source.write_bytes(b"imported = False\n")

    assert not tracker._import_code_is_current()


def test_tracker_bound_all_local_auditors_before_import() -> None:
    assert set(tracker._EARLY_IMPORT_CODE_BINDINGS) == {
        tracker._STORY_TRACKER_PATH,
        tracker._module_source_path(tracker.qa_evidence_manifest_module),
        tracker._module_source_path(tracker.field_story_evidence_module),
        tracker._module_source_path(tracker.performance_evidence_module),
    }
    assert tracker._IMPORT_CODE_BINDINGS == tracker._EARLY_IMPORT_CODE_BINDINGS
    assert Path(tracker._IMPORT_PYCACHE.name).is_dir()
    assert list(Path(tracker._IMPORT_PYCACHE.name).iterdir()) == []
    assert tracker.sys.dont_write_bytecode is True
    source = tracker._STORY_TRACKER_PATH.read_text(encoding="utf-8")
    assert source.index("sys.pycache_prefix = _IMPORT_PYCACHE.name") < source.index(
        "import scripts.performance_evidence as performance_evidence_module"
    )


def test_tracker_import_guard_accepts_an_equivalent_fresh_private_cache(
    tmp_path, monkeypatch
) -> None:
    replacement = tmp_path / "another-private-import-cache"
    replacement.mkdir()
    monkeypatch.setattr(tracker.sys, "pycache_prefix", str(replacement))
    monkeypatch.setattr(tracker.sys, "dont_write_bytecode", True)

    assert tracker._import_code_is_current()


@pytest.mark.parametrize(
    "failure", ["nonempty", "writes-enabled", "missing", "relative"]
)
def test_tracker_import_guard_rejects_unsafe_bytecode_cache_isolation(
    failure, tmp_path, monkeypatch
) -> None:
    replacement = tmp_path / "unsafe-import-cache"
    replacement.mkdir()
    prefix = str(replacement)
    monkeypatch.setattr(tracker.sys, "dont_write_bytecode", True)
    if failure == "nonempty":
        (replacement / "stale.pyc").write_bytes(b"stale")
    elif failure == "writes-enabled":
        monkeypatch.setattr(tracker.sys, "dont_write_bytecode", False)
    elif failure == "missing":
        replacement.rmdir()
    else:
        monkeypatch.chdir(tmp_path)
        prefix = replacement.name
    monkeypatch.setattr(tracker.sys, "pycache_prefix", prefix)

    assert not tracker._import_code_is_current()


def test_tracker_import_guard_rejects_a_linked_cache_ancestor(
    tmp_path, monkeypatch
) -> None:
    real_parent = tmp_path / "real-parent"
    cache = real_parent / "cache"
    cache.mkdir(parents=True)
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")
    monkeypatch.setattr(tracker.sys, "pycache_prefix", str(linked_parent / "cache"))
    monkeypatch.setattr(tracker.sys, "dont_write_bytecode", True)

    assert not tracker._import_code_is_current()


def test_tracker_import_guard_rejects_a_junction_cache_ancestor(
    tmp_path, monkeypatch
) -> None:
    junction_parent = tmp_path / "junction-parent"
    cache = junction_parent / "cache"
    cache.mkdir(parents=True)
    real_isjunction = getattr(tracker.os.path, "isjunction", lambda _path: False)
    monkeypatch.setattr(
        tracker.os.path,
        "isjunction",
        lambda path: Path(path) == junction_parent or real_isjunction(path),
        raising=False,
    )
    monkeypatch.setattr(tracker.sys, "pycache_prefix", str(cache))
    monkeypatch.setattr(tracker.sys, "dont_write_bytecode", True)

    assert not tracker._import_code_is_current()


def test_tracker_import_guard_rechecks_cache_after_source_validation(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "scripts" / "audit_module.py"
    source.parent.mkdir()
    source.write_bytes(b"imported = True\n")
    imported = tracker._capture_code_binding(source, root=tmp_path)
    cache = tmp_path / "private-import-cache"
    cache.mkdir()
    monkeypatch.setattr(tracker, "_IMPORT_ROOT", tmp_path)
    monkeypatch.setattr(tracker, "_IMPORT_CODE_BINDINGS", {source: imported})
    monkeypatch.setattr(tracker.sys, "pycache_prefix", str(cache))
    monkeypatch.setattr(tracker.sys, "dont_write_bytecode", True)
    real_capture = tracker._capture_code_binding
    injected = False

    def capture_then_fill_cache(path, *, root):
        nonlocal injected
        binding = real_capture(path, root=root)
        if not injected:
            injected = True
            (cache / "stale.pyc").write_bytes(b"stale")
        return binding

    monkeypatch.setattr(tracker, "_capture_code_binding", capture_then_fill_cache)

    assert not tracker._import_code_is_current()


def test_code_binding_final_fence_rehashes_when_revision_appears_unchanged(
    tmp_path,
) -> None:
    source = tmp_path / "scripts" / "audit_module.py"
    source.parent.mkdir()
    source.write_bytes(b"value = A\n")
    imported = tracker._capture_code_binding(source, root=tmp_path)
    source.write_bytes(b"value = B\n")
    replacement = tracker._capture_code_binding(source, root=tmp_path)
    # Model a filesystem whose exposed revision fields collide across a
    # same-size overwrite.  The expected digest must still reject B.
    colliding_revision = tracker.CodeBinding(
        path=source,
        revision=replacement.revision,
        ancestor_identities=replacement.ancestor_identities,
        sha256=imported.sha256,
    )

    with pytest.raises(OSError, match="bound repository file changed"):
        tracker._recheck_code_binding_revision(colliding_revision)


@pytest.mark.skipif(os.name != "nt", reason="Windows ChangeTime regression")
def test_code_binding_rejects_in_place_aba_during_read(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "scripts" / "audit_module.py"
    source.parent.mkdir()
    source.write_bytes(b"value = A\n")
    real_read = Path.read_bytes
    injected = False

    def read_then_restore_metadata(path: Path) -> bytes:
        nonlocal injected
        value = real_read(path)
        if path == source and not injected:
            injected = True
            before = source.stat()
            changed = bytes([value[0] ^ 1]) + value[1:]
            with source.open("r+b") as handle:
                handle.write(changed)
                handle.flush()
                os.fsync(handle.fileno())
                handle.seek(0)
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.utime(
                source, ns=(before.st_atime_ns, before.st_mtime_ns)
            )
        return value

    monkeypatch.setattr(Path, "read_bytes", read_then_restore_metadata)

    with pytest.raises(OSError, match="changed while read"):
        tracker._read_bound_repository_file(source, root=tmp_path)

    assert injected


def test_source_final_fence_rehashes_when_revision_appears_unchanged(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "server" / "main.py"
    source.parent.mkdir()
    source.write_bytes(b"value = A\n")
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    original = tracker._stable_supplied_source_snapshot(lambda: [source])
    source.write_bytes(b"value = B\n")
    replacement = tracker._stable_supplied_source_snapshot(lambda: [source])
    colliding_revision = tracker.SourceHashSnapshot(
        dict(original), dict(replacement.revisions)
    )

    assert not tracker._source_snapshot_revisions_are_current(
        colliding_revision, lambda: [source]
    )


def test_global_source_witness_fence_rechecks_early_sources_after_late_proofs(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "server" / "early.py"
    source.parent.mkdir()
    source.write_bytes(b"original\n")
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: [source])
    cache = tracker.AuditProofCache()
    cache.source_snapshots[("pytest", False)] = (
        tracker._stable_supplied_source_snapshot(tracker.pytest_source_files)
    )

    def late_field_recheck(_cache):
        source.write_bytes(b"changed while a later proof was checked\n")
        return True

    monkeypatch.setattr(
        tracker, "_field_proof_cache_is_current", late_field_recheck
    )

    assert not tracker._audit_proof_cache_is_current(cache)


def test_global_file_witness_fence_rechecks_early_artifacts_after_later_reads(
    tmp_path, monkeypatch
) -> None:
    first = tmp_path / "runs" / "proof" / "browser.json"
    second = first.with_name("source-hashes.json")
    first.parent.mkdir(parents=True)
    first.write_bytes(b"first sealed artifact\n")
    second.write_bytes(b"later sealed manifest\n")
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    cache = tracker.AuditProofCache()
    cache.file_fingerprints[first] = hashlib.sha256(
        first.read_bytes()
    ).hexdigest()
    cache.file_fingerprints[second] = hashlib.sha256(
        second.read_bytes()
    ).hexdigest()
    real_read = tracker._read_bound_repository_file

    def read_then_change_earlier(path, *, root):
        result = real_read(path, root=root)
        if Path(path) == second:
            first.write_bytes(b"changed during later artifact validation\n")
        return result

    monkeypatch.setattr(
        tracker, "_read_bound_repository_file", read_then_change_earlier
    )

    assert not tracker._audit_proof_cache_is_current(cache)


def test_collect_run_publishes_an_incomplete_pytest_seal(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    source = root / "server" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("tested source\n", encoding="utf-8")
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: [source])
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k one_leaf")
    monkeypatch.setenv("PYTEST_PLUGINS", "unreviewed.selector_plugin")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(tmp_path / "stale-pycache"))
    pycache_prefixes = []

    def fake_run(command, *, stdout, env, **_kwargs):
        assert "PYTEST_ADDOPTS" not in env
        assert "PYTEST_PLUGINS" not in env
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        pycache = Path(env["PYTHONPYCACHEPREFIX"])
        assert pycache.is_dir()
        assert list(pycache.iterdir()) == []
        pycache_prefixes.append(pycache)
        assert ["-o", "addopts="] == command[
            command.index("-o") : command.index("-o") + 2
        ]
        if "--collect-only" in command:
            Path(env[tracker.PYTEST_COLLECTION_ENV]).write_text(
                json.dumps(
                    {"schema_version": 1, "complete": True, "nodeids": []}
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0)
        assert tracker.PYTEST_COLLECTION_ENV not in env
        xml_arg = next(value for value in command if value.startswith("--junitxml="))
        Path(xml_arg.split("=", 1)[1]).write_bytes(b"<testsuite/>")
        stdout.write("pytest output\n")
        stdout.flush()
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(tracker.subprocess, "run", fake_run)

    run = tracker.collect_run(["tests"])

    sealed = tracker.load_sealed_pytest_evidence(run, expected_complete=False)
    assert sealed.manifest["complete"] is False
    assert sealed.manifest["source_hashes"] == tracker.hash_files([source])
    assert sealed.pytest_bytes == b"<testsuite/>"
    assert sealed.log_bytes.decode("utf-8").splitlines()[-1] == "pytest output"
    assert sealed.run_payload["exit_code"] == 7
    assert sealed.run_payload["pytest_args"] == ["tests"]
    assert sealed.run_payload["collected_nodeids"] == []
    assert sealed.run_payload["collection_complete"] is True
    assert sealed.run_payload["collection_exit_code"] == 0
    assert "--collect-only" in sealed.run_payload["collection_command"]
    assert not (run / "story-evidence.json").exists()
    assert len(pycache_prefixes) == 2
    assert pycache_prefixes[0] != pycache_prefixes[1]
    assert {path.name for path in pycache_prefixes} == {
        "collection-pycache",
        "execution-pycache",
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows stale timestamp pyc regression")
def test_fresh_pycache_prefix_ignores_timestamp_size_colliding_worktree_bytecode(
    tmp_path,
) -> None:
    source = tmp_path / "pyc_probe.py"
    source.write_text("VALUE = 'A'\n", encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    command = [
        sys.executable,
        "-c",
        "import pyc_probe; print(pyc_probe.VALUE)",
    ]
    initial = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert initial.stdout.strip() == "A"
    original = source.stat()
    source.write_text("VALUE = 'B'\n", encoding="utf-8")
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))

    stale = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert stale.stdout.strip() == "A"

    fresh_prefix = tmp_path / "fresh-pycache"
    fresh_prefix.mkdir()
    isolated_environment = {
        **environment,
        "PYTHONPYCACHEPREFIX": str(fresh_prefix),
    }
    isolated = subprocess.run(
        command,
        cwd=tmp_path,
        env=isolated_environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert isolated.stdout.strip() == "B"


@pytest.mark.parametrize(
    "argument",
    ["-k=one_leaf", "--deselect=tests/test_all.py::test_other", "-x", "--maxfail=1"],
)
def test_collect_run_rejects_selection_and_early_stop_arguments_before_running(
    argument, tmp_path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(
        tracker.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unsafe args must fail before pytest"),
    )

    with pytest.raises(SystemExit, match="unsafe pytest arguments"):
        tracker.collect_run([argument])

    assert list((qa / "runs").iterdir()) == []


def test_collect_run_rejects_a_linked_runs_parent_before_external_write(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    qa.mkdir(parents=True)
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    runs = qa / "runs"
    try:
        runs.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(
        tracker,
        "pytest_source_files",
        lambda: pytest.fail("source collection must not start"),
    )

    with pytest.raises(SystemExit, match="output parent is unsafe"):
        tracker.collect_run([])

    assert list(outside.iterdir()) == []


def test_check_rejects_a_linked_runs_parent_before_workbook_access(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    qa.mkdir(parents=True)
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    try:
        (qa / "runs").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: pytest.fail("linked runs ancestry must fail before workbook access"),
    )

    with pytest.raises(SystemExit, match="output parent is unsafe"):
        tracker.check()

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("change_kind", ["content", "added-file"])
def test_collect_run_does_not_seal_when_source_changes_during_pytest(
    change_kind, tmp_path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    source = root / "server" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    def source_files():
        return sorted(source.parent.glob("*.py"))

    monkeypatch.setattr(tracker, "pytest_source_files", source_files)

    def fake_run(command, *, stdout, env, **_kwargs):
        if "--collect-only" in command:
            Path(env[tracker.PYTEST_COLLECTION_ENV]).write_text(
                json.dumps(
                    {"schema_version": 1, "complete": True, "nodeids": []}
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0)
        assert tracker.PYTEST_COLLECTION_ENV not in env
        xml_arg = next(value for value in command if value.startswith("--junitxml="))
        Path(xml_arg.split("=", 1)[1]).write_bytes(b"<testsuite/>")
        stdout.write("pytest output\n")
        stdout.flush()
        if change_kind == "content":
            source.write_text("after\n", encoding="utf-8")
        else:
            (source.parent / "added.py").write_text("added\n", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(tracker.subprocess, "run", fake_run)

    run = tracker.collect_run([])

    assert not (run / "source-hashes.json").exists()
    assert {"pytest.xml", "pytest.log", "run.json"} <= {
        path.name for path in run.iterdir()
    }


def test_main_no_sync_leaves_the_pytest_manifest_incomplete(
    tmp_path, monkeypatch
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _write_pytest_envelope(
        run,
        xml=b"<testsuite/>",
        source_hashes={},
        complete=False,
        meta={
            "exit_code": 0,
            "finished_at": "2026-09-08T12:00:00+09:00",
            "python": "test",
        },
    )
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook")
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "collect_run", lambda _args: run)
    monkeypatch.setattr(
        tracker,
        "sync",
        lambda _run: pytest.fail("--no-sync must not invoke workbook sync"),
    )
    monkeypatch.setattr(
        tracker.sys, "argv", ["story_tracker.py", "run", "--no-sync"]
    )

    with pytest.raises(SystemExit) as exit_info:
        tracker.main()

    assert exit_info.value.code == 0
    assert tracker.load_sealed_pytest_evidence(
        run, expected_complete=False
    ).manifest["complete"] is False


def test_main_no_sync_returns_two_when_the_producer_did_not_publish_a_seal(
    tmp_path, monkeypatch
) -> None:
    run = tmp_path / "unsealed-run"
    run.mkdir()
    (run / "run.json").write_text(
        json.dumps({"exit_code": 0}), encoding="utf-8"
    )
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook")
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "collect_run", lambda _args: run)
    monkeypatch.setattr(
        tracker.sys, "argv", ["story_tracker.py", "run", "--no-sync"]
    )

    with pytest.raises(SystemExit) as exit_info:
        tracker.main()

    assert exit_info.value.code == 2


def test_main_uses_sealed_run_bytes_if_run_json_is_tampered_after_sync(
    tmp_path, monkeypatch
) -> None:
    run = tmp_path / "sealed-run"
    run.mkdir()
    _write_pytest_envelope(
        run,
        xml=b"<testsuite/>",
        source_hashes={},
        complete=True,
    )
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook")
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "collect_run", lambda _args: run)

    def tamper(_run):
        (run / "run.json").write_bytes((run / "run.json").read_bytes() + b" ")

    monkeypatch.setattr(tracker, "sync", tamper)
    monkeypatch.setattr(tracker.sys, "argv", ["story_tracker.py", "run"])

    with pytest.raises(SystemExit) as exit_info:
        tracker.main()

    assert exit_info.value.code == 2


def test_browser_snapshot_includes_manifest_producer() -> None:
    relative = {
        path.relative_to(tracker.ROOT).as_posix()
        for path in tracker.browser_source_files(native=False)
    }

    assert "scripts/qa_evidence_manifest.py" in relative


def test_browser_source_sets_are_file_only_exact_and_exclude_scratch(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    files = {
        "web/student.js": b"student",
        "web/vendor/qrcode.min.js": b"qr",
        "web/.scratch/generated.js": b"scratch",
        "server/pipeline.py": b"pipeline",
        "server/readme.txt": b"not native runtime python",
        "server/__pycache__/generated.py": b"scratch",
        "scripts/qa_browser.py": b"browser",
        "scripts/qa_native_browser.py": b"native",
        "scripts/qa_evidence_manifest.py": b"manifest",
        "scripts/story_tracker.py": b"tracker",
        "tests/conftest.py": b"fixtures",
        "docs/qa/stories.tsv": b"stories",
    }
    for relative, value in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "CATALOG", root / "docs/qa/stories.tsv")

    controlled = {
        path.relative_to(root).as_posix()
        for path in tracker.browser_source_files(native=False)
    }
    native = {
        path.relative_to(root).as_posix()
        for path in tracker.browser_source_files(native=True)
    }

    assert controlled == {
        "web/student.js",
        "web/vendor/qrcode.min.js",
        "scripts/qa_browser.py",
        "scripts/qa_evidence_manifest.py",
        "scripts/story_tracker.py",
        "docs/qa/stories.tsv",
    }
    assert native == controlled | {
        "server/pipeline.py",
        "scripts/qa_native_browser.py",
        "tests/conftest.py",
    }


def test_reviewed_tree_surfaces_a_link_like_directory_for_secure_rejection(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "server"
    linked_tree = root / "linked"
    linked_tree.mkdir(parents=True)
    real_is_junction = getattr(tracker.os.path, "isjunction", lambda _path: False)
    monkeypatch.setattr(
        tracker.os.path,
        "isjunction",
        lambda path: Path(path) == linked_tree or real_is_junction(path),
        raising=False,
    )

    assert linked_tree in tracker.reviewed_tree_files(root)

    monkeypatch.setattr(
        tracker.os.path,
        "isjunction",
        lambda path: Path(path) == root or real_is_junction(path),
        raising=False,
    )
    assert tracker.reviewed_tree_files(root) == [root]


def _browser_proof_workspace(tmp_path, monkeypatch, *, kind="browser"):
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    run = qa / "runs" / "browser-proof"
    run.mkdir(parents=True)
    relative_sources = {
        "web/student.js": b"student\n",
        "scripts/story_tracker.py": b"tracker\n",
        "scripts/qa_browser.py": b"browser\n",
        "scripts/qa_evidence_manifest.py": b"manifest\n",
        "docs/qa/stories.tsv": b"catalog\n",
    }
    if kind == "native-browser":
        relative_sources.update(
            {
                "server/pipeline.py": b"pipeline\n",
                "scripts/qa_native_browser.py": b"native\n",
                "tests/conftest.py": b"fixtures\n",
            }
        )
    source_files = []
    for relative, content in relative_sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        source_files.append(path)
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(
        tracker, "browser_source_files", lambda *, native: list(source_files)
    )
    payload = {
        "finished_at": "2026-09-08T12:00:00+09:00",
        "results": [
            {"story": "JOIN-02", "scenario": "qr_query_prefills_code", "status": "pass"}
        ],
    }
    _write_browser_envelope(
        run,
        payload=payload,
        source_hashes=tracker.hash_files(source_files),
        kind=kind,
    )
    reference = (run / "browser.json").relative_to(root).as_posix()
    return run, payload, reference


@pytest.mark.parametrize("field", ["status", "story", "scenario", "finished_at"])
def test_current_browser_proof_uses_only_bytes_sealed_by_a_complete_manifest(
    field, tmp_path, monkeypatch
) -> None:
    run, payload, reference = _browser_proof_workspace(tmp_path, monkeypatch)

    assert tracker.current_browser_proof("JOIN-02", reference)

    changed = json.loads(json.dumps(payload))
    if field == "finished_at":
        changed[field] = "2026- competing write"
    else:
        changed["results"][0][field] = "tampered"
    (run / "browser.json").write_bytes(_json_bytes(changed))

    assert not tracker.current_browser_proof("JOIN-02", reference)


def test_current_browser_proof_rejects_an_old_flat_source_hash_file(
    tmp_path, monkeypatch
) -> None:
    run, _, reference = _browser_proof_workspace(tmp_path, monkeypatch)
    (run / "source-hashes.json").write_text("{}", encoding="utf-8")

    assert not tracker.current_browser_proof("JOIN-02", reference)


def test_browser_manifest_kind_cannot_shrink_native_proof_scope(
    tmp_path, monkeypatch
) -> None:
    run, payload, reference = _browser_proof_workspace(tmp_path, monkeypatch)
    payload["results"][0]["scenario"] = "native_qr_query_prefills_code"
    _write_browser_envelope(
        run,
        payload=payload,
        source_hashes=tracker.hash_files(tracker.browser_source_files(native=False)),
        kind="browser",
    )

    assert not tracker.current_browser_proof(
        "JOIN-02", reference, required={"native_qr_query_prefills_code"}
    )


def test_current_browser_proof_rejects_an_incomplete_manifest(
    tmp_path, monkeypatch
) -> None:
    run, payload, reference = _browser_proof_workspace(tmp_path, monkeypatch)
    _write_browser_envelope(
        run,
        payload=payload,
        source_hashes=tracker.hash_files(tracker.browser_source_files(native=False)),
        complete=False,
    )

    assert not tracker.current_browser_proof("JOIN-02", reference)


def test_current_browser_proof_fails_closed_when_current_sources_are_link_like(
    tmp_path, monkeypatch
) -> None:
    _, _, reference = _browser_proof_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(
        tracker,
        "snapshot_supplied_source_hashes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            tracker.EvidenceManifestError("source is a link")
        ),
    )

    assert not tracker.current_browser_proof("JOIN-02", reference)


@pytest.mark.parametrize(
    "finished_at",
    ["not-a-timestamp", "2026-09-08T12:00:00", " 2026-09-08T12:00:00+09:00"],
)
def test_current_browser_proof_rejects_an_invalid_or_naive_finished_at(
    finished_at, tmp_path, monkeypatch
) -> None:
    run, payload, reference = _browser_proof_workspace(tmp_path, monkeypatch)
    payload["finished_at"] = finished_at
    _write_browser_envelope(
        run,
        payload=payload,
        source_hashes=tracker.hash_files(
            tracker.browser_source_files(native=False)
        ),
    )

    assert not tracker.current_browser_proof("JOIN-02", reference)


def _pytest_proof_workspace(tmp_path, monkeypatch, *, complete=True, kind="pytest"):
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    run = qa / "runs" / "pytest-proof"
    run.mkdir(parents=True)
    sources = []
    for relative, content in {
        "config.yaml": b"config\n",
        "server/main.py": b"main\n",
        "scripts/story_tracker.py": b"tracker\n",
        "tests/test_shared.py": b"tests\n",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        sources.append(path)
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: list(sources))
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", {"OPS-04"})
    monkeypatch.setattr(
        tracker,
        "catalog",
        lambda: [
            {
                "ID": "OPS-04",
                "自動テスト候補": "tests/test_shared.py::test_green",
            }
        ],
    )
    xml = (
        b'<testsuite><testcase classname="tests.test_shared" '
        b'name="test_green"/></testsuite>'
    )
    story = {
        "OPS-04": {
            "cases": [
                {"nodeid": "tests/test_shared.py::test_forged", "status": "fail"}
            ]
        }
    }
    _write_pytest_envelope(
        run,
        xml=xml,
        source_hashes=tracker.hash_files(sources),
        complete=complete,
        story_evidence=story,
        kind=kind,
    )
    reference = (run / "story-evidence.json").relative_to(root).as_posix()
    return run, reference


def test_current_pytest_proof_reconstructs_cases_from_the_sealed_junit_bytes(
    tmp_path, monkeypatch
) -> None:
    _, reference = _pytest_proof_workspace(tmp_path, monkeypatch)

    assert tracker.current_pytest_proof("OPS-04", reference)


def test_current_pytest_proof_binds_selectors_to_exact_catalog_bytes(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    run = qa / "runs" / "catalog-proof"
    run.mkdir(parents=True)
    selector = "tests/test_catalog.py::test_green"
    sources = []
    for relative, content in {
        "config.yaml": b"config\n",
        "server/main.py": b"server\n",
        "scripts/story_tracker.py": b"tracker\n",
        "tests/test_catalog.py": b"def test_green(): pass\n",
        "docs/qa/stories.tsv": _catalog_bytes("OPS-CAT", selector),
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        sources.append(path)
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "CATALOG", qa / "stories.tsv")
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: list(sources))
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", {"OPS-CAT"})
    xml = (
        b'<testsuite><testcase classname="tests.test_catalog" '
        b'name="test_green"/></testsuite>'
    )
    _write_pytest_envelope(
        run,
        xml=xml,
        source_hashes=tracker.hash_files(sources),
        complete=True,
        story_evidence={"OPS-CAT": {}},
    )
    reference = (run / "story-evidence.json").relative_to(root).as_posix()

    assert tracker.current_pytest_proof("OPS-CAT", reference)

    real_read = tracker._read_bound_repository_file

    def weakened_catalog_read(path, *, root):
        value, witness = real_read(path, root=root)
        if Path(path) == tracker.CATALOG:
            return _catalog_bytes("OPS-CAT", selector, area="weakened"), witness
        return value, witness

    monkeypatch.setattr(
        tracker, "_read_bound_repository_file", weakened_catalog_read
    )

    assert not tracker.current_pytest_proof("OPS-CAT", reference)


@pytest.mark.parametrize("kind", ["browser", "pytest"])
def test_current_proof_rejects_a_lexically_linked_run_directory(
    kind, tmp_path, monkeypatch
) -> None:
    if kind == "browser":
        run, _payload, reference = _browser_proof_workspace(
            tmp_path, monkeypatch
        )
        proof = lambda: tracker.current_browser_proof("JOIN-02", reference)
    else:
        run, reference = _pytest_proof_workspace(tmp_path, monkeypatch)
        proof = lambda: tracker.current_pytest_proof("OPS-04", reference)
    real_run = run.with_name(f"{run.name}-real")
    run.rename(real_run)
    try:
        run.symlink_to(real_run, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    assert not proof()


@pytest.mark.parametrize("artifact", ["pytest.xml", "run.json", "story-evidence.json"])
def test_current_pytest_proof_rejects_any_tampered_sealed_artifact(
    artifact, tmp_path, monkeypatch
) -> None:
    run, reference = _pytest_proof_workspace(tmp_path, monkeypatch)
    (run / artifact).write_bytes((run / artifact).read_bytes() + b"tampered")

    assert not tracker.current_pytest_proof("OPS-04", reference)


@pytest.mark.parametrize(
    ("complete", "kind"),
    [(False, "pytest"), (True, "browser")],
)
def test_current_pytest_proof_requires_a_complete_pytest_manifest(
    complete, kind, tmp_path, monkeypatch
) -> None:
    _, reference = _pytest_proof_workspace(
        tmp_path, monkeypatch, complete=complete, kind=kind
    )

    assert not tracker.current_pytest_proof("OPS-04", reference)


def test_current_pytest_proof_fails_closed_when_current_sources_cannot_be_read(
    tmp_path, monkeypatch
) -> None:
    _, reference = _pytest_proof_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(
        tracker,
        "snapshot_supplied_source_hashes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            tracker.EvidenceManifestError("source changed while being read")
        ),
    )

    assert not tracker.current_pytest_proof("OPS-04", reference)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("finished_at", "not-a-timestamp"),
        ("finished_at", "2026-09-08T12:00:00"),
        ("finished_at", " 2026-09-08T12:00:00+09:00"),
        ("exit_code", True),
    ],
)
def test_current_pytest_proof_rejects_semantically_malformed_run_metadata(
    field, value, tmp_path, monkeypatch
) -> None:
    run, reference = _pytest_proof_workspace(tmp_path, monkeypatch)
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    meta[field] = value
    _write_pytest_envelope(
        run,
        xml=(run / "pytest.xml").read_bytes(),
        source_hashes=tracker.hash_files(tracker.pytest_source_files()),
        complete=True,
        meta=meta,
        story_evidence={"OPS-04": {}},
    )

    assert not tracker.current_pytest_proof("OPS-04", reference)


@pytest.mark.parametrize(
    ("selector", "pytest_arg", "collected"),
    [
        (
            "tests/unit/test_model_guards.py::test_model_guard",
            "tests/unit/test_model_guards.py::test_model_guard[cpu]",
            "tests/unit/test_model_guards.py::test_model_guard[cpu]",
        ),
        (
            "tests/unit/test_mt_engines.py",
            "tests/unit/test_mt_engines.py::test_one_engine",
            "tests/unit/test_mt_engines.py::test_one_engine",
        ),
    ],
    ids=["parameterized-base", "file-level"],
)
def test_current_pytest_proof_rejects_leaf_scope_for_a_broad_catalog_selector(
    selector, pytest_arg, collected, tmp_path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    run = qa / "runs" / "leaf-only"
    run.mkdir(parents=True)
    sources = []
    for relative in (
        "config.yaml",
        "server/main.py",
        "scripts/story_tracker.py",
        selector.split("::", 1)[0],
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
        sources.append(path)
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: list(sources))
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", {"OPS-SCOPE"})
    monkeypatch.setattr(
        tracker,
        "catalog",
        lambda: [{"ID": "OPS-SCOPE", "自動テスト候補": selector}],
    )
    node_name = collected.rsplit("::", 1)[-1]
    xml = (
        '<testsuite><testcase classname="'
        + selector.split("::", 1)[0].removesuffix(".py").replace("/", ".")
        + f'" name="{node_name}"/></testsuite>'
    ).encode("utf-8")
    _write_pytest_envelope(
        run,
        xml=xml,
        source_hashes=tracker.hash_files(sources),
        complete=True,
        meta={
            "pytest_args": [pytest_arg],
            "collected_nodeids": [collected],
        },
        story_evidence={"OPS-SCOPE": {}},
    )
    reference = (run / "story-evidence.json").relative_to(root).as_posix()

    sealed = tracker.load_sealed_pytest_evidence(run, expected_complete=True)
    assert tracker._validated_pytest_run_payload(sealed.run_payload)
    assert not tracker.current_pytest_proof("OPS-SCOPE", reference)


@pytest.mark.parametrize(
    ("story", "leaf_nodeids"),
    [
        (
            "OPS-05",
            [
                "tests/invariants/test_failure_modes.py::TestFailClosedOnMissingModels::test_missing_asr_model_never_falls_back_to_fake",
                "qa_checks/test_ops05_join05_contracts.py::test_model_file_guard_rejects_missing_or_undersized_artifacts[missing-asr]",
                "qa_checks/test_ops05_join05_contracts.py::test_asr_factory_enforces_model_file_guard[faster-whisper]",
                "qa_checks/test_ops05_join05_contracts.py::test_mt_factory_enforces_model_file_guard[nllb]",
            ],
        ),
        (
            "OPS-07",
            [
                "tests/unit/test_mt_engines.py::test_hymt_prompt_contains_lang_label_and_text",
                "tests/integration/test_ws_boundary.py::TestHttpEndpoints::test_api_config_exposes_languages",
                "qa_checks/test_story_edges.py::test_ops07_invalid_language_configuration[languages0]",
            ],
        ),
    ],
)
def test_sync_never_promotes_real_catalog_broad_selectors_from_one_leaf_each(
    story, leaf_nodeids, tmp_path, monkeypatch
) -> None:
    catalog_row = next(row for row in tracker.catalog() if row["ID"] == story)
    selectors = catalog_row["自動テスト候補"].split(";")
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    run = qa / "runs" / "leaf-subset"
    run.mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook")
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: [])
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_args: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", {story})
    monkeypatch.setattr(tracker, "AUDITED_BROWSER", {})
    monkeypatch.setattr(tracker, "AUDITED_COMPOSITE", {})
    monkeypatch.setattr(tracker, "AUDITED_PERFORMANCE", {})
    monkeypatch.setattr(tracker, "AUDITED_FIELD", set())
    monkeypatch.setattr(tracker, "catalog", lambda: [catalog_row])
    cases = [
        {"nodeid": nodeid, "status": "pass", "detail": ""}
        for nodeid in leaf_nodeids
    ]
    monkeypatch.setattr(tracker, "junit_cases", lambda _source: list(cases))
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: {"A15": story, "F15": "未実施", "M15": ""},
    )
    operations = []
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_args: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    (run / "pytest.xml").write_text("<testsuite/>", encoding="utf-8")
    (run / "run.json").write_text(
        json.dumps(
            {
                "finished_at": "2026-09-09T01:00:00+09:00",
                "pytest_args": leaf_nodeids,
            }
        ),
        encoding="utf-8",
    )
    _seal_existing_pytest_run(run)

    tracker.sync(run)

    assert tracker.setcell("F15", value="合格") not in operations
    assert tracker.setcell("F15", value="部分確認") in operations
    evidence = json.loads((run / "story-evidence.json").read_text(encoding="utf-8"))
    assert set(evidence[story]["missing_selectors"]) == set(selectors)


@pytest.mark.parametrize(
    ("story", "leaf_nodeid"),
    [
        (
            "OPS-05",
            "qa_checks/test_ops05_join05_contracts.py::"
            "test_model_file_guard_rejects_missing_or_undersized_artifacts[missing-asr]",
        ),
        (
            "OPS-07",
            "tests/unit/test_mt_engines.py::"
            "test_hymt_prompt_contains_lang_label_and_text",
        ),
    ],
)
def test_sync_records_a_failing_leaf_even_when_it_cannot_complete_the_broad_scope(
    story, leaf_nodeid, tmp_path, monkeypatch
) -> None:
    catalog_row = next(row for row in tracker.catalog() if row["ID"] == story)
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    run = qa / "runs" / "failing-leaf"
    run.mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook")
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: [])
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_args: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", {story})
    monkeypatch.setattr(tracker, "AUDITED_BROWSER", {})
    monkeypatch.setattr(tracker, "AUDITED_COMPOSITE", {})
    monkeypatch.setattr(tracker, "AUDITED_PERFORMANCE", {})
    monkeypatch.setattr(tracker, "AUDITED_FIELD", set())
    monkeypatch.setattr(tracker, "catalog", lambda: [catalog_row])
    cases = [
        {"nodeid": leaf_nodeid, "status": "fail", "detail": "intentional"}
    ]
    monkeypatch.setattr(tracker, "junit_cases", lambda _source: list(cases))
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: {"A15": story, "F15": "合格", "M15": "old proof"},
    )
    operations = []
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_args: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    (run / "pytest.xml").write_text("<testsuite/>", encoding="utf-8")
    (run / "run.json").write_text(
        json.dumps(
            {
                "finished_at": "2026-09-09T01:00:00+09:00",
                "pytest_args": [leaf_nodeid],
            }
        ),
        encoding="utf-8",
    )
    _seal_existing_pytest_run(run)

    tracker.sync(run)

    assert tracker.setcell("F15", value="不合格") in operations
    evidence = json.loads((run / "story-evidence.json").read_text(encoding="utf-8"))
    assert evidence[story]["counts"]["fail"] == 1
    assert evidence[story]["missing_selectors"]


def test_pytest_story_selection_requires_collection_and_junit_descendants_to_match() -> None:
    selector = "tests/test_many.py"
    meta = {
        "pytest_args": [selector],
        "collected_nodeids": [
            f"{selector}::test_one",
            f"{selector}::test_two",
        ],
    }
    cases = [
        {"nodeid": f"{selector}::test_one", "status": "pass", "detail": ""}
    ]

    complete, selected, missing = tracker._pytest_story_selection(
        [selector], cases, meta
    )

    assert complete is True
    assert selected == cases
    assert missing == [selector]


def test_pytest_story_selection_rejects_a_junit_only_failing_sibling() -> None:
    selector = "tests/test_many.py"
    meta = {
        "pytest_args": [selector],
        "collected_nodeids": [f"{selector}::test_one"],
    }
    cases = [
        {"nodeid": f"{selector}::test_one", "status": "pass", "detail": ""},
        {"nodeid": f"{selector}::test_two", "status": "fail", "detail": "boom"},
    ]

    complete, selected, missing = tracker._pytest_story_selection(
        [selector], cases, meta
    )

    assert complete is True
    assert selected == cases
    assert missing == [selector]


@pytest.mark.parametrize(
    ("selector", "nodeids"),
    [
        (
            "tests/test_many.py",
            ["tests/test_many.py::test_one", "tests/test_many.py::test_two"],
        ),
        (
            "tests/test_many.py::test_param",
            [
                "tests/test_many.py::test_param[first]",
                "tests/test_many.py::test_param[second]",
            ],
        ),
    ],
)
def test_pytest_story_selection_accepts_the_exact_complete_descendant_set(
    selector, nodeids
) -> None:
    meta = {"pytest_args": [selector], "collected_nodeids": nodeids}
    cases = [
        {"nodeid": nodeid, "status": "pass", "detail": ""}
        for nodeid in nodeids
    ]

    complete, selected, missing = tracker._pytest_story_selection(
        [selector], cases, meta
    )

    assert complete is True
    assert selected == cases
    assert missing == []


def test_validated_junit_cases_rejects_duplicate_nodeids_even_if_last_passes(
    monkeypatch,
) -> None:
    duplicate = [
        {"nodeid": "tests/test_one.py::test_one", "status": "fail", "detail": ""},
        {"nodeid": "tests/test_one.py::test_one", "status": "pass", "detail": ""},
    ]
    monkeypatch.setattr(tracker, "junit_cases", lambda _source: duplicate)

    with pytest.raises(ValueError, match="duplicate"):
        tracker._validated_junit_cases(b"<testsuite/>")


def test_batch_uses_a_staged_workbook_when_a_later_chunk_fails(
    tmp_path, monkeypatch
) -> None:
    qa = tmp_path / "docs" / "qa"
    qa.mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    original = b"original workbook"
    book.write_bytes(original)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    calls = 0

    def failing_office(action, staged, *_args):
        nonlocal calls
        assert action == "batch"
        assert Path(staged) != book
        calls += 1
        Path(staged).write_bytes(f"partial chunk {calls}".encode())
        if calls == 2:
            raise RuntimeError("second chunk failed")
        return ""

    monkeypatch.setattr(tracker, "office", failing_office)

    with pytest.raises(RuntimeError, match="second chunk failed"):
        tracker.batch([tracker.setcell(f"A{index}", value="x") for index in range(41)])

    assert calls == 2
    assert book.read_bytes() == original


def test_batch_cas_preserves_a_concurrent_live_workbook_edit(
    tmp_path, monkeypatch
) -> None:
    qa = tmp_path / "docs" / "qa"
    qa.mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"original workbook")
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)

    def editing_office(_action, staged, *_args):
        Path(staged).write_bytes(b"finished staged workbook")
        book.write_bytes(b"concurrent user edit")
        return ""

    monkeypatch.setattr(tracker, "office", editing_office)

    with pytest.raises(SystemExit, match="changed while staged"):
        tracker.batch([tracker.setcell("A7", value="x")])

    assert book.read_bytes() == b"concurrent user edit"


def test_exclusive_workbook_backup_rejects_a_dangling_symlink_without_following_it(
    tmp_path, monkeypatch
) -> None:
    qa = tmp_path / "docs" / "qa"
    run = qa / "runs" / "run"
    run.mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook")
    outside = tmp_path / "outside.xlsx"
    backup = run / "feature-status-before-sync.xlsx"
    try:
        backup.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    monkeypatch.setattr(tracker, "BOOK", book)

    with pytest.raises(SystemExit, match="already exists"):
        tracker._exclusive_workbook_backup(backup)

    assert backup.is_symlink()
    assert not outside.exists()


def test_exclusive_evidence_writer_rejects_a_dangling_symlink_without_external_write(
    tmp_path,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    outside = tmp_path / "outside.json"
    evidence = run / "story-evidence.json"
    try:
        evidence.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(SystemExit, match="already exists"):
        tracker._exclusive_regular_file_write(
            evidence, b"{}", label="story evidence"
        )

    assert evidence.is_symlink()
    assert not outside.exists()


def test_atomic_html_replace_rejects_a_symlink_and_preserves_external_bytes(
    tmp_path,
) -> None:
    qa = tmp_path / "qa"
    qa.mkdir()
    outside = tmp_path / "outside.html"
    outside.write_bytes(b"external")
    preview = qa / "feature-status.html"
    try:
        preview.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(SystemExit, match="must not be a link"):
        tracker._atomic_replace_regular_file(
            preview, b"new preview", label="HTML preview"
        )

    assert outside.read_bytes() == b"external"


def test_atomic_html_replace_updates_an_existing_regular_file(tmp_path) -> None:
    qa = tmp_path / "qa"
    qa.mkdir()
    preview = qa / "feature-status.html"
    preview.write_bytes(b"old preview")

    tracker._atomic_replace_regular_file(
        preview, b"new preview", label="HTML preview"
    )

    assert preview.read_bytes() == b"new preview"


def test_at_least_ignores_only_binary_float_noise_at_an_equal_boundary() -> None:
    minimum = 120.016 + 20.0

    assert tracker._at_least(140.016, minimum)
    assert not tracker._at_least(140.015, minimum)


def test_sync_rejects_a_stale_run_before_touching_the_workbook(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    source = tmp_path / "source.py"
    source.write_text("tested version", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    _write_pytest_envelope(
        run,
        xml=b"<testsuite/>",
        source_hashes=tracker.hash_files([source]),
        complete=False,
    )
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: [source])
    source.write_text("new version", encoding="utf-8")

    with pytest.raises(SystemExit, match="現行状態で再実行"):
        tracker.sync(run)


def test_sync_rejects_a_legacy_flat_pytest_manifest_before_workbook_access(
    tmp_path, monkeypatch
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "pytest.xml").write_bytes(b"<testsuite/>")
    (run / "pytest.log").write_bytes(b"output\n")
    (run / "run.json").write_text(
        json.dumps({"finished_at": "2026-09-08T12:00:00+09:00"}),
        encoding="utf-8",
    )
    (run / "source-hashes.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: pytest.fail("legacy evidence must fail before workbook access"),
    )

    with pytest.raises(SystemExit, match="未完了seal"):
        tracker.sync(run)


def test_sync_seals_story_evidence_once_before_updating_the_workbook(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    run = qa / "runs" / "pytest-sync"
    run.mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook")
    source = root / "source.py"
    source.write_bytes(b"source\n")
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    _write_pytest_envelope(
        run,
        xml=(
            b'<testsuite><testcase classname="tests.test_one" '
            b'name="test_green"/></testsuite>'
        ),
        source_hashes={
            "source.py": hashlib.sha256(source.read_bytes()).hexdigest()
        },
        complete=False,
    )
    operations = []
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: [source])
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", {"OPS-TEST"})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(
        tracker,
        "catalog",
        lambda: [
            {
                "ID": "OPS-TEST",
                "自動テスト候補": "tests/test_one.py::test_green",
                "検証方式": "自動",
                "残作業・注意": "",
            }
        ],
    )
    cells = {"A15": "OPS-TEST", "F15": "未実施", "M15": ""}
    monkeypatch.setattr(tracker, "read_cells", lambda: dict(cells))
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_args: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    dependency_checks = []

    def current_completed_run(story, evidence, **_kwargs):
        sealed = tracker.load_sealed_pytest_evidence(
            run, expected_complete=True
        )
        dependency_checks.append((story, evidence, sealed.manifest["complete"]))
        return story == "OPS-TEST" and "story-evidence.json" in evidence

    monkeypatch.setattr(
        tracker, "current_pytest_proof", current_completed_run
    )

    tracker.sync(run)

    sealed = tracker.load_sealed_pytest_evidence(run, expected_complete=True)
    assert set(sealed.manifest["artifacts"]) == {
        "pytest.xml",
        "pytest.log",
        "run.json",
        "story-evidence.json",
    }
    assert sealed.story_evidence_payload["OPS-TEST"]["cases"] == [
        {
            "nodeid": "tests/test_one.py::test_green",
            "status": "pass",
            "detail": "",
        }
    ]
    assert tracker.setcell("F15", value="合格") in operations
    assert (run / "feature-status-before-sync.xlsx").is_file()
    assert len(dependency_checks) >= 2
    assert all(complete is True for _, _, complete in dependency_checks)

    with pytest.raises(SystemExit, match="未完了seal"):
        tracker.sync(run)


def test_browser_sync_rejects_a_stale_run_before_touching_the_workbook(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    source = tmp_path / "browser-source.js"
    source.write_text("tested version", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    _write_browser_envelope(
        run,
        payload={
            "finished_at": "2026-09-08T12:00:00+09:00",
            "results": [{"story": "X", "scenario": "dom_case", "status": "pass"}],
        },
        source_hashes=tracker.hash_files([source]),
    )
    monkeypatch.setattr(tracker, "browser_source_files", lambda *, native: [source])
    source.write_text("new version", encoding="utf-8")

    with pytest.raises(SystemExit, match="現行状態で再実行"):
        tracker.sync_browser(run)


def test_browser_sync_rejects_a_legacy_flat_manifest_before_workbook_access(
    tmp_path, monkeypatch
) -> None:
    run = tmp_path / "browser-run"
    run.mkdir()
    (run / "browser.json").write_text(
        json.dumps(
            {
                "finished_at": "2026-09-08T12:00:00+09:00",
                "results": [
                    {"story": "X", "scenario": "dom_case", "status": "pass"}
                ],
            }
        ),
        encoding="utf-8",
    )
    (run / "source-hashes.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: pytest.fail("legacy evidence must fail before workbook access"),
    )

    with pytest.raises(SystemExit, match="complete seal"):
        tracker.sync_browser(run)


def test_browser_sync_derives_native_scope_only_from_the_sealed_manifest(
    tmp_path, monkeypatch
) -> None:
    run = tmp_path / "browser-run"
    run.mkdir()
    _write_browser_envelope(
        run,
        kind="native-browser",
        source_hashes={},
        payload={
            "finished_at": "2026-09-08T12:00:00+09:00",
            "results": [
                {"story": "X", "scenario": "dom_case", "status": "pass"}
            ],
            "environment_skips": [],
        },
    )
    selected_native: list[bool] = []
    source = tmp_path / "native-source.py"
    source.write_text("current", encoding="utf-8")

    def source_files(*, native: bool):
        selected_native.append(native)
        return [source]

    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "browser_source_files", source_files)
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: pytest.fail("stale evidence must be rejected before workbook access"),
    )

    with pytest.raises(SystemExit, match="現行状態で再実行"):
        tracker.sync_browser(run)

    assert len(selected_native) >= 2
    assert set(selected_native) == {True}


def test_browser_sync_rejects_empty_results_before_touching_the_workbook(
    tmp_path, monkeypatch
) -> None:
    run = tmp_path / "browser-run"
    run.mkdir()
    _write_browser_envelope(
        run,
        kind="native-browser",
        source_hashes={},
        payload={
            "finished_at": "2026-09-08T12:00:00+09:00",
            "results": [],
            "environment_skips": [
                {
                    "story": "OPS-10",
                    "scenario": "native_lan_teacher_url_qr_and_caption",
                }
            ],
        },
    )
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: pytest.fail("empty results must not read or modify the workbook"),
    )

    with pytest.raises(SystemExit, match="実行結果がない"):
        tracker.sync_browser(run)


def test_audit_modes_and_composite_selectors_are_consistent() -> None:
    tracker.validate_audit_configuration()


@pytest.mark.parametrize(
    "story", ["OPS-09", "OPS-11", "PERF-03"],
    ids=["composite", "automatic", "performance"],
)
def test_green_subset_keeps_current_complete_workbook_proof(story, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: [])
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(
        tracker,
        "catalog",
        lambda: [{
            "ID": story,
            "自動テスト候補": "case-one;case-two",
            "検証方式": "ブラウザ" if story == "OPS-09" else "自動",
            "残作業・注意": "",
        }],
    )
    monkeypatch.setattr(
        tracker,
        "junit_cases",
        lambda _: [{"nodeid": "case-one", "status": "pass", "detail": ""}],
    )
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: {"A15": story, "F15": "合格", "M15": "current complete proof"},
    )
    monkeypatch.setattr(
        tracker,
        "current_browser_proof",
        lambda candidate_story, *_, **__: candidate_story == "OPS-09",
    )
    monkeypatch.setattr(
        tracker, "current_pytest_proof", lambda *_, **__: True
    )
    monkeypatch.setattr(
        tracker,
        "current_performance_proof",
        lambda candidate_story, *_, **__: candidate_story == "PERF-03",
    )
    workbook_ops = []
    monkeypatch.setattr(tracker, "batch", lambda ops: workbook_ops.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    run = tmp_path / "run"
    run.mkdir()
    (run / "pytest.xml").write_text("<testsuite/>", encoding="utf-8")
    (run / "run.json").write_text(
        json.dumps({"finished_at": "2026-09-07T00:00:00+09:00"}), encoding="utf-8"
    )
    _seal_existing_pytest_run(run)

    tracker.sync(run)

    assert workbook_ops == []
    evidence = json.loads((run / "story-evidence.json").read_text(encoding="utf-8"))
    assert evidence[story]["status"] == "合格"
    assert evidence[story]["workbook_preserved"] is True


def test_current_performance_proof_repromotes_a_partial_workbook_row(
    tmp_path, monkeypatch
) -> None:
    story = "PERF-03"
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: [])
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(
        tracker,
        "catalog",
        lambda: [{
            "ID": story,
            "自動テスト候補": "case-one;case-two",
            "検証方式": "長時間",
            "残作業・注意": "",
        }],
    )
    monkeypatch.setattr(
        tracker,
        "junit_cases",
        lambda _: [{"nodeid": "case-one", "status": "pass", "detail": ""}],
    )
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: {"A15": story, "F15": "部分確認", "M15": "current performance proof"},
    )
    monkeypatch.setattr(
        tracker, "current_performance_proof", lambda *_, **__: True
    )
    workbook_ops = []
    monkeypatch.setattr(tracker, "batch", lambda ops: workbook_ops.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    run = tmp_path / "run"
    run.mkdir()
    (run / "pytest.xml").write_text("<testsuite/>", encoding="utf-8")
    (run / "run.json").write_text(
        json.dumps({"finished_at": "2026-09-08T00:00:00+09:00"}),
        encoding="utf-8",
    )
    _seal_existing_pytest_run(run)

    tracker.sync(run)

    assert tracker.setcell("F15", value="合格") in workbook_ops
    evidence = json.loads((run / "story-evidence.json").read_text(encoding="utf-8"))
    assert evidence[story]["status"] == "合格"


def test_audited_real_model_story_promotes_when_all_selectors_are_green(
    tmp_path, monkeypatch
) -> None:
    story = "OPS-REAL"
    selectors = [
        (
            "tests/integration/test_real_asr.py::TestFasterWhisperEngine::"
            "test_transcribes_japanese_fixture"
        ),
        "tests/integration/test_real_mt.py::TestNllbEngine::test_translates_to_english",
    ]
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", {story})
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: [])
    monkeypatch.setattr(
        tracker,
        "snapshot_supplied_source_hashes",
        lambda *_: tracker.SourceHashSnapshot({}, {}),
    )
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(
        tracker,
        "catalog",
        lambda: [{
            "ID": story,
            "自動テスト候補": ";".join(selectors),
            "検証方式": "実モデル",
            "残作業・注意": "",
        }],
    )
    monkeypatch.setattr(
        tracker,
        "junit_cases",
        lambda _: [
            {"nodeid": selector, "status": "pass", "detail": ""}
            for selector in selectors
        ],
    )
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: {"A15": story, "F15": "未実施", "M15": ""},
    )
    workbook_ops = []
    monkeypatch.setattr(tracker, "batch", lambda ops: workbook_ops.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    monkeypatch.setattr(
        tracker,
        "current_pytest_proof",
        lambda candidate, *_args, **_kwargs: candidate == story,
    )
    run = tmp_path / "run"
    run.mkdir()
    (run / "pytest.xml").write_text("<testsuite/>", encoding="utf-8")
    (run / "run.json").write_text(
        json.dumps({"finished_at": "2026-09-07T00:00:00+09:00"}), encoding="utf-8"
    )
    _seal_existing_pytest_run(run)

    tracker.sync(run)

    evidence = json.loads((run / "story-evidence.json").read_text(encoding="utf-8"))
    assert evidence[story]["counts"] == {"pass": 2, "fail": 0, "error": 0, "skip": 0}
    assert evidence[story]["missing_selectors"] == []
    assert evidence[story]["status"] == "合格"
    assert tracker.setcell("F15", value="合格") in workbook_ops


def test_green_supplemental_browser_run_keeps_current_pytest_pass(
    tmp_path, monkeypatch
) -> None:
    story = "OPS-AUTO"
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "browser_source_files", lambda *, native: [])
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: {"A15": story, "F15": "合格", "M15": "current pytest proof"},
    )
    monkeypatch.setattr(tracker, "current_browser_proof", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        tracker,
        "current_pytest_proof",
        lambda candidate, *_, **__: candidate == story,
    )
    workbook_ops = []
    monkeypatch.setattr(tracker, "batch", lambda ops: workbook_ops.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    run = tmp_path / "browser-run"
    run.mkdir()
    (run / "source-hashes.json").write_text("{}", encoding="utf-8")
    (run / "browser.json").write_text(
        json.dumps(
            {
                "finished_at": "2026-09-07T00:00:00+09:00",
                "results": [
                    {"story": story, "scenario": "supplemental", "status": "pass"}
                ],
            }
        ),
        encoding="utf-8",
    )
    _seal_existing_browser_run(run)

    tracker.sync_browser(run)

    assert tracker.setcell("F15", value="合格") in workbook_ops


def test_green_supplemental_browser_run_keeps_current_performance_pass(
    tmp_path, monkeypatch
) -> None:
    story = "PERF-03"
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "browser_source_files", lambda *, native: [])
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: {"A15": story, "F15": "合格", "M15": "current performance proof"},
    )
    monkeypatch.setattr(tracker, "current_browser_proof", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        tracker,
        "current_performance_proof",
        lambda candidate, *_, **__: candidate == story,
    )
    monkeypatch.setattr(
        tracker, "current_pytest_proof", lambda *_, **__: False
    )
    workbook_ops = []
    monkeypatch.setattr(tracker, "batch", lambda ops: workbook_ops.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    run = tmp_path / "browser-run"
    run.mkdir()
    (run / "source-hashes.json").write_text("{}", encoding="utf-8")
    (run / "browser.json").write_text(
        json.dumps({
            "finished_at": "2026-09-07T00:00:00+09:00",
            "results": [{"story": story, "scenario": "supplemental", "status": "pass"}],
        }),
        encoding="utf-8",
    )
    _seal_existing_browser_run(run)

    tracker.sync_browser(run)

    assert tracker.setcell("F15", value="合格") in workbook_ops


def test_green_supplemental_browser_run_keeps_current_composite_pass(
    tmp_path, monkeypatch
) -> None:
    story = "OPS-COMPOSITE"
    required_browser = {"required_native"}
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(
        tracker,
        "AUDITED_COMPOSITE",
        {story: {"browser": required_browser, "pytest": {"required_pytest"}}},
    )
    monkeypatch.setattr(tracker, "browser_source_files", lambda *, native: [])
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    existing = (
        "docs/qa/runs/old-native-browser/browser.json #OPS-COMPOSITE; "
        "required_native=pass\n"
        "pytest: docs/qa/runs/old/story-evidence.json #OPS-COMPOSITE"
    )
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: {"A15": story, "F15": "合格", "M15": existing},
    )

    def browser_proof(candidate, evidence, *, required=None, **_kwargs):
        return (
            candidate == story
            and required == required_browser
            and "old-native-browser/browser.json" in evidence
        )

    monkeypatch.setattr(tracker, "current_browser_proof", browser_proof)
    monkeypatch.setattr(
        tracker,
        "current_pytest_proof",
        lambda candidate, *_, **__: candidate == story,
    )
    workbook_ops = []
    monkeypatch.setattr(tracker, "batch", lambda ops: workbook_ops.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    run = tmp_path / "browser-run"
    run.mkdir()
    (run / "source-hashes.json").write_text("{}", encoding="utf-8")
    (run / "browser.json").write_text(
        json.dumps(
            {
                "finished_at": "2026-09-07T00:00:00+09:00",
                "results": [
                    {"story": story, "scenario": "supplemental", "status": "pass"}
                ],
            }
        ),
        encoding="utf-8",
    )
    _seal_existing_browser_run(run)

    tracker.sync_browser(run)

    assert tracker.setcell("F15", value="合格") in workbook_ops


def test_compaction_preserves_required_composite_browser_proof() -> None:
    full = (
        "docs/qa/runs/full-native-browser/browser.json #OPS-09; "
        "native_http_audio_worklet=pass"
    )
    supplemental_one = (
        "docs/qa/runs/supplemental-one-browser/browser.json #OPS-09; "
        "supplemental_one=pass"
    )
    supplemental_two = (
        "docs/qa/runs/supplemental-two-browser/browser.json #OPS-09; "
        "supplemental_two=pass"
    )

    compacted = tracker.compact_evidence(
        "\n".join((full, supplemental_one, supplemental_two))
    )

    assert full in compacted
    assert supplemental_one in compacted
    assert supplemental_two in compacted


def test_compaction_keeps_current_proof_types_without_growing_forever() -> None:
    value = "\n".join(
        [
            "pytest: old",
            "pytest: current",
            "docs/qa/runs/a-browser/browser.json #X; first=pass",
            "docs/qa/runs/b-browser/browser.json #X; second=pass",
            "docs/qa/runs/c-browser/browser.json #X; third=pass",
            "docs/qa/runs/d-browser/browser.json #X; broken=fail",
            "90分試験実行中: docs/qa/runs/e-soak",
            "docs/qa/runs/e-soak/story-evidence.json #X; complete",
            "docs/qa/runs/f-scale/story-evidence.json #PERF-04; old",
            "docs/qa/runs/g-scale/story-evidence.json #PERF-04; current",
        ]
    )

    compacted = tracker.compact_evidence(value)

    assert "pytest: old" not in compacted
    assert "pytest: current" in compacted
    assert "first=pass" not in compacted
    assert "second=pass" in compacted
    assert "third=pass" in compacted
    assert "broken=fail" in compacted
    assert "試験実行中" not in compacted
    assert "e-soak/story-evidence.json" in compacted
    assert "f-scale/story-evidence.json" not in compacted
    assert "g-scale/story-evidence.json" in compacted


@pytest.mark.parametrize(
    ("mode", "story", "evidence"),
    [
        (
            "pytest",
            "AUTO-01",
            "pytest: old-valid docs/qa/runs/old/story-evidence.json #AUTO-01\n"
            "pytest: newest-invalid docs/qa/runs/new/story-evidence.json #AUTO-01",
        ),
        (
            "browser",
            "BROWSER-01",
            "\n".join(
                [
                    "docs/qa/runs/old-valid/browser.json #BROWSER-01; required=pass",
                    "docs/qa/runs/new-invalid-1/browser.json #BROWSER-01; required=pass",
                    "docs/qa/runs/new-invalid-2/browser.json #BROWSER-01; required=pass",
                    "docs/qa/runs/new-invalid-3/browser.json #BROWSER-01; required=pass",
                ]
            ),
        ),
        (
            "performance",
            "PERF-X",
            "docs/qa/runs/old-valid-soak/story-evidence.json #PERF-X; pass\n"
            "docs/qa/runs/new-invalid-soak/story-evidence.json #PERF-X; pass",
        ),
        (
            "field",
            "FIELD-01",
            "field: docs/qa/runs/old-valid/field-story-evidence.json #FIELD-01; review=pass\n"
            "field: docs/qa/runs/new-invalid/field-story-evidence.json #FIELD-01; review=pass",
        ),
        (
            "composite-pytest",
            "COMP-01",
            "pytest: old-valid docs/qa/runs/old/story-evidence.json #COMP-01\n"
            "pytest: newest-invalid docs/qa/runs/new/story-evidence.json #COMP-01\n"
            "docs/qa/runs/browser-old-valid/browser.json #COMP-01; required=pass",
        ),
        (
            "composite-browser",
            "COMP-01",
            "\n".join(
                [
                    "pytest: old-valid docs/qa/runs/old/story-evidence.json #COMP-01",
                    "docs/qa/runs/browser-old-valid/browser.json #COMP-01; required=pass",
                    "docs/qa/runs/browser-new-invalid-1/browser.json #COMP-01; required=pass",
                    "docs/qa/runs/browser-new-invalid-2/browser.json #COMP-01; required=pass",
                    "docs/qa/runs/browser-new-invalid-3/browser.json #COMP-01; required=pass",
                ]
            ),
        ),
    ],
)
def test_check_never_compacts_away_the_only_current_pass_witness(
    mode, story, evidence, monkeypatch
) -> None:
    cells = {"A7": story, "F7": "合格", "M7": evidence}
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(tracker, "catalog", lambda: [{"ID": story}])
    monkeypatch.setattr(tracker, "read_cells", lambda: dict(cells))
    monkeypatch.setattr(
        tracker,
        "AUDITED_AUTOMATIC",
        {story} if mode == "pytest" else set(),
    )
    monkeypatch.setattr(
        tracker,
        "AUDITED_BROWSER",
        {story: {"required"}} if mode == "browser" else {},
    )
    monkeypatch.setattr(
        tracker,
        "AUDITED_COMPOSITE",
        (
            {story: {"pytest": {"case"}, "browser": {"required"}}}
            if mode.startswith("composite")
            else {}
        ),
    )
    monkeypatch.setattr(
        tracker,
        "AUDITED_PERFORMANCE",
        {story: "soak"} if mode == "performance" else {},
    )
    monkeypatch.setattr(
        tracker,
        "AUDITED_FIELD",
        {story} if mode == "field" else set(),
    )

    def pytest_current(_story, value, **_kwargs):
        return "pytest: old-valid" in value

    def browser_current(_story, value, **_kwargs):
        return "browser-old-valid" in value or (
            mode == "browser" and "old-valid/browser.json" in value
        )

    monkeypatch.setattr(tracker, "current_pytest_proof", pytest_current)
    monkeypatch.setattr(tracker, "current_browser_proof", browser_current)
    monkeypatch.setattr(
        tracker,
        "current_performance_proof",
        lambda _story, value, **_kwargs: "old-valid-soak" in value,
    )
    monkeypatch.setattr(
        tracker,
        "current_field_proof",
        lambda _story, value, **_kwargs: "old-valid/field" in value,
    )

    class StopAfterCompaction(Exception):
        pass

    def stop_on_formatting(operations):
        for operation in operations:
            if operation.get("path") == "/Stories/M7":
                cells["M7"] = operation["props"]["value"]
                return
        raise StopAfterCompaction

    monkeypatch.setattr(tracker, "batch", stop_on_formatting)
    monkeypatch.setattr(tracker, "office", lambda *_args: "")

    with pytest.raises(StopAfterCompaction):
        tracker.check()

    assert cells["M7"] == evidence
    dependencies = tracker._proof_dependencies_for_story(
        story, cells["M7"], row=7
    )
    assert tracker._proof_dependencies_are_current(dependencies)


def performance_manifest(kind: str, **overrides) -> dict:
    if kind == "soak":
        arguments = {
            "minutes": 90.0,
            "students": 10,
            "audio": None,
            "corpus": "ja_ext",
            "langs": ["en", "zh"],
            "disconnect_every_s": 60.0,
            "mt_cache_size": None,
            "engine": "hy-mt2",
            "asr": "sherpa",
            "partial": None,
            "port": 18431,
            "drain_seconds": 20.0,
            "out_dir": "run",
            "no_config_update": True,
        }
    else:
        arguments = {
            "minutes": 2.0,
            "students": [1, 10, 20, 40],
            "lang_counts": [1, 2, 3, 5],
            "corpus": "ja_ext",
            "engine": "hy-mt2",
            "port": 18432,
            "drain_seconds": 20.0,
            "out_dir": "run",
        }
    arguments.update(overrides)
    return {
        "kind": kind,
        "finished_at": "2026-09-07T02:00:00+09:00",
        "arguments": arguments,
    }


def manifest_sealed_for_report(manifest: dict, report) -> dict:
    sealed = json.loads(json.dumps(manifest))
    sealed["primary_report"] = report.name
    sealed["artifacts"] = {
        report.name: tracker.hashlib.sha256(report.read_bytes()).hexdigest()
    }
    return sealed


def qualifying_soak_result(**overrides) -> dict:
    by_student = {
        f"s{index}": {
            "lang": "en" if index % 2 == 0 else "zh",
            "received": 100,
            "published": 100,
            "recoverable": 50,
            "gaps": 0,
            "beyond_history": 0,
        }
        for index in range(10)
    }
    injection_students = [f"s{index % 10}" for index in range(89)]
    injected_counts = dict(tracker.Counter(injection_students))
    result = {
        "engine": "hy-mt2",
        "asr_engine": "sherpa",
        "minutes": 90.0,
        "students": 10,
        "measurement_students": 10,
        "measurement_langs": {"en": 5, "zh": 5},
        "measurement_seconds": 5400.0,
        "audio_bytes_planned": 172800000,
        "audio_bytes_sent": 172800000,
        "corpus": "ja_ext",
        "disconnect_every_s": 60.0,
        "mt_cache_size": None,
        "langs": ["en", "zh"],
        "latency": {"median_s": 0.2, "max_s": 1.0},
        "memory": {"peak_mb": 2500, "increasing": False},
        "drift": {
            "window_s": 300.0,
            "empty_windows": 0,
            "windows": [
                {
                    "index": index,
                    "start_s": float(index * 300),
                    "count": 10,
                    "median_s": 0.2 if index < 17 else 0.3,
                    "max_s": 1.0,
                }
                for index in range(18)
            ],
        },
        "resources": [
            {"name": "threads", "samples": 2700, "growing": False},
            {"name": "handles", "samples": 2700, "growing": False},
            {"name": "tasks", "samples": 2700, "growing": False},
        ],
        "resource_observation": {
            "sampler_interval_s": 2.0,
            "stats_interval_s": 2.0,
            "process_samples": 2700,
            "process_first_at_s": 1.0,
            "process_measurement_last_at_s": 5399.0,
            "process_last_at_s": 5419.0,
            "process_max_gap_s": 2.1,
            "rss_samples": 2700,
            "thread_samples": 2700,
            "handle_samples": 2700,
            "stats_samples": 2700,
            "stats_first_at_s": 2.0,
            "stats_measurement_last_at_s": 5400.0,
            "stats_last_at_s": 5418.0,
            "stats_max_gap_s": 2.1,
        },
        "dropped": {"segments": 0, "frames_rejected": 0, "notices": {}},
        "restore": {
            "students": 10,
            "gaps": 0,
            "injected_disconnects": len(injection_students),
            "injection_attempts": len(injection_students),
            "injection_close_failures": 0,
            "by_student": by_student,
            "injected_by_student": injected_counts,
            "restored_by_student": injected_counts,
            "injection_events": [
                {
                    "attempt": index + 1,
                    "student": sid,
                    "lang": by_student[sid]["lang"],
                    "close_succeeded": True,
                    "joined": True,
                    "restore_complete": True,
                    "requested_last_seq": 0,
                    "history_from": 1,
                    "seq_head": 1,
                    "expected_seqs": [1],
                    "beyond_history_seqs": [],
                    "published_head_at_close": 0,
                    "published_count_at_close": 0,
                    "received_head_at_close": 0,
                    "received_count_at_close": 0,
                    "attempted_at_s": float((index + 1) * 60),
                    "missing_seqs": [],
                    "restore_seconds": 1.0,
                }
                for index, sid in enumerate(injection_students)
            ],
            "continuous_delivery_by_student": {
                sid: {
                    "published": 100,
                    "received": 100,
                    "exempt_own_history": [],
                    "expected": 100,
                    "missing_seqs": [],
                    "gaps": 0,
                }
                for sid in by_student
            },
        },
        "disconnects": 0,
        "reconnect_failures": 0,
        "crashes": 0,
        "errors": [],
        "ran_seconds": 5420.0,
        "total_elapsed_seconds": 5420.0,
        "final_stats": {
            "at_s": 5418.0,
            "students": 10,
            "langs": {"en": 5, "zh": 5},
            "queue_depth": 0,
            "audio_queue_seconds": 0.0,
            "mt_queue_depth": 0,
        },
        "verdict": {"passed": True, "reasons": []},
    }
    result.update(overrides)
    return result


def test_current_performance_proof_reaudits_report_and_manifest(
    tmp_path, monkeypatch
) -> None:
    qa = tmp_path / "docs" / "qa"
    run = qa / "runs" / "fresh-soak"
    run.mkdir(parents=True)
    report = run / "2026-09-07-acceptance.json"
    report.write_text(
        json.dumps({"results": [qualifying_soak_result()]}), encoding="utf-8"
    )
    evidence_path = run / "story-evidence.json"
    evidence_path.write_text(
        json.dumps({
            "source_report": report.relative_to(tmp_path).as_posix(),
            "source_manifest": (run / tracker.PERFORMANCE_MANIFEST_NAME)
            .relative_to(tmp_path)
            .as_posix(),
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(
        tracker,
        "validate_performance_manifest",
        lambda *args, **kwargs: manifest_sealed_for_report(
            performance_manifest("soak"), report
        ),
    )
    reference = f"{evidence_path.relative_to(tmp_path).as_posix()} #PERF-03"

    assert tracker.current_performance_proof("PERF-03", reference)

    monkeypatch.setattr(
        tracker,
        "validate_performance_manifest",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            tracker.PerformanceEvidenceError("source snapshotが古い")
        ),
    )
    assert not tracker.current_performance_proof("PERF-03", reference)


def test_performance_report_bytes_are_rechecked_immediately_before_json_audit(
    tmp_path,
) -> None:
    report = tmp_path / "report.json"
    report.write_text('{"results": []}', encoding="utf-8")
    manifest = manifest_sealed_for_report(performance_manifest("soak"), report)

    assert tracker._load_sealed_performance_json(report, manifest) == {"results": []}

    report.write_text('{"results": ["replacement"]}', encoding="utf-8")
    with pytest.raises(tracker.PerformanceEvidenceError, match="監査直前"):
        tracker._load_sealed_performance_json(report, manifest)


def test_performance_run_guard_rejects_a_changed_in_process_auditor(
    tmp_path, monkeypatch
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    report = run / "report.json"
    report.write_bytes(b"{}")
    evidence = run / "story-evidence.json"
    evidence_bytes = b'{"source_report":"report.json"}'
    evidence.write_bytes(evidence_bytes)
    manifest = {"sealed": "state"}
    audited = [{"qualified": {"PERF-03": True}}]
    monkeypatch.setattr(
        tracker, "_performance_manifest", lambda *_args: manifest
    )
    monkeypatch.setattr(
        tracker, "_load_sealed_performance_json", lambda *_args: {}
    )
    monkeypatch.setattr(tracker, "_audit_soak_report", lambda *_args: audited)
    monkeypatch.setattr(
        tracker, "_performance_auditor_state", lambda: (("changed",),)
    )

    with pytest.raises(SystemExit, match="performance evidence changed"):
        tracker._require_current_performance_run(
            run=run,
            kind="soak",
            report=report,
            expected_manifest=manifest,
            expected_audit=audited,
            evidence_path=evidence,
            evidence_bytes=evidence_bytes,
            expected_auditor_state=(("original",),),
            checkpoint="workbook replace",
        )


def tracker_book(tmp_path, monkeypatch, cells):
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "read_cells", lambda: cells)
    monkeypatch.setattr(tracker, "check", lambda: None)
    operations = []
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    return book, operations


def test_soak_sync_rejects_stale_manifest_before_workbook_backup(tmp_path, monkeypatch) -> None:
    book, _ = tracker_book(tmp_path, monkeypatch, {})
    run = tmp_path / "stale-soak"
    run.mkdir()
    (run / "2026-09-07-acceptance.json").write_text(
        json.dumps({"results": [qualifying_soak_result()]}), encoding="utf-8"
    )
    monkeypatch.setattr(
        tracker,
        "validate_performance_manifest",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            tracker.PerformanceEvidenceError("source snapshotが古い")
        ),
    )

    with pytest.raises(SystemExit, match="現行状態で再実行"):
        tracker.sync_soak(run)

    assert book.exists()
    assert not (run / "feature-status-before-sync.xlsx").exists()


def test_fresh_full_soak_promotes_perf03_and_perf05_only(tmp_path, monkeypatch) -> None:
    cells = {
        "A90": "PERF-01", "F90": "部分確認", "M90": "",
        "A91": "PERF-02", "F91": "部分確認", "M91": "",
        "A92": "PERF-03", "F92": "不合格", "M92": "prior failed soak evidence",
        "A94": "PERF-05", "F94": "不合格", "M94": "prior failed soak evidence",
    }
    _, operations = tracker_book(tmp_path, monkeypatch, cells)
    run = tmp_path / "fresh-soak"
    run.mkdir()
    report = run / "2026-09-07-acceptance.json"
    report.write_text(
        json.dumps({"results": [qualifying_soak_result()]}), encoding="utf-8"
    )
    manifest = manifest_sealed_for_report(performance_manifest("soak"), report)
    monkeypatch.setattr(tracker, "validate_performance_manifest", lambda *args, **kwargs: manifest)

    tracker.sync_soak(run)

    assert tracker.setcell("F90", value="部分確認") in operations
    assert tracker.setcell("F91", value="部分確認") in operations
    assert tracker.setcell("F92", value="合格") in operations
    assert tracker.setcell("F94", value="合格") in operations
    perf01_note = next(
        operation["props"]["value"]
        for operation in operations
        if operation["path"] == "/Stories/M90"
    )
    perf02_note = next(
        operation["props"]["value"]
        for operation in operations
        if operation["path"] == "/Stories/M91"
    )
    assert "物理ブラウザ10台は未検証" in perf01_note
    assert "物理ブラウザはPERF-02条件外" in perf02_note
    assert "現地validator v2" in perf02_note
    assert "搭載RAM/OS可視RAMのraw bytes" in perf02_note
    assert "実ブラウザは未検証" not in perf02_note
    evidence = json.loads((run / "story-evidence.json").read_text(encoding="utf-8"))
    assert evidence["results"][0]["student_restore"]["complete_collection"] is True


def test_soak_cannot_promote_short_or_aggregate_only_restore() -> None:
    short_manifest = performance_manifest("soak", minutes=2.0)
    short = qualifying_soak_result(minutes=2.0, ran_seconds=120.0)
    audited = tracker._audit_soak_report({"results": [short]}, short_manifest)
    assert audited[0]["qualified"]["PERF-03"] is False

    aggregate_only = qualifying_soak_result()
    aggregate_only["restore"] = {
        "students": 10,
        "gaps": 0,
        "injected_disconnects": 90,
    }
    audited = tracker._audit_soak_report(
        {"results": [aggregate_only]}, performance_manifest("soak")
    )
    assert audited[0]["qualified"]["PERF-05"] is False

    sparse_cadence = qualifying_soak_result()
    sparse_events = sparse_cadence["restore"]["injection_events"][:10]
    sparse_counts = dict(tracker.Counter(event["student"] for event in sparse_events))
    sparse_cadence["restore"].update({
        "injection_events": sparse_events,
        "injection_attempts": len(sparse_events),
        "injected_disconnects": len(sparse_events),
        "injected_by_student": sparse_counts,
        "restored_by_student": sparse_counts,
    })
    audited = tracker._audit_soak_report(
        {"results": [sparse_cadence]}, performance_manifest("soak")
    )
    assert audited[0]["qualified"]["PERF-05"] is False


def test_soak_requires_a_real_asr_even_when_cli_did_not_override_it() -> None:
    fake_asr = qualifying_soak_result(asr_engine="fake")
    audited = tracker._audit_soak_report(
        {"results": [fake_asr]}, performance_manifest("soak", asr=None)
    )

    assert audited[0]["qualified"]["PERF-03"] is False
    assert audited[0]["qualified"]["PERF-05"] is False


def test_soak_requires_full_measurement_and_a_final_drift_window() -> None:
    early_stop = qualifying_soak_result(measurement_seconds=5399.9)
    audited = tracker._audit_soak_report(
        {"results": [early_stop]}, performance_manifest("soak")
    )
    assert audited[0]["qualified"]["PERF-03"] is False
    assert audited[0]["qualified"]["PERF-05"] is False

    silent_after_ten_minutes = qualifying_soak_result()
    silent_after_ten_minutes["drift"] = {
        "window_s": 300.0,
        "empty_windows": 16,
        "windows": [
            {"index": 0, "start_s": 0.0, "count": 10, "median_s": 0.2},
            {"index": 1, "start_s": 300.0, "count": 10, "median_s": 0.2},
        ],
    }
    audited = tracker._audit_soak_report(
        {"results": [silent_after_ten_minutes]}, performance_manifest("soak")
    )
    assert audited[0]["qualified"]["PERF-03"] is False


def test_soak_requires_continuous_drift_and_resource_sampling() -> None:
    empty_middle_window = qualifying_soak_result()
    empty_middle_window["drift"]["windows"].pop(7)
    empty_middle_window["drift"]["empty_windows"] = 1
    audited = tracker._audit_soak_report(
        {"results": [empty_middle_window]}, performance_manifest("soak")
    )
    assert audited[0]["qualified"]["PERF-03"] is False

    stale_end_snapshot = qualifying_soak_result()
    stale_end_snapshot["final_stats"]["at_s"] = 5400.0
    audited = tracker._audit_soak_report(
        {"results": [stale_end_snapshot]}, performance_manifest("soak")
    )
    assert audited[0]["qualified"]["PERF-03"] is False
    assert audited[0]["qualified"]["PERF-05"] is False

    empty_final_window = qualifying_soak_result()
    empty_final_window["drift"]["windows"][-1]["count"] = 0
    audited = tracker._audit_soak_report(
        {"results": [empty_final_window]}, performance_manifest("soak")
    )
    assert audited[0]["qualified"]["PERF-03"] is False

    long_sampler_outage = qualifying_soak_result()
    long_sampler_outage["resource_observation"]["process_max_gap_s"] = 4300.0
    audited = tracker._audit_soak_report(
        {"results": [long_sampler_outage]}, performance_manifest("soak")
    )
    assert audited[0]["qualified"]["PERF-03"] is False


def test_soak_rejects_incomplete_reconnect_or_unaffected_student_loss() -> None:
    incomplete = qualifying_soak_result()
    incomplete["restore"]["injection_events"][0]["restore_complete"] = False
    audited = tracker._audit_soak_report(
        {"results": [incomplete]}, performance_manifest("soak")
    )
    assert audited[0]["qualified"]["PERF-05"] is False

    old_loss = qualifying_soak_result()
    old_loss["restore"]["continuous_delivery_by_student"]["s1"].update({
        "gaps": 1,
        "missing_seqs": [1],
    })
    audited = tracker._audit_soak_report(
        {"results": [old_loss]}, performance_manifest("soak")
    )
    assert audited[0]["qualified"]["PERF-05"] is False

    understated_receipt = qualifying_soak_result()
    understated_receipt["restore"]["continuous_delivery_by_student"]["s1"][
        "received"
    ] = 99
    audited = tracker._audit_soak_report(
        {"results": [understated_receipt]}, performance_manifest("soak")
    )
    assert audited[0]["qualified"]["PERF-03"] is False
    assert audited[0]["qualified"]["PERF-05"] is False


def qualifying_scale_points() -> list[dict]:
    points = []
    for languages in (1, 2, 3, 5):
        for students in (1, 10, 20, 40):
            active = min(languages, students)
            langs = ["en", "zh", "pt", "vi", "ko"][:languages]
            observed_langs = dict(
                tracker.Counter(langs[index % languages] for index in range(students))
            )
            points.append({
                "langs": langs,
                "lang_count": languages,
                "active_langs": active,
                "students": students,
                "measurement_students": students,
                "measurement_langs": observed_langs,
                "measurement_seconds": 120.0,
                "audio_bytes_planned": 3840000,
                "audio_bytes_sent": 3840000,
                "ran_seconds": 140.0,
                "mt_engine": "hy-mt2" if languages <= 2 else "fake",
                "asr_engine": "sherpa",
                "asr_calls": 19,
                "mt_calls": 12 * active,
                "captions": 19 * active,
                "published_seqs": 19,
                "continuous_delivery_by_student": {
                    f"s{index}": {
                        "published": 19,
                        "received": 19,
                        "exempt_own_history": [],
                        "expected": 19,
                        "missing_seqs": [],
                        "gaps": 0,
                    }
                    for index in range(students)
                },
                "dropped_segments": 0,
                "frames_rejected": 0,
                "disconnects": 0,
                "reconnect_failures": 0,
                "crashes": 0,
                "errors": [],
                "counter_snapshot_at_s": 138.0,
                "counter_snapshot": {
                    "at_s": 138.0,
                    "students": students,
                    "langs": observed_langs,
                    "queue_depth": 0,
                    "audio_queue_seconds": 0.0,
                    "mt_queue_depth": 0,
                    "asr_calls": 19,
                    "mt_calls": 12 * active,
                    "audio_frames_rejected": 0,
                    "asr_dropped_segments": 0,
                },
            })
    return points


def test_fresh_complete_scale_matrix_promotes_perf04(tmp_path, monkeypatch) -> None:
    cells = {"A93": "PERF-04", "F93": "不合格", "M93": "prior failed scale evidence"}
    _, operations = tracker_book(tmp_path, monkeypatch, cells)
    run = tmp_path / "fresh-scale"
    run.mkdir()
    report = {
        "minutes": 2.0,
        "corpus": "ja_ext",
        "points": qualifying_scale_points(),
    }
    report_path = run / "2026-09-07-scaling.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    manifest = manifest_sealed_for_report(performance_manifest("scale"), report_path)
    monkeypatch.setattr(tracker, "validate_performance_manifest", lambda *args, **kwargs: manifest)

    tracker.sync_scale(run)

    assert tracker.setcell("F93", value="合格") in operations
    evidence = json.loads((run / "story-evidence.json").read_text(encoding="utf-8"))
    assert evidence["result"]["passed"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda point: point.update(measurement_seconds=119.999),
        lambda point: point.update(asr_engine="fake"),
        lambda point: point.update(counter_snapshot=None),
        lambda point: point["counter_snapshot"].update(queue_depth=1),
        lambda point: point.update(measurement_students=39),
        lambda point: (
            point.update(counter_snapshot_at_s=120.0),
            point["counter_snapshot"].update(at_s=120.0),
        ),
        lambda point: point["continuous_delivery_by_student"]["s0"].update(
            received=18, gaps=1, missing_seqs=[19]
        ),
    ],
    ids=[
        "short",
        "fake-asr",
        "no-final-stats",
        "queue-not-drained",
        "students-missing",
        "snapshot-before-drain",
        "caption-not-delivered",
    ],
)
def test_scale_audit_fails_closed_when_a_point_lacks_runtime_proof(mutate) -> None:
    points = qualifying_scale_points()
    mutate(points[-1])
    audited = tracker._audit_scale_report(
        {"minutes": 2.0, "corpus": "ja_ext", "points": points},
        performance_manifest("scale"),
    )

    assert audited["passed"] is False


def _field_validation(
    tmp_path, *, approved_candidates=None, field_complete=True
):
    field_dir = tmp_path / "docs" / "field" / "evidence" / "2026-09-08"
    field_dir.mkdir(parents=True)
    bundle = field_dir / "field-story-evidence.json"
    bundle.write_text('{"kind":"field-story-candidates"}\n', encoding="utf-8")
    manifest = tmp_path / "docs" / "field" / "2026-09-08-evidence-sha256.txt"
    manifest.write_text("sealed evidence manifest\n", encoding="utf-8")
    return {
        "field_complete": field_complete,
        "approved_candidates": list(
            tracker.FIELD_STORIES
            if approved_candidates is None
            else approved_candidates
        ),
        "bundle_path": bundle,
        "bundle_sha256": "a" * 64,
        "manifest_path": manifest,
        "manifest_sha256": "b" * 64,
        "payload": {"captured_at": "2026-09-08T12:34:56+09:00"},
    }


def _field_test_book(tmp_path, monkeypatch, *, failed_story=None):
    qa = tmp_path / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"field workbook before sync")
    cells = {}
    for number, story in enumerate(tracker.FIELD_STORIES, start=20):
        cells.update({
            f"A{number}": story,
            f"F{number}": "不合格" if story == failed_story else "部分確認",
            f"M{number}": f"prior evidence for {story}",
            f"N{number}": f"prior remaining for {story}",
        })
    operations = []
    office_calls = []
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "read_cells", lambda: cells)
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *args, **kwargs: office_calls.append(args) or "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    return qa, book, cells, operations, office_calls


def _write_field_receipt(tmp_path, validation):
    receipt_dir = tmp_path / "docs" / "qa" / "runs" / "reviewed-field-import"
    receipt_dir.mkdir(parents=True)
    receipt_path = receipt_dir / "field-story-evidence.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": tracker.FIELD_IMPORT_SCHEMA,
                "kind": "field-story-import",
                "imported_at": "2026-09-08T13:00:00+09:00",
                "reviewer": "QA reviewer",
                "review_confirmation": (
                    "reviewer inspected every referenced artifact before approval"
                ),
                "bundle": validation["bundle_path"].relative_to(tmp_path).as_posix(),
                "bundle_sha256": validation["bundle_sha256"],
                "manifest": validation["manifest_path"].relative_to(tmp_path).as_posix(),
                "manifest_sha256": validation["manifest_sha256"],
                "stories": {
                    story: {
                        "candidate": "可",
                        "review_decision": "合格",
                        "reason": "all referenced evidence was reviewed",
                    }
                    for story in validation["approved_candidates"]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return receipt_path


def test_compaction_keeps_only_the_latest_field_receipt_per_story() -> None:
    old_ops01 = (
        "field: docs/qa/runs/old-field-import/field-story-evidence.json #OPS-01; "
        "review=pass; bundle_sha256=old"
    )
    current_ops01 = (
        "field: docs/qa/runs/current-field-import/field-story-evidence.json #OPS-01; "
        "review=pass; bundle_sha256=current"
    )
    current_ops02 = (
        "field: docs/qa/runs/current-field-import/field-story-evidence.json #OPS-02; "
        "review=pass; bundle_sha256=current"
    )

    compacted = tracker.compact_evidence(
        "\n".join((old_ops01, "pytest: current", current_ops02, current_ops01))
    )

    assert old_ops01 not in compacted
    assert current_ops01 in compacted
    assert current_ops02 in compacted
    assert "pytest: current" in compacted


def test_sync_field_writes_review_receipt_backup_and_all_story_rows(
    tmp_path, monkeypatch
) -> None:
    qa, book, _, operations, office_calls = _field_test_book(tmp_path, monkeypatch)
    validation = _field_validation(tmp_path)
    validation_calls = []

    def validate(run, *, root):
        validation_calls.append((run, root))
        return dict(validation)

    monkeypatch.setattr(tracker, "validate_field_bundle", validate)

    receipt_dir = tracker.sync_field(
        validation["bundle_path"].parent,
        reviewer="  QA reviewer  ",
        approve_candidates=True,
    )

    assert validation_calls == [
        (validation["bundle_path"].parent, tmp_path),
        (validation["bundle_path"].parent, tmp_path),
        (validation["bundle_path"].parent, tmp_path),
        (validation["bundle_path"].parent, tmp_path),
    ]
    receipt_path = receipt_dir / "field-story-evidence.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["reviewer"] == "QA reviewer"
    assert tuple(receipt["stories"]) == tracker.FIELD_STORIES
    assert all(
        decision == {
            "candidate": "可",
            "review_decision": "合格",
            "reason": "候補条件と全参照証拠をレビューし、合格条件を満たすと承認",
        }
        for decision in receipt["stories"].values()
    )
    assert (receipt_dir / "feature-status-before-sync.xlsx").read_bytes() == book.read_bytes()
    assert len(operations) == len(tracker.FIELD_STORIES) * 4
    for number, story in enumerate(tracker.FIELD_STORIES, start=20):
        assert tracker.setcell(f"F{number}", value="合格") in operations
        evidence_operation = next(
            operation
            for operation in operations
            if operation["path"] == f"/Stories/M{number}"
        )
        assert f"{receipt_path.relative_to(tmp_path).as_posix()} #{story};" in (
            evidence_operation["props"]["value"]
        )
        assert tracker.setcell(
            f"O{number}", value=validation["payload"]["captured_at"]
        ) in operations
    assert ("save", book) in office_calls
    assert receipt_dir.parent == qa / "runs"


def test_field_import_schema_is_v2_for_subset_semantics() -> None:
    assert tracker.FIELD_IMPORT_SCHEMA == 2


def test_sync_field_imports_only_an_ordered_nonempty_candidate_subset(
    tmp_path, monkeypatch
) -> None:
    candidates = ("OPS-02", "PERF-02")
    qa, book, _, operations, office_calls = _field_test_book(
        tmp_path, monkeypatch
    )
    validation = _field_validation(
        tmp_path,
        approved_candidates=candidates,
        field_complete=False,
    )
    validation_calls = 0

    def validate(*_args, **_kwargs):
        nonlocal validation_calls
        validation_calls += 1
        return dict(validation)

    monkeypatch.setattr(tracker, "validate_field_bundle", validate)

    receipt_dir = tracker.sync_field(
        validation["bundle_path"].parent,
        reviewer="QA reviewer",
        approve_candidates=True,
    )

    receipt = json.loads(
        (receipt_dir / "field-story-evidence.json").read_text(encoding="utf-8")
    )
    assert receipt["schema_version"] == 2
    assert tuple(receipt["stories"]) == candidates
    assert validation_calls == 4
    expected_paths = {
        f"/Stories/{column}{20 + tracker.FIELD_STORIES.index(story)}"
        for story in candidates
        for column in "FMNO"
    }
    assert {operation["path"] for operation in operations} == expected_paths
    stu11_row = 20 + tracker.FIELD_STORIES.index("STU-11")
    assert all(
        operation["path"]
        not in {f"/Stories/{column}{stu11_row}" for column in "FMNO"}
        for operation in operations
    )
    assert ("save", book) in office_calls
    assert receipt_dir.parent == qa / "runs"


def test_sync_field_keeps_an_existing_failure_and_its_remaining_finding(
    tmp_path, monkeypatch
) -> None:
    _, _, _, operations, _ = _field_test_book(
        tmp_path, monkeypatch, failed_story="STU-11"
    )
    validation = _field_validation(tmp_path)
    monkeypatch.setattr(
        tracker, "validate_field_bundle", lambda *args, **kwargs: dict(validation)
    )

    tracker.sync_field(
        validation["bundle_path"].parent,
        reviewer="QA reviewer",
        approve_candidates=True,
    )

    number = 20 + tracker.FIELD_STORIES.index("STU-11")
    assert tracker.setcell(f"F{number}", value="不合格") in operations
    remaining = next(
        operation["props"]["value"]
        for operation in operations
        if operation["path"] == f"/Stories/N{number}"
    )
    assert "prior remaining for STU-11" in remaining
    assert "既存の不合格findingは自動解除しない" in remaining


def test_sync_field_rejects_invalid_bundle_before_workbook_access(
    tmp_path, monkeypatch
) -> None:
    qa = tmp_path / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(
        tracker,
        "validate_field_bundle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            tracker.FieldEvidenceError("manifest mismatch")
        ),
    )
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: pytest.fail("invalid field evidence must not access the workbook"),
    )
    monkeypatch.setattr(
        tracker,
        "batch",
        lambda *_: pytest.fail("invalid field evidence must not write the workbook"),
    )

    with pytest.raises(SystemExit, match="台帳を変更しません"):
        tracker.sync_field(
            tmp_path / "invalid-field-bundle",
            reviewer="QA reviewer",
            approve_candidates=True,
        )

    assert list((qa / "runs").iterdir()) == []


@pytest.mark.parametrize(
    "candidates",
    [
        [],
        ["UNKNOWN"],
        ["OPS-02", "OPS-01"],
        ["OPS-01", "OPS-01"],
    ],
    ids=["empty", "unknown", "out-of-order", "duplicate"],
)
def test_sync_field_rejects_an_invalid_candidate_subset_before_workbook_access(
    candidates, tmp_path, monkeypatch
) -> None:
    validation = _field_validation(
        tmp_path,
        approved_candidates=candidates,
        field_complete=False,
    )
    qa = tmp_path / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(
        tracker,
        "validate_field_bundle",
        lambda *_args, **_kwargs: dict(validation),
    )
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: pytest.fail("invalid candidates must not access the workbook"),
    )

    with pytest.raises(SystemExit, match="台帳を変更しません"):
        tracker.sync_field(
            validation["bundle_path"].parent,
            reviewer="QA reviewer",
            approve_candidates=True,
        )

    assert list((qa / "runs").iterdir()) == []


def test_sync_field_rejects_candidates_changed_by_the_second_validation(
    tmp_path, monkeypatch
) -> None:
    qa, _, _, operations, office_calls = _field_test_book(tmp_path, monkeypatch)
    validation = _field_validation(tmp_path)
    validation_calls = 0

    def validate(*_args, **_kwargs):
        nonlocal validation_calls
        validation_calls += 1
        result = dict(validation)
        if validation_calls >= 2:
            result["approved_candidates"] = list(tracker.FIELD_STORIES[:-1])
        return result

    monkeypatch.setattr(tracker, "validate_field_bundle", validate)

    with pytest.raises(SystemExit, match="レビュー中に変化"):
        tracker.sync_field(
            validation["bundle_path"].parent,
            reviewer="QA reviewer",
            approve_candidates=True,
        )

    assert validation_calls == 2
    assert operations == []
    assert office_calls == []
    assert list((qa / "runs").iterdir()) == []


def test_sync_field_rejects_a_bundle_changed_by_the_second_validation_before_write(
    tmp_path, monkeypatch
) -> None:
    qa, _, _, operations, office_calls = _field_test_book(tmp_path, monkeypatch)
    validation = _field_validation(tmp_path)
    validation_calls = 0

    def validate(*args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        result = dict(validation)
        if validation_calls == 2:
            result["bundle_sha256"] = "c" * 64
        return result

    monkeypatch.setattr(tracker, "validate_field_bundle", validate)

    with pytest.raises(SystemExit, match="レビュー中に変化"):
        tracker.sync_field(
            validation["bundle_path"].parent,
            reviewer="QA reviewer",
            approve_candidates=True,
        )

    assert validation_calls == 2
    assert operations == []
    assert office_calls == []
    assert list((qa / "runs").iterdir()) == []


def test_sync_field_reaudits_new_receipt_and_bundle_immediately_before_batch(
    tmp_path, monkeypatch
) -> None:
    _, book, _, operations, office_calls = _field_test_book(tmp_path, monkeypatch)
    validation = _field_validation(tmp_path)
    original_bundle = validation["bundle_path"].read_bytes()
    validation_calls = 0

    def validate(*args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        if validation["bundle_path"].read_bytes() != original_bundle:
            raise tracker.FieldEvidenceError("bundle changed after receipt creation")
        return dict(validation)

    original_backup = tracker._exclusive_workbook_backup

    def backup_then_mutate_bundle(destination):
        result = original_backup(destination)
        validation["bundle_path"].write_text(
            '{"kind":"changed-after-second-validation"}\n', encoding="utf-8"
        )
        return result

    monkeypatch.setattr(tracker, "validate_field_bundle", validate)
    monkeypatch.setattr(
        tracker, "_exclusive_workbook_backup", backup_then_mutate_bundle
    )

    with pytest.raises(SystemExit, match="changed before workbook replace"):
        tracker.sync_field(
            validation["bundle_path"].parent,
            reviewer="QA reviewer",
            approve_candidates=True,
        )

    assert validation_calls == 4
    assert operations == []
    assert office_calls == []


def test_sync_field_requires_explicit_review_before_validation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        tracker,
        "validate_field_bundle",
        lambda *args, **kwargs: pytest.fail("unapproved candidates must not be validated"),
    )

    with pytest.raises(SystemExit, match="--approve-field-candidates"):
        tracker.sync_field(
            tmp_path / "field-bundle", reviewer="QA reviewer", approve_candidates=False
        )


def test_current_field_proof_accepts_an_exact_subset_without_field_complete(
    tmp_path, monkeypatch
) -> None:
    candidates = ("OPS-01", "PERF-02")
    validation = _field_validation(
        tmp_path,
        approved_candidates=candidates,
        field_complete=False,
    )
    receipt_path = _write_field_receipt(tmp_path, validation)
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", tmp_path / "docs" / "qa")
    validation_calls = 0

    def validate(*_args, **_kwargs):
        nonlocal validation_calls
        validation_calls += 1
        return dict(validation)

    monkeypatch.setattr(tracker, "validate_field_bundle", validate)
    reference = receipt_path.relative_to(tmp_path).as_posix()
    cache = {}
    candidate_evidence = (
        f"field: {reference} #OPS-01; review=pass; bundle_sha256=sealed"
    )
    noncandidate_evidence = (
        f"field: {reference} #STU-11; review=pass; bundle_sha256=sealed"
    )

    assert tracker.current_field_proof(
        "OPS-01", candidate_evidence, cache=cache
    )
    assert not tracker.current_field_proof(
        "STU-11", noncandidate_evidence, cache=cache
    )
    assert validation_calls == 1


@pytest.mark.parametrize(
    "candidates",
    [
        [],
        ["UNKNOWN"],
        ["OPS-02", "OPS-01"],
        ["OPS-01", "OPS-01"],
    ],
    ids=["empty", "unknown", "out-of-order", "duplicate"],
)
def test_field_receipt_rejects_invalid_validator_candidate_sequences(
    candidates, tmp_path, monkeypatch
) -> None:
    validation = _field_validation(
        tmp_path,
        approved_candidates=candidates,
        field_complete=False,
    )
    receipt_path = _write_field_receipt(tmp_path, validation)
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", tmp_path / "docs" / "qa")
    monkeypatch.setattr(
        tracker,
        "validate_field_bundle",
        lambda *_args, **_kwargs: dict(validation),
    )

    assert (
        tracker._approved_field_stories_from_receipt(
            receipt_path.relative_to(tmp_path).as_posix()
        )
        == set()
    )


def test_field_receipt_rejects_stories_that_are_not_the_exact_candidate_subset(
    tmp_path, monkeypatch
) -> None:
    validation = _field_validation(
        tmp_path,
        approved_candidates=("OPS-01", "PERF-02"),
        field_complete=False,
    )
    receipt_path = _write_field_receipt(tmp_path, validation)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["stories"].pop("PERF-02")
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", tmp_path / "docs" / "qa")
    monkeypatch.setattr(
        tracker,
        "validate_field_bundle",
        lambda *_args, **_kwargs: dict(validation),
    )

    assert (
        tracker._approved_field_stories_from_receipt(
            receipt_path.relative_to(tmp_path).as_posix()
        )
        == set()
    )


def test_current_field_proof_reaudits_one_receipt_once_per_sync_cache(
    tmp_path, monkeypatch
) -> None:
    validation = _field_validation(tmp_path)
    receipt_path = _write_field_receipt(tmp_path, validation)
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", tmp_path / "docs" / "qa")
    validation_calls = []

    def validate(run, *, root):
        validation_calls.append((run, root))
        return dict(validation)

    monkeypatch.setattr(tracker, "validate_field_bundle", validate)
    ops01_evidence = (
        f"field: {receipt_path.relative_to(tmp_path).as_posix()} #OPS-01; "
        "review=pass; bundle_sha256=sealed"
    )
    perf02_evidence = (
        f"field: {receipt_path.relative_to(tmp_path).as_posix()} #PERF-02; "
        "review=pass; bundle_sha256=sealed"
    )
    cache = {}

    assert tracker.current_field_proof("OPS-01", ops01_evidence, cache=cache)
    assert tracker.current_field_proof("PERF-02", perf02_evidence, cache=cache)
    assert not tracker.current_field_proof("PERF-02", ops01_evidence, cache=cache)
    assert not tracker.current_field_proof(
        "OPS-01", "revoked-" + ops01_evidence, cache=cache
    )
    assert not tracker.current_field_proof("TEACH-01", ops01_evidence, cache=cache)
    assert validation_calls == [(validation["bundle_path"].parent, tmp_path)]


def test_current_field_proof_rejects_receipt_changed_during_reaudit(
    tmp_path, monkeypatch
) -> None:
    validation = _field_validation(tmp_path)
    receipt_path = _write_field_receipt(tmp_path, validation)
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", tmp_path / "docs" / "qa")
    monkeypatch.setattr(
        tracker, "validate_field_bundle", lambda *args, **kwargs: dict(validation)
    )
    real_read = tracker._read_bound_repository_file
    receipt_reads = 0

    def read_then_mutate(path, *, root):
        nonlocal receipt_reads
        result = real_read(path, root=root)
        if Path(path) == receipt_path:
            receipt_reads += 1
            # The first read is the lexical-reference preflight.  Mutate only
            # after the exact bytes used for strict JSON validation are bound.
            if receipt_reads == 2:
                receipt_path.write_bytes(result[0] + b" ")
        return result

    monkeypatch.setattr(
        tracker, "_read_bound_repository_file", read_then_mutate
    )
    evidence = (
        f"field: {receipt_path.relative_to(tmp_path).as_posix()} #OPS-01; "
        "review=pass; bundle_sha256=sealed"
    )

    assert not tracker.current_field_proof("OPS-01", evidence)


def test_field_proof_cache_reaudits_receipt_bytes_before_workbook_write(
    tmp_path, monkeypatch
) -> None:
    validation = _field_validation(tmp_path)
    receipt_path = _write_field_receipt(tmp_path, validation)
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", tmp_path / "docs" / "qa")
    monkeypatch.setattr(
        tracker, "validate_field_bundle", lambda *args, **kwargs: dict(validation)
    )
    evidence = (
        f"field: {receipt_path.relative_to(tmp_path).as_posix()} #OPS-01; "
        "review=pass; bundle_sha256=sealed"
    )
    cache = {}

    assert tracker.current_field_proof("OPS-01", evidence, cache=cache)
    receipt_path.write_text(
        receipt_path.read_text(encoding="utf-8") + " ", encoding="utf-8"
    )

    assert not tracker._field_proof_cache_is_current(cache)


def test_all_field_pass_reaudit_downgrades_an_untouched_stale_story(
    monkeypatch,
) -> None:
    cells = {
        "A15": "OPS-12",
        "F15": "合格",
        "M15": "stale field receipt",
        "N15": "prior remaining",
    }
    operations = []
    monkeypatch.setattr(
        tracker, "current_field_proof", lambda *_args, **_kwargs: False
    )

    downgraded = tracker._reaudit_all_field_passes(cells, operations, {})

    assert downgraded == ["OPS-12"]
    assert tracker.setcell("F15", value="部分確認") in operations
    remaining = next(
        operation["props"]["value"]
        for operation in operations
        if operation["path"] == "/Stories/N15"
    )
    assert "prior remaining" in remaining
    assert "現地証拠を現行source/manifestで再監査できない" in remaining


def test_all_field_pass_reaudit_preserves_current_proof_over_a_partial_supplement(
    monkeypatch,
) -> None:
    cells = {"A15": "OPS-12", "F15": "合格", "M15": "current field receipt"}
    operations = [tracker.setcell("F15", value="部分確認")]
    monkeypatch.setattr(
        tracker, "current_field_proof", lambda *_args, **_kwargs: True
    )

    assert tracker._reaudit_all_field_passes(cells, operations, {}) == []
    assert tracker._planned_status(operations, 15) == "合格"


def test_all_field_pass_reaudit_never_overrides_a_new_explicit_failure(
    monkeypatch,
) -> None:
    cells = {"A15": "OPS-12", "F15": "合格", "M15": "field receipt"}
    operations = [tracker.setcell("F15", value="不合格")]
    monkeypatch.setattr(
        tracker,
        "current_field_proof",
        lambda *_args, **_kwargs: pytest.fail("an explicit failure must win"),
    )

    assert tracker._reaudit_all_field_passes(cells, operations, {}) == []
    assert tracker._planned_status(operations, 15) == "不合格"


@pytest.mark.parametrize(
    ("story", "mode"),
    [
        ("OPS-AUTO", "pytest"),
        ("OPS-BROWSER", "browser"),
        ("OPS-COMPOSITE", "composite"),
        ("PERF-STORY", "performance"),
    ],
)
def test_nonfield_pass_reaudit_downgrades_an_untouched_stale_pass(
    story, mode, monkeypatch
) -> None:
    cells = {
        "A15": story,
        "F15": "合格",
        "M15": "stale immutable proof",
        "N15": "prior finding",
    }
    operations = []
    dependencies = set()
    monkeypatch.setattr(
        tracker, "AUDITED_AUTOMATIC", {story} if mode == "pytest" else set()
    )
    monkeypatch.setattr(
        tracker,
        "AUDITED_BROWSER",
        {story: {"scenario"}} if mode == "browser" else {},
    )
    monkeypatch.setattr(
        tracker,
        "AUDITED_COMPOSITE",
        (
            {story: {"pytest": {"case"}, "browser": {"scenario"}}}
            if mode == "composite"
            else {}
        ),
    )
    monkeypatch.setattr(
        tracker,
        "AUDITED_PERFORMANCE",
        {story: "soak"} if mode == "performance" else {},
    )
    monkeypatch.setattr(
        tracker, "current_pytest_proof", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        tracker, "current_browser_proof", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        tracker, "current_performance_proof", lambda *_args, **_kwargs: False
    )

    downgraded = tracker._reaudit_all_nonfield_passes(
        cells, operations, tracker.AuditProofCache(), dependencies
    )

    assert downgraded == [story]
    assert tracker.setcell("F15", value="部分確認") in operations
    assert dependencies == set()


def test_nonfield_pass_reaudit_records_both_current_composite_dependencies(
    monkeypatch,
) -> None:
    story = "OPS-COMPOSITE"
    cells = {"A15": story, "F15": "合格", "M15": "current composite proof"}
    operations = []
    dependencies = set()
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", set())
    monkeypatch.setattr(tracker, "AUDITED_BROWSER", {})
    monkeypatch.setattr(
        tracker,
        "AUDITED_COMPOSITE",
        {story: {"pytest": {"case"}, "browser": {"scenario"}}},
    )
    monkeypatch.setattr(tracker, "AUDITED_PERFORMANCE", {})
    monkeypatch.setattr(
        tracker, "current_pytest_proof", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        tracker, "current_browser_proof", lambda *_args, **_kwargs: True
    )

    downgraded = tracker._reaudit_all_nonfield_passes(
        cells, operations, tracker.AuditProofCache(), dependencies
    )

    assert downgraded == []
    assert operations == []
    assert {dependency.kind for dependency in dependencies} == {"pytest", "browser"}


@pytest.mark.parametrize("sync_kind", ["pytest", "browser"])
def test_partial_sync_reaudits_an_unselected_stale_nonfield_pass(
    sync_kind, tmp_path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook")
    current_story = "RUN-STORY"
    stale_story = "STALE-STORY"
    cells = {
        "A15": current_story,
        "F15": "部分確認",
        "M15": "",
        "A16": stale_story,
        "F16": "合格",
        "M16": "stale prior proof",
        "N16": "",
    }
    operations = []
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", {stale_story})
    monkeypatch.setattr(tracker, "AUDITED_BROWSER", {})
    monkeypatch.setattr(tracker, "AUDITED_COMPOSITE", {})
    monkeypatch.setattr(tracker, "AUDITED_PERFORMANCE", {})
    monkeypatch.setattr(tracker, "AUDITED_FIELD", set())
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_args: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(tracker, "read_cells", lambda: dict(cells))
    monkeypatch.setattr(
        tracker, "current_pytest_proof", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_args: "")
    monkeypatch.setattr(tracker, "check", lambda: None)

    if sync_kind == "pytest":
        selector = "tests/test_current.py::test_current"
        stale_selector = "tests/test_stale.py::test_stale"
        monkeypatch.setattr(tracker, "pytest_source_files", lambda: [])
        monkeypatch.setattr(
            tracker,
            "catalog",
            lambda: [
                {
                    "ID": current_story,
                    "自動テスト候補": selector,
                    "検証方式": "自動",
                    "残作業・注意": "",
                },
                {
                    "ID": stale_story,
                    "自動テスト候補": stale_selector,
                    "検証方式": "自動",
                    "残作業・注意": "",
                },
            ],
        )
        monkeypatch.setattr(
            tracker,
            "junit_cases",
            lambda _source: [
                {"nodeid": selector, "status": "pass", "detail": ""}
            ],
        )
        run = qa / "runs" / "partial-pytest"
        run.mkdir()
        (run / "pytest.xml").write_text("<testsuite/>", encoding="utf-8")
        (run / "run.json").write_text(
            json.dumps({"finished_at": "2026-09-09T01:00:00+09:00"}),
            encoding="utf-8",
        )
        _seal_existing_pytest_run(run)
        tracker.sync(run)
    else:
        monkeypatch.setattr(tracker, "browser_source_files", lambda *, native: [])
        run = qa / "runs" / "partial-browser"
        run.mkdir()
        (run / "browser.json").write_text(
            json.dumps(
                {
                    "finished_at": "2026-09-09T01:00:00+09:00",
                    "results": [
                        {
                            "story": current_story,
                            "scenario": "supplemental",
                            "status": "pass",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        _seal_existing_browser_run(run)
        tracker.sync_browser(run)

    assert tracker.setcell("F16", value="部分確認") in operations


def test_pytest_sync_reaudits_selectorless_field_passes(
    tmp_path, monkeypatch
) -> None:
    qa = tmp_path / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    run = qa / "runs" / "selectorless-field"
    run.mkdir()
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    (run / "pytest.xml").write_text("<testsuite/>", encoding="utf-8")
    (run / "run.json").write_text(
        json.dumps({"finished_at": "2026-09-08T14:00:00+09:00"}),
        encoding="utf-8",
    )
    _seal_existing_pytest_run(run)
    cells = {
        "A15": "OPS-12",
        "F15": "合格",
        "M15": "stale field receipt",
        "N15": "school devices must be rerun",
    }
    operations = []
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: [])
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_args: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(
        tracker,
        "catalog",
        lambda: [
            {
                "ID": "OPS-12",
                "自動テスト候補": "",
                "検証方式": "実機",
                "残作業・注意": "",
            }
        ],
    )
    monkeypatch.setattr(tracker, "read_cells", lambda: cells)
    monkeypatch.setattr(
        tracker, "current_field_proof", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_args: "")
    monkeypatch.setattr(tracker, "check", lambda: None)

    tracker.sync(run)

    assert tracker.setcell("F15", value="部分確認") in operations
    assert json.loads((run / "story-evidence.json").read_text(encoding="utf-8")) == {}


def _configure_field_pytest_sync(tmp_path, monkeypatch, *, case_status):
    story = "OPS-01"
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: [])
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(
        tracker,
        "catalog",
        lambda: [{
            "ID": story,
            "自動テスト候補": "field-support-case",
            "検証方式": "実機",
            "残作業・注意": "",
        }],
    )
    monkeypatch.setattr(
        tracker,
        "junit_cases",
        lambda _: [{"nodeid": "field-support-case", "status": case_status, "detail": ""}],
    )
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: {
            "A15": story,
            "F15": "合格",
            "M15": "field: docs/qa/runs/field/field-story-evidence.json #OPS-01; review=pass",
        },
    )
    operations = []
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    run = tmp_path / f"pytest-{case_status}"
    run.mkdir()
    (run / "pytest.xml").write_text("<testsuite/>", encoding="utf-8")
    (run / "run.json").write_text(
        json.dumps({"finished_at": "2026-09-08T14:00:00+09:00"}), encoding="utf-8"
    )
    _seal_existing_pytest_run(run)
    return story, run, operations


@pytest.mark.parametrize("case_status", ["pass", "skip"])
def test_pytest_sync_keeps_current_field_pass_for_green_or_skipped_support(
    case_status, tmp_path, monkeypatch
) -> None:
    story, run, operations = _configure_field_pytest_sync(
        tmp_path, monkeypatch, case_status=case_status
    )
    monkeypatch.setattr(
        tracker,
        "current_field_proof",
        lambda candidate, *_args, **_kwargs: candidate == story,
    )

    tracker.sync(run)

    assert operations == []
    evidence = json.loads((run / "story-evidence.json").read_text(encoding="utf-8"))
    assert evidence[story]["status"] == "合格"
    assert evidence[story]["workbook_preserved"] is True


def test_pytest_sync_explicit_failure_overrides_current_field_pass(
    tmp_path, monkeypatch
) -> None:
    story, run, operations = _configure_field_pytest_sync(
        tmp_path, monkeypatch, case_status="fail"
    )
    monkeypatch.setattr(
        tracker,
        "current_field_proof",
        lambda *_args, **_kwargs: pytest.fail("a failing run must win before field proof"),
    )

    tracker.sync(run)

    assert tracker.setcell("F15", value="不合格") in operations
    evidence = json.loads((run / "story-evidence.json").read_text(encoding="utf-8"))
    assert evidence[story]["status"] == "不合格"
    assert evidence[story]["workbook_preserved"] is False


def test_pytest_sync_revalidates_the_incomplete_seal_before_completion(
    tmp_path, monkeypatch
) -> None:
    story, run, operations = _configure_field_pytest_sync(
        tmp_path, monkeypatch, case_status="pass"
    )
    monkeypatch.setattr(
        tracker,
        "current_field_proof",
        lambda candidate, *_args, **_kwargs: candidate == story,
    )
    real_load = tracker.load_sealed_pytest_evidence
    calls = 0

    def load_then_tamper(run_dir, *, expected_complete):
        nonlocal calls
        calls += 1
        if calls == 2:
            (run / "pytest.xml").write_bytes(
                (run / "pytest.xml").read_bytes() + b"tampered"
            )
        return real_load(run_dir, expected_complete=expected_complete)

    monkeypatch.setattr(tracker, "load_sealed_pytest_evidence", load_then_tamper)

    with pytest.raises(SystemExit, match="同期中に変化"):
        tracker.sync(run)

    assert operations == []
    assert not (run / "story-evidence.json").exists()
    assert not (run / "feature-status-before-sync.xlsx").exists()


def test_pytest_sync_revalidates_the_complete_seal_immediately_before_batch(
    tmp_path, monkeypatch
) -> None:
    story, run, operations = _configure_field_pytest_sync(
        tmp_path, monkeypatch, case_status="pass"
    )
    monkeypatch.setattr(
        tracker,
        "current_field_proof",
        lambda candidate, *_args, **_kwargs: candidate == story,
    )
    real_load = tracker.load_sealed_pytest_evidence
    calls = 0

    def load_then_tamper(run_dir, *, expected_complete):
        nonlocal calls
        calls += 1
        if calls == 4:
            story_path = run / "story-evidence.json"
            story_path.write_bytes(story_path.read_bytes() + b"tampered")
        return real_load(run_dir, expected_complete=expected_complete)

    monkeypatch.setattr(tracker, "load_sealed_pytest_evidence", load_then_tamper)

    with pytest.raises(SystemExit, match="batch直前"):
        tracker.sync(run)

    assert operations == []
    assert (run / "feature-status-before-sync.xlsx").is_file()


def _configure_field_browser_sync(tmp_path, monkeypatch, *, result_status):
    story = "OPS-10"
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "browser_source_files", lambda *, native: [])
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: {
            "A15": story,
            "F15": "合格",
            "M15": "field: docs/qa/runs/field/field-story-evidence.json #OPS-10; review=pass",
        },
    )
    monkeypatch.setattr(tracker, "current_browser_proof", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tracker, "current_pytest_proof", lambda *_args, **_kwargs: False)
    operations = []
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "office", lambda *_: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    run = tmp_path / f"browser-{result_status}"
    run.mkdir()
    (run / "source-hashes.json").write_text("{}", encoding="utf-8")
    (run / "browser.json").write_text(
        json.dumps({
            "finished_at": "2026-09-08T14:30:00+09:00",
            "results": [
                {"story": story, "scenario": "supplemental", "status": result_status}
            ],
        }),
        encoding="utf-8",
    )
    _seal_existing_browser_run(run)
    return story, run, operations


def test_browser_sync_keeps_current_field_pass_for_green_supplement(
    tmp_path, monkeypatch
) -> None:
    story, run, operations = _configure_field_browser_sync(
        tmp_path, monkeypatch, result_status="pass"
    )
    monkeypatch.setattr(
        tracker,
        "current_field_proof",
        lambda candidate, *_args, **_kwargs: candidate == story,
    )

    tracker.sync_browser(run)

    assert tracker.setcell("F15", value="合格") in operations
    evidence = next(
        operation["props"]["value"]
        for operation in operations
        if operation["path"] == "/Stories/M15"
    )
    assert "/field-story-evidence.json #OPS-10;" in evidence


def test_browser_sync_explicit_failure_overrides_current_field_pass(
    tmp_path, monkeypatch
) -> None:
    _, run, operations = _configure_field_browser_sync(
        tmp_path, monkeypatch, result_status="fail"
    )
    monkeypatch.setattr(
        tracker,
        "current_field_proof",
        lambda *_args, **_kwargs: pytest.fail("a failing browser run must win"),
    )

    tracker.sync_browser(run)

    assert tracker.setcell("F15", value="不合格") in operations


def test_browser_sync_revalidates_the_seal_before_creating_a_backup(
    tmp_path, monkeypatch
) -> None:
    _, run, operations = _configure_field_browser_sync(
        tmp_path, monkeypatch, result_status="pass"
    )
    monkeypatch.setattr(tracker, "current_field_proof", lambda *_args, **_kwargs: True)
    real_load = tracker.load_sealed_browser_evidence
    calls = 0

    def load_then_tamper(run_dir):
        nonlocal calls
        calls += 1
        if calls == 2:
            (run / "browser.json").write_bytes(
                (run / "browser.json").read_bytes() + b"tampered"
            )
        return real_load(run_dir)

    monkeypatch.setattr(tracker, "load_sealed_browser_evidence", load_then_tamper)

    with pytest.raises(SystemExit, match="同期中に変化"):
        tracker.sync_browser(run)

    assert operations == []
    assert not (run / "feature-status-before-sync.xlsx").exists()


def test_browser_sync_revalidates_the_seal_immediately_before_batch(
    tmp_path, monkeypatch
) -> None:
    _, run, operations = _configure_field_browser_sync(
        tmp_path, monkeypatch, result_status="pass"
    )
    monkeypatch.setattr(tracker, "current_field_proof", lambda *_args, **_kwargs: True)
    real_load = tracker.load_sealed_browser_evidence
    calls = 0

    def load_then_tamper(run_dir):
        nonlocal calls
        calls += 1
        if calls == 3:
            (run / "browser.json").write_bytes(
                (run / "browser.json").read_bytes() + b"tampered"
            )
        return real_load(run_dir)

    monkeypatch.setattr(tracker, "load_sealed_browser_evidence", load_then_tamper)

    with pytest.raises(SystemExit, match="batch直前"):
        tracker.sync_browser(run)

    assert operations == []
    assert (run / "feature-status-before-sync.xlsx").is_file()


def _configure_nonfield_pytest_dependency_sync(
    tmp_path,
    monkeypatch,
    *,
    story: str,
    existing_status: str,
    case_status: str,
    existing_evidence: str,
):
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "pytest_source_files", lambda: [])
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", set())
    monkeypatch.setattr(tracker, "AUDITED_BROWSER", {})
    monkeypatch.setattr(tracker, "AUDITED_COMPOSITE", {})
    monkeypatch.setattr(tracker, "AUDITED_PERFORMANCE", {})
    monkeypatch.setattr(tracker, "AUDITED_FIELD", set())
    selector = "tests/test_dependency.py::test_dependency"
    monkeypatch.setattr(
        tracker,
        "catalog",
        lambda: [
            {
                "ID": story,
                tracker.HEADERS[11]: selector,
                tracker.HEADERS[6]: "automatic",
                tracker.HEADERS[13]: "",
            }
        ],
    )
    monkeypatch.setattr(
        tracker,
        "junit_cases",
        lambda _: [
            {"nodeid": selector, "status": case_status, "detail": ""}
        ],
    )
    cells = {
        "A15": story,
        "F15": existing_status,
        "M15": existing_evidence,
    }
    monkeypatch.setattr(tracker, "read_cells", lambda: dict(cells))
    batch_calls = []
    monkeypatch.setattr(
        tracker, "batch", lambda ops: batch_calls.append(list(ops))
    )
    monkeypatch.setattr(tracker, "office", lambda *_args: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    run = tmp_path / "current-pytest"
    run.mkdir()
    (run / "pytest.xml").write_text("<testsuite/>", encoding="utf-8")
    (run / "run.json").write_text(
        json.dumps({"finished_at": "2026-09-08T14:00:00+09:00"}),
        encoding="utf-8",
    )
    _seal_existing_pytest_run(run)
    return run, batch_calls


def _configure_nonfield_browser_dependency_sync(
    tmp_path,
    monkeypatch,
    *,
    story: str,
    existing_status: str,
    existing_evidence: str,
):
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "browser_source_files", lambda *, native: [])
    monkeypatch.setattr(tracker, "snapshot_supplied_source_hashes", lambda *_: {})
    monkeypatch.setattr(tracker, "validate_audit_configuration", lambda: None)
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", set())
    monkeypatch.setattr(tracker, "AUDITED_BROWSER", {})
    monkeypatch.setattr(tracker, "AUDITED_COMPOSITE", {})
    monkeypatch.setattr(tracker, "AUDITED_PERFORMANCE", {})
    monkeypatch.setattr(tracker, "AUDITED_FIELD", set())
    cells = {
        "A15": story,
        "F15": existing_status,
        "M15": existing_evidence,
    }
    monkeypatch.setattr(tracker, "read_cells", lambda: dict(cells))
    batch_calls = []
    monkeypatch.setattr(
        tracker, "batch", lambda ops: batch_calls.append(list(ops))
    )
    monkeypatch.setattr(tracker, "office", lambda *_args: "")
    monkeypatch.setattr(tracker, "check", lambda: None)
    run = tmp_path / "current-browser"
    run.mkdir()
    (run / "browser.json").write_text(
        json.dumps(
            {
                "finished_at": "2026-09-08T14:30:00+09:00",
                "results": [
                    {
                        "story": story,
                        "scenario": "supplemental",
                        "status": "pass",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _seal_existing_browser_run(run)
    reference = (run / "browser.json").relative_to(tmp_path).as_posix()
    return run, reference, batch_calls


def _existing_proof_marker(tmp_path: Path, kind: str) -> Path:
    marker = tmp_path / "existing-run" / kind / "source-hashes.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"sealed")
    return marker


def _invalidate_dependency_at_checkpoint(
    monkeypatch,
    marker: Path,
    *,
    checkpoint: str,
) -> None:
    def invalidate() -> None:
        marker.write_bytes(b"tampered")

    if checkpoint == "first-gate":
        original_assert = tracker._assert_workbook_targets_current
        calls = 0

        def assert_then_invalidate(*args, **kwargs):
            nonlocal calls
            result = original_assert(*args, **kwargs)
            calls += 1
            if calls == 1:
                invalidate()
            return result

        monkeypatch.setattr(
            tracker, "_assert_workbook_targets_current", assert_then_invalidate
        )
        return
    if checkpoint == "after-story-seal":
        original_complete = tracker.complete_pytest_evidence

        def complete_then_invalidate(*args, **kwargs):
            result = original_complete(*args, **kwargs)
            invalidate()
            return result

        monkeypatch.setattr(
            tracker, "complete_pytest_evidence", complete_then_invalidate
        )
        return
    if checkpoint == "after-backup":
        original_backup = tracker._exclusive_workbook_backup

        def backup_then_invalidate(*args, **kwargs):
            result = original_backup(*args, **kwargs)
            invalidate()
            return result

        monkeypatch.setattr(
            tracker, "_exclusive_workbook_backup", backup_then_invalidate
        )
        return
    raise AssertionError(f"unknown checkpoint: {checkpoint}")


@pytest.mark.parametrize("invalid_kind", ["browser", "pytest"])
def test_pytest_sync_rechecks_both_composite_dependencies_before_story_seal(
    invalid_kind, tmp_path, monkeypatch
) -> None:
    story = "COMPOSITE-DEPENDENCY"
    existing = "existing composite browser and pytest proof"
    required_browser = {"native_required"}
    run, batch_calls = _configure_nonfield_pytest_dependency_sync(
        tmp_path,
        monkeypatch,
        story=story,
        existing_status=tracker.STATES[1],
        case_status="skip",
        existing_evidence=existing,
    )
    monkeypatch.setattr(
        tracker,
        "AUDITED_COMPOSITE",
        {
            story: {
                "browser": required_browser,
                "pytest": {"tests/test_dependency.py::test_dependency"},
            }
        },
    )
    markers = {
        kind: _existing_proof_marker(tmp_path, kind)
        for kind in ("browser", "pytest")
    }

    def browser_proof(candidate, evidence, *, required=None, **_kwargs):
        return (
            candidate == story
            and existing in evidence
            and required == required_browser
            and markers["browser"].read_bytes() == b"sealed"
        )

    def pytest_proof(candidate, evidence, **_kwargs):
        return (
            candidate == story
            and existing in evidence
            and markers["pytest"].read_bytes() == b"sealed"
        )

    monkeypatch.setattr(tracker, "current_browser_proof", browser_proof)
    monkeypatch.setattr(tracker, "current_pytest_proof", pytest_proof)
    _invalidate_dependency_at_checkpoint(
        monkeypatch, markers[invalid_kind], checkpoint="first-gate"
    )

    with pytest.raises(SystemExit, match="proof dependency is no longer current"):
        tracker.sync(run)

    assert batch_calls == []
    assert not (run / "story-evidence.json").exists()
    assert not (run / "feature-status-before-sync.xlsx").exists()


@pytest.mark.parametrize("invalid_kind", ["browser", "pytest"])
def test_browser_sync_rechecks_both_composite_dependencies_before_backup(
    invalid_kind, tmp_path, monkeypatch
) -> None:
    story = "COMPOSITE-DEPENDENCY"
    existing = "existing composite browser and pytest proof"
    required_browser = {"native_required"}
    run, reference, batch_calls = _configure_nonfield_browser_dependency_sync(
        tmp_path,
        monkeypatch,
        story=story,
        existing_status=tracker.STATES[1],
        existing_evidence=existing,
    )
    monkeypatch.setattr(
        tracker,
        "AUDITED_COMPOSITE",
        {
            story: {
                "browser": required_browser,
                "pytest": {"tests/test_dependency.py::test_dependency"},
            }
        },
    )
    markers = {
        kind: _existing_proof_marker(tmp_path, kind)
        for kind in ("browser", "pytest")
    }

    def browser_proof(candidate, evidence, *, required=None, **_kwargs):
        if evidence == reference and required is None:
            return False
        return (
            candidate == story
            and existing in evidence
            and reference in evidence
            and required == required_browser
            and markers["browser"].read_bytes() == b"sealed"
        )

    def pytest_proof(candidate, evidence, **_kwargs):
        return (
            candidate == story
            and existing in evidence
            and markers["pytest"].read_bytes() == b"sealed"
        )

    monkeypatch.setattr(tracker, "current_browser_proof", browser_proof)
    monkeypatch.setattr(tracker, "current_pytest_proof", pytest_proof)
    _invalidate_dependency_at_checkpoint(
        monkeypatch, markers[invalid_kind], checkpoint="first-gate"
    )

    with pytest.raises(SystemExit, match="proof dependency is no longer current"):
        tracker.sync_browser(run)

    assert batch_calls == []
    assert not (run / "feature-status-before-sync.xlsx").exists()


def test_pytest_sync_rechecks_automatic_prior_pytest_before_backup(
    tmp_path, monkeypatch
) -> None:
    story = "AUTOMATIC-DEPENDENCY"
    existing = "existing automatic pytest proof"
    run, batch_calls = _configure_nonfield_pytest_dependency_sync(
        tmp_path,
        monkeypatch,
        story=story,
        existing_status=tracker.STATES[1],
        case_status="skip",
        existing_evidence=existing,
    )
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", {story})
    marker = _existing_proof_marker(tmp_path, "pytest")
    monkeypatch.setattr(
        tracker,
        "current_pytest_proof",
        lambda candidate, evidence, **_kwargs: (
            candidate == story
            and existing in evidence
            and marker.read_bytes() == b"sealed"
        ),
    )
    _invalidate_dependency_at_checkpoint(
        monkeypatch, marker, checkpoint="after-story-seal"
    )

    with pytest.raises(SystemExit, match="proof dependency is no longer current"):
        tracker.sync(run)

    assert batch_calls == []
    assert (run / "story-evidence.json").is_file()
    assert not (run / "feature-status-before-sync.xlsx").exists()


def test_browser_sync_rechecks_automatic_prior_pytest_immediately_before_batch(
    tmp_path, monkeypatch
) -> None:
    story = "AUTOMATIC-DEPENDENCY"
    existing = "existing automatic pytest proof"
    run, reference, batch_calls = _configure_nonfield_browser_dependency_sync(
        tmp_path,
        monkeypatch,
        story=story,
        existing_status=tracker.STATES[1],
        existing_evidence=existing,
    )
    marker = _existing_proof_marker(tmp_path, "pytest")
    monkeypatch.setattr(
        tracker, "current_browser_proof", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        tracker,
        "current_pytest_proof",
        lambda candidate, evidence, **_kwargs: (
            candidate == story
            and existing in evidence
            and marker.read_bytes() == b"sealed"
        ),
    )
    _invalidate_dependency_at_checkpoint(
        monkeypatch, marker, checkpoint="after-backup"
    )

    with pytest.raises(SystemExit, match="proof dependency is no longer current"):
        tracker.sync_browser(run)

    assert reference.endswith("/browser.json")
    assert batch_calls == []
    assert (run / "feature-status-before-sync.xlsx").is_file()


def test_pytest_sync_rechecks_performance_proof_used_for_promotion_before_batch(
    tmp_path, monkeypatch
) -> None:
    story = "PERFORMANCE-DEPENDENCY"
    existing = "existing performance proof"
    run, batch_calls = _configure_nonfield_pytest_dependency_sync(
        tmp_path,
        monkeypatch,
        story=story,
        existing_status=tracker.STATES[3],
        case_status="pass",
        existing_evidence=existing,
    )
    monkeypatch.setattr(tracker, "AUDITED_PERFORMANCE", {story: "soak"})
    marker = _existing_proof_marker(tmp_path, "performance")
    monkeypatch.setattr(
        tracker,
        "current_performance_proof",
        lambda candidate, evidence, **_kwargs: (
            candidate == story
            and existing in evidence
            and marker.read_bytes() == b"sealed"
        ),
    )
    _invalidate_dependency_at_checkpoint(
        monkeypatch, marker, checkpoint="after-backup"
    )

    with pytest.raises(SystemExit, match="proof dependency is no longer current"):
        tracker.sync(run)

    assert batch_calls == []
    assert (run / "feature-status-before-sync.xlsx").is_file()


def test_browser_sync_rechecks_performance_proof_used_to_preserve_pass(
    tmp_path, monkeypatch
) -> None:
    story = "PERFORMANCE-DEPENDENCY"
    existing = "existing performance proof"
    run, _reference, batch_calls = _configure_nonfield_browser_dependency_sync(
        tmp_path,
        monkeypatch,
        story=story,
        existing_status=tracker.STATES[1],
        existing_evidence=existing,
    )
    monkeypatch.setattr(tracker, "AUDITED_PERFORMANCE", {story: "soak"})
    marker = _existing_proof_marker(tmp_path, "performance")
    monkeypatch.setattr(
        tracker, "current_browser_proof", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        tracker,
        "current_performance_proof",
        lambda candidate, evidence, **_kwargs: (
            candidate == story
            and existing in evidence
            and marker.read_bytes() == b"sealed"
        ),
    )
    _invalidate_dependency_at_checkpoint(
        monkeypatch, marker, checkpoint="first-gate"
    )

    with pytest.raises(SystemExit, match="proof dependency is no longer current"):
        tracker.sync_browser(run)

    assert batch_calls == []


def test_browser_sync_merge_preserves_an_older_current_witness_shadowed_by_newer_invalid_runs(
    tmp_path, monkeypatch
) -> None:
    story = "BROWSER-MERGE"
    existing = "\n".join(
        [
            f"docs/qa/runs/old-valid/browser.json #{story}; required=pass",
            f"docs/qa/runs/new-invalid-1/browser.json #{story}; required=pass",
            f"docs/qa/runs/new-invalid-2/browser.json #{story}; required=pass",
            f"docs/qa/runs/new-invalid-3/browser.json #{story}; required=pass",
        ]
    )
    run, _reference, batch_calls = _configure_nonfield_browser_dependency_sync(
        tmp_path,
        monkeypatch,
        story=story,
        existing_status="合格",
        existing_evidence=existing,
    )
    monkeypatch.setattr(tracker, "AUDITED_BROWSER", {story: {"required"}})
    monkeypatch.setattr(
        tracker,
        "current_browser_proof",
        lambda candidate, evidence, **_kwargs: (
            candidate == story and "old-valid/browser.json" in evidence
        ),
    )

    tracker.sync_browser(run)

    evidence_write = next(
        operation
        for operation in batch_calls[0]
        if operation["path"] == "/Stories/M15"
    )
    assert "old-valid/browser.json" in evidence_write["props"]["value"]
    assert (run / "feature-status-before-sync.xlsx").is_file()


def test_browser_sync_revalidates_the_seal_after_staged_office_work_before_replace(
    tmp_path, monkeypatch
) -> None:
    real_batch = tracker.batch
    story = "BROWSER-COMMIT-GUARD"
    run, _reference, _batch_calls = _configure_nonfield_browser_dependency_sync(
        tmp_path,
        monkeypatch,
        story=story,
        existing_status="未実施",
        existing_evidence="",
    )
    monkeypatch.setattr(tracker, "QA", tmp_path)
    monkeypatch.setattr(tracker, "AUDITED_BROWSER", {story: {"supplemental"}})
    monkeypatch.setattr(
        tracker,
        "current_browser_proof",
        lambda candidate, *_args, **_kwargs: candidate == story,
    )
    monkeypatch.setattr(tracker, "batch", real_batch)
    book_before = tracker.BOOK.read_bytes()
    tampered = False

    def tampering_office(action, _book, *_args):
        nonlocal tampered
        if action == "batch" and not tampered:
            tampered = True
            (run / "browser.json").write_bytes(
                (run / "browser.json").read_bytes() + b"tampered during office"
            )
        return ""

    monkeypatch.setattr(tracker, "office", tampering_office)

    with pytest.raises(SystemExit, match="before workbook replace"):
        tracker.sync_browser(run)

    assert tampered is True
    assert tracker.BOOK.read_bytes() == book_before
    assert (run / "feature-status-before-sync.xlsx").is_file()


def test_browser_sync_rechecks_workbook_cas_after_slow_proof_dependencies(
    tmp_path, monkeypatch
) -> None:
    real_batch = tracker.batch
    real_require = tracker._require_current_proof_dependencies
    story = "AUTOMATIC-COMMIT-CAS"
    run, _reference, _batch_calls = _configure_nonfield_browser_dependency_sync(
        tmp_path,
        monkeypatch,
        story=story,
        existing_status="合格",
        existing_evidence="current pytest proof",
    )
    monkeypatch.setattr(tracker, "QA", tmp_path)
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", {story})
    monkeypatch.setattr(
        tracker, "current_browser_proof", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        tracker,
        "current_pytest_proof",
        lambda candidate, evidence, **_kwargs: (
            candidate == story and "current pytest proof" in evidence
        ),
    )
    live_cells = {
        "A15": story,
        "F15": "合格",
        "M15": "current pytest proof",
    }
    monkeypatch.setattr(tracker, "read_cells", lambda: dict(live_cells))
    monkeypatch.setattr(tracker, "batch", real_batch)
    office_ran = False
    edited = False

    def office_then_allow_guard(action, _book, *_args):
        nonlocal office_ran
        if action == "batch":
            office_ran = True
        return ""

    def recheck_then_edit(*args, **kwargs):
        nonlocal edited
        result = real_require(*args, **kwargs)
        if office_ran and not edited:
            live_cells["M15"] = "concurrent workbook evidence edit"
            edited = True
        return result

    monkeypatch.setattr(tracker, "office", office_then_allow_guard)
    monkeypatch.setattr(
        tracker, "_require_current_proof_dependencies", recheck_then_edit
    )
    book_before = tracker.BOOK.read_bytes()

    with pytest.raises(SystemExit, match="台帳が同期中に変更"):
        tracker.sync_browser(run)

    assert edited is True
    assert live_cells["M15"] == "concurrent workbook evidence edit"
    assert tracker.BOOK.read_bytes() == book_before


def test_existing_failure_does_not_become_a_revalidated_proof_dependency(
    tmp_path, monkeypatch
) -> None:
    story = "PERFORMANCE-DEPENDENCY"
    existing = "existing performance proof"
    run, batch_calls = _configure_nonfield_pytest_dependency_sync(
        tmp_path,
        monkeypatch,
        story=story,
        existing_status=tracker.STATES[2],
        case_status="pass",
        existing_evidence=existing,
    )
    monkeypatch.setattr(tracker, "AUDITED_PERFORMANCE", {story: "soak"})
    calls = 0

    def performance_proof(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(
        tracker, "current_performance_proof", performance_proof
    )

    tracker.sync(run)

    assert calls == 1
    assert len(batch_calls) == 1
    status_operation = next(
        operation
        for operation in batch_calls[0]
        if operation["path"] == "/Stories/F15"
    )
    assert status_operation["props"]["value"] == tracker.STATES[2]


def _run_field_soak(tmp_path, monkeypatch, *, result):
    cells = {
        "A90": "PERF-01", "F90": "合格", "M90": "field proof for PERF-01",
        "A91": "PERF-02", "F91": "合格", "M91": "field proof for PERF-02",
        "A92": "PERF-03", "F92": "合格", "M92": "soak proof for PERF-03",
        "A94": "PERF-05", "F94": "合格", "M94": "soak proof for PERF-05",
    }
    _, operations = tracker_book(tmp_path, monkeypatch, cells)
    run = tmp_path / "field-preservation-soak"
    run.mkdir()
    report_path = run / "2026-09-08-acceptance.json"
    report_path.write_text(json.dumps({"results": [result]}), encoding="utf-8")
    manifest = manifest_sealed_for_report(performance_manifest("soak"), report_path)
    monkeypatch.setattr(
        tracker, "validate_performance_manifest", lambda *args, **kwargs: manifest
    )
    return run, operations


def test_soak_sync_keeps_field_pass_for_green_perf01_and_perf02(
    tmp_path, monkeypatch
) -> None:
    run, operations = _run_field_soak(
        tmp_path, monkeypatch, result=qualifying_soak_result()
    )
    monkeypatch.setattr(
        tracker,
        "current_field_proof",
        lambda story, *_args, **_kwargs: story in {"PERF-01", "PERF-02"},
    )

    tracker.sync_soak(run)

    assert tracker.setcell("F90", value="合格") in operations
    assert tracker.setcell("F91", value="合格") in operations


def test_soak_sync_explicit_performance_failures_override_field_pass(
    tmp_path, monkeypatch
) -> None:
    result = qualifying_soak_result()
    result["latency"]["max_s"] = 8.001
    result["memory"]["peak_mb"] = 5000.001
    run, operations = _run_field_soak(tmp_path, monkeypatch, result=result)
    monkeypatch.setattr(
        tracker,
        "current_field_proof",
        lambda *_args, **_kwargs: pytest.fail("a failing soak result must win"),
    )

    tracker.sync_soak(run)

    assert tracker.setcell("F90", value="不合格") in operations
    assert tracker.setcell("F91", value="不合格") in operations


@pytest.mark.parametrize(
    ("proof_is_current", "expected_status"),
    [(True, None), (False, "部分確認")],
    ids=["current-field-proof", "stale-field-proof"],
)
def test_audit_keeps_only_a_current_field_pass(
    proof_is_current, expected_status, tmp_path, monkeypatch
) -> None:
    qa = tmp_path / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    cells = {
        "A15": "OPS-01",
        "F15": "合格",
        "M15": "field: docs/qa/runs/field/field-story-evidence.json #OPS-01; review=pass",
        "N15": "prior remaining",
    }
    operations = []
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "read_cells", lambda: cells)
    monkeypatch.setattr(
        tracker, "current_field_proof", lambda *_args, **_kwargs: proof_is_current
    )
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "check", _verified_workbook_state_for_test)

    tracker.audit()

    if expected_status is None:
        assert operations == []
    else:
        assert tracker.setcell("F15", value=expected_status) in operations
        remaining = next(
            operation["props"]["value"]
            for operation in operations
            if operation["path"] == "/Stories/N15"
        )
        assert "現地証拠を現行source/manifestで再監査できない" in remaining
    audit_file = next((qa / "runs").glob("*-coverage-audit/audit.json"))
    audit_payload = json.loads(audit_file.read_text(encoding="utf-8"))
    assert audit_payload["downgraded_to_partial"] == (
        [] if proof_is_current else ["OPS-01"]
    )
    assert audit_payload["audited_field"] == sorted(tracker.AUDITED_FIELD)
    assert audit_payload["workbook"]["sha256"] == hashlib.sha256(
        book.read_bytes()
    ).hexdigest()


@pytest.mark.parametrize(
    ("proof_is_current", "expected_status"),
    [(True, None), (False, "部分確認")],
    ids=["current-performance-proof", "stale-performance-proof"],
)
def test_audit_keeps_only_a_current_performance_pass(
    proof_is_current, expected_status, tmp_path, monkeypatch
) -> None:
    qa = tmp_path / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    cells = {
        "A15": "PERF-03",
        "F15": "合格",
        "M15": "performance: docs/qa/runs/soak/soak-report.json #PERF-03",
        "N15": "prior remaining",
    }
    operations = []
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "read_cells", lambda: cells)
    monkeypatch.setattr(
        tracker,
        "current_performance_proof",
        lambda *_args, **_kwargs: proof_is_current,
    )
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "check", _verified_workbook_state_for_test)

    tracker.audit()

    if expected_status is None:
        assert operations == []
    else:
        assert tracker.setcell("F15", value=expected_status) in operations
        remaining = next(
            operation["props"]["value"]
            for operation in operations
            if operation["path"] == "/Stories/N15"
        )
        assert "性能証拠を現行source/manifestで再監査できない" in remaining
    audit_file = next((qa / "runs").glob("*-coverage-audit/audit.json"))
    audit_payload = json.loads(audit_file.read_text(encoding="utf-8"))
    assert audit_payload["downgraded_to_partial"] == (
        [] if proof_is_current else ["PERF-03"]
    )
    assert audit_payload["audited_performance"] == dict(
        sorted(tracker.AUDITED_PERFORMANCE.items())
    )


@pytest.mark.parametrize(
    ("story", "pytest_current", "browser_current", "should_keep"),
    [
        ("OPS-04", True, False, True),
        ("OPS-04", False, True, False),
        ("JOIN-02", False, True, True),
        ("JOIN-02", True, False, False),
        ("OPS-09", True, True, True),
        ("OPS-09", False, True, False),
        ("OPS-09", True, False, False),
    ],
    ids=[
        "automatic-current",
        "automatic-stale",
        "browser-current",
        "browser-stale",
        "composite-current",
        "composite-pytest-stale",
        "composite-browser-stale",
    ],
)
def test_audit_revalidates_non_field_pass_sources(
    story, pytest_current, browser_current, should_keep, tmp_path, monkeypatch
) -> None:
    qa = tmp_path / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    cells = {
        "A15": story,
        "F15": "合格",
        "M15": "saved immutable proof",
        "N15": "prior remaining",
    }
    operations = []
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "read_cells", lambda: cells)
    monkeypatch.setattr(
        tracker,
        "current_pytest_proof",
        lambda *_args, **_kwargs: pytest_current,
    )
    monkeypatch.setattr(
        tracker,
        "current_browser_proof",
        lambda *_args, **_kwargs: browser_current,
    )
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "check", _verified_workbook_state_for_test)

    tracker.audit()

    if should_keep:
        assert operations == []
    else:
        assert tracker.setcell("F15", value="部分確認") in operations
        remaining = next(
            operation["props"]["value"]
            for operation in operations
            if operation["path"] == "/Stories/N15"
        )
        assert "テスト証拠を現行sourceで再監査できない" in remaining


def _story_moved_after_snapshot(
    cells, *, story: str, old_row: int, new_row: int, replacement: str
):
    moved = dict(cells)
    moved[f"A{old_row}"] = replacement
    moved[f"A{new_row}"] = story
    for column in "FMNO":
        old_cell = f"{column}{old_row}"
        if old_cell in cells:
            moved[f"{column}{new_row}"] = cells[old_cell]
        moved[old_cell] = f"concurrent value for {replacement}"
    return moved


def _install_snapshot_then_move(
    monkeypatch, cells, *, story: str, old_row: int, new_row: int, replacement: str
):
    snapshots = iter([
        cells,
        _story_moved_after_snapshot(
            cells,
            story=story,
            old_row=old_row,
            new_row=new_row,
            replacement=replacement,
        ),
    ])
    monkeypatch.setattr(tracker, "read_cells", lambda: next(snapshots))


def test_sync_field_rejects_story_row_reorder_before_batch(
    tmp_path, monkeypatch
) -> None:
    _, _, cells, operations, _ = _field_test_book(tmp_path, monkeypatch)
    validation = _field_validation(tmp_path)
    monkeypatch.setattr(
        tracker, "validate_field_bundle", lambda *args, **kwargs: dict(validation)
    )
    _install_snapshot_then_move(
        monkeypatch,
        cells,
        story="OPS-01",
        old_row=20,
        new_row=40,
        replacement="OPS-99",
    )

    with pytest.raises(SystemExit, match="台帳が同期中に変更"):
        tracker.sync_field(
            validation["bundle_path"].parent,
            reviewer="QA reviewer",
            approve_candidates=True,
        )

    assert operations == []


def test_pytest_sync_rejects_field_story_row_reorder_before_batch(
    tmp_path, monkeypatch
) -> None:
    story, run, operations = _configure_field_pytest_sync(
        tmp_path, monkeypatch, case_status="pass"
    )
    cells = {
        "A15": story,
        "F15": "部分確認",
        "M15": (
            "field: docs/qa/runs/field/field-story-evidence.json #OPS-01; "
            "review=pass"
        ),
    }
    _install_snapshot_then_move(
        monkeypatch,
        cells,
        story=story,
        old_row=15,
        new_row=16,
        replacement="OPS-99",
    )
    monkeypatch.setattr(tracker, "current_field_proof", lambda *_args, **_kwargs: True)

    with pytest.raises(SystemExit, match="台帳が同期中に変更"):
        tracker.sync(run)

    assert operations == []


def test_pytest_sync_rejects_concurrent_field_target_cell_edit(
    tmp_path, monkeypatch
) -> None:
    story, run, operations = _configure_field_pytest_sync(
        tmp_path, monkeypatch, case_status="pass"
    )
    cells = {
        "A15": story,
        "F15": "部分確認",
        "M15": (
            "field: docs/qa/runs/field/field-story-evidence.json #OPS-01; "
            "review=pass"
        ),
    }
    edited = dict(cells)
    edited["M15"] = "concurrent manual evidence edit"
    snapshots = iter([cells, edited])
    monkeypatch.setattr(tracker, "read_cells", lambda: next(snapshots))
    monkeypatch.setattr(tracker, "current_field_proof", lambda *_args, **_kwargs: True)

    with pytest.raises(SystemExit, match=r"changed_cells=\['M15'\]"):
        tracker.sync(run)

    assert operations == []


def test_browser_sync_rejects_field_story_row_reorder_before_batch(
    tmp_path, monkeypatch
) -> None:
    story, run, operations = _configure_field_browser_sync(
        tmp_path, monkeypatch, result_status="pass"
    )
    cells = {
        "A15": story,
        "F15": "合格",
        "M15": (
            "field: docs/qa/runs/field/field-story-evidence.json #OPS-10; "
            "review=pass"
        ),
    }
    _install_snapshot_then_move(
        monkeypatch,
        cells,
        story=story,
        old_row=15,
        new_row=16,
        replacement="OPS-99",
    )
    monkeypatch.setattr(tracker, "current_field_proof", lambda *_args, **_kwargs: True)

    with pytest.raises(SystemExit, match="台帳が同期中に変更"):
        tracker.sync_browser(run)

    assert operations == []


def test_audit_rejects_field_story_row_reorder_before_batch(
    tmp_path, monkeypatch
) -> None:
    qa = tmp_path / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    cells = {
        "A15": "OPS-01",
        "F15": "合格",
        "M15": "stale field evidence",
        "N15": "prior remaining",
    }
    operations = []
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    _install_snapshot_then_move(
        monkeypatch,
        cells,
        story="OPS-01",
        old_row=15,
        new_row=16,
        replacement="OPS-99",
    )
    monkeypatch.setattr(tracker, "current_field_proof", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "check", lambda: None)

    with pytest.raises(SystemExit, match="台帳が同期中に変更"):
        tracker.audit()

    assert operations == []


def test_soak_sync_rejects_field_story_row_reorder_before_batch(
    tmp_path, monkeypatch
) -> None:
    run, operations = _run_field_soak(
        tmp_path, monkeypatch, result=qualifying_soak_result()
    )
    cells = {
        "A90": "PERF-01", "F90": "合格", "M90": "field proof for PERF-01",
        "A91": "PERF-02", "F91": "合格", "M91": "field proof for PERF-02",
        "A92": "PERF-03", "F92": "合格", "M92": "soak proof for PERF-03",
        "A94": "PERF-05", "F94": "合格", "M94": "soak proof for PERF-05",
    }
    _install_snapshot_then_move(
        monkeypatch,
        cells,
        story="PERF-01",
        old_row=90,
        new_row=95,
        replacement="PERF-99",
    )
    monkeypatch.setattr(
        tracker,
        "current_field_proof",
        lambda story, *_args, **_kwargs: story in {"PERF-01", "PERF-02"},
    )

    with pytest.raises(SystemExit, match="台帳が同期中に変更"):
        tracker.sync_soak(run)

    assert operations == []


def _run_field_scale(tmp_path, monkeypatch, *, cells):
    _, operations = tracker_book(tmp_path, monkeypatch, cells)
    run = tmp_path / "field-preservation-scale"
    run.mkdir()
    report_path = run / "2026-09-08-scaling.json"
    report_path.write_text(
        json.dumps(
            {
                "minutes": 2.0,
                "corpus": "ja_ext",
                "points": qualifying_scale_points(),
            }
        ),
        encoding="utf-8",
    )
    manifest = manifest_sealed_for_report(
        performance_manifest("scale"), report_path
    )
    monkeypatch.setattr(
        tracker, "validate_performance_manifest", lambda *args, **kwargs: manifest
    )
    return run, operations


def test_scale_sync_reaudits_an_untouched_stale_field_pass(
    tmp_path, monkeypatch
) -> None:
    run, operations = _run_field_scale(
        tmp_path,
        monkeypatch,
        cells={
            "A15": "OPS-12",
            "F15": "合格",
            "M15": "stale field receipt",
            "N15": "school devices must be rerun",
            "A93": "PERF-04",
            "F93": "不合格",
            "M93": "prior failed scale evidence",
        },
    )
    monkeypatch.setattr(
        tracker, "current_field_proof", lambda *_args, **_kwargs: False
    )

    tracker.sync_scale(run)

    assert tracker.setcell("F15", value="部分確認") in operations
    assert tracker.setcell("F93", value="合格") in operations


def test_scale_sync_rejects_field_story_row_reorder_before_batch(
    tmp_path, monkeypatch
) -> None:
    cells = {
        "A15": "OPS-12",
        "F15": "合格",
        "M15": "field receipt",
        "A93": "PERF-04",
        "F93": "不合格",
        "M93": "prior failed scale evidence",
    }
    run, operations = _run_field_scale(tmp_path, monkeypatch, cells=cells)
    _install_snapshot_then_move(
        monkeypatch,
        cells,
        story="OPS-12",
        old_row=15,
        new_row=16,
        replacement="OPS-99",
    )
    monkeypatch.setattr(
        tracker, "current_field_proof", lambda *_args, **_kwargs: False
    )

    with pytest.raises(SystemExit, match="台帳が同期中に変更"):
        tracker.sync_scale(run)

    assert operations == []


def test_pytest_sync_aborts_when_cached_field_receipt_changes_before_batch(
    tmp_path, monkeypatch
) -> None:
    _, run, operations = _configure_field_pytest_sync(
        tmp_path, monkeypatch, case_status="pass"
    )
    monkeypatch.setattr(
        tracker, "current_field_proof", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(tracker, "_field_proof_cache_is_current", lambda _cache: False)

    with pytest.raises(SystemExit, match="field receipt/source/manifest"):
        tracker.sync(run)

    assert operations == []


def test_browser_sync_aborts_when_cached_field_receipt_changes_before_batch(
    tmp_path, monkeypatch
) -> None:
    _, run, operations = _configure_field_browser_sync(
        tmp_path, monkeypatch, result_status="pass"
    )
    monkeypatch.setattr(
        tracker, "current_field_proof", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(tracker, "_field_proof_cache_is_current", lambda _cache: False)

    with pytest.raises(SystemExit, match="field receipt/source/manifest"):
        tracker.sync_browser(run)

    assert operations == []


def test_soak_sync_aborts_when_cached_field_receipt_changes_before_batch(
    tmp_path, monkeypatch
) -> None:
    run, operations = _run_field_soak(
        tmp_path, monkeypatch, result=qualifying_soak_result()
    )
    monkeypatch.setattr(
        tracker, "current_field_proof", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(tracker, "_field_proof_cache_is_current", lambda _cache: False)

    with pytest.raises(SystemExit, match="field receipt/source/manifest"):
        tracker.sync_soak(run)

    assert operations == []


def test_scale_sync_aborts_when_cached_field_receipt_changes_before_batch(
    tmp_path, monkeypatch
) -> None:
    run, operations = _run_field_scale(
        tmp_path,
        monkeypatch,
        cells={
            "A15": "OPS-12",
            "F15": "合格",
            "M15": "field receipt",
            "A93": "PERF-04",
            "F93": "不合格",
            "M93": "prior failed scale evidence",
        },
    )
    monkeypatch.setattr(
        tracker, "current_field_proof", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(tracker, "_field_proof_cache_is_current", lambda _cache: False)

    with pytest.raises(SystemExit, match="field receipt/source/manifest"):
        tracker.sync_scale(run)

    assert operations == []


def test_audit_aborts_when_cached_field_receipt_changes_before_batch(
    tmp_path, monkeypatch
) -> None:
    qa = tmp_path / "docs" / "qa"
    (qa / "runs").mkdir(parents=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    operations = []
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: {
            "A15": "OPS-01",
            "F15": "合格",
            "M15": "field receipt",
            "N15": "prior remaining",
        },
    )
    monkeypatch.setattr(
        tracker, "current_field_proof", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(tracker, "_field_proof_cache_is_current", lambda _cache: False)
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))
    monkeypatch.setattr(tracker, "check", lambda: None)

    with pytest.raises(SystemExit, match="field receipt/source/manifest"):
        tracker.audit()

    assert operations == []
    assert list((qa / "runs").iterdir()) == []


def _audit_test_workspace(tmp_path, monkeypatch, cells, *, read_cells=None):
    qa = tmp_path / "docs" / "qa"
    (qa / "runs").mkdir(parents=True, exist_ok=True)
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook placeholder")
    operations = []
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "read_cells", read_cells or (lambda: dict(cells)))
    monkeypatch.setattr(tracker, "batch", lambda ops: operations.extend(ops))

    def checked_state():
        workbook_bytes, fingerprint = tracker._stable_file_snapshot(book)
        return tracker.VerifiedWorkbookState(
            file_revision=fingerprint[0],
            parent_identity=fingerprint[1][:2],
            sha256=hashlib.sha256(workbook_bytes).hexdigest(),
            size=len(workbook_bytes),
            proof_cache=tracker.AuditProofCache(),
        )

    monkeypatch.setattr(tracker, "check", checked_state)
    return qa, operations


def test_audit_rejects_a_linked_runs_parent_before_external_write(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    qa = root / "docs" / "qa"
    qa.mkdir(parents=True)
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    runs = qa / "runs"
    try:
        runs.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")
    book = qa / "feature-status.xlsx"
    book.write_bytes(b"workbook")
    monkeypatch.setattr(tracker, "ROOT", root)
    monkeypatch.setattr(tracker, "QA", qa)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "read_cells", lambda: {})

    with pytest.raises(SystemExit, match="output parent is unsafe"):
        tracker.audit()

    assert list(outside.iterdir()) == []


def test_final_workbook_audit_rejects_a_new_unsupported_pass(
    tmp_path, monkeypatch
) -> None:
    book = tmp_path / "feature-status.xlsx"
    book.write_bytes(b"workbook")
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "BOOK", book)
    monkeypatch.setattr(tracker, "AUDITED_AUTOMATIC", {"OPS-FINAL"})
    monkeypatch.setattr(tracker, "AUDITED_BROWSER", {})
    monkeypatch.setattr(tracker, "AUDITED_COMPOSITE", {})
    monkeypatch.setattr(tracker, "AUDITED_PERFORMANCE", {})
    monkeypatch.setattr(tracker, "AUDITED_FIELD", set())
    monkeypatch.setattr(
        tracker,
        "read_cells",
        lambda: {
            "A15": "OPS-FINAL",
            "F15": "合格",
            "M15": "invalid proof",
            "N15": "",
        },
    )
    monkeypatch.setattr(
        tracker, "current_pytest_proof", lambda *_args, **_kwargs: False
    )

    with pytest.raises(SystemExit, match="without current complete proof"):
        tracker._capture_verified_workbook_state()


def test_audit_does_not_finalize_after_check_period_workbook_edit(
    tmp_path, monkeypatch
) -> None:
    cells = {
        "A15": "OPS-04",
        "F15": "部分確認",
        "M15": "invalid proof",
        "N15": "",
    }
    qa, _operations = _audit_test_workspace(tmp_path, monkeypatch, cells)

    def check_then_promote_without_proof():
        state = _verified_workbook_state_for_test()
        cells["F15"] = "合格"
        tracker.BOOK.write_bytes(b"concurrent unsupported pass")
        return state

    monkeypatch.setattr(tracker, "check", check_then_promote_without_proof)

    with pytest.raises(SystemExit, match="coverage audit publication"):
        tracker.audit()

    assert list((qa / "runs").glob("*-coverage-audit")) == []
    failed = list((qa / "runs").glob(".*-coverage-audit-failed"))
    assert len(failed) == 1
    assert (failed[0] / "audit.json").is_file()
    assert tracker.BOOK.read_bytes() == b"concurrent unsupported pass"


def test_audit_rejects_a_nonpassing_row_promoted_concurrently(
    tmp_path, monkeypatch
) -> None:
    cells = {
        "A15": "OPS-04",
        "F15": "部分確認",
        "M15": "existing evidence",
        "N15": "remaining work",
    }
    edited = {**cells, "F15": "合格"}
    reads = 0

    def read_cells():
        nonlocal reads
        reads += 1
        return dict(cells if reads == 1 else edited)

    qa, operations = _audit_test_workspace(
        tmp_path, monkeypatch, cells, read_cells=read_cells
    )

    with pytest.raises(SystemExit, match=r"changed_cells=.*F15"):
        tracker.audit()

    assert operations == []
    assert list((qa / "runs").glob("*-coverage-audit")) == []


@pytest.mark.parametrize("proof_is_current", [True, False], ids=["kept", "downgraded"])
@pytest.mark.parametrize(
    ("changed_column", "replacement"),
    [
        ("A", "OPS-05"),
        ("F", "部分確認"),
        ("M", "concurrent replacement evidence"),
        ("N", "concurrent replacement finding"),
    ],
    ids=["story-id", "status", "evidence", "finding"],
)
def test_audit_guards_evidence_cells_used_by_both_kept_and_downgraded_rows(
    proof_is_current, changed_column, replacement, tmp_path, monkeypatch
) -> None:
    cells = {
        "A15": "OPS-04",
        "F15": "合格",
        "M15": "original pytest evidence",
        "N15": "prior remaining",
    }
    edited = {**cells, f"{changed_column}15": replacement}
    reads = 0

    def read_cells():
        nonlocal reads
        reads += 1
        return dict(cells if reads == 1 else edited)

    qa, operations = _audit_test_workspace(
        tmp_path, monkeypatch, cells, read_cells=read_cells
    )
    monkeypatch.setattr(
        tracker, "current_pytest_proof", lambda *_args, **_kwargs: proof_is_current
    )

    with pytest.raises(
        SystemExit, match=rf"changed_cells=.*{changed_column}15"
    ):
        tracker.audit()

    assert operations == []
    assert list((qa / "runs").glob("*-coverage-audit")) == []


@pytest.mark.parametrize(
    ("story", "changing_proof"),
    [
        ("OPS-04", "pytest"),
        ("JOIN-02", "browser"),
        ("OPS-09", "composite-browser"),
        ("PERF-03", "performance"),
        ("OPS-01", "field"),
    ],
    ids=["automatic", "browser", "composite", "performance", "field"],
)
def test_audit_rechecks_each_proof_mode_immediately_before_commit(
    story, changing_proof, tmp_path, monkeypatch
) -> None:
    cells = {
        "A15": story,
        "F15": "合格",
        "M15": "proof that changes while audit is running",
        "N15": "prior remaining",
    }
    qa, operations = _audit_test_workspace(tmp_path, monkeypatch, cells)
    calls = 0

    def changing(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return calls == 1

    monkeypatch.setattr(
        tracker,
        "current_pytest_proof",
        changing if changing_proof == "pytest" else lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        tracker,
        "current_browser_proof",
        changing
        if changing_proof in {"browser", "composite-browser"}
        else lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        tracker,
        "current_performance_proof",
        changing if changing_proof == "performance" else lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        tracker,
        "current_field_proof",
        changing if changing_proof == "field" else lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(tracker, "_field_proof_cache_is_current", lambda _cache: True)

    with pytest.raises(SystemExit, match="audit evidence changed"):
        tracker.audit()

    assert calls == 2
    assert operations == []
    assert list((qa / "runs").glob("*-coverage-audit")) == []


@pytest.mark.parametrize(
    "changed_state",
    ["evidence-fingerprint", "source-snapshot"],
)
def test_audit_aborts_when_proof_state_changes_without_changing_decision(
    changed_state, tmp_path, monkeypatch
) -> None:
    cells = {
        "A15": "OPS-04",
        "F15": "合格",
        "M15": "current proof",
        "N15": "",
    }
    qa, operations = _audit_test_workspace(tmp_path, monkeypatch, cells)
    calls = 0

    def stable_result(_story, _evidence, *, cache):
        nonlocal calls
        calls += 1
        if changed_state == "evidence-fingerprint":
            cache.file_fingerprints["proof"] = str(calls)
        else:
            cache.source_snapshots[("pytest", False)] = {"proof": str(calls)}
        return True

    monkeypatch.setattr(tracker, "current_pytest_proof", stable_result)

    with pytest.raises(SystemExit, match="audit evidence changed"):
        tracker.audit()

    assert calls == 2
    assert operations == []
    assert list((qa / "runs").glob("*-coverage-audit")) == []


def test_audit_revalidates_second_pass_fingerprints_immediately_before_mutation(
    tmp_path, monkeypatch
) -> None:
    cells = {
        "A15": "OPS-04",
        "F15": "合格",
        "M15": "current proof",
        "N15": "",
    }
    marker = tmp_path / "proof.json"
    marker.write_bytes(b"sealed proof")
    reads = 0

    def read_cells():
        nonlocal reads
        reads += 1
        if reads == 3:
            marker.write_bytes(b"tampered after second plan")
        return dict(cells)

    qa, operations = _audit_test_workspace(
        tmp_path, monkeypatch, cells, read_cells=read_cells
    )

    def current(_story, _evidence, *, cache):
        cache.file_fingerprints[marker] = hashlib.sha256(
            marker.read_bytes()
        ).hexdigest()
        return True

    monkeypatch.setattr(tracker, "current_pytest_proof", current)

    with pytest.raises(SystemExit, match="audit proof state changed"):
        tracker.audit()

    assert operations == []
    assert list((qa / "runs").glob("*-coverage-audit")) == []


@pytest.mark.parametrize("proof_is_current", [True, False], ids=["no-write", "write"])
def test_audit_finalizes_coverage_artifacts_only_after_check_succeeds(
    proof_is_current, tmp_path, monkeypatch
) -> None:
    cells = {
        "A15": "OPS-04",
        "F15": "合格",
        "M15": "current pytest evidence",
        "N15": "",
    }
    qa, operations = _audit_test_workspace(tmp_path, monkeypatch, cells)
    monkeypatch.setattr(
        tracker,
        "current_pytest_proof",
        lambda *_args, **_kwargs: proof_is_current,
    )
    monkeypatch.setattr(
        tracker,
        "check",
        lambda: (_ for _ in ()).throw(RuntimeError("post-write validation failed")),
    )

    with pytest.raises(RuntimeError, match="post-write validation failed"):
        tracker.audit()

    assert bool(operations) is not proof_is_current
    assert list((qa / "runs").glob("*-coverage-audit")) == []
    assert list((qa / "runs").rglob("audit.json")) == []
    failed = list((qa / "runs").glob(".*-coverage-audit-failed"))
    # check() performs writes even when the audit itself has no downgrade ops,
    # so any post-write failure retains the recoverable staging backup.
    assert bool(failed) is True
    if failed:
        assert (failed[0] / "feature-status-before-sync.xlsx").is_file()


def test_audit_reuses_one_pytest_run_once_per_freshness_pass(
    tmp_path, monkeypatch
) -> None:
    stories = ("OPS-04", "OPS-05", "OPS-07")
    selectors = {story: f"tests/test_shared.py::test_{story.lower()}" for story in stories}
    qa = tmp_path / "docs" / "qa"
    run = qa / "runs" / "shared-pytest"
    run.mkdir(parents=True)
    evidence_path = run / "story-evidence.json"
    source_hashes = {
        name: hashlib.sha256(name.encode("utf-8")).hexdigest()
        for name in (
            "config.yaml",
            "server/main.py",
            "scripts/story_tracker.py",
            "tests/test_shared.py",
        )
    }
    xml = "<testsuite>" + "".join(
        f'<testcase classname="tests.test_shared" name="test_{story.lower()}"/>'
        for story in stories
    ) + "</testsuite>"
    monkeypatch.setattr(tracker, "ROOT", tmp_path)
    monkeypatch.setattr(tracker, "QA", qa)
    _write_pytest_envelope(
        run,
        xml=xml.encode("utf-8"),
        source_hashes=source_hashes,
        complete=True,
        story_evidence={story: {} for story in stories},
    )
    reference = evidence_path.relative_to(tmp_path).as_posix()
    cells = {}
    for number, story in enumerate(stories, start=15):
        cells.update(
            {
                f"A{number}": story,
                f"F{number}": "合格",
                f"M{number}": f"pytest: {reference} #{story}",
                f"N{number}": "",
            }
        )
    _audit_test_workspace(tmp_path, monkeypatch, cells)
    monkeypatch.setattr(
        tracker,
        "catalog",
        lambda: [
            {"ID": story, "自動テスト候補": selectors[story]} for story in stories
        ],
    )
    hash_calls = 0
    load_calls = 0

    def snapshot_supplied_source_hashes(_root, _supplier):
        nonlocal hash_calls
        hash_calls += 1
        return tracker.SourceHashSnapshot(dict(source_hashes), {})

    real_load = tracker.load_sealed_pytest_evidence

    def load_sealed(*args, **kwargs):
        nonlocal load_calls
        load_calls += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(
        tracker, "snapshot_supplied_source_hashes", snapshot_supplied_source_hashes
    )
    monkeypatch.setattr(
        tracker, "_source_snapshot_revisions_are_current", lambda *_args: True
    )
    monkeypatch.setattr(tracker, "load_sealed_pytest_evidence", load_sealed)

    tracker.audit()

    assert hash_calls == 4
    assert load_calls == 2


def test_audit_reuses_one_browser_run_once_per_freshness_pass(
    tmp_path, monkeypatch
) -> None:
    scenarios = {
        "JOIN-02": "qr_query_prefills_code",
        "JOIN-03": "wrong_code_then_retry",
        "STU-02": "follow_pause_and_latest",
    }
    qa = tmp_path / "docs" / "qa"
    run = qa / "runs" / "shared-browser"
    run.mkdir(parents=True)
    evidence_path = run / "browser.json"
    source_files = []
    source_hashes = {}
    for relative, payload in {
        "web/student.js": b"student",
        "scripts/story_tracker.py": b"tracker",
        "scripts/qa_browser.py": b"browser",
        "scripts/qa_evidence_manifest.py": b"manifest",
        "docs/qa/stories.tsv": b"catalog",
    }.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        source_files.append(path)
        source_hashes[relative] = tracker.hashlib.sha256(payload).hexdigest()
    _write_browser_envelope(
        run,
        source_hashes=source_hashes,
        payload={
            "finished_at": "2026-09-08T12:00:00+09:00",
            "results": [
                {"story": story, "scenario": scenario, "status": "pass"}
                for story, scenario in scenarios.items()
            ],
        },
    )
    reference = evidence_path.relative_to(tmp_path).as_posix()
    cells = {}
    for number, story in enumerate(scenarios, start=15):
        cells.update(
            {
                f"A{number}": story,
                f"F{number}": "合格",
                f"M{number}": f"{reference} #{story}",
                f"N{number}": "",
            }
        )
    _audit_test_workspace(tmp_path, monkeypatch, cells)
    monkeypatch.setattr(
        tracker, "browser_source_files", lambda *, native: list(source_files)
    )
    hash_calls = 0
    load_calls = 0

    def snapshot_supplied_source_hashes(_root, _supplier):
        nonlocal hash_calls
        hash_calls += 1
        return tracker.SourceHashSnapshot(dict(source_hashes), {})

    real_load = tracker.load_sealed_browser_evidence

    def load_sealed(*args, **kwargs):
        nonlocal load_calls
        load_calls += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(
        tracker, "snapshot_supplied_source_hashes", snapshot_supplied_source_hashes
    )
    monkeypatch.setattr(
        tracker, "_source_snapshot_revisions_are_current", lambda *_args: True
    )
    monkeypatch.setattr(tracker, "load_sealed_browser_evidence", load_sealed)

    tracker.audit()

    assert hash_calls == 4
    assert load_calls == 2


def test_audit_reuses_one_performance_run_once_per_freshness_pass(
    tmp_path, monkeypatch
) -> None:
    stories = ("PERF-03", "PERF-05", "PERF-06")
    monkeypatch.setattr(
        tracker, "AUDITED_PERFORMANCE", {story: "soak" for story in stories}
    )
    qa = tmp_path / "docs" / "qa"
    run = qa / "runs" / "shared-soak"
    run.mkdir(parents=True)
    report_path = run / "acceptance.json"
    report_path.write_text("{}", encoding="utf-8")
    (run / tracker.PERFORMANCE_MANIFEST_NAME).write_text("{}", encoding="utf-8")
    evidence_path = run / "story-evidence.json"
    evidence_path.write_text(
        json.dumps({"source_report": report_path.relative_to(tmp_path).as_posix()}),
        encoding="utf-8",
    )
    reference = evidence_path.relative_to(tmp_path).as_posix()
    cells = {}
    for number, story in enumerate(stories, start=15):
        cells.update(
            {
                f"A{number}": story,
                f"F{number}": "合格",
                f"M{number}": f"{reference} #{story}",
                f"N{number}": "",
            }
        )
    _audit_test_workspace(tmp_path, monkeypatch, cells)
    validation_calls = 0

    def validate(*_args, **_kwargs):
        nonlocal validation_calls
        validation_calls += 1
        return {"artifacts": {report_path.name: "sealed"}}

    monkeypatch.setattr(tracker, "validate_performance_manifest", validate)
    monkeypatch.setattr(tracker, "_load_sealed_performance_json", lambda *_args: {})
    monkeypatch.setattr(
        tracker,
        "_audit_soak_report",
        lambda *_args: [
            {
                "qualified": {story: True for story in stories},
                "evaluations": {story: True for story in stories},
            }
        ],
    )

    tracker.audit()

    assert validation_calls == 4
