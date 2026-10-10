@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Atelier 3D - installation
echo.
echo   Atelier 3D - installation (les etapes deja faites ne sont pas refaites)
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1" %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="3" (
  echo.
  echo Installation annulee : rien n'a ete installe. Relancez INSTALL.bat pour recommencer.
  echo.
  pause
  exit /b 0
)
if not "%RC%"=="0" (
  echo.
  echo [ERREUR] Installation interrompue - code %RC%.
  echo Le journal detaille se trouve dans le dossier "logs" du dossier d'installation.
  echo Relancez INSTALL.bat : les telechargements partiels sont repris.
  echo.
  pause
  exit /b %RC%
)
exit /b 0
