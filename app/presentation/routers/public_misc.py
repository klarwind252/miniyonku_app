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


def _staff_ok(request: Request) -> bool:
    """admin / view の認証クッキーを持つ（＝会場スタッフ端末）なら True。"""
    try:
        import secrets as _sec
        from app.presentation.auth import _store_tokens
        from app.core.config import admin_cookie_name, view_cookie_name
        sid, at, vt = _store_tokens(request)
        ac = request.cookies.get(admin_cookie_name(sid), "")
        vc = request.cookies.get(view_cookie_name(sid), "")
        return (bool(at) and _sec.compare_digest(ac, at)) or (bool(vt) and _sec.compare_digest(vc, vt))
    except Exception:
        return False


def _content_allowed(request: Request) -> bool:
    """観覧内容系エンドポイント（過去成績・レース一覧・レース画像・テロップ）の可否。

    参加者向けの本体は /api/pub-content で守っているが、そこから辿れる
    /api/history /api/races /api/race-asset 等が無条件公開だと、失効した端末や
    URLを知る第三者がそれらを直接開いて内容を見られる（抜け道）。
    有効な参加者クッキー、または会場スタッフの admin/view クッキーのどちらかを要求する。
    """
    if _staff_ok(request):
        return True
    try:
        from app.services import pub_gate
        store = getattr(request.state, "store", None)
        slug = store.slug if store else ""
        secret = pub_gate.secret_for(store)
        epoch = pub_gate.get_epoch(pub_gate.db_path_for(store))
        state, _ = pub_gate.check_cookie_value(secret, epoch, request.cookies.get(pub_gate.cookie_name(slug)))
        return state == "valid"
    except Exception:
        return False


def _deny_content(request: Request):
    """失効端末からの内容系アクセスを拒否。HTMLナビゲーションなら失効ページへ。"""
    from fastapi.responses import RedirectResponse, Response
    accept = request.headers.get("accept", "")
    store = getattr(request.state, "store", None)
    slug = store.slug if store else ""
    if "text/html" in accept:
        return RedirectResponse(url=(f"/{slug}/enter?src=expired" if slug else "/enter?src=expired"), status_code=302)
    return Response(status_code=401, headers={"Cache-Control": "no-store"})


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

    if not _content_allowed(request):
        return JSONResponse({"active": False, "text": "", "updated_at": ""},
                            headers={"Cache-Control": "no-store"})

    if cid:
        try:
            from app.services import access_stats
            store = getattr(request.state, "store", None)
            sid = getattr(store, "id", 0)
            ua = request.headers.get("user-agent", "")
            # どの認証で通ったか（切り分け用）: スタッフ(admin/view)クッキー or 参加者クッキー
            via = "staff" if _staff_ok(request) else "pub"
            access_stats.record_hit(sid, tid, cid, ua, via)
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


# ---------------------------------------------------------------------------
# 参加者向け観覧の入口（固定QR＋サーバー署名ゲート）
# ---------------------------------------------------------------------------
# 想定するあらゆるアクセス経路と、それぞれの扱い（設計の一覧）:
#
#   1. 会場の固定QRをカメラで読む            → /enter                  … 12時間発行（再スキャンのたびに更新）
#   2. /enter をURL直打ち・履歴・ブックマーク → /enter                  … QRと同じURLのため区別不能＝同じく発行
#                                                                           （固定QRの原理的限界。12時間TTLと
#                                                                            世代リセットで被害を限定）
#   3. 観覧トップ /<slug>/ の履歴・ブックマーク→ 静的HTML               … 有効クッキーが無ければ失効（発行なし）
#   4. 共有リンク（LINE等）                  → /enter                  … 2と同じ
#   5. 観覧ページの直接URL（/ や /<slug>/）  → nginx 静的配信          … nginx auth_request で /api/pub-auth を
#                                                                           照会させ、無効なら401（deploy/ 参照）。
#                                                                           JSも30秒ごと＋復帰時にサーバー判定
#   6. iOS PWA（独立Cookieストア）           → /enter?h=<引き継ぎ>     … ブラウザの発行時刻を引き継ぐ。新規12時間
#                                                                           は絶対に得られない。期限後は再インストール
#   7. Android PWA / デスクトップPWA          → /enter?src=pwa          … Chromeと同一Cookie。有効なら通過、無効は失効
#   8. 旧manifestのPWA（/enter?src=pwa）     → 同上                    … 有効クッキーが無ければ失効（再インストール）
#   9. シークレット/プライベートモード       → クッキー無し             … 秘密パスを読まない限り失効
#  10. 別ブラウザ・別端末                     → クッキー無し             … 同上
#  11. 端末時計の改竄 / localStorage改竄      → 無効                     … 判定はサーバー時刻・サーバー署名
#  12. 強制失効（管理画面／毎晩3:30）         → 世代+1                   … 発行済み全クッキー・全引き継ぎが即無効
#  13. /enter 連打                            → IP毎レート制限          … 抑止
#  14. 開きっぱなしのタブ                     → 30秒ポーリング＋復帰時   … 期限到来で /enter へ遷移（内容を破棄）
#  15. 署名トークン ?k=（旧方式）             → 廃止                     … 受け付けない
#
# 「/enter のURLを知っていれば再取得できる」のは固定QRの原理的限界。
# その被害は 12時間TTL＋毎晩の世代リセット＋手動の強制失効で限定する。

