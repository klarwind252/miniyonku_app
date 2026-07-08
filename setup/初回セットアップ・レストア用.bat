@echo off
title セットアップ・レストア - Mini4WD Race System
cd /d "%~dp0"
set "SRC_DIR=%~dp0"
if "%SRC_DIR:~-1%"=="\" set "SRC_DIR=%SRC_DIR:~0,-1%"
set "INSTALL_DIR=%USERPROFILE%\Documents\miniyonku_app"

REM ---- [0/6] オフライン用 wheelhouse 検出 ----
set "WHEELHOUSE=%SRC_DIR%\wheelhouse"
set "OFFLINE=0"
if exist "%WHEELHOUSE%\*.whl" set "OFFLINE=1"
if "%OFFLINE%"=="1" echo [MODE] オフライン導入（同梱 wheelhouse を使用）
if "%OFFLINE%"=="0" echo [MODE] オンライン導入（インターネットから取得）

echo ============================================
echo   Mini4WD Race System - セットアップ・レストア
echo ============================================
echo.
echo インストール先: %INSTALL_DIR%
echo.

REM ---- [1/6] インターネット接続確認（オフライン時はスキップ） ----
echo [1/6] インターネット接続を確認中...
if "%OFFLINE%"=="1" (
    echo   [SKIP] 同梱 wheelhouse を検出したため、接続確認を省略します。
    goto :net_done
)
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://8.8.8.8' -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop | Out-Null; exit 0 } catch { exit 1 }" > nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "try { $p = New-Object System.Net.NetworkInformation.Ping; $r = $p.Send('8.8.8.8', 3000); if ($r.Status -eq 'Success') { exit 0 } else { exit 1 } } catch { exit 1 }" > nul 2>&1
)
if errorlevel 1 (
    echo.
    echo [ERROR] インターネットに接続できません。
    echo.
    echo   このセットアップにはインターネット接続が必要です。
    echo   接続を確認してから再度実行してください。
    echo.
    pause
    exit /b 1
)
echo   [OK] インターネット接続を確認しました。
:net_done
echo.

REM ---- [2/6] Python 3.12 確認 ----
echo [2/6] Python 3.12 を確認中...
py -3.12 --version > nul 2>&1
if errorlevel 1 (
    echo.
    echo [INFO] Python 3.12 が見つかりません。インストーラーを起動します。
    echo.

    if "%PROCESSOR_ARCHITECTURE%"=="AMD64" goto :install64
    if "%PROCESSOR_ARCHITEW6432%"=="AMD64" goto :install64

    :install32
    if not exist "%SRC_DIR%\python-3.12.10_32bit.exe" (
        echo [ERROR] python-3.12.10_32bit.exe が見つかりません。
        echo   %SRC_DIR% に配置してから再実行してください。
        echo.
        pause
        exit /b 1
    )
    echo   32bit 版インストーラーを起動します...
    echo.
    echo ============================================
    echo   Python のインストールが完了したら
    echo   初回セットアップ・レストア用.bat を
    echo   もう一度実行してください。
    echo ============================================
    echo.
    start "" "%SRC_DIR%\python-3.12.10_32bit.exe"
    exit /b 0

    :install64
    if not exist "%SRC_DIR%\python-3.12.10_64bit.exe" (
        echo [ERROR] python-3.12.10_64bit.exe が見つかりません。
        echo   %SRC_DIR% に配置してから再実行してください。
        echo.
        pause
        exit /b 1
    )
    echo   64bit 版インストーラーを起動します...
    echo.
    echo ============================================
    echo   Python のインストールが完了したら
    echo   初回セットアップ・レストア用.bat を
    echo   もう一度実行してください。
    echo ============================================
    echo.
    start "" "%SRC_DIR%\python-3.12.10_64bit.exe"
    exit /b 0
)
echo   [OK]
py -3.12 --version
echo.

REM ---- [3/6] app・data バックアップ（コピー前に実施） ----
echo [3/6] バックアップ確認中...
for /f "delims=" %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "YYYYMMDD=%%d"

if exist "%INSTALL_DIR%\app" (
    echo   app バックアップ中: %INSTALL_DIR%\app_bkp\app_%YYYYMMDD%
    robocopy "%INSTALL_DIR%\app" "%INSTALL_DIR%\app_bkp\app_%YYYYMMDD%" /E /NFL /NDL /NJH /NJS
    if errorlevel 8 (
        echo.
        echo [ERROR] app のバックアップに失敗しました。
        echo.
        pause
        exit /b 1
    )
    echo   [OK] app バックアップ完了。
) else (
    echo   [INFO] app フォルダなし。スキップします。
)

