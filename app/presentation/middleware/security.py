"""
セキュリティヘッダ付与ミドルウェア（全モード共通で登録）。

- クリックジャッキング防止（X-Frame-Options）、MIMEスニッフィング防止
  （X-Content-Type-Options）、Referer漏洩抑制（Referrer-Policy）を全応答へ付与。
- クラウド版のみ、プロキシが HTTP で終端した場合に HTTPS へ 308 リダイレクトし、
  HSTS を付与する（X-Forwarded-Proto を信頼できるリバースプロキシ配下を前提）。

オンプレ版でも害はなく（HTTPリダイレクトはクラウド時のみ）、既定挙動を壊さない。
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

from app.core.config import IS_CLOUD


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if IS_CLOUD and request.headers.get("x-forwarded-proto", "https") == "http":
            url = request.url.replace(scheme="https")
            return RedirectResponse(str(url), status_code=308)

        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if IS_CLOUD:
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        return resp


def add_security_headers(app):
    """セキュリティヘッダミドルウェアを登録する（全モード共通）。"""
    app.add_middleware(SecurityHeadersMiddleware)
