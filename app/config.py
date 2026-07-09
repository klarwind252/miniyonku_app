"""互換シム：実装は app/core/config.py へ移設済み。移行完了後に削除。"""
from app.core.config import *  # noqa: F401,F403
from app.core.config import (  # noqa: F401
    DEPLOY_MODE, IS_CLOUD, PUBLIC_BASE_URL, ADMIN_TOKEN, VIEW_TOKEN,
    PUBLIC_HTML_DIR, ADMIN_COOKIE, VIEW_COOKIE,
    HEAT_TOURNAMENT_TYPES, GARAPPA_STORE_NAME,
    admin_cookie_name, view_cookie_name, inject_globals,
)