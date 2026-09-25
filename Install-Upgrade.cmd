@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0Install-Upgrade.ps1"
if errorlevel 1 (
  echo Upgrade stopped. Read the message above before trying again.
)
pause
