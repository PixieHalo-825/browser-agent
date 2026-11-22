@echo off
REM Browser-Agent Web UI launcher: http://127.0.0.1:8686
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment .venv not found.
    echo Run run.bat setup steps first ^(see README^).
    pause
    exit /b 1
)

".venv\Scripts\python.exe" webui.py %*
if errorlevel 1 pause
