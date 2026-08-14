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
            "dnf": bool(m.get("dnf")),   # E5(24.39)：CO=規定周回未達。予選で「CO」表示に使う
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

    - rank は **突き合わせできた人の中で振り直す**（1位から連番）
      ⚠ 計測の順位をそのまま使うと、対象外のレーンが上位にいた場合に
        「1位が不在で誰も勝者にならない」状態になるため。
        例）2レーン対戦に3レーン計測を反映 → 計測1位が対象外なら
            残り2人は元のまま2位・3位となり、勝者が決まらない。
    - win は（振り直した）1位のみ 1。ただし CO(未完走)は勝者にしない。
    - points は既存の配点ルール。CO(未完走)は 0 点。
    - is_co は E5(24.39)：規定周回未達=CO。予選画面は is_co の枠を「CO」表示にする
      （順位数字は出さない）。ranking() は完走者→未完走者の順なので、連番の
      後半（＝完走者の後ろ）が CO になり、完走者の順位は乱れない。
    - best_time はベストラップ（秒）。無ければ None
    - total_time はFINISHタイム（合計・秒）。無ければ None
    - lap_count は完走周回数
    """
    # 計測順位の昇順に並べ、その並びで1位から振り直す
    ordered = sorted(matched, key=lambda m: int(m["rank"]))
    rows = []
    for i, m in enumerate(ordered, start=1):
        co = bool(m.get("dnf"))
        rows.append({
            "lane_id": m["lane_id"],
            "win": 1 if (i == 1 and not co) else 0,
            "best_time": m.get("best_time"),
            # FINISHタイム（合計）。予選画面に出すため保存する。
            # 手入力では埋まらない項目なので、未反映のヒートは NULL のまま。
            "total_time": m.get("total_time"),
            "lap_count": int(m.get("completed_laps") or 0),
            "rank": i,
            "points": 0 if co else calc_points(i),
            "is_co": 1 if co else 0,   # 未完走(CO/DNF)は CO 表示・0点・非勝者
        })
    return rows


async def apply_race_to_bracket_group(db, *, race_id: int, group_id: int,
                                      ranking: list[dict]) -> dict:
    """計測結果を決勝のグループへ反映して保存する。

    決勝は予選と別系統のテーブルを使う。
        bracket_slots      (group_id, slot_no, entry_id)   … 誰がどの枠か
        bracket_results    (group_id, winner_slot_id)      … 勝者
        bracket_slot_ranks (group_id, slot_id, rank)       … 各枠の順位

    突き合わせの鍵は **slot_no ↔ start_lane**（予選の lane_no と同じ考え方）。
    スロット番号がそのままレーン番号に対応する前提。

    戻り値: {"saved": n, "winner_slot_id":.., "unmatched_slots":[], "unmatched_records":[]}
    """
    async with db.execute(
        "SELECT id AS slot_id, slot_no, entry_id, is_bye FROM bracket_slots "
        "WHERE group_id = ? ORDER BY slot_no",
        (group_id,),
    ) as cur:
        rows = await cur.fetchall()
    # 不戦勝(BYE)や空き枠は対象外
    slots = [
        {"lane_id": r["slot_id"], "lane_no": r["slot_no"], "entry_id": r["entry_id"]}
        for r in rows if r["entry_id"] is not None and not r["is_bye"]
    ]
    if not slots:
        return {"saved": 0, "error": "bracket_slots not found",
                "winner_slot_id": None, "unmatched_slots": [], "unmatched_records": []}

    m = match_ranking_to_lanes(ranking, slots)
    matched = m["matched"]
    if not matched:
        return {"saved": 0, "error": "no match",
                "winner_slot_id": None,
                "unmatched_slots": m["unmatched_lanes"],
                "unmatched_records": m["unmatched_records"]}

    # 順位決定が必要なラウンドか（決勝=final / 3位決定戦=third は 1-2-3 を確定させる）。
    # それ以外（通常ラウンド・敗者復活/裏トーナメント）は勝者を決めるだけ＝順位決定不要。
    #   → 順位不要のラウンドでは CO(未完走) に順位を付けない。カードは「順位なし＝CO」と
    #     判定して CO 表示する（22章・24.39）。final/third は従来どおり全員に順位を付ける。
    _rank_needed = True
    try:
        async with db.execute(
            "SELECT r.round_type FROM bracket_groups g "
            "JOIN bracket_rounds r ON g.round_id = r.id WHERE g.id = ?",
            (group_id,),
        ) as cur:
            _rt = await cur.fetchone()
        if _rt is not None:
            _rank_needed = (_rt[0] in ("final", "third"))
    except Exception:
        _rank_needed = True  # 判定不能時は安全側（従来どおり全員に順位）

    # 順位を入れ直す（再反映しても重複しないよう一度消す）
    # ⚠ 予選と同じく、突き合わせできた枠の中で1位から振り直す。
    #    計測順位をそのまま使うと、対象外の枠が上位にいた場合に勝者が決まらない。
    await db.execute("DELETE FROM bracket_slot_ranks WHERE group_id = ?", (group_id,))
    winner_slot_id = None
    i = 0
    for x in sorted(matched, key=lambda m: int(m["rank"])):
        co = bool(x.get("dnf"))
        if co and not _rank_needed:
            continue  # 順位決定不要のラウンド：CO には順位を付けない（画面で「CO」表示）
        i += 1
        await db.execute(
            "INSERT INTO bracket_slot_ranks "
            "(group_id, slot_id, rank, total_time, best_time) VALUES (?,?,?,?,?)",
            (group_id, x["lane_id"], i, x.get("total_time"), x.get("best_time")),
        )
        if i == 1 and not co:
            winner_slot_id = x["lane_id"]

    # 勝者を確定（既にあれば上書き）
    if winner_slot_id is not None:
        await db.execute(
            "INSERT INTO bracket_results (group_id, winner_slot_id, recorded_at) "
            "VALUES (?,?, datetime('now','localtime')) "
            "ON CONFLICT(group_id) DO UPDATE SET "
            "  winner_slot_id=excluded.winner_slot_id, recorded_at=excluded.recorded_at",
            (group_id, winner_slot_id),
        )

    # どのグループへ反映したかを記録する（同じ結果の重複反映を検出するため）
    await db.execute(
        "UPDATE timing_races SET applied_group_id = ? WHERE id = ?",
        (group_id, race_id),
    )
    await db.commit()

    return {
        "saved": len(matched),
        "winner_slot_id": winner_slot_id,
        "unmatched_slots": m["unmatched_lanes"],
        "unmatched_records": m["unmatched_records"],
    }


async def apply_race_to_ht_group(db, *, race_id: int, group_id: int,
                                 ranking: list[dict]) -> dict:
    """計測結果をヒートトーナメントのグループへ反映して保存する。

    ヒートトーナメントは決勝ブラケットと同じ「トーナメント形式」だが、専用テーブルを使う。
        ht_slots      (id, group_id, slot_no, entry_id)   … 誰がどの枠か
        ht_results    (group_id, winner_slot_id)          … 勝者
        ht_slot_ranks (group_id, slot_id, rank)           … 各枠の順位
    ※ ht_* にはタイム列が無いため、順位と勝者のみ保存する（画面表示も○のみで従来どおり）。

    突き合わせの鍵は **slot_no ↔ start_lane**（決勝ブラケットと同じ考え方）。

    戻り値: {"saved": n, "winner_slot_id":.., "unmatched_slots":[], "unmatched_records":[]}
    """
    async with db.execute(
        "SELECT id AS slot_id, slot_no, entry_id FROM ht_slots "
        "WHERE group_id = ? ORDER BY slot_no",
        (group_id,),
    ) as cur:
        rows = await cur.fetchall()
    # 空き枠（entry_id=NULL）は対象外
    slots = [
        {"lane_id": r["slot_id"], "lane_no": r["slot_no"], "entry_id": r["entry_id"]}
        for r in rows if r["entry_id"] is not None
    ]
    if not slots:
        return {"saved": 0, "error": "ht_slots not found",
                "winner_slot_id": None, "unmatched_slots": [], "unmatched_records": []}

    m = match_ranking_to_lanes(ranking, slots)
    matched = m["matched"]
    if not matched:
        return {"saved": 0, "error": "no match",
                "winner_slot_id": None,
                "unmatched_slots": m["unmatched_lanes"],
                "unmatched_records": m["unmatched_records"]}

    # 順位決定が必要なラウンドか（決勝=final / 3位決定戦=third）。それ以外（通常・敗者復活）は
    # 勝者を決めるだけ＝順位決定不要 → CO(未完走)には順位を付けない（カードで「CO」表示）。
    _rank_needed = True
    try:
        async with db.execute(
            "SELECT r.round_type FROM ht_groups g "
            "JOIN ht_rounds r ON g.round_id = r.id WHERE g.id = ?",
            (group_id,),
        ) as cur:
            _rt = await cur.fetchone()
        if _rt is not None:
            _rank_needed = (_rt[0] in ("final", "third"))
    except Exception:
        _rank_needed = True

    # 突き合わせできた枠の中で1位から順位を振り直す（決勝ブラケットと同じ流儀）。
    # タイム（total_time/best_time）も保存して、決勝と同様に画面へ表示できるようにする。
    # ※ total_time/best_time 列はマイグレーション（schema.py）で追加される。
    #   未適用の旧DBでは列なしINSERTへフォールバックし、反映自体は必ず成立させる。
    await db.execute("DELETE FROM ht_slot_ranks WHERE group_id = ?", (group_id,))
    winner_slot_id = None
    i = 0
    for x in sorted(matched, key=lambda mm: int(mm["rank"])):
        co = bool(x.get("dnf"))
        if co and not _rank_needed:
            continue  # 順位決定不要のラウンド：CO には順位を付けない（画面で「CO」表示）
        i += 1
        try:
            await db.execute(
                "INSERT INTO ht_slot_ranks (group_id, slot_id, rank, total_time, best_time) VALUES (?,?,?,?,?)",
                (group_id, x["lane_id"], i, x.get("total_time"), x.get("best_time")),
            )
        except Exception:
            await db.execute(
                "INSERT INTO ht_slot_ranks (group_id, slot_id, rank) VALUES (?,?,?)",
                (group_id, x["lane_id"], i),
            )
        if i == 1 and not co:
            winner_slot_id = x["lane_id"]

    # 勝者を確定（DELETE→INSERT。ht_results は group_id にユニーク制約が無いため）
    await db.execute("DELETE FROM ht_results WHERE group_id = ?", (group_id,))
    if winner_slot_id is not None:
        await db.execute(
            "INSERT INTO ht_results (group_id, winner_slot_id) VALUES (?,?)",
            (group_id, winner_slot_id),
        )
    # 核となる順位・勝者を先に確定させる（この後の記録が失敗しても反映は成立させる）
    await db.commit()

    # どのヒート組へ反映したかを記録（PIPの「反映済」表示・重複反映の検出に使う）。
    # マイグレーション未適用（カラム無し）の旧DBでも反映自体は成立させる。
    try:
        await db.execute(
            "UPDATE timing_races SET applied_ht_group_id = ? WHERE id = ?",
            (group_id, race_id),
        )
        await db.commit()
    except Exception:
        pass

    return {
        "saved": len(matched),
        "winner_slot_id": winner_slot_id,
        "unmatched_slots": m["unmatched_lanes"],
        "unmatched_records": m["unmatched_records"],
    }


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
            "(heat_lane_id, win, best_time, total_time, lap_count, rank, points, is_co) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (row["lane_id"], row["win"], row["best_time"], row.get("total_time"),
             row["lap_count"], row["rank"], row["points"], row["is_co"]),
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
