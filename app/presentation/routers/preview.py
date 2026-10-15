"""店舗固定URLの『事前告知ページ』（開催前お知らせ）。

通常のレース運用（日替わり2語ゲート／観覧URL）とは別系統。店舗ごとに推測され
にくい恒久トークンURL（/api/preview/<token>）を1本だけ持ち、管理者がモーダルで選んだ
「未開催レース」1件の情報（レース情報・事前エントリー・コースレイアウト・
タイムスケジュール・備考）を読み取り専用で表示する。

ポイント:
  - トークンは app_settings.key='preview_token' に保存（店舗DBごとに1本・恒久）。
    レース作成時に ensure_preview_token() で自動発行。アクセス時にも無ければ発行。
  - 表示対象レースは app_settings.key='preview_tournament_id'（管理者がモーダルで設定）。
    未設定時は「今日以降で最も近い開催」を自動採用するフォールバックを持つ。
  - 開催日が過ぎたら自動で観覧不可（closed 画面）。日付判定は localtime。
  - 中身は「読み込み時点のDB内容」を表示する普通のHP方式（自動追従なし）。
    Googleフォーム反映・手動反映で更新された内容は、ユーザーが再読込（または開き直し）
    したときに反映される。

公開ルーター（router）は /api/preview/<token>（nginxが既にアプリへ転送する /api 配下＝
nginx設定変更不要）、管理ルーター（admin_router）は /admin/preview/... で main.py から登録する。
/admin/preview/* は既存のクラウド認証（admin Cookie）で保護される。
"""
from __future__ import annotations

import secrets

import aiosqlite
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse

import html as _html
import re as _re
from markupsafe import Markup

from app.infrastructure.db.connection import get_db
from app.presentation.templates import templates
from app.domain.finalists import calc_finalists
from app.presentation.routers.tournaments import (
    TIME_SLOT_LABELS, QUALIFYING_LABELS, get_regulation_labels,
)

# ── URL自動リンク化（備考・タイムスケジュール等のテキスト用）────────────────
#   テキストをHTMLエスケープしてから http(s):// のURLを <a> に変換する。
#   先にエスケープするので XSS 安全（& は &amp; になり、href 属性としても正しい）。
_URL_RE = _re.compile(r'(https?://[^\s<>"\')]+)')


def _linkify(text) -> Markup:
    if not text:
        return Markup("")
    esc = _html.escape(str(text))

    def _repl(m):
        u = m.group(1)
        return (f'<a href="{u}" target="_blank" rel="noopener" '
                f'style="color:#2563eb;text-decoration:underline;word-break:break-all">{u}</a>')

    return Markup(_URL_RE.sub(_repl, esc))


# テンプレートから {{ value | linkify }} で使えるよう登録（既存があれば尊重）。
templates.env.filters.setdefault("linkify", _linkify)

_WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _format_date_ja(s) -> str:
    """'2026-12-13' → '2026年12月13日（日）'。失敗時は元文字列。"""
    import datetime
    if not s:
        return ""
    try:
        d = datetime.date.fromisoformat(str(s).strip()[:10])
        return f"{d.year}年{d.month}月{d.day}日（{_WEEKDAYS_JA[d.weekday()]}）"
    except Exception:
        return str(s)

router = APIRouter()        # 公開：/p/...
admin_router = APIRouter()  # 管理：/admin/preview/...

_TOKEN_KEY = "preview_token"
_SELECTED_KEY = "preview_tournament_id"
_ASSET_KINDS = ("course", "schedule", "remarks")


# ───────────────────────── 共通ヘルパ ─────────────────────────

async def _get_setting(db: aiosqlite.Connection, key: str) -> str:
    try:
        async with db.execute(
            "SELECT value FROM app_settings WHERE key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return ""
        try:
            return (row["value"] or "")
        except Exception:
            return (row[0] or "")
    except Exception:
        return ""


async def _set_setting(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        (key, value),
    )
    await db.commit()


async def ensure_preview_token(db: aiosqlite.Connection) -> str:
    """店舗DBの恒久プレビュートークンを取得。無ければ発行して返す。"""
    token = await _get_setting(db, _TOKEN_KEY)
    if token:
        return token
    token = secrets.token_urlsafe(16)  # URL安全・推測困難（約22文字）
    await _set_setting(db, _TOKEN_KEY, token)
    return token


def _public_base(request: Request) -> str:
    """参加者向け公開URLのベース（クラウド=絶対URL / オンプレ=相対の店舗prefix）。"""
    from app.core.config import IS_CLOUD, PUBLIC_BASE_URL
    store = getattr(request.state, "store", None)
    pfx = store.prefix if store else ""
    if IS_CLOUD and PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}{pfx}"
    return pfx


async def _store_name(db: aiosqlite.Connection, request: Request) -> str:
    store = getattr(request.state, "store", None)
    if store is not None and getattr(store, "name", ""):
        return store.name or ""
    return await _get_setting(db, "store_name")


async def _load_assets(db: aiosqlite.Connection, tid: int) -> dict:
    out = {k: [] for k in _ASSET_KINDS}
    try:
        async with db.execute(
            "SELECT kind, seq, name, data_uri FROM race_assets "
            "WHERE tournament_id=? ORDER BY kind, seq",
            (tid,),
        ) as cur:
            async for r in cur:
                if r["kind"] in out:
                    out[r["kind"]].append(
                        {"seq": r["seq"], "name": r["name"] or "", "data_uri": r["data_uri"]}
                    )
    except Exception:
        pass
    return out


