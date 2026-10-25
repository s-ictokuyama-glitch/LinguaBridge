from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from scripts.qa_evidence_manifest import load_sealed_browser_evidence


class _Expectation:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Page:
    def set_default_timeout(self, _timeout):
        pass

    def add_init_script(self, _script):
        pass

    def route(self, _pattern, _handler):
        pass

    def on(self, _event, _handler):
        pass

    def goto(self, _url):
        pass

    def locator(self, _selector):
        return object()


class _Context:
    def __init__(self):
        self.page = _Page()

    def new_page(self):
        return self.page

    def close(self):
        pass


class _CloseFailingBrowser:
    version = "Test Edge 1"

    def new_context(self, **_options):
        return _Context()

    def close(self):
        raise RuntimeError("simulated browser close failure")


class _ClosingBrowser(_CloseFailingBrowser):
    def close(self):
        pass


class _SourceABABrowser(_ClosingBrowser):
    def __init__(self, source: Path):
        self.source = source

    def close(self):
        original = self.source.read_bytes()
        self.source.write_bytes(b"transient edit during browser run\n")
        restore = self.source.with_name(f".{self.source.name}.restore")
        restore.write_bytes(original)
        restore.replace(self.source)


class _PlaywrightContext:
    def __init__(self, browser):
        self.browser = browser

    def __enter__(self):
        return SimpleNamespace(
            chromium=SimpleNamespace(launch=lambda **_kwargs: self.browser)
        )

    def __exit__(self, _exc_type, _exc, _tb):
        return False


def _load_runners(monkeypatch):
    playwright = ModuleType("playwright")
    playwright.__path__ = []
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: None
    sync_api.expect = lambda _target: _Expectation()
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.delitem(sys.modules, "scripts.qa_native_browser", raising=False)
    monkeypatch.delitem(sys.modules, "scripts.qa_browser", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    controlled = importlib.import_module("scripts.qa_browser")
    native = importlib.import_module("scripts.qa_native_browser")
    return controlled, native


def _prepare_runner_root(root: Path) -> None:
    for relative, value in {
        "server/main.py": b"server\n",
        "server/readme.txt": b"not python\n",
        "server/__pycache__/generated.py": b"scratch\n",
        "web/student.js": b"student\n",
        "web/vendor/qrcode.min.js": b"qr\n",
        "web/.scratch/generated.js": b"scratch\n",
        "scripts/qa_browser.py": b"controlled runner\n",
        "scripts/qa_native_browser.py": b"native runner\n",
        "scripts/qa_evidence_manifest.py": b"evidence helper\n",
        "scripts/story_tracker.py": b"consumer\n",
        "tests/conftest.py": b"fixtures\n",
        "docs/qa/stories.tsv": b"story\n",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)


def test_runners_bind_repository_imports_before_executing_them(monkeypatch) -> None:
    controlled, native = _load_runners(monkeypatch)
    controlled_names = {
        path.relative_to(controlled.ROOT).as_posix()
        for path in controlled._EARLY_IMPORT_CODE_FILES
    }
    native_names = {
        path.relative_to(native.ROOT).as_posix()
        for path in native._EARLY_IMPORT_CODE_FILES
    }

    assert controlled_names == {
        "scripts/qa_browser.py",
        "scripts/qa_evidence_manifest.py",
    }
    assert {
        "scripts/qa_native_browser.py",
        "scripts/qa_browser.py",
        "scripts/qa_evidence_manifest.py",
        "tests/conftest.py",
    } <= native_names
    assert any(name.startswith("server/") for name in native_names)
    assert set(native._loaded_native_repo_modules()) <= set(
        native._EARLY_IMPORT_CODE_FILES
    )
    controlled_cache = Path(controlled._IMPORT_PYCACHE.name)
    native_cache = Path(native._IMPORT_PYCACHE.name)
    assert controlled_cache.is_dir()
    assert native_cache.is_dir()
    assert controlled_cache != native_cache
    assert controlled.sys.dont_write_bytecode is True
    assert native.sys.dont_write_bytecode is True
    assert native.sys.pycache_prefix == native._IMPORT_PYCACHE.name
    assert list(controlled_cache.iterdir()) == []
    assert list(native_cache.iterdir()) == []
    for runner, import_statement in (
        (controlled, "from scripts.qa_evidence_manifest import ("),
        (native, "from scripts.qa_browser import MOCK"),
    ):
        source = Path(runner.__file__).read_text(encoding="utf-8")
        assert source.index("sys.pycache_prefix = _IMPORT_PYCACHE.name") < source.index(
            import_statement
        )


@pytest.mark.parametrize("runner_name", ["controlled", "native"])
def test_runner_import_guard_rejects_a_nonempty_bytecode_cache(
    runner_name: str, monkeypatch
) -> None:
    controlled, native = _load_runners(monkeypatch)
    runner = controlled if runner_name == "controlled" else native
    cache_root = Path(runner.sys.pycache_prefix)
    poison = cache_root / "stale.pyc"
    poison.write_bytes(b"untrusted cached code")
    try:
        assert not runner._import_code_is_current()
    finally:
        poison.unlink()


@pytest.mark.parametrize("runner_name", ["controlled", "native"])
def test_runner_rejects_code_changed_after_import_before_output(
    runner_name: str, tmp_path: Path, monkeypatch
) -> None:
    controlled, native = _load_runners(monkeypatch)
    runner = controlled if runner_name == "controlled" else native
    script = "qa_browser.py" if runner_name == "controlled" else "qa_native_browser.py"
    root = tmp_path / "repo"
    _prepare_runner_root(root)
    imported_files = (
        root / f"scripts/{script}",
        root / "scripts/qa_evidence_manifest.py",
    )
    imported = runner.snapshot_source_hashes(root, imported_files)
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "_IMPORT_ROOT", root)
    monkeypatch.setattr(runner, "_IMPORT_CODE_FILES", imported_files)
    monkeypatch.setattr(runner, "_IMPORT_CODE_SNAPSHOT", imported)
    (root / f"scripts/{script}").write_bytes(b"changed after import\n")
    monkeypatch.setattr(sys, "argv", [script])

    assert runner.main() == 2
    assert not (root / "docs/qa/runs").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ChangeTime regression")
