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
