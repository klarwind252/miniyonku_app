"""
rotation.py の単体テスト（機材ゼロ・DBゼロで実行できる）

ユーザーが最初に説明した挙動をそのままテストにする:
  1コーススタート → 1周目に2コース、2周目に3コース、3周目に1コースへ戻る
  （レイアウト: S/G → SQ → LC → SQ、rot_total=1）
"""

import pytest

from app.domain.rotation import (
    LayoutElement,
    build_course,
    expected_lane,
    expected_sg_lane,
    identify_start_lane,
    validate_layout,
)


# ---------------------------------------------------------------------------
# build_course: レイアウトから累積ずれを導出できるか
# ---------------------------------------------------------------------------

def test_build_course_basic_layout():
    """S/G → SQ(6) → LC → SQ(7) のレイアウト。

    S/G:   累積LC 0
    SQ(6): 累積LC 0（LCの前）
    SQ(7): 累積LC 1（LCの後）
    rot_total = 1
    """
    layout = [
        LayoutElement("SG", node_id=6),
        LayoutElement("SQ", node_id=0),
        LayoutElement("LC"),
        LayoutElement("SQ", node_id=1),
    ]
    course = build_course(layout)

    assert course.rot_total == 1
    assert course.lc_count == 1
    assert len(course.gates) == 3

    sg, sq_first, sq_second = course.gates
    assert sg.kind == "SG" and sg.rot_to_gate == 0
    assert sq_first.node_id == 0 and sq_first.rot_to_gate == 0
    assert sq_second.node_id == 1 and sq_second.rot_to_gate == 1


def test_build_course_two_lc():
    """S/G のみ + LC×2（SQなし）。rot_total=2。"""
    layout = [
        LayoutElement("SG", node_id=6),
        LayoutElement("LC"),
        LayoutElement("LC"),
    ]
    course = build_course(layout)
    assert course.rot_total == 2
    assert len(course.gates) == 1  # S/Gのみ


# ---------------------------------------------------------------------------
# ユーザーが説明した「1→2→3コース」の挙動（順方向）
# ---------------------------------------------------------------------------

def test_rotation_story_one_lc():
    """rot_total=1 のとき:
      1コーススタート → 1周目S/G完了で2、2周目で3、3周目で1に戻る
      2コーススタート → 3, 1, 2
      3コーススタート → 1, 2, 3
    S/G完了は passing=k（k周目完了）で見る。
    """
    rot_total = 1
    # (start_lane) -> [1周目完了, 2周目完了, 3周目完了]
    expected = {
        1: [2, 3, 1],
        2: [3, 1, 2],
        3: [1, 2, 3],
    }
    for start_lane, laps in expected.items():
        for k, want in enumerate(laps, start=1):
            got = expected_sg_lane(start_lane, passing=k, rot_total=rot_total)
            assert got == want, (
                f"start={start_lane} {k}周目完了: expected {want}, got {got}"
            )


def test_rotation_story_three_laps_returns_home():
    """3周でスタートレーンに戻る（rot_total=1 は3周で一巡）。"""
    rot_total = 1
    for start_lane in (1, 2, 3):
        after_3 = expected_sg_lane(start_lane, passing=3, rot_total=rot_total)
        assert after_3 == start_lane


def test_start_passing_is_start_lane():
    """passing=0（スタート打刻）は必ず start_lane と一致する。"""
    for start_lane in (1, 2, 3):
        assert expected_sg_lane(start_lane, passing=0, rot_total=1) == start_lane


# ---------------------------------------------------------------------------
# セクションゲートでの期待レーン（layout: S/G → SQ → LC → SQ）
# ---------------------------------------------------------------------------

def test_expected_lane_at_section_gates():
    """1コーススタートのマシン。
      SQ(rot_to_gate=0): 1周目=1, 2周目=2, 3周目=3
      SQ(rot_to_gate=1): 1周目=2, 2周目=3, 3周目=1
    """
    start_lane = 1
    # 最初のSQ（LCの前・rot_to_gate=0）
    assert expected_lane(start_lane, lap=1, rot_to_gate=0, rot_total=1) == 1
    assert expected_lane(start_lane, lap=2, rot_to_gate=0, rot_total=1) == 2
    assert expected_lane(start_lane, lap=3, rot_to_gate=0, rot_total=1) == 3
    # 次のSQ（LCの後・rot_to_gate=1）
    assert expected_lane(start_lane, lap=1, rot_to_gate=1, rot_total=1) == 2
    assert expected_lane(start_lane, lap=2, rot_to_gate=1, rot_total=1) == 3
    assert expected_lane(start_lane, lap=3, rot_to_gate=1, rot_total=1) == 1


