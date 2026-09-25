@echo off
cd /d "%~dp0"
set PORT=5059
set PYTHONIOENCODING=utf-8
title Kick Clipper Server - keep this process running
"venv\Scripts\python.exe" app.py >> "server-live.log" 2>&1
echo.
echo Kick Clipper stopped. Read server-live.log for the cause.
pause
