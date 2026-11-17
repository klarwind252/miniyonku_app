"""レース速度の計算と保存（application層）。

「都度計算」をやめ、レース反映（イベント受信）のタイミングで速度を1回だけ
計算して timing_race_speeds に保存する。表示側は保存値を読むだけ。

保存する指標（すべて m/s）:
  lane スコープ … total_avg（= そのレーンの LAP Av. の平均）, max_ms（区間速度の最大）
  lap  スコープ … lap_avg（= (合計距離 ÷ 周回数) ÷ ラップタイム）
  sec  スコープ … sector_ms（各区間の通過速度）, および S/G(0) の通過速度

距離（timing_layouts.lap_length_m）は「合計距離(m)」。1周の距離は 合計距離÷周回数。
距離未設定なら lap_avg / total_avg は出せない → その行は作らない（表示は「—」）。
ビーム間隔未設定なら sector_ms / max_ms が出せない → 同上。

⚠ lap / sector_no の「無し」は -1 で保存する（PRIMARY KEY の NULL 重複回避）。
"""

from __future__ import annotations

import statistics

from app.application import timing_speed_service as spd
from app.application.timing_race_service import build_race_result
from app.domain.rotation import LayoutElement
from app.infrastructure.db.repositories.timing_repository import (
    TimingRaceRepository,
    TimingLayoutRepository,
)

NO = -1  # lap / sector_no の「該当なし」を表す番兵

# 相対フィルタ: 同レースの他の通過計測（中央値）から「早い方向」に
# これ以上ずれた瞬間速度は、反射ノイズ由来の逸脱とみなして採用しない。
# 絶対上限（timing_layouts.max_speed_ms・既定 40km/h）をくぐり抜けた
# 残りノイズを、各レースの実力値を基準に相対で落とすための保険。
# ※20261116 「自車の周平均×K」で落とす案は不採用：コースアウト→復帰などで周平均だけ
#   極端に遅い車の、正常な通過速度まで消してしまうため（実データで確認）。
REL_K = 2.5


async def has_stored_speeds(db, race_id: int) -> bool:
    async with db.execute(
        "SELECT 1 FROM timing_race_speeds WHERE race_id=? LIMIT 1", (race_id,)
    ) as cur:
        return await cur.fetchone() is not None


async def delete_speeds(db, race_id: int) -> None:
    await db.execute("DELETE FROM timing_race_speeds WHERE race_id=?", (race_id,))


