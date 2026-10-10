@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Atelier 3D
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\run.ps1" start %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo [ERREUR] Demarrage impossible - code %RC%. Lancez DIAGNOSE.bat.
  pause
)
exit /b %RC%
