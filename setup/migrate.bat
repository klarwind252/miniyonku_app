@echo off
chcp 65001 > nul
title DB Migration

set "INSTALL_DIR=%USERPROFILE%\Documents\miniyonku_app"
cd /d "%INSTALL_DIR%"

if not exist "%INSTALL_DIR%\venv\Scripts\python.exe" (
    echo [ERROR] venv が見つかりません。初回セットアップ・レストア用.bat を先に実行してください。
    pause
    exit /b 1
)

echo Running DB migration...
echo.
"%INSTALL_DIR%\venv\Scripts\python.exe" "%INSTALL_DIR%\migrate_helper.py"
if %errorlevel% neq 0 (
    echo [ERROR] Migration failed.
    pause
    exit /b 1
)
echo.
pause
