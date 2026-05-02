@echo off
REM AI Exam Proctor Service - Quick Setup & Run Script for Windows
REM Usage: setup_and_run.bat

setlocal enabledelayedexpansion

echo.
echo ============================================================
echo  AI EXAM PROCTOR SERVICE - Setup ^& Run Script
echo ============================================================
echo.

REM Check Python
echo Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.8+
    exit /b 1
)
for /f "tokens=*" %%i in ('python --version') do set PYTHON_VERSION=%%i
echo [OK] Python found: %PYTHON_VERSION%
echo.

REM Check Node.js (optional)
node --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] Node.js not found (optional for CLI client)
    set HAS_NODE=0
) else (
    for /f "tokens=*" %%i in ('node --version') do set NODE_VERSION=%%i
    echo [OK] Node.js found: %NODE_VERSION%
    set HAS_NODE=1
)
echo.

REM Create virtual environment if not exists
if not exist "venv" (
    echo Creating Python virtual environment...
    python -m venv venv
    echo [OK] Virtual environment created
) else (
    echo [OK] Virtual environment exists
)
echo.

REM Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat
echo [OK] Virtual environment activated
echo.

REM Install Python dependencies
echo Installing Python dependencies...
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo WARNING: Some Python packages may not have installed correctly
) else (
    echo [OK] Python dependencies installed
)
echo.

REM Install Node dependencies if Node.js is available
if %HAS_NODE% equ 1 (
    echo Installing Node.js dependencies...
    npm install --quiet --no-audit --no-fund
    echo [OK] Node.js dependencies installed
    echo.
)

REM Check if model file exists
if not exist "face_landmarker.task" (
    echo WARNING: face_landmarker.task not found
    echo Download from: https://developers.google.com/mediapipe/solutions/vision/face_landmarker
    echo.
)

REM Display options
echo.
echo ============================================================
echo Setup complete! Choose how to run:
echo ============================================================
echo.
echo 1. Start the service:
echo    python proctor_service.py
echo.
echo 2. Then in another terminal, use one of:
echo.
if %HAS_NODE% equ 1 (
    echo    * JavaScript CLI:
    echo      node proctor_client.js start
    echo      node proctor_client.js monitor
    echo.
)
echo    * Web Dashboard:
echo      Open http://localhost:5000 in your browser
echo.
echo    * REST API:
echo      curl -X POST http://localhost:5000/api/start
echo      curl http://localhost:5000/api/status
echo.
echo 3. Documentation:
echo    * Quick Start:     QUICKSTART.md
echo    * API Reference:   SERVICE_API_GUIDE.md
echo    * Implementation:  IMPLEMENTATION_SUMMARY.md
echo    * File Manifest:   FILE_MANIFEST.md
echo.
echo ============================================================
echo.

setlocal
set /p CHOICE="Would you like to start the service now? (y/n): "
if /i "%CHOICE%"=="y" (
    echo.
    echo Starting AI Exam Proctor Service...
    echo.
    python proctor_service.py
) else (
    echo.
    echo To start the service later, run:
    echo    venv\Scripts\activate.bat
    echo    python proctor_service.py
    echo.
)

endlocal
