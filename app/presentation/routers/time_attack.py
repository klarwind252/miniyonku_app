"""タイムアタック予選ルーター

参加者が1人ずつ走行してタイムを計測する予選形式。
- 走行順は任意（レーサー×走行回数のグリッドに直接入力）
- 各走行はタイム（センチ秒）または CO（完走せず）で記録
- 順位付けはベストタイム（最小）の昇順
- 「予選終了」で決勝進出人数ぶんに advanced=1 を付与

記録は time_attack_runs テーブルに保持（entry_id × run_no 一意）。
締め状態は tournaments.ta_status（NULL=進行中 / 'closed'=予選終了）。
"""
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import aiosqlite

from app.infrastructure.db.connection import get_db
from app.infrastructure.db.tx import transaction
from app.routers.tournaments import calc_finalists, QUALIFYING_LABELS
from app.presentation.templates import templates

router = APIRouter()


# ---- タイム表記の相互変換 -------------------------------------------------
def cs_to_str(cs) -> str:
    """センチ秒(int) → '10.11' の文字列。None は '-'。"""
    if cs is None:
        return "-"
    return f"{cs // 100}.{cs % 100:02d}"


def parse_time_to_cs(raw: str):
    """'10.11' / '10' / '10.1' → センチ秒(int)。不正なら None。"""
    if raw is None:
        return None
    s = str(raw).strip().replace("：", ":").replace("’", ".").replace("''", ".")
    if not s:
        return None
    # 秒（小数2桁まで）を想定
    try:
        val = float(s)
    except ValueError:
        return None
    if val < 0:
        return None
    cs = int(round(val * 100))
    # 上限ガード（99分59.99秒相当）
    if cs > 599999:
        return None
    return cs


# ---- 集計・順位 -----------------------------------------------------------
async def _ta_load(tid: int, db):
    """エントリー一覧と各エントリーの走行記録をまとめて返す。

    returns: (entries[list[dict]], runs_map{entry_id: {run_no: {time_cs,is_co}}})
    """
    async with db.execute(
        """SELECT e.id AS entry_id, e.entry_order, e.advanced, r.name,
                  COALESCE(r.yomi,'') AS yomi
           FROM entries e JOIN racers r ON r.id=e.racer_id
           WHERE e.tournament_id=? AND e.status='active'
           ORDER BY e.entry_order""",
        (tid,),
    ) as cur:
        entries = [dict(row) for row in await cur.fetchall()]

    runs_map: dict[int, dict[int, dict]] = {}
    async with db.execute(
        "SELECT entry_id, run_no, time_cs, is_co FROM time_attack_runs WHERE tournament_id=?",
        (tid,),
    ) as cur:
        for row in await cur.fetchall():
            runs_map.setdefault(row["entry_id"], {})[row["run_no"]] = {
                "time_cs": row["time_cs"],
                "is_co": row["is_co"],
            }
    return entries, runs_map


def _ta_standings(entries, runs_map):
    """予選順位表を計算して返す（rank 昇順のリスト）。

    ランキング規則（既定）:
      1) 完走（タイムあり）がある者を上位、無い者を下位に置く
      2) 完走ありグループ：ベストタイム昇順 → 完走数の多い順 → 出走数の多い順 → エントリー順
      3) 完走なしグループ：出走数の多い順 → エントリー順
    """
    rows = []
    for e in entries:
        runs = runs_map.get(e["entry_id"], {})
        started = len(runs)
        finished = sum(1 for v in runs.values() if not v["is_co"] and v["time_cs"] is not None)
        times = [v["time_cs"] for v in runs.values() if not v["is_co"] and v["time_cs"] is not None]
        best = min(times) if times else None
        rate = round(finished / started * 100) if started else 0
        rows.append({
            "entry_id": e["entry_id"],
            "name": e["name"],
            "entry_order": e["entry_order"],
            "advanced": e.get("advanced"),
            "started": started,
            "finished": finished,
            "finish_rate": rate,
            "best_cs": best,
            "best_str": cs_to_str(best),
        })

    def sort_key(r):
        has_time = r["best_cs"] is not None
        return (
            0 if has_time else 1,
            r["best_cs"] if has_time else 0,
            -r["finished"],
            -r["started"],
            r["entry_order"],
        )

    rows.sort(key=sort_key)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def _publish():
    try:
        from app.services.publish_scheduler import schedule_publish
        schedule_publish()
    except Exception:
        pass


