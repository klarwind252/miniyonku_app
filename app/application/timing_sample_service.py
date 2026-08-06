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
from app.application.timing_sample_irregular import (
    assign_lane_patterns,
    should_emit,
    should_emit_start,
    describe_pattern,
)

# サンプル送信機能の有効/無効。既定は有効（テスト環境での確認用）。
# 本番では .env に M4LAPS_SAMPLE=0 を入れてボタンごと消す。
SAMPLE_ENABLED = os.environ.get("M4LAPS_SAMPLE", "1") != "0"

# 生成の既定値
TARGET_TOTAL_S = 35.0        # FINISHタイムの目安（秒）
TOTAL_JITTER_S = 5.0         # 上のブレ幅（±）。1台ずつ独立に引く
SECTOR_MAX_S = 2.0           # 1区間の上限（秒）。収まらない構成のときは総合タイムを優先する
DEFAULT_BEAM_GAP_MM = 20.0   # ビーム間隔の仮定値（mm）。t_us_b の打刻に使う
SPEED_RANGE_MS = (4.0, 9.0)  # 通過速度の生成レンジ（m/s）。ミニ四駆の実測に合わせる

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


def split_evenly(total: float, n: int, rnd, spread: float = 0.15,
                 cap: float | None = None) -> list[float]:
    """total を n 個に、ばらつきを付けて分ける（合計は必ず total）。

    cap を指定し、かつ total/n <= cap なら、各値が cap を超えないように整える。
    total/n > cap のときは物理的に無理なので、そのまま均等寄りに分ける。
    """
    if n <= 0:
        return []
    w = [rnd.uniform(1.0 - spread, 1.0 + spread) for _ in range(n)]
    sw = sum(w)
    out = [total * x / sw for x in w]

    if cap is not None and total / n <= cap:
        # capを超えたぶんを、余裕のある区間へ配り直す（最大数回で収束する）
        for _ in range(20):
            over = [i for i, v in enumerate(out) if v > cap]
            if not over:
                break
            excess = sum(out[i] - cap for i in over)
            for i in over:
                out[i] = cap
            room = [i for i in range(n) if out[i] < cap]
            if not room:
                break
            free = sum(cap - out[i] for i in room)
            for i in room:
                out[i] += excess * (cap - out[i]) / free
    return out


def estimate_sector_s(layout_elems: list[LayoutElement], target_laps: int,
                      total_s: float = TARGET_TOTAL_S) -> tuple[int, float]:
    """(1周あたりの区間数, 1区間の平均タイム見込み) を返す。

    区間数が少ないと、総合タイムを35秒前後にしたときに1区間が長くなる。
    画面で注意を出すために使う。
    """
    course = build_course(layout_elems)
    n_sections = max(1, len(course.gates))
    return n_sections, total_s / max(1, target_laps) / n_sections


def build_sample_events(
    layout_elems: list[LayoutElement],
    target_laps: int,
    lanes: int = LANES,
    mode: str = "free",
    base_t_us: int | None = None,
    total_s: float = TARGET_TOTAL_S,
    beam_gap_by_node: dict | None = None,
    seed: int | None = None,
    irregular: bool = False,
) -> tuple[int | None, list[dict]]:
    """1レース分のサンプル通過イベントを組み立てる。

    戻り値: (green_t_us または None, events)
      events は timing_api の受信口と同じ形:
      {device_id, src, src_boot_id, seq, lane, t_us, t_us_b, quality}

    mode: "race"（F1式・緑ランプあり）/ "free"（走行式・緑なし）

    タイムの決め方:
      - 各マシンのFINISHタイムを total_s ± TOTAL_JITTER_S から**独立に**引く。
        ⚠ 速さの係数を1つ引いて全周に掛ける作り方だと、レーン間の差が
           そのまま順位になり、見た目の並びが偏りやすい。1台ずつ独立に
           目標タイムを引けば、順位は毎回まんべんなく入れ替わる。
      - その目標タイムを周に分け、さらに周を区間へ分ける。
        区間は SECTOR_MAX_S 以内に収める（収まらない構成なら総合タイムを優先）。
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
    n_sections = n_gates          # 1周の区間数（S/G→SQ1…SQn→S/G）
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

    # イレギュラーパターン（DNS/CO/スキップ）を各スタートレーンに割り当てる。
    # 「無」のときは None のまま＝従来どおり全マシン完走。
    lane_patterns = None
    if irregular:
        gate_names = [g.kind for g in gates]  # ["SG","SQ0",...] 形式（ログ用）
        lane_patterns = assign_lane_patterns(
            n_lanes=lanes,
            total_laps=target_laps,
            n_gates=n_gates,
            rnd=rnd,
        )
        for sl, pat in enumerate(lane_patterns, 1):
            print(f"  [irregular] start_lane={sl}: "
                  f"{describe_pattern(pat, gate_names)}")

    for start_lane in range(1, lanes + 1):
        pat = lane_patterns[start_lane - 1] if lane_patterns else None

        # DNS：一度も通過しない＝このマシンのイベントを一切生成しない
        if pat and not should_emit_start(pat):
            continue

        # このマシンのFINISHタイム（1台ずつ独立に引く＝順位は毎回ランダム）
        target = rnd.uniform(total_s - TOTAL_JITTER_S, total_s + TOTAL_JITTER_S)

        # スタート（S/G 0回目の通過）
        if mode == "race":
            # 緑からの反応＋助走。FINISHは緑からの経過なので、この分を差し引く
            react = rnd.uniform(0.10, 0.45)
            t_cross0 = base + int(react * 1_000_000)
            run_s = max(target - react, 1.0)
        else:
            # 走行式は思い思いのタイミングでS/Gを通る（FINISHはS/G間の合計）
            t_cross0 = base + int(rnd.uniform(0.0, 1.5) * 1_000_000)
            run_s = target
        _emit(sg, expected_sg_lane(start_lane, 0, rot_total, lanes), t_cross0)

        lap_times = split_evenly(run_s, target_laps, rnd, spread=0.06)
        t_lap_start = t_cross0
        for lap in range(1, target_laps + 1):
            lap_us = int(lap_times[lap - 1] * 1_000_000)
            secs = split_evenly(lap_times[lap - 1], n_sections, rnd,
                                spread=0.18, cap=SECTOR_MAX_S)

            # 周の途中にあるセクションゲート（区間タイムを積み上げて打刻）
            # ⚠ 時刻(acc)は欠落があっても必ず積み上げる。欠落するのは「打刻を
            #    出すか」だけで、時間そのものは進む（後ろのゲートの時刻がズレない）。
            acc = 0.0
            for i, g in enumerate(gates[1:], start=1):
                acc += secs[i - 1]
                # gate_idx: 中間ゲートは 1.. （0はS/G周回完了）
                if pat and not should_emit(pat, lap, i):
                    continue
                _emit(
                    g,
                    expected_lane(start_lane, lap, g.rot_to_gate, rot_total, lanes),
                    t_lap_start + int(acc * 1_000_000),
                )

            # 周回完了（S/G lap回目の通過）。端数はここで吸収する
            t_lap_start += lap_us
            # gate_idx=0 が「周回完了S/G」。COで周を締められない場合は出さない。
            if not (pat and not should_emit(pat, lap, 0)):
                _emit(sg, expected_sg_lane(start_lane, lap, rot_total, lanes),
                      t_lap_start)

    events.sort(key=lambda e: e["t_us"])
    return green_t_us, events
