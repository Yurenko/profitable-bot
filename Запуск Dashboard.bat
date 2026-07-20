@echo off
chcp 65001 >nul
cd /d "%~dp0"
title MRDCA Trading Dashboard

if not exist .venv\Scripts\python.exe (
    echo Спочатку запустіть scripts\setup.bat
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
pip install -q fastapi uvicorn python-dotenv 2>nul

echo.
echo ========================================
echo   MRDCA Bot Dashboard
echo   http://127.0.0.1:8080
echo ========================================
echo   Браузер відкриється автоматично.
echo   Закрийте це вікно щоб зупинити сервер.
echo ========================================
echo.

python main.py dashboard
pause
