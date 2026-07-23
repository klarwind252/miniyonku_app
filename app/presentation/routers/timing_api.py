"""タイミング計測のGW受信口と結果表示。

- POST /api/timing/races          レース開始（ヒートID・レイアウト・周回・緑時刻）
- POST /api/timing/races/{id}/events  GWからの通過イベントバッチ（冪等・D11/D12）
- GET  /admin/timing/results      レース一覧（結果閲覧）
- GET  /admin/timing/results/{id} 1レースの結果（ラップ・セクター・順位）

GWからのPOSTは、環境変数 TIMING_TOKEN があれば X-Timing-Token を要求する
（未設定ならローカル運用として素通し・README_timing 方針）。
"""

import os

from fastapi import APIRouter, Request, Depends, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse
import aiosqlite

from app.infrastructure.db.connection import get_db
from app.infrastructure.db.repositories.timing_repository import (
    TimingRaceRepository,
    TimingLayoutRepository,
)
from app.application.timing_race_service import build_race_result
from app.presentation.templates import templates
from app.presentation.routers.m4laps_guard import require_m4laps

router = APIRouter()

TIMING_TOKEN = os.environ.get("TIMING_TOKEN", "")


def _check_token(x_timing_token: str | None):
    """TIMING_TOKEN が設定されていれば照合。未設定なら素通し。"""
    if TIMING_TOKEN and x_timing_token != TIMING_TOKEN:
        raise HTTPException(status_code=401, detail="invalid timing token")


# ---------------------------------------------------------------------------
# GW受信口（API）
# ---------------------------------------------------------------------------

@router.post("/api/timing/races")
async def create_race(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    x_timing_token: str | None = Header(default=None),
):
    """レースを開始し race_id を払い出す。

    body(JSON): {"heat_tag":int?, "layout_id":int?, "target_laps":int,
                 "green_t_us":int?}
    """
    _check_token(x_timing_token)
    data = await request.json()
    repo = TimingRaceRepository(db)
    race_id = await repo.create_race(
        heat_tag=data.get("heat_tag"),
        layout_id=data.get("layout_id"),
        target_laps=int(data.get("target_laps") or 3),
        green_t_us=data.get("green_t_us"),
    )
    return JSONResponse({"race_id": race_id})


@router.post("/api/timing/races/{race_id}/events")
async def post_events(
    race_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    x_timing_token: str | None = Header(default=None),
):
    """通過イベントのバッチ受信（冪等・D11/D12）。

    body(JSON): {"events":[{device_id,src,src_boot_id,seq,lane,t_us,t_us_b?,quality?}, ...]}
    戻り値: {"inserted":n, "duplicate":m}
    """
    _check_token(x_timing_token)
    data = await request.json()
    events = data.get("events", [])
    repo = TimingRaceRepository(db)

    race = await repo.get_race(race_id)
    if race is None:
        raise HTTPException(status_code=404, detail="race not found")

    inserted = 0
    duplicate = 0
    for ev in events:
        try:
            is_new = await repo.insert_event(race_id, ev)
        except (KeyError, ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"bad event: {e}")
        if is_new:
            inserted += 1
        else:
            duplicate += 1

    return JSONResponse({"inserted": inserted, "duplicate": duplicate})


# ---------------------------------------------------------------------------
# 結果表示（admin画面）
# ---------------------------------------------------------------------------

