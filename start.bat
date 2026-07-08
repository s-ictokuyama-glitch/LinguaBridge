@echo off
chcp 65001 >nul
rem LinguaBridge 起動（毎授業・ダブルクリック）
rem サーバーを起動し、モデルのロード完了後に先生ページを既定ブラウザで開く。
rem 参加コードと生徒用URLはこのウィンドウに表示されます。授業中は閉じないでください。
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [エラー] .venv が見つかりません。最初に setup.ps1 を実行してください。
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m server.main --open-browser
echo.
echo サーバーが停止しました。
pause
