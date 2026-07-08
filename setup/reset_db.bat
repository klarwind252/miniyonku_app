@echo off
chcp 65001 > nul
title Reset DB

set "INSTALL_DIR=%USERPROFILE%\Documents\miniyonku_app"
cd /d "%INSTALL_DIR%"

echo.
echo ============================================
echo   WARNING: data\miniyonku.db will be deleted
echo   ALL data including racers will be lost
echo ============================================
echo.
set /p CONFIRM=Type "yes" to confirm: 

if /i not "%CONFIRM%"=="yes" (
    echo Cancelled.
    pause
    exit /b 0
)

if exist "%INSTALL_DIR%\data\miniyonku.db" (
    del /f "%INSTALL_DIR%\data\miniyonku.db"
    echo [OK] Deleted.
) else (
    echo [INFO] DB file not found.
)

echo.
echo Done. Run start.bat to recreate the DB.
pause
