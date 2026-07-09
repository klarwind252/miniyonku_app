"""entries / tournaments（本日レース・エントリー操作）へのアクセス。"""
import aiosqlite


class EntryRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def list_today_tournaments(self, today_iso: str, with_regulation: bool = True):
        sql = ("SELECT id, name, regulation FROM tournaments "
               "WHERE date = ? AND status != 'finished' ORDER BY id")
        async with self.db.execute(sql, (today_iso,)) as cur:
            return await cur.fetchall()

    async def list_today_tournament_ids(self, today_iso: str):
        async with self.db.execute(
            "SELECT id FROM tournaments WHERE date = ? AND status != 'finished'",
            (today_iso,),
        ) as cur:
            return [r["id"] for r in await cur.fetchall()]

    async def list_tournament_ids_on(self, date_iso: str):
        """status を問わない当日の全レースID（来店取消用・旧挙動維持）。"""
        async with self.db.execute(
            "SELECT id FROM tournaments WHERE date = ?", (date_iso,)
        ) as cur:
            return [r["id"] for r in await cur.fetchall()]

    async def list_entries_in(self, tournament_ids: list[int]):
        placeholders = ",".join("?" * len(tournament_ids))
        async with self.db.execute(
            f"""SELECT e.racer_id, e.tournament_id,
                       COALESCE(e.entry_at, '') as entry_at
                FROM entries e
                WHERE e.tournament_id IN ({placeholders})""",
            tournament_ids,
        ) as cur:
            return await cur.fetchall()

    async def exists(self, tournament_id: int, racer_id: int) -> bool:
        async with self.db.execute(
            "SELECT id FROM entries WHERE tournament_id = ? AND racer_id = ?",
            (tournament_id, racer_id),
        ) as cur:
            return (await cur.fetchone()) is not None

    async def next_entry_order(self, tournament_id: int) -> int:
        async with self.db.execute(
            "SELECT COALESCE(MAX(entry_order),0)+1 FROM entries WHERE tournament_id=?",
            (tournament_id,),
        ) as cur:
            return (await cur.fetchone())[0]

    async def insert(self, tournament_id: int, racer_id: int, order: int, entry_at: str):
        await self.db.execute(
            "INSERT INTO entries (tournament_id, racer_id, entry_order, entry_at) VALUES (?,?,?,?)",
            (tournament_id, racer_id, order, entry_at),
        )

    async def delete_one(self, tournament_id: int, racer_id: int):
        await self.db.execute(
            "DELETE FROM entries WHERE tournament_id = ? AND racer_id = ?",
            (tournament_id, racer_id),
        )

    async def delete_racer_entries_in(self, racer_id: int, tournament_ids: list[int]):
        placeholders = ",".join("?" * len(tournament_ids))
        await self.db.execute(
            f"DELETE FROM entries WHERE racer_id = ? AND tournament_id IN ({placeholders})",
            [racer_id] + tournament_ids,
        )

    async def list_races_of_racer(self, racer_id: int, start: str, end: str):
        async with self.db.execute(
            """SELECT t.id, t.name, t.date, t.qualifying_type
               FROM tournaments t
               JOIN entries e ON e.tournament_id = t.id
               WHERE e.racer_id = ? AND t.date >= ? AND t.date <= ?
               ORDER BY t.date DESC, t.id DESC""",
            (racer_id, start, end),
        ) as cur:
            return await cur.fetchall()

    async def get_saved_today_day_type(self):
        async with self.db.execute(
            "SELECT value FROM app_settings WHERE key='today_day_type'"
        ) as cur:
            row = await cur.fetchone()
            return row["value"] if row else ""