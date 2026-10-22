"""#46: 起動ロジックは同梱の Python の有無で配布モード／開発モードを選ぶ。

scripts/launcher.ps1 の判定を PowerShell のサブプロセスで実行し、選ばれた計画だけを見る
（サーバーや setup.ps1 は起動しない）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "launcher.ps1"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows の起動ロジック")


def launch_plan(app_root: Path) -> dict:
    command = (
        f"$ErrorActionPreference = 'Stop'; . '{LAUNCHER}'; "
        f"Get-LaunchPlan -AppRoot '{app_root}' | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", command],
        capture_output=True, encoding="utf-8", timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_bundled_python_selects_distribution_mode_with_data_root(tmp_path):
    app = tmp_path / "LinguaBridge" / "app"
    python = touch(app / "python" / "python.exe")
    data = tmp_path / "LinguaBridge" / "data"

    plan = launch_plan(app)

    assert plan["Mode"] == "distribution"
    assert Path(plan["Python"]) == python
    assert Path(plan["DataRoot"]) == data
    # 配布モードでは setup.ps1（ネット取得）を呼ばない
    assert plan["NeedsSetup"] is False
    assert plan["ConfigArgs"] == [
        "--config", str(app / "config.yaml"),
        "--config-override", str(data / "config.yaml"),
        "--data-root", str(data),
    ]


@pytest.mark.parametrize("setup_done", [False, True])
def test_without_bundled_python_selects_development_venv(tmp_path, setup_done):
    repo = tmp_path / "repo"
    repo.mkdir()
    if setup_done:
        touch(repo / ".venv" / ".setup-complete")

    plan = launch_plan(repo)

    assert plan["Mode"] == "development"
    assert Path(plan["Python"]) == repo / ".venv" / "Scripts" / "python.exe"
    assert plan["DataRoot"] is None
    assert plan["NeedsSetup"] is (not setup_done)
    # 開発機では今と同じく引数なし（config.yaml・リポジトリのルート基準）
    assert plan["ConfigArgs"] == []
