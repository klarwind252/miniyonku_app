"""
クラウド版（IS_CLOUD=True）専用の固定トークン認証ミドルウェア。

仕様（仕様書 第10.4章 / 複数店舗化対応）:
  - admin（/admin/*）          : その店舗の admin トークンを要求（書き換え権限）
  - view（/view/*）            : その店舗の view トークン（admin トークンでも可）
  - html（参加者向け）/health  : 認証なし（誰でも観覧可）

複数店舗化:
  StoreResolverMiddleware が request.state.store に「現在の店舗」をセットし、
  scope から "/{slug}" を取り除いた後にこの認証が走る（resolver が外側）。
  そのため本ミドルウェアはスラッグを意識せず、/admin /view だけ見ればよい。
  トークンとCookie名は店舗ごとに分離する（admin_cookie_name(store.id) 等）。
  リダイレクト時のスラッグ前置は resolver が Location ヘッダで行うため、ここでは
  スラッグなしのクリーンURLへ 303 するだけでよい。

トークンの受け渡し:
  初回 ?key=<token> をクエリで受け取り HttpOnly Cookie（店舗別名）に保存。
  以降は Cookie で認証。鍵付きアクセス時は Cookie 設定後に鍵なしURLへ 303 する。

オンプレ版（IS_CLOUD=False）ではこのミドルウェアは登録されない（add_auth参照）。
"""
import os
import secrets
from urllib.parse import urlencode

# クラウドは常時HTTPS前提。誤ってHTTP配信された場合のトークン漏洩を防ぐため、
# 既定で Secure Cookie を付与する（HTTPで検証したい場合のみ COOKIE_SECURE=0）。
_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse, PlainTextResponse

from app.core.config import (

    IS_CLOUD, ADMIN_TOKEN, VIEW_TOKEN,
    admin_cookie_name, view_cookie_name,
)

_PUBLIC_PREFIXES = ("/static", "/health", "/logo", "/favicon", "/enter", "/entry", "/race-asset")


def _wants_admin(path: str) -> bool:
    return path.startswith("/admin")


def _wants_view(path: str) -> bool:
    return path.startswith("/view")


def _store_tokens(request):
    """(store_id, admin_token, view_token) を返す。
    store未解決（単一店舗フォールバック）なら (None, ADMIN_TOKEN, VIEW_TOKEN)。"""
    store = getattr(request.state, "store", None)
    if store is not None:
        return store.id, store.admin_token, store.view_token
    return None, ADMIN_TOKEN, VIEW_TOKEN


def _store_prefix(request) -> str:
    """現在の店舗の URL 接頭辞を返す（既定店舗="" / 店舗2〜="/store2"）。
    Cookie の path をこの店舗スコープに合わせ、PWA(scope=/slug/) でも
    Cookie が確実に送られるようにするため。"""
    store = getattr(request.state, "store", None)
    prefix = getattr(store, "prefix", "") if store is not None else ""
    # path は空文字だと不正なので、既定店舗は "/" にフォールバック
    return prefix or "/"


class FixedTokenAuthMiddleware(BaseHTTPMiddleware):
    """IS_CLOUD 時のみ登録される固定トークン認証（店舗別）。"""

    async def dispatch(self, request, call_next):
        path = request.url.path  # resolver が "/{slug}" を除去済み

        if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        need_admin = _wants_admin(path)
        need_view = _wants_view(path)
        if not (need_admin or need_view):
            return await call_next(request)

        store_id, admin_token, view_token = _store_tokens(request)
        ac_name = admin_cookie_name(store_id)
        vc_name = view_cookie_name(store_id)

        # クエリで鍵が渡された場合：店舗別Cookieへ保存し、鍵を外したURLへ 303
        key = request.query_params.get("key")
        if key is not None:
            params = dict(request.query_params)
            params.pop("key", None)
            clean_url = path + (("?" + urlencode(params)) if params else "")
            resp = RedirectResponse(url=clean_url, status_code=303)
            # トークンが Referer 経由で外部へ漏れないようにする
            resp.headers["Referrer-Policy"] = "no-referrer"
            cookie_path = _store_prefix(request)
            _cookie_kw = dict(httponly=True, samesite="lax", secure=_COOKIE_SECURE,
                              path=cookie_path, max_age=60 * 60 * 24 * 365)
            if admin_token and secrets.compare_digest(key, admin_token):
                resp.set_cookie(ac_name, key, **_cookie_kw)
            if view_token and secrets.compare_digest(key, view_token):
                resp.set_cookie(vc_name, key, **_cookie_kw)
            return resp

        admin_cookie = request.cookies.get(ac_name, "")
        view_cookie = request.cookies.get(vc_name, "")

        admin_ok = bool(admin_token) and secrets.compare_digest(admin_cookie, admin_token)
        view_ok = bool(view_token) and secrets.compare_digest(view_cookie, view_token)

        if need_admin:
            if admin_ok:
                return await call_next(request)
            return PlainTextResponse(
                "401 Unauthorized: 管理画面（admin）の認証が必要です。"
                "管理者から配布された admin 用URL（?key=...付き）でアクセスしてください。",
                status_code=401,
            )

        if need_view:
            if view_ok or admin_ok:
                return await call_next(request)
            return PlainTextResponse(
                "401 Unauthorized: 観覧画面（view）の認証が必要です。"
                "配布された view 用URL（?key=...付き）でアクセスしてください。",
                status_code=401,
            )

        return await call_next(request)


def add_auth(app):
    """クラウド版のときだけ認証ミドルウェアを登録する。
    オンプレ版では何もしない（従来挙動を完全維持）。"""
    if not IS_CLOUD:
        return
    app.add_middleware(FixedTokenAuthMiddleware)
    print("[AUTH] クラウド固定トークン認証 有効（店舗別 admin/view）", flush=True)
