"""レースの status（確定／要確認）判定（application層）。

二段構えDB（I群 §24.71）の受信時判定ロジック。
build_race_result が返す result と、生の通過イベントの回数から、
そのレースを 'confirmed'（確定）か 'needs_review'（要確認）に振り分ける。

自動 confirmed になるのは次の2ケースだけ（§24.71）:
  1. 全車完走 … 全レーンが規定周回を完了し、全ゲートの通過回数が期待どおり
  2. 全車CO   … 完走者ゼロ（total_time_us を持つマシンが1台も無い）

それ以外はすべて needs_review。

⚠ §24.73: 「全車CO＝完走者ゼロ」は、CO＋回転で同定が壊れると誤発火しうる。
   同定バグ解消済みであることが前提（このモジュールは判定するだけ）。
"""

from __future__ import annotations

from collections import Counter

from app.domain.rotation import build_course, LayoutElement

STATUS_CONFIRMED = "confirmed"
STATUS_NEEDS_REVIEW = "needs_review"


def expected_counts(layout_elems: list, target_laps: int, n_lanes: int) -> dict:
    """各物理ゲート(node_id)の期待通過回数を返す。

    - S/G:   (target_laps + 1) * n_lanes  … スタート打刻 passing=0 を含む
    - 中間SE: target_laps * n_lanes
    戻り値: {node_id: expected_count}
    """
    course = build_course([
        LayoutElement(kind=e["kind"] if isinstance(e, dict) else e.kind,
                      node_id=e["node_id"] if isinstance(e, dict) else e.node_id)
        for e in layout_elems
    ])
    exp = {}
    for g in course.gates:
        if g.node_id is None:
            continue
        if g.kind == "SG":
            exp[g.node_id] = (target_laps + 1) * n_lanes
        else:
            exp[g.node_id] = target_laps * n_lanes
    return exp


def actual_counts(event_rows) -> dict:
    """生イベントから各物理ゲート(src=node_id)の実通過回数を数える。

    event_rows: get_events の結果（各行に "src" を持つ）
    戻り値: {node_id: actual_count}
    """
    c = Counter()
    for r in event_rows:
        src = r["src"] if hasattr(r, "keys") else r[0]
        c[src] += 1
    return dict(c)


def judge_status(result, layout_elems: list, event_rows,
                 target_laps: int, n_lanes: int) -> tuple[str, dict]:
    """status を判定して返す。

    Returns:
        (status, detail)
        detail = {
          "finishers": 完走レーン数,
          "expected": {node_id: 期待回数},
          "actual":   {node_id: 実回数},
          "mismatch": {node_id: (実, 期待)}  # 一致しないゲートだけ
        }
    """
    finishers = [m for m in result.machines.values()
                 if m.total_time_us is not None] if result else []
    n_fin = len(finishers)

    exp = expected_counts(layout_elems, target_laps, n_lanes)
    act = actual_counts(event_rows)

    mismatch = {}
    all_node_ids = set(exp) | set(act)
    for nid in all_node_ids:
        e = exp.get(nid, 0)
        a = act.get(nid, 0)
        if a != e:
            mismatch[nid] = (a, e)

    detail = {
        "finishers": n_fin,
        "expected": exp,
        "actual": act,
        "mismatch": mismatch,
    }

    # ケース1: 全車完走 = 完走者が全レーン && 回数ズレ無し
    if n_fin == n_lanes and not mismatch:
        return STATUS_CONFIRMED, detail

    # ケース2: 全車CO = 完走者ゼロ（§24.71・完走者ゼロで確定）
    if n_fin == 0:
        return STATUS_CONFIRMED, detail

    # それ以外はすべて要確認
    return STATUS_NEEDS_REVIEW, detail
