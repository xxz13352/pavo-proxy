@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
python pavo_proxy.py --port 8787 --key sk-pavo
pause
