@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_CMD=python"
if exist ".\.venv\Scripts\python.exe" (
    set "PYTHON_CMD=.\.venv\Scripts\python.exe"
)

if not exist ".\.env" (
    echo [error] .env file was not found.
    echo [hint] Copy .env.example to .env and fill in your Jira / Notion values first.
    pause
    exit /b 1
)

set "REPORT_DATE=%~1"
if "%REPORT_DATE%"=="" (
    for /f %%i in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyy-MM-dd')"') do set "REPORT_DATE=%%i"
)

echo [info] Running Jira morning brief for %REPORT_DATE%
"%PYTHON_CMD%" ".\jira_daily_report.py" --date %REPORT_DATE% --morning-brief --publish-notion --verbose

if errorlevel 1 (
    echo.
    echo [error] Morning brief run failed.
    pause
    exit /b 1
)

echo.
echo [info] Morning brief completed successfully.
pause