async def _load_pre_entries(db: aiosqlite.Connection, tid: int) -> list:
    async with db.execute(
        "SELECT seq_no, name, yomi, prefecture, is_child, is_walkin "
        "FROM pre_entries WHERE tournament_id=? ORDER BY seq_no",
        (tid,),
    ) as cur:
        return await cur.fetchall()


async def _get_tournament(db: aiosqlite.Connection, tid: int):
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        return await cur.fetchone()


async def _auto_pick(db: aiosqlite.Connection):
    """今日以降で最も近い開催を1件。無ければ None。"""
    async with db.execute(
        "SELECT * FROM tournaments "
        "WHERE date >= date('now','localtime') "
        "ORDER BY date ASC, id ASC LIMIT 1"
    ) as cur:
        return await cur.fetchone()


def _date_passed(date_str: str) -> bool:
    """開催日が過ぎている（＝今日より前）か。空は False 扱い。"""
    import datetime
    if not date_str:
        return False
    try:
        d = datetime.date.fromisoformat(date_str.strip()[:10])
    except Exception:
        return False
    return d < datetime.date.today()


async def _resolve_target(db: aiosqlite.Connection):
    """(tournament_row|None, state) を返す。
    state: 'ok'（表示可）/ 'closed'（開催日経過で観覧不可）/ 'none'（対象なし）。"""
    sel = await _get_setting(db, _SELECTED_KEY)
    if sel:
        try:
            tid = int(sel)
        except Exception:
            tid = 0
        if tid:
            t = await _get_tournament(db, tid)
            if t is None:
                return None, "none"
            if _date_passed(t["date"]):
                return t, "closed"
            return t, "ok"
    # 明示選択なし → 直近の未開催へフォールバック
    t = await _auto_pick(db)
    if t is None:
        return None, "none"
    return t, "ok"


# ───────────────────────── 公開ページ ─────────────────────────
# 中身は「読み込み時点のDB内容」をそのまま表示する普通のHP方式。自動追従（ポーリング）
# はしない。Googleフォーム反映・手動反映で更新された内容は、ユーザーが再読込（または
# 開き直し）したときに反映される。

@router.get("/api/preview/{token}", response_class=HTMLResponse)
async def preview_page(token: str, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    real = await ensure_preview_token(db)
    if (not token) or (not secrets.compare_digest(token, real)):
        return PlainTextResponse("404 Not Found", status_code=404)

    t, state = await _resolve_target(db)
    prefix = getattr(getattr(request.state, "store", None), "prefix", "") or ""

    if t is None:
        return templates.TemplateResponse("preview.html", {
            "request": request, "t": None, "state": "none",
            "prefix": prefix, "store_name": await _store_name(db, request),
            "preview_token": token,
        })

    td = dict(t)
    pre_entries = await _load_pre_entries(db, td["id"]) if td.get("pre_entry") else []
    assets = await _load_assets(db, td["id"])

    return templates.TemplateResponse("preview.html", {
        "request": request,
        "t": td,
        "state": state,
        "date_fmt": _format_date_ja(td.get("date")),
        "prefix": prefix,
        "store_name": await _store_name(db, request),
        "pre_entries": pre_entries,
        "race_assets": assets,
        "finalists": calc_finalists(td.get("qualifying_type", ""), td),
        "time_slot_labels": TIME_SLOT_LABELS,
        "qualifying_labels": QUALIFYING_LABELS,
        "regulation_labels": await get_regulation_labels(db),
        "preview_token": token,
    })


# ───────────────────────── 管理API（モーダル用）─────────────────────────

@admin_router.get("/info")
async def preview_admin_info(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """事前告知モーダル用：固定URL・現在の選択・未開催レース一覧を返す。"""
    token = await ensure_preview_token(db)
    sel_raw = await _get_setting(db, _SELECTED_KEY)
    try:
        selected_tid = int(sel_raw) if sel_raw else 0
    except Exception:
        selected_tid = 0

    # 未開催（今日以降）のレース一覧
    races = []
    async with db.execute(
        "SELECT id, name, date, pre_entry FROM tournaments "
        "WHERE date >= date('now','localtime') "
        "ORDER BY date ASC, id ASC"
    ) as cur:
        async for r in cur:
            races.append({
                "id": r["id"], "name": r["name"], "date": r["date"],
                "pre_entry": r["pre_entry"] or 0,
            })

    base = _public_base(request)
    return JSONResponse({
        "token": token,
        "url": f"{base}/api/preview/{token}",
        "selected_tid": selected_tid,
        "races": races,
    })


@admin_router.post("/select")
async def preview_admin_select(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """事前告知で表示するレースを設定する（tournament_id=0 で選択解除＝自動選択に戻す）。"""
    form = await request.form()
    try:
        tid = int(form.get("tournament_id") or 0)
    except Exception:
        tid = 0

    if tid:
        t = await _get_tournament(db, tid)
        if t is None:
            return JSONResponse({"ok": False, "error": "no_such_race"}, status_code=400)

    await _set_setting(db, _SELECTED_KEY, str(tid) if tid else "")

    token = await ensure_preview_token(db)
    base = _public_base(request)
    return JSONResponse({"ok": True, "url": f"{base}/api/preview/{token}", "selected_tid": tid})
