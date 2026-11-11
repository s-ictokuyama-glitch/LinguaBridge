"""Own one disposable LinguaBridge CLI instance and retain its evidence."""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import re
from urllib.request import ProxyHandler, build_opener

import psutil
sys.path.insert(0, str(Path(__file__).resolve().parent))
from package import check_links, prepare_package, sha256, verify_package, write_json

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("action", choices=["launch", "doctor", "cleanup"])
parser.add_argument("--run-dir", type=Path, required=True)
parser.add_argument("--models-dir", type=Path)
parser.add_argument("--config-override", type=Path)
parser.add_argument("--advertise-ip")
parser.add_argument("--distribution-zip", type=Path)
parser.add_argument("--expected-sha256")
parser.add_argument("--expected-commit")
args = parser.parse_args()
if args.distribution_zip:
    if args.action != "launch" or args.models_dir or args.config_override:
        parser.error("distribution-zip is launch-only and cannot use models-dir/config-override")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", args.expected_sha256 or "") or not re.fullmatch(r"[0-9a-fA-F]{40}", args.expected_commit or ""):
        parser.error("distribution-zip requires expected-sha256 (64 hex) and expected-commit (full 40 hex)")
elif args.expected_sha256 or args.expected_commit:
    parser.error("expected-sha256/expected-commit require distribution-zip")
run = args.run_dir.resolve()
evidence_root = (ROOT / "artifacts/verify").resolve()
if not run.is_relative_to(evidence_root) or run == evidence_root:
    parser.error("run-dir must be a child of this checkout's artifacts/verify")
if any(p.is_symlink() or p.is_junction() for p in [args.run_dir.absolute(), *args.run_dir.absolute().parents]):
    parser.error("run-dir must not traverse a filesystem link")
manifest = run / "instance.json"
process = None
state = None


