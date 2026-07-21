@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONPATH=%~dp0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
.\venv\Scripts\python.exe -u src\main.py
pause
