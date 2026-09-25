"""#47: 配布モードの初回処理（UAC 昇格）と、setup.ps1 と共通のファイアウォール・電源処理。

scripts/launcher.ps1 と scripts/os_setup.ps1 を PowerShell のサブプロセスで実行する。
昇格・証明書生成・ファイアウォール・powercfg は偽の実行関数に差し替え、OS は変更しない。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "launcher.ps1"
OS_SETUP = ROOT / "scripts" / "os_setup.ps1"

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows の起動ロジック")


def powershell(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command",
         f"$ErrorActionPreference = 'Stop'; [Console]::OutputEncoding = [Text.Encoding]::UTF8; {command}"],
        capture_output=True, encoding="utf-8", timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


# ---- 初回処理の要否（完了マーカーは data に置く） ---------------------------------------------


def launch_plan(app_root: Path) -> dict:
    result = powershell(f". '{LAUNCHER}'; Get-LaunchPlan -AppRoot '{app_root}' | ConvertTo-Json -Compress")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_distribution_needs_first_run_until_marker_is_in_data(tmp_path):
    app = tmp_path / "LinguaBridge" / "app"
    touch(app / "python" / "python.exe")

    assert launch_plan(app)["NeedsFirstRun"] is True

    touch(tmp_path / "LinguaBridge" / "data" / ".first-run-complete")
    # 証明書が無ければ、UAC 無しで証明書だけを作り直す（Invoke-FirstRun）
    assert launch_plan(app)["NeedsFirstRun"] is True

    touch(tmp_path / "LinguaBridge" / "data" / "certs" / "cert.pem")
    assert launch_plan(app)["NeedsFirstRun"] is False


def test_replaced_app_keeps_first_run_marker_and_certificate_from_data(tmp_path):
    data = tmp_path / "LinguaBridge" / "data"
    data_marker = touch(data / ".first-run-complete")
    cert = touch(data / "certs" / "cert.pem")
    app = tmp_path / "LinguaBridge" / "app"
    touch(app / "python" / "python.exe")

    plan = launch_plan(app)

    assert Path(plan["FirstRunMarker"]) == data_marker
    assert Path(plan["CertFile"]) == cert
    assert Path(plan["FirstRunLog"]) == data / "first-run.log"
    assert plan["NeedsFirstRun"] is False


def test_development_mode_never_runs_first_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    assert launch_plan(repo)["NeedsFirstRun"] is False


# ---- 初回処理の実行（昇格・証明書は偽の実行関数） ---------------------------------------------


def run_first_run(data: Path, *, elevate: str, cert: str = "0") -> tuple[str, list[str], str]:
    """偽の昇格と証明書生成で Invoke-FirstRun を実行し、(状態, 呼ばれた順, 表示) を返す。"""
    command = (
        f". '{LAUNCHER}'; $script:calls = @(); "
        "$makeCert = { $script:calls += 'cert'; " + cert + " }; "
        "$elevate = { $script:calls += 'elevate'; " + elevate + " }; "
        f"$status = Invoke-FirstRun -Marker '{data / '.first-run-complete'}' "
        f"-CertFile '{data / 'certs' / 'cert.pem'}' -LogPath '{data / 'first-run.log'}' "
        "-HttpPort 8080 -HttpsPort 8443 -MakeCert $makeCert -Elevate $elevate; "
        "Write-Output \"STATUS=$status\"; Write-Output \"CALLS=$($script:calls -join ',')\""
    )
    result = powershell(command)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    status = next(line for line in lines if line.startswith("STATUS=")).removeprefix("STATUS=")
    calls = next(line for line in lines if line.startswith("CALLS=")).removeprefix("CALLS=")
    return status, [c for c in calls.split(",") if c], result.stdout


def test_approved_first_run_generates_cert_outside_elevation_and_writes_marker(tmp_path):
    data = tmp_path / "data"

    status, calls, _ = run_first_run(data, elevate="0")

    assert status == "completed"
    # 証明書は昇格の外で、VC++ ランタイムを入れた後に生成する
    assert calls == ["elevate", "cert"]
    assert (data / ".first-run-complete").is_file()


def test_marker_and_certificate_skip_uac_and_certificate(tmp_path):
    data = tmp_path / "data"
    touch(data / ".first-run-complete")
    touch(data / "certs" / "cert.pem")

    status, calls, _ = run_first_run(data, elevate="0")

    assert status == "not-needed"
    assert calls == []


def test_missing_certificate_is_made_again_without_uac(tmp_path):
    data = tmp_path / "data"
    touch(data / ".first-run-complete")

    status, calls, _ = run_first_run(data, elevate="0")

    assert status == "completed"
    assert calls == ["cert"]


def test_denied_uac_explains_skipped_items_and_retry_without_marker(tmp_path):
    data = tmp_path / "data"

    # 昇格の拒否は $null（Start-ElevatedScript が UAC の取り消しを見分けた）で表す
    status, calls, output = run_first_run(data, elevate="$null")

    assert status == "denied"
    # 拒否しても証明書は作る
    assert calls == ["elevate", "cert"]
    assert not (data / ".first-run-complete").exists()
    for skipped in ("Visual C++ ランタイム", "ファイアウォール", "TCP 8080, 8443", "スリープ"):
        assert skipped in output
    assert "start.bat をもう一度実行" in output
    assert "「はい」" in output


def test_failed_elevated_setup_does_not_write_marker(tmp_path):
    data = tmp_path / "data"

    status, _, output = run_first_run(data, elevate="1")

    assert status == "failed"
    assert not (data / ".first-run-complete").exists()
    assert str(data / "first-run.log") in output


@pytest.mark.parametrize("cert_exit, expected", [("3", "completed"), ("1", "failed")])
def test_certificate_failure_does_not_ask_uac_again(tmp_path, cert_exit, expected):
    # 3 は既存の証明書を変えずに残した（setup.ps1 と同じく警告で続行）。1 は生成の失敗。
    # どちらでも昇格した処理が済めばマーカーを書き、次回の UAC は出さない
    data = tmp_path / "data"

    status, _, output = run_first_run(data, elevate="0", cert=cert_exit)

    assert status == expected
    assert (data / ".first-run-complete").is_file()
    assert "証明書" in output


def start_elevated(fake_start_process: str) -> str:
    result = powershell(
        f". '{LAUNCHER}'; function Start-Process {{ {fake_start_process} }}; "
        "$r = Start-ElevatedScript -Arguments '-File x.ps1'; "
        "if ($null -eq $r) { Write-Output 'EXIT=null' } else { Write-Output \"EXIT=$r\" }"
    )
    assert result.returncode == 0, result.stderr
    return next(line for line in result.stdout.splitlines() if line.startswith("EXIT="))


def test_uac_cancel_is_reported_as_denied():
    cancelled = ("throw (New-Object InvalidOperationException 'canceled', "
                 "(New-Object ComponentModel.Win32Exception 1223))")

    assert start_elevated(cancelled) == "EXIT=null"


def test_other_start_failures_are_not_mistaken_for_denial():
    assert start_elevated("throw (New-Object ComponentModel.Win32Exception 2)") == "EXIT=-1"


def test_elevated_exit_code_is_returned():
    # 実物の代わりに、終了コード 7 で終わるプロセスを返す
    fake = "[Diagnostics.Process]::Start('cmd.exe', '/c exit 7')"

    assert start_elevated(fake) == "EXIT=7"


# ---- setup.ps1 と共通のファイアウォール・電源処理 --------------------------------------------


FAKE_OS = r"""
$global:calls = @()
function Get-NetFirewallRule { [CmdletBinding()] param([string]$DisplayName)
    if ($DisplayName -eq 'LinguaBridge TCP 8080') { return [pscustomobject]@{ DisplayName = $DisplayName } } }
