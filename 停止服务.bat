@echo off
cd /d "%~dp0"

echo Stopping service on port 8000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo Killing PID=%%a
    taskkill /PID %%a /F >nul 2>&1
)
echo Done. Port 8000 released. Press any key to close.
pause >nul
