#!/bin/bash
# 全DB初期化（すべて消える）— reset_db.bat の Mac 版
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
echo "============================================"
echo "  警告: data/miniyonku.db を削除します"
echo "  レーサーを含む全データが失われます"
echo "============================================"
read -r -p "実行するには yes と入力: " C
[ "$C" = "yes" ] || { echo "中止しました。"; read -r -p "Enter..." _; exit 0; }
if [ -f "$ROOT/data/miniyonku.db" ]; then
  rm -f "$ROOT/data/miniyonku.db" "$ROOT/data/miniyonku.db-wal" "$ROOT/data/miniyonku.db-shm"
  echo "[OK] 削除しました。start.command で再作成されます。"
else
  echo "[INFO] DB ファイルがありません。"
fi
read -r -p "Enter キーで閉じます..." _
