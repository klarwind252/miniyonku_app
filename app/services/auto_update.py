"""クラウド版：Git アップデート（admin 画面を開いたときの確認・実行）。

admin を開くと、フロント側がこのモジュールの check エンドポイントを叩き、
リモート（GitHub）に新しいコミットがあるかを確認する。
- 更新あり → 画面上で「更新しますか？」を確認。
    - する → git pull --ff-only の後、サービスを再起動。再起動検知後に元の画面を再表示。
    - しない → そのまま画面を表示。
- 更新なし → そのまま画面を表示。

再起動：
    1) sudo -n systemctl restart <SERVICE>   （既存の sudoers 例外を利用）
    2) 失敗時はプロセスを終了 → systemd(Restart=always) が再起動して新コードを反映
  サービス名は環境変数 MINIYONKU_SERVICE（既定 "miniyonku"）で変更可。

オンプレ版では呼ばれない（ルーター側の IS_CLOUD ガード）。
"""
from __future__ import annotations

import asyncio
import datetime
import os
import sqlite3
import subprocess
import time
import uuid

# リポジトリ（git 作業ディレクトリ）のルート。
#   .../miniyonku_app/app/services/auto_update.py → 3つ上が .../miniyonku_app
APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_PATH = os.path.join(APP_DIR, "data", "auto_update.log")
SERVICE_NAME = os.environ.get("MINIYONKU_SERVICE", "miniyonku")

# このプロセスの起動ID。再起動を検知するために /admin/update/ping で返す。
BOOT_ID = uuid.uuid4().hex

# 更新確認の結果を短時間キャッシュ（admin ページを何枚も開くたびに
# git fetch が走らないようにする）。
_CHECK_TTL = 60.0
_check_cache = {"at": 0.0, "available": False, "commit": ""}


def _log(msg: str) -> None:
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(f"[auto_update] {line}", flush=True)   # journalctl にも残す
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _git(*args, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=APP_DIR,
        capture_output=True, text=True, timeout=timeout,
    )


def _remote_has_update() -> tuple[bool, str, str]:
    """リモートに新しいコミットがあるか。戻り値 (更新あり, local, remote)。"""
    _git("fetch", "--quiet")
    local = _git("rev-parse", "HEAD").stdout.strip()
    # 追跡ブランチ（@{u}）と比較。設定が無ければ origin/HEAD にフォールバック。
    up = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    upstream = up.stdout.strip() if up.returncode == 0 and up.stdout.strip() else "origin/HEAD"
    remote = _git("rev-parse", upstream).stdout.strip()
    return (bool(local and remote and local != remote), local, remote)


def check_available(force: bool = False) -> dict:
    """更新の有無を返す（軽い確認）。TTL 内は前回結果を再利用。

    戻り値: {"available": bool, "commit": "<remote短縮>", ...}
    """
    now = time.monotonic()
    if not force and (now - _check_cache["at"] < _CHECK_TTL):
        return {"available": _check_cache["available"],
                "commit": _check_cache["commit"], "cached": True}
    try:
        has, _local, remote = _remote_has_update()
    except Exception as e:
        _log(f"更新確認に失敗: {e}")
        return {"available": False, "commit": "", "error": str(e)}
    short = remote[:7] if remote else ""
    _check_cache.update(at=now, available=has, commit=short)
    return {"available": has, "commit": short}


def _iter_store_db_paths():
    """レース進行チェック用：全店舗の DB パスを列挙（失敗時は既定DBのみ）。"""
    try:
        from app import registry
        paths = [s.db_path for s in registry.list_stores(include_disabled=True) if s.db_path]
        if paths:
            return paths
    except Exception:
        pass
    try:
        from app.infrastructure.db.connection import DB_PATH
        return [DB_PATH]
    except Exception:
        return []


def race_in_progress() -> bool:
    """いずれかの店舗で予選中／決勝中のレースがあれば True。"""
    for p in _iter_store_db_paths():
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            try:
                r = con.execute(
                    "SELECT COUNT(*) FROM tournaments WHERE status IN ('予選中','決勝中')"
                ).fetchone()
                if r and r[0]:
                    return True
            finally:
                con.close()
        except Exception:
            continue
    return False


def _restart_service() -> None:
    """サービス再起動。sudo systemctl → 失敗時はプロセス終了（systemd 再起動）。"""
    try:
        r = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", SERVICE_NAME],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            _log(f"systemctl restart {SERVICE_NAME} を実行しました")
            return
        _log(f"systemctl restart 失敗 rc={r.returncode}: {r.stderr.strip()}")
    except Exception as e:
        _log(f"systemctl restart 例外: {e}")
    _log("フォールバック：プロセスを終了し systemd(Restart=always) に再起動を委ねます")
    os._exit(0)


def do_update() -> dict:
    """git pull --ff-only の後、（更新があれば）再起動する（同期）。

    実際に更新した場合は後半で再起動されるため、通常この戻り値は使われない。
    """
    try:
        has, local, remote = _remote_has_update()
    except Exception as e:
        _log(f"リモート確認に失敗: {e}")
        return {"ok": False, "updated": False, "error": str(e)}
    if not has:
        _log("更新はありません（最新です）")
        return {"ok": True, "updated": False, "reason": "up_to_date"}

    _log(f"更新開始: {local[:7]} → {remote[:7]}")
    pull = _git("pull", "--ff-only")
    if pull.returncode != 0:
        _log(f"git pull 失敗: {pull.stderr.strip()}")
        return {"ok": False, "updated": False, "error": pull.stderr.strip()}

    # requirements.txt に変更があれば依存を更新（venv の pip を使用）
    try:
        diff = _git("diff", "--name-only", local, remote).stdout
        if "setup/requirements.txt" in diff:
            venv_pip = os.path.join(APP_DIR, "venv", "bin", "pip")
            if os.path.exists(venv_pip):
                _log("requirements.txt 変更を検出。依存パッケージを更新します")
                subprocess.run(
                    [venv_pip, "install", "-r", os.path.join("setup", "requirements.txt")],
                    cwd=APP_DIR, capture_output=True, text=True, timeout=600,
                )
    except Exception as e:
        _log(f"依存更新をスキップ: {e}")

    _check_cache.update(at=0.0, available=False, commit="")  # キャッシュを無効化
    _log(f"更新完了。再起動します（{remote[:7]}）")
    _restart_service()
    return {"ok": True, "updated": True, "commit": remote[:7]}


async def _run_blocking(fn, *a, **kw):
    return await asyncio.get_event_loop().run_in_executor(None, lambda: fn(*a, **kw))