# ---- 画面 -----------------------------------------------------------------
@router.get("/{tid}/qualifying/time-attack", response_class=HTMLResponse)
async def time_attack_screen(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url="/admin/tournaments/")
    t = dict(t)
    if t.get("qualifying_type") != "time_attack":
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)

    from app.routers.tournaments import _is_result_finalized
    is_finalized = await _is_result_finalized(tid, db)

    # M4LAPS（自動計測）が使えるか：クラウド版＋ライセンス＋このレースが使用設定
    from app.core.config import IS_CLOUD
    m4_on = bool(IS_CLOUD and getattr(request.state, "m4laps_licensed", False)
                 and (t.get("use_m4laps", 1) != 0))

    entries, runs_map = await _ta_load(tid, db)
    runs = int(t.get("qual_ta_runs") or 3)
    finalist_n = calc_finalists("time_attack", t) or 0
    standings = _ta_standings(entries, runs_map)

    # グリッド用にセル情報を組み立て（各エントリー × 1..runs）
    grid = []
    for e in entries:
        emap = runs_map.get(e["entry_id"], {})
        cells = []
        for rn in range(1, runs + 1):
            cell = emap.get(rn)
            if cell is None:
                cells.append({"run_no": rn, "state": "empty"})
            elif cell["is_co"]:
                cells.append({"run_no": rn, "state": "co"})
            else:
                cells.append({"run_no": rn, "state": "time",
                              "time_str": cs_to_str(cell["time_cs"])})
        grid.append({"entry_id": e["entry_id"], "name": e["name"], "cells": cells})

    return templates.TemplateResponse("admin/time_attack.html", {
        "request": request,
        "t": t,
        "runs": runs,
        "finalist_n": finalist_n,
        "grid": grid,
        "standings": standings,
        "is_finalized": is_finalized,
        "m4_on": m4_on,
        "any_adv": any(s.get("advanced") not in (None,) for s in standings),
        "qualifying_labels": QUALIFYING_LABELS,
    })


