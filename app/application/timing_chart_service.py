"""ポジションチャート（順位変動グラフ）用データの組み立て（application層）。

各レーンが「S/G→SE0→SE1→…→S/G→…」とゲートを通過していく過程を、
横軸=通過ゲートの通し番号・縦軸=その通過時点での順位、で表すためのデータを作る。

順位の定義（ユーザー確定・§H）:
  「同じ通過数の中での累積タイム順」
  = X番目のゲートまで到達した各マシンを、その地点までの累積タイム昇順に並べた順位。
  先に到達していても累積タイムが遅ければ順位は下。

CO車（途中リタイア）の扱い（ユーザー確定）:
  完了した最後のラップの通過までで線を止める（以降は描かない）。
  MachineResult.laps は完了した周しか持たないため、laps を辿るだけで自然に止まる。

このモジュールは build_race_result() が返す RaceResult を入力にする。
DB や生イベントには触らない（表示専用・集計に影響しない）。
"""

from __future__ import annotations

from app.domain.rotation import (
    build_course, LayoutElement, expected_lane, expected_sg_lane,
)


# 周ごとの色（線を周で塗り分ける。多周でも循環）。CSSに直接使える色名/HEX。
LAP_COLORS = [
    "#2563eb",  # 1周目 青
    "#dc2626",  # 2周目 赤
    "#f59e0b",  # 3周目 橙
    "#16a34a",  # 4周目 緑
    "#7c3aed",  # 5周目 紫
    "#0891b2",  # 6周目 シアン
]


def _gate_short_label(kind: str, sq_seq: int) -> str:
    """ゲート種別を短いラベルにする（SG / SE0 / SE1 ...）。

    sq_seq は SQ の連番（0始まり）。SG は連番を使わない。
    """
    if kind == "SG":
        return "SG"
    return f"SE{sq_seq}"


