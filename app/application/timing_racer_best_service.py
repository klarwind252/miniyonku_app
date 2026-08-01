"""レーサー別のベスト集計（application層）。

予選順位表に「そのレーサーが走ったベストタイム・速度」を出すために、
大会に紐づいた計測レース（反映済み＝heat_id を持つ timing_races）を走査し、
レーサー(entry_id)ごとに各指標のベストを1回のパスで集める。

⚠ 反映済みのヒートだけが対象。未反映のレーンは誰の記録か不明なため含めない。
   紐づけは apply 時に確定した lane_no ↔ entry_id を使う
   （M4LAPS の start_lane と heat_lanes.lane_no は一致する）。

指標（best_svc の metric 名に合わせる）:
  total, total_avg, max_ms, lap, lap_avg, sector1..7, sector_ms1..7

処理量: レース数（≒ヒート数、通常十数件）×レーン数。各レースの結果は1回だけ
        組み立てる。ページ表示のたびに走るが、件数が小さいため軽い。
"""

from __future__ import annotations

from app.application import timing_speed_service as spd
from app.application import timing_race_speed_store as speed_store
from app.application import timing_best_service as best_svc
from app.application.timing_race_service import build_race_result
from app.domain.rotation import LayoutElement
from app.infrastructure.db.repositories.timing_repository import (
    TimingRaceRepository,
    TimingLayoutRepository,
)


def _blank() -> dict:
    return {}


async def racer_bests_for_tournament(db, tournament_id: int,
                                     include_finals: bool = False) -> dict[int, dict]:
    """entry_id -> {metric: value} のベスト表を返す。

    値は「そのレーサーがこの大会で出した最良値」。
    タイム系は最小、速度系は最大が採用される。
    記録が無いレーサー・指標は単にキーが無い（テンプレート側で「—」表示）。
    include_finals=True のときは決勝（applied_group_id）の計測も合算する。
    """
    rrepo = TimingRaceRepository(db)
    lrepo = TimingLayoutRepository(db)

    # 予選ヒート（heat_id）の計測レース。include_finals なら決勝グループ（applied_group_id）も。
    if include_finals:
        async with db.execute(
            """SELECT tr.id AS race_id, tr.heat_id, NULL AS group_id
                 FROM timing_races tr
                 JOIN heats h ON h.id = tr.heat_id
                WHERE h.tournament_id = ?
               UNION ALL
               SELECT tr.id AS race_id, NULL AS heat_id, tr.applied_group_id AS group_id
                 FROM timing_races tr
                 JOIN bracket_groups bg ON bg.id = tr.applied_group_id
                 JOIN bracket_rounds br ON br.id = bg.round_id
                WHERE br.tournament_id = ?""",
            (tournament_id, tournament_id),
        ) as cur:
            race_rows = await cur.fetchall()
    else:
        async with db.execute(
            """SELECT tr.id AS race_id, tr.heat_id, NULL AS group_id
                 FROM timing_races tr
                 JOIN heats h ON h.id = tr.heat_id
                WHERE h.tournament_id = ?""",
            (tournament_id,),
        ) as cur:
            race_rows = await cur.fetchall()
    if not race_rows:
        return {}

    heat_ids = sorted({r["heat_id"] for r in race_rows if r["heat_id"] is not None})
    group_ids = sorted({r["group_id"] for r in race_rows if r["group_id"] is not None})

    # ("H", heat_id, lane_no) / ("G", group_id, slot_no) -> entry_id
    # M4LAPS の start_lane は heat_lanes.lane_no / bracket_slots.slot_no と一致する。
    lane_to_entry: dict = {}
    if heat_ids:
        ph = ",".join("?" * len(heat_ids))
        async with db.execute(
            f"SELECT heat_id, lane_no, entry_id FROM heat_lanes WHERE heat_id IN ({ph})",
            heat_ids,
        ) as cur:
            for r in await cur.fetchall():
                lane_to_entry[("H", r["heat_id"], r["lane_no"])] = r["entry_id"]
    if group_ids:
        ph = ",".join("?" * len(group_ids))
        async with db.execute(
            f"SELECT group_id, slot_no, entry_id FROM bracket_slots "
            f"WHERE group_id IN ({ph}) AND entry_id IS NOT NULL",
            group_ids,
        ) as cur:
            for r in await cur.fetchall():
                lane_to_entry[("G", r["group_id"], r["slot_no"])] = r["entry_id"]

    # レイアウトごとの速度設定はキャッシュ（同じコースを何度も読まない）
    cfg_cache: dict = {}
    layout_elems_cache: dict = {}

    bests: dict[int, dict] = {}

    def _put(entry_id: int, metric: str, value):
        if value is None:
            return
        cur_map = bests.setdefault(entry_id, {})
        old = cur_map.get(metric)
        if best_svc.is_better(metric, value, old):
            cur_map[metric] = value

    for rr in race_rows:
        rid = rr["race_id"]
        # 予選ヒート（H）か決勝グループ（G）かでレーン→エントリーの引き方が変わる
        if rr["heat_id"] is not None:
            _scope, _owner = "H", rr["heat_id"]
        else:
            _scope, _owner = "G", rr["group_id"]
        try:
            race, result = await build_race_result(db, rid)
        except Exception:
            continue
        if race is None or result is None:
            continue

        # 速度は反映時に保存済みの値を読む（都度計算しない・定義も統一）
        st = await speed_store.load_speeds(db, rid)

        for m in result.ranking():
            entry_id = lane_to_entry.get((_scope, _owner, m.start_lane))
            if entry_id is None:
                continue
            lane = m.start_lane
            lane_sp = st["lane"].get(lane, {})

            # TOTAL（タイム）は結果から、TOTAL Av./MAX は保存値から
            if m.total_time_us is not None:
                _put(entry_id, "total", m.total_time_us / 1e6)
            _put(entry_id, "total_avg", lane_sp.get("total_avg"))
            _put(entry_id, "max_ms", lane_sp.get("max_ms"))

            for lap in m.laps:
                _put(entry_id, "lap", lap.lap_time_us / 1e6)
                _put(entry_id, "lap_avg", st["lap"].get((lane, lap.lap)))
                for idx, sec in enumerate(lap.sectors):
                    sno = idx + 1
                    if sno > best_svc.MAX_SECTORS:
                        break
                    _put(entry_id, best_svc.sector_metric(sno), sec.dt_us / 1e6)
                    _put(entry_id, best_svc.sector_speed_metric(sno),
                         st["sec"].get((lane, lap.lap, sno)))

    return bests


