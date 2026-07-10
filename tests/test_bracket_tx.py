"""bracket 確定系（transaction() 適用済み）の機能・原子性テスト。

対象: bracket_clear_final_result（決勝・3位決定戦の順位結果のみ削除）。
  - 正常系: 対象グループの bracket_slot_ranks と bracket_results が両方消える
  - 異常系: 2つ目の削除で例外→ transaction() で巻き戻り、両方残る（片方だけ消えない）

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
from app.presentation.routers import bracket as b  # noqa: E402


async def _seed_final_group(tmpdir):
    """round_type='final' のグループに順位結果と勝者を1件ずつ入れる。group_id を返す。"""
    path = os.path.join(tmpdir, "t.db")
    with contextlib.redirect_stdout(io.StringIO()):
        await init_db(path)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute(
        "INSERT INTO tournaments (id,name,date,status) VALUES (1,'T','2026-07-10','active')")
    await db.execute(
        "INSERT INTO bracket_rounds (id,tournament_id,round_no,round_type) "
        "VALUES (1,1,1,'final')")
    await db.execute(
        "INSERT INTO bracket_groups (id,round_id,group_no) VALUES (100,1,1)")
    await db.execute(
        "INSERT INTO bracket_slot_ranks (group_id,slot_id,rank) VALUES (100,1,1)")
    await db.execute(
        "INSERT INTO bracket_results (group_id,winner_slot_id) VALUES (100,1)")
    await db.commit()
    return db


async def _scalar(db, sql, *p):
    async with db.execute(sql, p) as cur:
        row = await cur.fetchone()
        return row[0] if row else None


def test_clear_final_result_deletes_both():
    tmp = tempfile.mkdtemp()
    try:
        async def go():
            db = await _seed_final_group(tmp)
            try:
                await b.bracket_clear_final_result(1, db)
                ranks = await _scalar(db, "SELECT COUNT(*) FROM bracket_slot_ranks WHERE group_id=100")
                res = await _scalar(db, "SELECT COUNT(*) FROM bracket_results WHERE group_id=100")
                return ranks, res
            finally:
                await db.close()
        ranks, res = asyncio.run(go())
        assert ranks == 0 and res == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_clear_final_result_rolls_back_on_failure():
    tmp = tempfile.mkdtemp()
    try:
        async def go():
            db = await _seed_final_group(tmp)
            try:
                real_exec = db.execute

                def patched(*a, **k):
                    sql = a[0] if a else ""
                    if "DELETE FROM bracket_results" in sql:
                        raise RuntimeError("boom on 2nd delete")
                    return real_exec(*a, **k)

                db.execute = patched
                with pytest.raises(RuntimeError):
                    await b.bracket_clear_final_result(1, db)
                db.execute = real_exec

                ranks = await _scalar(db, "SELECT COUNT(*) FROM bracket_slot_ranks WHERE group_id=100")
                res = await _scalar(db, "SELECT COUNT(*) FROM bracket_results WHERE group_id=100")
                return ranks, res
            finally:
                await db.close()
        ranks, res = asyncio.run(go())
        # 片方だけ消えず、両方残る（原子性）
        assert ranks == 1 and res == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
