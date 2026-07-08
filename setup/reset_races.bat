@echo off
chcp 65001 > nul
title Reset Race Data

set "INSTALL_DIR=%USERPROFILE%\Documents\miniyonku_app"
cd /d "%INSTALL_DIR%"

if not exist "%INSTALL_DIR%\venv\Scripts\python.exe" (
    echo [ERROR] venv not found. 初回セットアップ・レストア用.bat を先に実行してください。
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Race data will be reset.
echo   Racer master data will be kept.
echo ============================================
echo.
set /p CONFIRM=Type "yes" to confirm: 

if /i not "%CONFIRM%"=="yes" (
    echo Cancelled.
    pause
    exit /b 0
)

echo.
"%INSTALL_DIR%\venv\Scripts\python.exe" "%INSTALL_DIR%\reset_races_helper.py"
if %errorlevel% neq 0 (
    echo [ERROR] Reset failed.
    pause
    exit /b 1
)
echo.
pause
