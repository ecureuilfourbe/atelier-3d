@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Atelier 3D - diagnostic
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\run.ps1" diagnose %*
echo.
pause
exit /b 0
