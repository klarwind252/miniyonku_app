"""レーサー管理ルーター（薄いHTTP層）。ロジックは RacerService に委譲。"""
import csv
import io

import aiosqlite
from fastapi import APIRouter, Request, Depends, Form, Query, UploadFile, File
from fastapi.responses import (
    HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse,
)

from app.infrastructure.db.connection import get_db
from app.application.racer_service import RacerService
from app.presentation.templates import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def racer_list(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    sort: str = Query("yomi"),
    order: str = Query("asc"),
    q: str = Query(""),
    kana: str = Query(""),
    regular: str = Query(""),
):
    svc = RacerService(db)
    racers, kana, regular, day_types = await svc.list_racers(sort, order, q, kana, regular)
    today_ctx = await svc.get_today_context()

    next_order = "desc" if order == "asc" else "asc"

    # entry_map と today_tournament_ids をJSONに変換
    entry_map_for_js = {
        str(rid): {str(tid): ts for tid, ts in entries.items()}
        for rid, entries in today_ctx.get("entry_map", {}).items()
    }
    today_tid_list = [t["id"] for t in today_ctx.get("today_tournaments", [])]

    from app.domain.kana import KANA_ROW_ORDER
    return templates.TemplateResponse("admin/racers.html", {
        "request": request,
        "racers": racers,
        "sort": sort,
        "order": order,
        "next_order": next_order,
        "q": q,
        "kana": kana,
        "kana_rows": KANA_ROW_ORDER,
        "regular": regular,
        "day_types": day_types,
        "entry_map_js": entry_map_for_js,
        "today_tournament_ids": today_tid_list,
        **today_ctx,
    })


async def _racer_list_error(request, db, error, form_name="", form_yomi="",
                            form_is_child=0, form_is_regular=0):
    """追加/編集の重複エラー時に一覧を再描画する。"""
    svc = RacerService(db)
    racers, kana, regular, day_types = await svc.list_racers("yomi", "asc", "", "", "")
    today_ctx = await svc.get_today_context()
    from app.domain.kana import KANA_ROW_ORDER
    ctx = {
        "request": request, "racers": racers, "sort": "yomi", "order": "asc",
        "next_order": "desc", "q": "", "kana": "", "kana_rows": KANA_ROW_ORDER,
        "regular": "", "day_types": day_types,
        "error": error, "form_name": form_name, "form_yomi": form_yomi,
        "form_is_child": form_is_child, "form_is_regular": form_is_regular,
        **today_ctx,
    }
    return templates.TemplateResponse("admin/racers.html", ctx)


@router.post("/add")
async def racer_add(
    request: Request,
    name: str = Form(...),
    yomi: str = Form(""),
    is_child: str = Form(""),
    is_regular: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    name = name.strip()
    yomi = yomi.strip()
    is_child_val = 1 if is_child == "1" else 0
    is_regular_val = 1 if is_regular == "1" else 0
    svc = RacerService(db)
    err = await svc.add_racer(name, yomi, is_child_val, is_regular_val)
    if err:
        return await _racer_list_error(request, db, err, name, yomi,
                                       is_child_val, is_regular_val)
    return RedirectResponse(url="/admin/racers/", status_code=303)


@router.post("/delete/{racer_id}")
async def racer_delete(request: Request, racer_id: int,
                       db: aiosqlite.Connection = Depends(get_db)):
    err = await RacerService(db).delete_racer(racer_id)
    if err:
        # エントリー履歴がある等で削除できない場合は、一覧へ理由を表示して戻す
        # （従来はここで FOREIGN KEY constraint failed により500エラーになっていた）
        return await _racer_list_error(request, db, err)
    return RedirectResponse(url="/admin/racers/", status_code=303)


@router.post("/edit/{racer_id}")
async def racer_edit(
    request: Request,
    racer_id: int,
    name: str = Form(...),
    yomi: str = Form(""),
    is_child: str = Form(""),
    is_regular: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    name = name.strip()
    yomi = yomi.strip()
    is_child_val = 1 if is_child == "1" else 0
    is_regular_val = 1 if is_regular == "1" else 0
    svc = RacerService(db)
    err = await svc.edit_racer(racer_id, name, yomi, is_child_val, is_regular_val)
    if err:
        return JSONResponse({"ok": False, "error": err})
    return JSONResponse({"ok": True, "is_child": is_child_val, "is_regular": is_regular_val})


@router.post("/entry-single/{racer_id}/{tournament_id}")
async def entry_single(racer_id: int, tournament_id: int,
                       db: aiosqlite.Connection = Depends(get_db)):
    return JSONResponse(await RacerService(db).entry_single(racer_id, tournament_id))


@router.post("/remove-entry-single/{racer_id}/{tournament_id}")
async def remove_entry_single(racer_id: int, tournament_id: int,
                              db: aiosqlite.Connection = Depends(get_db)):
    return JSONResponse(await RacerService(db).remove_entry_single(racer_id, tournament_id))


@router.get("/visit-data-api")
async def visit_data_api(db: aiosqlite.Connection = Depends(get_db)):
    return JSONResponse(await RacerService(db).visit_data())


@router.post("/entry-today/{racer_id}")
async def entry_today(racer_id: int, db: aiosqlite.Connection = Depends(get_db)):
    result = await RacerService(db).entry_today(racer_id)
    if result is None:
        return JSONResponse({"ok": False, "error": "レーサーが見つかりません"}, status_code=404)
    return JSONResponse(result)


@router.post("/cancel-visit/{racer_id}")
async def cancel_visit(racer_id: int, db: aiosqlite.Connection = Depends(get_db)):
    return JSONResponse(await RacerService(db).cancel_visit(racer_id))


@router.get("/export")
async def export_csv(db: aiosqlite.Connection = Depends(get_db)):
    rows = await RacerService(db).export_rows()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    buf.seek(0)
    data = buf.getvalue().encode("utf-8-sig")  # ExcelでBOM付きUTF-8
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=racers.csv"},
    )


@router.post("/import-preview")
async def import_preview(file: UploadFile = File(...),
                         db: aiosqlite.Connection = Depends(get_db)):
    raw = await file.read()
    result, errors = await RacerService(db).import_preview(raw)
    if result is None:
        return JSONResponse({"ok": False, "errors": errors})
    return JSONResponse(result)


@router.post("/import-commit")
async def import_commit(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    body = await request.json()
    rows = body.get("rows", [])
    return JSONResponse(await RacerService(db).import_commit(rows))


@router.get("/achievements/{racer_id}", response_class=HTMLResponse)
async def racer_achievements(
    racer_id: int,
    request: Request,
    start: str = Query(""),
    end: str = Query(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    result = await RacerService(db).achievements(racer_id, start, end)
    if result is None:
        return RedirectResponse(url="/admin/racers/")
    return templates.TemplateResponse("admin/racer_achievements.html", {
        "request": request,
        **result,
    })