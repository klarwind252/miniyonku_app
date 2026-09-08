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
            ua = request.headers.get("user-agent", "")
            access_stats.record_hit(sid, tid, cid, ua)
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

    案②（サーバー署名方式・pub_gate 参照）:
      レーサー用QRは /enter?k=<署名トークン> を指す。k は時刻窓ごとに自動更新され、
      サーバーは「現在」と「1つ前」の窓の k のみ受理する（過去URLの使い回しは
      最長48時間で必ず失効）。有効な k で通過した端末には署名付きクッキー
      （発行時刻＋HMAC、有効24時間）を発行し、観覧ページは /api/pub-status で
      サーバー判定を受けて期限切れなら自動更新を停止する。

    従来の localStorage 記録（案①）はオフライン時のフォールバック判定用として
    引き続き書き込む（正の判定はサーバー側が担う）。

    PWA（ホーム画面アイコン起動 ?src=pwa）の扱い:
      - クッキーが有効 → そのまま通過（※延長はしない）
      - クッキーが全く無い（そのアイコンの初回起動）→ 初回のみ発行して通過
      - クッキーが期限切れ → 失効ページ（カメラで最新QRを読むよう案内）
      これにより「アイコンをタップするだけで24時間が無限に延長される」問題と、
      期限切れオーバーレイの更新ボタン（?src=rescan）による無条件延長を廃止する。

    secret（店舗 admin_token / 環境変数 ADMIN_TOKEN）が未設定の環境では
    ゲート無効＝従来どおりの動作（後方互換）。
    """
    from app.services import pub_gate

    store = getattr(request.state, "store", None)
    slug = store.slug if store else ""
    base = f"/{slug}/" if slug else "/"
    key = f"m4_pub_issued_{slug or 'default'}"
    src = request.query_params.get("src", "")
    is_pwa = (src == "pwa")

    secret = pub_gate.secret_for(store)
    epoch = pub_gate.get_epoch(pub_gate.db_path_for(store))

    def _renewal_allowed() -> bool:
        """新たな24時間を発行してよいか（＝実質「会場でスキャンできる時間帯か」）。

        QRは固定運用（印刷・常設）のため、URL自体では「本物の再スキャン」と
        「履歴・ブックマークからの開き直し」を区別できない。そこで発行可否を
        店舗の営業時間設定（restrict_hours / access_start / access_end）に委ねる:
          - 営業時間制限が有効な店舗 → 営業時間内のみ発行（時間外は既存の
            有効クッキーで通過はできるが、延長はされない）
          - 制限なし → 常時発行（従来どおり）
        これにより、営業時間外に自宅からURLを開き直しても延長できず、
        発行済みの24時間が切れた時点で必ず失効する。
        """
        try:
            if store is not None and getattr(store, "restrict_hours", False):
                from app import registry
                return bool(registry.is_store_open(store))
        except Exception:
            pass
        return True

    def _pass_page(set_js: str) -> HTMLResponse:
        html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>読み込み中…</title></head><body>
<p style="font-family:sans-serif;text-align:center;margin-top:40vh;color:#555">読み込み中…</p>
<script>
{set_js}
location.replace({base!r});
</script></body></html>"""
        return HTMLResponse(html)

    def _blocked_page(pwa: bool, reason: str = "") -> HTMLResponse:
        import html as _html
        reason_safe = _html.escape(reason)[:80]  # 表示専用・エスケープ＋長さ制限
        # 失効：localStorage も 0 に落とし、観覧ページ側のローカル判定も確実に失効させる
        extra = ("<div style=\"margin-top:14px;font-size:12px;opacity:.75;line-height:1.7\">"
                 "ホーム画面アイコンの有効期限も切れています。<br>"
                 "カメラでQRコードを読み取るとブラウザで観覧できます。"
                 "</div>") if pwa else ""
        html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>有効期限切れ</title></head>
<body style="margin:0;background:#141821;color:#fff;font-family:sans-serif;">
<div style="min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px;box-sizing:border-box;">
  <div style="font-size:22px;font-weight:bold;margin-bottom:14px">観覧の有効期限が切れました</div>
  <div style="font-size:15px;line-height:1.7;margin-bottom:22px;opacity:.9">会場のQRコードを<br>もう一度スキャンしてください。</div>
  <div style="display:inline-block;background:#2c3e50;color:#cfd8e3;padding:12px 22px;border-radius:8px;font-size:15px;font-weight:bold;line-height:1.6">受付時間内にQRコードを再スキャンすると<br>新たに12時間観覧できます</div>
  {extra}
  <div style="margin-top:20px;font-size:11px;opacity:.45">code: {reason_safe}</div>
