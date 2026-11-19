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

echo [INFO] Starting FastAPI Middleware Service on 127.0.0.1:8000...
echo [INFO] Interactive Web Console: http://127.0.0.1:8000/ui
echo [INFO] Swagger API Docs: http://127.0.0.1:8000/docs
echo [INFO] ReDoc API Docs: http://127.0.0.1:8000/redoc
echo [INFO] Press Ctrl+C to stop the service.
echo.

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --log-level info

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Server terminated unexpectedly.
    pause
)
