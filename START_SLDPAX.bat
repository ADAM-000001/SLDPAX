@echo off
setlocal enabledelayedexpansion
title SLDPAX - Funded Master Trading Desk (8 Employees, Tailored R:R)

set "MT5_EXE=C:\Program Files\MetaTrader 5\terminal64.exe"
if not exist "!MT5_EXE!" set "MT5_EXE=C:\Program Files (x86)\MetaTrader 5\terminal64.exe"

echo ====================================================================
echo  SLDPAX - FUNDED MASTER TRADING DESK
echo  8 strategy employees, tailored R:R (1:1.5 - 1:3.5)
echo ====================================================================
echo.

echo [CHECK] MetaTrader 5 status...
tasklist /FI "IMAGENAME eq terminal64.exe" 2>NUL | find /I /N "terminal64.exe" >NUL
if "%ERRORLEVEL%"=="0" (
    echo [OK] MetaTrader 5 already running.
) else (
    if exist "!MT5_EXE!" (
        echo [LAUNCH] Starting MetaTrader 5...
        start "" "!MT5_EXE!"
        timeout /t 5 >nul 2>&1 || ping -n 6 127.0.0.1 >nul
    ) else (
        echo [WARNING] terminal64.exe not found in a standard location.
        echo          Ensure MetaTrader 5 is running and logged in.
    )
)

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo [CHECK] Python interpreter: !PY!
"!PY!" -c "import MetaTrader5, numpy, pandas" 2>NUL
if errorlevel 1 (
    echo [ERROR] Missing dependencies. Run:
    echo         !PY! -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo [CHECK] Dependencies OK.
echo.
echo [WARN] This engine places REAL orders. Open positions are NEVER closed
echo        automatically. The web dashboard opens in your browser; stop the
echo        engine from its minimized console (Ctrl+C) or Task Manager.
echo.

echo [LAUNCH] Starting engine in a minimized background console...
start "SLDPAX Engine - Ctrl+C to stop" /MIN !PY! main.py
echo [WAIT] Giving the engine a few seconds to arm...
timeout /t 5 >nul 2>&1 || ping -n 6 127.0.0.1 >nul

echo [OPEN] Launching the browser dashboard...
start "" "http://127.0.0.1:8080/"

echo.
echo [DONE] Engine launched and dashboard opened. Closing this window...
endlocal
exit /b 0