@router.get("/admin/timing/results", response_class=HTMLResponse)
async def results_page(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """計測結果の一覧（レーン単位の明細・セクター/速度つき）。

    1行 = 1レーン。POS/LANE/BEST/GAP に続けて S1..Sn のセクタータイムと速度を出す。
    速度はビーム間隔の設定が未実装のため、現状はダミー値（_dummy_speed_kmh 参照）。
    """
    repo = TimingRaceRepository(db)
    races = await repo.list_races(limit=50)

    rows = []          # 表示する明細行（1行=1レーン）
    max_sectors = 0    # 表のセクター列数（レイアウトにより可変）

    for r in races:
        rid = r["id"]
        try:
            race, result = await build_race_result(db, rid)
        except Exception:
            continue
        if race is None or result is None:
            continue

        ordered = result.ranking()          # 合計タイム昇順
        top_us = None
        for m in ordered:
            if m.total_time_us is not None:
                top_us = m.total_time_us
                break

        for pos, m in enumerate(ordered, start=1):
            # セクター：各周の同じ区間のうち最速を代表値にする
            # （1周だけ見ると遅い周に引っ張られるため。F1のセクター表示と同じ考え方）
            best_by_sector: dict[int, int] = {}
            for lap in m.laps:
                for idx, s in enumerate(lap.sectors):
                    cur = best_by_sector.get(idx)
                    if cur is None or s.dt_us < cur:
                        best_by_sector[idx] = s.dt_us

            sectors = []
            for idx in sorted(best_by_sector):
                sectors.append({
                    "no": idx + 1,
                    "s": round(best_by_sector[idx] / 1e6, 3),
                    "kmh": _dummy_speed_kmh(rid, m.start_lane, idx),
                })
            max_sectors = max(max_sectors, len(sectors))

            # MAX SPEED：S/G・各セクションゲートを通過したときの速度のうち最速
            max_kmh = max((s["kmh"] for s in sectors), default=None)

            gap = None
            if m.total_time_us is not None and top_us is not None:
                gap = round((m.total_time_us - top_us) / 1e6, 3)

            rows.append({
                "race_id": rid,
                "created_at": race["created_at"],
                "heat_id": race["heat_id"],
                "mode": result.mode,               # 'f1'=レース / 'run'=フリー
                "pos": pos,
                "start_lane": m.start_lane,
                "total_s": round(m.total_time_us / 1e6, 3) if m.total_time_us else None,
                "max_kmh": max_kmh,
                "gap": gap,
                "sectors": sectors,
                "is_first_of_race": pos == 1,      # 同一レースの先頭行だけ時刻を出す
                "race_row_count": len(ordered),
            })

    return templates.TemplateResponse(
        "admin/timing_results.html",
        {
            "request": request,
            "rows": rows,
            "sector_nos": list(range(1, max_sectors + 1)),
            "races": races,
        },
    )


def _dummy_speed_kmh(race_id: int, lane: int, sector_idx: int) -> float:
    """⚠ 仮の速度値（実機のビーム間隔設定が未実装のためのダミー）。

    本実装時はここを削除し、PassEvent の t_us / t_us_b の差と
    ゲートのビーム間隔(mm)から算出する:
        v[m/s]  = 間隔mm / 1000 / ((t_us_b - t_us) / 1e6)
        v[km/h] = v[m/s] * 3.6
    表示のたびに値が変わると見づらいので、入力から決まる再現可能な擬似値にしている。
    """
    seed = (race_id * 31 + lane * 7 + sector_idx * 13) % 100
    return round(24.0 + seed * 0.11, 1)   # おおよそ 24.0〜35.0 km/h


@router.get("/api/timing/pip/latest")
async def pip_latest(
    limit: int = 5,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """PIP（右下小窓）用：最近の計測レースを新しい順に、順位つきで返す。

    ⚠ クラウド版かつライセンス登録済みの環境でのみ利用可（require_m4laps）。
       オンプレ版・未登録環境では 404 を返し、機能自体を隠す。

    GWから送られてきた記録をそのまま見せるだけ。まだ誰のものかは紐づけない。
    （組み合わせ情報はGWへ送らない方針のため、突き合わせはアプリ側で後から行う）
    """
    repo = TimingRaceRepository(db)
    races = await repo.list_races(limit=max(1, min(limit, 20)))
    out = []
    for r in races:
        rid = r["id"]
        try:
            race, result = await build_race_result(db, rid)
        except Exception:
            continue
        if race is None:
            continue
        rows = []
        if result is not None:
            for pos, m in enumerate(result.ranking(), start=1):
                rows.append({
                    "pos": pos,
                    "start_lane": m.start_lane,
                    "total_s": round(m.total_time_us / 1e6, 3) if m.total_time_us else None,
                    "best_s": round(m.best_lap_us / 1e6, 3) if m.best_lap_us else None,
                    "completed_laps": m.completed_laps,
                })
        keys = race.keys() if hasattr(race, "keys") else []
        out.append({
            "race_id": rid,
            "heat_id": (race["heat_id"] if "heat_id" in keys else None),
            "target_laps": (race["target_laps"] if "target_laps" in keys else None),
            "created_at": (race["created_at"] if "created_at" in keys else None),
            "ranking": rows,
        })
    return JSONResponse({"races": out})


@router.get("/admin/timing/results/{race_id}", response_class=HTMLResponse)
async def result_detail_page(
    race_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    race, result = await build_race_result(db, race_id)
    if race is None:
        raise HTTPException(status_code=404, detail="race not found")

    # 表示用に整形（µs→秒）
    ranking = []
    machines = []
    if result is not None:
        for pos, m in enumerate(result.ranking(), start=1):
            ranking.append({
                "pos": pos,
                "start_lane": m.start_lane,
                "total_s": (m.total_time_us / 1e6) if m.total_time_us else None,
                "best_s": (m.best_lap_us / 1e6) if m.best_lap_us else None,
            })
        for sl in sorted(result.machines):
            m = result.machines[sl]
            laps = []
            for lap in m.laps:
                laps.append({
                    "lap": lap.lap,
                    "lap_s": lap.lap_time_us / 1e6,
                    "sectors": [
                        {"from": s.from_gate_index, "to": s.to_gate_index,
                         "s": s.dt_us / 1e6}
                        for s in lap.sectors
                    ],
                })
            machines.append({
                "start_lane": m.start_lane,
                "completed_laps": m.completed_laps,
                "total_s": (m.total_time_us / 1e6) if m.total_time_us else None,
                "laps": laps,
            })

    return templates.TemplateResponse(
        "admin/timing_result_detail.html",
        {
            "request": request,
            "race": race,
            "mode": result.mode if result else None,
            "ranking": ranking,
            "machines": machines,
            "has_result": result is not None,
        },
    )
