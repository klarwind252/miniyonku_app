"""クラウド版：DBの自動バックアップ（毎晩・N世代保持）。

data/ 配下の全DB（control.db・店舗1の miniyonku.db・stores/<slug>/miniyonku.db）を
毎晩1回、SQLite のオンラインバックアップAPIで data/_backups/YYYY-MM-DD/ へ複製する。
KEEP_GENERATIONS（=14）を超えた古い日付フォルダは削除する（14世代＝14日分保持）。

設計方針:
  - クラウド版のみ有効（オンプレ版は初回セットアップ/レストア.bat の日付バックアップがあるため対象外）。
  - 追加の cron / systemd ユニットは不要。アプリ起動時に asyncio 常駐タスクとして走る
    （git pull → サービス再起動 だけで有効化できる）。
  - ライブDBでも一貫したスナップショットを取るため、ファイルの単純コピーではなく
    sqlite3 の backup() を使う（コピー途中の書き込みで壊れた版を作らない）。
  - 世代フォルダは一時名で作ってから原子的に差し替える（途中失敗で壊れた世代を残さない）。

保存先 data/_backups/ は data/ 配下なので .gitignore の除外対象に入り、GitHub には上がらない。

復元（手動）:
  サービス停止中に、戻したい日付フォルダの中身を data/ へ上書きコピーする。例:
    sudo systemctl stop miniyonku
    cp -a data/_backups/2026-07-17/. data/
    sudo systemctl start miniyonku
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

# 実行時刻（JST）と保持世代数
_BACKUP_HOUR = 3          # 03:30 JST に実行
_BACKUP_MINUTE = 30
KEEP_GENERATIONS = 14     # 14世代（14日分）保持

try:
    from zoneinfo import ZoneInfo
    _JST = ZoneInfo("Asia/Tokyo")     # tzdata 同梱済み（requirements.txt）
except Exception:
    _JST = timezone(timedelta(hours=9))

_task = None   # 常駐タスクの強参照（GCで消えないよう保持）


def _backups_root() -> str:
    from app import registry
    return os.path.join(registry.DATA_DIR, "_backups")


def _db_targets() -> list[tuple[str, str]]:
    """(コピー元DBパス, data/ からの相対パス) の一覧。存在するものだけ返す。
    control.db と全店舗（無効含む）のDBが対象。"""
    from app import registry
    data_dir = registry.DATA_DIR
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(path: str):
        if not path or not os.path.isfile(path):
            return
        ap = os.path.abspath(path)
        if ap in seen:
            return
        seen.add(ap)
        rel = os.path.relpath(ap, data_dir)
        if rel.startswith(".."):      # data/ 外は念のためファイル名だけに退避
            rel = os.path.basename(ap)
        targets.append((ap, rel))

    add(registry.CONTROL_DB_PATH)
    try:
        for st in registry.list_stores(include_disabled=True):
            add(st.db_path)
    except Exception:
        add(registry.DEFAULT_DB_PATH)   # レジストリ未初期化時でも既定DBは拾う
    return targets


def _sqlite_backup(src_path: str, dst_path: str) -> None:
    """SQLite オンラインバックアップ（ライブDBでも一貫したスナップショット）。"""
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def run_backup_once() -> str:
    """1回分のバックアップを取り、古い世代を掃除する。作成した日付フォルダを返す。
    同期処理（sqlite3・ファイル操作）なので、イベントループ外（executor）で呼ぶこと。
    手動テストにも使える: venv/bin/python -c "from app.services import backup_scheduler as b; b.run_backup_once()"
    """
    stamp = datetime.now(_JST).strftime("%Y-%m-%d")
    dest_dir = os.path.join(_backups_root(), stamp)
    tmp_dir = dest_dir + ".tmp"
    if os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)

    n = 0
    for src_path, rel in _db_targets():
        try:
            _sqlite_backup(src_path, os.path.join(tmp_dir, rel))
            n += 1
        except Exception as e:
            print(f"[BACKUP] 失敗 {rel}: {e}", flush=True)

    # 完成した一時フォルダを本命へ原子的に差し替え（同日再実行は最新で上書き）
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)
    os.replace(tmp_dir, dest_dir)

    _prune_old()
    print(f"[BACKUP] {stamp}: DB {n}件を保存（{KEEP_GENERATIONS}世代保持 / {dest_dir}）", flush=True)
    return dest_dir


def _looks_like_date(name: str) -> bool:
    try:
        datetime.strptime(name, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _prune_old() -> None:
    """日付フォルダ（YYYY-MM-DD）を新しい順に並べ、KEEP_GENERATIONS を超える古い分を削除。"""
    root = _backups_root()
    try:
        names = [d for d in os.listdir(root)
                 if _looks_like_date(d) and os.path.isdir(os.path.join(root, d))]
    except FileNotFoundError:
        return
    names.sort(reverse=True)   # 新しい日付が先頭
    for old in names[KEEP_GENERATIONS:]:
        shutil.rmtree(os.path.join(root, old), ignore_errors=True)


def _seconds_until_next_run() -> float:
    now = datetime.now(_JST)
    nxt = now.replace(hour=_BACKUP_HOUR, minute=_BACKUP_MINUTE, second=0, microsecond=0)
    if nxt <= now:
        nxt = nxt + timedelta(days=1)
    return max(1.0, (nxt - now).total_seconds())


async def backup_loop() -> None:
    """毎晩 03:30(JST) に1回バックアップする常駐ループ。"""
    while True:
        try:
            await asyncio.sleep(_seconds_until_next_run())
            # sqlite3 の同期処理でイベントループを塞がないよう executor で実行
            await asyncio.get_event_loop().run_in_executor(None, run_backup_once)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[BACKUP] ループ内エラー（継続します）: {e}", flush=True)
            await asyncio.sleep(60)   # 暴走防止に最低1分待つ


def launch():
    """クラウド版の起動時に呼ぶ。常駐バックアップタスクを1度だけ開始する。"""
    global _task
    if _task is not None:
        return _task
    _task = asyncio.create_task(backup_loop())
    print(f"[BACKUP] 自動バックアップ有効（毎日 {_BACKUP_HOUR:02d}:{_BACKUP_MINUTE:02d} JST / "
          f"{KEEP_GENERATIONS}世代保持 / 保存先 data/_backups/）", flush=True)
    return _task
