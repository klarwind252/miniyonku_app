"""
race_builder.py の単体テスト（合成イベントで検証）

理想状態版: 全車完走・欠測なし。
1周を約12秒、3周レース、レイアウト S/G → SQ → LC → SQ（rot_total=1）で
合成イベントを作り、ラップ・セクター・合計・同定・F1/走行判別を確認する。
"""

import pytest

from app.domain.rotation import LayoutElement
from app.domain.race_builder import (
    PassEvent,
    build_race,
)


# ---------------------------------------------------------------------------
# 合成イベント生成
# ---------------------------------------------------------------------------

# レイアウト: S/G(node6) → SQ(node0) → LC → SQ(node1)
LAYOUT = [
    LayoutElement("SG", node_id=6),
    LayoutElement("SQ", node_id=0),
    LayoutElement("LC"),
    LayoutElement("SQ", node_id=1),
]

US = 1_000_000  # 1秒 = 1e6 µs


def expected_phys_lane(start_lane, passing_or_lap, rot_to_gate, rot_total=1, lanes=3):
    return (start_lane - 1 + passing_or_lap * rot_total + rot_to_gate - rot_to_gate) % lanes  # placeholder


def make_events(target_laps=3, green_t=None, start_t=1_000_000):
    """3台（スタートレーン1/2/3）が target_laps 周する合成イベントを作る。

    各マシンは一定ペースで走る:
      S/G通過（各周完了）は約12秒間隔
      SQ0(LC前)は周の +4秒、SQ1(LC後)は +8秒の位置
    物理レーンは rotation の式に従って移動する。
    """
    from app.domain.rotation import expected_sg_lane, expected_lane

    events = []
    rot_total = 1

    # 各マシンのペース（わざと少しずつ変える→順位が付く）
    lap_ms = {1: 12_000, 2: 12_200, 3: 11_900}  # 1周のミリ秒

    for start_lane in (1, 2, 3):
        lap_us = lap_ms[start_lane] * 1000
        # スタート打刻 passing=0（S/G上流スタート）
        sg_lane0 = expected_sg_lane(start_lane, 0, rot_total)
        events.append(PassEvent(node_id=6, lane=sg_lane0, t_us=start_t, seq=0))

        for lap in range(1, target_laps + 1):
            base = start_t + lap * lap_us  # この周のS/G完了時刻
            # 中間ゲート（この周 lap の通過）
            # SQ0(LC前・rot_to_gate=0): 周の途中 base - lap_us + 4秒相当
            lane_sq0 = expected_lane(start_lane, lap, rot_to_gate=0, rot_total=rot_total)
            t_sq0 = start_t + (lap - 1) * lap_us + int(lap_us * 0.33)
            events.append(PassEvent(node_id=0, lane=lane_sq0, t_us=t_sq0))

            lane_sq1 = expected_lane(start_lane, lap, rot_to_gate=1, rot_total=rot_total)
            t_sq1 = start_t + (lap - 1) * lap_us + int(lap_us * 0.66)
            events.append(PassEvent(node_id=1, lane=lane_sq1, t_us=t_sq1))

            # S/G完了 passing=lap
            sg_lane = expected_sg_lane(start_lane, lap, rot_total)
            events.append(PassEvent(node_id=6, lane=sg_lane, t_us=base))

    return events


# ---------------------------------------------------------------------------
# 走行式（緑なし）
# ---------------------------------------------------------------------------

def test_run_mode_basic():
    """緑なし → 走行式。3台が3周完走し、合計が出る。"""
    events = make_events(target_laps=3, green_t=None)
    race = build_race(LAYOUT, events, target_laps=3, green_t_us=None, heat_id=42)

    assert race.mode == "run"
    assert race.heat_id == 42
    assert set(race.machines.keys()) == {1, 2, 3}

    for start_lane, m in race.machines.items():
        assert m.completed_laps == 3
        assert len(m.laps) == 3
        assert m.total_time_us is not None


