"""組確定ハンドラ（transaction() 適用済み）の機能・原子性テスト。

qualifying_generate を実スキーマ（init_db）の実DBへ通し、
  - 正常系: 総当たりで想定どおりのヒート/レーンが生成される
  - 異常系: 書き込み途中で例外が起きても、transaction() により生成が
            まるごとロールバックされ半端な状態が残らない
を確認する。トランザクション化の横展開が実挙動を壊していないことの裏取り。

pytest-asyncio 非依存（asyncio.run）。init_db のログは抑制。
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


async def _seed_roundrobin(tmpdir, n_entries):
    path = os.path.join(tmpdir, "t.db")
    with contextlib.redirect_stdout(io.StringIO()):
        await init_db(path)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute(
        "INSERT INTO tournaments (id,name,date,qualifying_type,status,lane_count) "
        "VALUES (1,'T','2026-07-10','roundrobin','active',2)")
    for i in range(1, n_entries + 1):
        await db.execute("INSERT INTO racers (id,name) VALUES (?,?)", (i, f"R{i}"))
        await db.execute(
            "INSERT INTO entries (id,tournament_id,racer_id,status,entry_order) "
            "VALUES (?,1,?,'active',?)", (i, i, i))
    await db.commit()
    return db


async def _count(db, sql):
    async with db.execute(sql) as cur:
        return (await cur.fetchone())["c"]


def test_qualifying_generate_roundrobin_creates_all_pairs():
    tmp = tempfile.mkdtemp()
    try:
        async def go():
            db = await _seed_roundrobin(tmp, 4)
            try:
                await q.qualifying_generate(1, db)
                heats = await _count(db, "SELECT COUNT(*) c FROM heats WHERE tournament_id=1")
                lanes = await _count(
                    db, "SELECT COUNT(*) c FROM heat_lanes hl "
                        "JOIN heats h ON h.id=hl.heat_id WHERE h.tournament_id=1")
                return heats, lanes
            finally:
                await db.close()
        heats, lanes = asyncio.run(go())
        assert heats == 6      # C(4,2)
        assert lanes == 12     # 1レースにつき2レーン
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_qualifying_generate_rolls_back_on_midwrite_failure():
    tmp = tempfile.mkdtemp()
    try:
        async def go():
            db = await _seed_roundrobin(tmp, 4)
            try:
                # heat_lanes への 2 回目の INSERT で例外を注入
                real_exec = db.execute
                state = {"n": 0}

                def patched(*a, **k):
                    sql = a[0] if a else ""
                    if "INSERT INTO heat_lanes" in sql:
                        state["n"] += 1
                        if state["n"] == 2:
                            raise RuntimeError("boom mid-insert")
                    return real_exec(*a, **k)

                db.execute = patched
                with pytest.raises(RuntimeError):
                    await q.qualifying_generate(1, db)
                db.execute = real_exec

                heats = await _count(db, "SELECT COUNT(*) c FROM heats WHERE tournament_id=1")
                lanes = await _count(db, "SELECT COUNT(*) c FROM heat_lanes")
                return heats, lanes
            finally:
                await db.close()
        heats, lanes = asyncio.run(go())
        # ロールバックされ、生成物は一切残らない
        assert heats == 0
        assert lanes == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
