@echo off
rem  SmartOps portable launcher - CLI deploy (args passed through)
rem  Usage: deploy.bat --project xxx --version 1.0.0 --env test
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PATH=%~dp0runtime\python;%~dp0runtime\python\Scripts;%PATH%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%~dp0runtime\python\python.exe" "%~dp0deploy.py" %*
if errorlevel 1 pause
exit /b %errorlevel%