async def record_holders_for_tournament(db, tournament_id: int,
                                        include_finals: bool = False) -> dict:
    """大会全体の「記録保持者」を返す（予選のみ・反映済みヒート対象）。

    次の3記録について、最良値と、それを出した (entry_id, heat_id) の一覧を集める。
    同率（許容誤差内で同値）の場合は holders に複数入れる（表示側で羅列する）。

      overall     … TOTAL タイムの最小（総合最速タイム）
      fastest_lap … ラップタイムの最小（最速ラップ）
      top_speed   … MAX 速度の最大（最高速）

    戻り値:
      {
        "overall":     {"value": float|None, "holders": [(entry_id, heat_id), ...]},
        "fastest_lap": {"value": float|None, "holders": [...]},
        "top_speed":   {"value": float|None, "holders": [...]},
      }
    """
    records = {
        "overall":     {"value": None, "holders": []},
        "fastest_lap": {"value": None, "holders": []},
        "top_speed":   {"value": None, "holders": []},
        # sno(1..) -> {"value": 区間最速タイム, "holders": [(entry_id, heat_id), ...]}
        # SPRINTER（全区間1位）判定に使う。区間数は可変。
        "sectors":     {},
    }

    # 反映済みの計測レースを対象にする。include_finals なら決勝（applied_group_id）も合算。
    if include_finals:
        async with db.execute(
            """SELECT tr.id AS race_id, tr.heat_id, NULL AS group_id
                 FROM timing_races tr
                 JOIN heats h ON h.id = tr.heat_id
                WHERE h.tournament_id = ?
               UNION ALL
               SELECT tr.id AS race_id, NULL AS heat_id, tr.applied_group_id AS group_id
                 FROM timing_races tr
                 JOIN bracket_groups bg ON bg.id = tr.applied_group_id
                 JOIN bracket_rounds br ON br.id = bg.round_id
                WHERE br.tournament_id = ?""",
            (tournament_id, tournament_id),
        ) as cur:
            race_rows = await cur.fetchall()
    else:
        async with db.execute(
            """SELECT tr.id AS race_id, tr.heat_id, NULL AS group_id
                 FROM timing_races tr
                 JOIN heats h ON h.id = tr.heat_id
                WHERE h.tournament_id = ?""",
            (tournament_id,),
        ) as cur:
            race_rows = await cur.fetchall()
    if not race_rows:
        return records

    # レーン→エントリー（予選H：heat_lanes / 決勝G：bracket_slots）
    lane_to_entry: dict = {}
    heat_ids = sorted({r["heat_id"] for r in race_rows if r["heat_id"] is not None})
    if heat_ids:
        ph = ",".join("?" * len(heat_ids))
        async with db.execute(
            f"SELECT heat_id, lane_no, entry_id FROM heat_lanes WHERE heat_id IN ({ph})",
            heat_ids,
        ) as cur:
            for r in await cur.fetchall():
                lane_to_entry[("H", r["heat_id"], r["lane_no"])] = r["entry_id"]
    group_ids = sorted({r["group_id"] for r in race_rows if r["group_id"] is not None})
    if group_ids:
        ph = ",".join("?" * len(group_ids))
        async with db.execute(
            f"SELECT group_id, slot_no, entry_id FROM bracket_slots "
            f"WHERE group_id IN ({ph}) AND entry_id IS NOT NULL",
            group_ids,
        ) as cur:
            for r in await cur.fetchall():
                lane_to_entry[("G", r["group_id"], r["slot_no"])] = r["entry_id"]

    _TOL = 1e-6

    def _consider(key: str, value, entry_id: int, heat_id: int,
                  *, higher_is_better: bool):
        if value is None:
            return
        rec = records[key]
        rec.setdefault("_vals", set()).add(value)
        cur_v = rec["value"]
        if cur_v is None or (value > cur_v + _TOL if higher_is_better
                             else value < cur_v - _TOL):
            rec["value"] = value
            rec["holders"] = [(entry_id, heat_id)]
        elif abs(value - cur_v) <= _TOL and (entry_id, heat_id) not in rec["holders"]:
            rec["holders"].append((entry_id, heat_id))

    def _consider_sec(sno: int, value, entry_id: int, heat_id: int):
        # 区間タイムは小さいほど上位（最速）
        if value is None:
            return
        rec = records["sectors"].setdefault(sno, {"value": None, "holders": []})
        cur_v = rec["value"]
        if cur_v is None or value < cur_v - _TOL:
            rec["value"] = value
            rec["holders"] = [(entry_id, heat_id)]
        elif abs(value - cur_v) <= _TOL and (entry_id, heat_id) not in rec["holders"]:
            rec["holders"].append((entry_id, heat_id))

    for rr in race_rows:
        # 予選ヒート(H)か決勝グループ(G)かでレーン→エントリーの引き方が変わる。
        # holders に入れる heat_id は予選のみ実値、決勝は None（ラベルは付けない）。
        if rr["heat_id"] is not None:
            _scope, _owner, _hid = "H", rr["heat_id"], rr["heat_id"]
        elif rr["group_id"] is not None:
            _scope, _owner, _hid = "G", rr["group_id"], None
        else:
            continue
        try:
            race, result = await build_race_result(db, rr["race_id"])
        except Exception:
            continue
        if race is None or result is None:
            continue
        st = await speed_store.load_speeds(db, rr["race_id"])
        for m in result.ranking():
            entry_id = lane_to_entry.get((_scope, _owner, m.start_lane))
            if entry_id is None:
                continue
            lane_sp = st["lane"].get(m.start_lane, {})
            if m.total_time_us is not None:
                _consider("overall", m.total_time_us / 1e6,
                          entry_id, _hid, higher_is_better=False)
            _consider("top_speed", lane_sp.get("max_ms"),
                      entry_id, _hid, higher_is_better=True)
            for lap in m.laps:
                _consider("fastest_lap", lap.lap_time_us / 1e6,
                          entry_id, _hid, higher_is_better=False)
                for idx, sec in enumerate(lap.sectors):
                    _consider_sec(idx + 1, sec.dt_us / 1e6, entry_id, _hid)

    # 2位との差（distinct な上位2値の差）。overall/lap は昇順、top_speed は降順。
    for _k, _hi in (("overall", False), ("fastest_lap", False), ("top_speed", True)):
        _vals = sorted(records[_k].get("_vals", set()), reverse=_hi)
        records[_k]["gap"] = (abs(_vals[0] - _vals[1]) if len(_vals) >= 2 else None)
        records[_k].pop("_vals", None)

    return records
