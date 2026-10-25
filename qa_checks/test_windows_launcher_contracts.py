"""Windows 起動・初回準備の安全な制御経路を隔離先で検証する。

実ホストのファイアウォール、電源設定、モデル、既存 ``.venv`` には触れない。
``scripts/run.ps1`` と ``start.bat`` はそのまま一時ディレクトリへコピーし、
重いセットアップとサーバー本体だけを決定的なスタブへ置き換える。
そのため、ここで証明するのは起動制御であり、クリーン Windows での実セットアップ
や実モデルのオフライン起動は別途必要である。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import scripts.download_models as downloader


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
CMD = shutil.which("cmd.exe") or shutil.which("cmd")


def _copy_python_launcher(target_root: Path) -> None:
    """一時 ``.venv`` に、この環境の Python ランチャーだけを複製する。"""

    target_venv = target_root / ".venv"
    target_scripts = target_venv / "Scripts"
    target_scripts.mkdir(parents=True)
    shutil.copy2(ROOT / ".venv" / "Scripts" / "python.exe", target_scripts / "python.exe")
    shutil.copy2(ROOT / ".venv" / "pyvenv.cfg", target_venv / "pyvenv.cfg")


def _make_launcher_sandbox(tmp_path: Path, *, setup_complete: bool) -> Path:
    root = tmp_path / "launcher-sandbox"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "run.ps1", root / "scripts" / "run.ps1")
    shutil.copy2(ROOT / "start.bat", root / "start.bat")
    _copy_python_launcher(root)

    server = root / "server"
    server.mkdir()
    (server / "__init__.py").write_text("", encoding="utf-8")
    (server / "main.py").write_text(
        """from pathlib import Path
import json
import sys

Path('server-invoked.json').write_text(
    json.dumps({'argv': sys.argv[1:]}, ensure_ascii=False),
    encoding='utf-8',
)
""",
        encoding="utf-8",
    )
    if setup_complete:
        (root / ".venv" / ".setup-complete").write_text("completed qa\n", encoding="utf-8")
    return root


def _run_ps1(root: Path) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None, "Windows PowerShell が見つからない"
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "scripts" / "run.ps1"),
        ],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
        check=False,
    )


def _write_setup_stub(root: Path, body: str) -> None:
    (root / "setup.ps1").write_text(body, encoding="utf-8-sig")


def _assert_server_invoked(root: Path) -> None:
    invocation = json.loads((root / "server-invoked.json").read_text(encoding="utf-8"))
    assert invocation == {"argv": ["--open-browser"]}


def _assert_interrupted_download_keeps_completed_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = [
        downloader.ModelSpec(
            name="model-a",
            repo_id="example/model-a",
            license="test",
            note="completed before interruption",
            kind="snapshot",
            subdir="model-a",
        ),
        downloader.ModelSpec(
            name="model-b",
            repo_id="example/model-b",
            license="test",
            note="interrupted then resumed",
            kind="snapshot",
            subdir="model-b",
        ),
    ]
    dest = tmp_path / "models"
    calls: list[tuple[str, Path]] = []
    b_attempts = 0

    def snapshot_download(*, repo_id: str, local_dir: Path, allow_patterns) -> None:
        del allow_patterns
        nonlocal b_attempts
        target = Path(local_dir)
        target.mkdir(parents=True, exist_ok=True)
        calls.append((repo_id, target))
        if repo_id == "example/model-a":
            payload = target / "weights.bin"
            if not payload.exists():
                payload.write_bytes(b"completed model A")
            return
        b_attempts += 1
        if b_attempts == 1:
            (target / "weights.partial").write_bytes(b"partial model B")
            raise KeyboardInterrupt
        (target / "weights.bin").write_bytes(b"completed model B")

    monkeypatch.setattr(downloader, "SPECS", specs)
    monkeypatch.setattr(downloader, "snapshot_download", snapshot_download)
    monkeypatch.setattr(sys, "argv", ["download_models.py", "--dest", str(dest)])

    with pytest.raises(KeyboardInterrupt):
        downloader.main()

    model_a = dest / "model-a" / "weights.bin"
    model_b_partial = dest / "model-b" / "weights.partial"
    first_bytes = model_a.read_bytes()
    first_hash = hashlib.sha256(first_bytes).hexdigest()
    first_mtime_ns = model_a.stat().st_mtime_ns
    first_manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert set(first_manifest) == {"model-a"}
    assert first_manifest["model-a"]["bytes"] == len(first_bytes)
    assert model_b_partial.read_bytes() == b"partial model B"

    assert downloader.main() == 0

    assert model_a.read_bytes() == first_bytes
    assert hashlib.sha256(model_a.read_bytes()).hexdigest() == first_hash
    assert model_a.stat().st_mtime_ns == first_mtime_ns
    final_manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert set(final_manifest) == {"model-a", "model-b"}
    assert (dest / "model-b" / "weights.bin").read_bytes() == b"completed model B"
    assert [target for repo, target in calls if repo == "example/model-a"] == [
        dest / "model-a",
        dest / "model-a",
    ]
    assert [target for repo, target in calls if repo == "example/model-b"] == [
        dest / "model-b",
        dest / "model-b",
    ]


def test_prepared_launcher_skips_setup_and_starts_browser_entrypoint(tmp_path: Path) -> None:
    """完了マーカーがあれば、再取得せず先生画面付き起動へ進む。"""

    root = _make_launcher_sandbox(tmp_path, setup_complete=True)
    _write_setup_stub(
        root,
        "Set-Content -LiteralPath (Join-Path $PSScriptRoot 'setup-called.txt') -Value 'called'\n"
        "throw 'prepared launch must not call setup'\n",
    )

    result = _run_ps1(root)

    assert result.returncode == 0, result.stdout
    assert not (root / "setup-called.txt").exists()
    _assert_server_invoked(root)


def test_interrupted_setup_is_not_complete_and_next_launch_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失敗後は未完了のまま、次回起動で setup を再度呼び出す。"""

    root = _make_launcher_sandbox(tmp_path, setup_complete=False)
    attempts = root / "setup-attempts.txt"
    _write_setup_stub(
        root,
        f"Add-Content -LiteralPath '{attempts}' -Value 'failed'\n"
        "exit 37\n",
    )

    first = _run_ps1(root)

    assert first.returncode != 0, first.stdout
    assert attempts.read_text(encoding="utf-8").splitlines() == ["failed"]
    assert not (root / ".venv" / ".setup-complete").exists()
    assert not (root / "server-invoked.json").exists()

    _write_setup_stub(
        root,
        f"Add-Content -LiteralPath '{attempts}' -Value 'succeeded'\n"
        "$marker = Join-Path $PSScriptRoot '.venv\\.setup-complete'\n"
        "Set-Content -LiteralPath $marker -Value 'completed qa'\n"
        "& $env:ComSpec /d /c exit 0\n",
    )

    second = _run_ps1(root)

    assert second.returncode == 0, second.stdout
    assert attempts.read_text(encoding="utf-8").splitlines() == ["failed", "succeeded"]
    assert (root / ".venv" / ".setup-complete").is_file()
    _assert_server_invoked(root)
    _assert_interrupted_download_keeps_completed_payload(
        tmp_path / "download-retry", monkeypatch
    )


