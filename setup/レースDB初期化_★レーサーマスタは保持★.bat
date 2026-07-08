@echo off
chcp 65001 > nul
title レースDB初期化 - Mini4WD Race System

set "INSTALL_DIR=%USERPROFILE%\Documents\miniyonku_app"
cd /d "%INSTALL_DIR%"

echo ============================================
echo   レースDB初期化 (納品時用)
echo   ※ レーサーマスタは残ります
echo ============================================
echo.

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

if not exist "%INSTALL_DIR%\setup\reset_races_helper.py" (
    echo [ERROR] reset_races_helper.py が見つかりません。
    echo.
    pause
    exit /b 1
)

echo   レースデータをリセットします。レーサーマスタは保持されます。
echo.
set /p CONFIRM=実行するには「yes」と入力してください: 

if /i not "%CONFIRM%"=="yes" (
    echo.
    echo キャンセルしました。
    echo.
    pause
    exit /b 0
)

echo.
"%INSTALL_DIR%\venv\Scripts\python.exe" "%INSTALL_DIR%\setup\reset_races_helper.py"
if errorlevel 1 (
    echo.
    echo [ERROR] リセットに失敗しました。
    echo.
    pause
    exit /b 1
)
echo.
pause
