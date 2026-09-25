# LinguaBridge の起動ロジック本体（start.bat から呼ばれる）。
# 日本語メッセージや条件分岐は、cmd.exeのコードページ依存パースを避けるため
# ここ（PowerShell）に集約する。start.bat は純ASCIIの薄いシムに保つ。

param([switch]$Diagnose)

$ErrorActionPreference = "Stop"
# 開発機ではリポジトリのルート、配布パッケージでは本体領域（app）
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
. "$PSScriptRoot\launcher.ps1"
$plan = Get-LaunchPlan -AppRoot $root
$python = $plan.Python
$configArgs = $plan.ConfigArgs
if ($Diagnose) {
    # 診断ではセットアップ・モデル取得・OS設定変更へ進まない。
    if (-not (Test-Path $python)) {
        Write-Host "[未確認] .venv の Python がありません。設定担当者にセットアップ状況を確認してください。"
        exit 2
    }
    & $python -B -m server.diagnostics @configArgs
    exit $LASTEXITCODE
}

if ($plan.Mode -eq "distribution") {
    # 現地データ領域。証明書・授業記録・設定の上書きを置き、app の差し替えをまたいで残す
    New-Item -ItemType Directory -Force -Path $plan.DataRoot | Out-Null
}

if ($plan.NeedsFirstRun) {
    # 初回処理（完了マーカーか証明書が data に無いとき）。拒否・失敗してもサーバーは起動する。
    $ports = (& $python -B -c "import sys; from server.config import load_config; c = load_config(sys.argv[1], override=sys.argv[2]); print(c.server.http_port, c.server.https_port)" `
        (Join-Path $root "config.yaml") (Join-Path $plan.DataRoot "config.yaml"))
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[エラー] 設定を読み込めませんでした。data\config.yaml の内容を確認してください。" -ForegroundColor Red
        exit 1
    }
    $parts = "$ports".Trim().Split(" ")
    $httpPort = [int]$parts[0]
    $httpsPort = [int]$parts[1]
    $adminArgs = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -HttpPort {1} -HttpsPort {2} -VcRedist "{3}" -LogPath "{4}"' -f `
        (Join-Path $PSScriptRoot "first_run_admin.ps1"), $httpPort, $httpsPort,
        (Join-Path $root "vc_redist.x64.exe"), $plan.FirstRunLog
    $elevate = { Start-ElevatedScript -Arguments $adminArgs }
    $makeCert = {
        & $python -B (Join-Path $PSScriptRoot "make_cert.py") @configArgs | Out-Host
        $LASTEXITCODE
    }
    $null = Invoke-FirstRun -Marker $plan.FirstRunMarker -CertFile $plan.CertFile -LogPath $plan.FirstRunLog `
        -HttpPort $httpPort -HttpsPort $httpsPort -MakeCert $makeCert -Elevate $elevate
}

if ($plan.NeedsSetup) {
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host "  セットアップが未完了です。自動で行います（前回が途中まで進んで" -ForegroundColor Yellow
    Write-Host "  いれば続きから再開します）。モデルのダウンロード等で数分から" -ForegroundColor Yellow
    Write-Host "  数十分かかることがあります。このPCがインターネットに接続されて" -ForegroundColor Yellow
    Write-Host "  いる必要があります。" -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  ファイアウォール許可とスリープ無効化まで自動化したい場合は、"
    Write-Host "  一度このウィンドウを閉じ、start.bat を右クリックして「管理者として実行」で"
    Write-Host "  やり直してください（省略しても起動はできます）。"
    Write-Host ""

    & "$root\setup.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "[エラー] セットアップに失敗しました。上のメッセージを確認して原因を" -ForegroundColor Red
        Write-Host "取り除いた後、start.bat をもう一度実行してください（続きから再開します）。" -ForegroundColor Red
        exit 1
    }
    Write-Host ""
    Write-Host "セットアップが完了しました。引き続きサーバーを起動します。" -ForegroundColor Green
    Write-Host ""
}

# setup.ps1 の後に判定し直す（完了マーカーが書かれたかを確かめる）
if ((Get-LaunchPlan -AppRoot $root).NeedsSetup -or -not (Test-Path $python)) {
    Write-Host "[エラー] セットアップが完了していません。start.bat をもう一度実行してください。" -ForegroundColor Red
    Write-Host "（それでも直らない場合は setup.ps1 の出力を確認してください）" -ForegroundColor Red
    exit 1
}

& $python -m server.main --open-browser --select-network @configArgs
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Host ""
    Write-Host "[エラー] サーバーが起動できませんでした。上のメッセージを確認してください。" -ForegroundColor Red
    if ($plan.Mode -eq "distribution") {
        Write-Host "モデルやプログラムの欠損が疑われる場合は、配布パッケージの app フォルダを" -ForegroundColor Red
        Write-Host "展開し直したものと差し替えてください（data フォルダはそのまま残します）。" -ForegroundColor Red
    } else {
        Write-Host "モデルの欠損や破損が疑われる場合は、まず start.bat をもう一度実行してください。" -ForegroundColor Red
        Write-Host "直らないときは .venv フォルダ内の .setup-complete を削除してから start.bat を" -ForegroundColor Red
        Write-Host "実行すると、セットアップ（モデル再取得を含む）をやり直せます。" -ForegroundColor Red
    }
}
Write-Host ""
Write-Host "サーバーが停止しました。"
exit $exitCode