def prepare_checkout():
    from server.config import load_config
    from server.network import resolve_ip

    profile = args.config_override.resolve() if args.config_override is not None else None
    if profile is not None and not profile.is_file():
        raise FileNotFoundError(f"Config override does not exist: {profile}")
    config = load_config(ROOT / "config.yaml", override=profile)
    model_dir = (args.models_dir or config.models.resolved_dir).resolve()
    public_ip = resolve_ip(args.advertise_ip or config.server.advertise_ip)
    with socket.socket() as reservation:
        reservation.bind(("0.0.0.0", 0))
        port = reservation.getsockname()[1]
    override = config.model_dump(mode="json")
    override["server"].update(http_port=port, cert_dir="certs")
    override["models"]["dir"] = str(model_dir)
    override["recording"].update(default_on=False, out_dir="sessions")
    override_path = run / "override.json"
    write_json(override_path, override)
    effective = load_config(ROOT / "config.yaml", override=override_path, data_root=run / "tmp/data")
    write_json(run / "effective-config.json", effective.model_dump(mode="json"))
    command = [sys.executable, "-m", "server.main", "--config", str(ROOT / "config.yaml"), "--config-override", str(override_path), "--data-root", str(run / "tmp/data"), "--advertise-ip", public_ip]
    sources = [ROOT / "config.yaml", override_path]
    if profile is not None:
        sources.append(profile)
    sources += sorted((ROOT / "server").rglob("*.py"))
    sources += sorted(p for p in (ROOT / "web").rglob("*") if p.is_file())
    return {"command": command, "cwd": ROOT, "env": dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"), "port": port, "models_dir": str(model_dir), "sources": {str(p): sha256(p) for p in sources}}


def identity(owned, prefix=""):
    return {prefix + "pid": owned.pid, prefix + "created": owned.create_time(), prefix + "command": owned.cmdline()}


def stop_owned(*, require_complete_tree=False):
    processes = {}
    identities = {}
    descendants = {}
    child_snapshots = {}
    for prefix in ("", "launcher_"):
        pid = state.get(prefix + "pid")
        if pid is not None and psutil.pid_exists(pid):
            owned = psutil.Process(pid)
            snapshot = identity(owned)
            if snapshot["created"] != state[prefix + "created"] or snapshot["command"] != state[prefix + "command"]:
                raise RuntimeError("Process identity changed; refusing to terminate it")
            processes[pid] = owned
            identities[pid] = snapshot
    cleanup_processes = run / "cleanup-processes.json"
    if cleanup_processes.is_file():
        for snapshot in json.loads(cleanup_processes.read_text(encoding="utf-8")):
            try:
                child = psutil.Process(snapshot["pid"])
                if child.create_time() != snapshot["created"]:
                    continue
            except psutil.NoSuchProcess:
                continue
            child_snapshots[child.pid] = snapshot
            identities[child.pid] = snapshot
            descendants[child.pid] = child
    for owned in list(processes.values()):
        try:
            children = owned.children(recursive=True)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            if require_complete_tree:
                raise
            continue
        for child in children:
            if child.pid in identities:
                continue
            try:
                child_identity = identity(child)
            except psutil.NoSuchProcess:
                continue
            except psutil.Error:
                if require_complete_tree:
                    raise
                continue
            child_snapshots[child.pid] = child_identity
            identities[child.pid] = child_identity
            if child_identity["command"][1:] == state["command"][1:]:
                processes[child.pid] = child
            else:
                descendants[child.pid] = child

    errors = []
    if child_snapshots:
        try:
            pending = cleanup_processes.with_suffix(".tmp")
            write_json(pending, list(child_snapshots.values()))
            pending.replace(cleanup_processes)
        except OSError as error:
            if require_complete_tree:
                raise
            errors.append(str(error))

    def stop_and_wait(targets):
        for owned in reversed(targets):
            try:
                if identity(owned) != identities[owned.pid]:
                    raise RuntimeError("Process identity changed during cleanup")
                owned.terminate()
            except psutil.NoSuchProcess:
                pass
            except (psutil.Error, RuntimeError) as error:
                errors.append(str(error))
        for owned in targets:
            try:
                owned.wait(timeout=15)
            except psutil.Error as error:
                errors.append(str(error))

    stop_and_wait(list(processes.values()))
    waiting = [child for pid, child in descendants.items() if psutil.pid_exists(pid)]
    _, remaining = psutil.wait_procs(waiting, timeout=3)
    stop_and_wait(remaining)
    if errors:
        raise RuntimeError("Could not stop all owned processes: " + "; ".join(errors))
    return sorted(identities)

try:
    if args.action == "launch":
        if manifest.exists() or (run / "tmp").exists():
            raise RuntimeError("Use a new run directory for every launch")
        run.mkdir(parents=True, exist_ok=True)
        (run / "tmp").mkdir(exist_ok=False)
        state = {"pid": None, "created": None, "command": [], "data_dir": str(run / "tmp/data"), "sources": {}}
        write_json(manifest, state)
        Path(state["data_dir"]).mkdir()
        if args.distribution_zip:
            prepared = prepare_package(args.distribution_zip, args.expected_sha256.lower(), args.expected_commit.lower(), run, args.advertise_ip)
        else:
            prepared = prepare_checkout()
        state.update({key: prepared[key] for key in ("command", "port", "models_dir", "sources")})
        state["url"] = f"http://127.0.0.1:{state['port']}"
        if "package" in prepared:
            state["package"] = prepared["package"]
        write_json(manifest, state)
        with (run / "server.stdout.log").open("wb") as stdout, (run / "server.stderr.log").open("wb") as stderr:
            process = subprocess.Popen(
                prepared["command"], cwd=prepared["cwd"], env=prepared["env"], stdout=stdout, stderr=stderr,
                stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        state["pid"] = process.pid
        write_json(manifest, state)
        state.update(identity(psutil.Process(process.pid)))
        state.update(identity(psutil.Process(process.pid), "launcher_"))
        write_json(manifest, state)
        deadline = time.monotonic() + 60
    elif args.action == "cleanup" and not manifest.exists():
        if (run / "tmp").exists():
            raise RuntimeError("Scratch exists without ownership manifest; inspect before cleanup")
        state = {"pid": None}
    else:
        state = json.loads(manifest.read_text(encoding="utf-8"))
        deadline = time.monotonic()

    if args.action == "cleanup":
        run.mkdir(parents=True, exist_ok=True)
        stopped_pids = stop_owned(require_complete_tree=True)
        tmp = run / "tmp"
        if tmp.exists():
            check_links(tmp)
            if tmp.resolve() != run / "tmp":
                raise RuntimeError("Scratch directory contains links; inspect before removing")
            shutil.rmtree(tmp)
        report = {"stopped": True, "stopped_pids": stopped_pids, "scratch_removed": not tmp.exists(), "evidence_retained": manifest.exists()}
        (run / "cleanup.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
    else:
        for name, expected in state["sources"].items():
            if sha256(ROOT / name) != expected:
                raise RuntimeError(f"Source/config changed since launch: {name}")
        if args.action == "doctor" and "package" in state:
            verify_package(state["package"])
        opener = build_opener(ProxyHandler({}))
        while True:
            try:
                if args.action == "launch":
                    launcher = psutil.Process(state["launcher_pid"])
                    for candidate in [launcher, *launcher.children(recursive=True)]:
                        if candidate.cmdline()[1:] != state["launcher_command"][1:]:
                            continue
                        if any(c.status == psutil.CONN_LISTEN and c.laddr.port == state["port"] for c in candidate.net_connections(kind="tcp")):
                            state.update(pid=candidate.pid, created=candidate.create_time(), command=candidate.cmdline())
                            manifest.write_text(json.dumps(state, indent=2), encoding="utf-8")
                            break
                owned = psutil.Process(state["pid"])
                if owned.create_time() != state["created"] or owned.cmdline() != state["command"]:
                    raise RuntimeError("Process identity differs from launch record")
                if not any(c.status == psutil.CONN_LISTEN and c.laddr.port == state["port"] for c in owned.net_connections(kind="tcp")):
                    raise RuntimeError("Recorded process does not own the listening port")
                responses = {}
                for route in ["/ready", "/api/config", "/api/teacher-info"]:
                    with opener.open(state["url"] + route, timeout=10) as response:
                        responses[route] = json.load(response)
                if responses["/ready"] != {"status": "ok"}:
                    raise RuntimeError("Models are not ready")
                if {x["code"] for x in responses["/api/config"]["languages"]} != {"en", "zh"}:
                    raise RuntimeError("This recipe requires configured en/zh languages")
                code = responses["/api/teacher-info"]["code"]
                if not isinstance(code, str) or len(code) != 4 or not code.isdigit():
                    raise RuntimeError("Student join code is unavailable")
                report = {"ready": True, "pid": state["pid"], "url": state["url"], "code": code, "models_dir": state["models_dir"], "artifact": state.get("package")}
                write_json(run / ("doctor.json" if args.action == "doctor" else "launch.json"), report)
                print(json.dumps(report))
                break
            except (OSError, ValueError, RuntimeError, psutil.Error):
                if args.action != "launch" or process.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
except Exception as error:
    failure = {"action": args.action, "error": str(error)}
    if process is not None:
        try:
            failure["stopped_pids"] = stop_owned()
        except Exception as stop_error:
            failure["stop_error"] = str(stop_error)
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
        except (OSError, subprocess.SubprocessError) as stop_error:
            failure["handle_stop_error"] = str(stop_error)
    if state is not None:
        run.mkdir(parents=True, exist_ok=True)
        if process is None and args.action == "launch":
            try:
                tmp = run / "tmp"
                if tmp.exists():
                    check_links(tmp)
                    shutil.rmtree(tmp)
            except OSError as cleanup_error:
                failure["cleanup_error"] = str(cleanup_error)
        write_json(run / ("doctor-failure.json" if args.action == "doctor" else "failure.json"), failure)
    print(f"{args.action} failed: {error}; logs: {run}", file=sys.stderr)
    raise SystemExit(1)
