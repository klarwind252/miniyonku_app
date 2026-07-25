"""M4LAPS 速度の算出

設定値が入っていれば実測値を、無ければ None を返す。
画面側は None のとき「—」を表示する（ダミー値でごまかさない）。

必要な設定:
  通過速度   … ゲートごとの beam_gap_mm（2本のビームの間隔・mm）
                timing_layout_elements.beam_gap_mm
  ラップ平均 … コース1周の距離 lap_length_m（m）
                timing_layouts.lap_length_m

計算式:
  通過速度[m/s]   = 間隔mm / 1000 / ((t_us_b - t_us) / 1e6)
  ラップ平均[m/s] = コース全長m / ラップタイム秒
"""

from __future__ import annotations

from app.domain.rotation import LANES, build_course, identify_start_lane

# 現実的にありえない値を弾くための範囲（m/s）
# ミニ四駆の実速度域はおよそ 3〜15 m/s（11〜54 km/h）。
# ビームのチャタリングやノイズで極端な値が出た場合に採用しないための保険。
MIN_SPEED_MS = 0.5
MAX_SPEED_MS = 30.0


def pass_speed_ms(t_us: int | None, t_us_b: int | None,
                  beam_gap_mm: float | None) -> float | None:
    """ゲート通過速度[m/s]を算出する（純粋関数）。

    2本のビームを通過する時間差と、その間隔から求める。
    設定や打刻が欠けていれば None（＝画面では「—」表示）。
    """
    if t_us is None or t_us_b is None or not beam_gap_mm:
        return None
    dt_us = t_us_b - t_us
    if dt_us <= 0:
        return None                      # 逆順・同時刻は不正
    v = (beam_gap_mm / 1000.0) / (dt_us / 1e6)
    if not (MIN_SPEED_MS <= v <= MAX_SPEED_MS):
        return None                      # 明らかな異常値は採用しない
    return round(v, 2)


def lap_avg_speed_ms(lap_time_us: int | None,
                     lap_length_m: float | None) -> float | None:
    """1周の平均速度[m/s]を算出する（純粋関数）。

    コース全長が未設定なら None。
    """
    if not lap_time_us or not lap_length_m:
        return None
    v = lap_length_m / (lap_time_us / 1e6)
    if not (MIN_SPEED_MS <= v <= MAX_SPEED_MS):
        return None
    return round(v, 2)


def total_avg_speed_ms(total_time_us: int | None,
                       lap_length_m: float | None,
                       laps: int | None) -> float | None:
    """レース全体の平均速度[m/s]を算出する（純粋関数）。

    式: (1周の距離 × 周回数) ÷ TOTALタイム
    lap_avg が1周ぶんなのに対し、これは全周を通した平均。
    コース全長・周回数・タイムのどれかが無ければ None。
    """
    if not total_time_us or not lap_length_m or not laps:
        return None
    v = (lap_length_m * laps) / (total_time_us / 1e6)
    if not (MIN_SPEED_MS <= v <= MAX_SPEED_MS):
        return None
    return round(v, 2)


def build_pass_index(layout_elems, events) -> dict:
    """通過イベントを「どのマシンの・何周目の・どの区間か」で引ける形にする。

    ⚠ イベントに入っている lane は **物理レーン**。レーンチェンジがあると
       周回ごとにずれるため、そのままでは画面の start_lane と噛み合わない。
       race_builder と同じ手順（通過回数から逆算）でスタートレーンへ直す。

    layout_elems: LayoutElement の列（通過順）
    events      : {lane, src(=node_id), t_us, t_us_b} を持つ列（順不同）

    戻り値: {(start_lane, lap, idx): (t_us, t_us_b, node_id)}
      idx 0    … その周の起点となるS/G通過
      idx 1..G … その周に通ったセクションゲート（通過順）
      idx G+1  … その周を終えるS/G通過（次の周の idx 0 と同じ打刻）
    """
    course = build_course(layout_elems)
    gates = list(course.gates)
    sg_list = [g for g in gates if g.kind == "SG"]
    if len(sg_list) != 1 or not gates:
        return {}
    sg = sg_list[0]
    n_gates = len(gates)
    n_sections = n_gates - 1
    rot_total = course.rot_total

    # node_id → ゲート、および「S/Gを0とした通過順の位置」
    by_node = {g.node_id: g for g in gates if g.node_id is not None}
    pos_of = {g.index: (g.index - sg.index) % n_gates for g in gates}

    # (物理レーン, ゲート) ごとに時刻順へ並べ、何回目の通過かを数える
    grouped: dict = {}
    for ev in events:
        g = by_node.get(ev["src"])
        if g is None:
            continue
        grouped.setdefault((ev["lane"], g.index), []).append((ev["t_us"], ev, g))
    for lst in grouped.values():
        lst.sort(key=lambda x: x[0])

    out: dict = {}
    for (phys_lane, gate_index), lst in grouped.items():
        for occurrence, (t_us, ev, g) in enumerate(lst):
            if g.kind == "SG":
                passing = occurrence      # 0=スタート打刻 / k=k周目の完了
                start_lane = (phys_lane - 1 - passing * rot_total) % LANES + 1
                rec = (ev["t_us"], ev.get("t_us_b"), g.node_id)
                out[(start_lane, passing + 1, 0)] = rec        # 次の周の起点
                if passing >= 1:
                    out[(start_lane, passing, n_sections + 1)] = rec   # その周の終点
            else:
                lap = occurrence + 1
                start_lane = identify_start_lane(
                    phys_lane, lap, g.rot_to_gate, rot_total, LANES
                )
                out[(start_lane, lap, pos_of[g.index])] = (
                    ev["t_us"], ev.get("t_us_b"), g.node_id
                )
    return out


async def load_speed_config(db, layout_id: int | None) -> dict:
    """レイアウトから速度算出に必要な設定を読む。

    戻り値: {"lap_length_m": float|None,
             "beam_gap_by_node": {node_id: mm},
             "beam_gap_by_pos": {position: mm}}
    ゲートは node_id で引けるが、レイアウト上の位置でも引けるようにしておく。
    """
    out = {"lap_length_m": None, "beam_gap_by_node": {}, "beam_gap_by_pos": {}}
    if layout_id is None:
        return out

    async with db.execute(
        "SELECT lap_length_m FROM timing_layouts WHERE id = ?", (layout_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is not None:
        keys = row.keys() if hasattr(row, "keys") else []
        out["lap_length_m"] = row["lap_length_m"] if "lap_length_m" in keys else None

    async with db.execute(
        "SELECT position, node_id, beam_gap_mm FROM timing_layout_elements "
        "WHERE layout_id = ? ORDER BY position",
        (layout_id,),
    ) as cur:
        rows = await cur.fetchall()
    for r in rows:
        gap = r["beam_gap_mm"]
        if gap:
            if r["node_id"] is not None:
                out["beam_gap_by_node"][r["node_id"]] = gap
            out["beam_gap_by_pos"][r["position"]] = gap
    return out


def is_configured(cfg: dict) -> dict:
    """どの指標が実測可能かを返す（画面の注記に使う）。"""
    return {
        "pass_speed": bool(cfg.get("beam_gap_by_node")),
        "lap_avg": bool(cfg.get("lap_length_m")),
    }
