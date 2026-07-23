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
from app.application import timing_best_service as best_svc
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
    _guard: bool = Depends(require_m4laps),
):
    """レースを開始し race_id を払い出す。

    ⚠ M4LAPSはクラウド版限定。オンプレ版・ライセンス未登録では 404（require_m4laps）。
       トークン(X-Timing-Token)と併用し、二重に保護する。

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
    _guard: bool = Depends(require_m4laps),
):
    """通過イベントのバッチ受信（冪等・D11/D12）。

    ⚠ M4LAPSはクラウド版限定。オンプレ版・ライセンス未登録では 404（require_m4laps）。

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

    # ベスト記録を更新（毎回の再計算をやめ、受信時に保持する）
    # 失敗しても受信自体は成功として扱う（記録の取りこぼしを防ぐため）
    bests = {}
    try:
        bests = await best_svc.update_for_race(
            db, race_id, build_race_result, _dummy_speed_ms, _dummy_lap_avg_ms
        )
    except Exception:
        pass

    return JSONResponse({"inserted": inserted, "duplicate": duplicate,
                         "bests_updated": bests})


# ---------------------------------------------------------------------------
# 結果表示（admin画面）
# ---------------------------------------------------------------------------

@router.get("/admin/timing/results", response_class=HTMLResponse)
async def results_page(
    request: Request,
    date: str | None = None,
    limit: int = 10,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """計測結果の一覧（レーン×周回の明細）。

    既定は最新10レース。date=YYYY-MM-DD を指定するとその日の全レースを表示する。
    件数を絞らないと日が経つほど重くなるため、既定で制限をかけている。

    1行 = 1レーンの1周。TS/CASE/POS/LANE/TOTAL はレーンごとに rowspan で結合し、
    LAP（周回番号・ラップタイム・平均速度）と SECTOR 0〜7 を周ごとに並べる。

    速度は秒速(m/s)。ビーム間隔・コース全長の設定が未実装のため、現状はダミー値
    （_dummy_speed_ms / _dummy_lap_avg_ms 参照）。
    """
    repo = TimingRaceRepository(db)

    # 絞り込み用の日付一覧（プルダウン）
    date_options = await repo.list_race_dates(limit=60)

    if date:
        races = await repo.list_races_by_date(date, limit=500)
    else:
        races = await repo.list_races(limit=max(1, min(limit, 100)))

    # --- その日のベスト記録（ハイライト用） ---
    # 受信時に timing_bests へ保持しているので、ここでは読むだけ。
    # （以前は画面を開くたびに全レースを再計算していた）
    day_best: dict[str, dict] = {}
    target_dates = sorted({(r["created_at"] or "")[:10] for r in races if r["created_at"]})
    for d in target_dates:
        if not d:
            continue
        stored = await best_svc.load_bests(db, "day", d)
        day_best[d] = {k: v["value"] for k, v in stored.items()}

    rows = []          # 表示する明細行（1行=1レーンの1周）

    for r in races:
        rid = r["id"]
        try:
            race, result = await build_race_result(db, rid)
        except Exception:
            continue
        if race is None or result is None:
            continue

        # このレースの日のベスト（ハイライト判定に使う）
        bst = day_best.get((race["created_at"] or "")[:10], {})

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
                    sg_ms = (None if lap.lap == 1
                             else _dummy_speed_ms(rid, m.start_lane, 0, lap.lap))
                    sectors[0] = {
                        "s": None,
                        "ms": sg_ms,
                        "sg": True,
                        "best_s": False,
                        "best_ms": _eq_best(sg_ms, bst.get("sector_ms")),
                    }
                    for idx, sec in enumerate(lap.sectors):
                        if idx + 1 > 7:
                            break
                        sp = _dummy_speed_ms(rid, m.start_lane, idx, lap.lap)
                        sectors[idx + 1] = {
                            "s": round(sec.dt_us / 1e6, 3),
                            "ms": sp,
                            "sg": False,
                            # その日のベストか（タイムは最小・速度は最大）
                            "best_s": _eq_best(sec.dt_us / 1e6, bst.get("sector")),
                            "best_ms": _eq_best(sp, bst.get("sector_ms")),
                        }

                lap_avg = (_dummy_lap_avg_ms(rid, m.start_lane, lap.lap)
                           if lap is not None else None)
                rows.append({
                    # --- その日のベスト判定（ハイライト用） ---
                    "best_total": _eq_best(
                        m.total_time_us / 1e6 if m.total_time_us else None,
                        bst.get("total")),
                    "best_max": _eq_best(max_ms, bst.get("max_ms")),
                    "best_lap": _eq_best(
                        lap.lap_time_us / 1e6 if lap is not None else None,
                        bst.get("lap")),
                    "best_lap_avg": _eq_best(lap_avg, bst.get("lap_avg")),
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
                    "lap_avg_ms": lap_avg,
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
            "date_options": date_options,   # 絞り込みプルダウン用
            "current_date": date,           # 選択中の日付（Noneなら最新n件）
            "current_limit": limit,
        },
    )


@router.post("/admin/timing/results/{race_id}/delete")
async def delete_race(
    race_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """計測レースを削除する（通過イベントも一緒に消える・復元不可）。

    ⚠ 取り消しできない操作。画面側で確認ダイアログを出してから呼ぶこと。
    """
    repo = TimingRaceRepository(db)
    race = await repo.get_race(race_id)
    if race is None:
        raise HTTPException(status_code=404, detail="race not found")
    day = (race["created_at"] or "")[:10]
    n_events = await repo.delete_race(race_id)

    # 削除したレースがベストだった場合に備え、その日のベストを再計算する
    # （消した記録がベストのまま残らないようにするため）
    await db.execute("DELETE FROM timing_bests WHERE scope='race' AND scope_key=?",
                     (str(race_id),))
    await db.commit()
    recalculated = 0
    if day:
        try:
            recalculated = await best_svc.recalc_day(
                db, day,
                lambda d: repo.list_races_by_date(d, limit=500),
                build_race_result, _dummy_speed_ms, _dummy_lap_avg_ms,
            )
        except Exception:
            pass

    return JSONResponse({"ok": True, "race_id": race_id,
                         "deleted_events": n_events,
                         "bests_recalculated": recalculated})


@router.get("/admin/timing/bests", response_class=HTMLResponse)
async def bests_page(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    mode: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """ベスト集計（期間・タイプ指定）。

    その日のベストは受信時に保持しているが、任意期間のベストは保持しない。
    リアルタイム性が不要なため、この画面を開いた（＝集計ボタンを押した）ときに
    その場で計算する。
    """
    repo = TimingRaceRepository(db)
    date_options = await repo.list_race_dates(limit=365)

    # 既定：計測実績のある最古〜最新（＝全期間）
    if date_options:
        default_to = date_options[0]["date"]
        default_from = date_options[-1]["date"]
    else:
        default_to = default_from = ""
    d_from = date_from or default_from
    d_to = date_to or default_to

    result = {"bests": {}, "race_count": 0}
    if d_from and d_to:
        result = await best_svc.aggregate_range(
            db,
            date_from=d_from, date_to=d_to,
            mode=(mode if mode in ("f1", "run") else None),
            list_fn=lambda a, b: repo.list_races_between(a, b),
            build_fn=build_race_result,
            speed_fn=_dummy_speed_ms, lap_avg_fn=_dummy_lap_avg_ms,
        )

    # 表示用に整形（順番と単位・説明を固定）
    METRIC_VIEW = [
        ("total",     "トータルタイム",     "秒",  "最速"),
        ("max_ms",    "MAX SPEED",          "m/s", "最高"),
        ("lap",       "ラップタイム",       "秒",  "最速"),
        ("lap_avg",   "ラップ平均SPEED",    "m/s", "最高"),
        ("sector",    "セクタータイム",     "秒",  "最速"),
        ("sector_ms", "セクター通過SPEED",  "m/s", "最高"),
    ]
    items = []
    for key, label, unit, kind in METRIC_VIEW:
        b = result["bests"].get(key)
        items.append({
            "key": key, "label": label, "unit": unit, "kind": kind,
            "value": (round(b["value"], 3) if b else None),
            "race_id": (b.get("race_id") if b else None),
            "start_lane": (b.get("start_lane") if b else None),
            "lap": (b.get("lap") if b else None),
            "sector_no": (b.get("sector_no") if b else None),
            "created_at": (b.get("created_at") if b else None),
            "mode": (b.get("mode") if b else None),
        })

    return templates.TemplateResponse(
        "admin/timing_bests.html",
        {
            "request": request,
            "items": items,
            "race_count": result["race_count"],
            "date_from": d_from,
            "date_to": d_to,
            "mode": mode or "",
            "date_options": date_options,
        },
    )


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
    want = max(1, min(limit, 20))
    # 記録なし（レイアウト未設定・組み立て不能）のレースは表示しないため、
    # 多めに取得してから絞り込む。古い壊れたデータがPIPを埋めるのを防ぐ。
    races = await repo.list_races(limit=min(want * 5 + 10, 100))
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
        # 記録が組み立てられなかったレースは出さない（「計測中／記録なし」を除外）
        if not rows:
            continue

        keys = race.keys() if hasattr(race, "keys") else []
        out.append({
            "race_id": rid,
            "heat_id": (race["heat_id"] if "heat_id" in keys else None),
            "target_laps": (race["target_laps"] if "target_laps" in keys else None),
            "created_at": (race["created_at"] if "created_at" in keys else None),
            "ranking": rows,
        })
        if len(out) >= want:
            break
    return JSONResponse({"races": out})


def _eq_best(value, best, tol: float = 1e-6) -> bool:
    """保持しているベスト値と一致するか（浮動小数の誤差を許容）。

    timing_bests には「秒」「m/s」で保存している。µs のまま比較しないこと。
    """
    return (value is not None and best is not None and abs(value - best) < tol)


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
