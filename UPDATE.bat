@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Atelier 3D - mise a jour
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\update.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