import threading as _threading
import time as _time
_enter_rl_lock = _threading.Lock()
_enter_rl: dict = {}   # ip -> [count, window_start]
_ENTER_RL_MAX = 240    # 1分あたり /enter の上限（IP毎）。会場Wi-Fi(NAT)で大勢が同時にスキャンしても詰まらない値


def _rate_limited(ip: str) -> bool:
    now = _time.time()
    with _enter_rl_lock:
        c = _enter_rl.get(ip)
        if c is None or now - c[1] > 60:
            _enter_rl[ip] = [1, now]
            if len(_enter_rl) > 5000:   # メモリ上限（古いものから間引き）
                for k in sorted(_enter_rl, key=lambda x: _enter_rl[x][1])[:1000]:
                    _enter_rl.pop(k, None)
            return False
        c[0] += 1
        return c[0] > _ENTER_RL_MAX


def _client_ip(request: Request) -> str:
    xf = request.headers.get("x-forwarded-for", "")
    if xf:
        return xf.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _enter_common(request: Request):
    """/enter 系で共通に使う文脈をまとめて返す。"""
    from app.services import pub_gate
    store = getattr(request.state, "store", None)
    slug = store.slug if store else ""
    base = f"/{slug}/" if slug else "/"
    key = f"m4_pub_issued_{slug or 'default'}"
    dbp = pub_gate.db_path_for(store)
    secret = pub_gate.secret_for(store)
    epoch = pub_gate.get_epoch(dbp)
    cname = pub_gate.cookie_name(slug)
    state, _ = pub_gate.check_cookie_value(secret, epoch, request.cookies.get(cname))
    return pub_gate, store, slug, base, key, dbp, secret, epoch, cname, state


def _enter_path(request: Request) -> str:
    store = getattr(request.state, "store", None)
    slug = store.slug if store else ""
    return f"/{slug}/enter" if slug else "/enter"


def _pass_page(base: str, key: str, renew: bool) -> HTMLResponse:
    if renew:
        set_js = f"""try {{ localStorage.setItem({key!r}, String(Date.now())); }} catch(e) {{}}"""
    else:
        set_js = f"""try {{
  if (!localStorage.getItem({key!r})) {{ localStorage.setItem({key!r}, String(Date.now())); }}
}} catch(e) {{}}"""
    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>読み込み中…</title></head><body>
