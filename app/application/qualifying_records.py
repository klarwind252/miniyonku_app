"""予選「記録保持者」パネルの共通ロジック（application層）。

admin（予選管理）と viewer（観覧）の両方で同じ表示・同じ順位判定を使うための
純粋関数＋DBヘルパーをまとめる。記録の生値集計は
timing_racer_best_service.record_holders_for_tournament() が担当し、
ここでは「表示用整形」と「POINT LEADER の1名絞り込み」を提供する。
"""

from __future__ import annotations


def build_heat_labels(heats) -> dict:
    """heat_id -> 「予選N回目 レースM」ラベル。

    M は各予選回（round_no）内の連番（heat_no 昇順）。予選管理／観覧の
    スケジュール「#」列と一致させる。
    """
    round_heats: dict = {}
    for h in heats:
        round_heats.setdefault(h["round_no"], []).append(h)
    labels: dict = {}
    for rno, hs in round_heats.items():
        for seq, h in enumerate(sorted(hs, key=lambda x: x["heat_no"]), start=1):
            labels[h["id"]] = f"予選{rno}回目 レース{seq}"
    return labels


def format_records_display(raw, name_by_entry: dict, heat_labels: dict):
    """record_holders_for_tournament() の生値を表示用に整形する。

    raw: {"overall"/"fastest_lap"/"top_speed": {"value", "holders":[(entry_id,heat_id),...]}}
    戻り値: {"overall"/"fastest_lap"/"top_speed": {"value_str", "holders":[{"name","label"}]}}
            いずれも記録が無ければ None。全部無ければ全体 None。
    同率（holders 複数）はそのまま複数返す（表示側で羅列）。
    """
    if not raw:
        return None

    def _fmt(rec, *, speed: bool):
        if not rec or rec.get("value") is None or not rec.get("holders"):
            return None
        v = rec["value"]
        vstr = f"{v:.2f} m/s" if speed else f"{v:.3f} sec"
        _gap = rec.get("gap")
        gap_str = None
        if _gap is not None:
            gap_str = f"-{_gap:.2f}" if speed else f"-{_gap:.3f}"
        holders = [
            {"name": name_by_entry.get(eid, "?"), "label": heat_labels.get(hid, "")}
            for (eid, hid) in rec["holders"]
        ]
        return {"value_str": vstr, "gap_str": gap_str, "holders": holders}

    out = {
        "overall": _fmt(raw.get("overall"), speed=False),
        "fastest_lap": _fmt(raw.get("fastest_lap"), speed=False),
        "top_speed": _fmt(raw.get("top_speed"), speed=True),
    }
    return out if any(out.values()) else None