@pytest.mark.parametrize("runner_name", ["controlled", "native"])
def test_runner_early_binding_rejects_in_place_aba_during_read(
    runner_name: str, tmp_path: Path, monkeypatch
) -> None:
    controlled, native = _load_runners(monkeypatch)
    runner = controlled if runner_name == "controlled" else native
    source = tmp_path / f"{runner_name}.py"
    source.write_bytes(b"original\n")
    real_read = Path.read_bytes
    injected = False

    def read_then_restore_metadata(path: Path) -> bytes:
        nonlocal injected
        value = real_read(path)
        if path == source and not injected:
            injected = True
            before = source.stat()
            original = value
            changed = bytes([original[0] ^ 1]) + original[1:]
            with source.open("r+b") as handle:
                handle.write(changed)
                handle.flush()
                os.fsync(handle.fileno())
                handle.seek(0)
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            os.utime(
                source, ns=(before.st_atime_ns, before.st_mtime_ns)
            )
        return value

    monkeypatch.setattr(Path, "read_bytes", read_then_restore_metadata)

    with pytest.raises(RuntimeError, match="changed during import"):
        runner._early_self_state(source)

    assert injected


@pytest.mark.parametrize("runner_name", ["controlled", "native"])
def test_runner_early_binding_rejects_linked_source_ancestor(
    runner_name: str, tmp_path: Path, monkeypatch
) -> None:
    controlled, native = _load_runners(monkeypatch)
    runner = controlled if runner_name == "controlled" else native
    root = tmp_path / "repo"
    source = root / "scripts/runner.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"runner\n")
    linked_parent = source.parent
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == linked_parent or real_is_symlink(path),
    )

    with pytest.raises(RuntimeError, match="unsafe ancestor"):
        runner._early_self_state(source, root=root)


def test_native_preimport_walk_rejects_linked_server_directory(
    tmp_path: Path, monkeypatch
) -> None:
    _controlled, native = _load_runners(monkeypatch)
    root = tmp_path / "repo"
    linked_directory = root / "server/linked"
    linked_directory.mkdir(parents=True)
    (linked_directory / "module.py").write_bytes(b"value = 1\n")
    real_isjunction = getattr(os.path, "isjunction", lambda _path: False)
    monkeypatch.setattr(
        os.path,
        "isjunction",
        lambda path: Path(path) == linked_directory or real_isjunction(path),
    )

    with pytest.raises(RuntimeError, match="contains a link"):
        native._early_native_import_files(root)


