@echo off

REM ============================================================
REM  Restart cleanly: stop the previous instance, then launch.
REM  Give this window a temporary title so that ONLY the old
REM  console (title: Mini4WD Race System) becomes the kill target.
REM ============================================================
title Mini4WD_starting_%RANDOM%

REM Kill the old console (title: Mini4WD Race System) with its child tree.
taskkill /F /T /FI "WINDOWTITLE eq Mini4WD Race System" > nul 2>&1

REM Kill any process still LISTENING on port 8000 (safety for hidden launch).
for /f "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /F /PID %%P > nul 2>&1

REM Wait for the port to be released.
ping -n 3 127.0.0.1 > nul

REM Restore the real title (so the next launch can target it).
title Mini4WD Race System
cd /d "%USERPROFILE%\Documents\miniyonku_app"

echo ============================================
echo   Mini4WD Race System
echo ============================================
echo.

set "INSTALL_DIR=%USERPROFILE%\Documents\miniyonku_app"

py -3.12 --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.12 が見つかりません。
    echo   初回セットアップ・レストア用.bat を先に実行してください。
    echo.
    pause
    exit /b 1
)

if not exist "%INSTALL_DIR%\venv\Scripts\python.exe" (
    echo [ERROR] venv が見つかりません。
    echo   初回セットアップ・レストア用.bat を先に実行してください。
    echo.
    pause
    exit /b 1
)

"%INSTALL_DIR%\venv\Scripts\python.exe" --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] venv が壊れています。
    echo   初回セットアップ・レストア用.bat を再実行してください。
    echo.
    pause
    exit /b 1
)

"%INSTALL_DIR%\venv\Scripts\python.exe" "%INSTALL_DIR%\setup\open_browser_helper.py"
if errorlevel 1 (
    echo [ERROR] open_browser_helper.py の起動に失敗しました。
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   起動URL: http://localhost:8000/admin/
echo   このウィンドウを閉じるか Ctrl+C で停止します。
echo ============================================
echo.

"%INSTALL_DIR%\venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000

echo.
echo [INFO] サーバーが停止しました。
echo.
pause