# ---- 記録（JSON） ---------------------------------------------------------
@router.post("/{tid}/qualifying/time-attack/run")
async def time_attack_record(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    from app.routers.tournaments import _is_result_finalized
    if await _is_result_finalized(tid, db):
        return JSONResponse({"ok": False, "error": "finalized"})

    async with db.execute(
        "SELECT qualifying_type, qual_ta_runs FROM tournaments WHERE id=?", (tid,)
    ) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "time_attack":
        return JSONResponse({"ok": False, "error": "type"})
    max_runs = int(dict(t).get("qual_ta_runs") or 3)

    data = await request.json()
    try:
        entry_id = int(data.get("entry_id"))
        run_no = int(data.get("run_no"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "param"})
    if run_no < 1 or run_no > max_runs:
        return JSONResponse({"ok": False, "error": "run_no"})

    # エントリー存在チェック
    async with db.execute(
        "SELECT 1 FROM entries WHERE id=? AND tournament_id=? AND status='active'",
        (entry_id, tid),
    ) as cur:
        if not await cur.fetchone():
            return JSONResponse({"ok": False, "error": "entry"})

    is_co = bool(data.get("co"))
    if is_co:
        time_cs = None
    else:
        time_cs = parse_time_to_cs(data.get("time"))
        if time_cs is None:
            return JSONResponse({"ok": False, "error": "time"})

    async with transaction(db):
        await db.execute(
            """INSERT INTO time_attack_runs (tournament_id, entry_id, run_no, time_cs, is_co)
               VALUES (?,?,?,?,?)
               ON CONFLICT(tournament_id, entry_id, run_no)
               DO UPDATE SET time_cs=excluded.time_cs, is_co=excluded.is_co""",
            (tid, entry_id, run_no, time_cs, 1 if is_co else 0),
        )
        # 記録が入ったら予選中に遷移（締め済みでない限り）
        await db.execute(
            "UPDATE tournaments SET status='qualifying' WHERE id=? AND status='prepare'",
            (tid,),
        )
    _publish()
    entries, runs_map = await _ta_load(tid, db)
    return JSONResponse({
        "ok": True,
        "time_str": cs_to_str(time_cs) if not is_co else "CO",
        "standings": _ta_standings(entries, runs_map),
    })


@router.post("/{tid}/qualifying/time-attack/apply")
async def time_attack_apply_m4(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """M4LAPS（ラップタイマー）の最新計測タイムを、指定セル（entry_id×run_no）へ取り込む。

    タイムアタックは1人ずつ走行するため「最新の1レース＝そのレーサーの走行」とみなし、
    その総合タイム（total_s）をセンチ秒に変換して格納する。DNF(CO)なら CO 記録。
    """
    # クラウド版＋ライセンス必須（未ライセンス/オンプレは 404）
    from app.core.config import IS_CLOUD
    from app.domain import m4laps_license
    if not (IS_CLOUD and await m4laps_license.is_licensed(db)):
        return JSONResponse({"ok": False, "error": "m4laps_unavailable"}, status_code=404)

    from app.routers.tournaments import _is_result_finalized
    if await _is_result_finalized(tid, db):
        return JSONResponse({"ok": False, "error": "finalized"})

    async with db.execute(
        "SELECT qualifying_type, qual_ta_runs FROM tournaments WHERE id=?", (tid,)
    ) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "time_attack":
        return JSONResponse({"ok": False, "error": "type"})
    max_runs = int(dict(t).get("qual_ta_runs") or 3)

    data = await request.json()
    try:
        entry_id = int(data.get("entry_id"))
        run_no = int(data.get("run_no"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "param"})
    if run_no < 1 or run_no > max_runs:
        return JSONResponse({"ok": False, "error": "run_no"})

    async with db.execute(
        "SELECT 1 FROM entries WHERE id=? AND tournament_id=? AND status='active'",
        (entry_id, tid),
    ) as cur:
        if not await cur.fetchone():
            return JSONResponse({"ok": False, "error": "entry"})

    # 最新の計測レースを取得（timing_api のヘルパを再利用）
    from app.presentation.routers.timing_api import _pick_race, _ranking_payload
    from app.infrastructure.db.repositories.timing_repository import TimingRaceRepository
    repo = TimingRaceRepository(db)
    race, result = await _pick_race(db, repo, None)
    if result is None:
        return JSONResponse({"ok": False, "error": "no_measurement"})
    ranking = _ranking_payload(result)
    if not ranking:
        return JSONResponse({"ok": False, "error": "no_measurement"})

    top = ranking[0]  # 1人走行想定：先頭＝そのレーサーの結果
    if top.get("dnf") or top.get("total_s") is None:
        time_cs = None
        is_co = 1
    else:
        time_cs = int(round(top["total_s"] * 100))
        is_co = 0

    async with transaction(db):
        await db.execute(
            """INSERT INTO time_attack_runs (tournament_id, entry_id, run_no, time_cs, is_co)
               VALUES (?,?,?,?,?)
               ON CONFLICT(tournament_id, entry_id, run_no)
               DO UPDATE SET time_cs=excluded.time_cs, is_co=excluded.is_co""",
            (tid, entry_id, run_no, time_cs, is_co),
        )
        await db.execute(
            "UPDATE tournaments SET status='qualifying' WHERE id=? AND status='prepare'",
            (tid,),
        )
    _publish()
    entries, runs_map = await _ta_load(tid, db)
    return JSONResponse({
        "ok": True,
        "time_str": "CO" if is_co else cs_to_str(time_cs),
        "standings": _ta_standings(entries, runs_map),
    })


@router.post("/{tid}/qualifying/time-attack/run/cancel")
async def time_attack_cancel(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    from app.routers.tournaments import _is_result_finalized
    if await _is_result_finalized(tid, db):
        return JSONResponse({"ok": False, "error": "finalized"})

    data = await request.json()
    try:
        entry_id = int(data.get("entry_id"))
        run_no = int(data.get("run_no"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "param"})

    await db.execute(
        "DELETE FROM time_attack_runs WHERE tournament_id=? AND entry_id=? AND run_no=?",
        (tid, entry_id, run_no),
    )
    await db.commit()
    _publish()
    entries, runs_map = await _ta_load(tid, db)
    return JSONResponse({"ok": True, "standings": _ta_standings(entries, runs_map)})


@router.get("/{tid}/qualifying/time-attack/standings-json")
async def time_attack_standings_json(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    entries, runs_map = await _ta_load(tid, db)
    return JSONResponse({"standings": _ta_standings(entries, runs_map)})
