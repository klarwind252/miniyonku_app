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


@router.get("/")
async def root():
    return RedirectResponse(url="/admin/")


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/enter")
async def participant_enter(request: Request):
    """参加者向け入口（QRが指すURL）。

    案①（クライアント側ソフト有効期限）:
      この入口にアクセスした時刻を localStorage に記録し、参加者向け観覧ページへ進む。
      観覧ページ側の判定JSは、記録時刻から24時間を過ぎると自動更新を止めて
      オーバーレイ表示する。再びこのQR（/enter）を読み直すと時刻が更新され、
      新たな24時間が始まる（単純な再読込では復帰しない）。
    """
    store = getattr(request.state, "store", None)
    slug = store.slug if store else ""
    base = f"/{slug}/" if slug else "/"
    key = f"m4_pub_issued_{slug or 'default'}"
    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>読み込み中…</title></head><body>
<p style="font-family:sans-serif;text-align:center;margin-top:40vh;color:#555">読み込み中…</p>
<script>
try {{ localStorage.setItem({key!r}, String(Date.now())); }} catch(e) {{}}
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
        ctype = header.split(":", 1)[1].split(";", 1)[0] or "image/png"
        raw = base64.b64decode(b64)
    except Exception:
        return Response(status_code=404)
    return Response(content=raw, media_type=ctype, headers={"Cache-Control": "no-cache"})