async def resolve_point_leader(db, tournament_id: int, leaders: list,
                               best_total_by_entry: dict):
    """同率首位（leaders＝rank==1 のリスト）を1名に絞って返す。

    絞り方（admin/viewer 共通）:
      1) M4LAPSあり … ベスト TOTAL タイムが最速の1名（best_total_by_entry を使用）
      2) M4LAPSなし … 直接対決の勝ち数 ＞ 1着の数 ＞ CO の少なさ、で1名
         （それでも並ぶ場合は leaders の並び順で先頭＝確実に1名）

    leaders は entry_id / name を持つ dict のリスト。
    best_total_by_entry は {entry_id: ベストTOTAL秒}。M4LAPSが無ければ空 dict でよい。
    戻り値: leaders の要素1つ、または leaders が空なら None。
    """
    if not leaders:
        return None
    if len(leaders) == 1:
        return leaders[0]

    # (1) M4LAPS：ベスト TOTAL タイムが最速の1名
    timed = [(best_total_by_entry.get(s["entry_id"]), s) for s in leaders]
    timed = [(tv, s) for (tv, s) in timed if tv is not None]
    if timed:
        return min(timed, key=lambda x: x[0])[1]

    # (2) M4LAPSなし：直接対決 ＞ 1着数 ＞ CO数（少ない）
    ids = [s["entry_id"] for s in leaders]
    ph = ",".join("?" * len(ids))
    async with db.execute(
        f"""SELECT hl.entry_id, hl.heat_id, hr.rank,
                   COALESCE(hr.is_co,0) AS is_co
              FROM heat_lanes hl
              JOIN heats h ON h.id=hl.heat_id AND h.tournament_id=?
              LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
             WHERE hl.entry_id IN ({ph})""",
        [tournament_id, *ids],
    ) as cur:
        rows = await cur.fetchall()

    first = {i: 0 for i in ids}   # 1着の数
    co = {i: 0 for i in ids}      # CO の数
    by_heat: dict = {}            # heat_id -> [(entry_id, rank)]
    for r in rows:
        eid = r["entry_id"]
        if r["rank"] == 1:
            first[eid] = first.get(eid, 0) + 1
        if r["is_co"]:
            co[eid] = co.get(eid, 0) + 1
        if r["rank"] is not None:
            by_heat.setdefault(r["heat_id"], []).append((eid, r["rank"]))

    # 同率グループ内の直接対決：同じヒートで相手より上位なら勝ち1
    h2h = {i: 0 for i in ids}
    for lst in by_heat.values():
        for ae, ar in lst:
            for be, br in lst:
                if ae != be and ar < br:
                    h2h[ae] = h2h.get(ae, 0) + 1

    return sorted(
        leaders,
        key=lambda s: (-h2h.get(s["entry_id"], 0),
                       -first.get(s["entry_id"], 0),
                       co.get(s["entry_id"], 0)),
    )[0]


async def sweep_entries_for_tournament(db, tournament_id: int) -> set:
    """SWEEP：予選で自分の全レースを1位（○）で終えた entry_id の集合。
    1回でも CO / 2位以下(rank≠1) / 未消化(結果なし) があれば非該当。"""
    async with db.execute(
        """SELECT hl.entry_id, hr.rank, COALESCE(hr.is_co,0) AS is_co
             FROM heat_lanes hl
             JOIN heats h ON h.id=hl.heat_id AND h.tournament_id=?
             LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id""",
        (tournament_id,),
    ) as cur:
        rows = await cur.fetchall()
    scheduled: dict = {}   # entry_id -> 予定レース数
    good: dict = {}        # entry_id -> 1位かつCO無しの数
    for r in rows:
        eid = r["entry_id"]
        scheduled[eid] = scheduled.get(eid, 0) + 1
        if r["rank"] == 1 and not r["is_co"]:
            good[eid] = good.get(eid, 0) + 1
    return {eid for eid, n in scheduled.items()
            if n > 0 and good.get(eid, 0) == n}


def compute_achievements(rh_raw, point_leader_eid, sweep_eids, name_by_entry) -> dict:
    """称号（レーサー名リスト。空＝該当なし）を返す。

      SWEEP      … 予選で全レース1位（sweep_eids をそのまま）
      GRAND SLAM … OVERALL1位 かつ POINT LEADER（予選画面。決勝優勝は未確定→表示側で "Right there!!"）
      SPEED STAR … OVERALL1位・FASTEST LAP1位・TOP SPEED1位 をすべて保持
      SPRINTER   … 全区間（セクション）それぞれで1位
    """
    rh_raw = rh_raw or {}

    def _eids(rec):
        return {eid for (eid, _h) in (rec or {}).get("holders", [])}

    ov = _eids(rh_raw.get("overall"))
    lp = _eids(rh_raw.get("fastest_lap"))
    ts = _eids(rh_raw.get("top_speed"))

    # SPEED STAR：3記録すべて保持（いずれか未計測なら該当なし）
    speed_star = (ov & lp & ts) if (ov and lp and ts) else set()

    # SPRINTER：各セクション（区間）ごとの1位レーサーを表示する。
    #   [{"sector": 区間番号, "names": [1位レーサー名, ...]}, ...]（区間番号昇順）。
    secs = rh_raw.get("sectors") or {}
    sprinter_sectors = []
    for _sno in sorted(secs.keys()):
        rec = secs[_sno] or {}
        _nm = sorted({name_by_entry.get(eid, "?") for (eid, _h) in rec.get("holders", [])})
        if _nm:
            sprinter_sectors.append({"sector": _sno, "names": _nm})

    # GRAND SLAM（予選）：OVERALL1位 かつ POINT LEADER
    grand = ({point_leader_eid}
             if (point_leader_eid is not None and point_leader_eid in ov)
             else set())

    def _names(eids):
        return sorted(name_by_entry.get(e, "?") for e in eids)

    return {
        "sweep":      _names(set(sweep_eids or set())),
        "grand_slam": _names(grand),
        "speed_star": _names(speed_star),
        "sprinter":   sprinter_sectors,
    }


