"""transaction() ヘルパの機能テスト（aiosqlite 実DBで commit/rollback を検証）。

pytest-asyncio に依存しないよう、各テストは asyncio.run() でコルーチンを回す。
検証点:
  1. ガード用 SELECT のあとに transaction()（内部で BEGIN）へ入れる
     ＝ "cannot start a transaction within a transaction" にならない。
  2. 正常終了で commit され、変更が永続する。
  3. ブロック内で例外が出ると rollback され、変更が破棄される。
"""
import asyncio

import pytest

pytest.importorskip("aiosqlite")
import aiosqlite  # noqa: E402

from app.infrastructure.db.tx import transaction  # noqa: E402


async def _make_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    await db.execute("INSERT INTO t (id, v) VALUES (1, 'seed')")
    await db.commit()
    return db


async def _count(db):
    async with db.execute("SELECT COUNT(*) AS c FROM t") as cur:
        return (await cur.fetchone())["c"]


def test_commit_persists_after_guard_select():
    async def run():
        db = await _make_db()
        try:
            # ガード用の SELECT（書き込み前）→ その後 transaction へ入れること
            async with db.execute("SELECT id FROM t WHERE id=1") as cur:
                assert await cur.fetchone() is not None

            async with transaction(db):
                await db.execute("DELETE FROM t WHERE id=1")
                await db.execute("INSERT INTO t (id, v) VALUES (2, 'a'), (3, 'b')")

            # commit 済み：seed 削除 + 2件挿入 = 2件
            assert await _count(db) == 2
            async with db.execute("SELECT v FROM t ORDER BY id") as cur:
                rows = [r["v"] for r in await cur.fetchall()]
            assert rows == ["a", "b"]
        finally:
            await db.close()

    asyncio.run(run())


def test_exception_rolls_back():
    async def run():
        db = await _make_db()
        try:
            with pytest.raises(RuntimeError):
                async with transaction(db):
                    await db.execute("DELETE FROM t")            # seed を消す
                    await db.execute("INSERT INTO t (id, v) VALUES (9, 'x')")
                    raise RuntimeError("boom")                    # 途中失敗

            # rollback により元の状態（seed 1件のみ）に戻っている
            assert await _count(db) == 1
            async with db.execute("SELECT v FROM t") as cur:
                assert (await cur.fetchone())["v"] == "seed"
        finally:
            await db.close()

    asyncio.run(run())