def test_controlled_runner_close_failure_is_an_unsealed_harness_error(
    tmp_path: Path, monkeypatch,
) -> None:
    controlled, _native = _load_runners(monkeypatch)
    root = tmp_path / "repo"
    _prepare_runner_root(root)
    monkeypatch.setattr(controlled, "ROOT", root)
    monkeypatch.setattr(controlled, "__file__", str(root / "scripts/qa_browser.py"))
    monkeypatch.setattr(
        controlled,
        "sync_playwright",
        lambda: _PlaywrightContext(_CloseFailingBrowser()),
    )
    monkeypatch.setattr(controlled, "expect", lambda _target: _Expectation())
    monkeypatch.setattr(
        controlled,
        "CASES",
        [
            (
                "JOIN-01",
                "controlled_close_failure",
                "student",
                True,
                lambda _page: {"case_completed": True},
                {},
                "controlled test",
            )
        ],
    )
    monkeypatch.setattr(sys, "argv", ["qa_browser.py"])

    assert controlled.main() == 2

    run = next((root / "docs/qa/runs").glob("*-browser"))
    payload = json.loads((run / "browser.json").read_text(encoding="utf-8"))
    assert payload["results"] == [
        {
            "story": "JOIN-01",
            "scenario": "controlled_close_failure",
            "complete": True,
            "scope": "controlled test",
            "observation": {"case_completed": True},
            "status": "pass",
        }
    ]
    assert "simulated browser close failure" in payload["browser_close_error"]
    assert not (run / "source-hashes.json").exists()


def test_native_runner_close_failure_is_an_unsealed_harness_error(
    tmp_path: Path, monkeypatch,
) -> None:
    _controlled, native = _load_runners(monkeypatch)
    root = tmp_path / "repo"
    _prepare_runner_root(root)
    monkeypatch.setattr(native, "ROOT", root)
    monkeypatch.setattr(native, "__file__", str(root / "scripts/qa_native_browser.py"))
    monkeypatch.setattr(
        native,
        "sync_playwright",
        lambda: _PlaywrightContext(_CloseFailingBrowser()),
    )
    monkeypatch.setattr(sys, "argv", ["qa_native_browser.py", "--story", "NO-STORY"])

    assert native.main() == 2

    run = next((root / "docs/qa/runs").glob("*-native-browser"))
    payload = json.loads((run / "browser.json").read_text(encoding="utf-8"))
    assert payload["results"] == []
    assert "simulated browser close failure" in payload["browser_close_error"]
    assert not (run / "source-hashes.json").exists()


@pytest.mark.parametrize("runner_name", ["controlled", "native"])
def test_runner_start_source_failure_is_an_unsealed_harness_error(
    tmp_path: Path, monkeypatch, runner_name: str,
) -> None:
    controlled, native = _load_runners(monkeypatch)
    runner = controlled if runner_name == "controlled" else native
    root = tmp_path / "repo"
    _prepare_runner_root(root)
    script = "qa_browser.py" if runner_name == "controlled" else "qa_native_browser.py"
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "__file__", str(root / "scripts" / script))

    def fail_snapshot(_root, _sources):
        raise runner.EvidenceManifestError("simulated invalid start source")

    monkeypatch.setattr(runner, "snapshot_supplied_source_hashes", fail_snapshot)
    monkeypatch.setattr(sys, "argv", [script])

    assert runner.main() == 2

    suffix = "*-browser" if runner_name == "controlled" else "*-native-browser"
    run = next((root / "docs/qa/runs").glob(suffix))
    payload = json.loads((run / "browser.json").read_text(encoding="utf-8"))
    assert payload["results"] == []
    assert "simulated invalid start source" in payload["source_snapshot_error"]
    assert not (run / "source-hashes.json").exists()


