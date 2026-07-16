#!/bin/bash
# ミニ四駆レース管理システム — Mac 起動（ダブルクリックで実行）
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"

if [ ! -x "$ROOT/venv/bin/python" ]; then
  echo "[ERROR] venv が見つかりません。先に setup フォルダの setup_mac.command を実行してください。"
  read -r -p "Enter キーで閉じます..." _
  exit 1
fi

# 8000番ポートを使っている旧プロセスを終了（Windowsの taskkill/netstat 相当）
PIDS="$(lsof -ti tcp:8000 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
  echo "旧プロセスを終了します (port 8000)..."
  kill $PIDS 2>/dev/null || true
  sleep 1
fi

echo "============================================"
echo "  Mini4WD Race System"
echo "  管理画面: http://localhost:8000/admin/"
echo "  終了する時: このウィンドウで Control+C か、ウィンドウを閉じる"
echo "============================================"

# サーバー起動後にブラウザを自動で開く（バックグラウンド）
(
  for _ in $(seq 1 20); do
    sleep 1
    if curl -s -o /dev/null "http://localhost:8000/health"; then break; fi
  done
  open "http://localhost:8000/admin/"
) &

# ★LANの観覧端末（タブレット等）に見せたい時だけ 127.0.0.1 を 0.0.0.0 に変更（セキュリティ注意）
cd "$ROOT"
exec "$ROOT/venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
