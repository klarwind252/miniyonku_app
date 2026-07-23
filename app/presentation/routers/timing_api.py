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
    """計測結果の一覧（レーン×周回の明細）。

    1行 = 1レーンの1周。TS/CASE/POS/LANE/TOTAL はレーンごとに rowspan で結合し、
    LAP（周回番号・ラップタイム・平均速度）と SECTOR 0〜7 を周ごとに並べる。
    これにより「S1が何周目のものか」が明確になり、1周タイムも表示できる。

    速度は秒速(m/s)。ビーム間隔・コース全長の設定が未実装のため、現状はダミー値
    （_dummy_speed_ms / _dummy_lap_avg_ms 参照）。
    """
    repo = TimingRaceRepository(db)
    races = await repo.list_races(limit=50)

    rows = []          # 表示する明細行（1行=1レーンの1周）

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

        # このレースの総行数（＝各マシンの周回数の合計）。時刻セルの結合に使う。
        race_row_count = sum(max(1, len(m.laps)) for m in ordered)
        race_row_index = 0

        for pos, m in enumerate(ordered, start=1):
            gap = None
            if m.total_time_us is not None and top_us is not None:
                gap = round((m.total_time_us - top_us) / 1e6, 3)

            # MAX SPEED：S/G・各セクションゲートの通過速度のうち最速（全周を通じて）
            all_ms = []
            for lap in m.laps:
                for idx in range(len(lap.sectors)):
                    all_ms.append(_dummy_speed_ms(rid, m.start_lane, idx, lap.lap))
            max_ms = max(all_ms, default=None)

            lap_count = max(1, len(m.laps))

            for li, lap in enumerate(m.laps or [None]):
                # --- SECTOR 0〜7 ---
                # 0 = S/G通過（その周の起点）／1〜7 = 各区間
                sectors = [None] * 8
                if lap is not None:
                    # S/G通過：1周目は計測開始の瞬間なので速度なし
                    sectors[0] = {
                        "s": None,
                        "ms": (None if lap.lap == 1
                               else _dummy_speed_ms(rid, m.start_lane, 0, lap.lap)),
                        "sg": True,
                    }
                    for idx, sec in enumerate(lap.sectors):
                        if idx + 1 > 7:
                            break
                        sectors[idx + 1] = {
                            "s": round(sec.dt_us / 1e6, 3),
                            "ms": _dummy_speed_ms(rid, m.start_lane, idx, lap.lap),
                            "sg": False,
                        }

                rows.append({
                    "race_id": rid,
                    "created_at": race["created_at"],
                    "date_part": _split_ts(race["created_at"])[0],
                    "time_part": _split_ts(race["created_at"])[1],
                    "heat_id": race["heat_id"],
                    "mode": result.mode,               # 'f1'=レース / 'run'=フリー
                    "pos": pos,
                    "start_lane": m.start_lane,
                    "total_s": round(m.total_time_us / 1e6, 3) if m.total_time_us else None,
                    "max_ms": max_ms,
                    "gap": gap,
                    # LAP
                    "lap_no": lap.lap if lap is not None else None,
                    "lap_s": round(lap.lap_time_us / 1e6, 3) if lap is not None else None,
                    "lap_avg_ms": (_dummy_lap_avg_ms(rid, m.start_lane, lap.lap)
                                   if lap is not None else None),
                    "sectors": sectors,
                    # 結合制御
                    "is_first_of_lane": li == 0,       # レーンの先頭行（POS等を結合）
                    "lane_row_count": lap_count,
                    "is_first_of_race": race_row_index == 0,   # レースの先頭行（時刻を結合）
                    "race_row_count": race_row_count,
                })
                race_row_index += 1

    return templates.TemplateResponse(
        "admin/timing_results.html",
        {
            "request": request,
            "rows": rows,
            # セクションゲートは最大6基＝区間は最大7（S/G→SQ1…SQ6→S/G）。
            # レイアウトによらず常に S1〜S7 の枠を出し、無い区間は「—」を表示する。
            "sector_nos": list(range(1, 8)),
            "races": races,
        },
    )


def _split_ts(ts) -> tuple[str, str]:
    """受信時刻を「日付」と「時刻」に分ける（表示で2行に折り返すため）。

    DBの値は "2026-07-23 05:57:06" 形式。想定外の形でも落ちないよう、
    分割できなければ全体を日付側に入れて時刻は空にする。
    戻り値: ("2026/07/23", "05:57:06")
    """
    if not ts:
        return ("", "")
    s = str(ts)
    parts = s.split(" ", 1)
    date_part = parts[0].replace("-", "/")
    time_part = parts[1] if len(parts) > 1 else ""
    return (date_part, time_part)


def _dummy_speed_ms(race_id: int, lane: int, sector_idx: int, lap: int = 1) -> float:
    """⚠ 仮の通過速度（秒速 m/s）。実機のビーム間隔設定が未実装のためのダミー。

    本実装時はここを削除し、PassEvent の t_us / t_us_b の差と
    ゲートのビーム間隔(mm)から算出する:
        v[m/s] = 間隔mm / 1000 / ((t_us_b - t_us) / 1e6)
    表示のたびに値が変わると見づらいので、入力から決まる再現可能な擬似値にしている。
    ミニ四駆の実速度域（およそ 6.5〜9.5 m/s ＝ 23〜34 km/h）に合わせてある。
    """
    seed = (race_id * 31 + lane * 7 + sector_idx * 13 + lap * 17) % 100
    return round(6.5 + seed * 0.03, 2)   # おおよそ 6.50〜9.47 m/s


def _dummy_lap_avg_ms(race_id: int, lane: int, lap: int) -> float:
    """⚠ 仮のラップ平均速度（秒速 m/s）。コース全長の設定が未実装のためのダミー。

    本実装時はここを削除し、コース1周の距離から算出する:
        v[m/s] = コース全長m / ラップタイム秒
    （コース全長はレイアウト編集画面に「1周の距離(m)」を追加して保持する想定）
    通過速度より少し低め＝現実的な平均値になるようにしてある。
    """
    seed = (race_id * 23 + lane * 11 + lap * 29) % 100
    return round(5.8 + seed * 0.022, 2)  # おおよそ 5.80〜7.98 m/s


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
