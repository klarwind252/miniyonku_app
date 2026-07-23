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
# セクターは **区間ごとに独立** して3傑を出す。
# 区間の長さが違うため、全セクターをまとめて比べると短い区間ばかりが上位を占め、
# S1やS2の速いタイムが評価されなくなるため。
#   sector1..sector7      … 区間1〜7の区間タイム（最小が良い）
#   sector_ms1..sector_ms7 … 区間1〜7の通過速度（最大が良い）
MAX_SECTORS = 7

LOWER_IS_BETTER = {
    "total": True,
    "lap": True,
    "max_ms": False,
    "lap_avg": False,
}
for _i in range(1, MAX_SECTORS + 1):
    LOWER_IS_BETTER[f"sector{_i}"] = True       # 区間タイム：小さいほど良い
    LOWER_IS_BETTER[f"sector_ms{_i}"] = False   # 通過速度：大きいほど良い

METRICS = tuple(LOWER_IS_BETTER.keys())


def sector_metric(sector_no: int) -> str:
    """区間番号 → 区間タイムの metric 名。"""
    return f"sector{sector_no}"


def sector_speed_metric(sector_no: int) -> str:
    """区間番号 → 通過速度の metric 名。"""
    return f"sector_ms{sector_no}"


def is_better(metric: str, new_value: float, old_value: float | None) -> bool:
    """new_value が old_value より良い記録か（純粋関数）。"""
    if old_value is None:
        return True
    if LOWER_IS_BETTER.get(metric, True):
        return new_value < old_value
    return new_value > old_value


def collect_from_result(result, race_id: int, speed_fn, lap_avg_fn) -> dict[str, list[dict]]:
    """1レース分の結果から、各指標の上位候補を抜き出す（純粋関数）。

    speed_fn(race_id, start_lane, sector_idx, lap) -> m/s
    lap_avg_fn(race_id, start_lane, lap)           -> m/s
        （実機のビーム間隔・コース全長が未設定のため、現状はダミー関数を渡す）

    戻り値: {metric: [{"value":.., "race_id":.., "start_lane":.., "lap":.., "sector_no":..}, ...]}
            各指標につき上位 TOP_N 件（良い順）。
    """
    pool: dict[str, list[dict]] = {}

    def put(metric: str, value, **meta):
        if value is None:
            return
        pool.setdefault(metric, []).append(
            {"value": value, "race_id": race_id, **meta}
        )

    if result is None:
        return {}

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
                sno = idx + 1
                if sno > MAX_SECTORS:
                    break
                # 区間ごとに独立した metric へ入れる（S1はS1の中だけで競う）
                put(sector_metric(sno), sec.dt_us / 1e6, start_lane=m.start_lane,
                    lap=lap.lap, sector_no=sno)
                sp = speed_fn(race_id, m.start_lane, idx, lap.lap)
                put(sector_speed_metric(sno), sp, start_lane=m.start_lane,
                    lap=lap.lap, sector_no=sno)
                # 最高速は全区間を通じた最大（こちらは横断で正しい）
                put("max_ms", sp, start_lane=m.start_lane,
                    lap=lap.lap, sector_no=sno)

    # 各指標につき上位 TOP_N 件に絞る（同一レコードの重複は除く）
    return {metric: _merge_top(metric, [], cands) for metric, cands in pool.items()}


TOP_N = 3   # 保持する上位件数（画面で1〜3位を色分けするため）


async def load_bests(db, scope: str, scope_key: str) -> dict[str, list[dict]]:
    """保持済みのベストを読む（上位3件まで）。

    戻り値: {metric: [ {rank, value, race_id, start_lane, lap, sector_no}, ... ]}
            リストは rank 昇順（1位→3位）。
    """
    async with db.execute(
        "SELECT metric, rank, value, race_id, start_lane, lap, sector_no "
        "FROM timing_bests WHERE scope = ? AND scope_key = ? "
        "ORDER BY metric, rank",
        (scope, str(scope_key)),
    ) as cur:
        rows = await cur.fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["metric"], []).append({
            "rank": r["rank"], "value": r["value"], "race_id": r["race_id"],
            "start_lane": r["start_lane"], "lap": r["lap"], "sector_no": r["sector_no"],
        })
    return out


