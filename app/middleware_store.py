"""
StoreResolverMiddleware（クラウド版のみ登録）。

役割:
  1. リクエスト先頭パスから店舗を判定する。
       /admin /view /static /logo /health /api /enter / favicon → 既定店舗（店舗1）
       /{slug}/...                                            → 該当店舗（slug一致）
       不明・無効スラッグ                                      → 404
  2. スラッグ付きの場合、scope の path から "/{slug}" を取り除き、root_path に
     "/{slug}" をセットする。これで既存ルーター（/admin/... 等）はそのまま動く。
  3. request.state.store と ContextVar(current_store) に解決した店舗をセットする。
  4. スラッグ付き店舗のレスポンスについてのみ、HTML本文とリダイレクト先(Location)中の
     絶対パス（/admin /view /static /logo /api /enter）へ "/{slug}" を前置する。
     → テンプレートの絶対パス（約120箇所）を書き換えずに、スラッグ配下で正しく動かす。

既定店舗（店舗1・スラッグなし）はパス書き換えを一切行わないため、従来挙動と完全に同一。
オンプレ版では add_store_resolver() が何もしないため影響なし。
"""
from __future__ import annotations

import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, PlainTextResponse

from app import registry
from app.store_context import Store, current_store

# 既定店舗のアプリパス（これらが先頭ならスラッグ解決をスキップ）
_APP_PREFIXES = ("admin", "view", "static", "logo", "health", "api", "enter", "favicon")

# HTML本文中で前置対象にする絶対パス接頭辞
_REWRITE_RE = re.compile(r"([\"'`])(/(?:admin|view|static|logo|health|api|enter)\b)")


def _first_segment(path: str) -> str:
    # "/store2/admin/" -> "store2"
    p = path.lstrip("/")
    return p.split("/", 1)[0] if p else ""


class StoreResolverMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.scope.get("path", "/")

        # 内部レンダリング（public_html が自分自身へ httpx で投げる観覧ページ生成）用の
        # バイパス。スラッグなしURL＋ヘッダで店舗を指定する。パス書き換えは行わない。
        internal_id = request.headers.get("x-internal-store-id")
        if internal_id:
            try:
                istore = registry.get_store_by_id(int(internal_id))
            except Exception:
                istore = None
            if istore is not None and istore.enabled:
                request.state.store = istore
                tok = current_store.set(istore)
                try:
                    return await call_next(request)
                finally:
                    current_store.reset(tok)

        seg = _first_segment(path)

        store: Store | None = None
        slug = ""

        if seg == "" or seg in _APP_PREFIXES:
            # 既定店舗（店舗1）
            store = registry.get_default_store()
        else:
            # スラッグ候補
            store = registry.get_store_by_slug(seg)
            if store is None:
                return PlainTextResponse("404 Not Found: 不明な店舗です。", status_code=404)
            if not store.enabled:
                return PlainTextResponse("404 Not Found: この店舗は現在無効です。", status_code=404)
            slug = store.slug
            # /{slug}/static/... は scope 書き換えを行わない。
            # BaseHTTPMiddleware の scope 書き換えは Mount(StaticFiles) に届かず 404 に
            # なるため、main.py の専用ルート（/{slug}/static/{path}）にそのまま委譲する。
            # （store は解決済みなのでヘッダ等は通常どおり付く）
            _parts = path.lstrip("/").split("/")
            _second = _parts[1] if len(_parts) > 1 else ""
            if _second != "static":
                # scope から "/{slug}" を除去し root_path にセット
                stripped = path[len(f"/{slug}"):] or "/"
                request.scope["path"] = stripped
                request.scope["root_path"] = f"/{slug}"
                if "raw_path" in request.scope and request.scope["raw_path"]:
                    try:
                        request.scope["raw_path"] = request.scope["raw_path"][len(f"/{slug}".encode()):] or b"/"
                    except Exception:
                        pass
            else:
                # 書き換えしない場合でも、応答HTML前置(_rewrite_response)が
                # 二重前置しないよう slug は空に倒す（静的JSにHTML書き換えは不要）。
                slug = ""

        if store is None:
            return PlainTextResponse("500: 既定店舗が未初期化です。", status_code=500)

        # 利用時間制限（店舗2〜5・デモ/お試し用）。時間外は admin/view/参加者すべてブロック。
        if not registry.is_store_open(store):
            return self._blocked_response(store)

        request.state.store = store
        token = current_store.set(store)
        try:
            response = await call_next(request)
        finally:
            current_store.reset(token)

        # スラッグ付き店舗のみ、出力パスへ "/{slug}" を前置
        if slug:
            response = await self._rewrite_response(response, slug)
        return response

    async def _rewrite_response(self, response, slug: str):
        # --- リダイレクト先(Location)の前置 ---
        loc = response.headers.get("location")
        if loc and loc.startswith("/") and not loc.startswith(f"/{slug}/") and loc != f"/{slug}":
            response.headers["location"] = f"/{slug}{loc}"

        # --- HTML本文の前置（text/html のみ）---
        ctype = response.headers.get("content-type", "")
        if "text/html" not in ctype:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode("utf-8")

        text = body.decode("utf-8", "ignore")
        text = _REWRITE_RE.sub(lambda m: f"{m.group(1)}/{slug}{m.group(2)}", text)

        headers = dict(response.headers)
        headers.pop("content-length", None)  # 再計算させる
        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )

    def _blocked_response(self, store):
        """利用時間外の店舗へのアクセスに対して案内ページ（503）を返す。"""
        from html import escape
        name = escape(store.name or "")
        start = escape(store.access_start or "")
        end = escape(store.access_end or "")
        html = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ただいまご利用いただけません</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f4f5f7;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;color:#2c3e50}}
  .box{{background:#fff;border-radius:12px;padding:32px 28px;max-width:420px;text-align:center;
        box-shadow:0 4px 18px rgba(0,0,0,.08)}}
  h1{{font-size:18px;margin:0 0 10px}}
  p{{font-size:14px;color:#636e72;line-height:1.6;margin:6px 0}}
  .time{{display:inline-block;margin-top:10px;font-size:15px;font-weight:bold;color:#2c3e50;
         background:#f4f5f7;padding:6px 14px;border-radius:8px}}
</style></head>
<body>
  <div class="box">
    <h1>🚧 ただいまご利用いただけません</h1>
    <p>{name} は現在ご利用時間外です。</p>
    <p>下記の時間帯にアクセスしてください。</p>
    <div class="time">{start} 〜 {end}</div>
  </div>
</body></html>"""
        return Response(content=html, status_code=503, media_type="text/html")


def add_store_resolver(app):
    """クラウド版のときだけ店舗リゾルバを登録する。"""
    from app.config import IS_CLOUD
    if not IS_CLOUD:
        return
    app.add_middleware(StoreResolverMiddleware)
