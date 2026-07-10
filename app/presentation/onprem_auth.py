"""
オンプレ版（IS_CLOUD=False）向けの任意PIN認証ミドルウェア。

背景:
  オンプレ版は既定で無認証。単一PCで 127.0.0.1 バインドなら問題ないが、参加者端末へ
  /view/ を見せるために LAN 公開（0.0.0.0 バインド）すると、同一ネットワーク上の任意端末が
  無認証で /admin/ を操作できてしまう。

方針:
  環境変数 ONPREM_ADMIN_PIN を設定したときだけ有効化する（未設定なら従来どおり素通し＝
  後方互換）。/admin/* を PIN で保護し、参加者向けの /view /entry /health /static 等は
  従来どおり公開のまま。任意で ONPREM_VIEW_PIN を設定すると /view/* も保護できる。

受け渡し:
  初回 ?pin=<PIN> をクエリで受け取り HttpOnly Cookie に保存し、PINを外したURLへ 303。
  以降は Cookie で認証。クラウド版の固定トークン認証と同じ考え方・実装に揃えている。

登録:
  main.py が add_onprem_auth(app) を呼ぶ。IS_CLOUD かつ PIN未設定のときは何もしない。
"""
import os
import secrets
from urllib.parse import urlencode

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse, PlainTextResponse

from app.core.config import IS_CLOUD

_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"

# クラウドの認証と同じ公開プレフィックス集合（参加者向け・ヘルスチェック等）
_PUBLIC_PREFIXES = ("/static", "/health", "/logo", "/favicon", "/enter",
                    "/entry", "/race-asset", "/reserve")

_ADMIN_COOKIE = "m4_onprem_admin_pin"
_VIEW_COOKIE = "m4_onprem_view_pin"


def _admin_pin() -> str:
    return os.environ.get("ONPREM_ADMIN_PIN", "")


def _view_pin() -> str:
    return os.environ.get("ONPREM_VIEW_PIN", "")


class OnpremPinAuthMiddleware(BaseHTTPMiddleware):
    """オンプレ LAN 公開時の簡易PIN認証。admin を必須保護、view は任意。"""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        need_admin = path.startswith("/admin")
        need_view = path.startswith("/view")
        if not (need_admin or need_view):
            return await call_next(request)

        admin_pin = _admin_pin()
        view_pin = _view_pin()

        # ?pin=... を受け取ったら Cookie へ保存し、PINを外したURLへ 303
        pin = request.query_params.get("pin")
        if pin is not None:
            params = dict(request.query_params)
            params.pop("pin", None)
            clean_url = path + (("?" + urlencode(params)) if params else "")
            resp = RedirectResponse(url=clean_url, status_code=303)
            resp.headers["Referrer-Policy"] = "no-referrer"
            _kw = dict(httponly=True, samesite="lax", secure=_COOKIE_SECURE,
                       path="/", max_age=60 * 60 * 24 * 30)
            if admin_pin and secrets.compare_digest(pin, admin_pin):
                resp.set_cookie(_ADMIN_COOKIE, pin, **_kw)
            if view_pin and secrets.compare_digest(pin, view_pin):
                resp.set_cookie(_VIEW_COOKIE, pin, **_kw)
            return resp

        admin_cookie = request.cookies.get(_ADMIN_COOKIE, "")
        view_cookie = request.cookies.get(_VIEW_COOKIE, "")
        admin_ok = bool(admin_pin) and secrets.compare_digest(admin_cookie, admin_pin)
        view_ok = bool(view_pin) and secrets.compare_digest(view_cookie, view_pin)

        if need_admin:
            if admin_ok:
                return await call_next(request)
            return PlainTextResponse(
                "401 Unauthorized: 管理画面（admin）のPIN認証が必要です。"
                "管理者用URL（?pin=... 付き）でアクセスしてください。",
                status_code=401,
            )

        if need_view:
            # view PIN が未設定なら参加者向けは公開のまま（従来挙動）
            if not view_pin or view_ok or admin_ok:
                return await call_next(request)
            return PlainTextResponse(
                "401 Unauthorized: 観覧画面（view）のPIN認証が必要です。"
                "配布された view 用URL（?pin=... 付き）でアクセスしてください。",
                status_code=401,
            )

        return await call_next(request)


def add_onprem_auth(app):
    """オンプレ版かつ ONPREM_ADMIN_PIN 設定時のみ、PIN認証を登録する。

    クラウド版・PIN未設定のオンプレ版では何もしない（従来挙動を完全維持）。
    """
    if IS_CLOUD:
        return
    if not _admin_pin():
        return
    app.add_middleware(OnpremPinAuthMiddleware)
    print("[AUTH] オンプレ簡易PIN認証 有効（/admin 保護"
          + ("・/view 保護" if _view_pin() else "") + "）", flush=True)