# ── ⚡ M4LAPS RECORD HOLDERS 表示設定（設定ページの app_settings 'm4laps_ach_config'）──
_ACH_KEYS = ("overall", "fastest_lap", "top_speed", "point_leader",
             "sweep", "grand_slam", "speed_star", "sprinter")

# 各項目の既定の表示名（設定で自由に変更可能。未設定時はこれを使う）。
_ACH_DEFAULT_LABELS = {
    "overall":      "OVERALL",
    "fastest_lap":  "FASTEST LAP",
    "top_speed":    "TOP SPEED",
    "point_leader": "POINT LEADER",
    "sweep":        "SWEEP",
    "grand_slam":   "GRAND SLAM",
    "speed_star":   "SPEED STAR",
    "sprinter":     "SPRINTER",
}


async def get_ach_config(db) -> dict:
    """各項目の panel(予選/決勝) / bracket(トーナメント表) / cert(賞状) のON/OFF＋label(表示名)。
    未設定は panel=True（従来どおり全表示）、label は既定名。"""
    import json as _j
    cfg = {k: {"panel": True, "bracket": False, "cert": False,
               "label": _ACH_DEFAULT_LABELS[k]} for k in _ACH_KEYS}
    try:
        async with db.execute(
            "SELECT value FROM app_settings WHERE key='m4laps_ach_config'") as cur:
            row = await cur.fetchone()
        if row and row[0]:
            saved = _j.loads(row[0])
            for k in _ACH_KEYS:
                if isinstance(saved.get(k), dict):
                    cfg[k]["panel"] = bool(saved[k].get("panel", True))
                    cfg[k]["bracket"] = bool(saved[k].get("bracket", False))
                    cfg[k]["cert"] = bool(saved[k].get("cert", False))
                    _lb = saved[k].get("label")
                    if isinstance(_lb, str) and _lb.strip():
                        cfg[k]["label"] = _lb.strip()
    except Exception:
        pass
    return cfg


def labels_from_cfg(cfg: dict) -> dict:
    """get_ach_config の結果から {key: 表示名} を取り出す（テンプレへ渡す用）。"""
    cfg = cfg or {}
    return {k: (cfg.get(k, {}).get("label") or _ACH_DEFAULT_LABELS[k]) for k in _ACH_KEYS}


async def get_ach_labels(db) -> dict:
    """{key: 表示名} を返す簡易ヘルパー（RECORD HOLDERS 見出しの差し替え用）。"""
    return labels_from_cfg(await get_ach_config(db))


def apply_panel_config(record_holders, point_leader, achievements, cfg):
    """設定で panel=False の項目を「予選/決勝」パネルから隠す。
    record_holders の各記録は None に、point_leader は None に、
    achievements の各称号は None に（テンプレは None の行を出さない）。"""
    cfg = cfg or {}

    def _on(k):
        return cfg.get(k, {}).get("panel", True)

    if record_holders:
        if not _on("overall"):
            record_holders["overall"] = None
        if not _on("fastest_lap"):
            record_holders["fastest_lap"] = None
        if not _on("top_speed"):
            record_holders["top_speed"] = None
    if point_leader is not None and not _on("point_leader"):
        point_leader = None
    if achievements:
        for k in ("sweep", "grand_slam", "speed_star", "sprinter"):
            if not _on(k):
                achievements[k] = None
    return record_holders, point_leader, achievements
