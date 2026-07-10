"""
書き込みの原子化ヘルパ。

現状の各ルーターは「複数の execute → 末尾で1回 commit」という形が多く、
途中で例外が出た場合の巻き戻し（rollback）が明示されていない。1つの論理操作
（組確定→結果登録→順位再計算 等）が複数コミットに分割されている箇所もあり、
半端な状態がDBに残り得る（bracket_repair.py の起動時修復はこの後始末）。

このヘルパで書き込みハンドラを1トランザクションに囲い、成功時のみ commit、
例外時は必ず rollback する。段階移行のため、まず整合性が崩れやすい
組確定・ラウンド操作・結果一括更新のハンドラから適用していくことを推奨する。

使用例:
    from app.infrastructure.db.tx import transaction

    async with transaction(db):
        await _clear_existing_heats(tid, db)
        await _insert_generated_heats(tid, db, schedule)
    schedule_publish()

注意:
    - ブロック内に既存の `await db.commit()` が残っていると早期コミットになるため、
      ブロックへ移す際はハンドラ内の個別 commit を削除すること。
    - aiosqlite は既定で暗黙トランザクションを張るが、本ヘルパは明示 BEGIN で
      境界を1つに固定し、rollback を保証する点が異なる。
"""
from contextlib import asynccontextmanager


@asynccontextmanager
async def transaction(db):
    """複数ステップの書き込みを1トランザクションにまとめる。

    成功時のみ commit、例外時は rollback して再送出する。
    """
    try:
        await db.execute("BEGIN")
        yield db
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        raise
