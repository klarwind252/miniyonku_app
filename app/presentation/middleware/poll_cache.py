"""観覧ポーリング用・超短TTLレスポンスキャッシュ（最適化 20261105）。

【背景＝本システム最大のスケーラビリティ・ボトルネック】
  観覧ページ（viewer/bracket.html・viewer/qualifying.html）は 3〜5 秒ごとに
  /admin/tournaments/{tid}/bracket/html 等を fetch する。サーバーはこの1リクエスト
  ごとに bracket.py（約6,250行・インラインSQL 590箇所）で全ブラケットHTMLを
  都度フル生成している。会場で観覧端末が N 台つながっていれば
  「N台 × 3秒ごと × フルSQL+HTML生成」が常時走り、内容が変化していなくても
  毎回フル再生成＝サーバー側の“不要な再レンダリング”になる。
  （クライアント側は _lastBracketHtml 等で差分描画済みだが、サーバーの生成コストは
   そのまま掛かり続ける点が盲点だった）

【戦略】
  同一リソースへの同時多発ポーリングを、ごく短いTTL（既定OFF）で1回の生成に畳む。
  TTL はポーリング間隔（3〜5秒）より十分短い値（例 2000ms）に設定するため、
  観覧者が見る更新遅延は実質変わらない＝体感・動作は不変。N台ぶんの重複生成だけが消える。

【安全設計（＝“動作に一切影響しない”の担保）】
  - 既定 **無効**（M4_POLL_CACHE_MS 未設定 or <=0 なら完全スルー＝従来と同一応答）。
  - キャッシュ対象は「観覧がポーリングする読み取り専用GET」だけを許可リストで限定。
  - キーに Host を含めるため店舗（マルチテナント）をまたいだ混線が起きない。
    ミドルウェアの登録順にも依存しない。
  - 200 応答のみキャッシュ。Set-Cookie を含む応答や非GETは絶対にキャッシュしない。
  - キャッシュHITでも Content-Type 等のヘッダはオリジナルを完全再現。
"""
from __future__ import annotations

import os
import time

from starlette.types import ASGIApp
from starlette.requests import Request
from starlette.responses import Response

_TTL_MS = int(os.getenv("M4_POLL_CACHE_MS", "0"))   # 0 or 未設定 = 無効（従来挙動）
_MAX_ENTRIES = int(os.getenv("M4_POLL_CACHE_MAX", "256"))


def _is_pollable(path: str) -> bool:
    """観覧が高頻度ポーリングする読み取り専用GETだけを対象にする。"""
    if path.endswith("/bracket/html"):          # 決勝ブラケット＋予選ヒートブラケット
        return True
    if path == "/view/host-state":              # 追従スクロール/再読込判定
        return True
    return False


class _Entry:
    __slots__ = ("body", "status", "media_type", "headers", "exp")

    def __init__(self, body, status, media_type, headers, exp):
        self.body = body
        self.status = status
        self.media_type = media_type
        self.headers = headers
        self.exp = exp


_store: "dict[str, _Entry]" = {}


def add_poll_cache(app: ASGIApp):
    """main.py から呼ぶ。TTL<=0 のときは何もしない純粋スルー。"""

    @app.middleware("http")
    async def _poll_cache_mw(request: Request, call_next):
        # 無効時・対象外は即スルー（オーバーヘッドほぼゼロ・従来と完全一致）
        if _TTL_MS <= 0 or request.method != "GET" or not _is_pollable(request.url.path):
            return await call_next(request)

        # 店舗（Host）＋パス＋クエリで一意化（登録順・store_resolver に非依存）
        host = request.headers.get("host", "")
        key = f"{host}\n{request.url.path}?{request.url.query}"
        now = time.monotonic()

        ent = _store.get(key)
        if ent is not None and ent.exp > now:
            return Response(content=ent.body, status_code=ent.status,
                            media_type=ent.media_type, headers=dict(ent.headers))

        response = await call_next(request)

        # 200 かつ Set-Cookie を持たない応答のみ畳み込む
        if response.status_code == 200 and "set-cookie" not in response.headers:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk

            cache_headers = {
                k: v for k, v in response.headers.items()
                if k.lower() not in ("content-length", "set-cookie")
            }
            if len(_store) >= _MAX_ENTRIES:
                _store.clear()   # 単純LRU代替（会場規模では十分）
            _store[key] = _Entry(body, 200, response.media_type,
                                  cache_headers, now + _TTL_MS / 1000.0)
            return Response(content=body, status_code=200,
                            media_type=response.media_type, headers=cache_headers)

        return response