def build_position_chart(race_row, result, layout_elems: list) -> dict:
    """ポジションチャート用の JSON 化可能な dict を返す。

    Args:
        race_row:     get_race の行（target_laps を使う）
        result:       build_race の RaceResult
        layout_elems: LayoutElement のリスト（ゲート順・種別ラベル用）

    Returns:
        {
          "target_laps": 3,
          "x_axis": [ {"pos":0,"gate":"SG","lap":0,"boundary":true},
                      {"pos":1,"gate":"SE0","lap":1}, ... ],
          "lanes": [
            {"start_lane":1, "dnf":false,
             "points":[ {"pos":0,"t_s":0.0,"rank":1,"lap":0,"gate":"SG"},
                        {"pos":1,"t_s":3.9,"rank":2,"lap":1,"gate":"SE0"}, ...]},
            ...
          ],
          "lap_colors": ["#2563eb", ...]
        }
    """
    if result is None:
        return {"target_laps": 0, "x_axis": [], "lanes": [], "lap_colors": LAP_COLORS}

    target_laps = race_row["target_laps"]
    course = build_course([
        LayoutElement(kind=e.kind if hasattr(e, "kind") else e["kind"],
                      node_id=e.node_id if hasattr(e, "node_id") else e["node_id"])
        for e in layout_elems
    ])
    gates = list(course.gates)                 # index順（0=SG, 1..=SQ）
    n_gates = len(gates)                        # SG含む1周のゲート数
    mid_gates = [g for g in gates if g.kind != "SG"]  # 中間ゲート（SQ）

    # SQ に 0始まりの連番を振る（SE0, SE1, ...）。index順で採番。
    sq_seq = {}
    seq = 0
    for g in gates:
        if g.kind != "SG":
            sq_seq[g.index] = seq
            seq += 1

    # -------------------------------------------------------------------
    # X軸の定義: 通過の通し番号
    #   pos=0            : スタート（SG, lap0）
    #   その後 lap=1..N で [SE0, SE1, ..., SEk, SG(周回完了)] を繰り返す
    # -------------------------------------------------------------------
    x_axis = [{"pos": 0, "gate": "SG", "lap": 0, "boundary": True}]
    pos = 1
    # pos → (lap, gate_index) の対応表も作る（各マシンの累積時刻を並べるため）
    pos_map = []  # index=pos-1（pos>=1）: (lap, gate_index, gate_kind)
    for lap in range(1, target_laps + 1):
        for g in mid_gates:
            x_axis.append({"pos": pos, "gate": _gate_short_label(g.kind, sq_seq[g.index]),
                           "lap": lap, "boundary": False})
            pos_map.append((lap, g.index, g.kind))
            pos += 1
        # 周回完了の SG
        x_axis.append({"pos": pos, "gate": "SG", "lap": lap, "boundary": True})
        pos_map.append((lap, gates[0].index, "SG"))
        pos += 1

    total_pos = pos  # X軸の点数（0..total_pos-1）

    # -------------------------------------------------------------------
    # 各マシンの「pos → 累積タイム(秒)」を組み立てる
    #   sectors の dt_us を積み上げると各ゲート通過の累積時刻が復元できる。
    #   laps に入っている周だけ辿る＝CO周以降は自然に止まる。
    # -------------------------------------------------------------------
    lane_cumul = {}  # start_lane -> {pos: t_s}
    lane_dnf = {}
    for start_lane, m in result.machines.items():
        cumul_us = 0
        pts = {0: 0.0}  # pos=0（スタート）は 0 秒
        # 各周: sectors は S/G→SE0→SE1→...→S/G の隣接差
        # pos は lap ごとに [SE0..SEk, SG] の順に進む
        for lap_res in m.laps:
            lap = lap_res.lap
            # この周の pos 範囲の先頭 index を求める
            # pos=0 はスタート。lap>=1 の各周は (lap-1)*n_gates + 1 から始まる
            base_pos = (lap - 1) * n_gates + 1
            # sectors を index順（通過順）に並べ、累積を積む
            # sectors[0] = SG→最初のSQ, sectors[k] = ...→SG(完了)
            acc = cumul_us
            # 中間ゲート + 周回完了SG の順に pos を割り当てる
            # sectors は points をソートして作られているので通過順に並んでいる
            gate_pos = base_pos
            for sec in lap_res.sectors:
                acc += sec.dt_us
                # sec.to_gate_index が到達したゲート。SGなら周回完了。
                pts[gate_pos] = acc / 1_000_000
                gate_pos += 1
            cumul_us = acc  # 周完了時刻を次周の起点に
        lane_cumul[start_lane] = pts
        lane_dnf[start_lane] = m.dnf

    # -------------------------------------------------------------------
    # 各 pos で順位を計算（その pos に到達した各マシンを累積タイム昇順）
    # -------------------------------------------------------------------
    rank_at = {}  # (start_lane, pos) -> rank
    for p in range(total_pos):
        arrived = [(sl, c[p]) for sl, c in lane_cumul.items() if p in c]
        arrived.sort(key=lambda x: x[1])  # 累積タイム昇順
        for rank, (sl, _t) in enumerate(arrived, 1):
            rank_at[(sl, p)] = rank

    # -------------------------------------------------------------------
    # 出力用に整形
    # -------------------------------------------------------------------
    # gate_index → Gate（rot_to_gate 参照用）
    gate_by_index = {g.index: g for g in gates}
    rot_total = course.rot_total
    # レーン数（回転計算に使う）。物理レーンは 1..n_lanes を循環する。
    n_lanes = max(result.machines.keys()) if result.machines else 1

    def _phys_lane(start_lane: int, pos: int) -> int | None:
        """pos 番目の通過の物理レーン番号を返す。pos=0 はスタート。"""
        if pos == 0:
            return expected_sg_lane(start_lane, 0, rot_total, n_lanes)
        lap, gidx, kind = pos_map[pos - 1]
        if kind == "SG":
            return expected_sg_lane(start_lane, lap, rot_total, n_lanes)
        g = gate_by_index.get(gidx)
        if g is None:
            return None
        return expected_lane(start_lane, lap, g.rot_to_gate, rot_total, n_lanes)

    lanes_out = []
    for start_lane in sorted(lane_cumul.keys()):
        pts = lane_cumul[start_lane]
        series = []
        for p in sorted(pts.keys()):
            xinfo = x_axis[p]
            series.append({
                "pos": p,
                "t_s": round(pts[p], 3),
                "rank": rank_at.get((start_lane, p)),
                "lap": xinfo["lap"],
                "gate": xinfo["gate"],
                "phys": _phys_lane(start_lane, p),   # 物理レーン番号
            })
        m = result.machines.get(start_lane)
        total_s = None
        if m is not None and m.total_time_us is not None:
            total_s = round(m.total_time_us / 1_000_000, 3)
        lanes_out.append({
            "start_lane": start_lane,
            "dnf": lane_dnf.get(start_lane, False),
            "total_s": total_s,   # 完走時のみ。DNF/DNSはNone
            "points": series,
        })

    return {
        "target_laps": target_laps,
        "x_axis": x_axis,
        "lanes": lanes_out,
        "lap_colors": LAP_COLORS,
    }


