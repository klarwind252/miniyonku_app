"""テスト用サンプル計測データの生成（application層）。

実機のラップタイマー（GW）が繋がっていないテスト環境で、
「計測結果」「ベスト集計」画面の表示を確認するためのダミー通過イベントを作る。

生成の考え方:
  - レイアウト（コースの地図）をそのまま使い、rotation の順方向計算
    （expected_lane / expected_sg_lane）で「そのマシンが本来通るレーン」を出す。
    race_builder の同定はこの逆関数なので、生成 → 同定 が必ず一致する
    （＝実機と同じ手順で読み解ける、筋の通ったデータになる）。
  - S/G上流スタートなので、S/Gの通過は 周回数+1 回（0回目＝スタート打刻）。
  - レース(F1式)は green_t_us あり、フリー(走行式)は green_t_us なし。

DBには触らない。呼び出し側（timing_api）が create_race → insert_event する。

⚠ 本番環境で使うと本物の記録に混ざる。環境変数 M4LAPS_SAMPLE=0 で機能ごと隠せる。
"""

from __future__ import annotations

import os
import random
import time

from app.domain.rotation import (
    LANES,
    LayoutElement,
    build_course,
    expected_lane,
    expected_sg_lane,
)

# サンプル送信機能の有効/無効。既定は有効（テスト環境での確認用）。
# 本番では .env に M4LAPS_SAMPLE=0 を入れてボタンごと消す。
SAMPLE_ENABLED = os.environ.get("M4LAPS_SAMPLE", "1") != "0"

# 生成の既定値
DEFAULT_LAP_MS = 7000        # 1周の目安（ms）。ミニ四駆3レーンの実測レンジに合わせた
DEFAULT_BEAM_GAP_MM = 20.0   # ビーム間隔の仮定値（mm）。t_us_b の打刻に使う
SPEED_RANGE_MS = (5.0, 9.0)  # 通過速度の生成レンジ（m/s）

MAX_LAPS = 20
MAX_RACES = 20


def _gate_order(course):
    """S/Gを先頭にした通過順のゲート列を返す。

    レイアウトがS/G始まりでない場合でも、S/Gから1周ぶんの並びになるよう回す。
    """
    gates = list(course.gates)
    sg_pos = None
    for i, g in enumerate(gates):
        if g.kind == "SG":
            sg_pos = i
            break
    if sg_pos is None:
        raise ValueError("レイアウトにS/Gがありません")
    return gates[sg_pos:] + gates[:sg_pos]


def build_sample_events(
    layout_elems: list[LayoutElement],
    target_laps: int,
    lanes: int = LANES,
    mode: str = "free",
    base_t_us: int | None = None,
    lap_ms: int = DEFAULT_LAP_MS,
    beam_gap_by_node: dict | None = None,
    seed: int | None = None,
) -> tuple[int | None, list[dict]]:
    """1レース分のサンプル通過イベントを組み立てる。

    戻り値: (green_t_us または None, events)
      events は timing_api の受信口と同じ形:
      {device_id, src, src_boot_id, seq, lane, t_us, t_us_b, quality}

    mode: "race"（F1式・緑ランプあり）/ "free"（走行式・緑なし）
    """
    rnd = random.Random(seed)
    course = build_course(layout_elems)
    gates = _gate_order(course)

    if not gates:
        raise ValueError("レイアウトにゲートがありません")
    if any(g.node_id is None for g in gates):
        raise ValueError("機器が未割当のゲートがあります（レイアウトを確定してください）")
    if not (1 <= lanes <= LANES):
        raise ValueError(f"レーン数は1〜{LANES}です")

    n_gates = len(gates)
    sg = gates[0]
    rot_total = course.rot_total
    gaps = beam_gap_by_node or {}

    base = base_t_us if base_t_us is not None else int(time.time() * 1_000_000)
    green_t_us = base if mode == "race" else None

    # 冪等キー UNIQUE(device_id, src, src_boot_id, seq) はレースIDを含まない。
    # 連続生成でぶつからないよう、boot_id はレースごとに引き直す。
    boot_id = rnd.randrange(1, 2**31 - 1)
    seq_by_node: dict[int, int] = {}
    events: list[dict] = []

    def _emit(gate, lane: int, t_us: int):
        node_id = int(gate.node_id)
        seq_by_node[node_id] = seq_by_node.get(node_id, 0) + 1
        v = rnd.uniform(*SPEED_RANGE_MS)
        gap_mm = gaps.get(node_id) or DEFAULT_BEAM_GAP_MM
        dt_us = int((gap_mm / 1000.0) / v * 1_000_000)
        events.append({
            "device_id": f"SIM-{node_id:02d}",
            "src": node_id,
            "src_boot_id": boot_id,
            "seq": seq_by_node[node_id],
            "lane": lane,
            "t_us": t_us,
            "t_us_b": t_us + max(dt_us, 1),
            "quality": 0,
        })

    for start_lane in range(1, lanes + 1):
        # マシンごとの実力差（速い子・遅い子）
        skill = rnd.uniform(0.93, 1.10)

        # スタート（S/G 0回目の通過）
        if mode == "race":
            # 緑からの反応＋助走
            t_cross0 = base + int(rnd.uniform(0.10, 0.45) * 1_000_000)
        else:
            # 走行式は思い思いのタイミングでS/Gを通る
            t_cross0 = base + int(rnd.uniform(0.0, 1.5) * 1_000_000)
        _emit(sg, expected_sg_lane(start_lane, 0, rot_total, lanes), t_cross0)

        t_lap_start = t_cross0
        for lap in range(1, target_laps + 1):
            lap_us = int(lap_ms * 1000 * skill * rnd.uniform(0.97, 1.03))

            # 周回の途中にあるセクションゲート（S/Gからの位置で等分＋ゆらぎ）
            for i, g in enumerate(gates[1:], start=1):
                frac = i / n_gates + rnd.uniform(-0.03, 0.03)
                frac = min(max(frac, 0.05), 0.95)
                _emit(
                    g,
                    expected_lane(start_lane, lap, g.rot_to_gate, rot_total, lanes),
                    t_lap_start + int(lap_us * frac),
                )

            # 周回完了（S/G lap回目の通過）
            t_lap_start += lap_us
            _emit(sg, expected_sg_lane(start_lane, lap, rot_total, lanes), t_lap_start)

    events.sort(key=lambda e: e["t_us"])
    return green_t_us, events
