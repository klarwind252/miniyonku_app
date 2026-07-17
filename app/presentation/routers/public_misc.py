"""公開系の雑多なエンドポイント（/ /health /enter /logo /api/race-asset）。"""
import base64
import os

import aiosqlite
from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse, FileResponse, Response
from starlette.responses import HTMLResponse

from app.infrastructure.db.connection import get_db

router = APIRouter()

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "../../static")

# レース画像として配信を許可する Content-Type（保存型XSS対策）
_ALLOWED_ASSET_CTYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


@router.get("/")
async def root():
    return RedirectResponse(url="/admin/")


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/api/telop")
async def public_telop(request: Request, cid: str = "", tid: int = 0,
                       db: aiosqlite.Connection = Depends(get_db)):
    """参加者html / view 用：現在のテロップをJSONで返す（公開・トークン不要）。

    店舗はミドルウェアが解決済み（既定店舗は /api/telop、スラッグ店舗は
    /{slug}/api/telop でこのルートに届く）。'api' は既定店舗プレフィックスなので
    スラッグ無しの /api/telop は店舗1として解決される。

    参加者htmlはこの30秒ポーリングに cid（端末ID）と tid（大会ID）を相乗りさせる。
    cid があるときだけアクセス統計の心拍として記録する（view からは cid 無し＝不計上）。
    """
    from fastapi.responses import JSONResponse

    if cid:
        try:
            from app.services import access_stats
            store = getattr(request.state, "store", None)
            sid = getattr(store, "id", 0)
            access_stats.record_hit(sid, tid, cid)
        except Exception:
            pass

    async def _val(key: str) -> str:
        async with db.execute("SELECT value FROM app_settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return (row["value"] if row and row["value"] is not None else "")

    text = await _val("telop_text")
    active = (await _val("telop_active")) == "1"
    updated_at = await _val("telop_updated_at")
    return JSONResponse(
        {"active": active, "text": text, "updated_at": updated_at},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/enter")
async def participant_enter(request: Request):
    """参加者向け入口（QRが指すURL）。

    案①（クライアント側ソフト有効期限）:
      この入口にアクセスした時刻を localStorage に記録し、参加者向け観覧ページへ進む。
      観覧ページ側の判定JSは、記録時刻から24時間を過ぎると自動更新を止めて
      オーバーレイ表示する。再びこのQR（/enter）を読み直すと時刻が更新され、
      新たな24時間が始まる（単純な再読込では復帰しない）。

    PWA（ホーム画面アイコン）起動時の特例（?src=pwa）:
      ホーム画面アイコンの start_url もこの /enter を指しているため、区別なく
      無条件で時刻を上書きすると、アイコンをタップするだけで実質「QR再読み込み」が
      毎回自動発生し、24時間制限を無期限に延長できてしまう（QR再スキャン不要で
      アクセスし続けられる不具合）。
      これを防ぐため、PWA起動（?src=pwa 付き）のときは「まだ発行時刻が無い場合
      （そのアイコンの初回起動）」のみ記録し、既に発行時刻があるときは上書きしない。
      本物のQR（?src=pwa なし）は従来どおり常に更新＝再スキャンでの延長を維持する。
    """
    store = getattr(request.state, "store", None)
    slug = store.slug if store else ""
    base = f"/{slug}/" if slug else "/"
    key = f"m4_pub_issued_{slug or 'default'}"
    is_pwa = request.query_params.get("src") == "pwa"
    if is_pwa:
        # PWAアイコン起動：初回（未発行）のときだけ記録。既存の発行時刻（期限切れ含む）は上書きしない。
        set_js = f"""try {{
  if (!localStorage.getItem({key!r})) {{ localStorage.setItem({key!r}, String(Date.now())); }}
}} catch(e) {{}}"""
    else:
        # 本物のQR：常に上書き（再スキャンのたびに新たな24時間が始まる＝仕様どおり）
        set_js = f"""try {{ localStorage.setItem({key!r}, String(Date.now())); }} catch(e) {{}}"""
    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>読み込み中…</title></head><body>
<p style="font-family:sans-serif;text-align:center;margin-top:40vh;color:#555">読み込み中…</p>
<script>
{set_js}
location.replace({base!r});
</script></body></html>"""
    return HTMLResponse(html)


@router.get("/logo")
async def serve_logo():
    """ロゴ画像をno-cacheで返す（画像差し替えが即反映される）"""
    path = os.path.join(_STATIC_DIR, "logo_header.jpg")
    if not os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/api/race-asset/{tid}/{kind}/{seq}")
async def serve_race_asset(tid: int, kind: str, seq: int,
                           db: aiosqlite.Connection = Depends(get_db)):
    """レース情報の画像を配信HTMLとは別URLで返す。公開（トークン不要）。"""
    if kind not in ("course", "schedule", "remarks"):
        return Response(status_code=404)
    async with db.execute(
        "SELECT data_uri FROM race_assets WHERE tournament_id=? AND kind=? AND seq=?",
        (tid, kind, seq),
    ) as cur:
        row = await cur.fetchone()
    if not row or not row["data_uri"]:
        return Response(status_code=404)
    try:
        header, b64 = row["data_uri"].split(",", 1)
        ctype = (header.split(":", 1)[1].split(";", 1)[0] or "image/png")
        # 同一オリジンでの HTML/SVG 配信（保存型XSS）を防ぐため画像のみ許可。
        if ctype not in _ALLOWED_ASSET_CTYPES:
            return Response(status_code=404)
        raw = base64.b64decode(b64)
    except Exception:
        return Response(status_code=404)
    return Response(content=raw, media_type=ctype,
                   headers={"Cache-Control": "no-cache",
                            "X-Content-Type-Options": "nosniff"})
