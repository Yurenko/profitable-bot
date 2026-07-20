@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
echo LIVE mode — переконайтесь що testnet: true в config.yaml
python main.py live %*
pause
