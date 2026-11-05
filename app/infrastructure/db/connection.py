"""DB接続の提供（旧 app/models/database.py の接続部分を分離）。

スキーマ定義（init_db）は schema.py に分離した。
接続の取得だけがここの責務。

────────────────────────────────────────────────────────────────────
【最適化メモ 20261105】
目的: ①速度 ②スケーラビリティ の改善。SQLの結果は一切変えない（＝動作不変）。

  追加した PRAGMA はいずれも「クエリ結果に影響しない・I/O効率だけを変える」もの:
    - cache_size       : ページキャッシュ拡大（ディスク読み戻し削減）
    - temp_store=MEMORY : 一時表/索引をRAMに（ORDER BY/GROUP BY等の高速化）
    - mmap_size        : 読み取りをmmap化（read(2)システムコール削減）

  WAL だけは on-disk ジャーナル方式を変える（-wal/-shm ファイルが増える・
  バックアップ時に checkpoint が要る）ため、既定OFFの環境変数 opt-in とした。
  M4_SQLITE_WAL=1 を設定して初めて有効化される＝未設定なら従来挙動と完全一致。
────────────────────────────────────────────────────────────────────
"""
import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "../../../data/miniyonku.db")
DB_PATH = os.path.abspath(DB_PATH)

# 速度にのみ影響する安全PRAGMA（クエリ結果は不変）。環境変数で上書き可。
_CACHE_KIB = int(os.getenv("M4_SQLITE_CACHE_KIB", "16384"))   # 既定16MiB（負値=KiB指定）
_MMAP_BYTES = int(os.getenv("M4_SQLITE_MMAP_BYTES", str(64 * 1024 * 1024)))  # 既定64MiB
_USE_WAL = os.getenv("M4_SQLITE_WAL", "0") == "1"             # 既定OFF＝従来挙動

# 「このプロセスでこのDBファイルに対し一度でも WAL 設定を流したか」を記録。
# WAL は接続ごとではなくDB単位で永続するため、毎リクエストの journal_mode 発行は不要。
_wal_applied: set[str] = set()


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
    path = current_db_path()
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    # ── 従来どおり（挙動に関わる設定）──
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA foreign_keys=ON")
    # ── 追加（速度のみ・結果不変）──
    await db.execute(f"PRAGMA cache_size=-{_CACHE_KIB}")   # 負値=KiB単位
    await db.execute("PRAGMA temp_store=MEMORY")
    await db.execute(f"PRAGMA mmap_size={_MMAP_BYTES}")
    # ── 任意（opt-in）：WALで「読みが書きをブロックしない」→同時アクセスに強い ──
    if _USE_WAL and path not in _wal_applied:
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            _wal_applied.add(path)
        except Exception:
            pass
    try:
        yield db
    finally:
        await db.close()