<p style="font-family:sans-serif;text-align:center;margin-top:40vh;color:#555">読み込み中…</p>
<script>
{set_js}
location.replace({base!r} + "?v=" + Date.now());
</script></body></html>"""
    resp = HTMLResponse(html)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


_BLOCKED_TPL = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>観覧は終了しました</title>
<style>
html,body{margin:0;background:#141821;color:#fff;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px;box-sizing:border-box}
.ttl{font-size:22px;font-weight:bold;margin-bottom:16px}
.msg{font-size:16px;line-height:1.9;opacity:.92}
.sub{margin-top:18px;font-size:13px;opacity:.6;line-height:1.7}
.code{margin-top:24px;font-size:11px;opacity:.35}
</style></head>
<body>
<div class="wrap">
  <div class="ttl">観覧の有効期限が切れました</div>
  <div class="msg">お手数ですが店舗にご来店のうえ<br>本日の会場QRコードを<br>もう一度読み取ってください。</div>
  <div class="sub">※ このページ（保存した画面・ホーム画面アイコン）からは観覧できません。</div>
  <div class="code">code: __REASON__</div>
</div>
<script>try { localStorage.setItem(__KEY__, "0"); } catch(e) {}</script>
</body></html>"""


def _blocked_page(key: str, pwa: bool = False, reason: str = "",
                  enter_api_base: str = "/enter") -> HTMLResponse:
    """アクセス拒否ページ（観覧不可）。

    許可するのは「当日の会場QRアドレス（/<観覧語>/enter/<鍵語>）」を実際に読み取った
    場合だけ。期限切れ・古い保存URL・古いPWA・素の /enter 等はすべてこの画面で止める。
    ページ内カメラ等の再入場導線は持たない（＝このページからは二度と入れない）。
    pwa / enter_api_base 引数は呼び出し互換のため残置（本文では未使用）。
    """
    import html as _html, json as _json
    reason_safe = _html.escape(reason)[:80]
    html = (_BLOCKED_TPL
            .replace("__REASON__", reason_safe)
            .replace("__KEY__", _json.dumps(key)))
    resp = HTMLResponse(html, status_code=403)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _issue(request: Request, resp: HTMLResponse, cname: str, secret: str,
           epoch: int, issued_ts: float | None = None) -> HTMLResponse:
    from app.services import pub_gate
    fwd = request.headers.get("x-forwarded-proto", "")
    secure = (request.url.scheme == "https") or (fwd == "https")
    val = pub_gate.issue_cookie_value(secret, epoch, issued_ts)
    # 有効期限 = TTL と「発行した日の終わり（次の09:00）」の早い方。
    # 引き継ぎ発行（issued_ts 指定）は元の発行時刻を基準にする。
    base_ts = issued_ts if issued_ts is not None else _time.time()
    remain = max(1, pub_gate.remaining_valid_sec(base_ts))
    resp.set_cookie(cname, val, max_age=remain, path="/",
                    httponly=True, samesite="lax", secure=secure)
    return resp


def _hours_ok(store) -> bool:
    """営業時間制限が有効な店舗では、時間外は新規発行しない（追加の縛り。任意）。"""
    try:
        if store is not None and getattr(store, "restrict_hours", False):
            from app import registry
            return bool(registry.is_store_open(store))
    except Exception:
        pass
    return True


async def _dispatch_enter_api(request: Request):
    """/enter?api=... の同居API（内容・状態・引き継ぎ・manifest・jsqr）。該当なしは None。

    nginx が /enter は確実にアプリへ中継しているため、観覧内容・状態確認・引き継ぎ・
    manifest・jsqr も /enter?api=... で提供する（/api/pub-* を中継していない環境でも届く）。
    """
    api = request.query_params.get("api", "")
    if api == "content":
        return await public_gate_content(request)
    if api == "status":
        return await public_gate_status(request)
    if api == "auth":
        return await public_gate_auth(request)
    if api == "handoff":
        return await public_gate_handoff(request)
    if api == "manifest":
        return await public_gate_manifest(request)
    if api == "jsqr":
        import os
        from fastapi.responses import FileResponse, Response
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static", "vendor", "jsqr.min.js")
        if not os.path.isfile(path):
            return Response(status_code=404)
        return FileResponse(path, media_type="application/javascript",
                            headers={"Cache-Control": "public, max-age=86400"})
    if api == "entrypath":
        return _entrypath_json(request)
    return None


def _daily_target(request: Request, secret: str, epoch: int) -> str:
    """当日の観覧URL（店舗prefix付きの /enter/<当日の単語>）。secret無しは "/"。"""
    from app.services import pub_gate
    store = getattr(request.state, "store", None)
    slug = store.slug if store else ""
    pfx = f"/{slug}" if slug else ""
    w = pub_gate.daily_word(secret, epoch)
    return f"{pfx}/enter/{w}" if w else (f"{pfx}/" if pfx else "/")


