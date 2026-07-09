"""互換シム：実装は app/presentation/routers/viewer.py へ移設済み。

main.py が router を、services/public_html.py が _host_states を旧パスで
import するため再エクスポートする。全モジュール移行後に削除する。
"""
from app.presentation.routers.viewer import router, _host_states  # noqa: F401
