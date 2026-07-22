#!/usr/bin/env python3
"""
fake_race.py — ローテーション対応レースシミュレータ（機材ゼロで通しテスト）

14章の理想状態版を、実機なしで end-to-end に確認するためのCLI。
既存 tools/sim/fake_gw.py（旧構成・固定レーン）は触らず、別ツールとして置く。

やること:
  1. コースレイアウト（S/G・SQ・LC）を組み立てる
  2. 指定台数・周回数・ペースで、rotation の式に従った通過イベントを生成
  3. race_builder で組み立て、ラップ・セクター・合計・順位を表示
  4. （任意）緑時刻を与えて F1式 も確認

使い方:
  python fake_race.py --laps 3
  python fake_race.py --laps 6 --sq 2 --lc-after 1
  python fake_race.py --laps 3 --f1 --reaction 0.3,0.15,0.5
  python fake_race.py --laps 3 --json     # 結果をJSONで

⚠ これはサーバーにPOSTするのではなく、domain層を直接叩く「計算の通しテスト」。
   サーバーPOST経由の確認は、race_builder を timing_service に組み込んでから。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

# domain を import できるようにパスを通す（server/ をルートに）
sys.path.insert(0, ".")
sys.path.insert(0, "server")

from app.domain.rotation import (  # noqa: E402
    LayoutElement,
    build_course,
    expected_lane,
    expected_sg_lane,
    validate_layout,
)
from app.domain.race_builder import (  # noqa: E402
    PassEvent,
    build_race,
)

US = 1_000_000
LANES = 3


def build_layout(sq: int, lc: int) -> list[LayoutElement]:
    """S/G → SQ → LC → SQ → LC → ... と、SQとLCを交互気味に並べる簡易ビルダ。

    sq : セクションゲート総数（0..6）
    lc : レーンチェンジ総数（0以上）
    ノードID: SGは6、SQは0..sq-1 を順に割り当てる。

    SQの合間にLCを分散配置する。SQが0でもLCだけは置ける（S/Gのみ+LC）。
    """
    layout: list[LayoutElement] = [LayoutElement("SG", node_id=6)]
    # SQ を並べつつ、LC を均等に差し込む
    lc_remaining = lc
    if sq == 0:
        # S/Gのみ。LCを全部後ろに置く
        for _ in range(lc_remaining):
            layout.append(LayoutElement("LC"))
        return layout

    # SQ の後ろに LC を順に配っていく（最後のSQの後にも置ける）
    slots = sq  # LCを置ける位置の数（各SQの後ろ）
    for i in range(sq):
        layout.append(LayoutElement("SQ", node_id=i))
        # このSQの後ろに置くLC数（残りを均等配分）
        remaining_slots = slots - i
        put = -(-lc_remaining // remaining_slots) if remaining_slots else 0  # ceil
        put = min(put, lc_remaining)
        for _ in range(put):
            layout.append(LayoutElement("LC"))
        lc_remaining -= put
    return layout


def gen_events(
    layout: list[LayoutElement],
    target_laps: int,
    pace_ms: dict[int, int],
    start_t: int = 1_000_000,
) -> list[PassEvent]:
    """rotation の式に従った通過イベントを生成する（理想状態版・欠測なし）。

    各マシンは一定ペース pace_ms[start_lane]（1周のミリ秒）で走る。
    セクションゲートは周内を等分した位置で通過させる。
    """
    course = build_course(layout)
    rot_total = course.rot_total
    section_gates = [g for g in course.gates if g.kind == "SQ"]

    events: list[PassEvent] = []
    for start_lane in (1, 2, 3):
        lap_us = pace_ms[start_lane] * 1000

        # スタート打刻 passing=0
        events.append(PassEvent(
            node_id=6,
            lane=expected_sg_lane(start_lane, 0, rot_total),
            t_us=start_t,
        ))

        for lap in range(1, target_laps + 1):
            lap_base = start_t + (lap - 1) * lap_us
            n = len(section_gates)
            # セクションゲートを周内で等分配置
            for idx, g in enumerate(section_gates, start=1):
                frac = idx / (n + 1)
                events.append(PassEvent(
                    node_id=g.node_id,
                    lane=expected_lane(start_lane, lap, g.rot_to_gate, rot_total),
                    t_us=lap_base + int(lap_us * frac),
                ))
            # S/G完了 passing=lap
            events.append(PassEvent(
                node_id=6,
                lane=expected_sg_lane(start_lane, lap, rot_total),
                t_us=start_t + lap * lap_us,
            ))

    return events


def fmt_us(us: int | None) -> str:
    if us is None:
        return "----.---"
    return f"{us / US:8.3f}"


def print_result(race, layout) -> None:
    course = build_course(layout)
    mode_label = "F1式（反応込み）" if race.mode == "f1" else "走行式"
    print(f"\n=== レース結果  heat_id={race.heat_id}  {mode_label}  "
          f"{race.target_laps}周  LC={course.lc_count} ===")

    print("\n[順位]")
    for pos, m in enumerate(race.ranking(), start=1):
        print(f"  {pos}位  スタート{m.start_lane}コース  "
              f"合計 {fmt_us(m.total_time_us)}s  "
              f"ベストラップ {fmt_us(m.best_lap_us)}s")

    print("\n[ラップ詳細]")
    for start_lane in sorted(race.machines):
        m = race.machines[start_lane]
        print(f"  スタート{start_lane}コース:")
        for lap in m.laps:
            secs = "  ".join(
                f"S{s.from_gate_index}->{s.to_gate_index}:{fmt_us(s.dt_us)}"
                for s in lap.sectors
            )
            print(f"    {lap.lap}周目  {fmt_us(lap.lap_time_us)}s   [{secs}]")


def main() -> int:
    p = argparse.ArgumentParser(description="ローテーション対応レースシミュレータ")
    p.add_argument("--laps", type=int, default=3, help="周回数（3の倍数・最大9）")
    p.add_argument("--sq", type=int, default=2, help="セクションゲート数（0..6）")
    p.add_argument("--lc", type=int, default=1, help="レーンチェンジ数（0=同一レーン検証, 3の倍数は不可）")
    p.add_argument("--pace", default="12.0,12.2,11.9",
                   help="各スタートレーンの1周秒（カンマ区切り3つ）")
    p.add_argument("--f1", action="store_true", help="F1式（緑時刻を与える）")
    p.add_argument("--reaction", default="0.2,0.4,0.3",
                   help="F1式のとき、各レーンの反応秒（緑→スタート打刻）")
    p.add_argument("--heat-id", type=int, default=1)
    p.add_argument("--json", action="store_true", help="結果をJSONで出力")
    args = p.parse_args()

    # レイアウト
    layout = build_layout(args.sq, args.lc)

    # バリデーション（確定時チェックを通す）
    vr = validate_layout(layout)
    for issue in vr.issues:
        mark = "✕" if issue.severity == "error" else "⚠"
        print(f"{mark} [{issue.code}] {issue.message}", file=sys.stderr)
    if not vr.can_commit:
        print("レイアウトが不正です。中止します。", file=sys.stderr)
        return 1

    pace_list = [float(x) for x in args.pace.split(",")]
    pace_ms = {1: int(pace_list[0] * 1000),
               2: int(pace_list[1] * 1000),
               3: int(pace_list[2] * 1000)}

    if args.f1:
        # 緑=0 を基準に、各レーンが反応時間ぶん遅れてスタート打刻する
        green_t = 0
        reactions = [float(x) for x in args.reaction.split(",")]
        events = _gen_f1(layout, args.laps, pace_ms, reactions)
        race = build_race(layout, events, args.laps,
                          green_t_us=green_t, heat_id=args.heat_id)
    else:
        events = gen_events(layout, args.laps, pace_ms)
        race = build_race(layout, events, args.laps,
                          green_t_us=None, heat_id=args.heat_id)

    if args.json:
        out = {
            "heat_id": race.heat_id,
            "mode": race.mode,
            "target_laps": race.target_laps,
            "ranking": [
                {"pos": i + 1, "start_lane": m.start_lane,
                 "total_us": m.total_time_us, "best_lap_us": m.best_lap_us}
                for i, m in enumerate(race.ranking())
            ],
            "machines": {
                str(sl): {
                    "start_lane": m.start_lane,
                    "completed_laps": m.completed_laps,
                    "total_us": m.total_time_us,
                    "laps": [
                        {"lap": l.lap, "lap_time_us": l.lap_time_us,
                         "sectors": [asdict(s) for s in l.sectors]}
                        for l in m.laps
                    ],
                } for sl, m in race.machines.items()
            },
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print_result(race, layout)

    return 0


def _gen_f1(layout, target_laps, pace_ms, reactions):
    """F1式用: 各レーンが反応時間ぶん遅れてスタート打刻する版。"""
    course = build_course(layout)
    rot_total = course.rot_total
    section_gates = [g for g in course.gates if g.kind == "SQ"]
    events: list[PassEvent] = []
    for i, start_lane in enumerate((1, 2, 3)):
        start_t = int(reactions[i] * US)  # 緑=0 からの反応遅れ
        lap_us = pace_ms[start_lane] * 1000
        events.append(PassEvent(
            node_id=6, lane=expected_sg_lane(start_lane, 0, rot_total), t_us=start_t))
        for lap in range(1, target_laps + 1):
            lap_base = start_t + (lap - 1) * lap_us
            n = len(section_gates)
            for idx, g in enumerate(section_gates, start=1):
                frac = idx / (n + 1)
                events.append(PassEvent(
                    node_id=g.node_id,
                    lane=expected_lane(start_lane, lap, g.rot_to_gate, rot_total),
                    t_us=lap_base + int(lap_us * frac)))
            events.append(PassEvent(
                node_id=6, lane=expected_sg_lane(start_lane, lap, rot_total),
                t_us=start_t + lap * lap_us))
    return events


if __name__ == "__main__":
    raise SystemExit(main())