def _entrypath_json(request: Request):
    """観覧トップ（シェル）用：有効クッキー時のみ当日の観覧URLを返す。無効は401。

    秘密語を知らせてよいのは「すでに観覧許可を持つ端末」だけなので、クッキー検証に通った
    場合だけ path を返す（素の / を直接開いた無許可端末には渡さない）。
    """
    from fastapi.responses import JSONResponse
    pub_gate, store, slug, base, key, dbp, secret, epoch, cname, state = _enter_common(request)
    if state != "valid":
        return JSONResponse({"ok": False}, status_code=401, headers={"Cache-Control": "no-store"})
    return JSONResponse({"ok": True, "path": _daily_target(request, secret, epoch)},
                        headers={"Cache-Control": "no-store"})


def _content_response(request: Request, base: str, key: str, slug: str,
                      cname: str, secret: str, epoch: int,
                      reissue: bool = True, issued_ts: float | None = None):
    """観覧内容本体を【この日替わりURL上で】そのまま表示する。

    - 素の / や固定URLに着地させず、アドレスバーに毎日変わる /enter/<word> を出すため、
      リダイレクトせず本体HTMLを直接返す。
    - 本体の失効判定が参照する localStorage の発行時刻をここで必ずセットしてから本体を流す。
    - reissue=True のときだけ Set-Cookie する（PWA起動など既存クッキー保持時は再発行しない）。
    """
    import os
    from app.services.public_html import gated_content_path
    store = getattr(request.state, "store", None)
    path = gated_content_path(store)
    if not path or not os.path.isfile(path):
        resp = HTMLResponse(
            "<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>準備中</title></head><body style=\"background:#141821;color:#cfd8e3;"
            "font-family:sans-serif\"><p style=\"text-align:center;margin-top:40vh\">"
            "観覧内容を準備中です。しばらくしてから再度お試しください。</p></body></html>",
            status_code=503)
        resp.headers["Cache-Control"] = "no-store"
        return _issue(request, resp, cname, secret, epoch, issued_ts=issued_ts) if reissue else resp
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        body = f.read()
    slugkey = slug or "default"
    setter = ('<script>try{localStorage.setItem('
              + repr("m4_pub_issued_" + slugkey)
              + ',String(Date.now()));}catch(e){}</script>')
    low = body.lower()
    i = low.find("<head")
    if i >= 0:
        j = body.find(">", i)
        body = (body[:j + 1] + setter + body[j + 1:]) if j >= 0 else (setter + body)
    else:
        body = setter + body
    resp = HTMLResponse(body)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return _issue(request, resp, cname, secret, epoch, issued_ts=issued_ts) if reissue else resp


def _redirect_to_daily(request: Request, cname: str, secret: str, epoch: int,
                       reissue: bool = False, issued_ts: float | None = None) -> HTMLResponse:
    """当日の観覧URL（/enter/<word>）へ中継する小さなページ。固定URLに内容を出さないため。"""
    target = _daily_target(request, secret, epoch)
    html = (f"<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<meta name=\"referrer\" content=\"no-referrer\"><title>読み込み中…</title></head>"
            f"<body><p style=\"font-family:sans-serif;text-align:center;margin-top:40vh;color:#555\">"
            f"読み込み中…</p><script>location.replace({target!r});</script></body></html>")
    resp = HTMLResponse(html)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return _issue(request, resp, cname, secret, epoch, issued_ts=issued_ts) if reissue else resp


async def _issue_with_word(request: Request, word: str):
    """旧1語方式 /enter/<word> は廃止。常に失効ページ（新方式の2語URLのみ有効）。"""
    pub_gate, store, slug, base, key, dbp, secret, epoch, cname, state = _enter_common(request)
    return _blocked_page(key, False, "LEGACY_ENTER_WORD_DISABLED+COOKIE_" + state.upper(),
                         enter_api_base=_enter_path(request))


