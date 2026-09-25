@echo off
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo Kick Clipper is not set up yet. Run Setup.cmd first.
  pause
  exit /b 1
)
netstat -ano | findstr /R /C:"127.0.0.1:5059 .*LISTENING" >nul
if errorlevel 1 (
  echo Starting Kick Clipper in a minimized background window...
  start "Kick Clipper Server" /min cmd.exe /c ""%~dp0Run-Server.cmd""
) else (
  echo Kick Clipper is already running.
)
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:5059/dashboard"
echo Dashboard opened. The minimized Kick Clipper Server window keeps it running.
timeout /t 2 /nobreak >nul
endlocal
