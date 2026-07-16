#!/bin/bash
# レースDB初期化（レーサーマスタは保持）— reset_races.bat の Mac 版
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
if [ ! -x "$ROOT/venv/bin/python" ]; then
  echo "[ERROR] venv が見つかりません。先に setup_mac.command を実行してください。"
  read -r -p "Enter..." _; exit 1
fi
echo "レース・エントリー・トーナメント等を削除します（レーサーマスタは残ります）。"
read -r -p "実行するには yes と入力: " C
[ "$C" = "yes" ] || { echo "中止しました。"; read -r -p "Enter..." _; exit 0; }
TMP="$ROOT/.__reset_races_tmp.py"
cp "$ROOT/setup/reset_races_helper.py" "$TMP"
"$ROOT/venv/bin/python" "$TMP"
rm -f "$TMP"
read -r -p "Enter キーで閉じます..." _
