"""互換シム：実装は app/presentation/routers/admin.py へ移設済み。
main.py が router を旧パスで import するため再エクスポートする。移行完了後に削除。"""
from app.presentation.routers.admin import router  # noqa: F401
