"""順位計算 _calc_standings_rr の characterization テスト。

順位計算は DB 行を集計するため、本物の init_db() でテンポラリDB（実スキーマ）を作り、
最小データを投入して現状の出力を golden 値として固定する。以降 _calc_standings 系や
トランザクション化に手を入れたとき、順位や同率処理が変わればここが赤くなる。

pytest-asyncio に依存しないよう asyncio.run() でコルーチンを回す。
init_db は多数の移行ログを stdout に出すため抑制する。
"""
import os
import io
import asyncio
import tempfile
import shutil
import contextlib

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("aiosqlite")
import aiosqlite  # noqa: E402

from app.infrastructure.db.schema import init_db  # noqa: E402
from app.presentation.routers.qualifying import _calc_standings_rr  # noqa: E402


async def _build_db(tmpdir, wins_by_entry):
    """wins_by_entry = {entry_id: 勝ち数} の総当たり結果を持つDBを構築して返す。"""
    path = os.path.join(tmpdir, "t.db")
    with contextlib.redirect_stdout(io.StringIO()):   # 移行ログを抑制
        await init_db(path)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute(
        "INSERT INTO tournaments (id,name,date,qualifying_type,status) "
        "VALUES (1,'T','2026-07-10','roundrobin','active')"
    )
    for eid in wins_by_entry:
        await db.execute("INSERT INTO racers (id,name,yomi) VALUES (?,?,?)",
                         (eid, f"R{eid}", f"r{eid}"))
        await db.execute(
            "INSERT INTO entries (id,tournament_id,racer_id,status,entry_order,advanced) "
            "VALUES (?,1,?,'active',?,0)", (eid, eid, eid))
    await db.execute(
        "INSERT INTO heats (id,tournament_id,heat_no,group_no,round_no,status) "
        "VALUES (1,1,1,1,1,'done')")
    lane = 0
    for eid, w in wins_by_entry.items():
        for _ in range(w):
            lane += 1
            await db.execute(
                "INSERT INTO heat_lanes (id,heat_id,lane_no,entry_id) VALUES (?,1,?,?)",
                (lane, lane, eid))
            await db.execute(
                "INSERT INTO heat_results (heat_lane_id,win) VALUES (?,1)", (lane,))
    await db.commit()
    return db


def _run(wins_by_entry):
    tmp = tempfile.mkdtemp()
    try:
        async def go():
            db = await _build_db(tmp, wins_by_entry)
            try:
                res = await _calc_standings_rr(1, db)
            finally:
                await db.close()
            return res
        return asyncio.run(go())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ranks(res):
    return {r["entry_id"]: r["rank"] for r in res}


def test_standings_rr_tie_handling_golden():
    # 勝ち数 3,2,2,0 → 標準競技順位 1,2,2,4（同率は同順位、次は件数分飛ぶ）
    res = _run({1: 3, 2: 2, 3: 2, 4: 0})
    assert _ranks(res) == {1: 1, 2: 2, 3: 2, 4: 4}
    wins = {r["entry_id"]: r["wins"] for r in res}
    assert wins == {1: 3, 2: 2, 3: 2, 4: 0}


def test_standings_rr_strict_order_golden():
    # 全員異なる勝ち数 → 1,2,3,4
    res = _run({1: 3, 2: 2, 3: 1, 4: 0})
    assert _ranks(res) == {1: 1, 2: 2, 3: 3, 4: 4}


def test_standings_rr_all_zero_are_all_rank1():
    # 結果未入力（全員0勝）→ 全員同率1位
    res = _run({1: 0, 2: 0, 3: 0})
    assert set(_ranks(res).values()) == {1}


def test_standings_rr_invariants():
    res = _run({1: 5, 2: 5, 3: 3, 4: 1, 5: 0})
    # 件数一致
    assert len(res) == 5
    # 勝ち数の降順に並ぶ
    ws = [r["wins"] for r in res]
    assert ws == sorted(ws, reverse=True)
    # 順位は非減少
    rs = [r["rank"] for r in res]
    assert rs == sorted(rs)
    # 先頭は必ず1位
    assert res[0]["rank"] == 1
