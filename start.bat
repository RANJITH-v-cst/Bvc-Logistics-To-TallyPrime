@echo off
title Tally to BVC Logistics Middleware Service
cls
echo =========================================================================
echo               TALLY TO BVC LOGISTICS MIDDLEWARE SERVICE
echo =========================================================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python is not found in PATH!
    echo Please install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

echo [INFO] Starting FastAPI Middleware Service...
echo [INFO] Endpoint: http://127.0.0.1:8000
echo [INFO] Interactive Swagger Docs: http://127.0.0.1:8000/docs
echo [INFO] Press Ctrl+C to stop the service.
echo.

REM Start FastAPI with Uvicorn
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --log-level info

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Server terminated unexpectedly.
    pause
)
