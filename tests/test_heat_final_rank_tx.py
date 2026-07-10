"""heat_final_rank（データ書き込み部を transaction() 適用）の機能・原子性テスト。

  - 正常系: 順位を入れると heat_finals.deciding_rank が入り、advance 以内なら
            entries.advanced=1 になる（DDLマイグレーションはトランザクション外で先行）
  - 異常系: advanced 更新で例外→ deciding_rank・advanced とも巻き戻る

request.form() は最小スタブ。pytest-asyncio 非依存。init_db ログ抑制。
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

from app.presentation.routers import qualifying as q  # noqa: E402
from app.infrastructure.db.schema import init_db  # noqa: E402


class _FakeForm(dict):
    def get(self, k, default=""):
        return super().get(k, default)


class _FakeRequest:
    def __init__(self, data):
        self._f = _FakeForm(data)

    async def form(self):
        return self._f


async def _seed(tmpdir):
    path = os.path.join(tmpdir, "t.db")
    with contextlib.redirect_stdout(io.StringIO()):
        await init_db(path)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute(
        "INSERT INTO tournaments (id,name,date,status,qual_heat_final,"
        "qual_heat_final_advance,qual_heat_count) VALUES (1,'T','2026-07-10','active',1,1,1)")
    for i in (1, 2):
        await db.execute("INSERT INTO racers (id,name) VALUES (?,?)", (i, f"R{i}"))
        await db.execute(
            "INSERT INTO entries (id,tournament_id,racer_id,status,entry_order) "
            "VALUES (?,1,?,'active',?)", (i, i, i))
        await db.execute(
            "INSERT INTO heat_finals (tournament_id,round_no,group_no,slot_no,entry_id,final_type) "
            "VALUES (1,1,0,?,?,'heat')", (i, i))
    await db.commit()
    return db


async def _scalar(db, sql, *p):
    async with db.execute(sql, p) as cur:
        row = await cur.fetchone()
        return row[0] if row else None


def test_heat_final_rank_sets_deciding_and_advanced():
    tmp = tempfile.mkdtemp()
    try:
        async def go():
            db = await _seed(tmp)
            try:
                await q.heat_final_rank(1, _FakeRequest(
                    {"round_no": "1", "entry_id": "1", "rank": "1"}), db)
                dr = await _scalar(
                    db, "SELECT deciding_rank FROM heat_finals WHERE entry_id=1")
                adv = await _scalar(db, "SELECT advanced FROM entries WHERE id=1")
                return dr, adv
            finally:
                await db.close()
        dr, adv = asyncio.run(go())
        assert dr == 1
        assert adv == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_heat_final_rank_rolls_back_on_failure():
    tmp = tempfile.mkdtemp()
    try:
        async def go():
            db = await _seed(tmp)
            try:
                # 先にマイグレーション（deciding_rank 追加）を済ませておく
                await db.execute("ALTER TABLE heat_finals ADD COLUMN deciding_rank INTEGER")
                await db.commit()

                real_exec = db.execute

                def patched(*a, **k):
                    sql = a[0] if a else ""
                    if "UPDATE entries SET advanced=1" in sql:
                        raise RuntimeError("boom on advanced update")
                    return real_exec(*a, **k)

                db.execute = patched
                with pytest.raises(RuntimeError):
                    await q.heat_final_rank(1, _FakeRequest(
                        {"round_no": "1", "entry_id": "1", "rank": "1"}), db)
                db.execute = real_exec

                dr = await _scalar(
                    db, "SELECT deciding_rank FROM heat_finals WHERE entry_id=1")
                adv = await _scalar(db, "SELECT advanced FROM entries WHERE id=1")
                return dr, adv
            finally:
                await db.close()
        dr, adv = asyncio.run(go())
        # 巻き戻り：順位も advanced も未設定のまま
        assert dr is None
        assert adv is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
