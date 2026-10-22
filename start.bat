@echo off
rem LinguaBridge launcher (double-click). Kept pure-ASCII on purpose:
rem cmd.exe's batch parser can misparse non-ASCII text depending on the
rem active codepage at parse time, so all logic and Japanese messages
rem live in scripts\run.ps1 (PowerShell handles UTF-8 reliably).
rem The same file serves the repository (scripts\run.ps1) and the
rem distribution package, where the code lives under app\.
cd /d "%~dp0"
set "launcher=%~dp0scripts\run.ps1"
if exist "%~dp0app\scripts\run.ps1" set "launcher=%~dp0app\scripts\run.ps1"
if /I "%~1"=="--diagnose" goto diagnose
powershell -NoProfile -ExecutionPolicy Bypass -File "%launcher%"
goto finished
:diagnose
powershell -NoProfile -ExecutionPolicy Bypass -File "%launcher%" -Diagnose
:finished
set "startExitCode=%ERRORLEVEL%"
pause
exit /b %startExitCode%
