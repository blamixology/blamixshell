@echo off
REM Builds the Windows installer (MSI). Run build.bat first. Needs the .NET SDK 8+.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\build_msi.ps1 %*
pause
