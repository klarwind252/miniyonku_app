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
from fastapi import Response, Form
import subprocess, sys, tempfile, os, csv
from app.core.store_context import get_current_store

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
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
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
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
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


@router.get("/races/{race_id}/speeds-exist")
async def race_speeds_exist(
    race_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """この計測結果に、計算済みの速度データがあるか（上書き確認用）。"""
    from app.application import timing_race_speed_store as speed_store
    exists = await speed_store.has_stored_speeds(db, race_id)
    return JSONResponse({"exists": exists})


@router.post("/races/{race_id}/recalc-speeds")
async def race_recalc_speeds(
    race_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """この計測結果だけ Av.・速度を現在の設定で計算し直す（既存値は上書き）。"""
    from app.application import timing_race_speed_store as speed_store
    rows = await speed_store.compute_and_store_speeds(db, race_id)
    await db.commit()
    return JSONResponse({"ok": True, "rows": rows})


@router.post("/layouts/{layout_id}/delete")
async def layout_delete(
    layout_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """単一コース固定（方針A）のため、削除は行わない。編集画面へ戻す。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/timing/layouts", status_code=303)


# ===========================================================================
#  ⚙ 機材設定（GW WiFi/接続情報）— アプリで編集し、USB(Web Serial)で焼く
#  ・設定は NVS ネームスペース "m4cfg" の string キー ssid/pass/host/ip/token。
#  ・サーバーは値から nvs.bin(0x5000) を生成して返すだけ。焼くのはブラウザ(esptool-js)。
#  ・「店舗オリジナル設定」は per-store DB の timing_gw_profile に保存（DBが店舗別なので
#    店舗ごとに自然に分離される。store_id 列は不要）。
# ===========================================================================

# 焼く直前のA/Bセット判定に使うMAC台帳（機体台帳_20260730基準）。
# ⚠ 20260805でSE1/GW6/SG10のA/Bに訂正あり。実機のテプラを正として、ここは要照合。
AB_MAC_LEDGER = {
    "GW6":  {"A": "8c:94:df:9c:77:b0", "B": "8c:94:df:9c:78:50"},
    "GW7":  {"A": None, "B": None},
    "RC8":  {"A": "e8:f6:0a:16:c0:94", "B": "e8:f6:0a:14:d8:68"},
    "SG10": {"A": "e8:f6:0a:16:c2:74", "B": "e8:f6:0a:16:d3:08"},
    "SE0":  {"A": "8c:94:df:52:c5:f4", "B": "8c:94:df:54:15:34"},
    "SE1":  {"A": "8c:94:df:52:b2:f0", "B": "8c:94:df:54:1f:30"},
    "SE2":  {"A": "8c:94:df:52:c4:e0", "B": "8c:94:df:52:b3:08"},
}

# WiFi/サーバー設定を実際に読むのはGWのみ（SE/SG/RCはESP-NOW専用でNVS未読み）。
# よって設定対象はGWに限定。A/B判定(AB_MAC_LEDGER)は全機材ぶん保持する。
SETTINGS_TARGETS = ["GW6", "GW7"]

NVS_NAMESPACE = "m4cfg"
NVS_SIZE = "0x5000"   # partitions_8mb.csv の nvs パーティションサイズ


async def _ensure_profile_table(db: aiosqlite.Connection) -> None:
    """設定プロファイル表（店舗別DBに作る＝店舗ごとに分離）。target を主キーに1機材1件。冪等。

    旧スキーマ（id/name付き・複数可）が残っていれば作り直す。設定は使い捨て前提のため実害なし。
    """
    async with db.execute("PRAGMA table_info(timing_gw_profile)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if cols and ("name" in cols or "id" in cols):
        await db.execute("DROP TABLE IF EXISTS timing_gw_profile")
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS timing_gw_profile (
            target     TEXT PRIMARY KEY,
            ssid       TEXT NOT NULL DEFAULT '',
            wifi_pass  TEXT NOT NULL DEFAULT '',
            host       TEXT NOT NULL DEFAULT '',
            ip         TEXT NOT NULL DEFAULT '',
            token      TEXT NOT NULL DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
        """
    )
    await db.commit()


async def _list_profiles(db: aiosqlite.Connection):
    await _ensure_profile_table(db)
    async with db.execute(
        "SELECT target,ssid,wifi_pass,host,ip,token,updated_at "
        "FROM timing_gw_profile ORDER BY target"
    ) as cur:
        rows = await cur.fetchall()
    keys = ["target","ssid","wifi_pass","host","ip","token","updated_at"]
    return [dict(zip(keys, r)) for r in rows]


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    profiles = await _list_profiles(db)
    store = get_current_store()
    store_name = store.name if store else "（オンプレ/既定）"
    return templates.TemplateResponse(
        "admin/timing_settings.html",
        {
            "request": request,
            "profiles": profiles,
            "store_name": store_name,
            "targets": SETTINGS_TARGETS,
        },
    )


@router.get("/settings/ab-ledger")
async def settings_ab_ledger():
    """MAC→A/B判定用の台帳（ブラウザのフラッシャが読む）。"""
    return JSONResponse({"ledger": AB_MAC_LEDGER})


@router.get("/settings/profiles")
async def settings_profiles_list(db: aiosqlite.Connection = Depends(get_db)):
    return JSONResponse({"profiles": await _list_profiles(db)})


@router.post("/settings/profiles")
async def settings_profiles_save(
    request: Request, db: aiosqlite.Connection = Depends(get_db)
):
    """対象機材ごとに1件だけ保存（存在すれば上書き）。"""
    await _ensure_profile_table(db)
    form = await request.form()
    def g(k): return (form.get(k) or "").strip()
    target = g("target")
    if target not in SETTINGS_TARGETS:
        raise HTTPException(status_code=400, detail="対象機材が不正です")
    await db.execute(
        "INSERT INTO timing_gw_profile (target,ssid,wifi_pass,host,ip,token) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(target) DO UPDATE SET "
        "ssid=excluded.ssid,wifi_pass=excluded.wifi_pass,host=excluded.host,"
        "ip=excluded.ip,token=excluded.token,updated_at=datetime('now','localtime')",
        (target, g("ssid"), (form.get("wifi_pass") or ""),  # passは trim しない
         g("host"), g("ip"), (form.get("token") or "")),
    )
    await db.commit()
    return JSONResponse({"ok": True, "target": target})


@router.post("/settings/profiles/{target}/delete")
async def settings_profiles_delete(
    target: str, db: aiosqlite.Connection = Depends(get_db)
):
    await _ensure_profile_table(db)
    await db.execute("DELETE FROM timing_gw_profile WHERE target=?", (target,))
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/settings/nvs.bin")
async def settings_generate_nvs(request: Request):
    """フォーム値から NVS(0x5000) バイナリを生成して返す。焼くのはブラウザ側。

    受け取り：JSON {ssid,pass,host,ip,token}
    NVSは namespace "m4cfg"・string キー。空のキーは書かない（ファーム側で既定に落ちる）。
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    ssid  = str(data.get("ssid")  or "")
    passwd= str(data.get("pass")  or "")
    host  = str(data.get("host")  or "")
    ip    = str(data.get("ip")    or "")
    token = str(data.get("token") or "")
    if not ssid:
        raise HTTPException(status_code=400, detail="SSIDは必須です")

    # NVS生成用CSV（csvモジュールで正しくクオート＝パスワードにカンマ等が入っても安全）。
    rows = [["key","type","encoding","value"], [NVS_NAMESPACE,"namespace","",""]]
    for k, v in (("ssid",ssid),("pass",passwd),("host",host),("ip",ip),("token",token)):
        if v != "":                       # 空は書かない → ファーム側で secrets.h 既定へ
            rows.append([k,"data","string",v])

    with tempfile.TemporaryDirectory() as d:
        csv_path = os.path.join(d, "m4cfg.csv")
        bin_path = os.path.join(d, "m4cfg.bin")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        try:
            subprocess.run(
                [sys.executable, "-m", "esp_idf_nvs_partition_gen",
                 "generate", csv_path, bin_path, NVS_SIZE],
                check=True, capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            raise HTTPException(status_code=500,
                detail="esp-idf-nvs-partition-gen 未インストール（pip install esp-idf-nvs-partition-gen）")
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500,
                detail="NVS生成に失敗: " + (e.stderr or e.stdout or str(e))[:400])
        blob = open(bin_path, "rb").read()

    return Response(
        content=blob,
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="m4cfg_nvs.bin"'},
    )
