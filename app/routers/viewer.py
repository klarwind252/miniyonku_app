"""
観覧用画面ルーター
- /view/                     → 現在の画面に自動遷移
- /view/tournament/{tid}     → レース情報・エントリー
- /view/tournament/{tid}/qualifying → 予選スケジュール・順位
- /view/tournament/{tid}/bracket   → トーナメント表
- /view/host-state           → ホスト現在画面の取得(JSON)
- /view/host-sync            → ホストが画面変更を通知(POST)
"""
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import os
import aiosqlite
from app.models.database import get_db

async def _ht_get_finalists_ordered(tid: int, db) -> list[dict]:
    """heat_tournament: ヒートごとの順位（overall_rank）順で進出者を返す
    ヒート1の1位→2位→3位→ヒート2の1位→...の順
    """
    from app.routers.qualifying import _ht_get_advanced
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return []
    # 進出人数は正規値である qual_group_advance を優先（_ht_get_advanced はセクション単位で
    # heat_advance 名ずつ収集するため）。古いデータで qual_heat_advance だけ大きい値が残ると
    # 決勝進出者が実際より多く数えられてしまう不具合を防ぐ。
    heat_advance = int(dict(t).get("qual_group_advance") or dict(t).get("qual_heat_advance") or 1)
    heat_count = int(dict(t).get("qual_heat_count") or 1)

    result = []
    seen_ids = set()
    for hno in range(1, heat_count + 1):
        advanced = await _ht_get_advanced(tid, hno, heat_advance, db)
        for a in sorted(advanced, key=lambda x: x.get("overall_rank", 99)):
            if a.get("entry_id") and a["entry_id"] not in seen_ids:
                result.append({
                    "entry_id": a["entry_id"],
                    "name": a["name"],
                    "seeded": 0,
                })
                seen_ids.add(a["entry_id"])
    return result


router = APIRouter()
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


async def _load_race_assets_v(tid, db) -> dict:
    """参加者HTML用：レース情報の画像アセットを {course/schedule/remarks:[{seq,name,data_uri}]} で返す。
       テーブル未作成等でも安全に空を返す。"""
    out = {"course": [], "schedule": [], "remarks": []}
    try:
        async with db.execute(
            "SELECT kind, seq, name, data_uri FROM race_assets WHERE tournament_id=? ORDER BY kind, seq",
            (tid,),
        ) as cur:
            async for r in cur:
                if r["kind"] in out:
                    out[r["kind"]].append({"seq": r["seq"], "name": r["name"] or "", "data_uri": r["data_uri"]})
    except Exception:
        pass
    return out
from app.config import inject_globals as _inject_globals
_inject_globals(templates)

# ホストの現在画面を保持（メモリ）。複数店舗化のため店舗IDごとに分離。
_host_states: dict = {}


def _store_id_of(request) -> int:
    store = getattr(request.state, "store", None)
    return store.id if store is not None else 0


def _state_for(store_id: int) -> dict:
    return _host_states.setdefault(
        store_id or 0,
        {"url": "/view/", "updated_at": 0, "scroll_to": None, "scroll_at": 0},
    )


