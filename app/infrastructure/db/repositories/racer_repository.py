"""racers テーブルへのアクセス。SQLは旧 routers/racers.py から無変更で移設。"""
import aiosqlite
import uuid


class RacerRepository:
    VALID_SORT = {"name": "name", "yomi": "yomi", "created_at": "created_at",
                  "is_regular": "is_regular"}
    VALID_ORDER = {"asc": "ASC", "desc": "DESC"}

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def list_visible(self, sort_col: str, sort_ord: str, q: str = ""):
        """隠しレーサーを除く一覧（検索・ソート付き）。sort_col/sort_ord は
        VALID_SORT/VALID_ORDER で検証済みの値のみ渡すこと。"""
        if q:
            sql = (f"SELECT * FROM racers WHERE COALESCE(ephemeral,0)=0 "
                   f"AND (name LIKE ? OR yomi LIKE ?) ORDER BY {sort_col} {sort_ord}")
            params = (f"%{q}%", f"%{q}%")
        else:
            sql = f"SELECT * FROM racers WHERE COALESCE(ephemeral,0)=0 ORDER BY {sort_col} {sort_ord}"
            params = ()
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchall()

    async def list_visible_default(self):
        async with self.db.execute(
            "SELECT * FROM racers WHERE COALESCE(ephemeral,0)=0 ORDER BY yomi, name"
        ) as cur:
            return await cur.fetchall()

    async def find_by_name(self, name: str, exclude_id: int | None = None):
        if exclude_id is None:
            sql, params = "SELECT id FROM racers WHERE name = ?", (name,)
        else:
            sql, params = "SELECT id FROM racers WHERE name = ? AND id != ?", (name, exclude_id)
        async with self.db.execute(sql, params) as cur:
            return await cur.fetchone()

    async def find_by_uid(self, uid: str):
        async with self.db.execute("SELECT id FROM racers WHERE uid=?", (uid,)) as cur:
            return await cur.fetchone()

    async def get_visible(self, racer_id: int):
        async with self.db.execute(
            "SELECT id, name, yomi FROM racers WHERE id=? AND COALESCE(ephemeral,0)=0",
            (racer_id,),
        ) as cur:
            return await cur.fetchone()

    async def get_is_child(self, racer_id: int):
        async with self.db.execute(
            "SELECT is_child FROM racers WHERE id = ?", (racer_id,)
        ) as cur:
            return await cur.fetchone()

    async def insert(self, name, yomi, is_child, is_regular):
        await self.db.execute(
            "INSERT INTO racers (name, yomi, is_child, is_regular, uid) VALUES (?, ?, ?, ?, ?)",
            (name, yomi, is_child, is_regular, str(uuid.uuid4())),
        )

    async def insert_imported(self, name, yomi, is_child, uid):
        await self.db.execute(
            "INSERT INTO racers (name, yomi, is_child, uid) VALUES (?, ?, ?, ?)",
            (name, yomi, is_child, uid),
        )

    async def update_identity(self, racer_id, name, yomi, is_child, is_regular=None):
        if is_regular is None:
            await self.db.execute(
                "UPDATE racers SET name=?, yomi=?, is_child=? WHERE id=?",
                (name, yomi, is_child, racer_id),
            )
        else:
            await self.db.execute(
                "UPDATE racers SET name=?, yomi=?, is_child=?, is_regular=? WHERE id=?",
                (name, yomi, is_child, is_regular, racer_id),
            )

    async def delete(self, racer_id: int):
        await self.db.execute("DELETE FROM racers WHERE id = ?", (racer_id,))

    async def set_last_visit(self, racer_id: int, visit_at: str | None):
        await self.db.execute(
            "UPDATE racers SET last_visit_at = ? WHERE id = ?", (visit_at, racer_id)
        )

    async def list_today_visitors(self, today_iso: str):
        async with self.db.execute(
            """SELECT id, name, yomi, is_child, last_visit_at
               FROM racers
               WHERE last_visit_at >= ? AND last_visit_at < ?
                 AND COALESCE(ephemeral,0) = 0
               ORDER BY last_visit_at""",
            (today_iso + " 00:00:00", today_iso + " 23:59:59"),
        ) as cur:
            return await cur.fetchall()

    async def has_column(self, col: str) -> bool:
        async with self.db.execute("PRAGMA table_info(racers)") as cur:
            return col in {r["name"] async for r in cur}

    async def list_for_export(self, exclude_ephemeral: bool):
        if exclude_ephemeral:
            sql = ("SELECT uid, name, yomi, is_child FROM racers "
                   "WHERE COALESCE(ephemeral, 0) = 0 ORDER BY yomi, name")
        else:
            sql = "SELECT uid, name, yomi, is_child FROM racers ORDER BY yomi, name"
        async with self.db.execute(sql) as cur:
            return await cur.fetchall()

    async def list_for_matching(self):
        async with self.db.execute(
            "SELECT id, uid, name, yomi, is_child FROM racers WHERE COALESCE(ephemeral,0)=0"
        ) as cur:
            return await cur.fetchall()