def test_run_mode_lap_times():
    """走行式のラップタイムが、仕込んだペース通りか。"""
    events = make_events(target_laps=3)
    race = build_race(LAYOUT, events, target_laps=3)

    m1 = race.machines[1]
    # 1周12.0秒で仕込んだので、各ラップ ≈ 12,000,000 µs
    for lap in m1.laps:
        assert abs(lap.lap_time_us - 12_000_000) < 1000  # ±1ms
    # 合計 ≈ 36秒
    assert abs(m1.total_time_us - 36_000_000) < 3000


def test_ranking_order():
    """3が一番速く(11.9s)、2が一番遅い(12.2s)。順位は 3,1,2。"""
    events = make_events(target_laps=3)
    race = build_race(LAYOUT, events, target_laps=3)
    ranking = race.ranking()
    order = [m.start_lane for m in ranking]
    assert order == [3, 1, 2]


# ---------------------------------------------------------------------------
# F1式（緑あり）
# ---------------------------------------------------------------------------

def test_f1_mode_detected():
    """緑ありなら F1式。"""
    events = make_events(target_laps=3, start_t=2_000_000)
    green = 1_000_000  # スタート打刻より前に緑
    race = build_race(LAYOUT, events, target_laps=3, green_t_us=green)
    assert race.mode == "f1"


def test_f1_includes_reaction_time():
    """F1式では、緑からS/G通過までの反応時間が合計に乗る。

    緑=1.0s、スタート打刻=2.0s（=1秒の反応ロス）で仕込む。
    走行式の合計より、F1式の合計は約1秒多いはず。
    """
    green = 1_000_000
    start_t = 2_000_000  # 反応に1秒
    events = make_events(target_laps=3, start_t=start_t)

    race_f1 = build_race(LAYOUT, events, target_laps=3, green_t_us=green)
    race_run = build_race(LAYOUT, events, target_laps=3, green_t_us=None)

    m_f1 = race_f1.machines[1]
    m_run = race_run.machines[1]
    # F1式は緑起点、走行式はスタート打刻起点。差は反応時間の1秒。
    diff = m_f1.total_time_us - m_run.total_time_us
    assert abs(diff - 1_000_000) < 2000  # ≈1秒


# ---------------------------------------------------------------------------
# 同定の正しさ（物理レーンが移動しても、正しいマシンに束ねられる）
# ---------------------------------------------------------------------------

def test_identity_across_rotation():
    """1コーススタートのマシンは、S/Gで 1→2→3→(start)... と物理レーンが動くが、
    すべて start_lane=1 の1台として束ねられ、ラップが3本そろう。"""
    events = make_events(target_laps=3)
    race = build_race(LAYOUT, events, target_laps=3)
    # 3台とも、それぞれ3周ぶんのラップがある（＝別レーンの通過が混ざっていない）
    for start_lane in (1, 2, 3):
        m = race.machines[start_lane]
        assert len(m.laps) == 3
        assert [l.lap for l in m.laps] == [1, 2, 3]


def test_sectors_present():
    """各ラップにセクターが組まれている（S/G→SQ0→SQ1→S/G の3区間）。"""
    events = make_events(target_laps=3)
    race = build_race(LAYOUT, events, target_laps=3)
    m1 = race.machines[1]
    for lap in m1.laps:
        # S/G→SQ0, SQ0→SQ1, SQ1→S/G の3区間
        assert len(lap.sectors) == 3
        # 区間の合計 ≈ ラップタイム
        s = sum(sec.dt_us for sec in lap.sectors)
        assert abs(s - lap.lap_time_us) < 1000


def test_best_lap():
    """best_lap が最小ラップを返す。"""
    events = make_events(target_laps=3)
    race = build_race(LAYOUT, events, target_laps=3)
    m1 = race.machines[1]
    assert m1.best_lap_us == min(l.lap_time_us for l in m1.laps)


# ---------------------------------------------------------------------------
# 6周（3の倍数）でも成立する
# ---------------------------------------------------------------------------

def test_six_laps():
    events = make_events(target_laps=6)
    race = build_race(LAYOUT, events, target_laps=6)
    for start_lane in (1, 2, 3):
        m = race.machines[start_lane]
        assert m.completed_laps == 6
        assert m.total_time_us is not None
