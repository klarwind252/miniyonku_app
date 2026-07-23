"""M4LAPS 橋渡し用リポジトリ（計測結果 → 予選/決勝）

計測レースを「どのヒートに反映するか」を選ぶために、
ヒートとその出走者（レーン割当）を読み出す。

⚠ 参照するのはアプリ本体のテーブル（tournaments / heats / heat_lanes /
   entries / racers）。M4LAPS側のテーブルは触らない。
"""

from __future__ import annotations


async def list_recent_tournaments(db, limit: int = 20) -> list[dict]:
    """最近のレース（大会）を新しい順に返す。反映先を選ぶプルダウン用。"""
    async with db.execute(
        "SELECT id, name, date FROM tournaments ORDER BY date DESC, id DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [{"id": r["id"], "name": r["name"], "date": r["date"]} for r in rows]


async def list_heats_with_lanes(db, tournament_id: int) -> list[dict]:
    """指定レースのヒート一覧を、出走者（レーン順）つきで返す。

    戻り値:
      [{"heat_id":.., "heat_no":.., "group_no":.., "round_no":.., "status":..,
        "lanes":[{"lane_no":1,"entry_id":..,"name":"..."} ...],
        "has_result": bool}]

    has_result は既に結果が入力済みか（手入力・自動反映どちらでも）。
    上書きの確認に使う。
    """
    async with db.execute(
        "SELECT id, heat_no, group_no, round_no, status "
        "FROM heats WHERE tournament_id = ? "
        "ORDER BY round_no, group_no, heat_no, id",
        (tournament_id,),
    ) as cur:
        heats = await cur.fetchall()
    if not heats:
        return []

    heat_ids = [h["id"] for h in heats]
    ph = ",".join("?" * len(heat_ids))

    # レーン割当＋レーサー名（マスタ未使用のエントリーもあるので LEFT JOIN）
    async with db.execute(
        f"SELECT hl.heat_id, hl.lane_no, hl.entry_id, "
        f"       COALESCE(r.name, '(名前未設定)') AS name "
        f"FROM heat_lanes hl "
        f"LEFT JOIN entries e ON e.id = hl.entry_id "
        f"LEFT JOIN racers  r ON r.id = e.racer_id "
        f"WHERE hl.heat_id IN ({ph}) "
        f"ORDER BY hl.heat_id, hl.lane_no",
        heat_ids,
    ) as cur:
        lane_rows = await cur.fetchall()

    # 結果が入っているヒート（heat_lane 経由で判定）
    async with db.execute(
        f"SELECT DISTINCT hl.heat_id AS hid "
        f"FROM heat_results hr JOIN heat_lanes hl ON hl.id = hr.heat_lane_id "
        f"WHERE hl.heat_id IN ({ph})",
        heat_ids,
    ) as cur:
        done = {r["hid"] for r in await cur.fetchall()}

    lanes_by_heat: dict[int, list[dict]] = {}
    for r in lane_rows:
        lanes_by_heat.setdefault(r["heat_id"], []).append({
            "lane_no": r["lane_no"], "entry_id": r["entry_id"], "name": r["name"],
        })

    out = []
    for h in heats:
        out.append({
            "heat_id": h["id"],
            "heat_no": h["heat_no"],
            "group_no": h["group_no"],
            "round_no": h["round_no"],
            "status": h["status"],
            "lanes": lanes_by_heat.get(h["id"], []),
            "has_result": h["id"] in done,
        })
    return out


async def get_heat_summary(db, heat_id: int) -> dict | None:
    """1ヒートの概要（所属レース名・出走者）を返す。確認ダイアログ用。"""
    async with db.execute(
        "SELECT h.id, h.heat_no, h.group_no, h.round_no, "
        "       t.id AS tournament_id, t.name AS tournament_name, t.date "
        "FROM heats h JOIN tournaments t ON t.id = h.tournament_id "
        "WHERE h.id = ?",
        (heat_id,),
    ) as cur:
        h = await cur.fetchone()
    if h is None:
        return None

    async with db.execute(
        "SELECT hl.lane_no, hl.entry_id, COALESCE(r.name, '(名前未設定)') AS name "
        "FROM heat_lanes hl "
        "LEFT JOIN entries e ON e.id = hl.entry_id "
        "LEFT JOIN racers  r ON r.id = e.racer_id "
        "WHERE hl.heat_id = ? ORDER BY hl.lane_no",
        (heat_id,),
    ) as cur:
        lanes = [
            {"lane_no": r["lane_no"], "entry_id": r["entry_id"], "name": r["name"]}
            for r in await cur.fetchall()
        ]

    return {
        "heat_id": h["id"], "heat_no": h["heat_no"],
        "group_no": h["group_no"], "round_no": h["round_no"],
        "tournament_id": h["tournament_id"],
        "tournament_name": h["tournament_name"], "date": h["date"],
        "lanes": lanes,
    }
