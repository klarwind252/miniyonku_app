"""予選まわりの永続化アクセス（infrastructure層）。

【このファイルの位置づけ＝リファクタの“型”の提示】
  現状 app/presentation/routers/qualifying.py は約7,760行・インラインSQL 989箇所を
  抱える。presentation（HTTP/テンプレート）層に生SQLが散在し、
  責務分離・テスト容易性・結合度のいずれも損なっている。

  既存の TimingDeviceRepository 等と同じ作法（aiosqlite.Connection を受け取り
  SQLを薄くラップするだけ・ドメイン判断は持たない）で、ルーター内のSQLを
  この層へ機械的に引き上げるのが移行の基本単位。まず「ヒートロック」という
  凝集した小機能を例に抽出した。

【安全性】
  このファイルは **新規追加のみ**。既存コードはまだ誰も import しないため、
  これを置いただけでは動作は 1 ミリも変わらない。ルーター側の呼び替えは、
  PlatformIO ではなく pytest/手動確認で 1 ハンドラずつ差し替えて初めて効く。

────────────────────────────────────────────────────────────────────
Before（qualifying.py 内・presentation層に生SQLが同居）:

    async def _get_heat_locks(tid: int, db) -> dict:
        async with db.execute(
            "SELECT heat_locks FROM tournaments WHERE id=?", (tid,)) as cur:
            row = await cur.fetchone()
        ...（JSONパース・分岐がハンドラ群に点在）...

After（ハンドラは薄くなり、SQLは本リポジトリへ）:

    from app.infrastructure.db.repositories.qualifying_repository import (
        HeatLockRepository,
    )

    async def _get_heat_locks(tid: int, db) -> dict:
        return await HeatLockRepository(db).get_all(tid)

  → ルーターからSQL文字列が消え、ロック仕様の変更が1クラスに閉じる（結合度↓）。
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json

import aiosqlite


class HeatLockRepository:
    """tournaments.heat_locks（TEXT/JSON: {"<heat_no>": true}）へのアクセス。

    「ヒート（トーナメント）で決勝進出者が全員確定したらロック」という仕様の
    永続化だけを担う。ロックするか否かの業務判断はドメイン/ハンドラ側の責務。
    """

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def get_all(self, tid: int) -> dict:
        """{heat_no(str): bool} を返す。未設定・壊れたJSONは空dict。"""
        async with self.db.execute(
            "SELECT heat_locks FROM tournaments WHERE id=?", (tid,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return {}
        raw = row["heat_locks"] if "heat_locks" in row.keys() else None
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    async def is_locked(self, tid: int, heat_no: int) -> bool:
        locks = await self.get_all(tid)
        return bool(locks.get(str(heat_no)))

    async def set_locked(self, tid: int, heat_no: int, locked: bool) -> None:
        """1ヒートのロックON/OFF。commit は呼び出し側トランザクションに委ねず
        従来の _set_heat_lock と同じく即時反映（挙動を合わせるため commit する）。"""
        locks = await self.get_all(tid)
        if locked:
            locks[str(heat_no)] = True
        else:
            locks.pop(str(heat_no), None)
        await self.db.execute(
            "UPDATE tournaments SET heat_locks=? WHERE id=?",
            (json.dumps(locks), tid),
        )
        await self.db.commit()
