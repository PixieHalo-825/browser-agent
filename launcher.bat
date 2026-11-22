@echo off
REM Browser-Agent GUI launcher (environment checks + one-click start).
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

".venv\Scripts\python.exe" launcher.py
