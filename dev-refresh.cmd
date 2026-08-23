@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev-refresh.ps1" %*
exit /b %errorlevel%
