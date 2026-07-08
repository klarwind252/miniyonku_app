"""
デモ（お試し）予約の公開ルーター。

エンドポイント（すべて認証不要 = auth.py の _PUBLIC_PREFIXES に "/reserve" を登録済み）:
  GET  /reserve                     … 予約フォーム（カレンダー＋時間枠）
  GET  /reserve/availability?day=…  … 指定日の空き枠 JSON（カレンダーUIが取得）
  POST /reserve                     … 予約確定 → デモ店舗を確保・鍵再生成・メール送信

クラウド版（IS_CLOUD）専用機能。オンプレ版では 404 を返す。
DB へは control.db（demo モジュール）経由でアクセスし、レース用DBには触れない。
"""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from app import demo
from app.config import IS_CLOUD
from app.emailer import send_mail

router = APIRouter(tags=["demo-reserve"])

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[1] / "templates")
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _guard() -> Response | None:
    """クラウド版かつデモ有効でなければ 404。"""
    if not IS_CLOUD or not demo.enabled():
        return Response(status_code=404)
    return None


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def reserve_form(request: Request):
    g = _guard()
    if g:
        return g
    return templates.TemplateResponse("demo_reserve/form.html", {
        "request": request,
        "days": demo.list_days(),
        "slot_minutes": demo.slot_minutes(),
        "error": request.query_params.get("error", ""),
    })


@router.get("/availability")
async def availability(request: Request, day: str = ""):
    g = _guard()
    if g:
        return g
    return JSONResponse({"day": day, "slots": demo.slots_for_day(day)})


@router.post("", response_class=HTMLResponse)
@router.post("/", response_class=HTMLResponse)
async def reserve_submit(
    request: Request,
    email: str = Form(...),
    slot: str = Form(...),          # 'YYYY-MM-DD HH:MM'
    agree: str = Form(""),          # 個人情報を書かない旨の同意チェック
    website: str = Form(""),        # ハニーポット（人間は空）
):
    g = _guard()
    if g:
        return g

    # ボット対策：ハニーポットに値があれば、静かに完了扱い（実際には何もしない）
    if website.strip():
        return templates.TemplateResponse("demo_reserve/done.html", {
            "request": request, "email": "", "start": "", "end": "",
            "mail_ok": True,
        })

    email = email.strip()
    if not _EMAIL_RE.match(email):
        return _err(request, "メールアドレスの形式が正しくありません。")
    if agree != "1":
        return _err(request, "注意事項への同意にチェックしてください。")

    # 予約確定（店舗確保＋鍵再生成）
    try:
        row, store, admin_token, view_token = demo.reserve(email, slot)
    except demo.SlotFull as e:
        return _err(request, str(e))
    except demo.SlotInvalid as e:
        return _err(request, str(e))
    except Exception as e:
        print(f"[demo] reserve error: {e}", flush=True)
        return _err(request, "予約処理でエラーが発生しました。時間をおいて再度お試しください。")

    start = demo.parse_dt(row["start_dt"])
    end = start + timedelta(minutes=demo.slot_minutes())
    urls = demo.build_urls(store, admin_token, view_token)
    subject, body = demo.build_mail(email, store, start, end, urls)

    # メール送信。失敗したら予約を取り消して店舗を解放し、再試行を促す。
    mail_ok = True
    try:
        await send_mail(email, subject, body)
    except Exception as e:
        mail_ok = False
        print(f"[demo] mail send failed: {e}", flush=True)
        demo.cancel_reservation(row["id"])
        return _err(
            request,
            "確認メールの送信に失敗しました。メールアドレスをご確認のうえ、"
            "もう一度お試しください。",
        )
    else:
        demo.mark_mailed(row["id"])

    return templates.TemplateResponse("demo_reserve/done.html", {
        "request": request,
        "email": email,
        "start": demo.fmt_dt(start),
        "end": end.strftime("%H:%M"),
        "mail_ok": mail_ok,
    })


def _err(request: Request, msg: str) -> HTMLResponse:
    return templates.TemplateResponse("demo_reserve/form.html", {
        "request": request,
        "days": demo.list_days(),
        "slot_minutes": demo.slot_minutes(),
        "error": msg,
    }, status_code=400)
