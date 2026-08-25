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


# ---- タイム表記の相互変換（ミリ秒＝3桁。他予選のFINISH表示 %.3f と同じ桁）----
def ms_to_str(ms) -> str:
    """ミリ秒(int) → '10.115' の文字列（秒・小数3桁）。None は '-'。"""
    if ms is None:
        return "-"
    return f"{ms / 1000:.3f}"


def parse_time_to_ms(raw: str):
    """'10.115' / '10' / '10.1' → ミリ秒(int)。不正なら None。"""
    if raw is None:
        return None
    s = str(raw).strip().replace("：", ":").replace("’", ".").replace("''", ".")
    if not s:
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    if val < 0:
        return None
    ms = int(round(val * 1000))
    # 上限ガード（約59分59.999秒相当）
    if ms > 3599999:
        return None
    return ms


# ---- 集計・順位 -----------------------------------------------------------
async def _ta_load(tid: int, db):
    """エントリー一覧と各エントリーの走行記録をまとめて返す。

    returns: (entries[list[dict]], runs_map{entry_id: {run_no: {time_ms,is_co}}})
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
        "SELECT entry_id, run_no, time_ms, is_co FROM time_attack_runs WHERE tournament_id=?",
        (tid,),
    ) as cur:
        for row in await cur.fetchall():
            runs_map.setdefault(row["entry_id"], {})[row["run_no"]] = {
                "time_ms": row["time_ms"],
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
        finished = sum(1 for v in runs.values() if not v["is_co"] and v["time_ms"] is not None)
        times = [v["time_ms"] for v in runs.values() if not v["is_co"] and v["time_ms"] is not None]
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
            "best_ms": best,
            "best_str": ms_to_str(best),
        })

    def sort_key(r):
        has_time = r["best_ms"] is not None
        return (
            0 if has_time else 1,
            r["best_ms"] if has_time else 0,
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

    # ── M4LAPS 詳細指標（他予選と同じ集計エンジンを共有）──
    #    反映(M4LAPS)で time_attack_runs.race_id に紐づいた計測レースを走査し、
    #    レーサー別ベスト（TOTAL/LAP/セクター/速度）と RECORD HOLDERS を出す。
    from app.core.config import IS_CLOUD
    racer_bests = {}
    _m4_scan = None
    if IS_CLOUD and getattr(request.state, "m4laps_licensed", False) and t.get("use_m4laps", 1) != 0:
        try:
            from app.application import timing_racer_best_service as rbest_svc
            _m4_scan = await rbest_svc.scan_tournament_metrics(db, tid)
            racer_bests = _m4_scan["bests"] if _m4_scan else {}
        except Exception:
            racer_bests = {}
    max_sector_no = 0
    for _bm in racer_bests.values():
        for _i in range(1, 8):
            if _bm.get(f"sector{_i}") is not None or _bm.get(f"sector_ms{_i}") is not None:
                max_sector_no = max(max_sector_no, _i)
    best_total_min = None
    for _bm in racer_bests.values():
        _tv = _bm.get("total")
        if _tv is not None and (best_total_min is None or _tv < best_total_min):
            best_total_min = _tv
    _LOWER = {"total", "lap"}
    for _i in range(1, 8):
        _LOWER.add(f"sector{_i}")
    def _rank_map(metric, lower):
        vals = [(eid, bm[metric]) for eid, bm in racer_bests.items() if bm.get(metric) is not None]
        vals.sort(key=lambda x: x[1], reverse=not lower)
        out = {}; pos = 0; prev = None
        for i, (eid, v) in enumerate(vals, start=1):
            if prev is None or v != prev:
                pos = i; prev = v
            if pos > 3:
                break
            out[eid] = pos
        return out
    best_ranks = {}
    for _m in ["total", "total_avg", "max_ms", "lap", "lap_avg"]:
        best_ranks[_m] = _rank_map(_m, _m in _LOWER)
    for _i in range(1, 8):
        best_ranks[f"sector{_i}"] = _rank_map(f"sector{_i}", True)
        best_ranks[f"sector_ms{_i}"] = _rank_map(f"sector_ms{_i}", False)

    # RECORD HOLDERS / 称号（POINT LEADER はポイント制専用なので出さない）
    record_holders = None
    achievements = None
    ach_labels = {}
    if _m4_scan is not None:
        try:
            from app.application import qualifying_records as qrec
            _name_by_entry = {s["entry_id"]: s["name"] for s in standings}
            for _e in entries:
                _name_by_entry.setdefault(_e["entry_id"], _e["name"])
            _rh_raw = _m4_scan["records"]
            record_holders = qrec.format_records_display(_rh_raw, _name_by_entry, {})
            try:
                _sweep = await qrec.sweep_entries_for_tournament(db, tid)
            except Exception:
                _sweep = set()
            achievements = qrec.compute_achievements(_rh_raw, None, _sweep, _name_by_entry)
            _ach_cfg = await qrec.get_ach_config(db)
            record_holders, _pl, achievements = qrec.apply_panel_config(
                record_holders, None, achievements, _ach_cfg)
            ach_labels = qrec.labels_from_cfg(_ach_cfg)
        except Exception:
            record_holders = None
            achievements = None
            ach_labels = {}

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
                              "time_str": ms_to_str(cell["time_ms"])})
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
        "racer_bests": racer_bests,
        "best_ranks": best_ranks,
        "best_total_min": best_total_min,
        "max_sector_no": max_sector_no,
        "record_holders": record_holders,
        "achievements": achievements,
        "ach_labels": ach_labels,
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
        time_ms = None
    else:
        time_ms = parse_time_to_ms(data.get("time"))
        if time_ms is None:
            return JSONResponse({"ok": False, "error": "time"})

    async with transaction(db):
        await db.execute(
            """INSERT INTO time_attack_runs (tournament_id, entry_id, run_no, time_ms, is_co, race_id)
               VALUES (?,?,?,?,?,NULL)
               ON CONFLICT(tournament_id, entry_id, run_no)
               DO UPDATE SET time_ms=excluded.time_ms, is_co=excluded.is_co, race_id=NULL""",
            (tid, entry_id, run_no, time_ms, 1 if is_co else 0),
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
        "time_str": ms_to_str(time_ms) if not is_co else "CO",
        "standings": _ta_standings(entries, runs_map),
    })


@router.post("/{tid}/qualifying/time-attack/apply")
async def time_attack_apply_m4(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """M4LAPS（ラップタイマー）の最新計測タイムを、指定セル（entry_id×run_no）へ取り込む。

    タイムアタックは1人ずつ走行するため「最新の1レース＝そのレーサーの走行」とみなし、
    その総合タイム（total_s）をミリ秒に変換して格納する。DNF(CO)なら CO 記録。
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
    # PIP（ラップタイマー）で選択中のレースがあれば race_id 指定、無ければ最新（None）。
    from app.presentation.routers.timing_api import _pick_race, _ranking_payload
    from app.infrastructure.db.repositories.timing_repository import TimingRaceRepository
    _rid = data.get("race_id")
    try:
        race_id = int(_rid) if _rid not in (None, "") else None
    except (TypeError, ValueError):
        race_id = None
    repo = TimingRaceRepository(db)
    race, result = await _pick_race(db, repo, race_id)
    if result is None:
        return JSONResponse({"ok": False, "error": "no_measurement"})
    ranking = _ranking_payload(result)
    if not ranking:
        return JSONResponse({"ok": False, "error": "no_measurement"})

    top = ranking[0]  # 1人走行想定：先頭＝そのレーサーの結果
    if top.get("dnf") or top.get("total_s") is None:
        time_ms = None
        is_co = 1
    else:
        time_ms = int(round(top["total_s"] * 1000))
        is_co = 0

    async with transaction(db):
        await db.execute(
            """INSERT INTO time_attack_runs (tournament_id, entry_id, run_no, time_ms, is_co, race_id)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(tournament_id, entry_id, run_no)
               DO UPDATE SET time_ms=excluded.time_ms, is_co=excluded.is_co, race_id=excluded.race_id""",
            (tid, entry_id, run_no, time_ms, is_co, race["id"] if race else None),
        )
        await db.execute(
            "UPDATE tournaments SET status='qualifying' WHERE id=? AND status='prepare'",
            (tid,),
        )
    _publish()
    entries, runs_map = await _ta_load(tid, db)
    return JSONResponse({
        "ok": True,
        "time_str": "CO" if is_co else ms_to_str(time_ms),
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
