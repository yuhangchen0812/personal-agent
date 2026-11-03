@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Project .venv is missing. See README.md for setup. 1>&2
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -X utf8 -B "%~dp0scripts\verify.py" %*
exit /b %errorlevel%
