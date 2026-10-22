"""Own one disposable LinguaBridge CLI instance and retain its evidence."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

import psutil

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("action", choices=["launch", "doctor", "cleanup"])
parser.add_argument("--run-dir", type=Path, required=True)
parser.add_argument("--models-dir", type=Path)
parser.add_argument("--config-override", type=Path)
parser.add_argument("--advertise-ip")
args = parser.parse_args()
run = args.run_dir.resolve()
evidence_root = (ROOT / "artifacts/verify").resolve()
if not run.is_relative_to(evidence_root) or run == evidence_root:
    parser.error("run-dir must be a child of this checkout's artifacts/verify")
if any(p.is_symlink() or p.is_junction() for p in [args.run_dir.absolute(), *args.run_dir.absolute().parents]):
    parser.error("run-dir must not traverse a filesystem link")
manifest = run / "instance.json"
process = None

try:
    if args.action == "launch":
        from server.config import load_config
        from server.network import resolve_ip

        if manifest.exists() or (run / "tmp").exists():
            raise RuntimeError("Use a new run directory for every launch")
        profile = args.config_override.resolve() if args.config_override is not None else None
        if profile is not None and not profile.is_file():
            raise FileNotFoundError(f"Config override does not exist: {profile}")
        config = load_config(ROOT / "config.yaml", override=profile)
        model_dir = (args.models_dir or config.models.resolved_dir).resolve()
        public_ip = resolve_ip(args.advertise_ip or config.server.advertise_ip)
        with socket.socket() as reservation:
            reservation.bind(("0.0.0.0", 0))
            port = reservation.getsockname()[1]
        run.mkdir(parents=True, exist_ok=True)
        data = run / "tmp/data"
        data.mkdir(parents=True)
        override = config.model_dump(mode="json")
        override["server"].update(http_port=port, cert_dir="certs")
        override["models"]["dir"] = str(model_dir)
        override["recording"].update(default_on=False, out_dir="sessions")
        override_path = run / "override.json"
        override_path.write_text(json.dumps(override, indent=2), encoding="utf-8")
        effective = load_config(ROOT / "config.yaml", override=override_path, data_root=data)
        (run / "effective-config.json").write_text(effective.model_dump_json(indent=2), encoding="utf-8")
        command = [
            sys.executable, "-m", "server.main", "--config", str(ROOT / "config.yaml"),
            "--config-override", str(override_path), "--data-root", str(data),
            "--advertise-ip", public_ip,
        ]
        sources = [ROOT / "config.yaml", override_path]
        if profile is not None:
            sources.append(profile)
        sources += sorted((ROOT / "server").rglob("*.py"))
        sources += sorted(p for p in (ROOT / "web").rglob("*") if p.is_file())
        hashes = {
            str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sources
        }
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        with (run / "server.stdout.log").open("wb") as stdout, (run / "server.stderr.log").open("wb") as stderr:
            process = subprocess.Popen(
                command, cwd=ROOT, env=env, stdout=stdout, stderr=stderr,
                stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        state = {
            "pid": process.pid, "created": psutil.Process(process.pid).create_time(),
            "command": command, "url": f"http://127.0.0.1:{port}", "port": port,
            "data_dir": str(data), "models_dir": str(model_dir), "sources": hashes,
            "launcher_pid": process.pid, "launcher_created": psutil.Process(process.pid).create_time(),
            "launcher_command": command,
        }
        manifest.write_text(json.dumps(state, indent=2), encoding="utf-8")
        deadline = time.monotonic() + 60
    elif args.action == "cleanup" and not manifest.exists() and not (run / "tmp").exists():
        state = {"pid": None}
    else:
        state = json.loads(manifest.read_text(encoding="utf-8"))
        deadline = time.monotonic()

    if args.action == "cleanup":
        processes = {}
        for prefix in ["", "launcher_"]:
            pid = state.get(prefix + "pid")
            if pid is not None and psutil.pid_exists(pid):
                owned = psutil.Process(pid)
                if owned.create_time() != state[prefix + "created"] or owned.cmdline() != state[prefix + "command"]:
                    raise RuntimeError("Process identity changed; refusing to terminate it")
                processes[pid] = owned
        for owned in processes.values():
            if owned.is_running():
                owned.terminate()
        for owned in processes.values():
            owned.wait(timeout=15)
        tmp = run / "tmp"
        if tmp.exists():
            links = [p for p in [tmp, *tmp.rglob("*")] if p.is_symlink() or p.is_junction()]
            if links or tmp.resolve() != run / "tmp":
                raise RuntimeError("Scratch directory contains links; inspect before removing")
            shutil.rmtree(tmp)
        report = {"stopped": True, "scratch_removed": not tmp.exists(), "evidence_retained": True}
        (run / "cleanup.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
    else:
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
                for name, expected in state["sources"].items():
                    if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
                        raise RuntimeError(f"Source/config changed since launch: {name}")
                responses = {}
                for route in ["/ready", "/api/config", "/api/teacher-info"]:
                    with opener.open(state["url"] + route, timeout=10) as response:
                        responses[route] = json.load(response)
                if responses["/ready"] != {"status": "ok"}:
                    raise RuntimeError("Models are not ready")
                if {x["code"] for x in responses["/api/config"]["languages"]} != {"en", "zh"}:
                    raise RuntimeError("This recipe requires configured en/zh languages")
                code = responses["/api/teacher-info"]["code"]
                if len(code) != 4 or not code.isdigit():
                    raise RuntimeError("Teacher join code is unavailable")
                print(json.dumps({"ready": True, "pid": state["pid"], "url": state["url"], "code": code, "models_dir": state["models_dir"]}))
                break
            except (OSError, ValueError, RuntimeError, psutil.Error):
                if args.action != "launch" or process.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
except Exception as error:
    if process is not None:
        if process.poll() is None:
            for child in psutil.Process(process.pid).children(recursive=True):
                child.terminate()
                child.wait(timeout=15)
            process.terminate()
        process.wait(timeout=15)
    if args.action != "doctor":
        run.mkdir(parents=True, exist_ok=True)
        (run / "failure.json").write_text(json.dumps({"action": args.action, "error": str(error)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{args.action} failed: {error}; logs: {run}", file=sys.stderr)
    raise SystemExit(1)
