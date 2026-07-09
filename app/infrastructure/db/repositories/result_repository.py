"""レース結果（確定判定・表彰台・実績）のクエリ。

旧コードでは _is_result_finalized が routers/tournaments.py にあり、
routers/racers.py がルーター越しに import していた（層をまたぐ結合）。
「結果が確定しているか」はドメインの問い合わせなので、ここに集約する。
"""
import aiosqlite
from app.core.config import HEAT_TOURNAMENT_TYPES


class ResultRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def is_result_finalized(self, tid: int) -> bool:
        """1位が決まっているか（結果確定済み）"""
        async with self.db.execute(
            """SELECT 1 FROM bracket_slot_ranks bsr
               JOIN bracket_slots bs ON bs.id=bsr.slot_id
               JOIN bracket_groups bg ON bg.id=bsr.group_id
               JOIN bracket_rounds br ON br.id=bg.round_id
               WHERE br.tournament_id=? AND bsr.rank=1 LIMIT 1""",
            (tid,),
        ) as cur:
            if await cur.fetchone():
                return True
        # ヒートトーナメントの ht 決勝は「予選」であり、本来の結果確定（優勝確定）は
        # 決勝（bracket）の1位で判定する。
        async with self.db.execute(
            "SELECT qualifying_type FROM tournaments WHERE id=?", (tid,)
        ) as cur:
            _qt_row = await cur.fetchone()
        _is_heat_tour = bool(_qt_row and _qt_row["qualifying_type"] in HEAT_TOURNAMENT_TYPES)
        if not _is_heat_tour:
            async with self.db.execute(
                """SELECT 1 FROM ht_slot_ranks hsr
                   JOIN ht_groups hg ON hg.id=hsr.group_id
                   JOIN ht_rounds hr ON hr.id=hg.round_id
                   WHERE hr.tournament_id=? AND hsr.rank=1
                     AND hr.round_type='final' LIMIT 1""",
                (tid,),
            ) as cur:
                if await cur.fetchone():
                    return True
        async with self.db.execute(
            "SELECT 1 FROM tournaments WHERE id=? AND status='complete' AND qualifying_type='none_roundrobin' LIMIT 1",
            (tid,),
        ) as cur:
            if await cur.fetchone():
                return True
        return False

    async def race_podium_racer_ids(self, tid: int, qualifying_type: str) -> dict:
        """確定済みレースの 1〜3 位の racer_id を {rank: racer_id} で返す。"""
        podium = {}
        if qualifying_type == "none_roundrobin":
            async with self.db.execute(
                """SELECT e.none_rr_rank AS rank, e.racer_id
                   FROM entries e
                   WHERE e.tournament_id=? AND e.none_rr_rank IN (1,2,3)
                   ORDER BY e.none_rr_rank""", (tid,)
            ) as cur:
                for row in await cur.fetchall():
                    podium.setdefault(row["rank"], row["racer_id"])
        else:
            async with self.db.execute(
                """SELECT bsr.rank, br.round_type, e.racer_id
                   FROM bracket_slot_ranks bsr
                   JOIN bracket_slots bs ON bs.id=bsr.slot_id
                   JOIN bracket_groups bg ON bg.id=bs.group_id
                   JOIN bracket_rounds br ON br.id=bg.round_id
                   JOIN entries e ON e.id=bs.entry_id
                   WHERE br.tournament_id=? AND bsr.rank IN (1,2,3)
                   ORDER BY CASE br.round_type WHEN 'final' THEN 1 WHEN 'third' THEN 2 ELSE 3 END, bsr.rank""",
                (tid,)
            ) as cur:
                for row in await cur.fetchall():
                    if row["round_type"] == "final":
                        podium.setdefault(row["rank"], row["racer_id"])
                    elif row["round_type"] == "third":
                        if row["rank"] == 1 and 3 not in podium:
                            podium[3] = row["racer_id"]
            if 3 not in podium:
                async with self.db.execute(
                    """SELECT e.racer_id
                       FROM bracket_results bres
                       JOIN bracket_slots bs ON bs.id=bres.winner_slot_id
                       JOIN bracket_groups bg ON bg.id=bres.group_id
                       JOIN bracket_rounds br ON br.id=bg.round_id
                       JOIN entries e ON e.id=bs.entry_id
                       WHERE br.tournament_id=? AND br.round_type='third'
                       LIMIT 1""", (tid,)
                ) as cur:
                    tr = await cur.fetchone()
                    if tr:
                        podium[3] = tr["racer_id"]
        return podium

    async def last_award_dates(self, racer_ids: list[int]) -> dict:
        """前回優勝日・前回入賞日・前回来店日を
        {racer_id: {"win":…, "podium":…, "entry":…}} で返す。"""
        result = {rid: {"win": None, "podium": None, "entry": None} for rid in racer_ids}
        if not racer_ids:
            return result
        target = set(racer_ids)
        placeholders = ",".join("?" for _ in racer_ids)

        async with self.db.execute(
            f"""SELECT e.racer_id AS rid, MAX(t.date) AS d
                FROM entries e
                JOIN tournaments t ON t.id = e.tournament_id
                WHERE e.racer_id IN ({placeholders})
                GROUP BY e.racer_id""",
            tuple(racer_ids),
        ) as cur:
            for row in await cur.fetchall():
                result[row["rid"]]["entry"] = row["d"]

        async with self.db.execute(
            f"""SELECT DISTINCT t.id, t.date, t.qualifying_type
                FROM tournaments t
                JOIN entries e ON e.tournament_id = t.id
                WHERE e.racer_id IN ({placeholders})
                ORDER BY t.date DESC, t.id DESC""",
            tuple(racer_ids),
        ) as cur:
            cand = await cur.fetchall()

        need_win = set(target)
        need_podium = set(target)
        for t in cand:
            if not need_win and not need_podium:
                break
            if not await self.is_result_finalized(t["id"]):
                continue
            podium = await self.race_podium_racer_ids(t["id"], t["qualifying_type"])
            for rk in (1, 2, 3):
                rid = podium.get(rk)
                if rid is None or rid not in target:
                    continue
                if rid in need_podium:
                    result[rid]["podium"] = t["date"]
                    need_podium.discard(rid)
                if rk == 1 and rid in need_win:
                    result[rid]["win"] = t["date"]
                    need_win.discard(rid)
        return result