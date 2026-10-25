@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0"
if errorlevel 1 goto root_error
if not exist ".venv\Scripts\python.exe" goto missing_python
".venv\Scripts\python.exe" -X utf8 -B "scripts\build_package.py" --interactive %*
set "build_exit=%errorlevel%"
goto finish

:missing_python
echo Python environment is missing. Run these commands in this folder:
echo   py -3.12 -m venv .venv
echo   .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
set "build_exit=1"

:finish
popd
pause
exit /b %build_exit%

:root_error
echo Cannot open the project folder.
pause
exit /b 1
