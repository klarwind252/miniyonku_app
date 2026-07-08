@echo off
chcp 65001 > nul
title EXE Builder - Mini4WD Race System

set "INSTALL_DIR=%USERPROFILE%\Documents\miniyonku_app"
cd /d "%INSTALL_DIR%"

echo ============================================
echo   Mini4WD Race System - EXE Builder
echo ============================================
echo.

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

echo [OK] 環境確認完了。EXE Builder を起動します...
echo.
start "" "%INSTALL_DIR%\venv\Scripts\python.exe" "%INSTALL_DIR%\setup\make_exe.py"
taskkill /f /fi "WINDOWTITLE eq EXE Builder - Mini4WD Race System"
