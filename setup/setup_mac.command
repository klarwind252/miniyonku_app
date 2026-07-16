#!/bin/bash
# ミニ四駆レース管理システム — Mac 初回セットアップ / 再セットアップ
# venv を作り、オンラインで依存パッケージを導入します（インターネット接続が必要）。
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
echo "============================================"
echo "  ミニ四駆レース管理システム — Mac セットアップ"
echo "  場所: $ROOT"
echo "============================================"

# --- Python 3.12 を探す ---
PYBIN=""
if command -v python3.12 >/dev/null 2>&1; then
  PYBIN="python3.12"
elif python3 -c 'import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)' >/dev/null 2>&1; then
  PYBIN="python3"
fi
if [ -z "$PYBIN" ]; then
  echo ""
  echo "[!] Python 3.12 が見つかりませんでした。"
  echo "    ダウンロードページを開きます。universal2 版（Intel / Apple Silicon 兼用）を"
  echo "    インストールしてから、もう一度この setup_mac.command を実行してください。"
  open "https://www.python.org/downloads/macos/" 2>/dev/null || true
  read -r -p "Enter キーで閉じます..." _
  exit 1
fi
echo "[OK] 使用する Python: $($PYBIN --version)"

# --- 既存データのバックアップ（あれば） ---
STAMP="$(date +%Y%m%d)"
if [ -f "$ROOT/data/miniyonku.db" ]; then
  mkdir -p "$ROOT/data_bkp"
  cp -p "$ROOT/data/miniyonku.db" "$ROOT/data_bkp/miniyonku_${STAMP}.db"
  echo "[OK] 既存DBをバックアップ: data_bkp/miniyonku_${STAMP}.db"
fi

# --- venv 再構築 ---
if [ -d "$ROOT/venv" ]; then
  echo "既存の venv を削除中..."
  rm -rf "$ROOT/venv"
fi
echo "venv を作成中..."
"$PYBIN" -m venv "$ROOT/venv" || { echo "[ERROR] venv 作成に失敗しました。"; read -r -p "Enter..." _; exit 1; }

# --- 依存パッケージ（オンライン） ---
echo "依存パッケージを導入中（インターネット接続が必要）..."
"$ROOT/venv/bin/python" -m pip install --upgrade pip
REQ="$ROOT/setup/requirements-mac.txt"
[ -f "$REQ" ] || REQ="$ROOT/setup/requirements.txt"
"$ROOT/venv/bin/python" -m pip install -r "$REQ" || { echo "[ERROR] パッケージ導入に失敗。ネット接続を確認して再実行してください。"; read -r -p "Enter..." _; exit 1; }

echo ""
echo "============================================"
echo "  セットアップ完了！"
echo "  起動は start フォルダの start.command をダブルクリックしてください。"
echo "============================================"
read -r -p "Enter キーで閉じます..." _
