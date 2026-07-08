# LinguaBridge の起動ロジック本体（start.bat から呼ばれる）。
# 日本語メッセージや条件分岐は、cmd.exeのコードページ依存パースを避けるため
# ここ（PowerShell）に集約する。start.bat は純ASCIIの薄いシムに保つ。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$venvPython = "$root\.venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host "  初回起動を検出しました。セットアップを自動で行います。" -ForegroundColor Yellow
    Write-Host "  モデルのダウンロード等で数分から数十分かかることがあります。" -ForegroundColor Yellow
    Write-Host "  このPCがインターネットに接続されている必要があります。" -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  ファイアウォール許可とスリープ無効化まで自動化したい場合は、"
    Write-Host "  一度このウィンドウを閉じ、start.bat を右クリックして「管理者として実行」で"
    Write-Host "  やり直してください（省略しても起動はできます）。"
    Write-Host ""

    & "$root\setup.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "[エラー] セットアップに失敗しました。上のメッセージを確認して、" -ForegroundColor Red
        Write-Host "解決してから start.bat をもう一度実行してください。" -ForegroundColor Red
        exit 1
    }
    Write-Host ""
    Write-Host "セットアップが完了しました。引き続きサーバーを起動します。" -ForegroundColor Green
    Write-Host ""
}

if (-not (Test-Path $venvPython)) {
    Write-Host "[エラー] セットアップ後も .venv が見つかりません。setup.ps1 の出力を確認してください。" -ForegroundColor Red
    exit 1
}

& $venvPython -m server.main --open-browser
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Host ""
    Write-Host "[エラー] サーバーが起動できませんでした。上のメッセージを確認してください。" -ForegroundColor Red
    Write-Host "モデルの欠損や破損が原因の場合は、setup.ps1 を再実行すると復旧できます" -ForegroundColor Red
    Write-Host "（powershell -ExecutionPolicy Bypass -File setup.ps1）。" -ForegroundColor Red
}
Write-Host ""
Write-Host "サーバーが停止しました。"
exit $exitCode
