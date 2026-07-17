"""監査ログ：結果の入力・修正・取消などの操作履歴を残す（誰が・いつ・何を）。

方式は軽量：結果系エンドポイント（/qualifying/・/bracket/ へのPOST）を main.py の
ミドルウェア1か所で拾い、日付ごとの JSONL ファイルに1行ずつ追記する。
（クラウドは固定トークン認証で個人の識別が無いため「誰が」は 店舗＋IP を記録する。）

保管は14日分。書き込み時に古いファイルを1日1回だけ掃除する。店舗ごとに記録し、
ダウンロード／表示は要求元の店舗分だけに絞る。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta

KEEP_DAYS = 14
_lock = threading.Lock()
_last_prune_day = None


def _dir() -> str:
    from app import registry
    d = os.path.join(registry.DATA_DIR, "_audit")
    os.makedirs(d, exist_ok=True)
    return d


def _label(path: str) -> str:
    """パスから日本語の操作ラベルを推定する（取消・リセット等を優先判定）。"""
    p = (path or "").lower()
    if "clear-final-result" in p:
        return "決勝結果 取消"
    if "cancel" in p:
        return "取消"
    if "reset" in p:
        return "リセット"
    if "reopen" in p:
        return "再開"
    if "unlock" in p:
        return "ロック解除"
    if "auto-advanced" in p:
        return "自動集計（決勝進出確定）"
    if "set-advanced" in p:
        return "決勝進出 設定"
    if "seeded" in p:
        return "シード設定"
    if "decide-rank" in p:
        return "同率決定"
    if "next-stage" in p:
        return "次段階へ"
    if "make-group" in p:
        return "組確定"
    if "add-run" in p:
        return "追加走行"
    if "/win" in p:
        return "勝敗入力"
    if "generate" in p:
        return "生成"
    if "rank" in p or "/save" in p:
        return "結果入力／修正"
    return "操作"


def record(store_key: str, ip: str, method: str, path: str, status) -> None:
    """監査エントリを1件追記する。失敗してもリクエストは壊さない（例外は握りつぶす）。"""
    try:
        now = datetime.now()
        entry = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "store": store_key or "default",
            "ip": ip or "",
            "action": _label(path),
            "path": path or "",
            "method": method or "",
            "status": status,
        }
        line = json.dumps(entry, ensure_ascii=False)
        d = _dir()
        fpath = os.path.join(d, now.strftime("%Y-%m-%d") + ".jsonl")
        with _lock:
            with open(fpath, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _prune_locked(d, now)
    except Exception:
        pass


def _prune_locked(d: str, now: datetime) -> None:
    """14日より古い日付ファイルを削除（1日1回だけ実行）。"""
    global _last_prune_day
    today = now.strftime("%Y-%m-%d")
    if _last_prune_day == today:
        return
    _last_prune_day = today
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    try:
        for fn in os.listdir(d):
            if fn.endswith(".jsonl") and fn[:-6] < cutoff:
                try:
                    os.remove(os.path.join(d, fn))
                except OSError:
                    pass
    except OSError:
        pass


def read_entries(store_key: str, days: int = KEEP_DAYS) -> list:
    """指定店舗の直近 days 日分のエントリを新しい順で返す。"""
    out = []
    try:
        d = _dir()
        today = datetime.now().date()
        wanted = store_key or "default"
        for i in range(days):
            day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            fpath = os.path.join(d, day + ".jsonl")
            if not os.path.isfile(fpath):
                continue
            with open(fpath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if (e.get("store") or "default") == wanted:
                        out.append(e)
    except Exception:
        pass
    out.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return out
