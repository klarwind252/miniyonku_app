"""予選スケジュール生成の characterization / 不変条件テスト。

方針:
  - 決定的関数（calc_points, generate_heat_schedule, generate_roundrobin_schedule）
    は golden 値で固定する。
  - 乱数を使う関数（generate_point_schedule, generate_heat_roundrobin_schedule）は、
    random の出力列が Python バージョンで変わり得るため golden 値では固定せず、
    「意味的な不変条件」＋「同一シードでの再現性」で固定する。

これらの関数を含む qualifying ルーターは FastAPI 等に依存するため、未導入環境では
モジュールごとスキップする（domain テストは影響を受けない）。
"""
import random
import itertools

import pytest

pytest.importorskip("fastapi")  # Webスタック未導入環境ではこのファイルをスキップ

from app.presentation.routers.qualifying import (  # noqa: E402
    calc_points,
    generate_heat_schedule,
    generate_roundrobin_schedule,
    generate_point_schedule,
    generate_heat_roundrobin_schedule,
)


# ---- calc_points（決定的・golden）--------------------------------------
def test_calc_points_golden():
    assert [calc_points(r) for r in range(0, 8)] == [0, 10, 7, 5, 3, 2, 1, 0]


# ---- generate_heat_schedule（決定的・golden）---------------------------
def test_generate_heat_schedule_golden():
    assert generate_heat_schedule([], 3) == []
    assert generate_heat_schedule([1], 3) == [[1]]
    assert generate_heat_schedule([1, 2, 3], 3) == [[1, 2, 3]]
    assert generate_heat_schedule([1, 2, 3, 4], 3) == [
        [1, 2, 3, 4], [2, 3, 4, 1], [3, 4, 1, 2]
    ]
    assert generate_heat_schedule([1, 2, 3, 4, 5, 6, 7], 3) == [
        [1, 2, 3], [4, 5, 6, 7], [2, 3, 4], [5, 6, 7, 1], [3, 4, 5], [6, 7, 1, 2]
    ]


def test_generate_heat_schedule_covers_all_entries():
    ids = list(range(1, 10))
    heats = generate_heat_schedule(ids, 3)
    seen = set()
    for h in heats:
        seen.update(h)
    assert seen == set(ids)


# ---- generate_roundrobin_schedule（決定的・golden + 不変条件）----------
def test_generate_roundrobin_schedule_golden():
    assert generate_roundrobin_schedule([]) == []
    assert generate_roundrobin_schedule([1]) == []
    assert generate_roundrobin_schedule([1, 2]) == [(1, 1, 1, 2, 2)]
    assert generate_roundrobin_schedule([1, 2, 3]) == [
        (1, 1, 1, 2, 2), (2, 1, 3, 2, 1), (3, 1, 2, 2, 3)
    ]


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7, 8])
def test_generate_roundrobin_covers_every_pair_once(n):
    ids = list(range(1, n + 1))
    sched = generate_roundrobin_schedule(ids)
    # 戻り値: (race_no, laneA, idA, laneB, idB)
    pairs = [frozenset((row[2], row[4])) for row in sched]
    expected = [frozenset(c) for c in itertools.combinations(ids, 2)]
    # 総当たりは全ペアをちょうど1回ずつ含む
    assert len(pairs) == len(expected)
    assert sorted(map(sorted, pairs)) == sorted(map(sorted, expected))
    # 常に1・2コースのみ使用（3コース目は使わない）
    for row in sched:
        assert row[1] == 1 and row[3] == 2


# ---- generate_point_schedule（乱数・不変条件 + シード再現性）-----------
@pytest.mark.parametrize("n,rounds,lanes", [(6, 3, 3), (8, 4, 4), (5, 2, 3)])
def test_generate_point_schedule_round_covers_all(n, rounds, lanes):
    ids = list(range(1, n + 1))
    random.seed(0)
    sched = generate_point_schedule(ids, rounds, lanes)
    assert len(sched) == rounds
    for rnd in sched:
        flat = [e for group in rnd for e in group]
        # 各ラウンドで全 entry がちょうど1回ずつ登場する
        assert sorted(flat) == ids


def test_generate_point_schedule_seed_reproducible():
    ids = list(range(1, 7))
    random.seed(42)
    a = generate_point_schedule(ids, 3, 3)
    random.seed(42)
    b = generate_point_schedule(ids, 3, 3)
    assert a == b


# ---- generate_heat_roundrobin_schedule（乱数・構造 + シード再現性）-----
def test_generate_heat_roundrobin_structure_and_reproducible():
    ids = list(range(1, 7))
    random.seed(7)
    a = generate_heat_roundrobin_schedule(ids, 2, 2, 2)
    random.seed(7)
    b = generate_heat_roundrobin_schedule(ids, 2, 2, 2)
    assert a == b                      # 同一シードで再現
    assert isinstance(a, list) and a   # 空でない list
    for row in a:
        assert {"round_no", "group_no", "heat_no", "slots"} <= set(row.keys())
        assert isinstance(row["slots"], list)