@pytest.mark.parametrize("runner_name", ["controlled", "native"])
def test_runner_rejects_an_output_ancestor_before_creating_the_run(
    tmp_path: Path, monkeypatch, runner_name: str,
) -> None:
    controlled, native = _load_runners(monkeypatch)
    runner = controlled if runner_name == "controlled" else native
    root = tmp_path / "repo"
    _prepare_runner_root(root)
    script = "qa_browser.py" if runner_name == "controlled" else "qa_native_browser.py"
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "__file__", str(root / "scripts" / script))
    inspected = []

    def reject_output(root_arg: Path, output_arg: Path) -> None:
        assert root_arg == root
        assert not output_arg.exists()
        inspected.append(output_arg)
        raise runner.EvidenceManifestError("simulated output ancestor junction")

    monkeypatch.setattr(runner, "validate_output_parent", reject_output)
    monkeypatch.setattr(sys, "argv", [script])

    assert runner.main() == 2

    assert len(inspected) == 1
    assert not (root / "docs/qa/runs").exists()


def test_controlled_runner_seals_its_exact_result_and_helper_source(
    tmp_path: Path, monkeypatch,
) -> None:
    controlled, _native = _load_runners(monkeypatch)
    root = tmp_path / "repo"
    _prepare_runner_root(root)
    monkeypatch.setattr(controlled, "ROOT", root)
    monkeypatch.setattr(controlled, "__file__", str(root / "scripts/qa_browser.py"))
    monkeypatch.setattr(
        controlled, "sync_playwright", lambda: _PlaywrightContext(_ClosingBrowser())
    )
    monkeypatch.setattr(controlled, "expect", lambda _target: _Expectation())
    monkeypatch.setattr(
        controlled,
        "CASES",
        [
            (
                "JOIN-01",
                "controlled_success",
                "student",
                True,
                lambda _page: {"case_completed": True},
                {},
                "controlled test",
            )
        ],
    )
    monkeypatch.setattr(sys, "argv", ["qa_browser.py"])

    assert controlled.main() == 0

    run = next((root / "docs/qa/runs").glob("*-browser"))
    sealed = load_sealed_browser_evidence(run, expected_kind="browser")
    assert set(sealed.manifest) == {
        "schema_version",
        "kind",
        "complete",
        "source_hashes",
        "artifacts",
    }
    assert "scripts/qa_evidence_manifest.py" in sealed.manifest["source_hashes"]
    assert set(sealed.manifest["source_hashes"]) == {
        "web/student.js",
        "web/vendor/qrcode.min.js",
        "scripts/qa_browser.py",
        "scripts/qa_evidence_manifest.py",
        "scripts/story_tracker.py",
        "docs/qa/stories.tsv",
    }
    assert sealed.browser_payload["results"][0]["status"] == "pass"


def test_native_runner_uses_the_native_envelope_kind_and_helper_source(
    tmp_path: Path, monkeypatch,
) -> None:
    _controlled, native = _load_runners(monkeypatch)
    root = tmp_path / "repo"
    _prepare_runner_root(root)
    monkeypatch.setattr(native, "ROOT", root)
    monkeypatch.setattr(native, "__file__", str(root / "scripts/qa_native_browser.py"))
    monkeypatch.setattr(
        native, "sync_playwright", lambda: _PlaywrightContext(_ClosingBrowser())
    )
    monkeypatch.setattr(sys, "argv", ["qa_native_browser.py", "--story", "NO-STORY"])

    assert native.main() == 2

    run = next((root / "docs/qa/runs").glob("*-native-browser"))
    sealed = load_sealed_browser_evidence(run, expected_kind="native-browser")
    assert sealed.manifest["kind"] == "native-browser"
    assert "scripts/qa_evidence_manifest.py" in sealed.manifest["source_hashes"]
    assert set(sealed.manifest["source_hashes"]) == {
        "server/main.py",
        "web/student.js",
        "web/vendor/qrcode.min.js",
        "scripts/qa_native_browser.py",
        "scripts/qa_browser.py",
        "scripts/qa_evidence_manifest.py",
        "scripts/story_tracker.py",
        "tests/conftest.py",
        "docs/qa/stories.tsv",
    }
    assert sealed.browser_payload["results"] == []


