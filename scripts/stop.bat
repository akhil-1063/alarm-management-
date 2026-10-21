@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
title ICU Sentinel - Stop server

echo ==========================================================
echo  Stopping the ICU Sentinel server (port 8080 only)
echo ==========================================================

REM Stop only what belongs to THIS project: listeners on port 8080 plus any
REM uvicorn started with --port 8080. Other projects (e.g. port 8000) are
REM never touched.
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue; Write-Host ('  terminated listener PID ' + $_.OwningProcess) }; Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match '8080' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host ('  terminated PID ' + $_.ProcessId) }"

echo.
echo Done. Port 8080 should now be free (other ports untouched).
echo To start again: double-click scripts\run.bat
timeout /t 5
