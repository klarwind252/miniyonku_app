"""互換シム：実装は infrastructure/db へ移設済み。
qualifying / bracket / tournaments / admin / viewer 等の未移行モジュールが
旧パスで import しているため残す。全モジュール移行後に削除する。"""
from app.infrastructure.db.connection import DB_PATH, current_db_path, get_db  # noqa: F401
from app.infrastructure.db.schema import init_db  # noqa: F401