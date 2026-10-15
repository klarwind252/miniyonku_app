"""M4LAPS ライセンス状態を request.state にセットする軽量ミドルウェア。

base.html のナビは全admin画面共通なので、各リクエストで「M4LAPSが有効か」を
知る必要がある。ここで request.state.m4laps_licensed をセットし、
テンプレートから {{ request.state.m4laps_licensed }} で参照できるようにする。

- オンプレ版（IS_CLOUD=False）では常に False（根本非表示）。
- クラウド版では、その店舗DBの app_settings を同期SQLiteで軽く読む。
  /admin 配下（スラッグ付き /<slug>/admin 含む）のときだけ判定する。

【設計方針】店舗DBの特定は、store_resolver の実行順や request.state / ContextVar の
伝播に一切依存しない。URLの先頭セグメントから registry で店舗を直接引くため、本
ミドルウェアが store_resolver の前後どちらで走っても、スラッグ付き・なし両方で
正しい店舗DBを読む（過去にあった「順序依存でスラッグ付き or なしの一方が出ない」
不具合を根本回避）。
"""

from __future__ import annotations

import sqlite3

from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import IS_CLOUD
from app.infrastructure.db.connection import DB_PATH

# store_resolver と同じ「アプリ予約プレフィックス」。先頭セグメントがこれ／空なら既定店舗。
_APP_PREFIXES = ("admin", "view", "static", "logo", "health", "api", "enter", "favicon")


def _read_licensed(db_path: str) -> bool:
    """同期SQLiteで app_settings の m4laps_licensed を読む（軽量・読み取りのみ）。"""
    try:
        con = sqlite3.connect(db_path, timeout=2.0)
        try:
            cur = con.execute(
                "SELECT value FROM app_settings WHERE key = 'm4laps_licensed'"
            )
            row = cur.fetchone()
            return bool(row) and row[0] == "1"
        finally:
            con.close()
    except Exception:
        return False


def _resolve_db_and_admin(request):
    """URLパスから (店舗db_path, is_admin) を独立に解決する。

    - 先頭セグメントが空／アプリ予約語（admin, view, api …）→ 既定店舗。
    - それ以外 → スラッグとして registry から該当店舗を引く（/<slug>/admin/...）。
    is_admin はスラッグ除去後の残りが /admin で始まるかで判定する。
    """
    raw = request.url.path or "/"
    parts = raw.lstrip("/").split("/")
    seg = parts[0] if parts and parts[0] else ""

    # --- スラッグ除去は純粋な文字列処理で先に確定（registry の成否に依存させない）---
    is_slug = bool(seg) and seg not in _APP_PREFIXES
    rest = ("/" + "/".join(parts[1:])) if is_slug else raw
    is_admin = rest.startswith("/admin")

    # --- 店舗DBの特定（registry 参照。失敗時は既定DBへフォールバック）---
    db_path = DB_PATH
    try:
        from app import registry
        store = registry.get_store_by_slug(seg) if is_slug else registry.get_default_store()
        if store is not None and getattr(store, "db_path", None):
            db_path = store.db_path
    except Exception:
        db_path = DB_PATH

    return db_path, is_admin


class M4lapsLicenseMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        licensed = False
        if IS_CLOUD:
            try:
                db_path, is_admin = _resolve_db_and_admin(request)
                if is_admin and db_path:
                    licensed = _read_licensed(db_path)
            except Exception:
                licensed = False
        request.state.m4laps_licensed = licensed
        return await call_next(request)


def add_m4laps_license(app):
    """クラウド版でのみ意味を持つ。オンプレでは常に False をセットするだけ。"""
    app.add_middleware(M4lapsLicenseMiddleware)