def _merge_top(metric: str, existing: list[dict], cands: list[dict]) -> list[dict]:
    """保持中の上位リストと候補をまとめ、良い順に TOP_N 件へ絞る（純粋関数）。

    同じ値が複数あっても、別のマシン／周／セクターなら別の記録として扱う
    （同一レコードの重複だけを除く）。
    """
    merged = list(existing) + list(cands)
    seen = set()
    uniq = []
    for m in merged:
        key = (round(m["value"], 6), m.get("race_id"), m.get("start_lane"),
               m.get("lap"), m.get("sector_no"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(m)
    uniq.sort(key=lambda x: x["value"], reverse=not LOWER_IS_BETTER.get(metric, True))
    return uniq[:TOP_N]


async def merge_bests(db, scope: str, scope_key: str, candidates: dict[str, dict]) -> int:
    """候補を取り込み、上位3件を保持し直す。戻り値: 書き換えた指標数。

    candidates は {metric: 候補1件} でも {metric: [候補...]} でも受け付ける。
    """
    current = await load_bests(db, scope, scope_key)
    updated = 0
    for metric, cand in candidates.items():
        cands = cand if isinstance(cand, list) else [cand]
        before = current.get(metric, [])
        after = _merge_top(metric, before, cands)
        # 中身が変わらなければ書かない（無駄なUPDATEを避ける）
        if [x["value"] for x in before] == [x["value"] for x in after] and len(before) == len(after):
            continue
        await db.execute(
            "DELETE FROM timing_bests WHERE scope=? AND scope_key=? AND metric=?",
            (scope, str(scope_key), metric),
        )
        for i, m in enumerate(after, start=1):
            await db.execute(
                "INSERT INTO timing_bests "
                "(scope, scope_key, metric, rank, value, race_id, start_lane, lap, sector_no, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, datetime('now','localtime'))",
                (scope, str(scope_key), metric, i, m["value"], m.get("race_id"),
                 m.get("start_lane"), m.get("lap"), m.get("sector_no")),
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


async def aggregate_range(db, *, date_from: str, date_to: str, mode: str | None,
                          list_fn, build_fn, speed_fn, lap_avg_fn) -> dict:
    """期間・タイプを指定してベストを集計する（保持しない・都度計算）。

    date_from / date_to : 'YYYY-MM-DD'（両端を含む）
    mode                : 'f1'（レース）/ 'run'（フリー）/ None（すべて）
    list_fn(date_from, date_to) -> レース行の列挙

    リアルタイム性が不要なため timing_bests には保存せず、その場で計算して返す。
    戻り値: {"bests": {metric: {..., created_at, mode}}, "race_count": n}
    """
    merged: dict[str, list[dict]] = {}
    n_races = 0

    for r in await list_fn(date_from, date_to):
        rid = r["id"]
        try:
            race, result = await build_fn(db, rid)
        except Exception:
            continue
        if race is None or result is None:
            continue
        # タイプで絞る（result.mode は 'f1'=レース / 'run'=フリー）
        if mode and result.mode != mode:
            continue
        n_races += 1

        cands = collect_from_result(result, rid, speed_fn, lap_avg_fn)
        for metric, lst in cands.items():
            # 「いつ・どのタイプの計測か」を各候補に添える
            enriched = []
            for c in lst:
                c = dict(c)
                c["created_at"] = race["created_at"]
                c["mode"] = result.mode
                enriched.append(c)
            merged[metric] = _merge_top(metric, merged.get(metric, []), enriched)

    # 期間集計は1位だけを返す（画面はベスト1件を表示する）
    bests = {metric: lst[0] for metric, lst in merged.items() if lst}
    return {"bests": bests, "race_count": n_races}


async def recalc_day(db, date: str, list_races_fn, build_fn, speed_fn, lap_avg_fn) -> int:
    """指定日のベストを一から計算し直す（レース削除後に呼ぶ）。

    保持値を消してから、その日の全レースを走査して入れ直す。
    """
    await db.execute(
        "DELETE FROM timing_bests WHERE scope = 'day' AND scope_key = ?", (date,)
    )
    await db.commit()

    merged: dict[str, list[dict]] = {}
    for r in await list_races_fn(date):
        rid = r["id"]
        try:
            _race, result = await build_fn(db, rid)
        except Exception:
            continue
        cands = collect_from_result(result, rid, speed_fn, lap_avg_fn)
        for metric, lst in cands.items():
            merged[metric] = _merge_top(metric, merged.get(metric, []), lst)

    if merged:
        await merge_bests(db, "day", date, merged)
    return len(merged)
