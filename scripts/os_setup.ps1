# 管理者権限が必要な OS 設定（setup.ps1 と、配布モードの初回処理 first_run_admin.ps1 から
# dot-source する）。tests/integration/test_first_run.py が偽の実行関数に差し替えて検証する。

# HTTP/HTTPS ポートの受信許可。同名の規則が既にあれば追加しない。
function Grant-LinguaBridgeFirewall([Parameter(Mandatory)][int[]]$Ports) {
    foreach ($p in $Ports) {
        $name = "LinguaBridge TCP $p"
        if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP `
                -LocalPort $p -Action Allow -Profile Any | Out-Null
            Write-Host "追加: $name"
        } else {
            Write-Host "既存: $name"
        }
    }
}

# AC 接続時のスリープ・休止を無効にする。両方とも成功したら $true。
function Disable-AcSleep {
    $ok = $true
    foreach ($setting in @("standby-timeout-ac", "hibernate-timeout-ac")) {
        powercfg /change $setting 0 | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "powercfg /change $setting 0 に失敗しました (終了コード $LASTEXITCODE)。"
            $ok = $false
        }
    }
    if ($ok) { Write-Host "AC接続中はスリープ・休止しません（授業中はAC電源につないでください）。" }
    return $ok
}

# 同梱の vc_redist.x64.exe を無人で入れる。既に入っていれば実質何もしない。導入済みになれば $true。
function Install-VcRuntime([Parameter(Mandatory)][string]$Installer) {
    if (-not (Test-Path -LiteralPath $Installer)) {
        Write-Warning "VC++ ランタイムのインストーラーがありません: $Installer"
        return $false
    }
    $process = Start-Process -FilePath $Installer -ArgumentList "/install", "/quiet", "/norestart" `
        -Wait -PassThru -WindowStyle Hidden
    # 1638: 同じか新しい版が既にある / 3010: 導入済み・再起動待ち
    if ($process.ExitCode -in @(0, 1638, 3010)) {
        Write-Host "VC++ ランタイム: 導入済み (終了コード $($process.ExitCode))"
        return $true
    }
    Write-Warning "VC++ ランタイムのインストールに失敗しました (終了コード $($process.ExitCode))。"
    return $false
}