# ---------------------------------------------------------------------------
# 逆方向：同定（記録レーン → スタートレーン）
# ---------------------------------------------------------------------------

def test_identify_is_inverse_of_expected():
    """identify_start_lane は expected_lane の逆関数。全組合せで往復一致。"""
    for rot_total in (1, 2):
        for rot_to_gate in (0, 1, 2):
            for start_lane in (1, 2, 3):
                for lap in (1, 2, 3):
                    obs = expected_lane(start_lane, lap, rot_to_gate, rot_total)
                    back = identify_start_lane(obs, lap, rot_to_gate, rot_total)
                    assert back == start_lane


def test_identify_concrete_example():
    """SQ(rot_to_gate=1)で、2周目に3コースを記録 → 1コーススタートと同定。"""
    got = identify_start_lane(observed_lane=3, lap=2, rot_to_gate=1, rot_total=1)
    assert got == 1


# ---------------------------------------------------------------------------
# バリデーション（14章 DA8）
# ---------------------------------------------------------------------------

def _layout(sg=1, sq=0, lc=0, assign=True):
    """テスト用レイアウト生成。sg/sq/lc の数を指定。assign=Falseで未割当を作る。"""
    els: list[LayoutElement] = []
    node = 6
    for _ in range(sg):
        els.append(LayoutElement("SG", node_id=(6 if assign else None)))
    for i in range(sq):
        els.append(LayoutElement("SQ", node_id=(i if assign else None)))
    for _ in range(lc):
        els.append(LayoutElement("LC"))
    return els


def test_validate_ok_normal():
    """S/G 1個 + SQ 2個 + LC 1個 → エラーなし・確定可。"""
    layout = _layout(sg=1, sq=2, lc=1)
    r = validate_layout(layout)
    assert r.can_commit
    assert r.errors == []


def test_validate_sg_missing_is_error():
    layout = _layout(sg=0, sq=1, lc=1)
    r = validate_layout(layout)
    assert not r.can_commit
    assert any(i.code == "sg_missing" for i in r.errors)


def test_validate_lc_multiple_of_three_is_error():
    """LC=3 はエラー（1周で元レーンに戻る）。"""
    layout = _layout(sg=1, sq=1, lc=3)
    r = validate_layout(layout)
    assert not r.can_commit
    assert any(i.code == "lc_multiple_of_lanes" for i in r.errors)


def test_validate_lc_six_is_error():
    """LC=6 もエラー（3の倍数）。"""
    layout = _layout(sg=1, sq=1, lc=6)
    r = validate_layout(layout)
    assert any(i.code == "lc_multiple_of_lanes" for i in r.errors)


def test_validate_lc_zero_is_warning_but_commitable():
    """LC=0 は警告だが確定可（検証用途）。"""
    layout = _layout(sg=1, sq=1, lc=0)
    r = validate_layout(layout)
    assert r.can_commit  # 警告なので通せる
    assert any(i.code == "lc_zero" for i in r.warnings)
    # 警告文に用途の説明が含まれること
    w = next(i for i in r.warnings if i.code == "lc_zero")
    assert "検証" in w.message


def test_validate_sq_five_warns_no_tft_sector():
    """SQ=5 は警告（TFTセクター非表示）だが確定可。"""
    layout = _layout(sg=1, sq=5, lc=1)
    r = validate_layout(layout)
    assert r.can_commit
    assert any(i.code == "sq_no_tft_sector" for i in r.warnings)


def test_validate_sq_over_max_is_error():
    """SQ=7 は上限6超でエラー。"""
    layout = _layout(sg=1, sq=7, lc=1)
    r = validate_layout(layout)
    assert not r.can_commit
    assert any(i.code == "sq_over_max" for i in r.errors)


def test_validate_unassigned_gate_is_error():
    """機器未割当のゲートがあるとエラー。"""
    layout = _layout(sg=1, sq=2, lc=1, assign=False)
    r = validate_layout(layout)
    assert not r.can_commit
    assert any(i.code == "gate_unassigned" for i in r.errors)


def test_validate_duplicate_node_is_error():
    """同じ機体番号を2箇所に割り当てるとエラー。"""
    layout = [
        LayoutElement("SG", node_id=6),
        LayoutElement("SQ", node_id=0),
        LayoutElement("LC"),
        LayoutElement("SQ", node_id=0),  # 重複！
    ]
    r = validate_layout(layout)
    assert not r.can_commit
    assert any(i.code == "node_duplicated" for i in r.errors)
