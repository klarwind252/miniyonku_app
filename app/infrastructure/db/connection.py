"""DB接続の提供（旧 app/models/database.py の接続部分を分離）。

スキーマ定義（init_db）は schema.py に分離した。
接続の取得だけがここの責務。
"""
import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "../../../data/miniyonku.db")
DB_PATH = os.path.abspath(DB_PATH)


def current_db_path() -> str:
    """現在のリクエストが属する店舗のDBパスを返す。

    複数店舗化（クラウド版）では StoreResolverMiddleware が ContextVar に店舗を
    セットしているため、その店舗のDBを開く。未設定（オンプレ／単一店舗）では
    従来どおり既定の DB_PATH を返す。
    """
    try:
        from app.core.store_context import current_store
        store = current_store.get()
        if store and store.db_path:
            return store.db_path
    except Exception:
        pass
    return DB_PATH


async def get_db():
    db = await aiosqlite.connect(current_db_path())
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        await db.close()