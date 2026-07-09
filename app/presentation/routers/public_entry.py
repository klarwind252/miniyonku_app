"""公開エントリーフォーム（/entry）。認証不要（auth の _PUBLIC_PREFIXES 登録済み）。"""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.infrastructure.db.connection import DB_PATH
from app.application.pre_entry_service import PreEntryService
from app.presentation.templates import templates

router = APIRouter()


def _entry_db_path(request: Request) -> str:
    """現在のリクエストが属する店舗のDBパス。未解決（オンプレ）なら既定DB。"""
    store = getattr(request.state, "store", None)
    return store.db_path if store else DB_PATH


def _entry_prefix(request: Request) -> str:
    """URL接頭辞（既定店舗は "" 、店舗2〜は "/store2" 等）。"""
    store = getattr(request.state, "store", None)
    return store.prefix if store else ""


@router.get("/entry")
async def entry_select(request: Request):
    svc = PreEntryService(_entry_db_path(request))
    rows = await svc.list_open_form_races()
    return templates.TemplateResponse("entry_select.html", {
        "request": request,
        "races": rows,
        "prefix": _entry_prefix(request),
    })


@router.get("/entry/{tid}")
async def entry_form(tid: int, request: Request):
    prefix = _entry_prefix(request)
    svc = PreEntryService(_entry_db_path(request))
    t, token, closed = await svc.prepare_form(tid)
    if t is None:
        return RedirectResponse(url=f"{prefix}/entry", status_code=303)
    return templates.TemplateResponse("entry_form.html", {
        "request": request,
        "race": dict(t),
        "token": token,
        "closed": closed,
        "prefix": prefix,
        "done": False,
        "error": request.query_params.get("error", ""),
    })


@router.post("/entry/{tid}")
async def entry_submit(tid: int, request: Request):
    prefix = _entry_prefix(request)
    svc = PreEntryService(_entry_db_path(request))
    form = await request.form()

    status, t, added = await svc.submit(tid, form)
    if status == "redirect_list":
        return RedirectResponse(url=f"{prefix}/entry", status_code=303)
    if status.startswith("error:"):
        code = status.split(":", 1)[1]
        return RedirectResponse(url=f"{prefix}/entry/{tid}?error={code}", status_code=303)

    return templates.TemplateResponse("entry_form.html", {
        "request": request,
        "race": dict(t),
        "token": "",
        "closed": False,
        "prefix": prefix,
        "done": True,
        "added": added,
        "error": "",
    })