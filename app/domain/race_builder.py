"""
レース組み立て（通過イベント列 → マシンごとのラップ・セクター・合計）

14章 DA3/DA4/DA7 の実装。DB非依存の純粋関数。
rotation.py の同定エンジンを使い、GWから届いた通過イベントを
「スタートレーンごとのマシン」に束ね、ラップ・セクター・合計を組み上げる。

前提（理想状態版）:
  全車完走・欠測なし・予定外なし。イレギュラーは14章 R1-R10 で後日。

入力イベント（GWが記録した材料・14章 DA3）:
  PassEvent(node_id, lane, t_us, t_us_b, quality, seq)
  - node_id : 通過したゲートの実機ID（レイアウトでゲート位置に対応づく）
  - lane    : 物理レーン 1..3
  - t_us    : ビームAの打刻（GW時刻・µs）
  - t_us_b  : ビームBの打刻（速度算出用・任意）
  - quality : 0=正常 / 1=片ビーム欠 / 3=未同期（S4）
  レース単位のメタ:
  - green_t_us : 緑を出した時刻（None なら走行式・DA4）
  - heat_id    : ヒートID（対応づけ用タグ・DA5）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.domain.rotation import (
    CourseModel,
    Gate,
    build_course,
    identify_start_lane,
    LayoutElement,
    LANES,
)


# ---------------------------------------------------------------------------
# 入力データ
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PassEvent:
    """1つのゲート通過イベント（GWが記録した材料）。"""

    node_id: int          # 通過したゲートの実機ID
    lane: int             # 物理レーン 1..3
    t_us: int             # ビームAの打刻（GW時刻・µs）
    t_us_b: int | None = None   # ビームBの打刻（速度用・任意）
    quality: int = 0      # 0正常 / 1片ビーム欠 / 3未同期
    seq: int = 0          # 発生ノードごとの通番（欠番検出用）


StartMode = Literal["f1", "run"]  # F1式 / 走行式


# ---------------------------------------------------------------------------
# 出力データ
# ---------------------------------------------------------------------------

@dataclass
class SectorTime:
    """区間タイム（あるゲート→次のゲート）。"""

    from_gate_index: int
    to_gate_index: int
    dt_us: int


@dataclass
class LapResult:
    """1周分の結果。"""

    lap: int                       # 1始まり
    lap_time_us: int               # そのラップのタイム
    sectors: list[SectorTime] = field(default_factory=list)


@dataclass
class MachineResult:
    """1台（1スタートレーン）分の結果。"""

    start_lane: int
    laps: list[LapResult] = field(default_factory=list)
    total_time_us: int | None = None   # 完了時のみ
    completed_laps: int = 0
    dnf: bool = False                   # 理想状態版では常にFalse

    @property
    def best_lap_us(self) -> int | None:
        if not self.laps:
            return None
        return min(l.lap_time_us for l in self.laps)


@dataclass
class RaceResult:
    """レース1本（1ヒート）分の結果。"""

    heat_id: int | None
    mode: StartMode
    target_laps: int
    machines: dict[int, MachineResult] = field(default_factory=dict)  # start_lane -> result

    def ranking(self) -> list[MachineResult]:
        """合計タイムの昇順（完了者優先）。未完了は末尾。"""
        done = [m for m in self.machines.values() if m.total_time_us is not None]
        notdone = [m for m in self.machines.values() if m.total_time_us is None]
        done.sort(key=lambda m: m.total_time_us)  # type: ignore[arg-type]
        return done + notdone


# ---------------------------------------------------------------------------
# 組み立て本体
# ---------------------------------------------------------------------------

def _node_to_gate(course: CourseModel) -> dict[int, Gate]:
    """node_id → Gate の対応表。"""
    return {g.node_id: g for g in course.gates if g.node_id is not None}


def build_race(
    layout: list[LayoutElement],
    events: list[PassEvent],
    target_laps: int,
    green_t_us: int | None = None,
    heat_id: int | None = None,
    lanes: int = LANES,
) -> RaceResult:
    """通過イベント列からレース結果を組み立てる（理想状態版）。

    手順:
      1. レイアウトから CourseModel（ゲート順・累積ずれ）を作る。
      2. green_t_us の有無で F1式/走行式を判別（DA4）。
      3. イベントを (物理レーン, ゲート) ごとに時刻順で並べる。
         同じ (レーン, ゲート) の n 回目が n 周目の通過。
      4. 各通過について、rotation で「どのスタートレーンのマシンか」を同定。
      5. スタートレーンごとに、S/G通過列からラップ・合計を、
         ゲート列からセクターを組み上げる。

    ⚠ 理想状態版の前提: 各 (レーン, ゲート) の通過回数が規定通りで欠測なし。
    """
    course = build_course(layout)
    n2g = _node_to_gate(course)
    mode: StartMode = "f1" if green_t_us is not None else "run"

    # S/Gゲートを特定（レイアウト先頭のSGゲート・index=0想定だが厳密に探す）
    sg_gates = [g for g in course.gates if g.kind == "SG"]
    if len(sg_gates) != 1:
        raise ValueError("layout must contain exactly one S/G gate")
    sg_gate = sg_gates[0]

    result = RaceResult(
        heat_id=heat_id, mode=mode, target_laps=target_laps
    )

    # --- (物理レーン, ゲートindex) ごとに時刻順で並べ、通過順(=何回目)を付与 ---
    # key: (lane, gate_index) -> list[(t_us, event, gate)]
    grouped: dict[tuple[int, int], list[tuple[int, PassEvent, Gate]]] = {}
    for ev in events:
        gate = n2g.get(ev.node_id)
        if gate is None:
            # レイアウトに無いノード＝予定外（理想状態版では発生しない想定）
            # 捨てずに無視だけする（R5で扱う）
            continue
        key = (ev.lane, gate.index)
        grouped.setdefault(key, []).append((ev.t_us, ev, gate))

    for lst in grouped.values():
        lst.sort(key=lambda x: x[0])  # 時刻昇順

    # --- 各通過を同定し、スタートレーンごとに仕分ける ---
    # machine_passings[start_lane][gate_index] = { lap: (t_us, event) }
    machine_passings: dict[int, dict[int, dict[int, tuple[int, PassEvent]]]] = {}

    for (phys_lane, gate_index), lst in grouped.items():
        gate = n2g_index(course, gate_index)
        for occurrence, (t_us, ev, _g) in enumerate(lst):
            # S/Gは passing=0 がスタート、passing=k が k周目完了。
            # セクションゲートは occurrence 0 が 1周目。
            if gate.kind == "SG":
                passing = occurrence           # 0=スタート, 1..=周回完了
                lap_for_identify = passing     # S/Gの同定は passing をそのまま lap 相当に
                # start_lane 同定（S/Gは rot_to_gate=0・passing周ぶんずれる）
                start_lane = _identify_from_sg(phys_lane, passing, course.rot_total, lanes)
                lap_key = passing              # 0=スタート打刻
            else:
                lap = occurrence + 1           # 1周目=1
                start_lane = identify_start_lane(
                    phys_lane, lap, gate.rot_to_gate, course.rot_total, lanes
                )
                lap_key = lap

            machine_passings.setdefault(start_lane, {}) \
                            .setdefault(gate_index, {})[lap_key] = (t_us, ev)

    # --- スタートレーンごとに結果を組む ---
    for start_lane, gates_map in machine_passings.items():
        mres = MachineResult(start_lane=start_lane)

        sg_map = gates_map.get(sg_gate.index, {})
        # スタート時刻 t0: F1式なら緑、走行式ならS/Gのpassing=0
        if mode == "f1":
            t0 = green_t_us
        else:
            t0 = sg_map.get(0, (None, None))[0]

        # ラップ: S/Gの passing=1..target_laps を使う
        prev_t = t0
        for lap in range(1, target_laps + 1):
            cur = sg_map.get(lap)
            if cur is None or prev_t is None:
                break  # 欠測（理想状態版では起きない）
            cur_t = cur[0]
            lap_time = cur_t - prev_t
            lapres = LapResult(lap=lap, lap_time_us=lap_time)

            # セクター: この周の各ゲート通過時刻の差
            lapres.sectors = _build_sectors(course, gates_map, lap, sg_gate, prev_t, cur_t)

            mres.laps.append(lapres)
            prev_t = cur_t
            mres.completed_laps = lap

        # 合計: t0 → 最終ラップ完了
        if mode == "f1":
            final = sg_map.get(target_laps)
            if final is not None and t0 is not None and mres.completed_laps == target_laps:
                mres.total_time_us = final[0] - t0
        else:
            final = sg_map.get(target_laps)
            if final is not None and t0 is not None and mres.completed_laps == target_laps:
                mres.total_time_us = final[0] - t0

        result.machines[start_lane] = mres

    return result


def _identify_from_sg(phys_lane: int, passing: int, rot_total: int, lanes: int) -> int:
    """S/Gでの逆算。lane = (start-1 + passing*rot_total) mod lanes + 1 の逆。"""
    return (phys_lane - 1 - passing * rot_total) % lanes + 1


def n2g_index(course: CourseModel, gate_index: int) -> Gate:
    """gate_index から Gate を引く。"""
    for g in course.gates:
        if g.index == gate_index:
            return g
    raise KeyError(gate_index)


def _build_sectors(
    course: CourseModel,
    gates_map: dict[int, dict[int, tuple[int, PassEvent]]],
    lap: int,
    sg_gate: Gate,
    lap_start_t: int,
    lap_end_t: int,
) -> list[SectorTime]:
    """1周分のセクター（S/G → G1 → ... → G_last → S/G）を組む。

    その周に、各ゲートを通過した時刻を並べ、隣接差をセクターとする。
    理想状態版では全ゲートを順に通っている前提。
    """
    # この周に通ったゲートを、通過順（index順）に、時刻付きで集める
    # S/G(周回開始) は lap_start_t、S/G(周回完了) は lap_end_t を端点にする
    points: list[tuple[int, int]] = []  # (gate_index, t_us)

    # 周回開始の起点＝前のS/G通過（or 緑）: gate_index を S/G として端点に置く
    points.append((sg_gate.index, lap_start_t))

    # 中間のセクションゲート（index順）: この周(lap)の通過を拾う
    for g in course.gates:
        if g.kind == "SG":
            continue
        gm = gates_map.get(g.index, {})
        cur = gm.get(lap)
        if cur is not None:
            points.append((g.index, cur[0]))

    # 周回完了のS/G
    points.append((sg_gate.index, lap_end_t))

    # 時刻順に並べ（通常は既に順序通り）、隣接差をセクターに
    points.sort(key=lambda x: x[1])
    sectors: list[SectorTime] = []
    for a, b in zip(points, points[1:]):
        sectors.append(SectorTime(
            from_gate_index=a[0], to_gate_index=b[0], dt_us=b[1] - a[1]
        ))
    return sectors
