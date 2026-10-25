"""Performance manifests bind a measured report to its exact producing state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys

import pytest

import scripts.performance_evidence as evidence
from scripts.performance_evidence import (
    MANIFEST_NAME,
    PerformanceEvidenceError,
    ValidatedPerformanceManifest,
    begin_performance_run,
    complete_performance_run,
    recheck_validation_witness,
    release_performance_run_locks,
    validate_performance_manifest,
)


def test_performance_api_ignores_a_valid_stale_timestamp_pyc(tmp_path: Path) -> None:
    isolated_root = tmp_path / "isolated-performance"
    scripts = isolated_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(evidence.__file__, scripts / "performance_evidence.py")

    dependency = scripts / "yaml.py"
    stale = "raise RuntimeError('STALE')\n"
    fresh = "raise RuntimeError('FRESH')\n"
    assert len(stale) == len(fresh)
    timestamp = 1_700_000_000
    dependency.write_text(stale, encoding="utf-8")
    os.utime(dependency, (timestamp, timestamp))
    cache = dependency.parent / "__pycache__" / (
        f"{dependency.stem}.{sys.implementation.cache_tag}.pyc"
    )
    cache.parent.mkdir()
    py_compile.compile(str(dependency), cfile=str(cache), doraise=True)
    dependency.write_text(fresh, encoding="utf-8")
    os.utime(dependency, (timestamp, timestamp))
    environment = os.environ.copy()
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    result = subprocess.run(
        [sys.executable, str(scripts / "performance_evidence.py")],
        cwd=isolated_root,
        env=environment,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert result.stderr.rstrip().endswith(b"RuntimeError: FRESH")


def test_python_child_environment_is_fresh_unique_and_ignores_stale_pyc(
    tmp_path: Path, monkeypatch,
) -> None:
    dependency = tmp_path / "child_dependency.py"
    stale = "VALUE = 'STALE'\n"
    fresh = "VALUE = 'FRESH'\n"
    assert len(stale) == len(fresh)
    timestamp = 1_700_000_000
    dependency.write_text(stale, encoding="utf-8")
    os.utime(dependency, (timestamp, timestamp))
    cache = dependency.parent / "__pycache__" / (
        f"{dependency.stem}.{sys.implementation.cache_tag}.pyc"
    )
    cache.parent.mkdir()
    py_compile.compile(str(dependency), cfile=str(cache), doraise=True)
    dependency.write_text(fresh, encoding="utf-8")
    os.utime(dependency, (timestamp, timestamp))
    (tmp_path / "child_probe.py").write_text(
        "import child_dependency; print(child_dependency.VALUE)\n",
        encoding="utf-8",
    )

    inherited = tmp_path / "inherited-prefix"
    inherited.mkdir()
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(inherited))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    prefixes: list[Path] = []
    for _ in range(2):
        with evidence.fresh_python_child_environment() as environment:
            assert "PYTHONPYCACHEPREFIX" not in environment
            assert "PYTHONDONTWRITEBYTECODE" not in environment
            prefix = Path(environment["LINGUABRIDGE_CHILD_PYCACHE_PREFIX"])
            prefixes.append(prefix)
            assert prefix != inherited
            assert prefix.is_dir() and not prefix.is_symlink()
            assert list(prefix.iterdir()) == []
            command = evidence.isolated_python_module_command(
                "child_probe",
                root=tmp_path,
                environment=environment,
            )
            result = subprocess.run(
                command,
                cwd=tmp_path,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            assert result.stdout.strip() == "FRESH"
            assert list(prefix.iterdir()) == []
        assert not prefix.exists()

    assert prefixes[0] != prefixes[1]


def test_safe_cache_prefix_rejects_a_linked_ancestor(tmp_path: Path) -> None:
    backing = tmp_path / "backing"
    prefix = backing / "cache"
    prefix.mkdir(parents=True)
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(backing, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    assert evidence._safe_empty_cache_prefix(linked / "cache") is None


def test_safe_cache_prefix_rejects_nonempty_state_during_inspection(
    tmp_path: Path, monkeypatch,
) -> None:
    prefix = tmp_path / "cache"
    prefix.mkdir()
    original_iterdir = Path.iterdir
    injected = False

    def inject_before_enumeration(path: Path):
        nonlocal injected
        if path == prefix and not injected:
            injected = True
            (prefix / "attacker.pyc").write_bytes(b"untrusted")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", inject_before_enumeration)

    assert evidence._safe_empty_cache_prefix(prefix) is None


class _FixtureDistribution:
    version = "1.0"

    def __init__(self, site_packages: Path) -> None:
        self._site_packages = site_packages
        self.metadata = {"Name": "fixture-runtime"}
        self.files = (
            Path("fixture_runtime/__init__.py"),
            Path("fixture_runtime/lazy_backend.py"),
            Path("fixture_runtime/native_backend.pyd"),
            Path("fixture_runtime.pth"),
            Path("fixture_runtime-1.0.dist-info/METADATA"),
            Path("fixture_runtime-1.0.dist-info/RECORD"),
            Path("fixture_runtime-1.0.dist-info/direct_url.json"),
            Path("fixture_runtime-1.0.dist-info/top_level.txt"),
        )

    def locate_file(self, entry: Path) -> Path:
        return self._site_packages / entry


class _NamedFixtureDistribution:
    def __init__(
        self,
        site_packages: Path,
        name: str,
        *,
        version: str = "1.0",
        requirements: tuple[str, ...] = (),
        extra_files: tuple[Path, ...] = (),
    ) -> None:
        self._site_packages = site_packages
        self.version = version
        self.metadata = {"Name": name}
        normalized = name.replace("-", "_")
        package = site_packages / normalized
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "native_backend.pyd").write_bytes(f"native:{name}".encode())
        metadata_dir = site_packages / f"{normalized}-{version}.dist-info"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        metadata_lines = [f"Name: {name}", f"Version: {version}"]
        metadata_lines.extend(f"Requires-Dist: {item}" for item in requirements)
        (metadata_dir / "METADATA").write_text(
            "\n".join(metadata_lines) + "\n", encoding="utf-8"
        )
        (metadata_dir / "RECORD").write_text("fixture record\n", encoding="utf-8")
        self.files = (
            Path(f"{normalized}/__init__.py"),
            Path(f"{normalized}/native_backend.pyd"),
            Path(f"{normalized}-{version}.dist-info/METADATA"),
            Path(f"{normalized}-{version}.dist-info/RECORD"),
            *extra_files,
        )

    def locate_file(self, entry: Path) -> Path:
        return self._site_packages / entry


@pytest.fixture(autouse=True)
def fixture_runtime_distribution(tmp_path, monkeypatch):
    """Keep provenance tests small while retaining real package-file semantics."""

    monkeypatch.setattr(evidence, "_QUALIFIED_PRODUCER_BOOTSTRAP", True)
    monkeypatch.setattr(evidence, "_PRELOADED_CRITICAL_MODULES", frozenset())
    monkeypatch.setattr(
        evidence.sys, "pycache_prefix", str(evidence._IMPORT_PYCACHE_ROOT)
    )
    monkeypatch.setattr(evidence.sys, "dont_write_bytecode", True)

    site_packages = tmp_path / "site-packages"
    implementation = site_packages / "fixture_runtime" / "lazy_backend.py"
    package_init = site_packages / "fixture_runtime" / "__init__.py"
    metadata_dir = site_packages / "fixture_runtime-1.0.dist-info"
    metadata_dir.mkdir(parents=True)
    implementation.parent.mkdir(parents=True)
    package_init.write_text("", encoding="utf-8")
    implementation.write_text("LAZY_VALUE = 'A'\n", encoding="utf-8")
    (implementation.parent / "native_backend.pyd").write_bytes(b"native fixture")
    pth_target = site_packages / "editable-src"
    pth_target.mkdir()
    (pth_target / "pth_backend.py").write_text("PTH_VALUE = 'A'\n", encoding="utf-8")
    (site_packages / "fixture_runtime.pth").write_text(
        "editable-src\n", encoding="utf-8"
    )
    editable_checkout = tmp_path / "editable-checkout"
    editable_package = editable_checkout / "fixture_runtime_editable"
    editable_package.mkdir(parents=True)
    (editable_package / "lazy_backend.py").write_text(
        "EDITABLE_VALUE = 'A'\n", encoding="utf-8"
    )
    (metadata_dir / "METADATA").write_text(
        "Name: fixture-runtime\nVersion: 1.0\n", encoding="utf-8"
    )
    (metadata_dir / "RECORD").write_text("fixture record\n", encoding="utf-8")
    (metadata_dir / "direct_url.json").write_text(
        json.dumps(
            {
                "url": editable_checkout.as_uri(),
                "dir_info": {"editable": True},
            }
        ),
        encoding="utf-8",
    )
    (metadata_dir / "top_level.txt").write_text(
        "fixture_runtime_editable\n", encoding="utf-8"
    )
    distribution = _FixtureDistribution(site_packages)
    monkeypatch.setattr(evidence.metadata, "distributions", lambda: [distribution])
    monkeypatch.setattr(evidence, "_COMMON_DISTRIBUTIONS", {"fixture-runtime"})
    monkeypatch.setattr(
        evidence,
        "_ASR_DISTRIBUTIONS",
        {name: set() for name in evidence._ASR_DISTRIBUTIONS},
    )
    monkeypatch.setattr(
        evidence,
        "_MT_DISTRIBUTIONS",
        {name: set() for name in evidence._MT_DISTRIBUTIONS},
    )
    return implementation


def test_producer_guard_rejects_an_unqualified_interpreter(monkeypatch) -> None:
    monkeypatch.setattr(evidence, "_QUALIFIED_PRODUCER_BOOTSTRAP", False)

    with pytest.raises(PerformanceEvidenceError, match="fresh -I -S"):
        evidence.require_qualified_producer_bootstrap()


def test_producer_guard_rejects_a_preloaded_critical_module(monkeypatch) -> None:
    monkeypatch.setattr(
        evidence, "_PRELOADED_CRITICAL_MODULES", frozenset({"yaml"})
    )

    with pytest.raises(PerformanceEvidenceError, match="fresh -I -S"):
        evidence.require_qualified_producer_bootstrap()


def test_producer_guard_rejects_a_different_safe_empty_cache_prefix(
    tmp_path: Path, monkeypatch,
) -> None:
    replacement = tmp_path / "replacement-cache"
    replacement.mkdir()
    monkeypatch.setattr(evidence.sys, "pycache_prefix", str(replacement))

    with pytest.raises(PerformanceEvidenceError, match="import cache changed"):
        evidence.require_qualified_producer_bootstrap()


def test_runtime_distribution_closure_honors_markers_extras_and_native_files(
    tmp_path: Path, monkeypatch,
) -> None:
    site_packages = tmp_path / "closure-site"
    site_packages.mkdir()
    root = _NamedFixtureDistribution(
        site_packages,
        "fixture-root",
        requirements=(
            "fixture-child>=1",
            "fixture-base; extra == ''",
            "fixture-extra; extra == 'standard'",
            "fixture-skipped; python_version < '2'",
        ),
    )
    child = _NamedFixtureDistribution(site_packages, "fixture-child")
    base = _NamedFixtureDistribution(site_packages, "fixture-base")
    extra = _NamedFixtureDistribution(site_packages, "fixture-extra")
    skipped = _NamedFixtureDistribution(site_packages, "fixture-skipped")
    monkeypatch.setattr(
        evidence.metadata,
        "distributions",
        lambda: [root, child, base, extra, skipped],
    )
    monkeypatch.setattr(evidence, "_DISTRIBUTION_EXTRAS", {"fixture-root": {"standard"}})

    files = evidence._active_distribution_files({"fixture-root"})

    assert sorted(files) == [
        "fixture-base",
        "fixture-child",
        "fixture-extra",
        "fixture-root",
    ]
    assert any(path.suffix == ".pyd" for path in files["fixture-child"])


def test_runtime_distribution_closure_rejects_unsatisfied_dependency_version(
    tmp_path: Path, monkeypatch,
) -> None:
    site_packages = tmp_path / "version-site"
    site_packages.mkdir()
    root = _NamedFixtureDistribution(
        site_packages,
        "fixture-root",
        requirements=("fixture-child>=2",),
    )
    child = _NamedFixtureDistribution(site_packages, "fixture-child", version="1.0")
    monkeypatch.setattr(evidence.metadata, "distributions", lambda: [root, child])

    with pytest.raises(PerformanceEvidenceError, match="does not satisfy"):
        evidence._active_distribution_files({"fixture-root"})


def test_startup_pth_owner_and_static_import_are_in_the_active_closure(
    tmp_path: Path, monkeypatch,
) -> None:
    site_packages = tmp_path / "pth-site"
    site_packages.mkdir()
    (site_packages / "fixture_bootstrap.py").write_text(
        "VALUE = 'bound'\n", encoding="utf-8"
    )
    pth = site_packages / "fixture_bootstrap.pth"
    pth.write_text("import fixture_bootstrap\n", encoding="utf-8")
    owner = _NamedFixtureDistribution(
        site_packages,
        "fixture-owner",
        extra_files=(Path("fixture_bootstrap.py"), Path("fixture_bootstrap.pth")),
    )
    monkeypatch.setattr(evidence.metadata, "distributions", lambda: [owner])

    files = evidence._active_distribution_files(set())

    assert sorted(files) == ["fixture-owner"]
    assert pth in files["fixture-owner"]
    assert site_packages / "fixture_bootstrap.py" in files["fixture-owner"]


def test_startup_pth_rejects_unresolved_executable_code(
    tmp_path: Path, monkeypatch,
) -> None:
    site_packages = tmp_path / "unsafe-pth-site"
    site_packages.mkdir()
    (site_packages / "fixture_bootstrap.py").write_text(
        "def install(): pass\n", encoding="utf-8"
    )
    (site_packages / "fixture_bootstrap.pth").write_text(
        "import fixture_bootstrap; fixture_bootstrap.install()\n",
        encoding="utf-8",
    )
    owner = _NamedFixtureDistribution(
        site_packages,
        "fixture-owner",
        extra_files=(Path("fixture_bootstrap.py"), Path("fixture_bootstrap.pth")),
    )
    monkeypatch.setattr(evidence.metadata, "distributions", lambda: [owner])

    with pytest.raises(PerformanceEvidenceError, match="cannot be resolved safely"):
        evidence._active_distribution_files(set())


def test_startup_pth_rejects_an_unowned_file(
    tmp_path: Path, monkeypatch,
) -> None:
    site_packages = tmp_path / "unowned-pth-site"
    site_packages.mkdir()
    (site_packages / "rogue.pth").write_text("ignored-target\n", encoding="utf-8")
    owner = _NamedFixtureDistribution(site_packages, "fixture-owner")
    monkeypatch.setattr(evidence.metadata, "distributions", lambda: [owner])

    with pytest.raises(PerformanceEvidenceError, match="owner is missing"):
        evidence._active_distribution_files({"fixture-owner"})


def make_root(tmp_path):
    for relative in (
        "scripts/__init__.py",
        "scripts/acceptance.py",
        "scripts/bench.py",
        "scripts/performance_evidence.py",
        "scripts/replay_client.py",
        "scripts/story_tracker.py",
        "scripts/scale_bench.py",
        "scripts/text_metrics.py",
        "server/main.py",
        "config.yaml",
        "docs/qa/stories.tsv",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"content:{relative}", encoding="utf-8")
    models = tmp_path / "models"
    sherpa = models / "reazonspeech-k2-v2"
    sherpa.mkdir(parents=True)
    (sherpa / "weights.onnx").write_bytes(b"sherpa weights")
    (sherpa / "tokens.txt").write_bytes(b"sherpa tokenizer")
    hymt = models / "hy-mt2" / "Hy-MT2-1.8B-Q4_K_M.gguf"
    hymt.parent.mkdir(parents=True)
    hymt.write_bytes(b"hy-mt2 weights and tokenizer")
    (tmp_path / "config.yaml").write_text(
        "\n".join([
            "models:",
            f"  dir: '{models.as_posix()}'",
            "server:",
            f"  cert_dir: '{(tmp_path / 'no-certs').as_posix()}'",
            "asr:",
            "  engine: sherpa",
            "  sherpa:",
            "    model: reazonspeech-k2-v2",
            "vad:",
            "  engine: energy",
            "mt:",
            "  engine: hy-mt2",
            "  hy_mt2:",
            "    gguf_path: hy-mt2/Hy-MT2-1.8B-Q4_K_M.gguf",
        ]),
        encoding="utf-8",
    )
    audio = tmp_path / "lesson.wav"
    audio.write_bytes(b"pcm fixture")
    return tmp_path, audio


def soak_args(root, audio):
    return {
        "minutes": 90.0,
        "students": 10,
        "audio": str(audio.relative_to(root)),
        "corpus": "ja_ext",
        "langs": ["en", "zh"],
        "disconnect_every_s": 60.0,
        "mt_cache_size": None,
        "engine": "hy-mt2",
        "asr": "sherpa",
        "partial": None,
        "port": 18431,
        "drain_seconds": 20.0,
        "out_dir": "runs/fresh-soak",
        "no_config_update": True,
    }


def prepare_config(root, run):
    config = run / "_work" / "accept-hy-mt2.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text((root / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return config


def seal_run(root, run, manifest):
    report = run / "2026-09-07-acceptance.json"
    markdown = run / "2026-09-07-acceptance.md"
    config = run / "_work" / "accept-hy-mt2.yaml"
    report.write_text('{"results": []}', encoding="utf-8")
    markdown.write_text("report", encoding="utf-8")
    payload = complete_performance_run(
        manifest,
        primary_report=report,
        artifacts=[report, markdown],
        effective_configs=[config],
        root=root,
        finished_at="2026-09-07T02:00:00+09:00",
    )
    return report, payload


def _install_read_race(monkeypatch, trigger, action) -> None:
    """Run ``action`` exactly when the validator reads ``trigger``."""

    original = evidence._stable_sha256
    fired = False

    def hash_with_race(path, **kwargs):
        nonlocal fired
        if Path(path).resolve() == trigger.resolve() and not fired:
            fired = True
            action()
        return original(path, **kwargs)

    monkeypatch.setattr(evidence, "_stable_sha256", hash_with_race)


def test_complete_manifest_records_arguments_times_and_all_hashes(tmp_path) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, sealed = seal_run(root, run, manifest)

    assert manifest == run / MANIFEST_NAME
    assert sealed["complete"] is True
    assert sealed["started_at"] == "2026-09-07T00:30:00+09:00"
    assert sealed["finished_at"] == "2026-09-07T02:00:00+09:00"
    assert sealed["arguments"]["disconnect_every_s"] == 60.0
    assert "scripts/replay_client.py" in sealed["source_hashes"]
    assert "scripts/__init__.py" in sealed["source_hashes"]
    assert "scripts/bench.py" in sealed["source_hashes"]
    assert "lesson.wav" in sealed["input_hashes"]
    assert any("reazonspeech-k2-v2" in path for path in sealed["runtime_asset_hashes"])
    assert any("Hy-MT2-1.8B-Q4_K_M.gguf" in path for path in sealed["runtime_asset_hashes"])
    assert sealed["runtime"]["distributions"]["fixture-runtime"]
    assert sealed["runtime"]["distributions"]["fixture-runtime"][
        "implementation_hashes"
    ]
    implementation_hashes = sealed["runtime"]["distributions"]["fixture-runtime"][
        "implementation_hashes"
    ]
    assert any(path.endswith("native_backend.pyd") for path in implementation_hashes)
    assert any("editable-src" in path for path in implementation_hashes)
    assert any("editable-checkout" in path for path in implementation_hashes)
    assert sealed["runtime"]["executable_sha256"]
    assert sealed["configuration"]["effective"]
    assert sealed["primary_report"] in sealed["artifacts"]
    assert sealed["continuous_lock"]["mode"] == (
        "windows-share-read" if os.name == "nt" else "posix-advisory"
    )
    assert sealed["continuous_lock"]["interrupted"] is False
    assert sealed["continuous_lock"]["released_at"] == sealed["finished_at"]
    assert any(path.endswith("2026-09-07-acceptance.json") for path in sealed["continuous_lock"]["files"])
    validated = validate_performance_manifest(
        run, kind="soak", primary_report=report, root=root
    )
    assert isinstance(validated, ValidatedPerformanceManifest)
    assert validated == sealed
    assert json.loads(json.dumps(validated)) == sealed
    assert validated.validation_witness.files
    assert validated.validation_witness.directories
    with pytest.raises(AttributeError, match="immutable"):
        validated.validation_witness = validated.validation_witness
    recheck_validation_witness(validated)
    recheck_validation_witness(validated.validation_witness)


def test_validator_rejects_source_or_report_changed_after_run(tmp_path) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, _ = seal_run(root, run, manifest)

    (root / "scripts" / "replay_client.py").write_text("changed", encoding="utf-8")
    with pytest.raises(PerformanceEvidenceError, match="source snapshot"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)

    (root / "scripts" / "replay_client.py").write_text(
        "content:scripts/replay_client.py", encoding="utf-8"
    )
    model = root / "models" / "hy-mt2" / "Hy-MT2-1.8B-Q4_K_M.gguf"
    model.write_bytes(b"different model")
    with pytest.raises(PerformanceEvidenceError, match="モデル/TLS資産snapshot"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)

    model.write_bytes(b"hy-mt2 weights and tokenizer")
    report.write_text("tampered", encoding="utf-8")
    with pytest.raises(PerformanceEvidenceError, match="artifact"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)


def test_validator_reaudits_sealed_report_when_only_tracker_changed_after_run(
    tmp_path,
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, sealed = seal_run(root, run, manifest)

    (root / "scripts/story_tracker.py").write_text(
        "current audit rules", encoding="utf-8"
    )

    assert validate_performance_manifest(
        run, kind="soak", primary_report=report, root=root
    ) == sealed


def test_validator_rejects_performance_evidence_producer_changed_after_run(
    tmp_path,
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, _ = seal_run(root, run, manifest)
    (root / "scripts" / "performance_evidence.py").write_text(
        "changed producer", encoding="utf-8"
    )

    with pytest.raises(PerformanceEvidenceError, match="source snapshot"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)


def test_qualified_run_rejects_posix_advisory_lock_mode(
    tmp_path, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    monkeypatch.setattr(evidence, "_lock_mode", lambda: "posix-advisory")

    with pytest.raises(PerformanceEvidenceError, match="Windows mandatory"):
        begin_performance_run(
            run,
            kind="soak",
            arguments=soak_args(root, audio),
            effective_configs=[config],
            root=root,
        )


def test_validator_rejects_posix_advisory_lock_qualification(
    tmp_path, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
    )
    report, _ = seal_run(root, run, manifest)
    monkeypatch.setattr(evidence, "_lock_mode", lambda: "posix-advisory")

    with pytest.raises(PerformanceEvidenceError, match="Windows mandatory"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)


def test_run_cannot_be_sealed_if_source_changes_during_measurement(tmp_path) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
    )
    source = root / "config.yaml"
    moved = root / "config-moved.yaml"
    if os.name == "nt":
        with pytest.raises(PermissionError):
            source.write_text("changed during run", encoding="utf-8")
        with pytest.raises(PermissionError):
            source.rename(moved)
        release_performance_run_locks(manifest)
        source.rename(moved)
        moved.rename(source)
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        assert saved["complete"] is False
        assert saved["continuous_lock"]["interrupted"] is True
        with pytest.raises(PerformanceEvidenceError, match="read-lock"):
            complete_performance_run(
                manifest,
                primary_report=run / "missing.json",
                artifacts=[],
                effective_configs=[config],
                root=root,
            )
    else:
        source.write_text("changed during run", encoding="utf-8")
        report = run / "2026-09-07-acceptance.json"
        report.write_text("{}", encoding="utf-8")
        with pytest.raises(PerformanceEvidenceError, match="run中"):
            complete_performance_run(
                manifest,
                primary_report=report,
                artifacts=[report],
                effective_configs=[config],
                root=root,
            )
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        assert saved["complete"] is False
        assert saved["snapshot_changed_during_run"] == ["source"]


@pytest.mark.parametrize("change", ["delete_model_file", "rewrite_effective_config"])
def test_run_cannot_be_sealed_if_runtime_inputs_change(
    change, tmp_path
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
    )
    if change == "delete_model_file":
        target = root / "models" / "reazonspeech-k2-v2" / "tokens.txt"
        operation = target.unlink
        expected = "runtime_asset"
    else:
        operation = lambda: config.write_text(
            config.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8"
        )
        expected = "effective_config"
    if os.name == "nt":
        with pytest.raises(PermissionError):
            operation()
        release_performance_run_locks(manifest)
        operation()
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        assert saved["continuous_lock"]["interrupted"] is True
        return
    operation()
    report = run / "2026-09-07-acceptance.json"
    report.write_text("{}", encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match=expected):
        complete_performance_run(
            manifest,
            primary_report=report,
            artifacts=[report],
            effective_configs=[config],
            root=root,
        )

    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["complete"] is False
    assert expected in saved["snapshot_changed_during_run"]


def test_begin_refuses_to_overwrite_an_existing_run_manifest(tmp_path) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
    )
    original = manifest.read_bytes()

    try:
        with pytest.raises(PerformanceEvidenceError, match="上書きできません"):
            begin_performance_run(
                run,
                kind="soak",
                arguments=soak_args(root, audio),
                effective_configs=[config],
                root=root,
            )

        assert manifest.read_bytes() == original
    finally:
        release_performance_run_locks(manifest)


@pytest.mark.skipif(os.name != "nt", reason="Windows mandatory share-mode contract")
def test_report_is_locked_before_hashing_until_manifest_is_written(
    tmp_path, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
    )
    report = run / "2026-09-07-acceptance.json"
    report.write_text('{"results": []}', encoding="utf-8")
    attempted = []
    original_sha256 = evidence._sha256

    def hash_with_race_attempt(path):
        if path.resolve() == report.resolve() and not attempted:
            with pytest.raises(PermissionError):
                report.write_text("replacement", encoding="utf-8")
            attempted.append(True)
        return original_sha256(path)

    monkeypatch.setattr(evidence, "_sha256", hash_with_race_attempt)
    complete_performance_run(
        manifest,
        primary_report=report,
        artifacts=[report],
        effective_configs=[config],
        root=root,
    )

    assert attempted == [True]
    report.write_text("mutable after completion", encoding="utf-8")


def test_complete_manifest_cannot_be_resealed_with_replacement_artifacts(tmp_path) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
    )
    report, _ = seal_run(root, run, manifest)
    original_manifest = manifest.read_bytes()
    replacement = run / "replacement.json"
    replacement.write_text('{"replacement": true}', encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match="完了済み"):
        complete_performance_run(
            manifest,
            primary_report=replacement,
            artifacts=[replacement],
            effective_configs=[config],
            root=root,
        )

    assert manifest.read_bytes() == original_manifest
    assert report.read_text(encoding="utf-8") == '{"results": []}'


def test_validator_rejects_malformed_configuration_without_crashing(tmp_path) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, sealed = seal_run(root, run, manifest)
    sealed["configuration"] = []
    manifest.write_text(json.dumps(sealed), encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match="configuration"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)


def test_validator_rechecks_source_changed_during_later_artifact_validation(
    tmp_path, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, sealed = seal_run(root, run, manifest)
    source = root / "scripts" / "replay_client.py"
    later_artifact = run / "2026-09-07-acceptance.md"
    _install_read_race(
        monkeypatch,
        later_artifact,
        lambda: source.write_text("changed after source hash", encoding="utf-8"),
    )

    with pytest.raises(PerformanceEvidenceError, match="changed during validation"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)

    assert json.loads(manifest.read_text(encoding="utf-8")) == sealed


def test_validator_rechecks_earlier_artifact_while_later_artifact_hashes(
    tmp_path, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    first = run / "a-artifact.txt"
    later = run / "m-artifact.txt"
    report = run / "z-report.json"
    first.write_text("first", encoding="utf-8")
    later.write_text("later", encoding="utf-8")
    report.write_text('{"results": []}', encoding="utf-8")
    complete_performance_run(
        manifest,
        primary_report=report,
        artifacts=[first, later, report],
        effective_configs=[config],
        root=root,
    )
    _install_read_race(
        monkeypatch,
        later,
        lambda: first.write_text("changed after first hash", encoding="utf-8"),
    )

    with pytest.raises(PerformanceEvidenceError, match="changed during validation"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)


def test_validator_rejects_source_content_aba_with_changed_revision(
    tmp_path, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, _ = seal_run(root, run, manifest)
    source = root / "scripts" / "replay_client.py"
    original = source.read_bytes()
    before = source.stat()

    def replace_then_restore() -> None:
        source.write_bytes(b"temporary replacement")
        source.write_bytes(original)
        os.utime(
            source,
            ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000),
        )

    _install_read_race(
        monkeypatch,
        run / "2026-09-07-acceptance.md",
        replace_then_restore,
    )

    with pytest.raises(PerformanceEvidenceError, match="changed during validation"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_validator_rejects_persistent_source_set_change_during_validation(
    operation, tmp_path, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, _ = seal_run(root, run, manifest)
    if operation == "add":
        action = lambda: (root / "server" / "late.py").write_text(
            "late source", encoding="utf-8"
        )
    else:
        action = (root / "server" / "main.py").unlink
    _install_read_race(
        monkeypatch,
        run / "2026-09-07-acceptance.md",
        action,
    )

    with pytest.raises(PerformanceEvidenceError, match="changed during validation"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)


@pytest.mark.parametrize("linked_path", ["manifest", "run-dir"])
def test_validator_rejects_linked_manifest_or_run_directory(
    linked_path, tmp_path
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, _ = seal_run(root, run, manifest)

    try:
        if linked_path == "manifest":
            backing = run / "manifest-backing.json"
            manifest.rename(backing)
            manifest.symlink_to(backing.name)
        else:
            backing = root / "runs" / "run-backing"
            run.rename(backing)
            run.symlink_to(backing, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(PerformanceEvidenceError, match="link|junction|reparse"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)


def test_validation_witness_rejects_evidence_changed_by_downstream_work(
    tmp_path
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, _ = seal_run(root, run, manifest)
    validated = validate_performance_manifest(
        run, kind="soak", primary_report=report, root=root
    )

    report.write_text("downstream replacement", encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match="changed during validation"):
        recheck_validation_witness(validated.validation_witness)


@pytest.mark.skipif(os.name != "nt", reason="Windows creation-time revision bypass")
def test_validation_witness_rehashes_same_size_file_after_mtime_restore(
    tmp_path,
) -> None:
    target = tmp_path / "stable-size.bin"
    target.write_bytes(b"original-A")
    read_set = evidence.ValidationReadSet()
    read_set.sha256(target, description="same-size witness file")
    witness = read_set.freeze()
    original_six = evidence._snapshot_path_revision(target)[1]
    original_stat = target.stat()
    original_five = (
        original_stat.st_dev,
        original_stat.st_ino,
        original_stat.st_size,
        original_stat.st_mtime_ns,
        original_stat.st_ctime_ns,
    )

    target.write_bytes(b"replaced-B")
    os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    target.write_bytes(b"original-A")
    os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    current = target.stat()
    assert (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    ) == original_five
    assert evidence._snapshot_path_revision(target)[1] != original_six

    with pytest.raises(PerformanceEvidenceError, match="changed during validation"):
        witness.recheck()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory revision bypass")
def test_validation_witness_rechecks_directory_membership_after_mtime_restore(
    tmp_path,
) -> None:
    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "original.txt").write_text("original", encoding="utf-8")
    read_set = evidence.ValidationReadSet()
    read_set.watch_directory(watched, description="membership witness directory")
    witness = read_set.freeze()
    original_six = evidence._snapshot_path_revision(watched)[1]
    original_stat = watched.stat()
    original_membership = evidence._direct_membership_digest(
        watched, description="original membership"
    )

    (watched / "late.txt").write_text("late", encoding="utf-8")
    (watched / "late.txt").unlink()
    os.utime(watched, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert evidence._direct_membership_digest(
        watched, description="restored membership"
    ) == original_membership
    assert evidence._snapshot_path_revision(watched)[1] != original_six

    with pytest.raises(PerformanceEvidenceError, match="changed during validation"):
        witness.recheck()


@pytest.mark.skipif(os.name != "nt", reason="Windows mandatory share-mode contract")
def test_active_distribution_implementation_is_locked_during_measurement(
    tmp_path, fixture_runtime_distribution
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
    )
    try:
        with pytest.raises(PermissionError):
            fixture_runtime_distribution.write_text(
                "LAZY_VALUE = 'B'\n", encoding="utf-8"
            )
    finally:
        release_performance_run_locks(manifest)


@pytest.mark.skipif(os.name != "nt", reason="Windows ChangeTime membership contract")
def test_run_rejects_transient_site_membership_change_restored_before_seal(
    tmp_path, fixture_runtime_distribution
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
    )
    site_packages = fixture_runtime_distribution.parents[1]
    original = site_packages.stat()
    transient = site_packages / "shadow_runtime.py"
    transient.write_text("VALUE = 'B'\n", encoding="utf-8")
    transient.unlink()
    os.utime(
        site_packages,
        ns=(original.st_atime_ns, original.st_mtime_ns),
    )
    report = run / "report.json"
    report.write_text("{}", encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match="directory|changed"):
        complete_performance_run(
            manifest,
            primary_report=report,
            artifacts=[report],
            effective_configs=[config],
            root=root,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows ChangeTime membership contract")
@pytest.mark.parametrize("changed_tree", ["server", "corpus", "runtime-model"])
def test_run_rejects_transient_discovery_member_restored_before_seal(
    tmp_path, fixture_runtime_distribution, changed_tree: str
) -> None:
    root, audio = make_root(tmp_path)
    arguments = soak_args(root, audio)
    if changed_tree == "server":
        directory = root / "server"
        transient = directory / "transient.py"
    elif changed_tree == "corpus":
        directory = root / "tests" / "fixtures" / "ja"
        directory.mkdir(parents=True)
        (directory / "baseline.wav").write_bytes(b"baseline audio")
        arguments["audio"] = None
        arguments["corpus"] = "ja"
        transient = directory / "transient.wav"
    else:
        directory = root / "models" / "reazonspeech-k2-v2"
        transient = directory / "transient.onnx"
    run = root / "runs" / f"fresh-{changed_tree}"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=arguments,
        effective_configs=[config],
        root=root,
    )
    original_stat = directory.stat()
    original_revision = evidence._snapshot_path_revision(directory)[1]
    transient.write_bytes(b"transient executable input")
    transient.unlink()
    os.utime(
        directory,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert evidence._snapshot_path_revision(directory)[1] != original_revision
    report = run / "report.json"
    report.write_text("{}", encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match="directory|changed"):
        complete_performance_run(
            manifest,
            primary_report=report,
            artifacts=[report],
            effective_configs=[config],
            root=root,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows ChangeTime membership contract")
@pytest.mark.parametrize("cert_directory_exists", [True, False])
def test_run_rejects_transient_tls_assets_restored_before_seal(
    tmp_path, fixture_runtime_distribution, cert_directory_exists: bool
) -> None:
    root, audio = make_root(tmp_path)
    cert_parent = root / "external-cert-parent"
    cert_parent.mkdir()
    cert_directory = cert_parent / "runtime-certs"
    if cert_directory_exists:
        cert_directory.mkdir()
    base_config = root / "config.yaml"
    base_config.write_text(
        base_config.read_text(encoding="utf-8").replace(
            (root / "no-certs").as_posix(), cert_directory.as_posix()
        ),
        encoding="utf-8",
    )
    run = root / "runs" / (
        "fresh-existing-tls" if cert_directory_exists else "fresh-absent-tls"
    )
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
    )
    witnessed_directory = cert_directory if cert_directory_exists else cert_parent
    original_stat = witnessed_directory.stat()
    original_revision = evidence._snapshot_path_revision(witnessed_directory)[1]
    cert_directory.mkdir(exist_ok=True)
    (cert_directory / "cert.pem").write_bytes(b"transient certificate")
    (cert_directory / "key.pem").write_bytes(b"transient private key")
    (cert_directory / "cert.pem").unlink()
    (cert_directory / "key.pem").unlink()
    if not cert_directory_exists:
        cert_directory.rmdir()
    os.utime(
        witnessed_directory,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert (
        evidence._snapshot_path_revision(witnessed_directory)[1]
        != original_revision
    )
    report = run / "report.json"
    report.write_text("{}", encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match="directory|changed"):
        complete_performance_run(
            manifest,
            primary_report=report,
            artifacts=[report],
            effective_configs=[config],
            root=root,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows ChangeTime membership contract")
def test_tls_absence_witness_descends_if_directory_appears_before_snapshot(
    tmp_path, fixture_runtime_distribution, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    cert_parent = root / "external-cert-race-parent"
    cert_parent.mkdir()
    cert_directory = cert_parent / "runtime-certs"
    base_config = root / "config.yaml"
    base_config.write_text(
        base_config.read_text(encoding="utf-8").replace(
            (root / "no-certs").as_posix(), cert_directory.as_posix()
        ),
        encoding="utf-8",
    )
    original_watch_directory = evidence.ValidationReadSet.watch_directory
    injected = False

    def inject_before_parent_snapshot(self, path, *, description):
        nonlocal injected
        if evidence._absolute_path(path) == cert_parent and not injected:
            cert_directory.mkdir()
            injected = True
        return original_watch_directory(self, path, description=description)

    monkeypatch.setattr(
        evidence.ValidationReadSet,
        "watch_directory",
        inject_before_parent_snapshot,
    )
    run = root / "runs" / "fresh-tls-absence-race"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
    )
    assert injected
    original_stat = cert_directory.stat()
    original_revision = evidence._snapshot_path_revision(cert_directory)[1]
    (cert_directory / "cert.pem").write_bytes(b"transient certificate")
    (cert_directory / "key.pem").write_bytes(b"transient private key")
    (cert_directory / "cert.pem").unlink()
    (cert_directory / "key.pem").unlink()
    os.utime(
        cert_directory,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert evidence._snapshot_path_revision(cert_directory)[1] != original_revision
    report = run / "report.json"
    report.write_text("{}", encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match="directory|changed"):
        complete_performance_run(
            manifest,
            primary_report=report,
            artifacts=[report],
            effective_configs=[config],
            root=root,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows ChangeTime manifest contract")
@pytest.mark.parametrize("restore_original_bytes", [False, True])
def test_run_rejects_start_manifest_argument_tampering_with_restored_mtime(
    tmp_path, fixture_runtime_distribution, restore_original_bytes: bool
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / (
        "fresh-manifest-aba" if restore_original_bytes else "fresh-manifest-tamper"
    )
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
    )
    original_bytes = manifest.read_bytes()
    original_stat = manifest.stat()
    original_revision = evidence._snapshot_path_revision(manifest)[1]
    tampered = json.loads(original_bytes)
    tampered["arguments"]["minutes"] = 1.0
    tampered["arguments"]["students"] = 1
    manifest.write_text(
        json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if restore_original_bytes:
        manifest.write_bytes(original_bytes)
    os.utime(
        manifest,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    if restore_original_bytes:
        assert manifest.read_bytes() == original_bytes
    assert evidence._snapshot_path_revision(manifest)[1] != original_revision

    with pytest.raises(PerformanceEvidenceError, match="manifest|changed"):
        evidence.recheck_performance_run_state(manifest)
    release_performance_run_locks(manifest)


def test_validator_rejects_lazy_distribution_code_changed_after_run(
    tmp_path, fixture_runtime_distribution
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, _ = seal_run(root, run, manifest)
    fixture_runtime_distribution.write_text("LAZY_VALUE = 'B'\n", encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match="Python.*snapshot"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)


@pytest.mark.parametrize("linked_path", ["manifest", "run-dir"])
def test_validator_rejects_simulated_reparse_points_deterministically(
    linked_path, tmp_path, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, _ = seal_run(root, run, manifest)
    marked = evidence._absolute_path(manifest if linked_path == "manifest" else run)
    real_check = evidence._is_link_or_reparse

    def simulated_reparse(path, value=None):
        return evidence._absolute_path(Path(path)) == marked or real_check(path, value)

    monkeypatch.setattr(evidence, "_is_link_or_reparse", simulated_reparse)

    with pytest.raises(PerformanceEvidenceError, match="link|junction|reparse"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)


def test_validator_rejects_run_directory_membership_change_during_validation(
    tmp_path, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, _ = seal_run(root, run, manifest)
    _install_read_race(
        monkeypatch,
        run / "2026-09-07-acceptance.md",
        lambda: (run / "late-unsealed.txt").write_text("late", encoding="utf-8"),
    )

    with pytest.raises(PerformanceEvidenceError, match="changed during validation"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)


def test_validator_rejects_run_directory_swap_during_validation(
    tmp_path, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, _ = seal_run(root, run, manifest)
    replacement = root / "runs" / "replacement"
    displaced = root / "runs" / "displaced"
    shutil.copytree(run, replacement)

    def swap_run_directory() -> None:
        try:
            run.rename(displaced)
            replacement.rename(run)
        except OSError as exc:
            pytest.skip(f"run directory swap unavailable: {exc}")

    _install_read_race(
        monkeypatch,
        run / "2026-09-07-acceptance.md",
        swap_run_directory,
    )

    with pytest.raises(PerformanceEvidenceError, match="changed during validation"):
        validate_performance_manifest(run, kind="soak", primary_report=report, root=root)


def test_begin_rejects_stale_imported_local_module_state(
    tmp_path, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    imported = tmp_path / "loaded-helper.py"
    imported.write_text("loaded state A", encoding="utf-8")
    state = evidence._capture_imported_module_state(
        imported,
        description="simulated loaded helper",
    )
    monkeypatch.setattr(evidence, "_IMPORTED_LOCAL_MODULE_STATE", (state,))
    imported.write_text("stable disk state B", encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match="changed during validation|after import"):
        begin_performance_run(
            run,
            kind="soak",
            arguments=soak_args(root, audio),
            effective_configs=[config],
            root=root,
        )


def test_validation_witness_rechecks_imported_local_module_state(
    tmp_path, monkeypatch
) -> None:
    root, audio = make_root(tmp_path)
    run = root / "runs" / "fresh-soak"
    config = prepare_config(root, run)
    manifest = begin_performance_run(
        run,
        kind="soak",
        arguments=soak_args(root, audio),
        effective_configs=[config],
        root=root,
        started_at="2026-09-07T00:30:00+09:00",
    )
    report, _ = seal_run(root, run, manifest)
    imported = tmp_path / "loaded-helper.py"
    imported.write_text("loaded state A", encoding="utf-8")
    state = evidence._capture_imported_module_state(
        imported,
        description="simulated loaded helper",
    )
    monkeypatch.setattr(evidence, "_IMPORTED_LOCAL_MODULE_STATE", (state,))
    validated = validate_performance_manifest(
        run, kind="soak", primary_report=report, root=root
    )
    imported.write_text("stable disk state B", encoding="utf-8")

    with pytest.raises(PerformanceEvidenceError, match="changed during validation|after import"):
        recheck_validation_witness(validated.validation_witness)


@pytest.mark.skipif(os.name != "nt", reason="Windows creation-time revision bypass")
def test_import_witness_rehashes_same_size_file_after_mtime_restore(
    tmp_path, monkeypatch
) -> None:
    imported = tmp_path / "loaded-helper.py"
    imported.write_bytes(b"loaded-state-A")
    state = evidence._capture_imported_module_state(
        imported,
        description="simulated loaded helper",
    )
    monkeypatch.setattr(evidence, "_IMPORTED_LOCAL_MODULE_STATE", (state,))
    original_six = evidence._snapshot_path_revision(imported)[1]
    original_stat = imported.stat()

    imported.write_bytes(b"loaded-state-B")
    os.utime(imported, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    imported.write_bytes(b"loaded-state-A")
    os.utime(imported, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert evidence._snapshot_path_revision(imported)[1] != original_six

    with pytest.raises(PerformanceEvidenceError, match="changed during validation"):
        evidence._assert_imported_local_modules_current()