@pytest.mark.parametrize("drift_kind", ["content", "added-file"])
def test_controlled_runner_keeps_a_source_drift_result_unsealed(
    drift_kind: str, tmp_path: Path, monkeypatch,
) -> None:
    controlled, _native = _load_runners(monkeypatch)
    root = tmp_path / "repo"
    _prepare_runner_root(root)
    monkeypatch.setattr(controlled, "ROOT", root)
    monkeypatch.setattr(controlled, "__file__", str(root / "scripts/qa_browser.py"))
    monkeypatch.setattr(
        controlled, "sync_playwright", lambda: _PlaywrightContext(_ClosingBrowser())
    )
    monkeypatch.setattr(controlled, "expect", lambda _target: _Expectation())

    def mutate_source(_page):
        if drift_kind == "content":
            (root / "web/student.js").write_bytes(b"changed during run\n")
        else:
            (root / "web/added.js").write_bytes(b"added during run\n")
        return {"case_completed": True}

    monkeypatch.setattr(
        controlled,
        "CASES",
        [
            (
                "JOIN-01",
                "controlled_source_drift",
                "student",
                True,
                mutate_source,
                {},
                "controlled test",
            )
        ],
    )
    monkeypatch.setattr(sys, "argv", ["qa_browser.py"])

    assert controlled.main() == 2

    run = next((root / "docs/qa/runs").glob("*-browser"))
    assert (run / "browser.json").is_file()
    assert not (run / "source-hashes.json").exists()


@pytest.mark.parametrize("runner_name", ["controlled", "native"])
def test_runner_keeps_source_aba_result_unsealed(
    tmp_path: Path, monkeypatch, runner_name: str,
) -> None:
    controlled, native = _load_runners(monkeypatch)
    runner = controlled if runner_name == "controlled" else native
    root = tmp_path / "repo"
    _prepare_runner_root(root)
    source = root / "web/student.js"
    original = source.read_bytes()
    script = "qa_browser.py" if runner_name == "controlled" else "qa_native_browser.py"
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "__file__", str(root / "scripts" / script))
    monkeypatch.setattr(
        runner,
        "sync_playwright",
        lambda: _PlaywrightContext(_SourceABABrowser(source)),
    )
    if runner_name == "controlled":
        monkeypatch.setattr(runner, "CASES", [])
        monkeypatch.setattr(runner, "expect", lambda _target: _Expectation())
        argv = [script]
    else:
        argv = [script, "--story", "NO-STORY"]
    monkeypatch.setattr(sys, "argv", argv)

    assert runner.main() == 2

    suffix = "*-browser" if runner_name == "controlled" else "*-native-browser"
    run = next((root / "docs/qa/runs").glob(suffix))
    assert source.read_bytes() == original
    assert (run / "browser.json").is_file()
    assert not (run / "source-hashes.json").exists()


@pytest.mark.parametrize("snapshot_failure", ["missing", "link"])
def test_controlled_runner_keeps_an_invalid_end_source_unsealed(
    tmp_path: Path, monkeypatch, snapshot_failure: str,
) -> None:
    controlled, _native = _load_runners(monkeypatch)
    root = tmp_path / "repo"
    _prepare_runner_root(root)
    source = root / "web/student.js"
    monkeypatch.setattr(controlled, "ROOT", root)
    monkeypatch.setattr(controlled, "__file__", str(root / "scripts/qa_browser.py"))
    monkeypatch.setattr(
        controlled, "sync_playwright", lambda: _PlaywrightContext(_ClosingBrowser())
    )
    monkeypatch.setattr(controlled, "expect", lambda _target: _Expectation())
    mark_as_link = False
    real_is_symlink = Path.is_symlink

    if snapshot_failure == "link":
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda path: (mark_as_link and path == source) or real_is_symlink(path),
        )

    def invalidate_source(_page):
        nonlocal mark_as_link
        if snapshot_failure == "missing":
            source.unlink()
        else:
            mark_as_link = True
        return {"case_completed": True}

    monkeypatch.setattr(
        controlled,
        "CASES",
        [
            (
                "JOIN-01",
                f"controlled_source_{snapshot_failure}",
                "student",
                True,
                invalidate_source,
                {},
                "controlled test",
            )
        ],
    )
    monkeypatch.setattr(sys, "argv", ["qa_browser.py"])

    assert controlled.main() == 2

    run = next((root / "docs/qa/runs").glob("*-browser"))
    assert (run / "browser.json").is_file()
    assert not (run / "source-hashes.json").exists()
