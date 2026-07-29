"""観覧用：あるレーサー1名分の予選タイム詳細を、その場で組み立てる。

公開HTML／観覧画面の予選順位表で名前をタップしたときに、その1名だけを
都度計算して返す（常時全員分を計算・配信しないので負担が小さい）。

admin と同じ build_race_result / speed_store を再利用するため、数値は
admin の計測結果と一致する。反映時の突き合わせ規則により
start_lane == lane_no なので、レーサーの lane_no をそのまま start_lane
として RaceResult.machines から引ける。
"""

from app.application.timing_race_service import build_race_result
from app.application import timing_race_speed_store as speed_store


async def build_racer_qualifying_stats(db, tournament_id: int, entry_id: int) -> dict | None:
    """指定レーサーの予選各レースのタイム詳細を返す。

    戻り値:
      {
        "entry_id": int, "name": str,
        "races": [
          {"round_no", "heat_no",
           "total_time", "total_avg", "max_ms",
           "best_lap_time", "best_lap_avg",
           "sectors": [{"no", "time", "speed"}, ...]},
          ...
        ]
      }
    該当レーサーが存在しなければ None。計測が反映されていないヒートは races から除外。
    """
    async with db.execute(
        "SELECT r.name FROM entries e JOIN racers r ON r.id=e.racer_id WHERE e.id=?",
        (entry_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    name = row["name"]

    # そのレーサーが走った予選ヒート（この大会配下）とレーン番号
    async with db.execute(
        "SELECT h.id AS heat_id, h.round_no, h.heat_no, hl.lane_no "
        "FROM heat_lanes hl JOIN heats h ON h.id=hl.heat_id "
        "WHERE h.tournament_id=? AND hl.entry_id=? "
        "ORDER BY h.round_no, h.heat_no",
        (tournament_id, entry_id),
    ) as cur:
        heat_rows = await cur.fetchall()

    # 全体（この大会の予選全レース）で速い順の上位3 FINISH タイム。
    # レーススケジュールと同じ「レースを通しての1/2/3ベスト」基準でモーダルを色分けする。
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

    races: list[dict] = []
    for hr in heat_rows:
        heat_id = hr["heat_id"]
        lane_no = hr["lane_no"]

        # このヒートに紐づく計測レース（反映済みなら timing_races.heat_id に記録済み）
        async with db.execute(
            "SELECT id FROM timing_races WHERE heat_id=? ORDER BY id DESC LIMIT 1",
            (heat_id,),
        ) as cur:
            tr = await cur.fetchone()
        if tr is None:
            continue  # 未反映のヒートは飛ばす
        race_id = tr["id"]

        _race, result = await build_race_result(db, race_id)
        if result is None:
            continue
        machine = result.machines.get(lane_no)
        if machine is None:
            continue

        speeds = await speed_store.load_speeds(db, race_id)
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
            speeds["lap"].get((lane_no, best_lap_no))
            if best_lap_no is not None else None
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
            {"no": s,
             "time": round(sec_time[s], 3),
             "speed": (round(sec_speed[s], 2) if s in sec_speed else None)}
            for s in sorted(sec_time)
        ]

        races.append({
            "round_no": hr["round_no"],
            "heat_no": hr["heat_no"],
            "total_time": (round(machine.total_time_us / 1e6, 3)
                           if machine.total_time_us is not None else None),
            "total_avg": lane_sp.get("total_avg"),
            "max_ms": lane_sp.get("max_ms"),
            "best_lap_time": (round(best_lap_time, 3) if best_lap_time is not None else None),
            "best_lap_avg": best_lap_avg,
            "sectors": sectors,
            # 全体（大会の予選全レース）での上位3タイムなら 1/2/3、そうでなければ None
            "total_rank": global_rank.get((heat_id, lane_no)),
        })

    return {"entry_id": entry_id, "name": name, "races": races}
