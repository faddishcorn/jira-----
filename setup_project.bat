@echo off
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [error] Python was not found in PATH.
    echo [hint] Install Python 3.10+ first, then run this script again.
    pause
    exit /b 1
)

if not exist ".\.venv\Scripts\python.exe" (
    echo [info] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [error] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo [info] Installing required packages...
".\.venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo [error] Failed to upgrade pip.
    pause
    exit /b 1
)

".\.venv\Scripts\python.exe" -m pip install -r ".\requirements-jira-report.txt"
if errorlevel 1 (
    echo [error] Failed to install requirements.
    pause
    exit /b 1
)

if not exist ".\.env" (
    echo [info] Creating .env from .env.example...
    copy /Y ".\.env.example" ".\.env" >nul
    echo [info] .env file created. Fill in your real Jira / Notion values before running the report.
) else (
    echo [info] Existing .env found. Keeping current values.
)

echo.
echo [info] Setup completed successfully.
echo [next] 1. Open .env and fill in real values
echo [next] 2. Double-click run_morning_brief.bat
pause
