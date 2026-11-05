@echo off
cd /d "%~dp0"

echo ========================================
echo   Xunfang Assistant - Install Dependencies
echo ========================================
echo.
echo pip is blocked by the host (403 on User-Agent),
echo whatever index or proxy you use.
echo So we fetch wheels with _pipfetch.py, then install offline.
echo.

set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [0/3] .venv not found, creating...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Failed to create venv. Need Python 3.13 64-bit on PATH.
        pause
        exit /b 1
    )
    set "PY=.venv\Scripts\python.exe"
) else (
    echo [0/3] .venv found, skip creating
)

echo.
echo [1/3] Fetching wheels...
"%PY%" _pipfetch.py fastapi starlette pydantic pydantic-core annotated-types typing-extensions anyio sniffio idna click h11 colorama uvicorn websockets openpyxl et-xmlfile opencv-python-headless numpy python-multipart uiautomation comtypes psutil
if errorlevel 1 (
    echo.
    echo Fetch failed. Network to pypi.tuna.tsinghua.edu.cn is required.
    pause
    exit /b 1
)

echo.
echo [2/3] Installing offline from _wheels\ ...
"%PY%" -m pip install --no-index --find-links _wheels -r requirements.txt
if errorlevel 1 (
    echo.
    echo Install failed. If it says "No matching distribution found for X",
    echo add X to the [1/3] command line in this file and run again.
    pause
    exit /b 1
)

echo.
echo [3/3] Verifying imports...
"%PY%" -c "import fastapi,uvicorn,cv2,numpy,openpyxl,psutil,uiautomation,websockets,multipart;print('OK fastapi',fastapi.__version__,'| numpy',numpy.__version__,'| cv2',cv2.__version__)"

echo.
echo ========================================
echo   Done. Press any key to close.
echo ========================================
pause >nul