@router.post("/host-sync")
async def host_sync(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """管理画面から現在URLを通知"""
    import time
    body = await request.json()
    url = body.get("url", "/view/")
    scroll_to = body.get("scroll_to", None)
    # /admin/... → /view/... に変換
    view_url = _admin_to_view(url)
    st = _state_for(_store_id_of(request))
    st["url"] = view_url
    st["updated_at"] = time.time()
    if scroll_to is not None:
        st["scroll_to"] = scroll_to
        st["scroll_at"] = time.time()

    # 参加者向けHTML配信（トリガー①: ページ切り替え時）
    # asyncio.create_task は現在のコンテキスト（current_store）をコピーするため、
    # バックグラウンドでも正しい店舗のDB/配信先で書き出される。
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass

    return JSONResponse({"ok": True, "view_url": view_url})


@router.get("/host-state")
async def host_state(request: Request):
    """クライアントがポーリングして現在のview URLを取得"""
    return JSONResponse(_state_for(_store_id_of(request)))


def _admin_to_view(admin_url: str) -> str:
    """管理画面URLを観覧画面URLに変換

    複数店舗構成では admin 側が location.pathname をそのまま送ってくるため、
    先頭に "/{slug}" が付く（例: /pararira/admin/tournaments/2）。
    変換後の view URL は内部処理（host_state / public_html）で slug 無しの
    /view/... として扱うため、ここでは先頭の slug を無視してレースID部分だけ
    取り出す。先頭の "(?:/[^/]+)?" は slug 無し（店舗1の /admin/...）にも
    マッチするオプショナルなので後方互換は保たれる。
    """
    import re
    # /admin/tournaments/{tid}/bracket → /view/tournament/{tid}/bracket
    m = re.match(r"(?:/[^/]+)?/admin/tournaments/(\d+)/bracket", admin_url)
    if m:
        return f"/view/tournament/{m.group(1)}/bracket"
    # /admin/tournaments/{tid}/qualifying/... → /view/tournament/{tid}/qualifying
    m = re.match(r"(?:/[^/]+)?/admin/tournaments/(\d+)/qualifying", admin_url)
    if m:
        return f"/view/tournament/{m.group(1)}/qualifying"
    # /admin/tournaments/{tid} → /view/tournament/{tid}
    m = re.match(r"(?:/[^/]+)?/admin/tournaments/(\d+)/?$", admin_url)
    if m:
        return f"/view/tournament/{m.group(1)}"
    return "/view/"


@router.get("/", response_class=HTMLResponse)
async def viewer_top(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """観覧トップ：ホスト追従待機画面"""
    from app import pwa as _pwa
    return templates.TemplateResponse("viewer/base.html", {
        "request": request,
        "page": "top",
        "t": None,
        # 参加者向けhtml生成時は ?_public=1 を付けて内部GETされる。
        # 他ルート(tournament/qualifying/bracket)と同様に is_public_html を渡し、
        # 「お待ちください」状態の参加者向けHTMLに view 用UIが残らないようにする。
        "is_public_html": (request.query_params.get("_public") == "1"),
        # 待機画面の背景（設定ON時のみURL。OFF/未登録なら空）。view と html 双方に効く。
        "bg_url": _pwa.bg_url(request),
        # 待機画面スライドショー（設定ON時のみ画像URL一覧。OFF/未登録なら空リスト）。view / html 双方。
        "slideshow_urls": _pwa.slideshow_urls(request),
    })


@router.get("/tournament/{tid}", response_class=HTMLResponse)
async def viewer_tournament(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """レース情報・エントリー一覧"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse("/view/")

    async with db.execute(
        """SELECT e.entry_order, r.name, r.yomi, e.car_class
           FROM entries e JOIN racers r ON r.id=e.racer_id
           WHERE e.tournament_id=? AND e.status='active'
           ORDER BY r.yomi, r.name""",
        (tid,),
    ) as cur:
        entries = await cur.fetchall()

    from app.routers.tournaments import (
        TIME_SLOT_LABELS, REGULATION_LABELS, QUALIFYING_LABELS, calc_finalists
    )
    t_dict = dict(t)
    finalists = calc_finalists(t_dict.get("qualifying_type", ""), t_dict)

    return templates.TemplateResponse("viewer/tournament.html", {
        "request": request,
        "is_public_html": (request.query_params.get("_public") == "1"),
        "t": t,
        "race_assets": await _load_race_assets_v(tid, db),
        "show_info_bar": True,
        "entries": entries,
        "tid": tid,
        "time_slot_labels": TIME_SLOT_LABELS,
        "regulation_labels": REGULATION_LABELS,
        "qualifying_labels": QUALIFYING_LABELS,
        "finalists": finalists,
    })


@router.get("/tournament/{tid}/qualifying", response_class=HTMLResponse)
async def viewer_qualifying(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """予選画面：スケジュール＋順位表"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse("/view/")

    # ヒート一覧
    async with db.execute(
        """SELECT h.id, h.heat_no, h.status, h.round_no
           FROM heats h WHERE h.tournament_id=? ORDER BY h.heat_no""",
        (tid,),
    ) as cur:
        heats = [dict(r) for r in await cur.fetchall()]

    # レーンマップ
    heat_lanes = {}
    if heats:
        hids = [h["id"] for h in heats]
        ph = ",".join("?" * len(hids))
        async with db.execute(
            f"""SELECT hl.heat_id, hl.lane_no, COALESCE(r.name,'') as name,
                       COALESCE(r.yomi,'') as yomi,
                       hr.rank, COALESCE(hr.is_co,0) as is_co, hr.win
                FROM heat_lanes hl
                LEFT JOIN entries e ON e.id=hl.entry_id
                LEFT JOIN racers r ON r.id=e.racer_id
                LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
                WHERE hl.heat_id IN ({ph})
                ORDER BY hl.lane_no""",
            hids,
        ) as cur:
            for row in await cur.fetchall():
                heat_lanes.setdefault(row["heat_id"], []).append(dict(row))

    # 予選順位
    standings = []
    qt = dict(t).get("qualifying_type", "")
    if qt == "heat_roundrobin":
        # 総当たり：勝数→タイム順で集計
        async with db.execute(
            """SELECT r.name, e.advanced,
                      COUNT(CASE WHEN hr.rank=1 AND COALESCE(hr.is_co,0)=0 THEN 1 END) as wins,
                      COUNT(CASE WHEN hr.rank=2 AND COALESCE(hr.is_co,0)=0 THEN 1 END) as second,
                      COUNT(CASE WHEN hr.rank=3 AND COALESCE(hr.is_co,0)=0 THEN 1 END) as third,
                      COUNT(CASE WHEN COALESCE(hr.is_co,0)=1 THEN 1 END) as cos,
                      COUNT(hr.id) as races
               FROM entries e JOIN racers r ON r.id=e.racer_id
               LEFT JOIN heat_lanes hl ON hl.entry_id=e.id
                 AND hl.heat_id IN (SELECT id FROM heats WHERE tournament_id=?)
               LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
               WHERE e.tournament_id=? AND e.status='active'
               GROUP BY e.id ORDER BY wins DESC, second DESC, third DESC, cos ASC""",
            (tid, tid),
        ) as cur:
            for i, row in enumerate(await cur.fetchall(), 1):
                standings.append({**dict(row), "rank": i, "total_points": dict(row)["wins"]})
    elif qt in ("roundrobin", "none_roundrobin"):
        from app.routers.qualifying import _calc_standings_rr, _calc_standings_none_rr
        if qt == "none_roundrobin":
            rows = await _calc_standings_none_rr(tid, db)
        else:
            rows = await _calc_standings_rr(tid, db)
        standings = rows
    elif qt == "order":
        # 並び順（ポイント制）は admin（予選管理）と同一の順位ロジック(_calc_standings)で
        # 算出し、同率順位・同率内の並び順を予選管理画面と完全一致させる
        from app.routers.qualifying import _calc_standings as _cs_order
        standings = [dict(s) for s in await _cs_order(tid, db)]
    elif qt == "point":
        async with db.execute(
            """SELECT r.name, e.advanced,
                      COALESCE(SUM(hr.points),0) as total_points,
                      COUNT(hr.id) as race_count,
                      COALESCE(SUM(CASE WHEN COALESCE(hr.is_co,0)=0 AND hr.rank>0 THEN 1 ELSE 0 END),0) as finish_count
               FROM entries e JOIN racers r ON r.id=e.racer_id
               LEFT JOIN heat_lanes hl ON hl.entry_id=e.id
                 AND hl.heat_id IN (SELECT id FROM heats WHERE tournament_id=?)
               LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
               WHERE e.tournament_id=? AND e.status='active'
               GROUP BY e.id ORDER BY total_points DESC""",
            (tid, tid),
        ) as cur:
            rows_pt = await cur.fetchall()
            rank = 1
            for i, row in enumerate(rows_pt):
                row = dict(row)
                if i > 0:
                    prev = standings[-1]
                    if row["total_points"] == prev["total_points"]:
                        rank = prev["rank"]
                    else:
                        rank = i + 1
                standings.append({**row, "rank": rank})

    # ポイント制／並び順（ポイント制）: ボーダーライン同率グループに is_tied_cutoff フラグ付与
    if qt in ("point", "order") and standings:
        from app.routers.tournaments import calc_finalists
        finalist_n = calc_finalists(qt, dict(t)) or 0
        if finalist_n and finalist_n <= len(standings):
            cutoff_rank = standings[finalist_n - 1]["rank"]
            cutoff_group = [st for st in standings if st["rank"] == cutoff_rank]
            count_above = sum(1 for st in standings if st["rank"] < cutoff_rank)
            is_border = count_above < finalist_n < count_above + len(cutoff_group)
            for st in standings:
                st["is_tied_cutoff"] = is_border and st["rank"] == cutoff_rank

    # 勝敗表マトリクス（heat_roundrobin のみ）
    win_matrix = []   # [{"name": str, "row": [{"vs": str, "result": "W"/"L"/"CO"/"—"/"-"}]}]
    if qt in ("heat_roundrobin", "roundrobin", "none_roundrobin") and standings:
        # standings の順（勝数降順）でレーサー名リスト
        racer_names = [s["name"] for s in standings]
        # name → entry_id
        async with db.execute(
            """SELECT r.name, e.id as eid FROM entries e
               JOIN racers r ON r.id=e.racer_id
               WHERE e.tournament_id=? AND e.status='active'""",
            (tid,),
        ) as cur:
            name_to_eid = {row["name"]: row["eid"] for row in await cur.fetchall()}

        # 対戦結果: {(winner_eid, loser_eid)} の集合、CO: {(co_eid, opponent_eid)}
        wins_set  = set()
        co_set    = set()
        async with db.execute(
            """SELECT hl1.entry_id as e1, hl2.entry_id as e2,
                      hr1.rank as r1, hr2.rank as r2, hr1.win as win,
                      COALESCE(hr1.is_co,0) as co1, COALESCE(hr2.is_co,0) as co2
               FROM heat_lanes hl1
               JOIN heat_lanes hl2 ON hl1.heat_id=hl2.heat_id AND hl1.entry_id < hl2.entry_id
               LEFT JOIN heat_results hr1 ON hr1.heat_lane_id=hl1.id
               LEFT JOIN heat_results hr2 ON hr2.heat_lane_id=hl2.id
               WHERE hl1.heat_id IN (SELECT id FROM heats WHERE tournament_id=? AND COALESCE(deciding_position,0)=0)""",
            (tid,),
        ) as cur:
            for row in await cur.fetchall():
                row = dict(row)
                e1, e2, r1, r2, co1, co2 = row["e1"], row["e2"], row["r1"], row["r2"], row["co1"], row["co2"]
                if r1 is None and row.get("win") is None:
                    continue
                # roundrobin/none_roundrobin は win フィールドで判定
                win1 = row.get("win")
                if co1 and not co2:
                    co_set.add((e1, e2))   # e1 が CO
                elif co2 and not co1:
                    co_set.add((e2, e1))   # e2 が CO
                elif not co1 and not co2:
                    if win1 == 1:
                        wins_set.add((e1, e2))
                    elif win1 == 0:
                        wins_set.add((e2, e1))
                    elif r1 is not None and r2 is not None:
                        if r1 < r2:
                            wins_set.add((e1, e2))
                        elif r2 < r1:
                            wins_set.add((e2, e1))

        # マトリクス構築
        for row_name in racer_names:
            row_eid = name_to_eid.get(row_name)
            row_data = []
            for col_name in racer_names:
                if col_name == row_name:
                    row_data.append({"vs": col_name, "result": "—"})
                    continue
                col_eid = name_to_eid.get(col_name)
                if (row_eid, col_eid) in wins_set:
                    row_data.append({"vs": col_name, "result": "W"})
                elif (col_eid, row_eid) in wins_set:
                    row_data.append({"vs": col_name, "result": "L"})
                elif (row_eid, col_eid) in co_set:
                    row_data.append({"vs": col_name, "result": "CO"})
                else:
                    row_data.append({"vs": col_name, "result": ""})
            win_matrix.append({"name": row_name, "row": row_data})

    # ヒート制総当たり用データ
    hr_heats_data = []  # [{heat_no, groups:[{group_no, heats:[], standings:[], hoshitori:{}}]}]
    if qt == "heat_roundrobin":
        from app.routers.qualifying import (
            _calc_standings_group_round, _calc_hoshitori_group_round,
            _calc_standings_group, _calc_hoshitori_group,
        )
        # ヒート番号(round_no)とグループ番号の一覧
        async with db.execute(
            "SELECT DISTINCT round_no, group_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY round_no, group_no",
            (tid,),
        ) as cur:
            rg_pairs = [(r["round_no"], r["group_no"]) for r in await cur.fetchall()]

        heat_nos_hr = sorted(set(rno for rno, _ in rg_pairs))
        group_nos_hr = sorted(set(gno for _, gno in rg_pairs))

        for rno in heat_nos_hr:
            groups_data = []
            for gno in group_nos_hr:
                if (rno, gno) not in rg_pairs:
                    continue
                # このヒート×グループのヒート一覧
                async with db.execute(
                    "SELECT * FROM heats WHERE tournament_id=? AND round_no=? AND group_no=? ORDER BY heat_no",
                    (tid, rno, gno),
                ) as cur:
                    g_heats = [dict(r) for r in await cur.fetchall()]

                # レーン情報
                g_heat_ids = [h["id"] for h in g_heats]
                g_lanes = {}
                if g_heat_ids:
                    ph2 = ",".join("?" * len(g_heat_ids))
                    async with db.execute(
                        f"""SELECT hl.heat_id, hl.lane_no, COALESCE(r.name,'') as name,
                                   COALESCE(r.yomi,'') as yomi,
                                   hr.rank, COALESCE(hr.is_co,0) as is_co, hr.win
                            FROM heat_lanes hl
                            LEFT JOIN entries e ON e.id=hl.entry_id
                            LEFT JOIN racers r ON r.id=e.racer_id
                            LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
                            WHERE hl.heat_id IN ({ph2}) ORDER BY hl.lane_no""",
                        g_heat_ids,
                    ) as cur:
                        for row in await cur.fetchall():
                            g_lanes.setdefault(row["heat_id"], []).append(dict(row))

                # グループ×ヒートの順位表・星取表
                g_standings = await _calc_standings_group_round(tid, gno, rno, db)
                g_entries, g_matrix_raw = await _calc_hoshitori_group_round(tid, gno, rno, db)

                # matrix を名前ベースに変換（テンプレートで使いやすく）
                # g_entries: [{entry_id, name}], g_matrix_raw: {(eid_a, eid_b): ["win"/"lose"]}
                eid_to_name = {e["entry_id"]: e["name"] for e in g_entries}
                g_matrix_named = {}
                for (ea, eb), results in g_matrix_raw.items():
                    na = eid_to_name.get(ea, "")
                    nb = eid_to_name.get(eb, "")
                    if not na or not nb or na == nb:
                        continue
                    # 複数対戦の場合は勝利数で判定
                    wins = sum(1 for r in results if r == "win")
                    losses = sum(1 for r in results if r == "lose")
                    if wins > losses:
                        val = "W"
                    elif losses > wins:
                        val = "L"
                    else:
                        val = ""
                    if na not in g_matrix_named:
                        g_matrix_named[na] = {}
                    g_matrix_named[na][nb] = val

                groups_data.append({
                    "group_no": gno,
                    "heats": g_heats,
                    "lanes": g_lanes,
                    "standings": g_standings,
                    "hoshitori_entries": g_entries,
                    "hoshitori_matrix": g_matrix_named,
                })

            # このヒートのトーナメント（bracket）HTMLは既存の bracket_html エンドポイントで取得
            # このヒートの決勝進出者
            group_advance_v = int(dict(t).get("qual_group_advance", 1) or 1)
            qual_heat_exclude_v = bool(dict(t).get("qual_heat_exclude", 0))
            qual_heat_final_v = bool(dict(t).get("qual_heat_final", 0))
            hr_slots = []
            seen_v: set = set()
            if qual_heat_final_v:
                # ヒート優勝トーナメントの勝者を進出者とする
                async with db.execute(
                    """SELECT hf.slot_no, hf.entry_id, r.name
                       FROM heat_finals hf
                       JOIN entries e ON e.id=hf.entry_id
                       JOIN racers r ON r.id=e.racer_id
                       WHERE hf.tournament_id=? AND hf.round_no=? AND hf.group_no=0
                         AND hf.final_type='heat' AND hf.winner_entry_id IS NOT NULL
                       ORDER BY hf.slot_no""",
                    (tid, rno),
                ) as cur:
                    heat_winners_v = [dict(r) for r in await cur.fetchall()]
                for i, hw in enumerate(heat_winners_v):
                    if qual_heat_exclude_v and hw["entry_id"] in seen_v:
                        continue
                    hr_slots.append({"group_no": 0, "rank": i + 1, "name": hw["name"]})
                    if qual_heat_exclude_v:
                        seen_v.add(hw["entry_id"])
            else:
                # グループ上位N名を進出者とする
                for gd in groups_data:
                    picked = 0
                    for st in gd["standings"]:
                        if picked >= group_advance_v:
                            break
                        eid = st.get("entry_id")
                        if qual_heat_exclude_v and eid in seen_v:
                            continue
                        hr_slots.append({"group_no": gd["group_no"], "rank": picked + 1, "name": st["name"]})
                        if qual_heat_exclude_v and eid:
                            seen_v.add(eid)
                        picked += 1

            # このヒートが全レース完了済みか判定
            async with db.execute(
                "SELECT id FROM heats WHERE tournament_id=? AND round_no=? AND group_no>0",
                (tid, rno),
            ) as cur:
                rno_all_ids = {r["id"] for r in await cur.fetchall()}
            async with db.execute(
                """SELECT DISTINCT hl.heat_id FROM heat_results hr2
                   JOIN heat_lanes hl ON hl.id=hr2.heat_lane_id
                   JOIN heats h2 ON h2.id=hl.heat_id
                   WHERE h2.tournament_id=? AND h2.round_no=?""",
                (tid, rno),
            ) as cur:
                rno_done_ids = {r["heat_id"] for r in await cur.fetchall()}
            regular_complete = bool(rno_all_ids) and rno_all_ids.issubset(rno_done_ids)

            # 優勝トーナメントありの場合はその完了もチェック
            if qual_heat_final_v and regular_complete:
                async with db.execute(
                    """SELECT COUNT(*) as cnt FROM heat_finals
                       WHERE tournament_id=? AND round_no=? AND group_no=0
                         AND final_type='heat' AND winner_entry_id IS NOT NULL""",
                    (tid, rno),
                ) as cur:
                    winner_cnt = (await cur.fetchone())["cnt"]
                heat_is_complete = winner_cnt > 0
            else:
                heat_is_complete = regular_complete

            # ヒート決勝の順位（deciding_rank）を取得
            final_slots = []
            if dict(t).get("qual_heat_final"):
                try:
                    async with db.execute(
                        """SELECT hf.entry_id, hf.deciding_rank, r.name
                           FROM heat_finals hf
                           LEFT JOIN entries e ON e.id=hf.entry_id
                           LEFT JOIN racers r ON r.id=e.racer_id
                           WHERE hf.tournament_id=? AND hf.round_no=?
                             AND (hf.final_type='heat' OR hf.final_type IS NULL)
                           ORDER BY hf.slot_no""",
                        (tid, rno),
                    ) as cur:
                        final_slots = [dict(r) for r in await cur.fetchall()]
                except Exception:
                    final_slots = []

            hr_heats_data.append({"heat_no": rno, "groups": groups_data, "advanced": hr_slots if heat_is_complete else [], "is_complete": heat_is_complete, "final_slots": final_slots})

    # ヒートトーナメント系データ
    ht_rounds_data = []
    ht_advanced_by_heat = []
    ht_heats_data = []
    if qt == "heat_tournament":
        try:
            from app.routers.qualifying import _ht_get_advanced as _htga
            heat_count = int(dict(t).get("qual_heat_count") or 1)
            heat_advance = int(dict(t).get("qual_heat_advance") or 1)
            group_count = int(dict(t).get("qual_group_count") or 1)
            group_advance = int(dict(t).get("qual_group_advance") or heat_advance)
            adv_per = group_advance  # qual_group_advance が正規の値（group_count=1でも同じ）
            async with db.execute(
                "SELECT * FROM ht_rounds WHERE tournament_id=? ORDER BY heat_no, COALESCE(section_no,1), round_no",
                (tid,),
            ) as cur:
                ht_rounds = [dict(r) for r in await cur.fetchall()]
            heat_nos = list(range(1, heat_count + 1)) if heat_count else (sorted(set(r["heat_no"] for r in ht_rounds)) or [1])
            for hno in heat_nos:
                h_rounds = [r for r in ht_rounds if r["heat_no"] == hno]
                # ラウンド未生成のヒートはセクションを作らない（viewerで「準備中」表示）
                section_nos = sorted(set(r.get("section_no") if r.get("section_no") is not None else 1 for r in h_rounds)) if h_rounds else []
                sections = []
                heat_final_section = None
                for sec_no in section_nos:
                    sec_rounds = [r for r in h_rounds if (r.get("section_no") if r.get("section_no") is not None else 1) == sec_no]
                    sec_groups_data = []
                    for hr in sec_rounds:
                        async with db.execute(
                            "SELECT * FROM ht_groups WHERE round_id=? ORDER BY group_no", (hr["id"],)
                        ) as cur:
                            ht_groups = [dict(r) for r in await cur.fetchall()]
                        for hg in ht_groups:
                            async with db.execute(
                                """SELECT hs.slot_no, COALESCE(r.name,'') as name, hsr.rank
                                   FROM ht_slots hs
                                   LEFT JOIN entries e ON e.id=hs.entry_id
                                   LEFT JOIN racers r ON r.id=e.racer_id
                                   LEFT JOIN ht_slot_ranks hsr ON hsr.slot_id=hs.id AND hsr.group_id=?
                                   WHERE hs.group_id=? ORDER BY hs.slot_no""",
                                (hg["id"], hg["id"]),
                            ) as cur:
                                slots = [dict(r) for r in await cur.fetchall()]
                            async with db.execute(
                                "SELECT winner_slot_id FROM ht_results WHERE group_id=?", (hg["id"],)
                            ) as cur:
                                res = await cur.fetchone()
                            sec_groups_data.append({
                                "round": hr, "group": hg, "slots": slots,
                                "winner_slot_id": res["winner_slot_id"] if res else None,
                            })
                            ht_rounds_data.append({"round": hr, "groups": [{"group": hg, "slots": slots}]})
                    if sec_no == 0:
                        heat_final_section = {
                            "section_no": 0,
                            "label": "ヒート決勝",
                            "rounds": sec_rounds,
                            "groups_data": sec_groups_data,
                        }
                    else:
                        sections.append({
                            "section_no": sec_no,
                            "label": chr(64 + sec_no),
                            "rounds": sec_rounds,
                            "groups_data": sec_groups_data,
                        })
                adv = await _htga(tid, hno, adv_per, db)
                # admin と同じデータ：ヒート決勝の本戦進出者・グループ通過者・完了フラグ
                from app.routers.qualifying import _ht_get_heatfinal_advancers, _ht_get_group_advancers
                hf_advancers = await _ht_get_heatfinal_advancers(tid, hno, heat_advance, db)
                group_advancers = await _ht_get_group_advancers(tid, hno, group_advance, db)
                hf_done = False
                async with db.execute(
                    """SELECT COUNT(*) FROM ht_slot_ranks sr
                       JOIN ht_groups hg ON hg.id=sr.group_id
                       JOIN ht_rounds hr ON hr.id=hg.round_id
                       WHERE hr.tournament_id=? AND hr.heat_no=? AND hr.section_no=0
                         AND hr.round_type='final'""",
                    (tid, hno),
                ) as cur:
                    hf_done = (await cur.fetchone())[0] > 0
                ht_heats_data.append({
                    "heat_no": hno,
                    "sections": sections,
                    "heat_final_section": heat_final_section,
                    "advanced": sorted(adv, key=lambda x: x.get("overall_rank", 99)),
                    "group_count": group_count,
                    "hf_advancers": hf_advancers,
                    "group_advancers": group_advancers,
                    "hf_done": hf_done,
                })
                # 決勝進出者：ヒート決勝ありは本戦進出者(is_advance)のみ、なしはグループ通過者
                if bool(dict(t).get("qual_heat_final", 0)):
                    _adv_fin = [
                        {"overall_rank": a.get("rank", 1), "name": a.get("name", ""), "yomi": a.get("yomi", "")}
                        for a in hf_advancers if a.get("is_advance")
                    ]
                else:
                    _adv_fin = sorted(adv, key=lambda x: x.get("overall_rank", 99))
                if _adv_fin:
                    ht_advanced_by_heat.append({
                        "heat_no": hno,
                        "advanced": _adv_fin,
                    })
        except Exception as ex:
            ht_rounds_data = []
            ht_heats_data = []

    # エントリー
    async with db.execute(
        """SELECT e.entry_order, r.name, COALESCE(r.yomi,'') as yomi FROM entries e
           JOIN racers r ON r.id=e.racer_id
           WHERE e.tournament_id=? AND e.status='active'
           ORDER BY r.yomi, r.name""",
        (tid,),
    ) as cur:
        entries = await cur.fetchall()

    # none_roundrobin: 確定済み順位 top3_nr
    is_confirmed_nr = False
    top3_nr = [None, None, None]
    if qt == "none_roundrobin":
        is_confirmed_nr = dict(t).get("status") == "complete"
        decided_map = {}
        async with db.execute(
            "SELECT id as entry_id, none_rr_rank FROM entries WHERE tournament_id=? AND none_rr_rank IS NOT NULL",
            (tid,),
        ) as cur:
            for row in await cur.fetchall():
                decided_map[row["entry_id"]] = row["none_rr_rank"]

        # standings に rank がない場合は decided_map または集計順位から補完
        for i, st in enumerate(standings):
            if "rank" not in st or st.get("rank") is None:
                st = dict(st)
                st["rank"] = decided_map.get(st["entry_id"], i + 1)
                standings[i] = st

        # _final_preview: 同率グループは decided_map 優先、残りは自動配置
        _final_preview = {}
        _proc = set()
        for pos in (1, 2, 3):
            if pos in _proc:
                continue
            tied = [st for st in standings if st.get("rank") == pos]
            if len(tied) >= 2:
                n = len(tied)
                max_dec = min(pos + n - 2, 3)
                dec_eids = set()
                for dp in range(pos, max_dec + 1):
                    for st in tied:
                        if decided_map.get(st["entry_id"]) == dp:
                            _final_preview[st["entry_id"]] = dp
                            dec_eids.add(st["entry_id"])
                next_r = max_dec + 1
                for st in tied:
                    if st["entry_id"] not in dec_eids:
                        _final_preview[st["entry_id"]] = next_r
                for p in range(pos, pos + n):
                    _proc.add(p)
            else:
                for st in tied:
                    _final_preview[st["entry_id"]] = pos
                _proc.add(pos)
        for st in standings:
            if st["entry_id"] not in _final_preview:
                _final_preview[st["entry_id"]] = decided_map.get(st["entry_id"]) or st.get("rank", 99)
        top3_nr = []
        for pos in (1, 2, 3):
            matched = [st for st in standings if _final_preview.get(st["entry_id"]) == pos]
            top3_nr.append(matched[0] if matched else None)

    # 決勝進出レーサー（heat_tournament は決勝結果から順位付け、それ以外は advanced=1 のポイント順）
    # none_roundrobin は決勝画面なので finalists_list 不要
    if qt == "none_roundrobin":
        finalists_list = []
    elif qt == "heat_tournament":
        finalists_list = await _ht_get_finalists_ordered(tid, db)
    else:
        if qt == "roundrobin":
            # 勝ち数順（standings と同じ並び）
            from app.routers.qualifying import _calc_standings_rr
            rr_st = await _calc_standings_rr(tid, db)
            adv_eids = set()
            async with db.execute(
                "SELECT id FROM entries WHERE tournament_id=? AND advanced>=1", (tid,)
            ) as cur2:
                for r2 in await cur2.fetchall():
                    adv_eids.add(r2["id"])
            finalists_list = [
                {"name": st["name"], "yomi": st["yomi"] if "yomi" in st.keys() else "", "entry_order": 0, "seeded": 0,
                 "total_points": st["wins"], "qual_rank": st["rank"]}
                for st in rr_st if st["entry_id"] in adv_eids
            ]
        else:
            # point制: _calc_standings の rank（同率考慮）を使う
            from app.routers.qualifying import _calc_standings as _cs
            pt_st = await _cs(tid, db)
            adv_eids2 = set()
            async with db.execute(
                "SELECT id FROM entries WHERE tournament_id=? AND advanced>=1", (tid,)
            ) as cur2:
                for r2 in await cur2.fetchall():
                    adv_eids2.add(r2["id"])
            finalists_list = [
                {"name": st["name"], "yomi": st["yomi"] if "yomi" in st.keys() else "", "entry_order": 0, "seeded": 0,
                 "total_points": st["total_points"], "qual_rank": st["rank"]}
                for st in pt_st if st["entry_id"] in adv_eids2
            ]

    # 順位付け（qual_rank が未設定のものだけ連番で補完）
    for i, f in enumerate(finalists_list, 1):
        if "qual_rank" not in f or f["qual_rank"] is None:
            f["qual_rank"] = i

    # 決勝進出予定数（admin の tournament_detail と同一ロジックで算出）
    from app.routers.tournaments import calc_finalists as _calc_finalists
    _finalists_planned = _calc_finalists(qt, dict(t)) or 0

    # 並び順（ポイント制）：観覧画面に「出走待ち（待機列）」を表示するためのデータ
    from app.routers.qualifying import _order_current_round, _order_queue_pending
    order_pending = []
    if qt == "order" and (dict(t).get("order_status") or "") != "closed":
        _ocr = await _order_current_round(tid, t, db)
        order_pending = await _order_queue_pending(tid, _ocr, db)

    # 並び順（勝ち抜け）：観覧画面用データ（現段階・段階一覧・待機列・組・通過者）
    ow_ctx = {}
    if qt == "order_winner":
        from app.routers.qualifying import (
            _ow_current_stage, _ow_stage_row, _ow_stage_count,
            _ow_queue_pending, _ow_passed_count,
        )
        _ow_stage_no = await _ow_current_stage(tid, t, db)
        _ow_stage = await _ow_stage_row(tid, _ow_stage_no, db) or {}
        _ow_total_stages = await _ow_stage_count(tid, t, db)
        async with db.execute(
            """SELECT stage_no, win_target, max_runs, advance_count, status
               FROM order_winner_stages WHERE tournament_id=? ORDER BY stage_no""",
            (tid,),
        ) as cur:
            _ow_stages = [dict(r) for r in await cur.fetchall()]
        _ow_pending = await _ow_queue_pending(tid, _ow_stage_no, db)
        _ow_passed_n = await _ow_passed_count(tid, _ow_stage_no, db)
        # 現段階の未確定の組（走行中）
        async with db.execute(
            "SELECT id, heat_no FROM heats WHERE tournament_id=? AND round_no=? AND status!='done' ORDER BY heat_no",
            (tid, _ow_stage_no),
        ) as cur:
            _ow_open = [dict(r) for r in await cur.fetchall()]
        _ow_open_heats = []
        for h in _ow_open:
            async with db.execute(
                """SELECT r.name FROM heat_lanes hl
                   JOIN entries e ON e.id=hl.entry_id JOIN racers r ON r.id=e.racer_id
                   WHERE hl.heat_id=? ORDER BY hl.lane_no""",
                (h["id"],),
            ) as cur:
                _ow_open_heats.append({"heat_no": h["heat_no"], "names": [r["name"] for r in await cur.fetchall()]})
        # 現段階の通過者一覧（表示用）
        async with db.execute(
            """SELECT owr.passed_seq, r.name FROM order_winner_racers owr
               JOIN entries e ON e.id=owr.entry_id JOIN racers r ON r.id=e.racer_id
               WHERE owr.tournament_id=? AND owr.stage_no=? AND owr.status='passed'
               ORDER BY owr.passed_seq""",
            (tid, _ow_stage_no),
        ) as cur:
            _ow_passed_list = [dict(r) for r in await cur.fetchall()]
        ow_ctx = {
            "ow_stage_no": _ow_stage_no,
            "ow_stage_count": _ow_total_stages,
            "ow_stages": _ow_stages,
            "ow_stage_status": _ow_stage.get("status") or "pending",
            "ow_advance_count": _ow_stage.get("advance_count") or 0,
            "ow_pending": _ow_pending,
            "ow_passed": _ow_passed_n,
            "ow_open_heats": _ow_open_heats,
            "ow_passed_list": _ow_passed_list,
            "ow_is_last_stage": _ow_stage_no >= _ow_total_stages,
        }
        # 決勝進出予定数（最終段階の advance_count）
        _finalists_planned = (_ow_stages[-1]["advance_count"] if _ow_stages else 0)

    return templates.TemplateResponse("viewer/qualifying.html", {
        "request": request,
        "t": t,
        "race_assets": await _load_race_assets_v(tid, db),
        "show_info_bar": True,
        "tid": tid,
        # 参加者向けhtml生成時は ?_public=1 を付けて内部GETされる。
        # これが真ならスマホ用htmlレイアウト、偽ならview（FHD）用レイアウト。
        "is_public_html": (request.query_params.get("_public") == "1"),
        "heats": heats,
        "heat_lanes": heat_lanes,
        "standings": standings,
        "entries": entries,
        "qualifying_type": qt,
        "ht_rounds_data": ht_rounds_data,
        "ht_heats_data": ht_heats_data,
        "ht_group_advance": int(dict(t).get("qual_group_advance") or 1),
        "ht_heat_advance": (int(dict(t).get("qual_heat_advance") or 1) if dict(t).get("qual_heat_final")
                            else (int(dict(t).get("qual_group_count") or 1) * int(dict(t).get("qual_group_advance") or 1))),
        "ht_heat_count": int(dict(t).get("qual_heat_count") or 1),
        "ht_has_heat_final": bool(dict(t).get("qual_heat_final", 0)),
        "hr_heats_data": hr_heats_data,
        "ht_advanced_by_heat": ht_advanced_by_heat,
        "finalists_list": finalists_list,
        "finalists": _finalists_planned,
        "win_matrix": win_matrix,
        "is_rr_type": qt in ("roundrobin", "none_roundrobin", "heat_roundrobin"),
        "is_none_roundrobin": qt == "none_roundrobin",
        "is_confirmed_nr": is_confirmed_nr,
        "top3_nr": top3_nr,
        "has_any_result": any(
            lane.get("win") is not None
            for lanes in heat_lanes.values()
            for lane in lanes
        ),
        # ── 並び順（ポイント制）order 用：現在ラウンド・モード・予選終了状態 ──
        "order_status": (dict(t).get("order_status") or ""),
        "order_pending": order_pending,
        "order_round_mode": (dict(t).get("order_round_mode") or "free"),
        "order_round_count": int(dict(t).get("order_round_count") or 1),
        "order_current_round": (
            (dict(t).get("order_current_round") or 1)
            if (dict(t).get("order_round_mode") or "free") == "round" else 1
        ),
        "all_heats_complete": (
            # 並び順（ポイント制）は管理者が「予選を終了」(order_status=='closed')するまで未完了扱い。
            # → 予選中にラウンド1が埋まっただけで決勝進出が出るのを防ぐ
            (dict(t).get("order_status") == "closed")
            if qt == "order" else
            # 並び順（勝ち抜け）：最終段階が closed（＝決勝進出者確定済み）になるまで未完了扱い
            (ow_ctx.get("ow_is_last_stage") and ow_ctx.get("ow_stage_status") == "closed")
            if qt == "order_winner" else
            all(hd["is_complete"] for hd in hr_heats_data)
            if hr_heats_data else
            # heat_roundrobin以外: 全heatsが完了しているか
            bool(heat_lanes) and all(
                lane.get("win") is not None
                for lanes in heat_lanes.values()
                for lane in lanes
            )
        ),
        "is_order_winner": qt == "order_winner",
        **ow_ctx,
    })


@router.get("/tournament/{tid}/bracket", response_class=HTMLResponse)
async def viewer_bracket(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """決勝画面：トーナメント表のみ"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse("/view/")

    qt = dict(t).get("qualifying_type", "")

    # 決勝進出予定レーサー（heat_tournament は決勝結果から順位付け、それ以外は advanced=1 のポイント順）
    if qt == "heat_tournament":
        finalists_list = await _ht_get_finalists_ordered(tid, db)
        from app.routers.qualifying import _ht_get_advanced as _htga2
        from app.routers.qualifying import _ht_get_heatfinal_advancers as _hfa2
        _t2 = dict(t)
        heat_advance2 = int(_t2.get("qual_heat_advance") or 1)
        group_advance2 = int(_t2.get("qual_group_advance") or heat_advance2)
        heat_count = int(_t2.get("qual_heat_count") or 1)
        adv_per2 = group_advance2
        _has_hf2 = bool(_t2.get("qual_heat_final", 0))
        ht_advanced_by_heat = []
        for hno in range(1, heat_count + 1):
            adv = await _htga2(tid, hno, adv_per2, db)
            # ヒート決勝ありは本戦進出者(is_advance)のみ、なしはグループ通過者
            if _has_hf2:
                _hf = await _hfa2(tid, hno, heat_advance2, db)
                _adv_fin = [
                    {"overall_rank": a.get("rank", 1), "name": a.get("name", ""), "yomi": a.get("yomi", "")}
                    for a in _hf if a.get("is_advance")
                ]
            else:
                _adv_fin = sorted(adv, key=lambda x: x.get("overall_rank", 99))
            if _adv_fin:
                ht_advanced_by_heat.append({
                    "heat_no": hno,
                    "advanced": _adv_fin,
                })
    else:
        ht_advanced_by_heat = []
        if qt == "roundrobin":
            # 勝ち数順（standings と同じ並び）
            from app.routers.qualifying import _calc_standings_rr
            rr_st = await _calc_standings_rr(tid, db)
            adv_eids = set()
            async with db.execute(
                "SELECT id FROM entries WHERE tournament_id=? AND advanced>=1", (tid,)
            ) as cur2:
                for r2 in await cur2.fetchall():
                    adv_eids.add(r2["id"])
            finalists_list = [
                {"name": st["name"], "yomi": st["yomi"] if "yomi" in st.keys() else "", "entry_order": 0, "seeded": 0,
                 "total_points": st["wins"], "qual_rank": st["rank"]}
                for st in rr_st if st["entry_id"] in adv_eids
            ]
        else:
            from app.routers.qualifying import _calc_standings as _cs2
            pt_st2 = await _cs2(tid, db)
            adv_eids3 = set()
            async with db.execute(
                "SELECT id FROM entries WHERE tournament_id=? AND advanced>=1", (tid,)
            ) as cur2:
                for r2 in await cur2.fetchall():
                    adv_eids3.add(r2["id"])
            finalists_list = [
                {"name": st["name"], "yomi": st["yomi"] if "yomi" in st.keys() else "", "entry_order": 0, "seeded": 0,
                 "total_points": st["total_points"], "qual_rank": st["rank"]}
                for st in pt_st2 if st["entry_id"] in adv_eids3
            ]

    for i, f in enumerate(finalists_list, 1):
        if "qual_rank" not in f or f["qual_rank"] is None:
            f["qual_rank"] = i

    # 全エントリー（決勝進出者がいない場合に表示）
    async with db.execute(
        """SELECT e.entry_order, r.name, r.yomi FROM entries e
           JOIN racers r ON r.id=e.racer_id
           WHERE e.tournament_id=? AND e.status='active'
           ORDER BY r.yomi, r.name""",
        (tid,),
    ) as cur:
        entries = [dict(r) for r in await cur.fetchall()]

    # heat_roundrobin: 決勝進出者（qual_heat_final=1 なら heat_finals 勝者、なければグループ上位）
    hr_heats_data_bv = []
    if qt == "heat_roundrobin":
        t_dict_bv = dict(t)
        qual_heat_final_bv = bool(t_dict_bv.get("qual_heat_final", 0))
        qual_heat_exclude_bv = bool(t_dict_bv.get("qual_heat_exclude", 0))
        async with db.execute(
            "SELECT DISTINCT round_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY round_no",
            (tid,),
        ) as cur:
            heat_nos_bv = [r["round_no"] for r in await cur.fetchall()]
        seen_bv: set = set()
        for rno in heat_nos_bv:
            slots_bv = []
            if qual_heat_final_bv:
                async with db.execute(
                    """SELECT hf.slot_no, hf.entry_id, r.name
                       FROM heat_finals hf
                       JOIN entries e ON e.id=hf.entry_id
                       JOIN racers r ON r.id=e.racer_id
                       WHERE hf.tournament_id=? AND hf.round_no=? AND hf.group_no=0
                         AND hf.final_type='heat' AND hf.winner_entry_id IS NOT NULL
                       ORDER BY hf.slot_no""",
                    (tid, rno),
                ) as cur:
                    heat_winners_bv = [dict(r) for r in await cur.fetchall()]
                for i, hw in enumerate(heat_winners_bv):
                    eid = hw["entry_id"]
                    if qual_heat_exclude_bv and eid in seen_bv:
                        continue
                    slots_bv.append({"group_no": 0, "rank": i + 1, "name": hw["name"]})
                    if qual_heat_exclude_bv:
                        seen_bv.add(eid)
            else:
                from app.routers.qualifying import _calc_standings_group_round
                group_advance_bv = int(t_dict_bv.get("qual_group_advance", 1) or 1)
                async with db.execute(
                    "SELECT DISTINCT round_no, group_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY round_no, group_no",
                    (tid,),
                ) as cur:
                    rg_pairs_bv = [(r["round_no"], r["group_no"]) for r in await cur.fetchall()]
                group_nos_bv = sorted(set(gno for _, gno in rg_pairs_bv))
                for gno in group_nos_bv:
                    if (rno, gno) not in rg_pairs_bv:
                        continue
                    st_bv = await _calc_standings_group_round(tid, gno, rno, db)
                    picked = 0
                    for p in st_bv:
                        if picked >= group_advance_bv:
                            break
                        eid = p["entry_id"]
                        if qual_heat_exclude_bv and eid in seen_bv:
                            continue
                        slots_bv.append({"group_no": gno, "rank": p["rank"], "name": p["name"]})
                        if qual_heat_exclude_bv:
                            seen_bv.add(eid)
                        picked += 1
            hr_heats_data_bv.append({"heat_no": rno, "advanced": slots_bv})

    # 決勝結果（1〜3位）を取得
    final_results = []  # [{rank, name, round_type}]
    async with db.execute(
        """SELECT bsr.rank, br.round_type, r.name
           FROM bracket_slot_ranks bsr
           JOIN bracket_slots bs ON bs.id=bsr.slot_id
           JOIN entries e ON e.id=bs.entry_id
           JOIN racers r ON r.id=e.racer_id
           JOIN bracket_groups bg ON bg.id=bsr.group_id
           JOIN bracket_rounds br ON br.id=bg.round_id
           WHERE br.tournament_id=? AND bsr.rank IN (1,2,3)
           ORDER BY CASE br.round_type WHEN 'final' THEN 1 WHEN 'third' THEN 2 WHEN 'revival' THEN 3 ELSE 4 END, bsr.rank""",
        (tid,),
    ) as cur:
        seen_ranks = set()
        for row in await cur.fetchall():
            row = dict(row)
            # 同じ順位は最初のもの（final優先）のみ
            if row["rank"] not in seen_ranks:
                final_results.append(row)
                seen_ranks.add(row["rank"])

    # 3位決定戦（③third）の勝者のみ3位として補完。
    # ②revival（敗者復活戦）の勝者は決勝へ進むため3位ではない（3位は決勝rank=3で決まる）。
    if 3 not in seen_ranks:
        async with db.execute(
            """SELECT ra.name, br.round_type
               FROM bracket_results bres
               JOIN bracket_slots bs ON bs.id=bres.winner_slot_id
               JOIN bracket_groups bg ON bg.id=bres.group_id
               JOIN bracket_rounds br ON br.id=bg.round_id
               JOIN entries e ON e.id=bs.entry_id
               JOIN racers ra ON ra.id=e.racer_id
               WHERE br.tournament_id=? AND br.round_type = 'third'
               LIMIT 1""",
            (tid,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                final_results.append({"rank": 3, "name": row["name"], "round_type": row["round_type"]})

    final_results.sort(key=lambda x: x["rank"])

    return templates.TemplateResponse("viewer/bracket.html", {
        "request": request,
        "is_public_html": (request.query_params.get("_public") == "1"),
        "t": t,
        "race_assets": await _load_race_assets_v(tid, db),
        "show_info_bar": True,
        "tid": tid,
        "finalists_list": finalists_list,
        "entries": entries,
        "ht_advanced_by_heat": ht_advanced_by_heat,
        "hr_heats_data": hr_heats_data_bv,
        "final_results": final_results,
    })
