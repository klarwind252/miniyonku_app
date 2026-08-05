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


# ---------------------------------------------------------------------------
# E5(24.39)：CO→DNF
# ---------------------------------------------------------------------------

# 無ローテーション（LCなし）レイアウト: S/G → SQ → SQ。
#   rot_total=0 なので各車は自分の物理レーンに固定され、1台がCOしても
#   他車の同定に干渉しない（DNF混在の検証に使う。rotation.py の「LC0=検証用途」）。
LAYOUT_NOROT = [
    LayoutElement("SG", node_id=6),
    LayoutElement("SQ", node_id=0),
    LayoutElement("SQ", node_id=1),
]


def make_events_norot(laps_by_lane, target_laps=3, start_t=1_000_000):
    """無ローテーション版イベント生成。laps_by_lane で各スタートレーンの周回数を指定。

    指定周回数だけS/G完了＋中間ゲート通過を作る（省略時は target_laps 周＝完走）。
    rot_total=0 なので phys_lane == start_lane。周回数を減らせばCO（DNF）を再現できる。
    """
    events = []
    lap_ms = {1: 12_000, 2: 12_200, 3: 11_900}
    for start_lane in (1, 2, 3):
        n = laps_by_lane.get(start_lane, target_laps)
        lap_us = lap_ms[start_lane] * 1000
        # スタート打刻 passing=0
        events.append(PassEvent(node_id=6, lane=start_lane, t_us=start_t, seq=0))
        for lap in range(1, n + 1):
            base = start_t + lap * lap_us
            t_sq0 = start_t + (lap - 1) * lap_us + int(lap_us * 0.33)
            events.append(PassEvent(node_id=0, lane=start_lane, t_us=t_sq0))
            t_sq1 = start_t + (lap - 1) * lap_us + int(lap_us * 0.66)
            events.append(PassEvent(node_id=1, lane=start_lane, t_us=t_sq1))
            events.append(PassEvent(node_id=6, lane=start_lane, t_us=base))
    return events


def test_e5_all_dnf_when_target_higher():
    """全車2周で止まり規定3周に満たない → 全車DNF。途中経過は残り欠測ではない。"""
    events = make_events(target_laps=2)                # 2周ぶんだけ生成（通常レイアウト）
    race = build_race(LAYOUT, events, target_laps=3)   # 規定は3周
    assert set(race.machines.keys()) == {1, 2, 3}
    for m in race.machines.values():
        assert m.completed_laps == 2
        assert m.dnf is True                # 規定未達＝DNF
        assert m.total_time_us is None      # 未完了は合計なし＝ranking末尾
        assert m.missing is False           # 2周ぶんは揃っている＝欠測ではない
        assert len(m.laps) == 2             # 途中経過はそのまま残す


def test_e5_dnf_mixed_and_ranking():
    """レーン2が1周でCO、他2台は3周完走 → レーン2のみDNFで末尾。完走車は通常表示。"""
    events = make_events_norot({2: 1}, target_laps=3)  # レーン2だけ1周で停止
    race = build_race(LAYOUT_NOROT, events, target_laps=3)

    m1, m2, m3 = race.machines[1], race.machines[2], race.machines[3]
    assert m1.dnf is False and m1.completed_laps == 3 and m1.total_time_us is not None
    assert m3.dnf is False and m3.completed_laps == 3 and m3.total_time_us is not None
    assert m2.dnf is True and m2.completed_laps == 1 and m2.total_time_us is None
    assert len(m2.laps) == 1               # 途中経過（1周ぶん）は残る

    ranking = race.ranking()
    assert ranking[-1].start_lane == 2                      # DNFは末尾
    assert {r.start_lane for r in ranking[:2]} == {1, 3}    # 完走2台が上位


def test_e5_dnf_partial_progress_more_laps_ranked_higher():
    """DNF車どうしは周回数の多い順（途中経過を活かして並べる）。"""
    events = make_events_norot({1: 1, 2: 2, 3: 0}, target_laps=3)
    race = build_race(LAYOUT_NOROT, events, target_laps=3)
    for m in race.machines.values():
        assert m.dnf is True               # 全車3周未達＝DNF
    order = [m.start_lane for m in race.ranking()]
    assert order == [2, 1, 3]              # 周回数 2 > 1 > 0 の順


# ---------------------------------------------------------------------------
# D7/E6(24.34)：予定モードと実測(green_t_us)の食い違い判定
# ---------------------------------------------------------------------------

from app.domain.race_builder import mode_mismatch


def test_e6_mode_match_f1():
    """予定F1式・緑あり → 一致（警告なし）。"""
    assert mode_mismatch("f1", green_t_us=1_000_000) is False


def test_e6_mode_match_run():
    """予定走行式・緑なし → 一致（警告なし）。"""
    assert mode_mismatch("run", green_t_us=None) is False


def test_e6_mode_mismatch_f1_but_no_green():
    """予定F1式なのに緑なし（出し忘れ） → 食い違い。"""
    assert mode_mismatch("f1", green_t_us=None) is True


def test_e6_mode_mismatch_run_but_green():
    """予定走行式なのに緑あり（誤操作） → 食い違い。"""
    assert mode_mismatch("run", green_t_us=1_000_000) is True


def test_e6_no_planned_mode_never_warns():
    """予定未設定(None) → 照合しない（緑の有無に関わらずFalse）。"""
    assert mode_mismatch(None, green_t_us=None) is False
    assert mode_mismatch(None, green_t_us=1_000_000) is False


def test_e6_unknown_planned_mode_never_warns():
    """未知の予定値 → 照合しない（安全側）。"""
    assert mode_mismatch("bogus", green_t_us=None) is False
    assert mode_mismatch("", green_t_us=1_000_000) is False
