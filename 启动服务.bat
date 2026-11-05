@echo off
cd /d "%~dp0"

echo ========================================
echo   Xunfang Assistant - Start Server
echo ========================================
echo.

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo Python  : %PY%
echo Address : http://localhost:8000
echo Close this window to stop the server.
echo.

start "" http://localhost:8000
"%PY%" server.py

echo.
echo Server exited. Press any key to close.
pause >nul
