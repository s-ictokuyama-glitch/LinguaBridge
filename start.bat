@echo off
rem LinguaBridge launcher (double-click). Kept pure-ASCII on purpose:
rem cmd.exe's batch parser can misparse non-ASCII text depending on the
rem active codepage at parse time, so all logic and Japanese messages
rem live in scripts\run.ps1 (PowerShell handles UTF-8 reliably).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1"
pause
