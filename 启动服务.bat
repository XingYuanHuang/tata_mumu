@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   巡访助手 - 启动服务
echo ========================================
echo.

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo 使用解释器: %PY%
echo 服务地址:   http://localhost:8000
echo 关闭此窗口即停止服务
echo.

start "" http://localhost:8000
"%PY%" server.py

echo.
echo 服务已退出，按任意键关闭窗口
pause >nul
