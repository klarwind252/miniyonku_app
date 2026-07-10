"""admin / view の動的 manifest とハートビート。

認証ミドルウェア下（/admin・/view プレフィックス）にあるため、認証済みセッション
のみが取得できる。Cookie のトークンを読み取り、key 埋め込み起動が有効なら
start_url に ?key=<token> を埋める（iOS のスタンドアロン起動時に再認証される）。
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.core.config import IS_CLOUD, admin_cookie_name, view_cookie_name

router = APIRouter()


def _serve_manifest(request: Request, screen: str):
    from app import pwa
    if not IS_CLOUD:
        return Response(status_code=404)
    settings = pwa.get_pwa_settings(request)
    if settings.get("pwa_enabled") != "1":
        return Response(status_code=404)
    store = getattr(request.state, "store", None)
    slug = store.slug if store else ""
    sid = store.id if store else None
    key = None
    if screen == "admin" and settings.get("pwa_keylaunch_admin") == "1":
        key = request.cookies.get(admin_cookie_name(sid), "") or None
    elif screen == "view" and settings.get("pwa_keylaunch_view") == "1":
        # view 画面は view トークン優先。無ければ admin トークン（admin でも観覧可のため）。
        key = (request.cookies.get(view_cookie_name(sid), "")
               or request.cookies.get(admin_cookie_name(sid), "")) or None
    data = pwa.build_manifest_dict(screen, settings, slug=slug, key=key)
    return JSONResponse(data, media_type="application/manifest+json")


@router.get("/admin/manifest.webmanifest")
async def admin_manifest(request: Request):
    return _serve_manifest(request, "admin")


@router.get("/view/manifest.webmanifest")
async def view_manifest(request: Request):
    return _serve_manifest(request, "view")


@router.post("/api/admin-heartbeat")
async def admin_heartbeat():
    return {"ok": True}