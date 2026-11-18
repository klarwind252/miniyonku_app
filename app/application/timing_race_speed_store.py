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
# ★20261118 相対フィルタの基準を「ラップ平均（LAP Av.）の上側の統計」に変更。
#   ・ラップ平均はラップタイム÷距離で出すためビーム揺れの影響を受けない。
#   ・遅い方向の外れ（コースアウト・減速・S/G打刻抜けで周が長くなる）はよくあるが、
#     速い方向の外れは物理的にほぼ起きない。中央値は遅い車に引っ張られて速い車の正常値まで
#     切り得るため、基準は上側＝速い側で取る：
#       基準 = max(このレースの最速ラップ平均, 同コース過去レースのラップ平均 P95)
#     （同コース＝同じ layout_id かつ同じ合計距離。過去データが COURSE_MIN_N 件以上のときだけ使う）
#   ・通過速度 ≤ 基準 × REL_K_LAP を採用。
#   実データ（arcadebase 4524通過・20261118集計）：通過速度/そのレース最速ラップ平均 は
#     中央 1.19・P90 1.59（正常走行の最大はおよそ1.5〜1.6）。2超の裾は「最速ラップ平均が
#     2m/s台＝コースアウト・停止のみのレース」で、基準が低すぎることが原因＝上記の保険で吸収。
#   → REL_K_LAP=2.0（正常最大1.52に約3割の余裕）。現状のクラスで上限 約44km/h。
#   距離未設定でラップ平均が無いレースだけ、従来どおり通過速度の中央値 × REL_K を基準にする。
REL_K_LAP = 2.0
COURSE_PCT = 0.95
COURSE_MIN_N = 20
LAP_SANE_K = 1.5       # これを超える「速すぎる周」は S/G 余分打刻とみなし ref に使わない
NODE_MIN_N = 3          # ノード中央値を使う最小サンプル数（このレース内）
# 係数 pass_k / rel_k / node_med_k は timing_speed_calib（日次の自動校正）が管理する。


async def has_stored_speeds(db, race_id: int) -> bool:
    async with db.execute(
        "SELECT 1 FROM timing_race_speeds WHERE race_id=? LIMIT 1", (race_id,)
    ) as cur:
        return await cur.fetchone() is not None


async def delete_speeds(db, race_id: int) -> None:
    await db.execute("DELETE FROM timing_race_speeds WHERE race_id=?", (race_id,))


