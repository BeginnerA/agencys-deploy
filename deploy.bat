@echo off
rem Redball one-click deploy launcher for Windows (cmd / double-click).
rem Equivalent to deploy.sh: all arguments are passed through to deploy.py.
rem
rem Usage:
rem   deploy.bat --project xxx --version 1.0.29 --previous-version 1.0.28 --env test
rem   deploy.bat -h
rem
rem Design note: this launcher only does a Python dependency precheck and
rem passes arguments through; deploy.py is the single source of truth for
rem parsing. Maven/JDK lookup lives only in build.py (config/deploy*.yaml
rem build.maven_home|java_home -> env vars/PATH -> common locations).

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

python "%SCRIPT_DIR%deploy.py" %*
exit /b %errorlevel%
