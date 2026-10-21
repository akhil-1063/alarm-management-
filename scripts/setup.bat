@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0.."
title ICU Sentinel - One-time Setup

echo ==========================================================
echo  ICU Sentinel - first-time setup (Windows)
echo ==========================================================
echo.

REM --- 1) Python check -------------------------------------------------------
where py >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python launcher 'py' not found. Install Python 3.11+ from python.org
    echo         and tick "Add python.exe to PATH" during install.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('py -3 --version 2^>^&1') do set PYV=%%v
echo [1/5] Found !PYV!

REM --- 2) Virtual environment ------------------------------------------------
if not exist ".venv" (
    echo [2/5] Creating virtual environment .venv ...
    py -3 -m venv .venv
    if errorlevel 1 ( echo [ERROR] venv creation failed. & pause & exit /b 1 )
) else (
    echo [2/5] .venv already exists - reusing it.
)

set "PY=.venv\Scripts\python.exe"
set "PIP=.venv\Scripts\pip.exe"

REM --- 3) Install dependencies ----------------------------------------------
echo [3/5] Installing dependencies from requirements.txt ...
"%PY%" -m pip install --upgrade pip >nul 2>&1
"%PIP%" install -r requirements.txt
if errorlevel 1 ( echo [ERROR] pip install failed. Check your internet connection. & pause & exit /b 1 )

REM --- 4) .env file ----------------------------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        echo [4/5] Creating .env from .env.example ...
        copy /Y ".env.example" ".env" >nul
    ) else (
        echo [4/5] No .env.example found - skipping.
    )
) else (
    echo [4/5] .env already exists - keeping your settings.
)

REM --- 5) Create the first admin account ------------------------------------
echo [5/5] Creating the first admin account (skipped if one exists) ...
"%PY%" -m backend.create_admin
echo.

echo ==========================================================
echo  Setup complete.
echo    Run the server :  run.bat
echo    Stop the server:  stop.bat
echo    Admin login    :  shown above (or ICU_BOOTSTRAP_* env vars)
echo ==========================================================
pause
