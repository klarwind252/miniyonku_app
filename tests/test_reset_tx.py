"""結果リセット heat_result_reset（transaction() 適用済み）の機能・原子性テスト。

  - 正常系: リセットで heat_results が消え、heats.status が 'prepare' になる
  - 異常系: 途中（status更新）で例外が起きると、先行の削除もロールバックされ、
            結果がそのまま残る（＝中途半端に一部だけ消えない）

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


async def _seed_done_heat(tmpdir):
    """結果入力済み・status='done' の1ヒート（2レーン）を作る。"""
    path = os.path.join(tmpdir, "t.db")
    with contextlib.redirect_stdout(io.StringIO()):
        await init_db(path)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute(
        "INSERT INTO tournaments (id,name,date,qualifying_type,status) "
        "VALUES (1,'T','2026-07-10','roundrobin','active')")
    await db.execute(
        "INSERT INTO heats (id,tournament_id,heat_no,group_no,status) "
        "VALUES (1,1,1,0,'done')")
    for lid, lane in ((10, 1), (11, 2)):
        await db.execute(
            "INSERT INTO heat_lanes (id,heat_id,lane_no,entry_id) VALUES (?,1,?,?)",
            (lid, lane, lane))
        await db.execute(
            "INSERT INTO heat_results (heat_lane_id, win) VALUES (?,?)", (lid, 1))
    await db.commit()
    return db


async def _scalar(db, sql, *p):
    async with db.execute(sql, p) as cur:
        row = await cur.fetchone()
        return row[0] if row else None


def test_reset_clears_results_and_sets_prepare():
    tmp = tempfile.mkdtemp()
    try:
        async def go():
            db = await _seed_done_heat(tmp)
            try:
                await q.heat_result_reset(1, 1, db)
                n = await _scalar(db, "SELECT COUNT(*) FROM heat_results")
                st = await _scalar(db, "SELECT status FROM heats WHERE id=1")
                return n, st
            finally:
                await db.close()
        n, st = asyncio.run(go())
        assert n == 0
        assert st == "prepare"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reset_rolls_back_on_failure():
    tmp = tempfile.mkdtemp()
    try:
        async def go():
            db = await _seed_done_heat(tmp)
            try:
                real_exec = db.execute

                def patched(*a, **k):
                    sql = a[0] if a else ""
                    if "UPDATE heats SET status='prepare'" in sql:
                        raise RuntimeError("boom before status update commit")
                    return real_exec(*a, **k)

                db.execute = patched
                with pytest.raises(RuntimeError):
                    await q.heat_result_reset(1, 1, db)
                db.execute = real_exec

                n = await _scalar(db, "SELECT COUNT(*) FROM heat_results")
                st = await _scalar(db, "SELECT status FROM heats WHERE id=1")
                return n, st
            finally:
                await db.close()
        n, st = asyncio.run(go())
        # 削除がロールバックされ、結果は残り、status も done のまま
        assert n == 2
        assert st == "done"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
