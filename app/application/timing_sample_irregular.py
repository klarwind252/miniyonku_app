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
# docs/24 §24.67 の頻度感に合わせる:
#   - 1レーン完走率 4〜7割 → finish を中央値の 55% に
#   - 3レーン全員完走 12〜22% → 0.55^3 ≒ 16.6% で範囲内
#   - DNS（スタートすらしない）は稀 → 3%
#   - CO は早期ほど多く、終盤ほど少ない実感に沿わせる
# ---------------------------------------------------------------------------
_PATTERN_WEIGHTS: list[tuple[str, float]] = [
    ("finish",   0.55),  # 完走（1レーン完走率の中央値）
    ("dns",      0.03),  # 0 DNS（スタート失敗は稀）
    ("co_early", 0.18),  # 1,2 早期CO（1周目途中）
    ("co_mid",   0.13),  # 1,2 中盤CO
    ("co_late",  0.05),  # 1,2 終盤CO（最終周途中）
    ("skip",     0.06),  # 4 スキップ復帰
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

    # ⚠ 以前は「全員完走なら1台COに差し替える」保険を入れていたが、
    #    §24.67 の完走率（1レーン55%）では全員完走も 16% 程度で正常に起こる
    #    正しい分布。意図的に潰すと頻度が歪むため、保険は設けない。
    #    「有」を選んでも全員完走が出ることはある（それが実データの姿）。

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


def describe_pattern_short(pattern: dict, gate_names: list[str] | None = None) -> str:
    """一覧の注釈用の短い説明（例: "完走" / "CO(2周目SQ1手前)" / "DNS"）。

    レース一覧の「レース」ラベル下に L1/L2/L3 と並べて出すため、簡潔にする。
    """
    ptype = pattern["type"]

    def gname(idx: int) -> str:
        if gate_names and 0 <= idx < len(gate_names):
            return gate_names[idx]
        return f"G{idx}"

    if ptype == "finish":
        return "完走"
    if ptype == "dns":
        return "DNS"
    if ptype == "co":
        return "CO({}周{}手前)".format(pattern["stop_lap"], gname(pattern["stop_gate"]))
    if ptype == "skip":
        return "スキップ({}周{}欠)".format(pattern["skip_lap"], gname(pattern["skip_gate"]))
    return "?"


def summarize_patterns(patterns: list[dict]) -> str:
    """レース全体の一言サマリ（例: "全車CO" / "全車完走" / "1台DNS" / "混在"）。

    L1/L2/L3 の詳細とは別に、ぱっと見の状態を短く表す。
    """
    types = [p["type"] for p in patterns]
    n = len(types)
    n_fin = types.count("finish")
    n_dns = types.count("dns")
    n_co = types.count("co")
    n_skip = types.count("skip")

    if n_fin == n:
        return "全車完走"
    if n_dns == n:
        return "全車DNS"
    if n_co == n:
        return "全車CO"
    # 完走ゼロ＝誰も完走せず
    if n_fin == 0:
        return "全車リタイア"
    # それ以外は混在。特筆すべき台数だけ添える
    tags = []
    if n_dns:
        tags.append(f"DNS{n_dns}")
    if n_co:
        tags.append(f"CO{n_co}")
    if n_skip:
        tags.append(f"スキップ{n_skip}")
    return "完走{}／{}".format(n_fin, "・".join(tags)) if tags else "混在"
