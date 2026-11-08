import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import zipfile


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def check_links(root):
    for path in [root, *root.rglob("*")]:
        if path.is_symlink() or path.is_junction():
            raise RuntimeError(f"Filesystem link is not allowed: {path}")


def archive_members(archive):
    paths = {}
    files = set()
    members = archive.infolist()
    for member in members:
        name = member.filename
        parts = name.rstrip("/").split("/")
        mode = stat.S_IFMT(member.external_attr >> 16)
        if (not name or "\\" in name or name.startswith("/") or "\x00" in member.orig_filename
                or mode not in (0, stat.S_IFREG, stat.S_IFDIR) or member.external_attr & 0x400):
            raise ValueError(f"Unsafe ZIP entry: {name!r}")
        for part in parts:
            base = part.split(".")[0].upper()
            if (not part or part in (".", "..") or part.endswith((".", " "))
                    or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                    or re.fullmatch(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³]", base)):
                raise ValueError(f"Unsafe Windows ZIP path: {name!r}")
        for index in range(1, len(parts) + 1):
            spelling = "/".join(parts[:index])
            key = spelling.casefold()
            kind = "file" if index == len(parts) and not member.is_dir() else "dir"
            if key in paths and paths[key] != (spelling, kind):
                raise ValueError(f"Aliased or conflicting ZIP path: {name!r}")
            paths[key] = (spelling, kind)
        if not member.is_dir():
            key = name.casefold()
            if key in files:
                raise ValueError(f"Duplicate ZIP file: {name!r}")
            files.add(key)
    return members


PROBE = r'''
import json, pathlib, sys
from server import config, network
app, override, data, requested_ip = sys.argv[1:]
app = pathlib.Path(app).resolve()
cfg = config.load_config(app / "config.yaml", override=override, data_root=data)
def inside(path):
    path = pathlib.Path(path).resolve()
    if not path.is_relative_to(app) or not path.exists():
        raise ValueError("Missing or external packaged input: " + str(path))
    return str(path)
if cfg.asr.engine not in ("haas-onnx", "sherpa", "faster-whisper") or cfg.mt.engine not in ("hy-mt2", "nllb") or cfg.vad.engine != "silero":
    raise ValueError("Distribution verification requires real ASR/MT and silero VAD")
models = cfg.models.resolved_dir.resolve()
refs = {"models": inside(models)}
modules = {"server.config": inside(config.__file__), "server.network": inside(network.__file__)}
if cfg.asr.engine == "haas-onnx":
    import onnxruntime
    from onnxruntime.capi import onnxruntime_pybind11_state
    from server.asr import haas_onnx_engine
    modules["onnxruntime"] = inside(onnxruntime.__file__)
    modules["onnxruntime.capi.onnxruntime_pybind11_state"] = inside(onnxruntime_pybind11_state.__file__)
    modules["server.asr.haas_onnx_engine"] = inside(haas_onnx_engine.__file__)
    model = cfg.models.resolve(cfg.asr.haas_onnx.model)
    refs["asr"] = inside(model)
    refs["asr/manifest.json"] = inside(model / "manifest.json")
    for path in haas_onnx_engine.verified_model_paths(model, cfg.asr.haas_onnx.quantization).values():
        refs["asr/" + path.relative_to(model).as_posix()] = inside(path)
elif cfg.asr.engine == "sherpa":
    from server.asr import sherpa_engine
    model = models / cfg.asr.sherpa.model
    refs["asr"] = inside(model)
    for name in (sherpa_engine.ENCODER_FILE, sherpa_engine.DECODER_FILE, sherpa_engine.JOINER_FILE, sherpa_engine.TOKENS_FILE):
        refs["asr/" + name] = inside(model / name)
    hotwords = cfg.asr.sherpa.resolved_hotwords_file()
    if hotwords is not None:
        refs["hotwords"] = inside(hotwords)
else:
    refs["asr"] = inside(models / cfg.asr.model)
if cfg.mt.engine == "hy-mt2":
    refs["mt"] = inside(models / cfg.mt.hy_mt2.gguf_path)
else:
    refs["mt"] = inside(models / cfg.mt.nllb.model_dir)
    refs["tokenizer"] = inside(models / cfg.mt.nllb.tokenizer_dir)
import faster_whisper.vad
refs["vad"] = inside(pathlib.Path(faster_whisper.vad.__file__).parent / "assets" / "silero_vad_v6.onnx")
modules["faster_whisper.vad"] = inside(faster_whisper.vad.__file__)
for item in sys.path:
    inside(item or pathlib.Path.cwd())
print(json.dumps({"executable": inside(sys.executable), "sys_path": sys.path, "modules": modules,
    "config": cfg.model_dump(mode="json"), "models_dir": str(models), "inputs": refs,
    "public_ip": network.resolve_ip(requested_ip or cfg.server.advertise_ip),
    "addresses": [vars(a) for a in network.list_addresses()]}))
'''


def prepare_package(archive, expected_sha256, expected_commit, run, advertise_ip):
    archive = archive.resolve(strict=True)
    package = run / "tmp/package"
    package.mkdir()
    actual = sha256(archive)
    write_json(run / "package-archive.json", {"archive": str(archive), "sha256": actual, "expected_sha256": expected_sha256, "expected_commit": expected_commit})
    if actual != expected_sha256:
        raise ValueError("Distribution ZIP SHA256 differs from expected identity")
    with zipfile.ZipFile(archive) as zipped:
        members = archive_members(zipped)
        for member in members:
            target = package / member.filename
            if not target.resolve().is_relative_to(package):
                raise ValueError(f"ZIP target escapes scratch: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(member) as source, target.open("xb") as dest:
                    shutil.copyfileobj(source, dest)
    app = package / "app"
    for relative in ("start.bat", "app/python/python.exe", "app/config.yaml", "app/version.json", "app/server", "app/web", "app/models"):
        if not (package / relative).exists():
            raise ValueError(f"Missing distribution entry: {relative}")
    check_links(package)
    version = json.loads((app / "version.json").read_text(encoding="utf-8"))
    write_json(run / "package-version.json", version)
    if version.get("dirty") is not False or version.get("commit") != expected_commit:
        raise ValueError("Distribution version is dirty or commit differs from expected identity")
    if sha256(app / "config.yaml") != version.get("config_sha256"):
        raise ValueError("Distribution config SHA256 differs from version.json")
    inventory = {str(p.relative_to(package)).replace("\\", "/"): sha256(p) for p in sorted(package.rglob("*")) if p.is_file()}
    write_json(run / "package-files.json", inventory)
    (run / "package-config.yaml").write_bytes((app / "config.yaml").read_bytes())
    with socket.socket() as http, socket.socket() as https:
        http.bind(("0.0.0.0", 0))
        https.bind(("0.0.0.0", 0))
        port, https_port = http.getsockname()[1], https.getsockname()[1]
    override_path = run / "override.json"
    write_json(override_path, {"server": {"http_port": port, "https_port": https_port, "cert_dir": "certs"}, "recording": {"default_on": False, "out_dir": "sessions"}})
    removed = [name for name in os.environ if name.upper().startswith("PYTHON") or name.upper() in ("VIRTUAL_ENV", "CONDA_PREFIX")]
    env = {key: value for key, value in os.environ.items() if key not in removed}
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    env["PATH"] = os.pathsep.join(map(str, [app / "python", windows / "System32", windows / "System32/WindowsPowerShell/v1.0", windows]))
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    write_json(run / "package-environment.json", {"removed_variables": removed, "PATH": env["PATH"], "offline_models": True})
    data = run / "tmp/data"
    python = app / "python/python.exe"
    command = [str(python), "-B", "-m", "server.main", "--config", str(app / "config.yaml"), "--config-override", str(override_path), "--data-root", str(data)]
    probe_command = [str(python), "-B", "-c", PROBE, str(app), str(override_path), str(data), advertise_ip or ""]
    (run / "package-probe.py").write_text(PROBE, encoding="utf-8")
    with (run / "package-probe.stdout.log").open("wb") as stdout, (run / "package-probe.stderr.log").open("wb") as stderr:
        subprocess.run(probe_command, cwd=data, env=env, stdout=stdout, stderr=stderr, timeout=60, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    probe = json.loads((run / "package-probe.stdout.log").read_text(encoding="utf-8"))
    write_json(run / "package-runtime.json", probe)
    write_json(run / "effective-config.json", probe["config"])
    command += ["--advertise-ip", probe["public_ip"]]
    sources = {str(override_path): sha256(override_path), str(run / "package-files.json"): sha256(run / "package-files.json")}
    return {"command": command, "cwd": data, "env": env, "port": port, "models_dir": probe["models_dir"], "sources": sources,
            "package": {"archive": str(archive), "sha256": actual, "version": version, "root": str(package), "inventory": str(run / "package-files.json"), "runtime_probe": str(run / "package-runtime.json")}}


def verify_package(package):
    root = Path(package["root"])
    check_links(root)
    if sha256(package["archive"]) != package["sha256"]:
        raise RuntimeError("Original distribution ZIP changed since launch")
    expected = json.loads(Path(package["inventory"]).read_text(encoding="utf-8"))
    actual = {str(p.relative_to(root)).replace("\\", "/"): sha256(p) for p in root.rglob("*") if p.is_file()}
    if actual != expected:
        raise RuntimeError("Extracted distribution differs from launch inventory")
