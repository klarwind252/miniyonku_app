#!/bin/bash
# DBマイグレーション（アプリ更新後にDB構造を最新へ）— migrate.bat の Mac 版
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
if [ ! -x "$ROOT/venv/bin/python" ]; then
  echo "[ERROR] venv が見つかりません。先に setup_mac.command を実行してください。"
  read -r -p "Enter..." _; exit 1
fi
# migrate_helper.py は自身の位置基準で data/ を探すため、ルートに一時コピーして実行
TMP="$ROOT/.__migrate_tmp.py"
cp "$ROOT/setup/migrate_helper.py" "$TMP"
"$ROOT/venv/bin/python" "$TMP"
rm -f "$TMP"
read -r -p "Enter キーで閉じます..." _
