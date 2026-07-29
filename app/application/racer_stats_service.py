"""観覧用：あるレーサー1名分のタイム詳細（予選＋決勝）を、その場で組み立てる。

公開HTML／観覧画面で名前をタップしたときに、その1名だけを都度計算して返す
（常時全員分を計算・配信しないので負担が小さい）。

admin と同じ build_race_result / speed_store を再利用するため、数値は admin の
計測結果と一致する。反映時の突き合わせ規則により、予選は start_lane==lane_no、
決勝(ブラケット)は start_lane==slot_no なので、その番号で machines から引ける。
"""

from app.application.timing_race_service import build_race_result
from app.application import timing_race_speed_store as speed_store

# 決勝（ブラケット）ラウンド種別 → 表示ラベル
_BR_TYPE_LABEL = {
    "final": "決勝",
    "third": "3位決定戦",
    "revival": "敗者復活戦",
    "losers": "敗者復活",
}


def _lane_stat(machine, speeds, lane_no: int) -> dict:
    """1レースのうち、指定レーン(=lane_no/slot_no)の集計を返す。
    {total_time, total_avg, max_ms, best_lap_time, best_lap_avg, sectors[]}"""
    lane_sp = speeds["lane"].get(lane_no, {})

    # ベストラップ（最小ラップタイム）とそのラップの平均速度
    best_lap_time = None
    best_lap_no = None
    for lap in machine.laps:
        t = lap.lap_time_us / 1e6
        if best_lap_time is None or t < best_lap_time:
            best_lap_time = t
            best_lap_no = lap.lap
    best_lap_avg = (
        speeds["lap"].get((lane_no, best_lap_no)) if best_lap_no is not None else None
    )

    # 区間ごとのベスト（各区間で最小タイム＋最大通過速度）
    sec_time: dict[int, float] = {}
    sec_speed: dict[int, float] = {}
    for lap in machine.laps:
        for idx, sec in enumerate(lap.sectors):
            sno = idx + 1
            t = sec.dt_us / 1e6
            if sno not in sec_time or t < sec_time[sno]:
                sec_time[sno] = t
            sp = speeds["sec"].get((lane_no, lap.lap, sno))
            if sp is not None and (sno not in sec_speed or sp > sec_speed[sno]):
                sec_speed[sno] = sp
    sectors = [
        {"no": s, "time": round(sec_time[s], 3),
         "speed": (round(sec_speed[s], 2) if s in sec_speed else None)}
        for s in sorted(sec_time)
    ]

    total_sec = (round(machine.total_time_us / 1e6, 3)
                 if machine.total_time_us is not None else None)
    return {
        "total_time": total_sec,
        "total_avg": lane_sp.get("total_avg"),
        "max_ms": lane_sp.get("max_ms"),
        "best_lap_time": (round(best_lap_time, 3) if best_lap_time is not None else None),
        "best_lap_avg": best_lap_avg,
        "sectors": sectors,
    }


async def build_racer_qualifying_stats(db, tournament_id: int, entry_id: int) -> dict | None:
    """指定レーサーの予選(races)＋決勝(final_races)のタイム詳細を返す。

    戻り値:
      {"entry_id", "name",
       "races":       [{"label","round_no","heat_no","total_time","gap","total_avg",
                        "max_ms","best_lap_time","best_lap_avg","sectors","total_rank"}...],
       "final_races": [{"label","total_time","total_avg","max_ms",
                        "best_lap_time","best_lap_avg","sectors"}...]}
    該当レーサーが存在しなければ None。計測が反映されていないレースは除外。
    """
    async with db.execute(
        "SELECT r.name FROM entries e JOIN racers r ON r.id=e.racer_id WHERE e.id=?",
        (entry_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    name = row["name"]

    # ── 予選 ──────────────────────────────────────────────
    async with db.execute(
        "SELECT h.id AS heat_id, h.round_no, h.heat_no, hl.lane_no "
        "FROM heat_lanes hl JOIN heats h ON h.id=hl.heat_id "
        "WHERE h.tournament_id=? AND hl.entry_id=? "
        "ORDER BY h.round_no, h.heat_no",
        (tournament_id, entry_id),
    ) as cur:
        heat_rows = await cur.fetchall()

    # 予選全体の上位3 FINISH（色分け基準）と全体ベスト（GAP基準）
    async with db.execute(
        "SELECT hl.heat_id, hl.lane_no, hr.total_time "
        "FROM heat_lanes hl JOIN heats h ON h.id=hl.heat_id "
        "LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id "
        "WHERE h.tournament_id=? AND hr.total_time IS NOT NULL",
        (tournament_id,),
    ) as cur:
        _all = [(r["total_time"], r["heat_id"], r["lane_no"]) for r in await cur.fetchall()]
    _all.sort()
    global_rank = {(hid, lno): i for i, (_t, hid, lno) in enumerate(_all[:3], start=1)}
    overall_best = _all[0][0] if _all else None

    races: list[dict] = []
    for hr in heat_rows:
        heat_id = hr["heat_id"]
        lane_no = hr["lane_no"]
        async with db.execute(
            "SELECT id FROM timing_races WHERE heat_id=? ORDER BY id DESC LIMIT 1",
            (heat_id,),
        ) as cur:
            tr = await cur.fetchone()
        if tr is None:
            continue
        _race, result = await build_race_result(db, tr["id"])
        if result is None:
            continue
        machine = result.machines.get(lane_no)
        if machine is None:
            continue
        speeds = await speed_store.load_speeds(db, tr["id"])
        stat = _lane_stat(machine, speeds, lane_no)
        _tt = stat["total_time"]
        label = (f"予選{hr['round_no']}回目 レース{hr['heat_no']}"
                 if hr["round_no"] else f"レース{hr['heat_no']}")
        races.append({
            "label": label,
            "round_no": hr["round_no"],
            "heat_no": hr["heat_no"],
            "gap": (round(_tt - overall_best, 3)
                    if (_tt is not None and overall_best is not None) else None),
            "total_rank": global_rank.get((heat_id, lane_no)),
            **stat,
        })

    # ── 決勝（ブラケット） ────────────────────────────────
    async with db.execute(
        "SELECT bg.id AS group_id, bg.group_no, bs.slot_no, br.round_type, br.round_no "
        "FROM bracket_slots bs "
        "JOIN bracket_groups bg ON bg.id=bs.group_id "
        "JOIN bracket_rounds br ON br.id=bg.round_id "
        "WHERE br.tournament_id=? AND bs.entry_id=? "
        "ORDER BY br.round_no, bg.group_no",
        (tournament_id, entry_id),
    ) as cur:
        b_rows = await cur.fetchall()

    final_races: list[dict] = []
    for b in b_rows:
        gid = b["group_id"]
        slot_no = b["slot_no"]
        async with db.execute(
            "SELECT id FROM timing_races WHERE applied_group_id=? ORDER BY id DESC LIMIT 1",
            (gid,),
        ) as cur:
            tr = await cur.fetchone()
        if tr is None:
            continue
        _race, result = await build_race_result(db, tr["id"])
        if result is None:
            continue
        machine = result.machines.get(slot_no)
        if machine is None:
            continue
        speeds = await speed_store.load_speeds(db, tr["id"])
        stat = _lane_stat(machine, speeds, slot_no)
        label = _BR_TYPE_LABEL.get(b["round_type"], f"ラウンド{b['round_no']}")
        final_races.append({"label": label, **stat})

    return {"entry_id": entry_id, "name": name,
            "races": races, "final_races": final_races}
