@echo off
chcp 65001 >nul
echo 正在停止占用 8000 端口的服务...

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo 结束进程 PID=%%a
    taskkill /PID %%a /F >nul 2>&1
)

echo 完成。按任意键关闭窗口
pause >nul
