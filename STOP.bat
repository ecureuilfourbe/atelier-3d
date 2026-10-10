@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\run.ps1" stop %*
exit /b %ERRORLEVEL%
