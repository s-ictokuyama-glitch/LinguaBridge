# LinguaBridge 初回セットアップ（1回だけ・要管理者権限）
#   1. 右クリック →「PowerShell で実行」、または管理者PowerShellで:
#      powershell -ExecutionPolicy Bypass -File setup.ps1
#   実行内容: venv作成 → 依存インストール → モデルDL → 証明書生成 →
#             ファイアウォール許可 → AC接続時スリープ無効化
#   毎授業の起動は start.bat（ダブルクリック）。

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

function Section($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ネイティブコマンドは $ErrorActionPreference では止まらないので、終了コードを明示確認する
function Assert-ExitOk($what) {
    if ($LASTEXITCODE -ne 0) {
        Write-Error "$what に失敗しました (終了コード $LASTEXITCODE)。上のメッセージを確認してください。"
        exit 1
    }
}

# 管理者権限チェック（ファイアウォール・電源設定に必要）
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warning "管理者権限がありません。ファイアウォール許可とスリープ無効化はスキップされます。"
    Write-Warning "完全な自動化には、管理者としてこのスクリプトを再実行してください。"
}

Section "Python 3.12 以上の確認"
$py = $null
foreach ($cand in @("py -3.12", "python", "py")) {
    try {
        $ver = & cmd /c "$cand --version" 2>&1 | Out-String
        if ($ver -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 12) { $py = $cand; break }
    } catch {}
}
if (-not $py) {
    Write-Error "Python 3.12 以上が見つかりません。https://www.python.org からインストールしてください。"
    exit 1
}
Write-Host "使用: $py ($($ver.Trim()))"

Section "仮想環境(.venv)の作成"
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    & cmd /c "$py -m venv .venv"
    Assert-ExitOk "仮想環境の作成"
    Write-Host ".venv を作成しました。"
} else {
    Write-Host ".venv は既に存在します。"
}
$venvPy = "$root\.venv\Scripts\python.exe"

Section "依存パッケージのインストール"
& $venvPy -m pip install --upgrade pip -q
Assert-ExitOk "pip の更新"
& $venvPy -m pip install -r "$root\requirements.txt"
Assert-ExitOk "依存パッケージのインストール"

Section "モデルのダウンロード（数GB・時間がかかります）"
& $venvPy "$root\scripts\download_models.py"
Assert-ExitOk "モデルのダウンロード"

Section "自己署名証明書の生成"
& $venvPy "$root\scripts\make_cert.py"
Assert-ExitOk "証明書の生成"

# 設定ファイルからポートを取得
$ports = (& $venvPy -c "from server.config import load_config; c=load_config('config.yaml'); print(c.server.http_port, c.server.https_port)")
Assert-ExitOk "設定の読み込み"
$parts = $ports.Trim().Split(" ")
$httpPort = [int]$parts[0]
$httpsPort = [int]$parts[1]

if ($isAdmin) {
    Section "ファイアウォール許可（受信TCP $httpPort, $httpsPort）"
    foreach ($p in @($httpPort, $httpsPort)) {
        $name = "LinguaBridge TCP $p"
        if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP `
                -LocalPort $p -Action Allow -Profile Any | Out-Null
            Write-Host "追加: $name"
        } else {
            Write-Host "既存: $name"
        }
    }

    Section "AC接続時のスリープ無効化"
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    Write-Host "AC接続中はスリープ・休止しません（授業中はAC電源につないでください）。"
}

Section "完了"
Write-Host "セットアップが完了しました。毎授業は start.bat をダブルクリックで起動します。" -ForegroundColor Green
