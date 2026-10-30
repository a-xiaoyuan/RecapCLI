@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Run uv sync --extra tui in this folder first.
    pause
    exit /b 1
)
where wt.exe >nul 2>nul
if errorlevel 1 (
    "%~dp0.venv\Scripts\python.exe" -m recapcli %*
) else (
    wt.exe -w new new-tab --title RecapCLI --startingDirectory "%~dp0." "%~dp0.venv\Scripts\python.exe" -m recapcli %*
)