if exist "%INSTALL_DIR%\data\miniyonku.db" (
    if not exist "%INSTALL_DIR%\data_bkp" mkdir "%INSTALL_DIR%\data_bkp"
    echo   data バックアップ中: %INSTALL_DIR%\data_bkp\miniyonku_%YYYYMMDD%.db
    copy /y "%INSTALL_DIR%\data\miniyonku.db" "%INSTALL_DIR%\data_bkp\miniyonku_%YYYYMMDD%.db" > nul
    if errorlevel 1 (
        echo.
        echo [ERROR] data のバックアップに失敗しました。
        echo.
        pause
        exit /b 1
    )
    echo   [OK] data バックアップ完了。
) else (
    echo   [INFO] DBファイルなし。スキップします。
)
echo.

REM ---- [4/6] フォルダごとコピー（構造維持） ----
echo [4/6] ファイルをインストール中...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

REM SRC_DIR は setup\ フォルダなので、親フォルダ（miniyonku_app）をコピー
set "PARENT_DIR=%SRC_DIR%\.."
robocopy "%PARENT_DIR%" "%INSTALL_DIR%" /E /XD "%PARENT_DIR%\data" "%PARENT_DIR%\data_bkp" "%PARENT_DIR%\app_bkp" "%PARENT_DIR%\venv" /NFL /NDL /NJH /NJS
if errorlevel 8 (
    echo.
    echo [ERROR] ファイルのコピーに失敗しました。
    echo.
    pause
    exit /b 1
)
echo   [OK] ファイルコピー完了。
echo.

REM ---- [5/6] venv 再構築 ----
echo [5/6] venv を準備中...
if exist "%INSTALL_DIR%\venv" (
    echo   既存の venv を削除中...
    rmdir /s /q "%INSTALL_DIR%\venv"
)
echo   venv を作成中...
py -3.12 -m venv "%INSTALL_DIR%\venv"
if errorlevel 1 (
    echo.
    echo [ERROR] venv の作成に失敗しました。
    echo.
    pause
    exit /b 1
)
echo   [OK] venv を作成しました。

echo   パッケージをインストール中...
if "%OFFLINE%"=="1" goto :pip_offline

REM --- オンライン導入 ---
"%INSTALL_DIR%\venv\Scripts\python.exe" -m pip install --upgrade pip -q
if errorlevel 1 goto :pip_err_net
"%INSTALL_DIR%\venv\Scripts\python.exe" -m pip install -r "%INSTALL_DIR%\setup\requirements.txt"
if errorlevel 1 goto :pip_err_net
goto :pip_done

REM --- オフライン導入（同梱 wheelhouse を使用） ---
:pip_offline
echo   [オフライン] 同梱 wheelhouse から導入します。
"%INSTALL_DIR%\venv\Scripts\python.exe" -m pip install --no-index --find-links "%INSTALL_DIR%\setup\wheelhouse" --upgrade pip setuptools wheel
if errorlevel 1 goto :pip_err_offline
"%INSTALL_DIR%\venv\Scripts\python.exe" -m pip install --no-index --find-links "%INSTALL_DIR%\setup\wheelhouse" -r "%INSTALL_DIR%\setup\requirements.txt"
if errorlevel 1 goto :pip_err_offline
goto :pip_done

:pip_err_net
echo.
echo [ERROR] パッケージのインストールに失敗しました。
echo   ネットワーク接続を確認してください。
echo.
pause
exit /b 1

:pip_err_offline
echo.
echo [ERROR] オフライン導入に失敗しました。
echo   setup\wheelhouse 内の .whl がこの PC の Python(3.12 / 64bit) と
echo   一致しているか確認してください。
echo.
pause
exit /b 1

:pip_done
echo   [OK] パッケージインストール完了。
echo.

REM ---- [6/6] 起動用EXE・アイコン設定用.bat の実行判定 ----
echo [6/6] EXE・アイコン設定の確認中...
if exist "%INSTALL_DIR%\Mini4wd.exe" (
    if exist "%INSTALL_DIR%\app\static\logo_header.jpg" (
        echo   [OK] EXE・アイコンは設定済みのためスキップします。
        echo.
        echo ============================================
        echo   セットアップ完了！
        echo   起動は %INSTALL_DIR%\start\start.bat を実行してください。
        echo ============================================
        exit /b 0
    )
)
echo   EXE・アイコン未設定のため 起動用EXE・アイコン設定用.bat を実行します...
start "" "%INSTALL_DIR%\setup\起動用EXE・アイコン設定用.bat"
exit /b 0