@router.get("/enter")
async def participant_enter(request: Request):
    """素の /enter は【内容を表示しない・発行しない】。観覧は当日の /<観覧語>/enter/<鍵語> のみ。

    /enter?api=... （content/status/handoff/manifest/jsqr/entrypath）は観覧ページの
    ライブ更新に必要なので従来どおり通す。それ以外の素の /enter は、クッキー・
    PWA起動(?src=pwa)・引き継ぎ(?h=) の有無に関わらず常に失効ページ。
    """
    resp = await _dispatch_enter_api(request)
    if resp is not None:
        return resp
    pub_gate, store, slug, base, key, dbp, secret, epoch, cname, state = _enter_common(request)
    src = request.query_params.get("src", "")
    return _blocked_page(key, src in ("pwa", "rescan"),
                         "NO_ISSUE_BARE+COOKIE_" + state.upper(),
                         enter_api_base=_enter_path(request))


@router.get("/enter/{word}")
async def participant_enter_word(word: str, request: Request):
    """旧1語方式。廃止（常に失効ページ）。"""
    return await _issue_with_word(request, word)


# ============ 2語方式：入場 /<観覧語>/enter/<鍵語> → 観覧 /<観覧語> ============
# store_resolver がドメイン直下のワードURLを下の内部ルートへ書き換えて到達させる。
def _redirect_to_view(request: Request, cname: str, secret: str, epoch: int) -> HTMLResponse:
    """入場成功：クッキーを発行し、観覧URL /<観覧語>（店舗prefix付き）へ移動させる。"""
    from app.services import pub_gate
    store = getattr(request.state, "store", None)
    slug = store.slug if store else ""
    pfx = f"/{slug}" if slug else ""
    vw = pub_gate.view_word(secret, epoch)
    target = f"{pfx}/{vw}" if vw else (pfx or "/")
    html = ("<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\"><title>読み込み中…</title></head>"
            "<body><p style=\"font-family:sans-serif;text-align:center;margin-top:40vh;color:#555\">"
            "読み込み中…</p><script>location.replace(" + repr(target) + ");</script></body></html>")
    resp = HTMLResponse(html)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return _issue(request, resp, cname, secret, epoch)


@router.get("/enter/__m4entry__/{w24}/{wkey}")
async def participant_entry(w24: str, wkey: str, request: Request):
    """入場：当日の観覧語＋鍵語が一致したらクッキー発行→/<観覧語>へ移動。"""
    pub_gate, store, slug, base, key, dbp, secret, epoch, cname, state = _enter_common(request)
    if _rate_limited(_client_ip(request)):
        return _blocked_page(key, False, "RATE_LIMIT", enter_api_base=_enter_path(request))
    if not _hours_ok(store):
        return _blocked_page(key, False, "CLOSED+COOKIE_" + state.upper(),
                             enter_api_base=_enter_path(request))
    if not secret:
        return _redirect_to_view(request, cname, secret, epoch)
    if pub_gate.verify_view_word(secret, epoch, w24) and pub_gate.verify_key_word(secret, epoch, wkey):
        return _redirect_to_view(request, cname, secret, epoch)
    return _blocked_page(key, False, "ENTRY_WORD_INVALID+COOKIE_" + state.upper(),
                         enter_api_base=_enter_path(request))


@router.get("/enter/__m4view__/{w24}")
async def participant_view(w24: str, request: Request):
    """観覧：当日の観覧語＋有効クッキーのときだけ内容表示。旧語・鍵なし(クッキー無)は拒否。"""
    pub_gate, store, slug, base, key, dbp, secret, epoch, cname, state = _enter_common(request)
    if secret and not pub_gate.verify_view_word(secret, epoch, w24):
        return _blocked_page(key, False, "VIEW_WORD_INVALID+COOKIE_" + state.upper(),
                             enter_api_base=_enter_path(request))
    if state != "valid":
        return _blocked_page(key, False, "VIEW_NO_COOKIE", enter_api_base=_enter_path(request))
    return _content_response(request, base, key, slug, cname, secret, epoch, reissue=False)