</div>
<script>try {{ localStorage.setItem({key!r}, "0"); }} catch(e) {{}}</script>
</body></html>"""
        return HTMLResponse(html, status_code=403)

    # ---- ゲート無効（secret 未設定）：従来動作（後方互換） ----
    if not secret:
        if is_pwa:
            set_js = f"""try {{
  if (!localStorage.getItem({key!r})) {{ localStorage.setItem({key!r}, String(Date.now())); }}
}} catch(e) {{}}"""
        else:
            set_js = f"""try {{ localStorage.setItem({key!r}, String(Date.now())); }} catch(e) {{}}"""
        return _pass_page(set_js)

    # ---- ゲート有効 ----
    k = request.query_params.get("k", "")
    cname = pub_gate.cookie_name(slug)
    state, _remain = pub_gate.check_cookie_value(secret, epoch, request.cookies.get(cname))
    _fwd_proto = request.headers.get("x-forwarded-proto", "")
    secure = (request.url.scheme == "https") or (_fwd_proto == "https")

    def _issue(resp: HTMLResponse) -> HTMLResponse:
        resp.set_cookie(
            cname, pub_gate.issue_cookie_value(secret, epoch),
            max_age=pub_gate.TTL_SEC, path="/",
            httponly=True, samesite="lax", secure=secure,
        )
        return resp

    renew_js = f"""try {{ localStorage.setItem({key!r}, String(Date.now())); }} catch(e) {{}}"""
    keep_js = f"""try {{
  if (!localStorage.getItem({key!r})) {{ localStorage.setItem({key!r}, String(Date.now())); }}
}} catch(e) {{}}"""

    if pub_gate.verify_qr_token(secret, epoch, k):
        # 署名トークン付きURL（任意運用・常に有効）：新たな24時間を発行
        return _issue(_pass_page(renew_js))

    if _renewal_allowed() and not is_pwa:
        # 固定QRのスキャン（および同URLの開き直し）：発行可能時間帯なら
        # 新たな24時間を発行する。QRが固定である以上、URLだけでは再スキャンと
        # 開き直しを区別できないため、可否は営業時間設定＋世代リセットで統制する。
        return _issue(_pass_page(renew_js))

    if state == "valid":
        # 有効期間内の再訪（PWAアイコン起動・時間外の開き直し等）：
        # 通過はさせるが延長はしない
        return _pass_page(keep_js)

    if is_pwa and state == "none" and _renewal_allowed():
        # PWAアイコンの初回起動（クッキーが一度も無い）は、発行可能な時間帯の
        # ときだけ初回発行する。
        # 【重要】iOS の PWA は Safari とは別の独立した Cookie ストアを持ち、
        # 未使用7日で Cookie を自動削除する。さらに利用者がサイトデータを消せば
        # 任意に state=="none" を作れる。この分岐を無条件発行にすると、
        # 「PWA を開くたび／消すたびに新しい12時間がもらえる」抜け穴になり、
        # 強制失効も営業時間ゲートも回避されてしまう（実際に回避が確認された）。
        # そのため _renewal_allowed() を必須条件にして、通常の /enter と同じ
        # 統制下に置く。営業時間外・発行不可時は下の失効処理に落とす。
        return _issue(_pass_page(renew_js))

    # 発行不可時間帯かつ有効クッキー無し → 失効（延長させない）
    # 理由コード: CLOSED=営業時間外 / COOKIE_<state>=クッキー状態 / SRC_<src>
    # 反射を避けるため src は既知値のみ採用（未知値は "OTHER"）。
    src_tag = src.upper() if src in ("pwa", "rescan") else ("OTHER" if src else "")
    reason = "CLOSED+COOKIE_" + state.upper()
    if src_tag:
        reason += "+SRC_" + src_tag
    return _blocked_page(pwa=(is_pwa or src == "rescan"), reason=reason)


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
        payload = {"gate": True, "state": state, "remain": remain}
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
        "back_href": (f"/{slug}/" if slug else "/"),
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
        "back_href": (f"/{slug}/" if slug else "/"),
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