async def compute_and_store_speeds(db, race_id: int) -> int:
    """race_id の速度を計算し、timing_race_speeds へ入れ直す。

    既存のこのレースぶんは削除してから入れ直す（再計算＝上書き）。
    戻り値: 書き込んだ行数（0＝出せる速度が無い＝距離もビーム間隔も未設定など）。
    """
    rrepo = TimingRaceRepository(db)
    lrepo = TimingLayoutRepository(db)

    race = await rrepo.get_race(race_id)
    if race is None:
        return 0
    layout_id = race["layout_id"]

    try:
        _r, result = await build_race_result(db, race_id)
    except Exception:
        result = None
    if result is None:
        await delete_speeds(db, race_id)
        return 0

    cfg = await spd.load_speed_config(db, layout_id)
    total_len_m = cfg.get("lap_length_m")     # ← 意味は「合計距離(m)」
    # ★20261118 レース生成時に記録した距離があればそれを優先（レイアウト変更後の再計算でも当時の距離）
    try:
        async with db.execute(
            "SELECT course_len_m FROM timing_races WHERE id=?", (race_id,)
        ) as _c:
            _rr = await _c.fetchone()
        if _rr is not None and _rr[0]:
            total_len_m = _rr[0]
    except Exception:
        pass
    gap_by_node = cfg.get("beam_gap_by_node") or {}
    max_speed_ms = cfg.get("max_speed_ms")    # コース別の実用上限（None→既定 40km/h）

    # 1周の距離 = 合計距離 ÷ 周回数（周回数はレースの target_laps）
    laps_n = race["target_laps"] or 0
    per_lap_len_m = (total_len_m / laps_n) if (total_len_m and laps_n) else None

    # 通過速度の引き当て表（ビーム間隔があるときだけ）
    # ★20261116 引き当て方を変更。
    #   旧：build_pass_index（物理レーン×ゲートごとの「何回目の通過か」から周回を逆算）。
    #       これは全レーンに車がいる前提で、1〜2台走行（フリー/TA）や欠測があると
    #       2周目以降の通過を別の周・別の車として数えてしまい、速度が出なかった。
    #   新：レース結果(build_race_result)が区間の到達点として実際に採用したイベント
    #       (SectorTime.to_event_id) の A/B 打刻をそのまま使う。タイムと同じ帰属になるので
    #       台数・欠測に左右されない。結果がイベントidを持たない旧データのみ旧方式。
    pass_index = {}
    ev_by_id: dict = {}          # timing_events.id -> (t_us, t_us_b, src)
    ev_for: dict = {}            # (start_lane, lap, idx) -> timing_events.id
    if gap_by_node:
        async with db.execute(
            "SELECT id, lane, src, t_us, t_us_b FROM timing_events "
            "WHERE race_id=? ORDER BY t_us",
            (race_id,),
        ) as cur:
            _rows = await cur.fetchall()
        evs = [
            {"lane": e["lane"], "src": e["src"],
             "t_us": e["t_us"], "t_us_b": e["t_us_b"]}
            for e in _rows
        ]
        ev_by_id = {e["id"]: (e["t_us"], e["t_us_b"], e["src"]) for e in _rows}

        for m in result.ranking():
            prev_end = None      # 直前の周を終えたS/G通過（＝次の周の起点 idx 0）
            prev_lap = None
            for lap in m.laps:
                if prev_end is not None and prev_lap == lap.lap - 1:
                    ev_for[(m.start_lane, lap.lap, 0)] = prev_end
                for idx, sec in enumerate(lap.sectors):
                    if sec.to_event_id is not None:
                        ev_for[(m.start_lane, lap.lap, idx + 1)] = sec.to_event_id
                prev_end = lap.sectors[-1].to_event_id if lap.sectors else None
                prev_lap = lap.lap

        elems = await lrepo.get_elements(layout_id)
        layout_elems = [LayoutElement(kind=e["kind"], node_id=e["node_id"]) for e in elems]
        if layout_elems:
            try:
                pass_index = spd.build_pass_index(layout_elems, evs)
            except Exception:
                pass_index = {}

    def _pass_speed(lane, lap, idx):
        if ev_for:
            rec = ev_by_id.get(ev_for.get((lane, lap, idx)))
        else:
            # レース結果がイベントidを持たない旧データだけ旧方式で引く
            rec = pass_index.get((lane, lap, idx))
        if not rec:
            return None
        # 絶対上限（コース別 max_speed_ms・既定 40km/h）をここで適用
        return spd.pass_speed_ms(rec[0], rec[1], gap_by_node.get(rec[2]), max_speed_ms)

    # --- 1st pass: レース全体のビーム通過速度を集める（絶対上限は適用済み）---
    #     相対フィルタの基準（中央値）を作るため、先に全レーン分を集計する。
    beam_samples: dict = {}          # (lane, lap, idx) -> speed[m/s]
    for m in result.ranking():
        lane = m.start_lane
        for lap in m.laps:
            if lap.lap >= 2:
                sg = _pass_speed(lane, lap.lap, 0)
                if sg is not None:
                    beam_samples[(lane, lap.lap, 0)] = sg
            for idx in range(len(lap.sectors)):
                sno = idx + 1
                sp = _pass_speed(lane, lap.lap, sno)
                if sp is not None:
                    beam_samples[(lane, lap.lap, sno)] = sp

    # 相対の足切り値 = 同レース中央値 × REL_K（早い方向の逸脱を排除）
    _vals = list(beam_samples.values())
    rel_cap = (statistics.median(_vals) * REL_K) if _vals else None

    def _accept(v: float) -> bool:
        return rel_cap is None or v <= rel_cap

    rows: list[tuple] = []   # (start_lane, metric, lap, sector_no, value)

    # --- 2nd pass: 相対フィルタを通した値だけで行を作る ---
    for m in result.ranking():
        lane = m.start_lane
        lap_avgs: list[float] = []
        speeds_all: list[float] = []

        for lap in m.laps:
            # LAP Av. = 1周の距離 ÷ ラップタイム（上限も適用）
            la = spd.lap_avg_speed_ms(lap.lap_time_us, per_lap_len_m, max_speed_ms)
            if la is not None:
                lap_avgs.append(la)
                rows.append((lane, "lap_avg", lap.lap, NO, la))

            # S/G通過(0) の速度（2周目以降のみ意味を持つ）
            if lap.lap >= 2:
                sg = beam_samples.get((lane, lap.lap, 0))
                if sg is not None and _accept(sg):
                    speeds_all.append(sg)
                    rows.append((lane, "sector_ms", lap.lap, 0, sg))

            # 各区間(1..7)の通過速度
            for idx in range(len(lap.sectors)):
                sno = idx + 1
                sp = beam_samples.get((lane, lap.lap, sno))
                if sp is not None and _accept(sp):
                    speeds_all.append(sp)
                    rows.append((lane, "sector_ms", lap.lap, sno, sp))

        # TOTAL Av. = LAP Av. の平均（周ぶんの平均）
        if lap_avgs:
            avg = round(sum(lap_avgs) / len(lap_avgs), 2)
            rows.append((lane, "total_avg", NO, NO, avg))

        # TOTAL MAX = 区間通過速度の最大（絶対+相対フィルタ後）
        if speeds_all:
            rows.append((lane, "max_ms", NO, NO, round(max(speeds_all), 2)))

    await delete_speeds(db, race_id)
    for (lane, metric, lap, sno, val) in rows:
        await db.execute(
            "INSERT OR REPLACE INTO timing_race_speeds "
            "(race_id, start_lane, metric, lap, sector_no, value) VALUES (?,?,?,?,?,?)",
            (race_id, lane, metric, lap, sno, val),
        )
    return len(rows)