async def compute_and_store_speeds(db, race_id: int, *, params: dict | None = None,
                                   sink: dict | None = None) -> int:
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
        if sink is None:
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
    # ★20261118 レース生成時に固定したコース構成（layout_json）があれば、そのノードと
    #   センサー幅を使う（後でレイアウトや台帳を変えても、このレースの速度は変わらない）。
    #   固定値にセンサー幅が無いノードだけ台帳の値で補う（旧スナップショット互換）。
    snap_elems = None
    try:
        async with db.execute(
            "SELECT layout_json FROM timing_races WHERE id=?", (race_id,)
        ) as _c:
            _lr = await _c.fetchone()
        if _lr is not None and _lr[0]:
            import json as _json
            snap_elems = _json.loads(_lr[0])
    except Exception:
        snap_elems = None
    if snap_elems:
        _snap_gap = {e.get("node_id"): e.get("beam_gap_mm") for e in snap_elems
                     if e.get("node_id") is not None}
        _need = [n for n, g in _snap_gap.items() if not g]
        if _need:
            try:
                _ph = ",".join("?" * len(_need))
                async with db.execute(
                    f"SELECT node_id, sensor_width_mm FROM timing_devices WHERE node_id IN ({_ph})", _need
                ) as _c:
                    for _d in await _c.fetchall():
                        if _d["sensor_width_mm"]:
                            _snap_gap[_d["node_id"]] = _d["sensor_width_mm"]
            except Exception:
                pass
        gap_by_node = {n: g for n, g in _snap_gap.items() if g}
    # ★20261118 コース設定の固定上限は使わない（統計フィルタ＋安全上限100km/hのみ）。
    #   設定値に依存しないので、後から設定を変えても過去レースの速度は変わらない。
    max_speed_ms = None

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

        if snap_elems:
            layout_elems = [LayoutElement(kind=e["kind"], node_id=e.get("node_id")) for e in snap_elems]
        else:
            elems = await lrepo.get_elements(layout_id)
            layout_elems = [LayoutElement(kind=e["kind"], node_id=e["node_id"]) for e in elems]
        if layout_elems:
            try:
                pass_index = spd.build_pass_index(layout_elems, evs)
            except Exception:
                pass_index = {}

    def _pass_speed(lane, lap, idx):
        """(速度m/s or None, ノードID)。安全上限(100km/h)のみ適用。"""
        if ev_for:
            rec = ev_by_id.get(ev_for.get((lane, lap, idx)))
        else:
            # レース結果がイベントidを持たない旧データだけ旧方式で引く
            rec = pass_index.get((lane, lap, idx))
        if not rec:
            return None, None
        return spd.pass_speed_ms(rec[0], rec[1], gap_by_node.get(rec[2]), max_speed_ms), rec[2]

    # --- 1st pass: レース全体のビーム通過速度を集める（安全上限のみ適用）---
    beam_samples: dict = {}          # (lane, lap, idx) -> speed[m/s]
    beam_node: dict = {}             # (lane, lap, idx) -> node_id
    for m in result.ranking():
        lane = m.start_lane
        for lap in m.laps:
            if lap.lap >= 2:
                sg, nd = _pass_speed(lane, lap.lap, 0)
                if sg is not None:
                    beam_samples[(lane, lap.lap, 0)] = sg
                    beam_node[(lane, lap.lap, 0)] = nd
            for idx in range(len(lap.sectors)):
                sno = idx + 1
                sp, nd = _pass_speed(lane, lap.lap, sno)
                if sp is not None:
                    beam_samples[(lane, lap.lap, sno)] = sp
                    beam_node[(lane, lap.lap, sno)] = nd

    # --- 基準速度 ref（速い側の統計。遅い車に引っ張られない）---
    #   ref_course = 同コース（同 layout_id・同合計距離）の過去レースのラップ平均 P95（COURSE_MIN_N 件以上）
    #   このレースのラップ平均のうち「ありえる範囲」のものの最大を ref_race とする。
    #     ありえる範囲 = ref_course × LAP_SANE_K 以下（履歴が無ければ このレースのラップ平均中央値 × LAP_SANE_K）
    #     ※S/G の余分な打刻で「速すぎる周」が1本混ざると、最大値が化ける（実例 race378: 20m/s）ため。
    #   ref = max(ref_race, ref_course)
    #   ※通過速度の中央値を基準に使うのは廃止（全通過が揺れのレースで基準ごと膨らむ＝実例 race356）。
    _lap_refs = []
    for m in result.ranking():
        for lap in m.laps:
            _la = spd.lap_avg_speed_ms(lap.lap_time_us, per_lap_len_m, None)
            if _la is not None:
                _lap_refs.append(_la)
    _vals = list(beam_samples.values())
    _ref = None
    if _lap_refs:
        _ref_course = None
        try:
            async with db.execute(
                "SELECT s.value FROM timing_race_speeds s JOIN timing_races r ON r.id = s.race_id "
                "WHERE s.metric = 'lap_avg' AND s.race_id <> ? AND r.layout_id IS ? "
                "AND ABS(COALESCE(r.course_len_m, 0) - COALESCE(?, 0)) < 0.05",
                (race_id, layout_id, total_len_m),
            ) as _c:
                _hist = sorted(r[0] for r in await _c.fetchall() if r[0] is not None)
            if len(_hist) >= COURSE_MIN_N:
                _ref_course = _hist[min(len(_hist) - 1, int(round(COURSE_PCT * (len(_hist) - 1))))]
        except Exception:
            _ref_course = None
        _sane = (_ref_course if _ref_course else statistics.median(_lap_refs)) * LAP_SANE_K
        _ok = [x for x in _lap_refs if x <= _sane]
        _ref = max(_ok) if _ok else None
        if _ref_course:
            _ref = max(_ref or 0, _ref_course)

    # --- フィルタの係数（★20261118 学習調整：timing_speed_calib）---
    #   レースごとに「使った係数」を timing_races.speed_params に記録し、再計算でも同じ係数を使う
    #   （日次の自動調整で係数が変わっても、過去レースの速度は変わらない）。
    #   params を明示で渡されたとき（校正の試算・強制再計算）だけそれを使う。
    from app.application import timing_speed_calib as _calib
    if params is None:
        params = await _calib.params_for_race(db, race_id)
    pass_k = float(params.get("pass_k", _calib.DEFAULTS["pass_k"]))
    rel_k = float(params.get("rel_k", _calib.DEFAULTS["rel_k"]))
    node_med_k = float(params.get("node_med_k", _calib.DEFAULTS["node_med_k"]))
    node_rel_k = float(params.get("node_rel_k", _calib.DEFAULTS["node_rel_k"]))
    node_base = params.get("node_base") or {}

    # ノード別の中央値（このレース内）。外れはノード（機器）ごとに出るため、ノード単位で見る。
    _by_node: dict = {}
    for k, v in beam_samples.items():
        _by_node.setdefault(beam_node.get(k), []).append(v)
    node_med = {n: statistics.median(vs) for n, vs in _by_node.items() if len(vs) >= NODE_MIN_N}
    # ノード健全性：ノード中央値/ref が、そのノードの基準線 node_base × node_rel_k を超えたら
    #   そのノード全体がずれている（ビーム揺れ・センサー幅の誤設定など）→ このレースでは不採用。
    #   基準線の無いノード（新設など）は絶対線 node_med_k を使う。
    bad_nodes = set()
    if _ref:
        for n, md in node_med.items():
            _b = node_base.get(str(n))
            _line = (_b * node_rel_k) if _b else node_med_k
            if md > _ref * _line:
                bad_nodes.add(n)

    if _ref:
        rel_cap = _ref * rel_k
    else:
        rel_cap = (statistics.median(_vals) * REL_K) if _vals else None

    if sink is not None:
        # 校正用：生の材料だけ返して保存しない
        sink.update({"race_id": race_id, "layout_id": layout_id, "ref": _ref,
                     "samples": [(beam_node.get(k), k[1], k[2], v) for k, v in beam_samples.items()],
                     "lap_refs": _lap_refs})
        return 0

    _accepted = set()
    for k, v in beam_samples.items():
        nd = beam_node.get(k)
        if nd in bad_nodes:
            continue
        if rel_cap is not None and v > rel_cap:
            continue
        md = node_med.get(nd)
        if md is not None and v > md * pass_k:
            continue
        _accepted.add(k)

    def _accept_key(k) -> bool:
        return k in _accepted

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
                if sg is not None and _accept_key((lane, lap.lap, 0)):
                    speeds_all.append(sg)
                    rows.append((lane, "sector_ms", lap.lap, 0, sg))

            # 各区間(1..7)の通過速度
            for idx in range(len(lap.sectors)):
                sno = idx + 1
                sp = beam_samples.get((lane, lap.lap, sno))
                if sp is not None and _accept_key((lane, lap.lap, sno)):
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
    await _calib.remember_params(db, race_id, params)
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
