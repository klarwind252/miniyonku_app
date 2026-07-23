"""M4LAPS 計測結果 → 予選/決勝への反映（橋渡し）

方針（決定事項）:
  - 組み合わせ情報はGWへ送らない。GWは「レーンNのタイム」だけを送ってくる。
  - どのヒートの記録かは、走り終わってから人がアプリ上で紐づける。
  - 紐づけ後、レーン番号で突き合わせて heat_results に順位・ポイントを保存する。
  - 現段階は「全員完走・全記録が正しい」前提。CO等のイレギュラーは
    保存後に既存の手入力画面から上書き訂正できる（自動確定は上書き可能）。

突き合わせの鍵:
    M4LAPS   … MachineResult.start_lane（スタートレーン番号）
    アプリ側 … heat_lanes.lane_no（そのヒートのレーン番号）
  この2つを一致させる。lane_no ↔ entry_id は対戦表作成時に確定済みなので、
  人が組み合わせを入力し直す必要はない。
"""

from __future__ import annotations


# 順位→ポイントの変換は既存ルール（qualifying.calc_points）に合わせる。
# 循環importを避けるため、呼び出し側から渡せるようにしておく。
DEFAULT_POINT_TABLE = {1: 3, 2: 2, 3: 1}


def default_calc_points(rank: int) -> int:
    return DEFAULT_POINT_TABLE.get(rank, 0)


def match_ranking_to_lanes(ranking: list[dict], lanes: list[dict]) -> dict:
    """計測の順位と、ヒートのレーン割当を突き合わせる（純粋関数）。

    ranking: [{"pos":1,"start_lane":2,"total_s":9.8,"best_s":2.7,...}, ...]
             （合計タイム昇順。build_race_result().ranking() 由来）
    lanes  : [{"lane_id":10,"lane_no":1,"entry_id":55}, ...]
             （heat_lanes 由来。lane_no はそのヒートのレーン番号）

    戻り値:
      {
        "matched":   [{"lane_id":..,"lane_no":..,"entry_id":..,
                       "rank":1,"best_time":2.7,"total_time":9.8}, ...],
        "unmatched_lanes":   [lane_no, ...],   # 計測記録が無かったレーン
        "unmatched_records": [start_lane, ...] # 対応するレーンが無かった記録
      }

    「全員完走」前提でも、レーン数の食い違い（例：2レーン対戦なのに3レーン計測）
    は起こりうる。その場合は突き合わせできなかった側を返し、呼び出し側が
    警告を出せるようにする（黙って捨てない）。
    """
    by_lane_no = {int(l["lane_no"]): l for l in lanes}
    used_lane_nos: set[int] = set()

    matched = []
    unmatched_records = []

    for m in ranking:
        sl = m.get("start_lane")
        if sl is None:
            continue
        sl = int(sl)
        lane = by_lane_no.get(sl)
        if lane is None:
            unmatched_records.append(sl)
            continue
        used_lane_nos.add(sl)
        matched.append({
            "lane_id": lane["lane_id"],
            "lane_no": sl,
            "entry_id": lane["entry_id"],
            "rank": int(m["pos"]),
            "best_time": m.get("best_s"),
            "total_time": m.get("total_s"),
            "completed_laps": m.get("completed_laps") or 0,
        })

    unmatched_lanes = [
        int(l["lane_no"]) for l in lanes if int(l["lane_no"]) not in used_lane_nos
    ]

    return {
        "matched": matched,
        "unmatched_lanes": unmatched_lanes,
        "unmatched_records": unmatched_records,
    }


def build_result_rows(matched: list[dict], calc_points=default_calc_points) -> list[dict]:
    """突き合わせ結果を heat_results への保存行に変換する（純粋関数）。

    - rank は計測の順位（合計タイム昇順）をそのまま使う
    - win は 1位のみ 1
    - points は既存の配点ルール
    - best_time はベストラップ（秒）。無ければ None
    - lap_count は完走周回数
    """
    rows = []
    for m in matched:
        rank = int(m["rank"])
        rows.append({
            "lane_id": m["lane_id"],
            "win": 1 if rank == 1 else 0,
            "best_time": m.get("best_time"),
            "lap_count": int(m.get("completed_laps") or 0),
            "rank": rank,
            "points": calc_points(rank),
            "is_co": 0,   # 現段階は全員完走前提
        })
    return rows


async def apply_race_to_heat(db, *, race_id: int, heat_id: int,
                             ranking: list[dict], calc_points=default_calc_points) -> dict:
    """計測結果を指定ヒートへ反映して保存する。

    1) heat_lanes からそのヒートのレーン割当を読む
    2) レーン番号で突き合わせ
    3) heat_results を DELETE → INSERT（既存の保存形式に合わせる）
    4) timing_races.heat_id に紐づけを記録（PIPで「反映済」と出せる）

    戻り値: {"saved": n, "unmatched_lanes": [...], "unmatched_records": [...]}
    """
    # 1) レーン割当
    async with db.execute(
        "SELECT id AS lane_id, lane_no, entry_id FROM heat_lanes "
        "WHERE heat_id = ? ORDER BY lane_no",
        (heat_id,),
    ) as cur:
        rows = await cur.fetchall()
    lanes = [
        {"lane_id": r["lane_id"], "lane_no": r["lane_no"], "entry_id": r["entry_id"]}
        for r in rows
    ]
    if not lanes:
        return {"saved": 0, "error": "heat_lanes not found",
                "unmatched_lanes": [], "unmatched_records": []}

    # 2) 突き合わせ
    m = match_ranking_to_lanes(ranking, lanes)

    # 3) 保存（既存の手入力と同じ形式。後から手で上書き可能）
    result_rows = build_result_rows(m["matched"], calc_points=calc_points)
    for row in result_rows:
        await db.execute(
            "DELETE FROM heat_results WHERE heat_lane_id=?", (row["lane_id"],)
        )
        await db.execute(
            "INSERT INTO heat_results "
            "(heat_lane_id, win, best_time, lap_count, rank, points, is_co) "
            "VALUES (?,?,?,?,?,?,?)",
            (row["lane_id"], row["win"], row["best_time"], row["lap_count"],
             row["rank"], row["points"], row["is_co"]),
        )

    # 4) 紐づけを記録
    await db.execute(
        "UPDATE timing_races SET heat_id=? WHERE id=?", (heat_id, race_id)
    )
    await db.commit()

    return {
        "saved": len(result_rows),
        "unmatched_lanes": m["unmatched_lanes"],
        "unmatched_records": m["unmatched_records"],
    }
