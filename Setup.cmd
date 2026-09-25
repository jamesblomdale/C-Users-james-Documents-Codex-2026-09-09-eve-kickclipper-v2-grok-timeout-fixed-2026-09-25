@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0Setup.ps1"
if errorlevel 1 (
  echo Setup did not finish. Read the error above before starting the app.
  pause
  exit /b 1
)
pause
