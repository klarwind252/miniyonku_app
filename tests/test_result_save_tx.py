"""結果保存ハンドラ heat_result_save（transaction() 適用済み）の機能・原子性テスト。

  - 正常系: 総当たりの1レース結果を保存すると heat_results が入り、
            heats.status が 'done' になり、順位計算にも反映される
  - 異常系: 保存途中で例外が起きると transaction() によりロールバックされ、
            heat_results は空、heats.status は 'done' にならない（半端な確定が残らない）

request.form() は最小スタブで代替する（ハンドラは form.get() しか使わない）。
pytest-asyncio 非依存（asyncio.run）。init_db ログは抑制。
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
from app.presentation.routers import qualifying as q  # noqa: E402
from app.presentation.routers.qualifying import _calc_standings_rr  # noqa: E402


class _FakeForm(dict):
    def get(self, k, default=""):
        return super().get(k, default)


class _FakeRequest:
    def __init__(self, data):
        self._form = _FakeForm(data)

    async def form(self):
        return self._form


async def _seed_one_roundrobin_heat(tmpdir):
    """総当たり・2エントリー・1ヒート（2レーン）のDBを作り、lane_id を返す。"""
    path = os.path.join(tmpdir, "t.db")
    with contextlib.redirect_stdout(io.StringIO()):
        await init_db(path)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute(
        "INSERT INTO tournaments (id,name,date,qualifying_type,status,lane_count) "
        "VALUES (1,'T','2026-07-10','roundrobin','active',2)")
    for i in (1, 2):
        await db.execute("INSERT INTO racers (id,name) VALUES (?,?)", (i, f"R{i}"))
        await db.execute(
            "INSERT INTO entries (id,tournament_id,racer_id,status,entry_order) "
            "VALUES (?,1,?,'active',?)", (i, i, i))
    await db.execute(
        "INSERT INTO heats (id,tournament_id,heat_no,group_no,status) "
        "VALUES (1,1,1,0,'pending')")
    await db.execute("INSERT INTO heat_lanes (id,heat_id,lane_no,entry_id) VALUES (10,1,1,1)")
    await db.execute("INSERT INTO heat_lanes (id,heat_id,lane_no,entry_id) VALUES (11,1,2,2)")
    await db.commit()
    return db


async def _scalar(db, sql, *params):
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
        return row[0] if row else None


def test_heat_result_save_persists_and_marks_done():
    tmp = tempfile.mkdtemp()
    try:
        async def go():
            db = await _seed_one_roundrobin_heat(tmp)
            try:
                # lane 10（entry 1）を勝ちに
                req = _FakeRequest({"win_10": "1", "time_10": "", "time_11": ""})
                await q.heat_result_save(1, 1, req, db)

                n = await _scalar(db, "SELECT COUNT(*) FROM heat_results")
                status = await _scalar(db, "SELECT status FROM heats WHERE id=1")
                win10 = await _scalar(db, "SELECT win FROM heat_results WHERE heat_lane_id=10")
                win11 = await _scalar(db, "SELECT win FROM heat_results WHERE heat_lane_id=11")
                standings = await _calc_standings_rr(1, db)
                return n, status, win10, win11, standings
            finally:
                await db.close()
        n, status, win10, win11, standings = asyncio.run(go())
        assert n == 2                 # 2レーン分の結果
        assert status == "done"       # ヒートが確定
        assert win10 == 1 and win11 == 0
        ranks = {r["entry_id"]: r["rank"] for r in standings}
        assert ranks[1] == 1 and ranks[2] == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_heat_result_save_rolls_back_on_failure():
    tmp = tempfile.mkdtemp()
    try:
        async def go():
            db = await _seed_one_roundrobin_heat(tmp)
            try:
                real_exec = db.execute
                state = {"n": 0}

                def patched(*a, **k):
                    sql = a[0] if a else ""
                    if "INSERT INTO heat_results" in sql:
                        state["n"] += 1
                        if state["n"] == 2:
                            raise RuntimeError("boom mid-save")
                    return real_exec(*a, **k)

                db.execute = patched
                req = _FakeRequest({"win_10": "1", "time_10": "", "time_11": ""})
                with pytest.raises(RuntimeError):
                    await q.heat_result_save(1, 1, req, db)
                db.execute = real_exec

                n = await _scalar(db, "SELECT COUNT(*) FROM heat_results")
                status = await _scalar(db, "SELECT status FROM heats WHERE id=1")
                return n, status
            finally:
                await db.close()
        n, status = asyncio.run(go())
        # ロールバック：結果は残らず、ヒートも done にならない
        assert n == 0
        assert status == "pending"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
