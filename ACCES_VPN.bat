@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Atelier 3D - acces par le VPN WireGuard de la Freebox
echo.
echo   Atelier 3D - acces a distance par le VPN WireGuard de la Freebox
echo   (regle de pare-feu + route ; Windows demande l'autorisation administrateur)
echo   Pour tout retirer : ACCES_VPN.bat -Remove
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\tools\vpn_access.ps1" %*
exit /b %ERRORLEVEL%