def test_start_bat_shim_reaches_the_same_prepared_launcher(tmp_path: Path) -> None:
    """利用者がダブルクリックするバッチも PowerShell 起動経路へ到達する。"""

    assert CMD is not None, "cmd.exe が見つからない"
    root = _make_launcher_sandbox(tmp_path, setup_complete=True)
    _write_setup_stub(root, "throw 'setup must not run'\n")

    result = subprocess.run(
        [CMD, "/d", "/c", str(root / "start.bat")],
        cwd=root,
        input="\n",
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout
    _assert_server_invoked(root)


def test_setup_marker_follows_every_required_native_step() -> None:
    """依存・モデル・証明書の成功確認より前に完了扱いしない。"""

    setup = (ROOT / "setup.ps1").read_text(encoding="utf-8-sig")
    marker = setup.index('Set-Content -Encoding UTF8 "$root\\.venv\\.setup-complete"')
    required_in_order = [
        "-m pip install --upgrade pip",
        'Assert-ExitOk "pip の更新"',
        '-m pip install -r "$root\\requirements.txt"',
        'Assert-ExitOk "依存パッケージのインストール"',
        '"$root\\scripts\\download_models.py"',
        'Assert-ExitOk "モデルのダウンロード"',
        '"$root\\scripts\\make_cert.py"',
        'Assert-ExitOk "証明書の生成"',
        "from server.config import load_config",
        'Assert-ExitOk "設定の読み込み"',
    ]
    positions = [setup.index(fragment) for fragment in required_in_order]

    assert positions == sorted(positions)
    assert all(position < marker for position in positions)
    assert '$ErrorActionPreference = "Stop"' in setup[:marker]


def test_admin_and_non_admin_setup_policies_are_both_explicit() -> None:
    """管理者時の受信許可・AC電源設定と、非管理者時の案内を固定する。"""

    setup = (ROOT / "setup.ps1").read_text(encoding="utf-8-sig")
    admin_block = setup[setup.index("if ($isAdmin) {") :]

    assert "New-NetFirewallRule" in admin_block
    assert "-Direction Inbound" in admin_block
    assert "-Protocol TCP" in admin_block
    assert "-LocalPort $p" in admin_block
    assert "powercfg /change standby-timeout-ac 0" in admin_block
    assert "powercfg /change hibernate-timeout-ac 0" in admin_block
    assert "管理者権限がありません" in setup
    assert "ファイアウォール許可とスリープ無効化はスキップされます" in setup


def test_powershell_launch_scripts_parse_without_errors() -> None:
    """出荷する PowerShell 2本を Windows PowerShell 自身の parser で検査する。"""

    assert POWERSHELL is not None, "Windows PowerShell が見つからない"
    parser = (
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:QA_PARSE_PATH, [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    for script in (ROOT / "setup.ps1", ROOT / "scripts" / "run.ps1"):
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-Command", parser],
            cwd=ROOT,
            env={**os.environ, "QA_PARSE_PATH": str(script)},
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, f"{script}:\n{result.stdout}"


def test_start_bat_remains_ascii_and_invokes_the_bundled_run_script() -> None:
    """cmd.exe のコードページに依存しない薄いシムを維持する。"""

    batch = (ROOT / "start.bat").read_bytes()
    text = batch.decode("ascii")

    assert 'cd /d "%~dp0"' in text
    assert 'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\\run.ps1"' in text
    assert "pause" in text.lower().splitlines()
