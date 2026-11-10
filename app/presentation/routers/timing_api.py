"""タイミング計測のGW受信口と結果表示。

- POST /api/timing/races          レース開始（ヒートID・レイアウト・周回・緑時刻）
- POST /api/timing/races/{id}/events  GWからの通過イベントバッチ（冪等・D11/D12）
- GET  /admin/timing/results      レース一覧（結果閲覧）
- GET  /admin/timing/results/{id} 1レースの結果（ラップ・セクター・順位）

GWからのPOSTは、環境変数 TIMING_TOKEN があれば X-Timing-Token を要求する
（未設定ならローカル運用として素通し・README_timing 方針）。
"""

import os
import time

from fastapi import APIRouter, Request, Depends, HTTPException, Header, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
import aiosqlite
import secrets

from app.infrastructure.db.connection import get_db
from app.infrastructure.db.repositories.timing_repository import (
    TimingRaceRepository,
    TimingLayoutRepository,
)
from app.application.timing_race_service import build_race_result, invalidate_race_result_cache
from app.application import timing_best_service as best_svc
from app.application import timing_bridge_service as bridge_svc
from app.application import timing_speed_service as spd_svc
from app.application import timing_sample_service as sample_svc
from app.application.timing_sample_irregular import (
    describe_pattern_short as irr_describe,
    summarize_patterns as irr_summary,
)
from app.application.timing_chart_service import build_position_chart, build_raw_detections, build_sensor_matrix
from app.application import timing_status_service as status_svc
from app.application import timing_race_speed_store as speed_store
from app.domain.rotation import LANES, LayoutElement
from app.domain.race_builder import mode_mismatch  # D7/E6(24.34)：予定/実測モード照合
from app.presentation.templates import templates
from app.presentation.routers.m4laps_guard import require_m4laps

router = APIRouter()

TIMING_TOKEN = os.environ.get("TIMING_TOKEN", "")
# クラウド公開時にトークン未設定だと、GW受信口が事実上の無認証公開になる。
# 既存デプロイを壊さないため既定は警告のみ。TIMING_TOKEN_REQUIRED=1 で強制拒否。
_TOKEN_REQUIRED = os.environ.get("TIMING_TOKEN_REQUIRED", "0") == "1"
try:
    from app.core.config import IS_CLOUD as _IS_CLOUD_T
    if _IS_CLOUD_T and not TIMING_TOKEN:
        print("[timing] WARNING: クラウド版で TIMING_TOKEN 未設定。"
              "/api/timing/* が無認証で公開されています。環境変数 TIMING_TOKEN を設定してください。", flush=True)
except Exception:
    pass


def _check_token(x_timing_token: str | None):
    """TIMING_TOKEN が設定されていれば照合。未設定なら素通し
    （TIMING_TOKEN_REQUIRED=1 のときは未設定を拒否＝安全側強制）。"""
    if _TOKEN_REQUIRED and not TIMING_TOKEN:
        raise HTTPException(status_code=503, detail="TIMING_TOKEN not configured")
    # S2: 定数時間比較に統一（タイミング攻撃対策）。未設定時の素通し挙動は従来どおり。
    # bytes で比較する：str 版は非ASCII文字で TypeError(500) になるため（ヘッダは latin-1 で届きうる）。
    if TIMING_TOKEN and not secrets.compare_digest(
            (x_timing_token or "").encode("utf-8"), TIMING_TOKEN.encode("utf-8")):
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
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    repo = TimingRaceRepository(db)
    race_id = await repo.create_race(
        heat_tag=data.get("heat_tag"),
        layout_id=data.get("layout_id"),
        target_laps=int(data.get("target_laps") or 3),
        client_key=(str(data.get("client_key")) if data.get("client_key") else None),
        green_t_us=data.get("green_t_us"),
    )
    return JSONResponse({"race_id": race_id})


@router.post("/api/timing/races/{race_id}/green")
async def set_race_green(
    race_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    x_timing_token: str | None = Header(default=None),
    _guard: bool = Depends(require_m4laps),
):
    """既存レースに緑時刻を後付けする（走行式→F1式へ切り替え）。

    GWは赤ボタン時点では緑時刻を持たないため走行式で create_race し、
    緑を出した瞬間にこのAPIで green_t_us を書き込む。これにより
    「緑の瞬間にレースを作り直す」割り切り（docs/19 残課題7）を解消する。

    ⚠ M4LAPSはクラウド版限定。オンプレ版・ライセンス未登録では 404（require_m4laps）。

    body(JSON): {"green_t_us": int}
    戻り値: {"updated": 1} / レースが無ければ 404
    """
    _check_token(x_timing_token)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    green = data.get("green_t_us")
    if green is None:
        raise HTTPException(status_code=400, detail="green_t_us required")
    repo = TimingRaceRepository(db)
    if await repo.get_race(race_id) is None:
        raise HTTPException(status_code=404, detail="race not found")
    updated = await repo.set_green_t_us(race_id, int(green))
    return JSONResponse({"updated": updated})


async def _post_ingest_work(store, db_path: str, race_id: int) -> None:
    """受信後の後処理（速度保存→ベスト更新→状態判定→TA viewへpush）。

    ★20261031：応答後にバックグラウンドで実行する（旧：応答前に同期実行）。
      GWは応答を待つだけで後処理結果を使わないのに、後処理が重いと GW 側タイムアウト→
      同じバッチを再POST（冪等で捨てるが毎回再計算）という“無駄打ち”を生んでいた。
    ⚠ リクエストのDB接続は応答後に閉じられ、店舗ContextVarも戻されるため、
      店舗を自分でセットし直し、専用接続を開いて処理する。
    """
    from app.core.store_context import current_store
    token = current_store.set(store)
    try:
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA foreign_keys=ON")
            repo = TimingRaceRepository(db)
            # 速度・平均を計算して保存（ベストは保存値を読むので先）。
            try:
                await speed_store.compute_and_store_speeds(db, race_id)
                await db.commit()
            except Exception as e:
                print(f"[timing] speed store failed race={race_id}: {type(e).__name__}: {e}", flush=True)
            # ベスト記録を更新（保存済みの速度を使う）。
            try:
                await best_svc.update_for_race(
                    db, race_id, build_race_result, *(await _speed_fns_for_race(db, race_id))
                )
            except Exception as e:
                print(f"[timing] bests update failed race={race_id}: {type(e).__name__}: {e}", flush=True)
            # 二段構えDB（I群 §24.71）: status を自動判定して書き込む。
            try:
                race2, result2 = await build_race_result(db, race_id)
                if result2 is not None:
                    lrepo = TimingLayoutRepository(db)
                    elem_rows = await lrepo.get_elements(race2["layout_id"])
                    layout_elems = [{"kind": e["kind"], "node_id": e["node_id"]} for e in elem_rows]
                    ev_rows = await repo.get_events(race_id)
                    status, _detail = status_svc.judge_status(
                        result2, layout_elems, ev_rows,
                        target_laps=race2["target_laps"], n_lanes=LANES,
                    )
                    await repo.update_status(race_id, status)
                    # ★20261110：M4LAPS ON＋アクティブ選択中なら、クリーンな計測を自動反映。
                    await _maybe_auto_apply(store, db, race_id, result2,
                                            layout_elems, ev_rows, race2, repo)
            except Exception as e:
                print(f"[timing] status judge failed race={race_id}: {type(e).__name__}: {e}", flush=True)
        finally:
            await db.close()
    except Exception as e:
        print(f"[timing] post-ingest failed race={race_id}: {type(e).__name__}: {e}", flush=True)
    finally:
        current_store.reset(token)
    # TA view（FHD計測ビューア）へ push（後処理完了後＝結果が確定してから）。失敗は無視。
    try:
        from app.services import live_events
        live_events.publish(store.id if store is not None else 0, "ta")
    except Exception:
        pass


