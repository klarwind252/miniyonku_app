@echo off
title wheelhouse 作成 - Mini4WD Race System（インターネット接続 PC で実行）
cd /d "%~dp0"

REM インターネットに接続できる PC で 1 回だけ実行します。
REM この PC の Python(3.12) 向けに、必要な .whl を setup\wheelhouse へ
REM まとめてダウンロードします。作成後は setup フォルダごと（wheelhouse 含む）
REM オフライン PC へコピーし、初回セットアップ・レストア用.bat を実行してください。
REM （wheelhouse があると自動的にオフライン導入になります）

py -3.12 --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.12 が見つかりません。先に導入してください。
    pause
    exit /b 1
)

echo wheelhouse を作成します（この PC の Python 3.12 向け）...
py -3.12 -m pip download -r requirements.txt colorama pip setuptools wheel --dest wheelhouse
if errorlevel 1 (
    echo.
    echo [ERROR] ダウンロードに失敗しました。ネットワークを確認してください。
    echo   ※32bit 環境では google-cloud-storage の 32bit wheel が無いため
    echo     失敗する場合があります。GCS 配信を使わないなら requirements.txt から
    echo     google-cloud-storage 行を外して再実行してください（オンプレ既定では未使用）。
    echo.
    pause
    exit /b 1
)
echo.
echo 完了：setup\wheelhouse に .whl を保存しました。
echo setup フォルダごとオフライン PC へコピーしてください。
pause
exit /b 0
