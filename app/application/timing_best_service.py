"""M4LAPS ベスト記録の算出・保持

方針:
  - 画面を開くたびに全レースを再計算するのをやめ、**受信時に更新して保持**する。
  - 保持するスコープは2つ。
      day  : その日のベスト（'YYYY-MM-DD'）
      race : そのレース内のベスト（レースIDの文字列）
  - レース削除時はその日のスコープを**再計算**する（消した記録がベストのまま
    残らないようにするため）。削除は例外的な操作なので再計算の負荷は問題ない。
  - 期間指定など任意条件のベストは、ここでは保持しない。別途「集計」操作で
    まとめて算出する（リアルタイム性が不要なため）。

指標（metric）:
  total      トータルタイム（秒・最小がベスト）
  max_ms     最高速度（m/s・最大がベスト）
  lap        ラップタイム（秒・最小）
  lap_avg    ラップ平均速度（m/s・最大）
  sector     セクタータイム（秒・最小）
  sector_ms  セクター通過速度（m/s・最大）
"""

from __future__ import annotations

# 指標ごとの「良い方向」。True=小さいほど良い（タイム系）
LOWER_IS_BETTER = {
    "total": True,
    "lap": True,
    "sector": True,
    "max_ms": False,
    "lap_avg": False,
    "sector_ms": False,
}

METRICS = tuple(LOWER_IS_BETTER.keys())


def is_better(metric: str, new_value: float, old_value: float | None) -> bool:
    """new_value が old_value より良い記録か（純粋関数）。"""
    if old_value is None:
        return True
    if LOWER_IS_BETTER.get(metric, True):
        return new_value < old_value
    return new_value > old_value


def collect_from_result(result, race_id: int, speed_fn, lap_avg_fn) -> dict[str, dict]:
    """1レース分の結果から、各指標のベスト候補を抜き出す（純粋関数）。

    speed_fn(race_id, start_lane, sector_idx, lap) -> m/s
    lap_avg_fn(race_id, start_lane, lap)           -> m/s
        （実機のビーム間隔・コース全長が未設定のため、現状はダミー関数を渡す）

    戻り値: {metric: {"value":.., "race_id":.., "start_lane":.., "lap":.., "sector_no":..}}
    """
    best: dict[str, dict] = {}

    def put(metric: str, value, **meta):
        if value is None:
            return
        cur = best.get(metric)
        if is_better(metric, value, cur["value"] if cur else None):
            best[metric] = {"value": value, "race_id": race_id, **meta}

    if result is None:
        return best

    for m in result.ranking():
        if m.total_time_us is not None:
            put("total", m.total_time_us / 1e6, start_lane=m.start_lane,
                lap=None, sector_no=None)

        for lap in m.laps:
            put("lap", lap.lap_time_us / 1e6, start_lane=m.start_lane,
                lap=lap.lap, sector_no=None)
            put("lap_avg", lap_avg_fn(race_id, m.start_lane, lap.lap),
                start_lane=m.start_lane, lap=lap.lap, sector_no=None)

            for idx, sec in enumerate(lap.sectors):
                put("sector", sec.dt_us / 1e6, start_lane=m.start_lane,
                    lap=lap.lap, sector_no=idx + 1)
                sp = speed_fn(race_id, m.start_lane, idx, lap.lap)
                put("sector_ms", sp, start_lane=m.start_lane,
                    lap=lap.lap, sector_no=idx + 1)
                # 最高速はセクター通過速度の最大＝同じ母数
                put("max_ms", sp, start_lane=m.start_lane,
                    lap=lap.lap, sector_no=idx + 1)

    return best


async def load_bests(db, scope: str, scope_key: str) -> dict[str, dict]:
    """保持済みのベストを読む。戻り値: {metric: {value, race_id, start_lane, lap, sector_no}}"""
    async with db.execute(
        "SELECT metric, value, race_id, start_lane, lap, sector_no "
        "FROM timing_bests WHERE scope = ? AND scope_key = ?",
        (scope, str(scope_key)),
    ) as cur:
        rows = await cur.fetchall()
    return {
        r["metric"]: {
            "value": r["value"], "race_id": r["race_id"],
            "start_lane": r["start_lane"], "lap": r["lap"], "sector_no": r["sector_no"],
        }
        for r in rows
    }


async def merge_bests(db, scope: str, scope_key: str, candidates: dict[str, dict]) -> int:
    """候補と保持値を比べ、良い方だけ残して保存する。戻り値: 更新した指標数。"""
    current = await load_bests(db, scope, scope_key)
    updated = 0
    for metric, cand in candidates.items():
        cur = current.get(metric)
        if not is_better(metric, cand["value"], cur["value"] if cur else None):
            continue
        await db.execute(
            "INSERT INTO timing_bests "
            "(scope, scope_key, metric, value, race_id, start_lane, lap, sector_no, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?, datetime('now','localtime')) "
            "ON CONFLICT(scope, scope_key, metric) DO UPDATE SET "
            "  value=excluded.value, race_id=excluded.race_id, "
            "  start_lane=excluded.start_lane, lap=excluded.lap, "
            "  sector_no=excluded.sector_no, updated_at=excluded.updated_at",
            (scope, str(scope_key), metric, cand["value"], cand.get("race_id"),
             cand.get("start_lane"), cand.get("lap"), cand.get("sector_no")),
        )
        updated += 1
    await db.commit()
    return updated


async def update_for_race(db, race_id: int, build_fn, speed_fn, lap_avg_fn) -> dict:
    """1レース受信後に、そのレースと当日のベストを更新する。

    build_fn(db, race_id) -> (race_row, RaceResult)
    """
    race, result = await build_fn(db, race_id)
    if race is None or result is None:
        return {"race": 0, "day": 0}

    cands = collect_from_result(result, race_id, speed_fn, lap_avg_fn)
    n_race = await merge_bests(db, "race", str(race_id), cands)

    day = (race["created_at"] or "")[:10]
    n_day = await merge_bests(db, "day", day, cands) if day else 0
    return {"race": n_race, "day": n_day, "date": day}


async def recalc_day(db, date: str, list_races_fn, build_fn, speed_fn, lap_avg_fn) -> int:
    """指定日のベストを一から計算し直す（レース削除後に呼ぶ）。

    保持値を消してから、その日の全レースを走査して入れ直す。
    """
    await db.execute(
        "DELETE FROM timing_bests WHERE scope = 'day' AND scope_key = ?", (date,)
    )
    await db.commit()

    merged: dict[str, dict] = {}
    for r in await list_races_fn(date):
        rid = r["id"]
        try:
            _race, result = await build_fn(db, rid)
        except Exception:
            continue
        cands = collect_from_result(result, rid, speed_fn, lap_avg_fn)
        for metric, cand in cands.items():
            cur = merged.get(metric)
            if is_better(metric, cand["value"], cur["value"] if cur else None):
                merged[metric] = cand

    if merged:
        await merge_bests(db, "day", date, merged)
    return len(merged)
