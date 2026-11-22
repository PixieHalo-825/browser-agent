@echo off
REM Browser-Agent launcher: uses the project venv (.venv) automatically.
REM Double-click to run, or pass any args for main.py.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment .venv not found.
    echo Run these first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    echo     .venv\Scripts\python -m playwright install chromium
    pause
    exit /b 1
)

".venv\Scripts\python.exe" main.py %*
if errorlevel 1 pause
