"""レーサー別のベスト集計（application層）。

大会に紐づいた計測レース（反映済み＝heat_id / applied_ht_group_id /
applied_group_id を持つ timing_races）を1回のパスで走査し、
「レーサー別ベスト（bests）」と「記録保持者（records）」を同時に集める。

⚠ 反映済みのレースだけが対象。未反映のレーンは誰の記録か不明なため含めない。
   紐づけは apply 時に確定した lane_no/slot_no ↔ entry_id を使う。

従来は bests と records が別関数でそれぞれフルスキャンしており、
1ページ表示で最大5回の重複走査が発生していた。scan_tournament_metrics()
に一本化し、既存API（racer_bests_for_tournament /
record_holders_for_tournament）はその結果を切り出す薄いラッパーとして
互換維持する。ルーター側はページ先頭で1回だけ scan して使い回すこと。

指標（best_svc の metric 名に合わせる）:
  total, total_avg, max_ms, lap, lap_avg, sector1..7, sector_ms1..7
"""

from __future__ import annotations

from app.application import timing_race_speed_store as speed_store
from app.application import timing_best_service as best_svc
from app.application.timing_race_service import build_race_result

_TOL = 1e-6


async def _collect_race_rows(db, tournament_id: int, include_finals: bool) -> list[dict]:
    """反映済み計測レースの一覧（予選H＋予選HT、include_finals なら決勝Gも）。"""
    if include_finals:
        sql = ("SELECT tr.id AS race_id, tr.heat_id, NULL AS group_id"
               "  FROM timing_races tr JOIN heats h ON h.id = tr.heat_id"
               " WHERE h.tournament_id = ?"
               " UNION ALL "
               "SELECT tr.id AS race_id, NULL AS heat_id, tr.applied_group_id AS group_id"
               "  FROM timing_races tr"
               "  JOIN bracket_groups bg ON bg.id = tr.applied_group_id"
               "  JOIN bracket_rounds br ON br.id = bg.round_id"
               " WHERE br.tournament_id = ?")
        params = (tournament_id, tournament_id)
    else:
        sql = ("SELECT tr.id AS race_id, tr.heat_id, NULL AS group_id"
               "  FROM timing_races tr JOIN heats h ON h.id = tr.heat_id"
               " WHERE h.tournament_id = ?")
        params = (tournament_id,)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    race_rows = [{"race_id": r["race_id"], "heat_id": r["heat_id"],
                  "group_id": r["group_id"], "ht_group_id": None} for r in rows]
    # 予選ヒートトーナメント（applied_ht_group_id）は「予選の記録」として
    # include_finals に関わらず常に合算する（旧DBは列なし→静かにスキップ）。
    try:
        async with db.execute(
            """SELECT tr.id AS race_id, tr.applied_ht_group_id AS ht_group_id
                 FROM timing_races tr
                 JOIN ht_groups hg ON hg.id = tr.applied_ht_group_id
                 JOIN ht_rounds hr ON hr.id = hg.round_id
                WHERE hr.tournament_id = ?""",
            (tournament_id,),
        ) as cur:
            for r in await cur.fetchall():
                race_rows.append({"race_id": r["race_id"], "heat_id": None,
                                  "group_id": None, "ht_group_id": r["ht_group_id"]})
    except Exception:
        pass
    # 安全網：1レースは必ず1回だけ走査する。反映操作の履歴によっては heat_id と
    # applied_(ht_)group_id が同時に残り得るため、race_id で重複排除する
    # （先勝ち＝上の並びどおり 予選H → 決勝G → 予選HT の優先）。重複を許すと
    # 同じ記録が別のレーン対応表で二重集計され、RECORD HOLDERS が壊れる。
    _seen: set = set()
    _uniq: list[dict] = []
    for _row in race_rows:
        if _row["race_id"] in _seen:
            continue
        _seen.add(_row["race_id"])
        _uniq.append(_row)
    return _uniq


async def _build_lane_to_entry(db, race_rows: list[dict]) -> dict:
    """("H"|"G"|"T", owner_id, lane/slot_no) → entry_id の対応表をIN句で一括構築。

    M4LAPS の start_lane は heat_lanes.lane_no / bracket_slots.slot_no /
    ht_slots.slot_no と一致する。
    """
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
    ht_group_ids = sorted({r["ht_group_id"] for r in race_rows if r["ht_group_id"] is not None})
    if ht_group_ids:
        ph = ",".join("?" * len(ht_group_ids))
        async with db.execute(
            f"SELECT group_id, slot_no, entry_id FROM ht_slots "
            f"WHERE group_id IN ({ph}) AND entry_id IS NOT NULL",
            ht_group_ids,
        ) as cur:
            for r in await cur.fetchall():
                lane_to_entry[("T", r["group_id"], r["slot_no"])] = r["entry_id"]
    return lane_to_entry


