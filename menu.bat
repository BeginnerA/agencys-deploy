@echo off
rem Redball interactive deploy menu launcher for Windows (cmd / double-click).
setlocal
set "SCRIPT_DIR=%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [platform] Python not found. Please install Python 3 and add it to PATH.
    exit /b 1
)

python -c "import yaml, requests, pymysql, paramiko" >nul 2>nul
if errorlevel 1 (
    echo [platform] Missing Python dependencies. Run: pip install -r requirements.txt
    exit /b 1
)

python "%SCRIPT_DIR%menu.py"
exit /b %errorlevel%