async def load_speeds(db, race_id: int) -> dict:
    """保存済み速度を引きやすい形で返す。

    戻り値:
      {
        "lane": {start_lane: {"total_avg":v, "max_ms":v}},
        "lap":  {(start_lane, lap): lap_avg},
        "sec":  {(start_lane, lap, sector_no): sector_ms},
      }
    """
    out = {"lane": {}, "lap": {}, "sec": {}}
    async with db.execute(
        "SELECT start_lane, metric, lap, sector_no, value "
        "FROM timing_race_speeds WHERE race_id=?",
        (race_id,),
    ) as cur:
        for r in await cur.fetchall():
            lane, metric, lap, sno, val = (
                r["start_lane"], r["metric"], r["lap"], r["sector_no"], r["value"])
            if metric in ("total_avg", "max_ms"):
                out["lane"].setdefault(lane, {})[metric] = val
            elif metric == "lap_avg":
                out["lap"][(lane, lap)] = val
            elif metric == "sector_ms":
                out["sec"][(lane, lap, sno)] = val
    return out


async def recalc_layout(db, layout_id: int) -> dict:
    """あるレイアウトで走った全レースの速度を計算し直す。

    コース編集の「Av.再計算」から呼ぶ。戻り値: {"races": 対象数, "rows": 書込行数}
    """
    async with db.execute(
        "SELECT id FROM timing_races WHERE layout_id=?", (layout_id,)
    ) as cur:
        ids = [r["id"] for r in await cur.fetchall()]
    total = 0
    for rid in ids:
        total += await compute_and_store_speeds(db, rid)
    await db.commit()
    return {"races": len(ids), "rows": total}
