"""テスト用サンプルの「イレギュラーパターン」生成（application層）。

H群パターン台帳（docs/24 §24.61）に基づき、CO・DNS・スキップ復帰などの
イレギュラーなラップデータを意図的に生成するための補助モジュール。

timing_sample_service.build_sample_events() からのみ呼ばれる想定。
このモジュールは「各スタートレーンにどのパターンを割り当てるか」を決め、
「ある周・あるゲートの打刻を出してよいか（should_emit）」を判定するだけ。
実際の打刻（t_us生成・event辞書組み立て）は呼び出し側が持つ。

対応パターン（H群 §24.61 の 0/1/2/4）:
  finish  … 完走（欠落なし）
  dns     … 0 DNS（一度も通過しない＝スタートすらしない）
  co      … 1,2 CO（ある周・あるゲートで停止、以降ゼロ）
  skip    … 4 スキップ復帰（中間の1ゲートだけ欠落・その先は継続）

⚠ 未対応（順序が壊れる・回数が増える系。追加フェーズで実装）:
  5 戻り復帰（同ゲート重複）／6,7 他レーン乱入（順序汚染・後方侵入）
  これらは「別マシンの打刻をこのマシンのレーンに差し込む」処理になり、
  build_sample_events の単純な per-lane 生成ループでは表現しきれないため、
  乱入生成専用のフックを別途設ける必要がある（H-4/H-5と連動）。
"""

from __future__ import annotations

import random

# ---------------------------------------------------------------------------
# パターンの出現重み（合計は内部で正規化するので厳密に1.0でなくてよい）
# docs/24 §24.67 の頻度感を素朴に反映（テスト用なので厳密でなくてよい）。
# ---------------------------------------------------------------------------
_PATTERN_WEIGHTS: list[tuple[str, float]] = [
    ("finish",   0.30),  # 完走
    ("dns",      0.12),  # 0 DNS
    ("co_early", 0.25),  # 1,2 早期CO（1周目途中）
    ("co_mid",   0.18),  # 1,2 中盤CO
    ("co_late",  0.08),  # 1,2 終盤CO（最終周途中）
    ("skip",     0.07),  # 4 スキップ復帰
]


def assign_lane_patterns(
    n_lanes: int,
    total_laps: int,
    n_gates: int,
    rnd: random.Random,
) -> list[dict]:
    """各スタートレーン（1..n_lanes）のパターンを事前決定して返す。

    Args:
        n_lanes:    レーン数
        total_laps: 周回数
        n_gates:    1周あたりのゲート数（S/G含む。gates列の長さ）
        rnd:        呼び出し側と共有する random.Random（再現性のため）

    Returns:
        list[dict] 長さ n_lanes。index 0 が start_lane=1 に対応。

    ⚠ 全員 finish になると「イレギュラー有」を選んだ意味が薄れるため、
       全員 finish のときは最低1台をCOに差し替える。
    """
    # 中間ゲートは gate_idx = 1..(n_gates-1)。CO/スキップ位置はここから選ぶ。
    max_gate_idx = max(1, n_gates - 1)

    patterns: list[dict] = []
    for _ in range(n_lanes):
        patterns.append(_roll_one(total_laps, max_gate_idx, n_gates, rnd))

    # 全員完走の保険：最低1台はイレギュラーにする
    if all(p["type"] == "finish" for p in patterns):
        idx = rnd.randrange(n_lanes)
        stop_gate = rnd.randint(1, max_gate_idx)
        patterns[idx] = {"type": "co", "stop_lap": min(2, total_laps),
                         "stop_gate": stop_gate}

    return patterns