async def _maybe_auto_apply(store, db, race_id, result, layout_elems, event_rows, race_row, repo):
    """M4LAPS ON＋アクティブレース選択中なら、クリーンな計測を自動でアクティブへ反映する（20261110）。

    - 発火条件：M4LAPSライセンス有効／GWアクティブ設定あり／auto_clean（乱入なし・CO許容）。
    - 冪等：この計測レースが既にどこか（heat_id/applied_group_id/applied_ht_group_id）へ
      反映済みなら何もしない。
    - 反映先に既に結果がある場合は上書きせず、保留（gw_auto_pending）に積んで
      admin画面のダイアログに委ねる。
    - 反映は手動と同じエンドポイント関数を force=True で呼ぶ（後処理・アクティブ移動も共通）。
    - 全形式対応：予選リスト(heat)／勝ち抜け(ow-heat)／ヒートトーナメント(ht-group)／決勝(bracket)。
    - 例外は握りつぶす（後処理本体を妨げない）。手動反映ボタンとPIP選択は従来どおり残る。
    """
    try:
        from app.domain import m4laps_license
        from app.infrastructure.db.repositories import gw_active_repo as _gwa

        if not await m4laps_license.is_licensed(db):
            return
        act = await _gwa.get(db)
        if not act:
            return
        # 乱入なし・クリーン判定（CO許容）。needs_review相当は手動に残す。
        target_laps = race_row["target_laps"] if "target_laps" in race_row.keys() else None
        if target_laps is None:
            return
        if not status_svc.auto_clean(result, layout_elems, event_rows, target_laps, LANES):
            return
        # この計測レースが既に反映済みなら何もしない（冪等）。
        try:
            async with db.execute(
                "SELECT heat_id, applied_group_id, applied_ht_group_id FROM timing_races WHERE id = ?",
                (race_id,)) as c:
                tr = await c.fetchone()
            if tr and (tr["heat_id"] or tr["applied_group_id"] or tr["applied_ht_group_id"]):
                return
        except Exception:
            pass

        kind, ref = act["kind"], act["ref_id"]
        apply_path = None
        handler = None
        has_result = False

        # ★自動反映の除外（20261110）：敗者復活戦・3位決定戦・裏トーナメントは自動反映しない。
        #   「1回でも負けたら記録の対象外」。これらのラウンド種別（優先度1=敗者復活/裏, 2=3位決定）は
        #   対象外とし、手動反映ボタンだけ従来どおり使えるようにする。通常ラウンド・決勝は対象。
        if kind in (_gwa.KIND_BRACKET_GROUP, _gwa.KIND_HT_GROUP):
            _pfx = "bracket" if kind == _gwa.KIND_BRACKET_GROUP else "ht"
            async with db.execute(
                f"SELECT rd.round_type AS rt FROM {_pfx}_groups g "
                f"JOIN {_pfx}_rounds rd ON rd.id = g.round_id WHERE g.id = ?", (ref,)) as c:
                _rtrow = await c.fetchone()
            if _rtrow and _gwa._rtype_pri(_rtrow["rt"]) in (1, 2):
                return   # 敗者復活/3位決定/裏 → 自動反映しない（手動反映は可）

        if kind == _gwa.KIND_BRACKET_GROUP:
            apply_path = f"/api/timing/apply/bracket/{ref}"
            handler = (apply_latest_to_bracket, {"group_id": ref})
            async with db.execute(
                "SELECT 1 FROM bracket_results WHERE group_id = ? LIMIT 1", (ref,)) as c:
                has_result = bool(await c.fetchone())
        elif kind == _gwa.KIND_HT_GROUP:
            apply_path = f"/api/timing/apply/ht-group/{ref}"
            handler = (apply_latest_to_ht_group, {"group_id": ref})
            async with db.execute(
                "SELECT 1 FROM ht_results WHERE group_id = ? LIMIT 1", (ref,)) as c:
                has_result = bool(await c.fetchone())
        elif kind == _gwa.KIND_HEAT:
            # qualifying_type で heat / ow-heat を分岐。未知（タイムアタック等）は自動対象外。
            async with db.execute(
                "SELECT t.qualifying_type AS qt FROM heats h "
                "JOIN tournaments t ON t.id = h.tournament_id WHERE h.id = ?", (ref,)) as c:
                hrow = await c.fetchone()
            qt = hrow["qt"] if hrow else None
            if qt == "order_winner":
                return   # ★勝ち抜けは手動のみ＝自動反映しない（段階制のため自動で触らない）
            elif qt == "time_attack":
                return   # タイムアタックは別機構（個別走行）のため自動反映対象外
            else:
                # ★それ以外の予選（none/roundrobin/none_roundrobin/point/order/heat_roundrobin 等）は
                #   すべて apply/heat で自動反映する（20261110 ユーザー確定「すべて自動」）。
                apply_path = f"/api/timing/apply/heat/{ref}"
                handler = (apply_latest_to_heat, {"heat_id": ref})
            # 結果有無：status='done' または heat_results あり
            async with db.execute("SELECT status FROM heats WHERE id = ?", (ref,)) as c:
                st = await c.fetchone()
            done = bool(st and (st["status"] or "") == "done")
            async with db.execute(
                "SELECT 1 FROM heat_results hr JOIN heat_lanes hl ON hl.id = hr.heat_lane_id "
                "WHERE hl.heat_id = ? LIMIT 1", (ref,)) as c:
                hasr = bool(await c.fetchone())
            has_result = done or hasr
        else:
            return

        label = await _gwa._race_label(db, kind, ref)

        if has_result:
            # 上書きはせず、admin画面の確認ダイアログに委ねる（Q2）。
            await _gwa.set_auto_pending(db, kind, ref, race_id, apply_path, label)
            return

        # 反映実行（結果無し＝警告なし。force=True。ハンドラ内で後処理＋アクティブ移動も走る）。
        fn, kw = handler
        try:
            await fn(race_id=race_id, force=True, db=db, _guard=True, **kw)
            print(f"[timing] auto-apply ok race={race_id} kind={kind} ref={ref}", flush=True)
        except Exception as e:
            print(f"[timing] auto-apply exec failed race={race_id} kind={kind} ref={ref}: "
                  f"{type(e).__name__}: {e}", flush=True)
    except Exception as e:
        print(f"[timing] auto-apply error race={race_id}: {type(e).__name__}: {e}", flush=True)


@router.post("/api/timing/races/{race_id}/events")
async def post_events(
    race_id: int,
    request: Request,
    background: BackgroundTasks,
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
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    events = data.get("events", [])
    repo = TimingRaceRepository(db)

    race = await repo.get_race(race_id)
    if race is None:
        raise HTTPException(status_code=404, detail="race not found")

    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="events must be a list")
    if len(events) > 2000:
        # GWの1バッチは最大でも数百件（1レース=3レーン×周回×ゲート数）。
        # 上限超過は不正または暴走とみなし、メモリ・Tx肥大を防ぐ。
        raise HTTPException(status_code=413, detail="too many events in one batch")
    try:
        inserted, duplicate = await repo.insert_events_batch(race_id, events)
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"bad event: {e}")

    # ★後処理（速度→ベスト→状態→TA push）は応答後にバックグラウンドで。
    #   新規挿入ゼロ（＝GWの再送・全件重複）なら何も変わらないので後処理自体をスキップ＝無駄打ち吸収。
    if inserted > 0:
        from app.infrastructure.db.connection import current_db_path
        _store = getattr(request.state, "store", None)
        background.add_task(_post_ingest_work, _store, current_db_path(), race_id)

    return JSONResponse({"inserted": inserted, "duplicate": duplicate,
                         "bests_updated": {}})   # 後処理は非同期化したため空（GWは参照しない）


# ---------------------------------------------------------------------------
# 結果表示（admin画面）
# ---------------------------------------------------------------------------

def _best_items(bests: dict) -> list[dict]:
    """ベスト辞書 {metric: 記録} を、画面表示用の並び・単位付きリストにする。

    セクターは区間ごとに独立して集計しているため、S1〜S7 を個別に出す。
    （区間の長さが違うので、まとめて比べると短い区間ばかりが上位になる）
    記録が無い区間は行ごと出さない（レイアウトによって区間数が違うため）。
    """
    metric_view = [
        ("total",     "トータルタイム",     "秒",  "最速"),
        ("total_avg", "TOTAL平均SPEED",     "m/s", "最高"),
        ("max_ms",    "MAX SPEED",          "m/s", "最高"),
        ("lap",       "ラップタイム",       "秒",  "最速"),
        ("lap_avg",   "ラップ平均SPEED",    "m/s", "最高"),
    ]
    for i in range(1, best_svc.MAX_SECTORS + 1):
        if bests.get(best_svc.sector_metric(i)):
            metric_view.append(
                (best_svc.sector_metric(i), f"S{i} セクタータイム", "秒", "最速"))
        if bests.get(best_svc.sector_speed_metric(i)):
            metric_view.append(
                (best_svc.sector_speed_metric(i), f"S{i} 通過SPEED", "m/s", "最高"))

    items = []
    for key, label, unit, kind in metric_view:
        b = bests.get(key)
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
    return items


