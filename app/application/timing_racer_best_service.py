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
from app.application import timing_best_service as best_svc
from app.application.timing_race_service import build_race_result
from app.domain.rotation import LayoutElement
from app.infrastructure.db.repositories.timing_repository import (
    TimingRaceRepository,
    TimingLayoutRepository,
)


def _blank() -> dict:
    return {}


async def racer_bests_for_tournament(db, tournament_id: int) -> dict[int, dict]:
    """entry_id -> {metric: value} のベスト表を返す。

    値は「そのレーサーがこの大会で出した最良値」。
    タイム系は最小、速度系は最大が採用される。
    記録が無いレーサー・指標は単にキーが無い（テンプレート側で「—」表示）。
    """
    rrepo = TimingRaceRepository(db)
    lrepo = TimingLayoutRepository(db)

    # この大会のヒートに反映された計測レースを集める（heat_id で紐づく）
    async with db.execute(
        """SELECT tr.id AS race_id, tr.heat_id
             FROM timing_races tr
             JOIN heats h ON h.id = tr.heat_id
            WHERE h.tournament_id = ?""",
        (tournament_id,),
    ) as cur:
        race_rows = await cur.fetchall()
    if not race_rows:
        return {}

    heat_ids = sorted({r["heat_id"] for r in race_rows})
    ph = ",".join("?" * len(heat_ids))

    # (heat_id, lane_no) -> entry_id （start_lane == lane_no で引く）
    lane_to_entry: dict = {}
    async with db.execute(
        f"SELECT heat_id, lane_no, entry_id FROM heat_lanes WHERE heat_id IN ({ph})",
        heat_ids,
    ) as cur:
        for r in await cur.fetchall():
            lane_to_entry[(r["heat_id"], r["lane_no"])] = r["entry_id"]

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
        heat_id = rr["heat_id"]
        try:
            race, result = await build_race_result(db, rid)
        except Exception:
            continue
        if race is None or result is None:
            continue

        layout_id = race["layout_id"]
        if layout_id not in cfg_cache:
            cfg_cache[layout_id] = await spd.load_speed_config(db, layout_id)
            elems = await lrepo.get_elements(layout_id)
            layout_elems_cache[layout_id] = [
                LayoutElement(kind=e["kind"], node_id=e["node_id"]) for e in elems
            ]
        cfg = cfg_cache[layout_id]
        gap_by_node = cfg.get("beam_gap_by_node") or {}
        lap_len_m = cfg.get("lap_length_m")

        # 通過速度の引き当て表（設定があるときだけ）
        pass_index = {}
        if gap_by_node and layout_elems_cache[layout_id]:
            async with db.execute(
                "SELECT lane, src, t_us, t_us_b FROM timing_events "
                "WHERE race_id = ? ORDER BY t_us",
                (rid,),
            ) as cur:
                evs = [
                    {"lane": e["lane"], "src": e["src"],
                     "t_us": e["t_us"], "t_us_b": e["t_us_b"]}
                    for e in await cur.fetchall()
                ]
            try:
                pass_index = spd.build_pass_index(layout_elems_cache[layout_id], evs)
            except Exception:
                pass_index = {}

        def _pass_speed(lane, lap, idx):
            rec = pass_index.get((lane, lap, idx))
            if not rec:
                return None
            return spd.pass_speed_ms(rec[0], rec[1], gap_by_node.get(rec[2]))

        for m in result.ranking():
            entry_id = lane_to_entry.get((heat_id, m.start_lane))
            if entry_id is None:
                continue

            # TOTAL / TOTAL平均速度 / MAX
            if m.total_time_us is not None:
                _put(entry_id, "total", m.total_time_us / 1e6)
                _put(entry_id, "total_avg",
                     spd.total_avg_speed_ms(m.total_time_us, lap_len_m, len(m.laps)))

            for lap in m.laps:
                _put(entry_id, "lap", lap.lap_time_us / 1e6)
                _put(entry_id, "lap_avg",
                     spd.lap_avg_speed_ms(lap.lap_time_us, lap_len_m))
                for idx, sec in enumerate(lap.sectors):
                    sno = idx + 1
                    if sno > best_svc.MAX_SECTORS:
                        break
                    _put(entry_id, best_svc.sector_metric(sno), sec.dt_us / 1e6)
                    sp = _pass_speed(m.start_lane, lap.lap, sno)
                    _put(entry_id, best_svc.sector_speed_metric(sno), sp)
                    _put(entry_id, "max_ms", sp)

    return bests
