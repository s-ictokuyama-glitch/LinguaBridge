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

# 管理者権限チェック（ファイアウォール・電源設定に必要）
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warning "管理者権限がありません。ファイアウォール許可とスリープ無効化はスキップされます。"
    Write-Warning "完全な自動化には、管理者としてこのスクリプトを再実行してください。"
}

Section "Python 3.12 の確認"
$py = $null
foreach ($cand in @("py -3.12", "python")) {
    try {
        $ver = & cmd /c "$cand --version" 2>&1
        if ($ver -match "3\.1[2-9]") { $py = $cand; break }
    } catch {}
}
if (-not $py) {
    Write-Error "Python 3.12 以上が見つかりません。https://www.python.org からインストールしてください。"
    exit 1
}
Write-Host "使用: $py ($ver)"

Section "仮想環境(.venv)の作成"
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    & cmd /c "$py -m venv .venv"
    Write-Host ".venv を作成しました。"
} else {
    Write-Host ".venv は既に存在します。"
}
$venvPy = "$root\.venv\Scripts\python.exe"

Section "依存パッケージのインストール"
& $venvPy -m pip install --upgrade pip -q
& $venvPy -m pip install -r "$root\requirements.txt"

Section "モデルのダウンロード（数GB・時間がかかります）"
& $venvPy "$root\scripts\download_models.py"

Section "自己署名証明書の生成"
& $venvPy "$root\scripts\make_cert.py"

# 設定ファイルからポートを取得
$ports = (& $venvPy -c "from server.config import load_config; c=load_config('config.yaml'); print(c.server.http_port, c.server.https_port)").Trim().Split(" ")
$httpPort = [int]$ports[0]
$httpsPort = [int]$ports[1]

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