def _roll_one(total_laps: int, max_gate_idx: int, n_gates: int,
              rnd: random.Random) -> dict:
    """重み付き抽選で1台ぶんのパターンを決める。"""
    total_w = sum(w for _, w in _PATTERN_WEIGHTS)
    r = rnd.random() * total_w
    cumul = 0.0
    name = "finish"
    for nm, w in _PATTERN_WEIGHTS:
        cumul += w
        if r < cumul:
            name = nm
            break

    if name == "finish":
        return {"type": "finish"}

    if name == "dns":
        return {"type": "dns"}

    if name == "co_early":
        return {"type": "co", "stop_lap": 1,
                "stop_gate": rnd.randint(1, max_gate_idx)}

    if name == "co_mid":
        return {"type": "co", "stop_lap": min(2, total_laps),
                "stop_gate": rnd.randint(1, max_gate_idx)}

    if name == "co_late":
        return {"type": "co", "stop_lap": total_laps,
                "stop_gate": rnd.randint(1, max_gate_idx)}

    if name == "skip":
        # スキップは中間ゲートのみ（最終ゲートを飛ばすとCOと区別つかないので
        # gate_idx は 1..(n_gates-2) に限定）。ゲートが少ないときはCOに退避。
        if n_gates - 2 < 1:
            return {"type": "co", "stop_lap": min(2, total_laps),
                    "stop_gate": max(1, max_gate_idx)}
        return {"type": "skip",
                "skip_lap": rnd.randint(1, total_laps),
                "skip_gate": rnd.randint(1, n_gates - 2)}

    return {"type": "finish"}


def should_emit_start(pattern: dict) -> bool:
    """スタート打刻（S/G passing=0）を出すかどうか。

    DNS だけがスタート打刻すら出さない。それ以外は必ず起点を打つ。
    """
    return pattern["type"] != "dns"


def should_emit(pattern: dict, lap: int, gate_idx: int) -> bool:
    """lap 周目・gate_idx のゲート打刻を出すかどうか。

    Args:
        pattern:  assign_lane_patterns() が返した1要素
        lap:      1-indexed の周回数
        gate_idx: 1周内のゲートインデックス
                  0 = S/G(周回完了打刻) / 1.. = 中間ゲート(SQ)
                  ※スタート打刻(passing=0)はここでは扱わない→should_emit_start

    Returns:
        True = 打刻を出す / False = 欠落させる
    """
    ptype = pattern["type"]

    if ptype == "dns":
        return False

    if ptype == "finish":
        return True

    if ptype == "co":
        stop_lap = pattern["stop_lap"]
        stop_gate = pattern["stop_gate"]
        # stop_lap より後の周は全欠落
        if lap > stop_lap:
            return False
        # stop_lap 周内は stop_gate 以降を欠落
        if lap == stop_lap:
            # gate_idx=0 は「周回完了S/G」＝その周を締める打刻。
            # 中間ゲートで止まったなら締めS/Gも来ない。
            if gate_idx == 0:
                return False  # 周を完了できずに停止
            if gate_idx >= stop_gate:
                return False
        return True

    if ptype == "skip":
        # 指定の1ゲートだけ欠落。前後は出す。
        if lap == pattern["skip_lap"] and gate_idx == pattern["skip_gate"]:
            return False
        return True

    return True  # 未知は安全側で出す


def describe_pattern(pattern: dict, gate_names: list[str] | None = None) -> str:
    """ログ用の人間可読な説明文。"""
    ptype = pattern["type"]

    def gname(idx: int) -> str:
        if gate_names and 0 <= idx < len(gate_names):
            return gate_names[idx]
        return f"gate[{idx}]"

    if ptype == "finish":
        return "完走"
    if ptype == "dns":
        return "DNS（0 スタートせず）"
    if ptype == "co":
        return ("CO（{}周目 {} 手前で停止）".format(
            pattern["stop_lap"], gname(pattern["stop_gate"])))
    if ptype == "skip":
        return ("スキップ復帰4（{}周目 {} だけ欠落）".format(
            pattern["skip_lap"], gname(pattern["skip_gate"])))
    return "不明({})".format(ptype)
