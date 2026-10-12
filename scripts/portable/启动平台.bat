@echo off
rem  SmartOps portable launcher - Web console (double click)
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PATH=%~dp0runtime\python;%~dp0runtime\python\Scripts;%PATH%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%~dp0runtime\python\python.exe" "%~dp0launcher.py"
if errorlevel 1 pause
exit /b %errorlevel%
