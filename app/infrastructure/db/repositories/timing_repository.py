"""timing_devices / timing_layouts へのアクセス。

既存リポジトリ（racer_repository 等）と同じく、aiosqlite.Connection を受け取り
SQL を薄くラップする。ドメイン判断（バリデーション等）は持たない。
"""

import aiosqlite


class TimingDeviceRepository:
    """端末台帳（固定12台）へのアクセス。"""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def list_all(self):
        async with self.db.execute(
            "SELECT node_id, kind, label, mac, note FROM timing_devices "
            "ORDER BY node_id"
        ) as cur:
            return await cur.fetchall()

    async def list_by_kind(self, kind: str):
        async with self.db.execute(
            "SELECT node_id, kind, label, mac, note FROM timing_devices "
            "WHERE kind = ? ORDER BY node_id",
            (kind,),
        ) as cur:
            return await cur.fetchall()

    async def get(self, node_id: int):
        async with self.db.execute(
            "SELECT node_id, kind, label, mac, note FROM timing_devices "
            "WHERE node_id = ?",
            (node_id,),
        ) as cur:
            return await cur.fetchone()

    async def update_meta(self, node_id: int, label: str, mac: str, note: str):
        """表示名・MAC・メモの更新（node_id と kind は固定なので触らない）。"""
        await self.db.execute(
            "UPDATE timing_devices SET label = ?, mac = ?, note = ? "
            "WHERE node_id = ?",
            (label, mac, note, node_id),
        )
        await self.db.commit()


class TimingLayoutRepository:
    """コースレイアウト（地図）へのアクセス。"""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def list_layouts(self):
        async with self.db.execute(
            "SELECT id, name, target_laps, created_at, updated_at "
            "FROM timing_layouts ORDER BY updated_at DESC, id DESC"
        ) as cur:
            return await cur.fetchall()

    async def get_layout(self, layout_id: int):
        async with self.db.execute(
            "SELECT id, name, target_laps, created_at, updated_at "
            "FROM timing_layouts WHERE id = ?",
            (layout_id,),
        ) as cur:
            return await cur.fetchone()

    async def get_elements(self, layout_id: int):
        """通過順に並んだ要素列を返す。"""
        async with self.db.execute(
            "SELECT id, position, kind, node_id FROM timing_layout_elements "
            "WHERE layout_id = ? ORDER BY position",
            (layout_id,),
        ) as cur:
            return await cur.fetchall()

    async def create_layout(self, name: str, target_laps: int) -> int:
        cur = await self.db.execute(
            "INSERT INTO timing_layouts (name, target_laps) VALUES (?, ?)",
            (name, target_laps),
        )
        await self.db.commit()
        return cur.lastrowid

    async def save_elements(self, layout_id: int, elements: list[dict]):
        """要素列を丸ごと置き換える（position 0..N-1 で入れ直す）。

        elements: [{"kind": "SG"|"SQ"|"LC", "node_id": int|None}, ...]（通過順）
        """
        await self.db.execute(
            "DELETE FROM timing_layout_elements WHERE layout_id = ?",
            (layout_id,),
        )
        for pos, el in enumerate(elements):
            await self.db.execute(
                "INSERT INTO timing_layout_elements "
                "(layout_id, position, kind, node_id) VALUES (?, ?, ?, ?)",
                (layout_id, pos, el["kind"], el.get("node_id")),
            )
        await self.db.execute(
            "UPDATE timing_layouts SET updated_at = datetime('now','localtime') "
            "WHERE id = ?",
            (layout_id,),
        )
        await self.db.commit()

    async def update_meta(self, layout_id: int, name: str, target_laps: int):
        await self.db.execute(
            "UPDATE timing_layouts SET name = ?, target_laps = ?, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (name, target_laps, layout_id),
        )
        await self.db.commit()

    async def delete_layout(self, layout_id: int):
        await self.db.execute(
            "DELETE FROM timing_layout_elements WHERE layout_id = ?",
            (layout_id,),
        )
        await self.db.execute(
            "DELETE FROM timing_layouts WHERE id = ?",
            (layout_id,),
        )
        await self.db.commit()