function New-NetFirewallRule { [CmdletBinding()] param($DisplayName, $Direction, $Protocol, $LocalPort, $Action, $Profile)
    $global:calls += "new $DisplayName $Direction $Protocol $LocalPort $Action $Profile"; [pscustomobject]@{} }
function powercfg { $global:calls += "powercfg $($args -join ' ')"; $global:LASTEXITCODE = 0 }
"""


def test_firewall_adds_only_missing_rules_like_setup(tmp_path):
    result = powershell(
        FAKE_OS + f". '{OS_SETUP}'; Grant-LinguaBridgeFirewall -Ports @(8080, 8443); "
        "$global:calls | ForEach-Object { Write-Output \"CALL=$_\" }"
    )

    assert result.returncode == 0, result.stderr
    assert "既存: LinguaBridge TCP 8080" in result.stdout
    assert "追加: LinguaBridge TCP 8443" in result.stdout
    calls = [line.removeprefix("CALL=") for line in result.stdout.splitlines() if line.startswith("CALL=")]
    assert calls == ["new LinguaBridge TCP 8443 Inbound TCP 8443 Allow Any"]


def test_sleep_disabled_on_ac_like_setup(tmp_path):
    result = powershell(
        FAKE_OS + f". '{OS_SETUP}'; $ok = Disable-AcSleep; Write-Output \"OK=$ok\"; "
        "$global:calls | ForEach-Object { Write-Output \"CALL=$_\" }"
    )

    assert result.returncode == 0, result.stderr
    calls = [line.removeprefix("CALL=") for line in result.stdout.splitlines() if line.startswith("CALL=")]
    assert calls == ["powercfg /change standby-timeout-ac 0", "powercfg /change hibernate-timeout-ac 0"]
    assert "OK=True" in result.stdout


@pytest.mark.parametrize("code, ok", [(0, True), (1638, True), (3010, True), (1603, False)])
def test_vc_runtime_installer_exit_codes(tmp_path, code, ok):
    # 1638 は同じか新しい版が既に入っている、3010 は再起動待ちで、どちらも導入済みとみなす
    installer = tmp_path / "vc_redist.cmd"
    installer.write_text(f"@exit /b {code}\r\n", encoding="ascii")

    result = powershell(f". '{OS_SETUP}'; $ok = Install-VcRuntime -Installer '{installer}'; Write-Output \"OK=$ok\"")

    assert result.returncode == 0, result.stderr
    assert f"OK={ok}" in result.stdout


def test_setup_ps1_uses_shared_firewall_and_power_steps():
    setup = (ROOT / "setup.ps1").read_text(encoding="utf-8-sig")

    assert "os_setup.ps1" in setup
    assert "Grant-LinguaBridgeFirewall" in setup
    assert "Disable-AcSleep" in setup
    # 重複した実装を残さない
    assert "New-NetFirewallRule" not in setup
    assert "powercfg" not in setup


def test_elevated_script_continues_after_a_failed_step(tmp_path):
    installer = tmp_path / "vc_redist.cmd"
    installer.write_text("@exit /b 0\r\n", encoding="ascii")
    log = tmp_path / "first-run.log"
    failing_firewall = FAKE_OS + (
        "function New-NetFirewallRule { [CmdletBinding()] param($DisplayName, $Direction, $Protocol, "
        "$LocalPort, $Action, $Profile) throw 'denied by policy' }; "
    )

    result = powershell(
        failing_firewall + f"& '{ROOT / 'scripts' / 'first_run_admin.ps1'}' -HttpPort 8080 -HttpsPort 8443 "
        f"-VcRedist '{installer}' -LogPath '{log}'; Write-Output \"EXIT=$LASTEXITCODE\"; "
        "$global:calls | ForEach-Object { Write-Output \"CALL=$_\" }"
    )

    assert result.returncode == 0, result.stderr
    assert "EXIT=1" in result.stdout
    # ファイアウォールが失敗しても、スリープの無効化は行う
    assert "CALL=powercfg /change standby-timeout-ac 0" in result.stdout
    assert "denied by policy" in log.read_text(encoding="utf-8-sig", errors="replace")
