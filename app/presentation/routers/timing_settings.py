"""タイミング計測の設定画面（端末台帳・コースレイアウト）。

14章 DA6/DA8/DA9 の admin 側。計算ロジックは domain/rotation.py を呼ぶ。
既存 admin ルーターと同じ流儀（APIRouter / Depends(get_db) / 共通templates）。
"""

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import aiosqlite

from app.infrastructure.db.connection import get_db
from app.infrastructure.db.repositories.timing_repository import (
    TimingDeviceRepository,
    TimingLayoutRepository,
)
from app.presentation.templates import templates
from app.domain.rotation import LayoutElement, validate_layout, build_course
from app.presentation.routers.m4laps_guard import require_m4laps

router = APIRouter(dependencies=[Depends(require_m4laps)])


# ---------------------------------------------------------------------------
# 端末台帳
# ---------------------------------------------------------------------------

@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    repo = TimingDeviceRepository(db)
    devices = await repo.list_all()
    return templates.TemplateResponse(
        "admin/timing_devices.html",
        {"request": request, "devices": devices},
    )


@router.post("/devices/{node_id}")
async def devices_update(
    node_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    form = await request.form()
    label = (form.get("label") or "").strip()
    mac = (form.get("mac") or "").strip()
    note = (form.get("note") or "").strip()
    # センサー幅(mm)。空欄や不正値は未設定(None)扱い。
    _sw = (form.get("sensor_width_mm") or "").strip()
    try:
        sensor_width_mm = float(_sw) if _sw != "" else None
        if sensor_width_mm is not None and sensor_width_mm <= 0:
            sensor_width_mm = None
    except ValueError:
        sensor_width_mm = None
    repo = TimingDeviceRepository(db)
    dev = await repo.get(node_id)
    if dev is None:
        raise HTTPException(status_code=404, detail="device not found")
    if not label:
        label = dev["label"]
    await repo.update_meta(node_id, label, mac, note, sensor_width_mm=sensor_width_mm)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/timing/devices", status_code=303)


# ---------------------------------------------------------------------------
# コースレイアウト
# ---------------------------------------------------------------------------

SINGLE_LAYOUT_NAME = "メインコース"


async def _get_or_create_single_layout(db: aiosqlite.Connection) -> int:
    """単一コース固定（方針A）。既存の最初のレイアウトを返す。無ければ1つ作る。"""
    repo = TimingLayoutRepository(db)
    layouts = await repo.list_layouts()
    if layouts:
        return layouts[0]["id"]
    return await repo.create_layout(SINGLE_LAYOUT_NAME, 3)


@router.get("/layouts")
async def layouts_page(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """単一コース固定：一覧は廃止し、そのコースの編集画面へ直行する。"""
    from fastapi.responses import RedirectResponse
    lid = await _get_or_create_single_layout(db)
    return RedirectResponse(url=f"/admin/timing/layouts/{lid}/edit", status_code=303)


@router.get("/layouts/{layout_id}/edit", response_class=HTMLResponse)
async def layout_edit_page(
    layout_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    lrepo = TimingLayoutRepository(db)
    drepo = TimingDeviceRepository(db)
    layout = await lrepo.get_layout(layout_id)
    if layout is None:
        raise HTTPException(status_code=404, detail="layout not found")
    elements = await lrepo.get_elements(layout_id)
    # 割当可能な機器（SG/SQ のみ。レイアウトのゲート枠で選ぶ）
    # ⚠ aiosqlite.Row はそのままでは tojson できないため dict に変換する。
    sg_rows = await drepo.list_by_kind("GW")   # S/GはGW実機
    sq_rows = await drepo.list_by_kind("SQ")
    sg_devices = [{"node_id": r["node_id"], "label": r["label"], "note": r["note"]} for r in sg_rows]
    sq_devices = [{"node_id": r["node_id"], "label": r["label"], "note": r["note"]} for r in sq_rows]

    # elements も dict 化（tojson でJSに渡すため）
    elem_list = [
        {"position": e["position"], "kind": e["kind"], "node_id": e["node_id"]}
        for e in elements
    ]

    return templates.TemplateResponse(
        "admin/timing_layout_edit.html",
        {
            "request": request,
            "layout": layout,
            "elements": elem_list,
            "sg_devices": sg_devices,
            "sq_devices": sq_devices,
        },
    )


@router.post("/layouts/{layout_id}/validate")
async def layout_validate(
    layout_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    """要素列を受け取り、確定可否をJSONで返す（保存はしない）。

    body(JSON): {"elements": [{"kind":"SG","node_id":6}, {"kind":"SQ","node_id":0},
                              {"kind":"LC"}, ...]}
    """
    data = await request.json()
    raw = data.get("elements", [])
    layout = [LayoutElement(kind=e["kind"], node_id=e.get("node_id")) for e in raw]
    result = validate_layout(layout)
    course = build_course(layout)
    return JSONResponse({
        "can_commit": result.can_commit,
        "lc_count": course.lc_count,
        "rot_total": course.rot_total,
        "issues": [
            {"severity": i.severity, "code": i.code, "message": i.message}
            for i in result.issues
        ],
    })


@router.post("/layouts/{layout_id}/save")
async def layout_save(
    layout_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    """確定時バリデーションを通してから保存する。

    body(JSON): {"name":..., "target_laps":..., "lap_length_m":float|None, "force":bool,
                 "elements":[...]}
    warning のみなら force=true で保存可。error があれば拒否。
    """
    data = await request.json()
    raw = data.get("elements", [])
    name = (data.get("name") or "コース").strip()
    try:
        target_laps = int(data.get("target_laps") or 3)
    except (ValueError, TypeError):
        target_laps = 3
    force = bool(data.get("force"))

    # 1周の距離(m)。ラップ平均速度の算出に使う。未設定(None)なら速度は「—」表示。
    # 合計距離(m)。整数のみ（小数は切り捨て）。未設定(None)なら速度は「—」表示。
    lap_length_m = data.get("lap_length_m")
    try:
        lap_length_m = int(float(lap_length_m)) if lap_length_m not in (None, "") else None
        if lap_length_m is not None and lap_length_m <= 0:
            lap_length_m = None
    except (TypeError, ValueError):
        lap_length_m = None

    layout = [LayoutElement(kind=e["kind"], node_id=e.get("node_id")) for e in raw]
    result = validate_layout(layout)

    if not result.can_commit:
        return JSONResponse({
            "ok": False,
            "reason": "error",
            "issues": [
                {"severity": i.severity, "code": i.code, "message": i.message}
                for i in result.errors
            ],
        }, status_code=400)

    if result.warnings and not force:
        # 警告があり、まだ確認前 → クライアントに確認を促す
        return JSONResponse({
            "ok": False,
            "reason": "warning",
            "issues": [
                {"severity": i.severity, "code": i.code, "message": i.message}
                for i in result.warnings
            ],
        }, status_code=409)

    lrepo = TimingLayoutRepository(db)
    await lrepo.update_meta(layout_id, name, target_laps, lap_length_m=lap_length_m)
    await lrepo.save_elements(layout_id, raw)
    return JSONResponse({"ok": True})


@router.get("/layouts/{layout_id}/speeds-exist")
async def layout_speeds_exist(
    layout_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """このレイアウトのレースに、計算済みの速度データがあるか（上書き確認用）。"""
    async with db.execute(
        "SELECT 1 FROM timing_race_speeds s "
        "JOIN timing_races r ON r.id = s.race_id "
        "WHERE r.layout_id = ? LIMIT 1",
        (layout_id,),
    ) as cur:
        exists = await cur.fetchone() is not None
    return JSONResponse({"exists": exists})


@router.post("/layouts/{layout_id}/recalc-speeds")
async def layout_recalc_speeds(
    layout_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """このコースで走った全レースの Av.・速度を現在の設定で計算し直す。

    距離を後から入れた／変えたときの反映に使う。既存値は上書きされる。
    """
    from app.application import timing_race_speed_store as speed_store
    res = await speed_store.recalc_layout(db, layout_id)
    return JSONResponse({"ok": True, **res})


@router.post("/layouts/{layout_id}/delete")
async def layout_delete(
    layout_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """単一コース固定（方針A）のため、削除は行わない。編集画面へ戻す。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/timing/layouts", status_code=303)