def build_raw_detections(race_row, event_rows, layout_elems: list,
                         n_lanes: int | None = None) -> dict:
    """物理レーンごとの「全検出」を時刻順に整形する（生データ表示用）。

    同定を通さず、届いた通過をそのまま物理レーン別に並べる。
    乗入（検出過多）・欠落（検出過少）が、期待回数との対比で一目で分かる。

    Args:
        race_row:     get_race の行（target_laps を使う）
        event_rows:   get_events の結果（src, lane, t_us, quality, seq を持つ）
        layout_elems: LayoutElement 相当（kind/node_id）。ゲート名の解決に使う。
        n_lanes:      レーン数。省略時はイベントに現れる最大laneから推定。

    Returns:
        {
          "gates": [{"node_id":6,"name":"GW"}, {"node_id":0,"name":"SE0"}, ...],
          "expected": {node_id: 期待回数, ...},
          "lanes": [
            {
              "lane": 1,
              "detections": [{"gate":"GW","node_id":6,"t_s":0.0,"seq":..,"quality":..}, ...],
              "counts": {node_id: 実回数},
              "count_status": {node_id: "ok"|"over"|"under"}
            }, ...
          ]
        }
    """
    target_laps = race_row["target_laps"]

    # node_id → 表示名（GW / SE0 / SE1 ...）。レイアウトの通過順で採番。
    course = build_course([
        LayoutElement(kind=e["kind"] if isinstance(e, dict) else e.kind,
                      node_id=e["node_id"] if isinstance(e, dict) else e.node_id)
        for e in layout_elems
    ])
    name_by_node = {}
    gates_meta = []
    sq = 0
    for g in course.gates:
        if g.node_id is None:
            continue
        if g.kind == "SG":
            nm = "GW"
        else:
            nm = f"SE{sq}"
            sq += 1
        name_by_node[g.node_id] = nm
        gates_meta.append({"node_id": g.node_id, "name": nm})

    # レーン数
    rows = list(event_rows)
    if n_lanes is None:
        lanes_seen = [(_row_get(r, "lane")) for r in rows]
        n_lanes = max(lanes_seen) if lanes_seen else 1

    # 期待回数: SG=(N+1)*L / SE=N*L
    expected = {}
    for g in course.gates:
        if g.node_id is None:
            continue
        if g.kind == "SG":
            expected[g.node_id] = (target_laps + 1) * n_lanes
        else:
            expected[g.node_id] = target_laps * n_lanes

    # 端末（ゲート＝node_id）ごとに検出を集める（時刻順）。
    # 1端末は全レーンの通過を拾うので、各検出に物理レーンを添える。
    t0 = min((_row_get(r, "t_us") for r in rows), default=0)
    by_device = {}
    for r in rows:
        lane = _row_get(r, "lane")
        node = _row_get(r, "src")
        t_us = _row_get(r, "t_us")
        seq = _row_get(r, "seq")
        q = _row_get(r, "quality")
        eid = _row_get(r, "id")
        exc = bool(_row_get(r, "excluded"))
        by_device.setdefault(node, []).append({
            "id": eid,
            "lane": lane,
            "t_s": round((t_us - t0) / 1_000_000, 3),
            "seq": seq,
            "quality": q,
            "excluded": exc,
        })

    # レイアウトの通過順で端末を並べる（GW, SE0, SE1 ...）。台帳外のnodeは末尾。
    ordered_nodes = [g["node_id"] for g in gates_meta]
    for node in by_device:
        if node not in ordered_nodes:
            ordered_nodes.append(node)

    devices_out = []
    for node in ordered_nodes:
        dets = by_device.get(node, [])
        # 期待回数との対比は「有効な検出」で行う（除外した乗入は数から外れる）。
        active = [d for d in dets if not d["excluded"]]
        devices_out.append({
            "node_id": node,
            "name": name_by_node.get(node, f"n{node}"),
            "detections": dets,                 # 除外も含む全件（UIで薄字表示）
            "count": len(active),               # 有効検出のみの回数
            "expected": expected.get(node),     # 全レーン合計の期待回数
        })

    return {
        "gates": gates_meta,
        "expected": expected,
        "devices": devices_out,
    }


def _row_get(row, key):
    """sqlite Row / dict / tuple のいずれでも値を取れるヘルパー。"""
    if hasattr(row, "keys"):
        return row[key]
    # tuple: SELECT id, src, lane, t_us, t_us_b, quality, seq, excluded の順
    idx = {"id": 0, "src": 1, "lane": 2, "t_us": 3, "t_us_b": 4,
           "quality": 5, "seq": 6, "excluded": 7}[key]
    return row[idx]