async def scan_tournament_metrics(db, tournament_id: int,
                                  include_finals: bool = False) -> dict:
    """全計測レースを1回だけ走査し {"bests": …, "records": …} を返す。

    bests   … entry_id → {metric: best_value}（タイム系は最小、速度系は最大）
    records … overall / fastest_lap / top_speed / sectors。
              同率（許容誤差内）は holders に複数入る（表示側で羅列）。
              gap は distinct な上位2値の差。
    処理量: レース数×レーン数×ラップ数。1リクエストにつき1回だけ呼ぶこと。
    """
    bests: dict[int, dict] = {}
    records = {
        "overall":     {"value": None, "holders": []},
        "fastest_lap": {"value": None, "holders": []},
        "top_speed":   {"value": None, "holders": []},
        # sno(1..) -> {"value": 区間最速タイム, "holders": [(entry_id, heat_id), ...]}
        "sectors":     {},
    }

    race_rows = await _collect_race_rows(db, tournament_id, include_finals)
    if not race_rows:
        for _k in ("overall", "fastest_lap", "top_speed"):
            records[_k]["gap"] = None
        return {"bests": bests, "records": records}

    lane_to_entry = await _build_lane_to_entry(db, race_rows)

    def _put(entry_id: int, metric: str, value):
        if value is None:
            return
        cur_map = bests.setdefault(entry_id, {})
        if best_svc.is_better(metric, value, cur_map.get(metric)):
            cur_map[metric] = value

    def _consider(key: str, value, entry_id: int, heat_id,
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

    def _consider_sec(sno: int, value, entry_id: int, heat_id):
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
        # 予選ヒート(H)／決勝グループ(G)／予選ヒートトーナメント(T)で
        # レーン→エントリーの引き方が変わる。holders に入れる heat_id は
        # 予選ヒートのみ実値、それ以外は None（ラベルは付けない）。
        if rr["heat_id"] is not None:
            _scope, _owner, _hid = "H", rr["heat_id"], rr["heat_id"]
        elif rr["group_id"] is not None:
            _scope, _owner, _hid = "G", rr["group_id"], None
        else:
            _scope, _owner, _hid = "T", rr["ht_group_id"], None
        try:
            race, result = await build_race_result(db, rr["race_id"])
        except Exception:
            continue
        if race is None or result is None:
            continue

        # 速度は反映時に保存済みの値を読む（都度計算しない・定義も統一）
        st = await speed_store.load_speeds(db, rr["race_id"])

        for m in result.ranking():
            entry_id = lane_to_entry.get((_scope, _owner, m.start_lane))
            if entry_id is None:
                continue
            lane = m.start_lane
            lane_sp = st["lane"].get(lane, {})

            # TOTAL（タイム）は結果から、TOTAL Av./MAX は保存値から
            if m.total_time_us is not None:
                _tv = m.total_time_us / 1e6
                _put(entry_id, "total", _tv)
                _consider("overall", _tv, entry_id, _hid, higher_is_better=False)
            _put(entry_id, "total_avg", lane_sp.get("total_avg"))
            _mx = lane_sp.get("max_ms")
            _put(entry_id, "max_ms", _mx)
            _consider("top_speed", _mx, entry_id, _hid, higher_is_better=True)

            for lap in m.laps:
                _lv = lap.lap_time_us / 1e6
                _put(entry_id, "lap", _lv)
                _consider("fastest_lap", _lv, entry_id, _hid, higher_is_better=False)
                _put(entry_id, "lap_avg", st["lap"].get((lane, lap.lap)))
                for idx, sec in enumerate(lap.sectors):
                    sno = idx + 1
                    if sno > best_svc.MAX_SECTORS:
                        break
                    _sv = sec.dt_us / 1e6
                    _put(entry_id, best_svc.sector_metric(sno), _sv)
                    _put(entry_id, best_svc.sector_speed_metric(sno),
                         st["sec"].get((lane, lap.lap, sno)))
                    _consider_sec(sno, _sv, entry_id, _hid)

    # 2位との差（distinct な上位2値の差）。overall/lap は昇順、top_speed は降順。
    # _vals は pop して保持しない（メモリ節約）。
    for _k, _hi in (("overall", False), ("fastest_lap", False), ("top_speed", True)):
        _vals = sorted(records[_k].pop("_vals", set()), reverse=_hi)
        records[_k]["gap"] = (abs(_vals[0] - _vals[1]) if len(_vals) >= 2 else None)

    return {"bests": bests, "records": records}


async def racer_bests_for_tournament(db, tournament_id: int,
                                     include_finals: bool = False,
                                     *, scan: dict | None = None) -> dict[int, dict]:
    """entry_id -> {metric: value} のベスト表を返す（互換API）。

    scan= に scan_tournament_metrics() の結果を渡すと再走査せずに
    その結果を使う（同一リクエスト内の重複スキャン防止）。
    """
    if scan is None:
        scan = await scan_tournament_metrics(db, tournament_id, include_finals)
    return scan["bests"]


async def record_holders_for_tournament(db, tournament_id: int,
                                        include_finals: bool = False,
                                        *, scan: dict | None = None) -> dict:
    """記録保持者（overall/fastest_lap/top_speed/sectors）を返す（互換API）。

    scan= 対応は racer_bests_for_tournament と同じ。
    """
    if scan is None:
        scan = await scan_tournament_metrics(db, tournament_id, include_finals)
    return scan["records"]


async def _ht_finish_counts(db, tournament_id: int) -> dict:
    """ヒートトーナメント予選の (出走数, 完走数) を entry_id → (rc, fc) で返す。

    分母 = そのエントリーが出走枠を持つ組のうち、計測が反映された組の数
           （ht_slot_ranks にその組の行が1つ以上ある）
    分子 = そのうち本人の total_time が記録された（＝完走した）組の数
    ※ ht_slot_ranks.total_time はマイグレーション追加列。旧DBでは空 dict を返す。
    """
    counts: dict = {}
    try:
        async with db.execute(
            """SELECT hs.entry_id,
                      COUNT(*) AS race_count,
                      SUM(CASE WHEN mine.total_time IS NOT NULL THEN 1 ELSE 0 END) AS finish_count
                 FROM ht_slots hs
                 JOIN ht_groups hg ON hg.id = hs.group_id
                 JOIN ht_rounds hr ON hr.id = hg.round_id
                 LEFT JOIN ht_slot_ranks mine
                        ON mine.group_id = hs.group_id AND mine.slot_id = hs.id
                WHERE hr.tournament_id = ?
                  AND hs.entry_id IS NOT NULL
                  AND EXISTS (SELECT 1 FROM ht_slot_ranks any_r
                               WHERE any_r.group_id = hs.group_id)
                GROUP BY hs.entry_id""",
            (tournament_id,),
        ) as cur:
            for r in await cur.fetchall():
                counts[r["entry_id"]] = (r["race_count"] or 0, r["finish_count"] or 0)
    except Exception:
        return {}
    return counts


async def _heat_finish_counts(db, tournament_id: int) -> dict:
    """通常予選ヒートの (出走数, 完走数) を entry_id → (rc, fc) で返す。

    _calc_standings と同じ定義:
      分母 = 確定した heat_results の行数
      分子 = CO でなく rank>0 の行数
    heats テーブルは予選のみが対象（決勝は bracket_* に入るため混入しない）。
    """
    counts: dict = {}
    try:
        async with db.execute(
            """SELECT hl.entry_id,
                      COUNT(hr.id) AS race_count,
                      COALESCE(SUM(CASE WHEN COALESCE(hr.is_co,0)=0 AND hr.rank > 0
                                        THEN 1 ELSE 0 END), 0) AS finish_count
                 FROM heat_lanes hl
                 JOIN heats h ON h.id = hl.heat_id
                 JOIN heat_results hr ON hr.heat_lane_id = hl.id
                WHERE h.tournament_id = ?
                  AND hl.entry_id IS NOT NULL
                GROUP BY hl.entry_id""",
            (tournament_id,),
        ) as cur:
            for r in await cur.fetchall():
                counts[r["entry_id"]] = (r["race_count"] or 0, r["finish_count"] or 0)
    except Exception:
        return {}
    return counts


async def qualifying_finish_rates(db, tournament_id: int) -> dict:
    """予選のみの完走率（%）を entry_id → int|None で返す（決勝は一切含めない）。

    予選タイプに依存せず、
      ・通常予選ヒート（heats / heat_results）由来の出走・完走数
      ・予選ヒートトーナメント（ht_*）由来の出走・完走数
    を件数レベルで合算してから％にする。これにより
      point/order（ヒートのみ）・heat_tournament系（HTのみ）・
      heat_roundrobin（総当たりヒート＋ヒート決勝HTの混在）
    のすべてを1つの関数でカバーする。
    予選の記録が1件も無いエントリーは None（表示は「—」）。
    """
    rates: dict = {}
    heat_counts = await _heat_finish_counts(db, tournament_id)
    ht_counts = await _ht_finish_counts(db, tournament_id)
    for eid in set(heat_counts) | set(ht_counts):
        h_rc, h_fc = heat_counts.get(eid, (0, 0))
        t_rc, t_fc = ht_counts.get(eid, (0, 0))
        rc = h_rc + t_rc
        fc = h_fc + t_fc
        rates[eid] = (int(fc / rc * 100) if rc > 0 else None)
    return rates


async def ht_finish_rates(db, tournament_id: int) -> dict:
    """ヒートトーナメント予選の完走率（%）を entry_id → int|None で返す。

    定義（通常予選の finish_count/race_count と整合する計測ベースの近似）:
      分母 = そのエントリーが出走枠を持つ組のうち、計測が反映された組の数
             （ht_slot_ranks にその組の行が1つ以上ある）
      分子 = そのうち本人の total_time が記録された（＝完走した）組の数
    計測反映が1組も無いエントリーは None（表示は「—」）。
    ※ ht_slot_ranks.total_time はマイグレーション追加列。旧DBでは全員 None を返す
      （従来どおり「—」表示のまま。例外は出さない）。
    """
    counts = await _ht_finish_counts(db, tournament_id)
    return {eid: (int(fc / rc * 100) if rc > 0 else None)
            for eid, (rc, fc) in counts.items()}