@router.get("/api/pub-auth")
async def public_gate_auth(request: Request):
    """nginx auth_request 用。有効クッキーなら 204、無効なら 401。

    参加者向け静的HTML（/ や /<slug>/）を nginx が配信する前にこのエンドポイントを
    照会させることで、直接URL・curl・保存済みURLからの取得もサーバー側で遮断する。
    設定例は deploy/nginx_pub_gate.conf.example を参照。
    """
    from fastapi.responses import Response
    pub_gate, store, slug, base, key, dbp, secret, epoch, cname, state = _enter_common(request)
    if state == "valid":
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return Response(status_code=401, headers={"Cache-Control": "no-store"})


@router.get("/api/pub-content")
async def public_gate_content(request: Request):
    """観覧内容の本体を返す（サーバー側で観覧クッキーを検証。無効なら 401）。

    nginx が配信する index.html は内容を持たないシェルで、実際の観覧内容は
    必ずここを通る。よって
      - 12時間経過／強制失効／世代リセット後は、開きっぱなしのタブも次回
        ポーリング（最大30秒）で 401 を受けて /enter（失効ページ）へ遷移する
      - URL直打ち・curl・保存済みURL・PWA のどれでも、有効クッキーが無ければ
        内容は取得できない（nginx の設定変更に依存しない）
    """
    import hashlib, os
    from fastapi.responses import Response
    from app.services.public_html import gated_content_path
    pub_gate, store, slug, base, key, dbp, secret, epoch, cname, state = _enter_common(request)
    if state != "valid":
        return Response(status_code=401, headers={"Cache-Control": "no-store"})
    path = gated_content_path(store)
    if not path or not os.path.isfile(path):
        return Response("観覧内容がまだ生成されていません。", status_code=503,
                        media_type="text/plain; charset=utf-8", headers={"Cache-Control": "no-store"})
    with open(path, "rb") as f:
        body = f.read()
    etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})
    return Response(body, media_type="text/html; charset=utf-8",
                    headers={"ETag": etag, "Cache-Control": "no-store"})


@router.get("/api/pub-handoff")
async def public_gate_handoff(request: Request):
    """PWA 用の引き継ぎトークンを返す（有効クッキー保持時のみ）。"""
    from fastapi.responses import JSONResponse
    pub_gate, store, slug, base, key, dbp, secret, epoch, cname, state = _enter_common(request)
    if state != "valid":
        return JSONResponse({"ok": False}, status_code=401, headers={"Cache-Control": "no-store"})
    ts = pub_gate.cookie_issued_ts(request.cookies.get(cname))
    return JSONResponse({"ok": True, "h": pub_gate.issue_handoff(secret, epoch, ts)},
                        headers={"Cache-Control": "no-store"})


@router.get("/api/pub-manifest")
async def public_gate_manifest(request: Request):
    """参加者用の動的 manifest。start_url に引き継ぎトークン h を埋める。

    観覧ページのJSが有効な間に <link rel=manifest> をこのURLに差し替える。
    iOS はホーム画面追加時に manifest を読むため、追加された PWA の start_url は
    /enter?h=<token>&src=pwa となり、初回起動でブラウザと同じ期限のクッキーを得る。
    """
    from fastapi.responses import JSONResponse, Response
    from app.core.config import IS_CLOUD
    if not IS_CLOUD:
        return Response(status_code=404)
    from app import pwa
    pub_gate, store, slug, base, key, dbp, secret, epoch, cname, state = _enter_common(request)
    settings = pwa.get_pwa_settings(request)
    data = pwa.build_manifest_dict("html", settings, slug=slug)
    h = request.query_params.get("h", "")
    pfx = ("/" + slug) if slug else ""
    if h and pub_gate.verify_handoff(secret, epoch, h):
        data["start_url"] = f"{pfx}/enter?h={h}&src=pwa"
    else:
        data["start_url"] = f"{pfx}/enter?src=pwa"
    return JSONResponse(data, media_type="application/manifest+json",
                        headers={"Cache-Control": "no-store"})


