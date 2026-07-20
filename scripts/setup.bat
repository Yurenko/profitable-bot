@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === Встановлення залежностей ===
python -m venv .venv
if errorlevel 1 (
    echo Помилка: встановіть Python 3.10+ з python.org
    exit /b 1
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

if not exist .env (
    copy .env.example .env
    echo Створено .env — додайте API ключі для live/testnet
)

echo.
echo === Готово ===
echo Далі: run_tests.bat  або  run_backtest.bat  або  run_paper.bat
pause
