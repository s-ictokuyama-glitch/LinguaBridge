# LinguaBridge の起動ロジック本体（start.bat から呼ばれる）。
# 日本語メッセージや条件分岐は、cmd.exeのコードページ依存パースを避けるため
# ここ（PowerShell）に集約する。start.bat は純ASCIIの薄いシムに保つ。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$venvPython = "$root\.venv\Scripts\python.exe"
# セットアップ要否は「.venvの有無」ではなく「完了マーカーの有無」で判定する。
# モデルDL（数GB）の途中で中断されると .venv だけ残るため、.venv基準だと
# 次回起動でセットアップをスキップしてしまい start.bat だけでは復旧できない。
$setupComplete = "$root\.venv\.setup-complete"

if (-not (Test-Path $setupComplete)) {
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

if (-not (Test-Path $setupComplete) -or -not (Test-Path $venvPython)) {
    Write-Host "[エラー] セットアップが完了していません。start.bat をもう一度実行してください。" -ForegroundColor Red
    Write-Host "（それでも直らない場合は setup.ps1 の出力を確認してください）" -ForegroundColor Red
    exit 1
}

& $venvPython -m server.main --open-browser
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Host ""
    Write-Host "[エラー] サーバーが起動できませんでした。上のメッセージを確認してください。" -ForegroundColor Red
    Write-Host "モデルの欠損や破損が疑われる場合は、まず start.bat をもう一度実行してください。" -ForegroundColor Red
    Write-Host "直らないときは .venv フォルダ内の .setup-complete を削除してから start.bat を" -ForegroundColor Red
    Write-Host "実行すると、セットアップ（モデル再取得を含む）をやり直せます。" -ForegroundColor Red
}
Write-Host ""
Write-Host "サーバーが停止しました。"
exit $exitCode