@router.get("/api/pub-status")
async def public_gate_status(request: Request):
    """参加者向けHTMLの有効期限ゲート状態を返す（公開・トークン不要）。

    観覧ページの30秒ポーリングから呼ばれ、サーバー時刻・サーバー署名で
    有効/失効を判定する。gate=false の環境（secret 未設定）では従来どおり
    クライアント側判定のみで動作する。
    """
    from fastapi.responses import JSONResponse
    from app.services import pub_gate

    store = getattr(request.state, "store", None)
    slug = store.slug if store else ""
    secret = pub_gate.secret_for(store)
    if not secret:
        payload = {"gate": False, "state": "valid", "remain": 0}
    else:
        epoch = pub_gate.get_epoch(pub_gate.db_path_for(store))
        cval = request.cookies.get(pub_gate.cookie_name(slug))
        state, remain = pub_gate.check_cookie_value(secret, epoch, cval)
        import time as _t
        payload = {"gate": True, "state": state, "remain": remain,
                   "expires_at": int(_t.time() + remain) if state == "valid" else 0}
        if request.query_params.get("debug") == "1":
            payload["debug"] = {
                "slug": slug or "default",
                "epoch": epoch,
                "cookie_present": bool(cval),
                "scheme": request.url.scheme,
                "x_forwarded_proto": request.headers.get("x-forwarded-proto", ""),
            }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


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


# ---- 過去成績（参加者向け・ライブ配信。/api 配下なので nginx 無改修で届く）----
from app.presentation.templates import templates as _templates
from app.application.racer_service import RacerService as _RacerService

_history_cache = {}      # store_id -> (monotonic_ts, data)
_HISTORY_TTL = 60        # 集計は結果確定時しか変わらないため60秒キャッシュ


