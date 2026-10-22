"""#48: 配布パッケージの置き場所の警告と、展開物に残った MOTW の解除。

scripts/launcher.ps1 を PowerShell のサブプロセスで実行する。OneDrive の場所は環境変数を
一時ディレクトリに向けて差し替え、MOTW は NTFS の代替データストリーム（Zone.Identifier）を
テストで付けて確かめる。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "launcher.ps1"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows の起動ロジック")

ONEDRIVE_VARS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


def powershell(command: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if k not in ONEDRIVE_VARS}
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command",
         f"$ErrorActionPreference = 'Stop'; [Console]::OutputEncoding = [Text.Encoding]::UTF8; {command}"],
        capture_output=True, encoding="utf-8", timeout=30, env={**base, **(env or {})},
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


# ---- OneDrive の配下なら警告する（起動は止めない） --------------------------------------------


def location_warning(package: Path, env: dict[str, str]) -> tuple[bool, str]:
    result = powershell(
        f". '{LAUNCHER}'; $warned = Write-LocationWarning -PackageRoot '{package}'; "
        "Write-Output \"WARNED=$warned\"", env)
    assert result.returncode == 0, result.stderr
    return "WARNED=True" in result.stdout, result.stdout


@pytest.mark.parametrize("variable", ONEDRIVE_VARS)
def test_package_under_onedrive_is_warned(tmp_path, variable):
    onedrive = tmp_path / "OneDrive"
    package = onedrive / "ドキュメント" / "LinguaBridge"
    package.mkdir(parents=True)

    warned, output = location_warning(package, {variable: str(onedrive)})

    assert warned
    assert str(package) in output
    assert "OneDrive" in output
    assert "C:\\LinguaBridge\\" in output


def test_onedrive_match_ignores_case_and_trailing_separator(tmp_path):
    onedrive = tmp_path / "OneDrive"
    package = onedrive / "Desktop" / "LinguaBridge"
    package.mkdir(parents=True)

    warned, _ = location_warning(package, {"OneDrive": str(onedrive).upper() + "\\"})

    assert warned


@pytest.mark.parametrize("package_name", ["LinguaBridge", "OneDriveBackup/LinguaBridge"])
def test_package_outside_onedrive_is_not_warned(tmp_path, package_name):
    onedrive = tmp_path / "OneDrive"
    onedrive.mkdir()
    package = tmp_path / package_name
    package.mkdir(parents=True)

    warned, output = location_warning(package, {"OneDrive": str(onedrive)})

    assert not warned
    assert "OneDrive" not in output


def test_no_onedrive_on_this_pc_is_not_warned(tmp_path):
    package = tmp_path / "LinguaBridge"
    package.mkdir()

    warned, _ = location_warning(package, {})

    assert not warned


# ---- 展開物に残った MOTW を解除する ---------------------------------------------------------


def mark_of_the_web(path: Path) -> Path:
    touch(path)
    with open(f"{path}:Zone.Identifier", "w", encoding="ascii") as stream:
        stream.write("[ZoneTransfer]\r\nZoneId=3\r\n")
    return path


def has_motw(path: Path) -> bool:
    return os.path.exists(f"{path}:Zone.Identifier")


def clear_motw(package: Path) -> subprocess.CompletedProcess:
    result = powershell(
        f". '{LAUNCHER}'; $r = Clear-MarkOfTheWeb -AppRoot '{package / 'app'}'; "
        "Write-Output \"RESULT=$r\"")
    assert result.returncode == 0, result.stderr
    return result


def test_motw_under_app_and_start_bat_is_cleared_once_per_app(tmp_path):
    package = tmp_path / "LinguaBridge"
    marked = [
        mark_of_the_web(package / "start.bat"),
        mark_of_the_web(package / "app" / "scripts" / "run.ps1"),
        mark_of_the_web(package / "app" / "python" / "python.exe"),
        mark_of_the_web(package / "app" / "python" / "Lib" / "site-packages" / "x" / "native.pyd"),
    ]
    unmarked = touch(package / "app" / "config.yaml")

    first = clear_motw(package)

    assert "RESULT=cleared" in first.stdout
    assert not any(has_motw(path) for path in marked)
    assert unmarked.is_file()
    assert (package / "app" / ".motw-cleared").is_file()

    # 2回目以降は走査しない（同じ app のあいだは起動を遅らせない）
    assert "RESULT=not-needed" in clear_motw(package).stdout


def test_replaced_app_is_cleared_again(tmp_path):
    package = tmp_path / "LinguaBridge"
    touch(package / "start.bat")
    touch(package / "app" / "scripts" / "run.ps1")
    clear_motw(package)

    # 更新: app を丸ごと差し替える（マーカーも消える）
    for path in sorted((package / "app").rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    new_file = mark_of_the_web(package / "app" / "scripts" / "run.ps1")

    assert "RESULT=cleared" in clear_motw(package).stdout
    assert not has_motw(new_file)


def test_app_copied_over_the_old_one_is_cleared_again(tmp_path):
    # 更新で app を消さずに上から上書きコピーしても、版情報が変わればもう一度解除する
    package = tmp_path / "LinguaBridge"
    version = package / "app" / "version.json"
    version.parent.mkdir(parents=True)
    version.write_text('{"commit": "old"}', encoding="utf-8")
    clear_motw(package)

    version.write_text('{"commit": "new"}', encoding="utf-8")
    new_file = mark_of_the_web(package / "app" / "scripts" / "run.ps1")

    assert "RESULT=cleared" in clear_motw(package).stdout
    assert not has_motw(new_file)
    assert "RESULT=not-needed" in clear_motw(package).stdout


def test_data_is_left_untouched(tmp_path):
    package = tmp_path / "LinguaBridge"
    touch(package / "app" / "scripts" / "run.ps1")
    data_file = mark_of_the_web(package / "data" / "config.yaml")

    clear_motw(package)

    assert has_motw(data_file)


def test_failed_unblock_does_not_write_marker(tmp_path):
    package = tmp_path / "LinguaBridge"
    touch(package / "app" / "scripts" / "run.ps1")
    fake_unblock = ("function Unblock-File { [CmdletBinding()] param([string[]]$LiteralPath) "
                    "Write-Error -Message 'in use' -TargetObject $LiteralPath[0] }; ")

    result = powershell(
        f". '{LAUNCHER}'; {fake_unblock}"
        f"$r = Clear-MarkOfTheWeb -AppRoot '{package / 'app'}'; "
        "Write-Output \"RESULT=$r\"")

    assert result.returncode == 0, result.stderr
    assert "RESULT=failed" in result.stdout
    assert "次回の start.bat でもう一度" in result.stdout
    assert not (package / "app" / ".motw-cleared").exists()


def test_unexpected_error_does_not_stop_startup(tmp_path):
    # run.ps1 は $ErrorActionPreference = 'Stop'。走査やマーカーの書き込みで例外が出ても起動は続ける
    package = tmp_path / "LinguaBridge"
    touch(package / "app" / "scripts" / "run.ps1")
    fake_listing = "function Get-ChildItem { throw 'access denied' }; "

    result = powershell(
        f". '{LAUNCHER}'; {fake_listing}"
        f"$r = Clear-MarkOfTheWeb -AppRoot '{package / 'app'}'; Write-Output \"RESULT=$r\"")

    assert result.returncode == 0, result.stderr
    assert "RESULT=failed" in result.stdout
    assert "access denied" in result.stdout
    assert not (package / "app" / ".motw-cleared").exists()
