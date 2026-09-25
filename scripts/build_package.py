"""配布パッケージ（zip）を開発機でビルドする（#46、ADR 0001）。

    .venv\\Scripts\\python scripts\\build_package.py

埋め込み版 Python 3.12 に依存・コード・Web・配布用の既定の設定・同梱モデル・
VC++ ランタイムのインストーラー・版情報を載せた本体領域（app）を組み立て、
自己検証がすべて通ったときだけ zip にする:

  (1) 同梱したネイティブDLLの依存先が「同梱物・Windows 標準・VC++ ランタイム」のどれか
  (2) 実エンジンのネイティブモジュールを同梱の Python で import できる
  (3) 同梱の Python から、フェイクのエンジン構成と一時的なデータルートでサーバーを起動し、
      準備完了のエンドポイントが 200 を返す

作業領域と出力先は既定で %LOCALAPPDATA%/LinguaBridge 配下（GB級のため OneDrive の外、plan.md R-08）。
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.download_models import SPECS, ModelSpec  # noqa: E402
from server.config import load_config  # noqa: E402

EMBED_PYTHON_VERSION = "3.12.10"
EMBED_PYTHON_URL = (
    f"https://www.python.org/ftp/python/{EMBED_PYTHON_VERSION}/"
    f"python-{EMBED_PYTHON_VERSION}-embed-amd64.zip"
)
# 初回取得時に記録した値。展開後の python.exe の署名（Python Software Foundation）も確かめる
EMBED_PYTHON_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

# 既定構成（reazonspeech-k2-v2 と hy-mt2）と予備の NLLB（本体とトークナイザ）。whisper 系は運ばない
BUNDLED_MODELS = ("reazonspeech", "hy-mt2", "nllb", "nllb-tokenizer")
SPECS_BY_NAME = {spec.name: spec for spec in SPECS}
NATIVE_MODULES = ("sherpa_onnx", "llama_cpp", "ctranslate2", "onnxruntime")

# 依存先として許す Windows 標準のDLL。まっさらな Windows 11 にあるものだけを並べる。
# 未知の依存でビルドが止まったら、それが本当に Windows 標準か確かめてから足すこと
WINDOWS_SYSTEM_DLLS = frozenset({
    "advapi32.dll", "avicap32.dll", "bcrypt.dll", "bcryptprimitives.dll", "cabinet.dll",
    "cfgmgr32.dll", "combase.dll", "comctl32.dll", "comdlg32.dll", "crypt32.dll",
    "cryptbase.dll", "dbghelp.dll", "dnsapi.dll", "dwmapi.dll", "dxgi.dll", "d3d11.dll",
    "d3d12.dll", "gdi32.dll", "gdiplus.dll", "imagehlp.dll", "imm32.dll", "iphlpapi.dll",
    "kernel32.dll", "kernelbase.dll", "mpr.dll", "msi.dll", "msvcrt.dll", "mswsock.dll",
    "ncrypt.dll", "netapi32.dll", "normaliz.dll", "ntdll.dll", "ole32.dll", "oleaut32.dll",
    "pdh.dll", "powrprof.dll", "propsys.dll", "psapi.dll", "rpcrt4.dll", "secur32.dll",
    "setupapi.dll", "shell32.dll", "shlwapi.dll", "ucrtbase.dll", "user32.dll", "userenv.dll",
    "uxtheme.dll", "version.dll", "winhttp.dll", "wininet.dll", "winmm.dll", "wintrust.dll",
    "ws2_32.dll", "wtsapi32.dll",
})
# API セット（UCRT を含む）は Windows 10 以降の標準
WINDOWS_API_SET_PREFIXES = ("api-ms-win-", "ext-ms-win-")
# vc_redist.x64.exe が入れるもの。初回処理で同梱のインストーラーから入れる
VC_RUNTIME_DLLS = frozenset({
    "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll",
    "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
    "concrt140.dll", "vccorlib140.dll", "vcomp140.dll", "vcamp140.dll",
})

MODELS_DIR_LINE = re.compile(r'^(  dir: )"%LOCALAPPDATA%/LinguaBridge/models"$', re.MULTILINE)
CODE_FILES = (
    "scripts/__init__.py", "scripts/run.ps1", "scripts/launcher.ps1", "scripts/make_cert.py",
    "scripts/diagnose_windows.ps1", "scripts/network_interfaces.ps1",
)
READY_TIMEOUT_S = 120


class BuildError(Exception):
    """ビルドを止める理由。メッセージはそのまま開発者に見せる。"""


# ---- 純粋な判定（tests/unit/test_build_package.py） ------------------------------------------


def _is_known_system_dependency(name: str) -> bool:
    return (
        name in WINDOWS_SYSTEM_DLLS
        or name in VC_RUNTIME_DLLS
        or name.startswith(WINDOWS_API_SET_PREFIXES)
    )


def unknown_dependencies(
    imports: Mapping[str, Iterable[str]], bundled: Iterable[str],
) -> dict[str, list[str]]:
    """モジュールごとの依存先のうち、同梱物・Windows 標準・VC++ ランタイムのどれでもないもの。"""
    known = {name.lower() for name in bundled}
    unknown: dict[str, list[str]] = {}
    for module, dependencies in imports.items():
        missing = [
            dep for dep in dependencies
            if dep.lower() not in known and not _is_known_system_dependency(dep.lower())
        ]
        if missing:
            unknown[module] = missing
    return unknown


def _visible_files(directory: Path) -> list[Path]:
    """ダウンロード作業領域（.cache 等の隠しファイル）を除いたファイル。"""
    return sorted(
        path for path in directory.rglob("*")
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(directory).parts)
    )


def _spec_files(spec: ModelSpec, directory: Path) -> tuple[list[Path], str | None]:
    """モデルの目録（download_models.SPECS）と照合した同梱ファイルと、欠けているもの。"""
    if not directory.is_dir():
        return [], "フォルダがありません"
    files = _visible_files(directory)
    if spec.kind == "gguf":
        matches = [f for f in files if fnmatch.fnmatch(f.name.lower(), spec.gguf_pattern.lower())]
        return (matches[:1], None) if matches else ([], f"{spec.gguf_pattern} がありません")
    if spec.allow_patterns is None:
        return (files, None) if files else ([], "ファイルがありません")
    chosen: list[Path] = []
    for pattern in spec.allow_patterns:
        matches = [f for f in files if fnmatch.fnmatch(f.relative_to(directory).as_posix(), pattern)]
        if not matches:
            return [], f"{pattern} がありません"
        chosen.extend(matches)
    return chosen, None


def bundled_model_files(models_dir: Path) -> list[Path]:
    """同梱するモデルのファイル一覧。1つでも欠けていれば取得方法つきで止める。"""
    files: list[Path] = []
    problems: list[str] = []
    for name in BUNDLED_MODELS:
        spec = SPECS_BY_NAME[name]
        directory = models_dir / spec.subdir
        found, problem = _spec_files(spec, directory)
        if problem is not None:
            problems.append(
                f"  - {name}: {directory}（{problem}）\n"
                f"      取得: .venv\\Scripts\\python scripts/download_models.py --only {name}"
            )
        files.extend(found)
    if problems:
        raise BuildError(
            "同梱するモデルが開発機に揃っていません。取得してからビルドし直してください:\n"
            + "\n".join(problems)
        )
    return files


# ---- ビルドの各段階 --------------------------------------------------------------------------


def _step(message: str) -> None:
    print(f"\n=== {message} ===", flush=True)


def _run(command: list[str], what: str, **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(command, **kwargs)
    if result.returncode != 0:
        raise BuildError(f"{what} に失敗しました（終了コード {result.returncode}）")
    return result


def _download(url: str, dest: Path) -> Path:
    """dest が無ければ取得する（作業領域にキャッシュし、再ビルドでは取りに行かない）。"""
    if dest.is_file():
        print(f"キャッシュを使用: {dest}")
        return dest
    print(f"取得: {url}")
    partial = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
    except (OSError, urllib.error.URLError) as exc:
        raise BuildError(f"{url} を取得できませんでした: {exc}") from exc
    partial.replace(dest)
    return dest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def install_embedded_python(cache: Path, python_dir: Path) -> Path:
    archive = _download(EMBED_PYTHON_URL, cache / Path(EMBED_PYTHON_URL).name)
    if _sha256(archive) != EMBED_PYTHON_SHA256:
        archive.unlink()
        raise BuildError(f"{archive.name} のハッシュが固定値と一致しません（取得し直してください）")
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(python_dir)
    # 埋め込み版は ._pth で sys.path を固定する。site-packages と app（コードの置き場）を足し、
    # site を有効にする（依存パッケージの .pth を処理させる）
    python = python_dir / "python.exe"
    _require_signature(python, "Python Software Foundation")
    (pth,) = python_dir.glob("python3*._pth")
    lines = [line for line in pth.read_text(encoding="utf-8").splitlines() if "import site" not in line]
    pth.write_text("\n".join([*lines, "Lib\\site-packages", "..", "import site", ""]), encoding="utf-8")
    return python


def install_dependencies(site_packages: Path, work_dir: Path) -> None:
    # 開発機の Python（3.12・win_amd64）の pip で、同じ ABI の wheel だけを同梱先に入れる。
    # 版は開発機の .venv に入っているもの（テストを通した版）に揃える。
    # CPU 版 llama-cpp の追加インデックスは requirements.txt に書かれたものを使う
    frozen = _run(
        [sys.executable, "-m", "pip", "list", "--format=freeze", "--exclude-editable",
         "--disable-pip-version-check"],
        "開発機の依存の版の取得", capture_output=True, encoding="utf-8",
    ).stdout
    constraints = work_dir / "constraints.txt"
    constraints.write_text(frozen, encoding="utf-8")
    _run(
        [sys.executable, "-m", "pip", "install", "--target", str(site_packages),
         "--only-binary=:all:", "--no-warn-script-location", "--disable-pip-version-check",
         "-r", str(ROOT / "requirements.txt"), "-c", str(constraints)],
        "依存パッケージの同梱",
    )
    # --target では wheel のデータ領域・コンソールスクリプトが site-packages\bin に落ちる。
    # 読み込まれない（llama_cpp は llama_cpp\lib の DLL を使う）うえ、llama.cpp の CLI 用
    # llama-common.dll は同梱しない OpenSSL に依存するので運ばない
    shutil.rmtree(site_packages / "bin", ignore_errors=True)


def copy_code(app: Path, stage: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "server", app / "server", ignore=ignore)
    shutil.copytree(ROOT / "web", app / "web", ignore=ignore)
    for relative in CODE_FILES:
        target = app / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    shutil.copy2(ROOT / "start.bat", stage / "start.bat")
    write_distribution_config(ROOT / "config.yaml", app / "config.yaml")


def write_distribution_config(source: Path, dest: Path) -> None:
    """配布用の既定の設定。開発機の config.yaml から、モデルの場所だけを app 内に変える。"""
    text, count = MODELS_DIR_LINE.subn(
        r"\1models/   # 配布パッケージ: app\\models（app 基準の相対）",
        source.read_text(encoding="utf-8"),
    )
    if count != 1:
        raise BuildError(f"{source} の models.dir を配布用に置き換えられませんでした（書式が変わった？）")
    dest.write_text(text, encoding="utf-8")
    if load_config(dest).models.dir != "models/":
        raise BuildError("配布用の既定の設定で models.dir が app 内を指していません")


def copy_models(models_dir: Path, files: list[Path], dest: Path) -> None:
    for source in files:
        target = dest / source.relative_to(models_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    # 同梱モデルの出所とライセンス（NLLB は CC-BY-NC）を app 内に残す
    manifest = {
        name: {
            "repo_id": SPECS_BY_NAME[name].repo_id,
            "license": SPECS_BY_NAME[name].license,
            "note": SPECS_BY_NAME[name].note,
            "path": SPECS_BY_NAME[name].subdir,
        }
        for name in BUNDLED_MODELS
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def fetch_vc_redist(cache: Path, dest: Path) -> None:
    installer = _download(VC_REDIST_URL, cache / "vc_redist.x64.exe")
    try:
        _require_signature(installer, "Microsoft Corporation")
    except BuildError:
        installer.unlink()  # 次のビルドで取り直す
        raise
    shutil.copy2(installer, dest)


def _require_signature(path: Path, organization: str) -> None:
    """Authenticode 署名が有効で、署名者が organization であること。"""
    signature = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
         "\"$($s.Status)|$($s.SignerCertificate.Subject)\""],
        capture_output=True, encoding="utf-8",
    ).stdout.strip()
    status, _, subject = signature.partition("|")
    if status != "Valid" or f"O={organization}" not in subject:
        raise BuildError(f"{path.name} の署名を確認できませんでした: {signature or '取得失敗'}")


def git_commit() -> tuple[str, bool]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, encoding="utf-8", check=True
        ).stdout.strip()

    try:
        return git("rev-parse", "HEAD"), bool(git("status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BuildError(f"コミットを取得できませんでした: {exc}") from exc


def write_version(app: Path, built_at: datetime, commit: str, dirty: bool) -> dict:
    version = {
        "built_at": built_at.isoformat(timespec="seconds"),
        "commit": commit,
        "dirty": dirty,  # 未コミットの変更を含んだビルドか
        "python": EMBED_PYTHON_VERSION,
        "models": list(BUNDLED_MODELS),
    }
    (app / "version.json").write_text(json.dumps(version, indent=2), encoding="utf-8")
    return version


# ---- 自己検証 --------------------------------------------------------------------------------


def native_imports(app: Path) -> tuple[dict[str, list[str]], list[str]]:
    """同梱した全ネイティブモジュールの import テーブルと、同梱物のファイル名。"""
    import pefile  # type: ignore[import-untyped]

    binaries = [p for p in app.rglob("*") if p.suffix.lower() in {".dll", ".pyd", ".exe"}]
    imports: dict[str, list[str]] = {}
    for binary in binaries:
        if binary.suffix.lower() == ".exe":
            continue  # vc_redist などのインストーラー・ランチャーは読み込まれるモジュールではない
        try:
            pe = pefile.PE(str(binary), fast_load=True)
            pe.parse_data_directories(
                directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
            )
        except pefile.PEFormatError as exc:
            raise BuildError(f"DLL を解析できません: {binary}: {exc}") from exc
        imports[binary.relative_to(app).as_posix()] = [
            entry.dll.decode("ascii", "replace") for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])
        ]
        pe.close()
    return imports, [binary.name for binary in binaries]


def verify_dll_dependencies(app: Path) -> None:
    imports, bundled = native_imports(app)
    unknown = unknown_dependencies(imports, bundled)
    if unknown:
        lines = [f"  {module}: {', '.join(deps)}" for module, deps in sorted(unknown.items())]
        raise BuildError(
            "同梱物・Windows 標準・VC++ ランタイムのどれでもない依存先があります"
            "（サーバーPCで DLL が見つからず止まります）:\n" + "\n".join(lines)
        )
    print(f"DLL 依存: {len(imports)} モジュールすべて既知の依存先のみ")


def _clean_env() -> dict[str, str]:
    """開発機の Python 設定を同梱の Python に持ち込まない。"""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}


def verify_native_modules(python: Path) -> None:
    _run(
        [str(python), "-B", "-c", f"import {', '.join(NATIVE_MODULES)}"],
        f"同梱の Python での import（{', '.join(NATIVE_MODULES)}）",
        env=_clean_env(),
    )
    print(f"import: {', '.join(NATIVE_MODULES)}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _advertise_ip() -> str:
    from server.network import list_addresses

    usable = [item.ip for item in list_addresses() if item.usable]
    if not usable:
        raise BuildError("自己検証でサーバーを起動するには、稼働中のネットワークが1つ必要です")
    return usable[0]


def verify_server_ready(app: Path, python: Path) -> None:
    http_port, https_port = _free_port(), _free_port()
    with tempfile.TemporaryDirectory(prefix="linguabridge-selfcheck-") as temp:
        data = Path(temp)
        override = data / "config.yaml"
        override.write_text(
            f"server: {{ http_port: {http_port}, https_port: {https_port} }}\n"
            "asr: { engine: fake }\nmt: { engine: fake }\n",
            encoding="utf-8",
        )
        log = data / "server.log"
        with log.open("wb") as out:
            server = subprocess.Popen(
                [str(python), "-B", "-m", "server.main",
                 "--config", str(app / "config.yaml"), "--config-override", str(override),
                 "--data-root", str(data), "--advertise-ip", _advertise_ip()],
                cwd=data, env=_clean_env(), stdout=out, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            try:
                status = _wait_ready(f"http://127.0.0.1:{http_port}/ready", server)
            finally:
                server.kill()
                server.wait(timeout=30)
        if status != 200:
            raise BuildError(
                f"同梱の Python でサーバーが準備完了になりませんでした（/ready: {status}）。ログ:\n"
                + log.read_text(encoding="utf-8", errors="replace")[-4000:]
            )
    print(f"サーバー: /ready 200（フェイクのエンジン構成・一時的なデータルート）")


def _wait_ready(url: str, server: subprocess.Popen) -> int | str:
    deadline = time.monotonic() + READY_TIMEOUT_S
    status: int | str = "応答なし"
    while time.monotonic() < deadline:
        if server.poll() is not None:
            return f"プロセスが終了（終了コード {server.returncode}）"
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            status = exc.code  # 503 = モデル起動中
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    return f"{READY_TIMEOUT_S}秒以内に 200 にならず（最後: {status}）"


# ---- zip 化 ----------------------------------------------------------------------------------


def write_zip(stage: Path, dest: Path) -> None:
    partial = dest.with_suffix(".zip.part")
    with zipfile.ZipFile(partial, "w", allowZip64=True) as bundle:
        for path in sorted(stage.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(stage)
            # モデルは圧縮が効かないので格納のみ（ビルド時間を抑える）
            method = zipfile.ZIP_STORED if relative.parts[:2] == ("app", "models") else zipfile.ZIP_DEFLATED
            bundle.write(path, relative.as_posix(), compress_type=method)
    partial.replace(dest)


def build(work_dir: Path, out_dir: Path, models_dir: Path) -> Path:
    _step("同梱するモデルの確認")
    model_files = bundled_model_files(models_dir)
    print(f"{models_dir} から {len(model_files)} ファイル")

    built_at = datetime.now(timezone.utc).astimezone()
    commit, dirty = git_commit()
    cache = work_dir / "cache"
    stage = work_dir / "stage"
    app = stage / "app"
    cache.mkdir(parents=True, exist_ok=True)
    if stage.exists():
        shutil.rmtree(stage)
    app.mkdir(parents=True)

    _step(f"埋め込み版 Python {EMBED_PYTHON_VERSION}")
    python = install_embedded_python(cache, app / "python")
    _step("依存パッケージ")
    install_dependencies(app / "python" / "Lib" / "site-packages", work_dir)
    _step("コード・Web・既定の設定")
    copy_code(app, stage)
    _step("モデル")
    copy_models(models_dir, model_files, app / "models")
    _step("VC++ ランタイムのインストーラー")
    fetch_vc_redist(cache, app / "vc_redist.x64.exe")
    _step("版情報")
    version = write_version(app, built_at, commit, dirty)
    print(json.dumps(version, ensure_ascii=False))

    _step("自己検証 (1) ネイティブDLLの依存")
    verify_dll_dependencies(app)
    _step("自己検証 (2) 実エンジンのネイティブモジュール")
    verify_native_modules(python)
    _step("自己検証 (3) 同梱の Python でサーバーの準備完了")
    verify_server_ready(app, python)

    _step("zip 化")
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-dirty" if dirty else ""
    dest = out_dir / f"LinguaBridge-{built_at:%Y%m%d-%H%M}-{commit[:7]}{suffix}.zip"
    write_zip(stage, dest)
    return dest


def main() -> int:
    local = Path(os.path.expandvars("%LOCALAPPDATA%")) / "LinguaBridge"
    parser = argparse.ArgumentParser(description="配布パッケージの zip をビルドする（#46）")
    parser.add_argument("--work-dir", type=Path, default=local / "build",
                        help="作業領域（取得物のキャッシュと組み立て中の app）")
    parser.add_argument("--out-dir", type=Path, default=local / "dist", help="zip の出力先")
    parser.add_argument("--models-dir", type=Path, default=None,
                        help="同梱するモデルの取得元（既定: config.yaml の models.dir）")
    args = parser.parse_args()
    models_dir = args.models_dir or load_config(ROOT / "config.yaml").models.resolved_dir
    try:
        dest = build(args.work_dir, args.out_dir, models_dir)
    except BuildError as exc:
        print(f"\n[ビルド失敗] {exc}", file=sys.stderr)
        return 1
    print(f"\n完了: {dest}（{dest.stat().st_size / 1e9:.2f} GB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