@router.get("/api/history", response_class=HTMLResponse)
async def public_history(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """全レーサーの過去成績（参加数・優勝数・入賞数）の一覧ページを返す（公開）。"""
    import time as _time
    store = getattr(request.state, "store", None)
    slug = (getattr(store, "slug", "") or "")
    sid = getattr(store, "id", 0)

    now = _time.monotonic()
    cached = _history_cache.get(sid)
    if cached and (now - cached[0]) < _HISTORY_TTL:
        data = cached[1]
    else:
        data = await _RacerService(db).history_overview()
        _history_cache[sid] = (now, data)

    return _templates.TemplateResponse("viewer/history.html", {
        "request": request,
        "prefix": (f"/{slug}" if slug else ""),
        "back_href": "javascript:history.back()",
        "race_total": data.get("race_total", 0),
        "open_total": data.get("open_total", 0),
        "ltd_total": data.get("ltd_total", 0),
        "racers": data.get("racers", []),
    })


# ---- 過去レース結果一覧（「レースの結果」ボタン）。無限スクロールで順次読み込み ----
_races_cache = {}        # (store_id, licensed) -> (ts, (races,total))  ※初回ページ(絞込なし)のみ
_RACES_TTL = 60
_RACES_PAGE = 8          # 1バッチの件数（初回・追記とも）


async def _m4laps_licensed(request, db) -> bool:
    from app.core.config import IS_CLOUD
    if not IS_CLOUD:
        return False
    try:
        async with db.execute(
            "SELECT value FROM app_settings WHERE key='m4laps_licensed'"
        ) as cur:
            row = await cur.fetchone()
        return bool(row) and (row[0] == "1")
    except Exception:
        return False


async def _record_holder_for(db, tid: int):
    from app.application import timing_racer_best_service as _rbest
    from app.application import qualifying_records as _qr
    try:
        raw = await _rbest.record_holders_for_tournament(db, tid, include_finals=True)
    except Exception:
        return None
    if not raw:
        return None
    name_by_eid = {}
    try:
        async with db.execute(
            "SELECT e.id AS eid, r.name AS name FROM entries e "
            "JOIN racers r ON r.id = e.racer_id WHERE e.tournament_id = ?", (tid,)
        ) as cur:
            for row in await cur.fetchall():
                name_by_eid[row["eid"]] = row["name"]
    except Exception:
        name_by_eid = {}
    try:
        return _qr.format_records_display(raw, name_by_eid, {})
    except Exception:
        return None


def _races_filters(qp):
    f_from = (qp.get("from") or "").strip()
    f_to = (qp.get("to") or "").strip()
    f_reg = (qp.get("reg") or "").strip()
    if f_reg not in ("open", "ltd"):
        f_reg = ""
    return f_from, f_to, f_reg


async def _races_batch(db, licensed, *, f_from, f_to, f_reg, offset, limit):
    data = await _RacerService(db).race_results_list(
        date_from=f_from or None, date_to=f_to or None,
        reg=f_reg or None, offset=offset, limit=limit,
    )
    races = data.get("races", [])
    if licensed:
        for r in races:
            try:
                r["record"] = await _record_holder_for(db, r["id"])
            except Exception:
                r["record"] = None
    return races, data.get("total", len(races))


@router.get("/api/races", response_class=HTMLResponse)
async def public_races(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """レース結果一覧の初回ページ。以降はスクロールで /api/races/fragment を読む。"""
    import time as _time
    store = getattr(request.state, "store", None)
    slug = (getattr(store, "slug", "") or "")
    sid = getattr(store, "id", 0)
    prefix = (f"/{slug}" if slug else "")

    f_from, f_to, f_reg = _races_filters(request.query_params)
    active_filter = bool(f_from or f_to or f_reg)
    show_all = (request.query_params.get("all") == "1")   # JS無効時のフォールバック
    licensed = await _m4laps_licensed(request, db)

    limit = None if show_all else _RACES_PAGE

    if (not active_filter) and (not show_all):
        now = _time.monotonic()
        ck = (sid, bool(licensed))
        cached = _races_cache.get(ck)
        if cached and (now - cached[0]) < _RACES_TTL:
            races, total = cached[1]
        else:
            races, total = await _races_batch(db, licensed, f_from=f_from, f_to=f_to,
                                              f_reg=f_reg, offset=0, limit=limit)
            _races_cache[ck] = (now, (races, total))
    else:
        races, total = await _races_batch(db, licensed, f_from=f_from, f_to=f_to,
                                          f_reg=f_reg, offset=0, limit=limit)

    shown = len(races)
    return _templates.TemplateResponse("viewer/races.html", {
        "request": request, "prefix": prefix,
        "back_href": "javascript:history.back()",
        "m4laps": bool(licensed),
        "races": races, "total": total, "shown": shown,
        "page_size": _RACES_PAGE, "next_offset": shown,
        "has_more": (not show_all) and (shown < total),
        "active_filter": active_filter, "show_all": show_all,
        "f_from": f_from, "f_to": f_to, "f_reg": f_reg,
    })


@router.get("/api/races/fragment", response_class=HTMLResponse)
async def public_races_fragment(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """スクロール追記用のカード断片（cardのHTMLのみ）を返す。"""
    store = getattr(request.state, "store", None)
    slug = (getattr(store, "slug", "") or "")
    prefix = (f"/{slug}" if slug else "")
    qp = request.query_params
    f_from, f_to, f_reg = _races_filters(qp)
    try:
        offset = int(qp.get("offset") or 0)
    except ValueError:
        offset = 0
    try:
        limit = int(qp.get("limit") or _RACES_PAGE)
    except ValueError:
        limit = _RACES_PAGE
    limit = max(1, min(limit, 30))
    licensed = await _m4laps_licensed(request, db)
    races, _total = await _races_batch(db, licensed, f_from=f_from, f_to=f_to,
                                       f_reg=f_reg, offset=offset, limit=limit)
    return _templates.TemplateResponse("viewer/_race_cards.html", {
        "request": request, "prefix": prefix, "m4laps": bool(licensed),
        "races": races,
    })


@router.get("/api/history/racer/{racer_id}")
async def public_history_racer(racer_id: int, request: Request,
                               db: aiosqlite.Connection = Depends(get_db)):
    """1レーサーの大会別成績（過去成績ページの詳細）をJSONで返す（公開）。"""
    from fastapi.responses import JSONResponse
    r = await _RacerService(db).achievements(racer_id, "1900-01-01", "")
    if not r:
        return JSONResponse({"ok": False}, status_code=404)
    racer = r.get("racer")
    return JSONResponse({
        "ok": True,
        "name": (racer["name"] if racer else ""),
        "race_count": r.get("race_count", 0),
        "win_rate": r.get("win_rate", 0),
        "podium_rate": r.get("podium_rate", 0),
        "rows": r.get("rows", []),
    })