@router.get("/admin/timing/results", response_class=HTMLResponse)
async def results_page(
    request: Request,
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 10,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """計測結果の一覧（レーン×周回の明細）。

    既定は最新10レース。表示件数は 10/30/50 から選ぶ。
    from_date〜to_date（YYYY-MM-DD）を指定すると、営業日ルールで計測日を
    絞り込む。from_date・to_date はどちらも「計測日（カレンダー日）」として扱い、
    範囲は from_date 09:00 〜 to_date の翌朝 08:59:59。from=to=同じ日なら、その
    営業日1日分（09:00〜翌08:59）になる。
    date=YYYY-MM-DD は旧リンク互換（その日のカレンダー日で表示）。

    1行 = 1レーンの1周。TS/CASE/POS/LANE/TOTAL はレーンごとに rowspan で結合し、
    LAP（周回番号・ラップタイム・平均速度）と SECTOR 0〜7 を周ごとに並べる。

    速度は秒速(m/s)。レイアウトに「ビーム間隔(mm)」「1周の距離(m)」を設定すると
    実測値になる。未設定なら None＝画面では「—」を表示する。
    """
    repo = TimingRaceRepository(db)

    # 表示件数は 10/30/50 のみ許可（それ以外は既定10）
    if limit not in (10, 30, 50):
        limit = 10

    # 期間絞り込みの既定値（営業日 09:00〜翌08:59）。
    # 例) 現在が 07-30 なら from_date=07-30・to_date=07-31。
    _bw_from, _bw_to, _bw_label = best_svc.business_day_window()
    default_from_date = _bw_from[:10]
    # ★既定の終了日は「営業日ラベル（=その計測日そのもの）」。翌朝の日付ではない。
    #   終端は下の窓計算で内部的に翌日08:59へ繰り下げる。
    default_to_date = _bw_label

    current_range = None
    range_from_date = default_from_date
    range_to_date = default_to_date

    if date:
        # 旧リンク互換：その日のカレンダー日で表示
        races = await repo.list_races_by_date(date, limit=500)
    elif from_date or to_date:
        # 期間絞り込み（営業日ルール：開始日09:00〜終了日の翌朝08:59:59）
        fd = from_date or default_from_date
        td = to_date or default_to_date
        # 営業日ルール：選んだ計測日の 09:00 から、その日の翌朝 08:59:59 まで。
        # to_date は「選んだ計測日そのもの（カレンダー日）」として扱い、終端を翌日へ
        # 繰り下げる。こうしないと from=to=同じ日のとき窓が逆転して0件になる（旧バグ）。
        from datetime import datetime as _dt, timedelta as _tdelta
        ts_from = f"{fd} 09:00:00"
        try:
            _td_next = (_dt.strptime(td, "%Y-%m-%d") + _tdelta(days=1)).strftime("%Y-%m-%d")
        except Exception:
            _td_next = td
        ts_to = f"{_td_next} 08:59:59"
        # list_races_between_ts は古い順で返るため、表示用に新しい順へ反転
        races = list(reversed(await repo.list_races_between_ts(ts_from, ts_to, limit=2000)))
        current_range = {"from": fd, "to": td}
        range_from_date, range_to_date = fd, td
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
        # {metric: [1位の値, 2位の値, 3位の値]} の形にしておく
        day_best[d] = {k: [x["value"] for x in v] for k, v in stored.items()}

    rows = []          # 表示する明細行（1行=1レーンの1周）
    # 速度の設定状況（1レースでも設定済みなら True）。画面下の注記に使う。
    speed_ready = {"pass_speed": False, "lap_avg": False}
    _cfg_by_layout: dict = {}   # ★性能：速度設定はレイアウト単位。1日分のレース数ぶん同じ問合せを繰り返さない

    for r in races:
        rid = r["id"]
        try:
            race, result = await build_race_result(db, rid)
        except Exception as e:
            print(f"[timing] results: build failed race={rid}: {type(e).__name__}: {e}", flush=True)   # C13：黙って消さない
            continue
        if race is None or result is None:
            continue

        # 速度は反映時に計算・保存済み。ここでは読むだけ（都度計算しない）。
        stored = await speed_store.load_speeds(db, rid)
        # 画面の注記用：どの指標が実測できる設定になっているか
        _lid = race["layout_id"]
        if _lid not in _cfg_by_layout:
            _cfg_by_layout[_lid] = await spd_svc.load_speed_config(db, _lid)
        cfg = _cfg_by_layout[_lid]
        _ok = spd_svc.is_configured(cfg)
        speed_ready["pass_speed"] = speed_ready["pass_speed"] or _ok["pass_speed"]
        speed_ready["lap_avg"] = speed_ready["lap_avg"] or _ok["lap_avg"]

        # このレースの日のベスト（ハイライト判定に使う）
        bst = day_best.get((race["created_at"] or "")[:10], {})
        # ★20261031 C3：明細行の組み立ては rows_for_race に一本化（TA view と同一実装）
        rows.extend(rows_for_race(race, result, stored, bst, list_row=r))

    # --- 当日のベスト（09:00〜翌08:59）：★C3 today_best_map_for に一本化（TA view と同一集計）---
    _tb = await today_best_map_for(db)
    ts_from, ts_to, today_label = _tb["today_from"], _tb["today_to"], _tb["today_label"]
    today_bests, today_best_map, today_race_count = _tb["today_bests"], _tb["today_best_map"], _tb["today_race_count"]

    return templates.TemplateResponse(
        "admin/timing_results.html",
        {
            "request": request,
            "rows": rows,
            # セクションゲートは最大6基＝区間は最大7（S/G→SQ1…SQ6→S/G）。
            # レイアウトによらず常に S1〜S7 の枠を出し、無い区間は「—」を表示する。
            "sector_nos": list(range(1, 8)),
            "races": races,
            "current_date": date,           # 旧リンク互換（?date=）で選択中の日付
            "current_limit": limit,
            # 期間絞り込み（計測日）：営業日ルール 09:00〜翌08:59
            "current_range": current_range,     # {"from","to"} 絞り込み中のみ
            "range_from_date": range_from_date, # 日付入力の値（既定=営業日）
            "range_to_date": range_to_date,
            # 当日のベスト（09:00区切り）
            "today_bests": today_bests,
            "today_best_map": today_best_map,
            "today_label": today_label,
            "today_from": ts_from,
            "today_to": ts_to,
            "today_race_count": today_race_count,
            # 速度が実測できる設定になっているか（未設定なら画面に案内を出す）
            "speed_ready": speed_ready,
            # テスト用サンプル送信パネル（M4LAPS_SAMPLE=0 で非表示）
            "sample_enabled": sample_svc.SAMPLE_ENABLED,
            "sample_layouts": await TimingLayoutRepository(db).list_layouts(),
            "sample_lanes": LANES,
        },
    )


@router.get("/admin/timing/export")
async def export_db(
    request: Request,
    _guard: bool = Depends(require_m4laps),
):
    """現在の店舗のDBを丸ごと安全コピー(.backup)してダウンロードさせる。

    生のSQLite 1ファイルなので、timing_events(t_us/t_us_b/quality/excluded)・
    レイアウト・速度・ベストまで全部入り＝解析用の決定版。店舗はリクエスト
    コンテキストから解決するので、開いている店舗のDBが確実に落ちる。
    """
    import sqlite3
    import tempfile
    import datetime
    from starlette.background import BackgroundTask
    from fastapi.responses import FileResponse
    from app.infrastructure.db.connection import current_db_path

    src_path = current_db_path()
    if not os.path.exists(src_path):
        raise HTTPException(status_code=404, detail="store db not found")

    slug = "default"
    try:
        from app.core.store_context import get_current_store
        st = get_current_store()
        if st and st.slug:
            slug = st.slug
    except Exception:
        pass
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"m4laps_{slug}_{ts}.db"

    # ライブDBをロックせず安全にスナップショット（WALも正しく取り込める）
    fd, tmp_path = tempfile.mkstemp(prefix="m4laps_export_", suffix=".db")
    os.close(fd)
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    return FileResponse(
        tmp_path,
        media_type="application/octet-stream",
        filename=fname,
        background=BackgroundTask(os.remove, tmp_path),
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
    invalidate_race_result_cache(race_id)   # ★組み立てキャッシュも破棄（idは再利用されないが念のため）

    # 削除したレースがベストだった場合に備え、その日のベストを再計算する
    # （消した記録がベストのまま残らないようにするため）
    await db.execute("DELETE FROM timing_bests WHERE scope='race' AND scope_key=?",
                     (str(race_id),))
    await db.commit()
    recalculated = 0
    if day:
        try:
            day_races = await repo.list_races_by_date(day, limit=500)
            sf, lf, tf = await _speed_fns_for_races(db, [r["id"] for r in day_races])
            recalculated = await best_svc.recalc_day(
                db, day,
                lambda d: repo.list_races_by_date(d, limit=500),
                build_race_result, sf, lf, tf,
            )
        except Exception as e:
            print(f"[timing] delete: day recalc failed day={day}: {type(e).__name__}: {e}", flush=True)

    return JSONResponse({"ok": True, "race_id": race_id,
                         "deleted_events": n_events,
                         "bests_recalculated": recalculated})


@router.post("/admin/timing/sample")
async def create_sample_race(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """テスト用のサンプル計測データを投入する（実機GWの代わり）。

    実機が無いテスト環境で画面・集計を確認するための機能。
    通常の受信口（POST /api/timing/races/{id}/events）と同じ形のイベントを
    サーバ内で組み立てて保存するので、実機から届いた記録と同じ扱いになる。

    body(JSON): {"layout_id":int, "mode":"race"|"free", "laps":int,
                 "lanes":int, "races":int, "heat_tag":int?}

    ⚠ 本番環境では .env に M4LAPS_SAMPLE=0 を入れて無効化する（404になる）。
    """
    if not sample_svc.SAMPLE_ENABLED:
        raise HTTPException(status_code=404)

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    lrepo = TimingLayoutRepository(db)
    rrepo = TimingRaceRepository(db)

    layout_id = int(data.get("layout_id") or 0)
    layout = await lrepo.get_layout(layout_id)
    if layout is None:
        raise HTTPException(status_code=404, detail="レイアウトが見つかりません")

    mode = "race" if str(data.get("mode") or "free") == "race" else "free"
    laps = max(1, min(int(data.get("laps") or layout["target_laps"] or 3),
                      sample_svc.MAX_LAPS))
    lanes = max(1, min(int(data.get("lanes") or LANES), LANES))
    n_races = max(1, min(int(data.get("races") or 1), sample_svc.MAX_RACES))
    heat_tag = data.get("heat_tag")
    heat_tag = int(heat_tag) if heat_tag not in (None, "") else None
    # イレギュラーデータ生成フラグ（無=従来どおり全完走 / 有=CO/DNS等を混ぜる）
    irregular = bool(data.get("irregular"))

    elems = await lrepo.get_elements(layout_id)
    layout_elems = [LayoutElement(kind=e["kind"], node_id=e["node_id"]) for e in elems]

    # ビーム間隔が設定されていれば、それに合わせた打刻にする（通過速度が実測相当になる）
    cfg = await spd_svc.load_speed_config(db, layout_id)
    gaps = cfg.get("beam_gap_by_node") or {}

    race_ids: list[int] = []
    base = int(time.time() * 1_000_000)
    for i in range(n_races):
        try:
            green_t_us, events, lane_patterns, gate_names = sample_svc.build_sample_events(
                layout_elems,
                target_laps=laps,
                lanes=lanes,
                mode=mode,
                # レース同士が時間的に重ならないよう、1本ぶんずつ後ろにずらす
                base_t_us=base + int(i * (sample_svc.TARGET_TOTAL_S + 10) * 1_000_000),
                beam_gap_by_node=gaps,
                irregular=irregular,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # イレギュラー生成のときは、各レーンの割当パターンを注釈にして残す。
        # 例: "全車CO ｜ L1:CO(1周SQ0手前) L2:CO(2周SQ1手前) L3:DNS"
        # gate_names は build_course のゲート列（LC除外）なので gate_idx と一致する。
        sample_note = None
        if irregular and lane_patterns:
            summary = irr_summary(lane_patterns)
            parts = []
            for sl, pat in enumerate(lane_patterns, 1):
                parts.append(f"L{sl}:{irr_describe(pat, gate_names)}")
            sample_note = summary + " ｜ " + " ".join(parts)

        race_id = await rrepo.create_race(
            heat_tag=heat_tag,
            layout_id=layout_id,
            target_laps=laps,
            green_t_us=green_t_us,
            sample_note=sample_note,
        )
        for ev in events:
            await rrepo.insert_event(race_id, ev)

        # 速度を先に保存（ベストは保存値を読むため）
        try:
            await speed_store.compute_and_store_speeds(db, race_id)
        except Exception as e:
            print(f"[timing] sample: speed store failed race={race_id}: {type(e).__name__}: {e}", flush=True)

        # 受信口と同じくベスト記録を更新する（失敗しても投入自体は成功扱い）
        try:
            await best_svc.update_for_race(
                db, race_id, build_race_result,
                *(await _speed_fns_for_race(db, race_id))
            )
        except Exception as e:
            print(f"[timing] sample: bests update failed race={race_id}: {type(e).__name__}: {e}", flush=True)

        # 二段構えDB（I群 §24.71）: サンプルも受信口と同じく status を判定。
        # イレギュラー有のサンプルはここで needs_review に振り分けられる。
        try:
            race2, result2 = await build_race_result(db, race_id)
            if result2 is not None:
                el2 = await lrepo.get_elements(race2["layout_id"])
                le2 = [{"kind": e["kind"], "node_id": e["node_id"]} for e in el2]
                ev2 = await rrepo.get_events(race_id)
                st, _d = status_svc.judge_status(
                    result2, le2, ev2,
                    target_laps=race2["target_laps"], n_lanes=LANES)
                await rrepo.update_status(race_id, st)
        except Exception as e:
            print(f"[timing] sample: status judge failed race={race_id}: {type(e).__name__}: {e}", flush=True)
        race_ids.append(race_id)

    # 区間数が少ないと、FINISHを35秒前後にしたとき1区間が2秒に収まらない。
    # 黙って条件を外すのではなく、画面に理由を出す。
    n_sections, est = sample_svc.estimate_sector_s(layout_elems, laps)
    note = None
    if est > sample_svc.SECTOR_MAX_S:
        need = -(-int(sample_svc.TARGET_TOTAL_S)
                 // int(n_sections * sample_svc.SECTOR_MAX_S))
        note = (f"1周{n_sections}区間×{laps}周では、FINISH約"
                f"{sample_svc.TARGET_TOTAL_S:.0f}秒に対して1区間が約{est:.1f}秒になります"
                f"（2秒以内にするには{need}周以上、またはセクションゲートの追加が必要）。")

    return JSONResponse({"ok": True, "race_ids": race_ids,
                         "laps": laps, "lanes": lanes, "mode": mode,
                         "note": note})


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
        # 期間内の全レース分の速度を先に読み込む（speed_fn は同期関数のため）
        range_races = await repo.list_races_between(d_from, d_to)
        agg_speed_fn, agg_lap_avg_fn, agg_total_avg_fn = await _speed_fns_for_races(
            db, [r["id"] for r in range_races]
        )
        result = await best_svc.aggregate_range(
            db,
            date_from=d_from, date_to=d_to,
            mode=(mode if mode in ("f1", "run") else None),
            list_fn=lambda a, b: repo.list_races_between(a, b),
            build_fn=build_race_result,
            speed_fn=agg_speed_fn, lap_avg_fn=agg_lap_avg_fn,
            total_avg_fn=agg_total_avg_fn,
        )

    # 表示用に整形（順番と単位・説明は _best_items に集約）
    items = _best_items(result["bests"])

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


@router.post("/api/timing/apply/heat/{heat_id}")
async def apply_latest_to_heat(
    heat_id: int,
    race_id: int | None = None,
    force: bool = False,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """最新の計測結果を予選ヒートへ反映する。

    race_id を省略した場合は「記録のある最新レース」を使う。
    運用上、走り終わった直後にその組のカードで押す想定のため、
    どのレースかを選ばせず常に最新を採る。

    通常は確認なしで反映する。ただし次のときは 409 を返し、
    画面側で確認を取ってから force=true で再送してもらう（決勝と同じ流儀）。
      - そのヒートに既に結果が入っている（上書きになる）
      - 同じ計測結果を既に別のヒートへ反映している（重複反映）
    """
    repo = TimingRaceRepository(db)
    race, result = await _pick_race(db, repo, race_id)
    if result is None:
        raise HTTPException(status_code=404, detail="反映できる計測結果がありません")

    if not force:
        warnings = []
        # ① このヒートに既に結果があるか
        async with db.execute(
            "SELECT 1 FROM heat_results hr JOIN heat_lanes hl ON hl.id = hr.heat_lane_id "
            "WHERE hl.heat_id = ? LIMIT 1",
            (heat_id,),
        ) as cur:
            if await cur.fetchone():
                warnings.append({
                    "code": "already_has_result",
                    "message": "このヒートには既に結果が入力されています。上書きされます。",
                })
        # ② この計測結果を既に別のヒートへ反映していないか
        keys = race.keys() if hasattr(race, "keys") else []
        prev = race["heat_id"] if "heat_id" in keys else None
        if prev is not None and int(prev) != int(heat_id):
            async with db.execute(
                "SELECT heat_no, round_no FROM heats WHERE id = ?", (prev,)
            ) as cur:
                h = await cur.fetchone()
            if h and h["round_no"]:
                hname = f"予選{h['round_no']}回目の{h['heat_no']}レース目"
            elif h:
                hname = f"{h['heat_no']}レース目"
            else:
                hname = f"ヒートID {prev}"
            warnings.append({
                "code": "already_applied_to_other",
                "message": f"この計測結果は既に{hname}へ反映済みです。"
                           f"同じ結果を二重に使うことになります。",
            })
        # ③ D7/E6(24.34)：予定モードと実測(green_t_us)の食い違い（事後チェック）。
        #   ヒート設定 planned_mode('f1'/'run'/NULL) と反映レコードの green_t_us 有無を照合。
        #   緑の出し忘れ/誤操作で予定とズレていれば警告。NULLなら照合しない（後方互換）。
        try:
            async with db.execute(
                "SELECT planned_mode FROM heats WHERE id = ?", (heat_id,)
            ) as cur:
                hrow = await cur.fetchone()
            planned_mode = hrow["planned_mode"] if hrow else None
        except Exception:
            planned_mode = None   # 旧DB（planned_mode列なし）は照合しない
        race_green = race["green_t_us"] if "green_t_us" in keys else None
        if mode_mismatch(planned_mode, race_green):
            planned_label = "F1式（緑あり）" if planned_mode == "f1" else "走行式（緑なし）"
            actual_label = "F1式（緑あり）" if race_green is not None else "走行式（緑なし）"
            warnings.append({
                "code": "mode_mismatch",
                "message": f"このヒートの予定は{planned_label}ですが、"
                           f"計測結果は{actual_label}です。"
                           f"緑の出し忘れ・誤操作の可能性があります。",
            })
        if warnings:
            return JSONResponse(
                {"ok": False, "reason": "warning", "warnings": warnings},
                status_code=409,
            )

    ranking = _ranking_payload(result)
    res = await bridge_svc.apply_race_to_heat(
        db, race_id=race["id"], heat_id=heat_id, ranking=ranking
    )
    if res.get("error"):
        raise HTTPException(status_code=400,
                            detail="このヒートのレーン割当が見つかりません")
    # 反映＝確定。アクティブを次のレースへ自動移動（手動／自動反映で統一）。失敗は無視。
    from app.infrastructure.db.repositories import gw_active_repo as _gwa
    await _gwa.advance_after_confirm(db, _gwa.KIND_HEAT, heat_id)
    return JSONResponse({"ok": True, "race_id": race["id"], **res})


@router.post("/api/timing/apply/bracket/{group_id}")
async def apply_latest_to_bracket(
    group_id: int,
    race_id: int | None = None,
    force: bool = False,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """最新の計測結果を決勝グループへ反映する（slot_no ↔ レーン番号で照合）。

    通常は確認なしで反映する。ただし次のときは 409 を返し、
    画面側で確認を取ってから force=true で再送してもらう。
      - そのグループに既に結果が入っている（上書きになる）
      - 同じ計測結果を既に別のグループへ反映している（重複反映）
    """
    repo = TimingRaceRepository(db)
    race, result = await _pick_race(db, repo, race_id)
    if result is None:
        raise HTTPException(status_code=404, detail="反映できる計測結果がありません")

    if not force:
        warnings = []
        # ① このグループに既に結果があるか
        async with db.execute(
            "SELECT 1 FROM bracket_results WHERE group_id = ?", (group_id,)
        ) as cur:
            if await cur.fetchone():
                warnings.append({
                    "code": "already_has_result",
                    "message": "この組には既に結果が入力されています。上書きされます。",
                })
        # ② この計測結果を既に別のグループへ反映していないか
        keys = race.keys() if hasattr(race, "keys") else []
        prev = race["applied_group_id"] if "applied_group_id" in keys else None
        if prev is not None and int(prev) != int(group_id):
            async with db.execute(
                "SELECT group_no FROM bracket_groups WHERE id = ?", (prev,)
            ) as cur:
                g = await cur.fetchone()
            gname = f"グループ{g['group_no']}" if g else f"グループID {prev}"
            warnings.append({
                "code": "already_applied_to_other",
                "message": f"この計測結果は既に{gname}へ反映済みです。"
                           f"同じ結果を二重に使うことになります。",
            })
        if warnings:
            return JSONResponse(
                {"ok": False, "reason": "warning", "warnings": warnings},
                status_code=409,
            )

    ranking = _ranking_payload(result)
    res = await bridge_svc.apply_race_to_bracket_group(
        db, race_id=race["id"], group_id=group_id, ranking=ranking
    )
    if res.get("error"):
        raise HTTPException(status_code=400,
                            detail="このグループの出走枠が見つかりません")

    # 反映＝確定。アクティブを次のレースへ自動移動（手動／自動反映で統一）。失敗は無視。
    from app.infrastructure.db.repositories import gw_active_repo as _gwa
    await _gwa.advance_after_confirm(db, _gwa.KIND_BRACKET_GROUP, group_id)

    # --- 手動で勝者を決めたときと同じ後処理を行う ---
    # ⚠ これを呼ばないと勝者が次ラウンドへ進まず、トーナメントが停止する。
    #    bracket.py の bracket_save と同じ流れを踏襲する。
    advanced = False
    try:
        from app.presentation.routers import bracket as bracket_mod

        async with db.execute(
            "SELECT br.id AS round_id, br.round_no, br.tournament_id "
            "FROM bracket_groups bg JOIN bracket_rounds br ON br.id = bg.round_id "
            "WHERE bg.id = ?",
            (group_id,),
        ) as cur:
            rnd = await cur.fetchone()

        if rnd:
            tid = rnd["tournament_id"]
            # 勝者を次ラウンドの対応スロットへ仮反映
            await bracket_mod._prefill_next_round(
                tid, rnd["round_id"], rnd["round_no"], db
            )
            # 全グループ完了なら次ラウンドを生成
            advanced = await bracket_mod._try_advance_round(tid, rnd["round_id"], db)
            # 準決勝の勝者が変わったら 3位決定戦・敗者復活戦の参加者を作り直す
            await bracket_mod._sync_third_revival(tid, db)
            # 裏トーナメント（有効時のみ）
            await bracket_mod._sync_losers_bracket(tid, db)
            await bracket_mod._try_insert_reviver(tid, db)

            # 参加者向けHTMLの再配信
            try:
                from app.services.publish_scheduler import schedule_publish
                schedule_publish()
            except Exception:
                pass
    except Exception as e:
        # 後処理に失敗しても順位保存は済んでいるので、結果は返す
        return JSONResponse({"ok": True, "race_id": race["id"],
                             "advance_error": str(e), **res})

    return JSONResponse({"ok": True, "race_id": race["id"],
                         "advanced": bool(advanced), **res})


async def _ht_group_round(db, group_id: int):
    """ht グループ → 所属ラウンド情報（tid/heat_no/round_*）を返す。無ければ None。"""
    async with db.execute(
        """SELECT hr.tournament_id AS tid, hr.heat_no AS heat_no,
                  hr.round_type, hr.round_no, hr.id AS round_id,
                  COALESCE(hr.section_no, 1) AS section_no
           FROM ht_groups hg JOIN ht_rounds hr ON hr.id = hg.round_id
           WHERE hg.id = ?""",
        (group_id,),
    ) as cur:
        return await cur.fetchone()


@router.post("/api/timing/apply/ht-group/{group_id}")
async def apply_latest_to_ht_group(
    group_id: int,
    race_id: int | None = None,
    force: bool = False,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """最新の計測結果をヒートトーナメントのグループへ反映する（slot_no ↔ レーン番号で照合）。

    決勝ブラケットの反映（apply_latest_to_bracket）と同じ流儀。ヒートトーナメント専用の
    後処理（進出・次ラウンド生成・ロック）は qualifying._ht_finalize_group を共用する。
    """
    from app.presentation.routers import qualifying as qual_mod

    rnd = await _ht_group_round(db, group_id)
    if rnd is None:
        raise HTTPException(status_code=404, detail="グループが見つかりません")
    tid, heat_no = rnd["tid"], rnd["heat_no"]

    # ヒートがロック（結果確定）中は反映不可
    if await qual_mod._is_heat_locked(tid, heat_no, db):
        return JSONResponse(
            {"ok": False, "reason": "locked",
             "message": "このヒートは結果確定済み（ロック中）です。取消してから反映してください。"},
            status_code=409,
        )

    repo = TimingRaceRepository(db)
    race, result = await _pick_race(db, repo, race_id)
    if result is None:
        raise HTTPException(status_code=404, detail="反映できる計測結果がありません")

    if not force:
        warnings = []
        async with db.execute(
            "SELECT 1 FROM ht_results WHERE group_id = ?", (group_id,)
        ) as cur:
            if await cur.fetchone():
                warnings.append({
                    "code": "already_has_result",
                    "message": "この組には既に結果が入力されています。上書きされます。",
                })
        # この計測結果を既に別のヒート組へ反映していないか
        keys = race.keys() if hasattr(race, "keys") else []
        prev = race["applied_ht_group_id"] if "applied_ht_group_id" in keys else None
        if prev is not None and int(prev) != int(group_id):
            async with db.execute(
                "SELECT group_no FROM ht_groups WHERE id = ?", (prev,)
            ) as cur:
                pg = await cur.fetchone()
            gname = f"グループ{pg['group_no']}" if pg else f"組ID {prev}"
            warnings.append({
                "code": "already_applied_to_other",
                "message": f"この計測結果は既に{gname}へ反映済みです。"
                           f"同じ結果を二重に使うことになります。",
            })
        if warnings:
            return JSONResponse(
                {"ok": False, "reason": "warning", "warnings": warnings},
                status_code=409,
            )

    ranking = _ranking_payload(result)
    try:
        res = await bridge_svc.apply_race_to_ht_group(
            db, race_id=race["id"], group_id=group_id, ranking=ranking
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"反映処理でエラー: {e}")
    if res.get("error"):
        raise HTTPException(status_code=400,
                            detail="このグループの出走枠が見つかりません")

    # 手動保存と同じ後処理（進出・次ラウンド生成・ロック判定）
    try:
        fin = await qual_mod._ht_finalize_group(
            tid, heat_no, group_id, dict(rnd), res.get("winner_slot_id"), db
        )
    except Exception as e:
        return JSONResponse({"ok": True, "race_id": race["id"],
                             "saved": res.get("saved", 0), "advance_error": str(e)})

    # 反映＝確定。アクティブを次のレースへ自動移動（手動／自動反映で統一）。失敗は無視。
    from app.infrastructure.db.repositories import gw_active_repo as _gwa
    await _gwa.advance_after_confirm(db, _gwa.KIND_HT_GROUP, group_id)

    # 参加者向けHTMLの再配信（bracket 反映と同じ）
    try:
        from app.services.publish_scheduler import schedule_publish
        schedule_publish()
    except Exception:
        pass

    return JSONResponse({"race_id": race["id"], "saved": res.get("saved", 0), **fin})


@router.post("/api/timing/apply/ht-group/{group_id}/reset")
async def reset_ht_group(
    group_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """ヒートトーナメントのグループ結果を取り消す（進出も再計算する）。"""
    from app.presentation.routers import qualifying as qual_mod

    rnd = await _ht_group_round(db, group_id)
    if rnd is None:
        raise HTTPException(status_code=404, detail="グループが見つかりません")
    tid, heat_no = rnd["tid"], rnd["heat_no"]

    if await qual_mod._is_heat_locked(tid, heat_no, db):
        return JSONResponse(
            {"ok": False, "reason": "locked",
             "message": "このヒートは結果確定済み（ロック中）です。"},
            status_code=409,
        )

    await db.execute("DELETE FROM ht_results WHERE group_id = ?", (group_id,))
    await db.execute("DELETE FROM ht_slot_ranks WHERE group_id = ?", (group_id,))
    # この組に紐づいていた計測レースの反映先リンクを解除（PIPの「反映済」を消す）。
    # カラム未追加の旧DBでも取消自体は成立させる。
    try:
        await db.execute(
            "UPDATE timing_races SET applied_ht_group_id = NULL WHERE applied_ht_group_id = ?",
            (group_id,),
        )
    except Exception as e:
        print(f"[timing] ht reset: unlink races failed group={group_id}: {type(e).__name__}: {e}", flush=True)
    await db.commit()

    fin = await qual_mod._ht_finalize_group(tid, heat_no, group_id, dict(rnd), None, db)
    try:
        from app.services.publish_scheduler import schedule_publish
        schedule_publish()
    except Exception:
        pass
    return JSONResponse(fin)


@router.post("/api/timing/apply/ow-heat/{heat_id}")
async def apply_latest_to_ow_heat(
    heat_id: int,
    race_id: int | None = None,
    force: bool = False,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """最新（またはPIP選択）の計測結果を「並び順（勝ち抜け）」の組へ反映する。

    勝ち抜けは1着だけが勝ち上がるので、計測の1位（完走した最速）を1着として自動入力する。
    全員CO（完走なし）のときは「該当者なし」で確定する。
    突き合わせは slot(lane_no) ↔ start_lane。順位確定・通過/敗退判定は
    qualifying._ow_apply_win を共用（手動入力と同一ロジック）。
    """
    from app.presentation.routers import qualifying as qual_mod

    # 対象の組（heats）と大会種別
    async with db.execute(
        """SELECT h.id, h.tournament_id, h.round_no AS stage_no, h.status,
                  t.qualifying_type
             FROM heats h JOIN tournaments t ON t.id = h.tournament_id
            WHERE h.id = ?""",
        (heat_id,),
    ) as cur:
        heat = await cur.fetchone()
    if heat is None:
        raise HTTPException(status_code=404, detail="組が見つかりません")
    if heat["qualifying_type"] != "order_winner":
        raise HTTPException(status_code=400, detail="勝ち抜け以外のレースです")
    tid = heat["tournament_id"]

    # 既に確定済みなら上書き確認（決勝/予選と同じ 409 流儀）
    if not force and heat["status"] == "done":
        return JSONResponse(
            {"ok": False, "reason": "warning",
             "warnings": [{"code": "already_has_result",
                           "message": "この組には既に結果が入力されています。上書きされます。"}]},
            status_code=409,
        )

    repo = TimingRaceRepository(db)
    race, result = await _pick_race(db, repo, race_id)
    if result is None:
        raise HTTPException(status_code=404, detail="反映できる計測結果がありません")

    # 既に別の組へ反映済みの計測結果か（重複反映の警告）
    if not force:
        keys = race.keys() if hasattr(race, "keys") else []
        prev = race["heat_id"] if "heat_id" in keys else None
        if prev is not None and int(prev) != int(heat_id):
            return JSONResponse(
                {"ok": False, "reason": "warning",
                 "warnings": [{"code": "already_applied_to_other",
                               "message": "この計測結果は既に別の組へ反映済みです。"
                                          "同じ結果を二重に使うことになります。"}]},
                status_code=409,
            )

    # 組のレーン（lane_no ↔ start_lane で突き合わせ）
    async with db.execute(
        "SELECT hl.id AS lane_id, hl.lane_no, hl.entry_id "
        "FROM heat_lanes hl WHERE hl.heat_id=? ORDER BY hl.lane_no",
        (heat_id,),
    ) as cur:
        lanes = [dict(r) for r in await cur.fetchall()]
    if not lanes:
        raise HTTPException(status_code=400, detail="組にレーサーがいません")

    ranking = _ranking_payload(result)
    m = bridge_svc.match_ranking_to_lanes(ranking, lanes)
    matched = m["matched"]
    if not matched:
        raise HTTPException(status_code=400,
                            detail="計測レーンと組のレーンが一致しません")

    # 1着＝完走した最速（rank最小で dnf でない）。全員COなら該当者なし（win_eid=None）。
    win_eid = None
    for x in sorted(matched, key=lambda mm: int(mm["rank"])):
        if not x.get("dnf"):
            win_eid = x["entry_id"]
            break
    # レーンごとの計測タイム・着順（order と同じ「1位/2位/3位・CO・FINISH」表示に使う）
    lane_times = {x["lane_id"]: x.get("total_time") for x in matched}
    lane_ranks = {x["lane_id"]: {"rank": x.get("rank"), "is_co": bool(x.get("dnf"))}
                  for x in matched}

    res = await qual_mod._ow_apply_win(tid, heat_id, win_eid, db,
                                       lane_times=lane_times, lane_ranks=lane_ranks)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message") or "反映できませんでした")

    # どのレースを反映したか記録（PIPの「反映済」表示・重複反映の検出に使う）
    try:
        await db.execute("UPDATE timing_races SET heat_id=? WHERE id=?", (heat_id, race["id"]))
        await db.commit()
    except Exception:
        pass

    # 反映＝確定。アクティブを次のレースへ自動移動（手動／自動反映で統一）。失敗は無視。
    from app.infrastructure.db.repositories import gw_active_repo as _gwa
    await _gwa.advance_after_confirm(db, _gwa.KIND_HEAT, heat_id)

    res["race_id"] = race["id"]
    res["unmatched_lanes"] = m.get("unmatched_lanes", [])
    res["unmatched_records"] = m.get("unmatched_records", [])
    return JSONResponse(res)


async def _pick_race(db, repo, race_id: int | None):
    """反映元のレースを決める。race_id 未指定なら記録のある最新レース。"""
    if race_id is not None:
        race = await repo.get_race(race_id)
        if race is None:
            return None, None
        _r, result = await build_race_result(db, race_id)
        return race, result

    for r in await repo.list_races(limit=20):
        try:
            race, result = await build_race_result(db, r["id"])
        except Exception as e:
            print(f"[timing] pick: build failed race={r['id']}: {type(e).__name__}: {e}", flush=True)
            continue
        if result is not None and result.ranking():
            return race, result
    return None, None


def _ranking_payload(result) -> list[dict]:
    """RaceResult を橋渡しサービスが期待する形（順位つきリスト）に変換する。"""
    rows = []
    for pos, m in enumerate(result.ranking(), start=1):
        # ★フライング（jump_start）は CO 扱い＝タイムも順位もなし（20261110 ユーザー確定）。
        co = bool(m.dnf) or bool(m.jump_start)
        rows.append({
            "pos": pos,
            "start_lane": m.start_lane,
            "total_s": None if co else (round(m.total_time_us / 1e6, 3) if m.total_time_us else None),
            "best_s": None if co else (round(m.best_lap_us / 1e6, 3) if m.best_lap_us else None),
            "completed_laps": m.completed_laps,
            "jump_start": m.jump_start,   # G1(24.53)：フライング=JS表示用
            "missing": m.missing,         # E1(24.37)：欠測あり=⚠要確認
            "dnf": co,                    # フライングもCO(DNF)として反映＝順位なし
        })
    return rows


@router.get("/api/timing/pip/latest")
async def pip_latest(
    limit: int = 5,
    mode: str = "race",
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """PIP（右下小窓）用：最近の計測レースを新しい順に、順位つきで返す。

    ⚠ クラウド版かつライセンス登録済みの環境でのみ利用可（require_m4laps）。
       オンプレ版・未登録環境では 404 を返し、機能自体を隠す。

    GWから送られてきた記録をそのまま見せるだけ。まだ誰のものかは紐づけない。
    （組み合わせ情報はGWへ送らない方針のため、突き合わせはアプリ側で後から行う）

    mode: "race"（既定・緑ランプありのレースのみ）/ "free"（走行式のみ）/ "all"
      PIPは進行中のレースを見るための小窓なので、既定ではフリー走行を出さない。
      練習の記録が混ざると、どれが今のヒートの結果か分からなくなるため。
    """
    repo = TimingRaceRepository(db)
    want = max(1, min(limit, 20))
    mode = mode if mode in ("race", "free", "all") else "race"
    # 記録なし（レイアウト未設定・組み立て不能）やモード違いのレースは表示しないため、
    # 多めに取得してから絞り込む。古い壊れたデータがPIPを埋めるのを防ぐ。
    races = await repo.list_races(limit=min(want * 10 + 20, 200))
    out = []
    for r in races:
        rid = r["id"]
        # レース(F1式)は green_t_us あり、フリー(走行式)は None で見分ける
        is_race = r["green_t_us"] is not None
        if (mode == "race" and not is_race) or (mode == "free" and is_race):
            continue
        try:
            race, result = await build_race_result(db, rid)
        except Exception as e:
            print(f"[timing] pip: build failed race={rid}: {type(e).__name__}: {e}", flush=True)
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
                    "dnf": m.dnf,                       # E5(24.39)：CO=DNF表示
                })
        # 記録が組み立てられなかったレースは出さない（「計測中／記録なし」を除外）
        if not rows:
            continue

        keys = race.keys() if hasattr(race, "keys") else []
        heat_id = race["heat_id"] if "heat_id" in keys else None
        group_id = race["applied_group_id"] if "applied_group_id" in keys else None
        ht_group_id = race["applied_ht_group_id"] if "applied_ht_group_id" in keys else None

        # 反映先を人間向けラベルにする（内部IDを画面に出さない）。
        # 予選＝heat_id、決勝＝applied_group_id、予選ヒートトーナメント＝applied_ht_group_id。
        # いずれも無ければ未反映（None）。
        applied_label = None
        if heat_id is not None:
            async with db.execute(
                "SELECT h.heat_no, h.round_no, t.qualifying_type "
                "FROM heats h JOIN tournaments t ON t.id = h.tournament_id WHERE h.id = ?",
                (heat_id,),
            ) as cur:
                h = await cur.fetchone()
            if h and h["qualifying_type"] == "order_winner":
                # 勝ち抜け：heats.round_no に段階(1次/2次…)を格納している
                applied_label = f"勝ち抜け {h['round_no']}次 レース{h['heat_no']}"
            elif h and h["round_no"]:
                applied_label = f"予選{h['round_no']}回目 レース{h['heat_no']}"
            elif h:
                applied_label = f"レース{h['heat_no']}"
            else:
                applied_label = f"ヒート{heat_id}"   # 保険：ヒートが消えている等
        elif ht_group_id is not None:
            # 予選ヒートトーナメント：ヒート番号＋ラウンド（準々決勝/準決勝/決勝）＋グループ
            async with db.execute(
                """SELECT hg.group_no, hr.round_no, hr.round_type, hr.heat_no,
                          COALESCE(hr.section_no, 1) AS section_no, hr.tournament_id,
                          (SELECT COUNT(*) FROM ht_groups hg2 WHERE hg2.round_id = hr.id) AS grp_count
                     FROM ht_groups hg
                     JOIN ht_rounds hr ON hr.id = hg.round_id
                    WHERE hg.id = ?""",
                (ht_group_id,),
            ) as cur:
                g = await cur.fetchone()
            if g:
                async with db.execute(
                    "SELECT MAX(round_no) AS mx FROM ht_rounds "
                    "WHERE tournament_id = ? AND heat_no = ? AND COALESCE(section_no,1) = ? "
                    "AND round_type IN ('normal','final')",
                    (g["tournament_id"], g["heat_no"], g["section_no"]),
                ) as cur2:
                    _mx = await cur2.fetchone()
                total_rounds = (_mx["mx"] or 0) if _mx else 0
                from app.presentation.routers.bracket import round_label
                _rl = round_label(g["round_no"], total_rounds, g["round_type"])
                _grp = f" グループ{g['group_no']}" if (g["grp_count"] or 1) > 1 else ""
                if g["section_no"] == 0:
                    applied_label = f"ヒート{g['heat_no']} ヒート決勝 {_rl}{_grp}".strip()
                else:
                    applied_label = f"ヒート{g['heat_no']} {_rl}{_grp}"
            else:
                applied_label = f"ヒート組{ht_group_id}"
        elif group_id is not None:
            # ラウンド種別（決勝／準決勝／3位決定戦／敗者復活戦／裏R…）を反映してラベル化する。
            async with db.execute(
                """SELECT bg.group_no, br.round_no, br.round_type, br.tournament_id,
                          (SELECT COUNT(*) FROM bracket_groups bg2 WHERE bg2.round_id = br.id) AS grp_count
                     FROM bracket_groups bg
                     JOIN bracket_rounds br ON br.id = bg.round_id
                    WHERE bg.id = ?""",
                (group_id,),
            ) as cur:
                g = await cur.fetchone()
            if g:
                # normal ラウンドの「準決勝/準々決勝」判定は決勝までの距離で決まるため、
                # 表ラウンド（normal/final）の最大 round_no を総ラウンド数として渡す。
                async with db.execute(
                    "SELECT MAX(round_no) AS mx FROM bracket_rounds "
                    "WHERE tournament_id = ? AND round_type IN ('normal','final')",
                    (g["tournament_id"],),
                ) as cur2:
                    _mx = await cur2.fetchone()
                total_rounds = (_mx["mx"] or 0) if _mx else 0
                from app.presentation.routers.bracket import round_label
                _rl = round_label(g["round_no"], total_rounds, g["round_type"])
                # 複数グループあるラウンドだけ「グループN」を付ける（決勝など1組は付けない）。
                applied_label = (f"{_rl} グループ{g['group_no']}"
                                 if (g["grp_count"] or 1) > 1 else _rl)
            else:
                applied_label = f"グループ{group_id}"
        else:
            # タイムアタック予選：reflect は timing_races（heat_id/applied_*）ではなく
            # time_attack_runs.race_id に記録される。この計測レースがどのレーサーの
            # 何回目の走行へ使われたかを引いてラベル化する（PIPに「反映済」を出すため）。
            async with db.execute(
                """SELECT tar.run_no, r.name
                     FROM time_attack_runs tar
                     JOIN entries e ON e.id = tar.entry_id
                     JOIN racers  r ON r.id = e.racer_id
                    WHERE tar.race_id = ?
                    ORDER BY tar.run_no
                    LIMIT 1""",
                (rid,),
            ) as cur:
                ta = await cur.fetchone()
            if ta:
                applied_label = f"TA {ta['name']} {ta['run_no']}回目"

        out.append({
            "race_id": rid,
            "heat_id": heat_id,
            "applied_group_id": group_id,
            "applied_ht_group_id": ht_group_id,
            "applied_label": applied_label,
            "target_laps": (race["target_laps"] if "target_laps" in keys else None),
            "created_at": (race["created_at"] if "created_at" in keys else None),
            "ranking": rows,
        })
        if len(out) >= want:
            break
    return JSONResponse({"races": out})


def _best_rank(value, tops, tol: float = 1e-6) -> int:
    """その日の上位3傑の何位かを返す（1〜3。該当なしは0）。

    tops は [1位の値, 2位の値, 3位の値]（timing_bests 由来・秒/m·s）。
    µs のまま比較しないこと。浮動小数の誤差は許容する。
    """
    if value is None or not tops:
        return 0
    for i, t in enumerate(tops, start=1):
        if t is not None and abs(value - t) < tol:
            return i
    return 0


def _format_sample_note_lines(note: str | None) -> list[str] | None:
    """一覧セル用に sample_note を1レーン=1行へ整形する（モーダルは生文字列のまま）。

    入力例:  "完走1／CO2 ｜ L1:CO(2周目：SQ0-SQ1) L2:完走 L3:CO(1周目：GW-SQ0)"
    出力例:  ["L1：CO　[2周目SQ0-SQ1]", "L2：完走", "L3：CO　[1周目GW-SQ0]"]

    - 先頭のサマリ（"完走1／CO2 ｜ "）は落とす。
    - 各レーンは "L{n}：{種別}"。区間があれば全角スペース＋"[…]"で続ける。
      区間内の全角コロン "：" は詰める（"2周目：SQ0-SQ1" → "2周目SQ0-SQ1"）。
    """
    if not note:
        return None
    body = note.split(" ｜ ", 1)[1] if " ｜ " in note else note
    lines: list[str] = []
    for token in body.split(" "):
        if ":" not in token:
            continue
        label, rest = token.split(":", 1)          # "L1", "CO(2周目：SQ0-SQ1)"
        if "(" in rest and rest.endswith(")"):
            kind, inner = rest[:-1].split("(", 1)   # "CO", "2周目：SQ0-SQ1"
            inner = inner.replace("：", "")
            lines.append(f"{label}：{kind}　[{inner}]")
        else:
            lines.append(f"{label}：{rest}")
    return lines or None


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


async def _speed_fns_for_races(db, race_ids: list[int]):
    """複数レース分の速度をまとめて読み、(speed_fn, lap_avg_fn, total_avg_fn) を返す。

    ⚠ 反映時に保存済みの timing_race_speeds を読む（都度計算しない）。
       これで明細行・当日ベスト・ベスト集計がすべて同じ保存値を使う。
       speed_fn は同期関数のため、先に全レース分を辞書へ展開しておく。
    """
    speed_map: dict = {}      # (race_id, lane, lap, sector_idx0) -> m/s
    lapavg_map: dict = {}     # (race_id, lane, lap) -> m/s
    totavg_map: dict = {}     # (race_id, lane) -> m/s（= LAP Av. の平均）

    for rid in race_ids:
        st = await speed_store.load_speeds(db, rid)
        for lane, mp in st["lane"].items():
            if "total_avg" in mp:
                totavg_map[(rid, lane)] = mp["total_avg"]
        for (lane, lap), v in st["lap"].items():
            lapavg_map[(rid, lane, lap)] = v
        for (lane, lap, sno), v in st["sec"].items():
            # 保存は sector_no=1..7（0=S/G）。collect 側は 0基点 idx で引くので -1 する。
            speed_map[(rid, lane, lap, sno - 1)] = v

    def speed_fn(rid, lane, sector_idx, lap=1):
        return speed_map.get((rid, lane, lap, sector_idx))

    def lap_avg_fn(rid, lane, lap):
        return lapavg_map.get((rid, lane, lap))

    def total_avg_fn(rid, lane, _total_us=None, _laps=None):
        return totavg_map.get((rid, lane))

    return speed_fn, lap_avg_fn, total_avg_fn


async def _speed_fns_for_race(db, race_id: int):
    """レースIDから (speed_fn, lap_avg_fn, total_avg_fn) を作る（複数レース版の特例・★C6で一本化）。

    旧実装は sector 番号の 0基点/1基点変換を複数レース版と逆向きに別実装していた
    （`sno-1` と `idx+1`）。等価だが2箇所に暗黙の契約があったので、複数版に委譲する。
    """
    return await _speed_fns_for_races(db, [race_id])


@router.get("/api/timing/races/{race_id}/chart")
async def race_position_chart(
    race_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    _guard: bool = Depends(require_m4laps),
):
    """ポジションチャート（順位変動グラフ）用の JSON を返す。

    横軸=通過ゲートの通し番号、縦軸=その通過時点での順位（同じ通過数の
    累積タイム順）。CO車は完了周までで線が止まる（timing_chart_service）。
    """
    race, result = await build_race_result(db, race_id)
    if race is None:
        raise HTTPException(status_code=404, detail="race not found")
    if result is None:
        return JSONResponse({"target_laps": 0, "x_axis": [], "lanes": [],
                             "lap_colors": []})

    # レイアウト要素（ゲート順・種別ラベル用）を取得
    from app.domain.rotation import LayoutElement
    lrepo = TimingLayoutRepository(db)
    repo = TimingRaceRepository(db)
    elem_rows = await lrepo.get_elements(race["layout_id"])
    layout_elems = [LayoutElement(kind=e["kind"], node_id=e["node_id"])
                    for e in elem_rows]

    chart = build_position_chart(race, result, layout_elems)

    # モーダルに「どのパターンで生成したか」を出す（サンプルのみ。実データはNULL）。
    sample_note = None
    try:
        async with db.execute(
            "SELECT sample_note FROM timing_races WHERE id = ?", (race_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            sample_note = row["sample_note"] if hasattr(row, "keys") else row[0]
    except Exception:
        sample_note = None
    chart["sample_note"] = sample_note

    # 生データ（物理レーンごとの全検出）。乗入・欠落を人間が確認するための素材。
    try:
        ev_rows = await repo.get_events(race_id)
        chart["raw"] = build_raw_detections(race, ev_rows, layout_elems, n_lanes=LANES)
        chart["sensors"] = build_sensor_matrix(race, ev_rows, layout_elems, n_lanes=LANES)
    except Exception as e:
        print(f"[timing] raw build failed race={race_id}: {type(e).__name__}: {e}", flush=True)
        chart["raw"] = None
        chart["sensors"] = None

    return JSONResponse(chart)


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
                "dnf": m.dnf,               # E5(24.39)：CO=DNF表示
                "jump_start": m.jump_start, # G1(24.53)：フライング=JS表示
                "missing": m.missing,       # E1(24.37)：欠測あり=⚠要確認
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
                "dnf": m.dnf,   # E5(24.39)：CO=DNF表示
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

@router.get("/api/timing/layouts/{layout_id}/for_gw")
async def layout_for_gw(
    layout_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    x_timing_token: str | None = Header(default=None),
    _guard: bool = Depends(require_m4laps),
):
    """GW向けレイアウト軽量版（docs/19.16）。周回数・使用ノード・LC数を返す。"""
    _check_token(x_timing_token)
    repo = TimingLayoutRepository(db)
    lay = await repo.get_layout(layout_id)
    if not lay:
        return JSONResponse({"detail": "layout not found"}, status_code=404)
    elems = await repo.get_elements(layout_id)
    # ★20261031 C12：位置インデックス（e[2]/lay[0]…）→ 列名参照。SELECTの列順変更でGW向けAPIが黙って壊れない。
    nodes = [
        {"node_id": e["node_id"], "kind": e["kind"]}
        for e in elems if e["kind"] != "LC" and e["node_id"] is not None
    ]
    lc_count = sum(1 for e in elems if e["kind"] == "LC")
    return JSONResponse({
        "layout_id": lay["id"],
        "name": lay["name"],
        "target_laps": lay["target_laps"],
        "lap_length_m": lay["lap_length_m"],
        "lc_count": lc_count,
        "nodes": nodes,
        "node_count": len(nodes),
        "updated_at": lay["updated_at"],
    })


# ── GW向け：現在アクティブなレースのレーン別レーサー名（ロスターREADY用・20261109）──
#  admin が予選/決勝管理で #（レース番号）やグループをクリックして設定したアクティブレースを返す。
#  kana はサーバーで正規化済み（ひらがな→カタカナ／半角英数）。アクティブ無し→ lanes:[]。
@router.get("/api/timing/current_race_for_gw")
async def current_race_for_gw(
    db: aiosqlite.Connection = Depends(get_db),
    x_timing_token: str | None = Header(default=None),
):
    # ★require_m4laps は外す（オンプレ=IS_CLOUD偽でもGWが名前を取れるように）。認証はトークンで担保。
    _check_token(x_timing_token)
    from app.infrastructure.db.repositories import gw_active_repo
    data = await gw_active_repo.resolve_lanes(db)
    return JSONResponse({"active": data["active"], "lanes": data["lanes"]})


# ── admin PIP用：現在アクティブなレースの完全情報（ラベル＋レーン別名）。20261109 ──
#  pip_latest と同じく require_m4laps のみ（トークン不要＝admin画面のJSから読める）。
@router.get("/api/timing/active-race")
async def active_race_read(
    db: aiosqlite.Connection = Depends(get_db),
):
    # ★require_m4laps は外す（オンプレ/クラウド両対応。中身はレーサー名のみで公開viewと同等）。
    from app.infrastructure.db.repositories import gw_active_repo
    return JSONResponse(await gw_active_repo.resolve_lanes(db))


# ── 自動反映の「上書き確認」保留（admin PIPのダイアログ用）。20261110 ──
@router.get("/api/timing/auto-apply/pending")
async def auto_apply_pending(
    db: aiosqlite.Connection = Depends(get_db),
):
    """自動反映が『反映先に既に結果あり』で保留になっている場合、その内容を返す。
       admin画面のPIPがポーリングし、上書き確認ダイアログを出す。無ければ pending:false。"""
    from app.infrastructure.db.repositories import gw_active_repo
    p = await gw_active_repo.get_auto_pending(db)
    if not p:
        return JSONResponse({"pending": False})
    return JSONResponse({"pending": True, **p})


@router.post("/api/timing/auto-apply/dismiss")
async def auto_apply_dismiss(
    db: aiosqlite.Connection = Depends(get_db),
):
    """保留中の自動反映ダイアログを閉じる（キャンセル／上書き実行後）。同じ計測は再通知しない。"""
    from app.infrastructure.db.repositories import gw_active_repo
    await gw_active_repo.dismiss_auto_pending(db)
    return JSONResponse({"ok": True})


def rows_for_race(race, result, stored: dict, bst: dict, *, list_row=None) -> list[dict]:
    """1レース分の明細行（1行=1レーンの1周）。★20261031 C3：results_page と TA view の唯一の実装。

    race     : timing_races 行（created_at / heat_id を参照）
    result   : RaceResult
    stored   : speed_store.load_speeds() の戻り（反映時に保存済みの速度）
    bst      : その日のベスト {metric: [1位,2位,3位]}（ハイライト順位）
    list_row : 一覧の行（sample_note / status を持つ）。無ければ race から読む。
    """
    src = list_row if list_row is not None else race
    _keys = src.keys() if hasattr(src, "keys") else []
    sample_note = src["sample_note"] if "sample_note" in _keys else None
    status = src["status"] if "status" in _keys else "confirmed"
    _rkeys = race.keys() if hasattr(race, "keys") else []
    created_at = race["created_at"] if "created_at" in _rkeys else None
    heat_id = race["heat_id"] if "heat_id" in _rkeys else None
    rid = race["id"] if "id" in _rkeys else None

    def _pass_speed(lane, lap_no, sector_idx):
        # sector_idx は 0=S/G通過, 1..7=各区間（保存キーと同じ）
        return stored["sec"].get((lane, lap_no, sector_idx))

    # 順位（POS）は合計タイム昇順で決める。表示は必ずレーン番号順（現場で見比べやすい）。
    ranked = result.ranking()
    pos_map = {m.start_lane: i for i, m in enumerate(ranked, start=1)}
    ordered = sorted(ranked, key=lambda m: m.start_lane)
    top_us = None
    for m in ordered:
        if m.total_time_us is not None:
            top_us = m.total_time_us
            break
    # このレースの総行数（＝各マシンの周回数の合計）。時刻セルの結合に使う。
    race_row_count = sum(max(1, len(m.laps)) for m in ordered)
    race_row_index = 0
    rows: list[dict] = []
    for m in ordered:
        pos = pos_map.get(m.start_lane)
        gap = None
        if m.total_time_us is not None and top_us is not None:
            gap = round((m.total_time_us - top_us) / 1e6, 3)
        max_ms = stored["lane"].get(m.start_lane, {}).get("max_ms")         # MAX SPEED
        total_avg = stored["lane"].get(m.start_lane, {}).get("total_avg")   # TOTAL Av.
        lap_count = max(1, len(m.laps))
        for li, lap in enumerate(m.laps or [None]):
            # --- SECTOR 0〜7：0 = S/G通過（その周の起点）／1〜7 = 各区間 ---
            sectors = [None] * 8
            if lap is not None:
                sg_ms = (None if lap.lap == 1 else _pass_speed(m.start_lane, lap.lap, 0))   # 1周目は速度なし
                sectors[0] = {
                    "s": None, "ms": sg_ms, "sg": True,
                    "rank_ms": _best_rank(sg_ms, bst.get("max_ms")),   # S/Gは全区間の最高速(max_ms)と比べる
                    "rank_s": 0,
                }
                for idx, sec in enumerate(lap.sectors):
                    if idx + 1 > 7:
                        break
                    sp = _pass_speed(m.start_lane, lap.lap, idx + 1)
                    sectors[idx + 1] = {
                        "s": round(sec.dt_us / 1e6, 3), "ms": sp, "sg": False,
                        # その日のベストか（タイムは最小・速度は最大）。区間ごとに独立して判定。
                        "rank_s": _best_rank(sec.dt_us / 1e6, bst.get(best_svc.sector_metric(idx + 1))),
                        "rank_ms": _best_rank(sp, bst.get(best_svc.sector_speed_metric(idx + 1))),
                    }
            lap_avg = (stored["lap"].get((m.start_lane, lap.lap)) if lap is not None else None)
            rows.append({
                # --- その日のベスト判定（ハイライト用）：上位3傑の何位か（0=該当なし） ---
                "rank_total": _best_rank(m.total_time_us / 1e6 if m.total_time_us else None, bst.get("total")),
                "rank_max": _best_rank(max_ms, bst.get("max_ms")),
                "rank_total_avg": _best_rank(total_avg, bst.get("total_avg")),
                "rank_lap": _best_rank(lap.lap_time_us / 1e6 if lap is not None else None, bst.get("lap")),
                "rank_lap_avg": _best_rank(lap_avg, bst.get("lap_avg")),
                "race_id": rid,
                "created_at": created_at,
                "date_part": _split_ts(created_at)[0],
                "time_part": _split_ts(created_at)[1],
                "heat_id": heat_id,
                "mode": result.mode,               # 'f1'=レース / 'run'=フリー
                "sample_note": sample_note,
                "sample_note_lines": _format_sample_note_lines(sample_note),
                "status": status,
                "jump_start": m.jump_start,         # G1(24.53)：フライング=JS表示
                "missing": m.missing,               # E1(24.37)：欠測あり=⚠要確認表示
                "dnf": m.dnf,                       # E5(24.39)：CO=DNF表示
                "pos": pos,
                "start_lane": m.start_lane,
                "total_s": round(m.total_time_us / 1e6, 3) if m.total_time_us else None,
                "max_ms": max_ms,
                "total_avg_ms": total_avg,
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
    return rows


async def build_result_rows(db, race_id: int):
    """1レース分の明細行を /admin/timing/results と完全に同じ構造で組み立てる。
       TA view（/view/ta）が本物テーブルを再現するために使う。
       戻り: {race, mode, has_result, rows}（rows は results_page と同一スキーマ）。"""
    repo = TimingRaceRepository(db)
    try:
        race, result = await build_race_result(db, race_id)
    except Exception:
        return {"race": None, "mode": None, "has_result": False, "rows": []}
    if race is None or result is None:
        return {"race": race, "mode": None, "has_result": False, "rows": []}

    # 速度（反映時に計算・保存済み）を読む
    stored = await speed_store.load_speeds(db, race_id)

    # その日のベスト（ハイライト判定）
    dkey = (race["created_at"] or "")[:10]
    stored_bests = await best_svc.load_bests(db, "day", dkey) if dkey else {}
    bst = {k: [x["value"] for x in v] for k, v in stored_bests.items()}
    # ★20261031 C3：results_page と同一実装（rows_for_race）。従来は約110行のコピーで、
    #   既に race_id/日時/mode/sample_note/status の列差分が生じていた（本関数側に無かった）。
    #   一本化により TA view 側にもそれらのキーが付く（追加のみ・既存キーは不変）。
    rows = rows_for_race(race, result, stored, bst, list_row=race)
    return {"race": race, "mode": result.mode, "has_result": True, "rows": rows}


async def today_best_map_for(db):
    """当日（09:00〜翌08:59）のベスト集計。results_page と TA view の両方がこれを使う（★C3 一本化）。

    深夜まで走る運用があるため、カレンダーの日付ではなく09:00区切りで集計する。
    保持済みの timing_bests はカレンダー日付キーなのでここでは使わず、その場で計算する。
    """
    repo = TimingRaceRepository(db)
    ts_from, ts_to, today_label = best_svc.business_day_window()
    today_bests = []
    today_race_count = 0
    try:
        today_races = await repo.list_races_between_ts(ts_from, ts_to)
        if today_races:
            t_sf, t_lf, t_tf = await _speed_fns_for_races(db, [r["id"] for r in today_races])
            agg = await best_svc.aggregate_range(
                db, date_from=ts_from, date_to=ts_to, mode=None,
                list_fn=lambda a, b: repo.list_races_between_ts(a, b),
                build_fn=build_race_result, speed_fn=t_sf, lap_avg_fn=t_lf, total_avg_fn=t_tf,
            )
            today_race_count = agg["race_count"]
            today_bests = [x for x in _best_items(agg["bests"]) if x["value"] is not None]
    except Exception as e:
        print(f"[timing] today bests failed: {type(e).__name__}: {e}", flush=True)   # ベストが出せなくても一覧は表示する
        today_bests = []
    return {
        "today_bests": today_bests,
        "today_best_map": {x["key"]: x for x in today_bests},
        "today_label": today_label,
        "today_from": ts_from, "today_to": ts_to, "today_race_count": today_race_count,
    }
