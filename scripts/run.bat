@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
title ICU Sentinel - Server

echo ==========================================================
echo  ICU Sentinel - starting FastAPI backend + web dashboard
echo ==========================================================

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] No virtual environment found. Run scripts\setup.bat first.
    pause
    exit /b 1
)

if not exist "backend\__init__.py" (
    if not exist "backend\main.py" (
        echo [ERROR] backend\main.py not found. Run this script from the project root.
        pause
        exit /b 1
    )
)

echo Starting server on http://127.0.0.1:8080
echo   Dashboard : http://127.0.0.1:8080/
echo   API docs  : http://127.0.0.1:8080/docs
echo   Health    : http://127.0.0.1:8080/api/v1/health
echo.
echo Press Ctrl+C to stop, or run scripts\stop.bat from another window.
echo ==========================================================

start "" cmd /c "timeout /t 3 >nul & start "" http://127.0.0.1:8080"

".venv\Scripts\python.exe" -m uvicorn backend.main:app --host 0.0.0.0 --port 8080

pause
