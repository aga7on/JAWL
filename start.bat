@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".\venv\Scripts\python.exe" (
    .\venv\Scripts\python.exe jawl.py
    goto :done
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3.11 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        py -3.11 jawl.py
        goto :done
    )
)

where python >nul 2>nul
if not errorlevel 1 (
    python jawl.py
    goto :done
)

echo.
echo [ERROR] Python was not found.
echo Install Python 3.11 and run this file again:
echo https://www.python.org/downloads/

:done
pause
