@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
echo Paper trading: реальні коти з біржі, угоди симулюються.
echo Ctrl+C для зупинки.
python main.py paper %*
pause
