@echo off
chcp 65001 > nul
title 全DB初期化 - Mini4WD Race System

set "INSTALL_DIR=%USERPROFILE%\Documents\miniyonku_app"
cd /d "%INSTALL_DIR%"

echo ============================================
echo   全DB初期化
echo   ★ レーサー含む全データが消えます ★
echo ============================================
echo.
echo   data\miniyonku.db を完全に削除します。
echo   この操作は元に戻せません。
echo.
set /p CONFIRM=実行するには「yes」と入力してください: 

if /i not "%CONFIRM%"=="yes" (
    echo.
    echo キャンセルしました。
    echo.
    pause
    exit /b 0
)

if exist "%INSTALL_DIR%\data\miniyonku.db" (
    del /f "%INSTALL_DIR%\data\miniyonku.db"
    if errorlevel 1 (
        echo.
        echo [ERROR] DBファイルの削除に失敗しました。
        echo   start.bat を停止してから再実行してください。
        echo.
        pause
        exit /b 1
    )
    echo [OK] 削除しました。
) else (
    echo [INFO] DBファイルが見つかりません。スキップします。
)

echo.
echo 完了。start.bat を起動するとDBが再作成されます。
echo.
pause
