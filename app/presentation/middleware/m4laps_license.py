"""M4LAPS ライセンス状態を request.state にセットする軽量ミドルウェア。

base.html のナビは全admin画面共通なので、各リクエストで「M4LAPSが有効か」を
知る必要がある。ここで request.state.m4laps_licensed をセットし、
テンプレートから {{ request.state.m4laps_licensed }} で参照できるようにする。

- オンプレ版（IS_CLOUD=False）では常に False（根本非表示）。
- クラウド版では、その店舗DBの app_settings を同期SQLiteで軽く読む。
  /admin 配下のGETのときだけ判定する（他のパスやAPIでは不要）。
"""

from __future__ import annotations

import sqlite3

from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import IS_CLOUD
from app.infrastructure.db.schema import current_db_path


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


class M4lapsLicenseMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        licensed = False
        if IS_CLOUD:
            # StoreResolverMiddleware より内側で実行されるため、ここでは
            #   ・scope["path"] はスラッグ除去後（例 "/admin/timing/results"）
            #   ・request.state.store は解決済みの店舗
            # になっている。スラッグ付き店舗でも正しい店舗DBを読めるよう、
            # ContextVare ではなく request.state.store の db_path を使う。
            path = request.scope.get("path", "") or request.url.path
            # admin画面の表示にだけ必要（ナビ判定）。無駄な判定を避ける。
            if path.startswith("/admin"):
                db_path = None
                store = getattr(request.state, "store", None)
                if store is not None and getattr(store, "db_path", None):
                    db_path = store.db_path
                if db_path is None:
                    # フォールバック（既定店舗 / 単一店舗）。
                    try:
                        db_path = current_db_path()
                    except Exception:
                        db_path = None
                if db_path:
                    try:
                        licensed = _read_licensed(db_path)
                    except Exception:
                        licensed = False
        request.state.m4laps_licensed = licensed
        return await call_next(request)


def add_m4laps_license(app):
    """クラウド版でのみ意味を持つ。オンプレでは常に False をセットするだけ。"""
    app.add_middleware(M4lapsLicenseMiddleware)
