from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import aiosqlite
import os
import uuid
import json
from datetime import date

from app.infrastructure.db.connection import get_db

from app.services.barcode import build_code

from app.core.config import HEAT_TOURNAMENT_TYPES, GARAPPA_STORE_NAME, IS_CLOUD
from app.presentation.templates import templates

router = APIRouter()


def _is_junior_tournament(regulation: str | None) -> bool:
    """レギュレーション文字列に「ジュニア」「junior」「Jr」「子供」が含まれるか"""
    if not regulation:
        return False
    reg = regulation.lower()
    return "ジュニア" in regulation or "子供" in regulation or "junior" in reg or "jr" in reg

STATUS_LABELS = {
    "prepare": "準備中",
    "qualifying": "予選中",
    "final": "決勝中",
    "finished": "終了",
}

TIME_SLOT_LABELS = {
    "day": "デイレース",
    "night": "ナイトレース",
    "extended": "延長",
    "free": "フリーワード",
}

REGULATION_LABELS = {
    "open": "オープンクラス",
    "junior": "ジュニアクラス",
    "stock": "ストッククラス",
    "stock_junior": "ストッククラス（Jr.の部）",
    "bmax": "B-MAX",
    "gt_advance": "GT-Advance",
    "scratch": "巣組",
    "normal_motor": "ノーマルモーター限定",
    "tune_motor": "チューンモーター限定",
    "hyper_dash": "ハイパーダッシュ限定",
    "single_axis": "片軸限定",
    "single_axis_junior": "片軸限定（Jr.の部）",
}


async def get_regulation_labels(db: aiosqlite.Connection) -> dict:
    """DBのapp_settingsからレギュレーション一覧を取得。未設定はREGULATION_LABELSにフォールバック"""
    import json
    try:
        async with db.execute("SELECT value FROM app_settings WHERE key='regulations'") as cur:
            row = await cur.fetchone()
        if row:
            items = json.loads(row["value"])
            # ラベル文字列をそのままキーにした辞書を返す
            return {v: v for v in items}
    except Exception:
        pass
    return REGULATION_LABELS

QUALIFYING_LABELS = {
    "none": "なし（即決勝トーナメント）",
    "none_roundrobin": "なし（即決勝総当たり）",
    "heat_tournament": "ヒート（トーナメント）",
    "heat_tournament_garappa": "ヒート（トーナメント）[がらっぱ堂]",
    "heat_roundrobin": "ヒート（総当たり）",
    "point": "ポイント",
    "roundrobin": "総当たり",
    "order": "並び順（ポイント制）",
    "order_winner": "並び順（勝ち抜け）",
}


async def _get_store1_name(db) -> str:
    """店舗1（既定店舗）の表示名を返す。

    クラウド版：店舗レジストリ（control.db）の既定店舗名。
    オンプレ版：メインDBの app_settings キー 'store_name'。
    未設定・失敗時は空文字。
    """
    if IS_CLOUD:
        try:
            from app import registry
            st = registry.get_default_store()
            return (st.name or "") if st else ""
        except Exception:
            return ""
    try:
        async with db.execute("SELECT value FROM app_settings WHERE key='store_name'") as cur:
            row = await cur.fetchone()
        return (row["value"] if row else "") or ""
    except Exception:
        return ""


# calc_finalists は domain/finalists.py へ移設。旧参照互換のため再エクスポート。
from app.domain.finalists import calc_finalists  # noqa: F401


def parse_order_winner_stages(form) -> list[dict]:
    """フォーム（starlette FormData / dict）から order_winner の段階設定を読み取る。
    ow_stage_count と、各段階の ow_win_target_{n} / ow_max_runs_{n} / ow_advance_count_{n}
    を 1..N で読む。壊れた値は既定へフォールバックする。
    戻り値: [{stage_no, win_target, max_runs, advance_count}, ...]（stage_no=1..N）
    """
    def _get_int(key, default):
        try:
            v = form.get(key)
            if v is None or v == "":
                return default
            return int(v)
        except (ValueError, TypeError):
            return default

    stage_count = _get_int("ow_stage_count", 1)
    if stage_count < 1:
        stage_count = 1
    if stage_count > 20:
        stage_count = 20  # 上限ガード（自由入力の暴走防止）

    stages: list[dict] = []
    for n in range(1, stage_count + 1):
        win_target = _get_int(f"ow_win_target_{n}", 1)
        max_runs = _get_int(f"ow_max_runs_{n}", 3)
        advance_count = _get_int(f"ow_advance_count_{n}", 12)
        # 範囲ガード
        win_target = max(1, min(win_target, 10))
        max_runs = max(1, min(max_runs, 20))
        advance_count = max(1, min(advance_count, 999))
        stages.append({
            "stage_no": n,
            "win_target": win_target,
            "max_runs": max_runs,
            "advance_count": advance_count,
        })
    return stages


async def save_order_winner_stages(tid: int, stages: list[dict], db) -> None:
    """order_winner の段階設定を保存する。
    既存の段階行を全削除してから入れ直す（段階数の増減に対応）。
    tournaments.order_winner_stage_count / order_winner_current_stage も更新。
    ※ 進行中データ（order_winner_racers / order_queue の結果）は編集時ここでは触らない。
      結果が入った後の編集は _is_result_finalized 側で弾かれる前提。
    """
    await db.execute("DELETE FROM order_winner_stages WHERE tournament_id=?", (tid,))
    for s in stages:
        await db.execute(
            """INSERT INTO order_winner_stages
               (tournament_id, stage_no, win_target, max_runs, advance_count, status)
               VALUES (?,?,?,?,?, 'pending')""",
            (tid, s["stage_no"], s["win_target"], s["max_runs"], s["advance_count"]),
        )
    stage_count = len(stages) if stages else 1
    await db.execute(
        "UPDATE tournaments SET order_winner_stage_count=?, order_winner_current_stage=1 WHERE id=?",
        (stage_count, tid),
    )


async def load_order_winner_stages(tid: int, db) -> list[dict]:
    """order_winner の段階設定を stage_no 昇順で読み出す（編集画面の初期描画用）。"""
    async with db.execute(
        """SELECT stage_no, win_target, max_runs, advance_count, status
           FROM order_winner_stages WHERE tournament_id=? ORDER BY stage_no""",
        (tid,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


@router.get("/", response_class=HTMLResponse)
async def tournament_list(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    async with db.execute(
        """SELECT t.*, COUNT(e.id) as entry_count
           FROM tournaments t
           LEFT JOIN entries e ON e.tournament_id = t.id
           GROUP BY t.id ORDER BY t.date DESC, t.id DESC"""
    ) as cur:
        tournaments = await cur.fetchall()

    # 各レースの1位・2位・3位を取得
    results_map = {}
    for t in tournaments:
        tid = t["id"]
        qt = t["qualifying_type"] or ""

        if qt == "none_roundrobin":
            # 即決勝総当たり → entries.none_rr_rank から取得
            async with db.execute(
                """SELECT e.none_rr_rank as rank, r.name
                   FROM entries e
                   JOIN racers r ON r.id=e.racer_id
                   WHERE e.tournament_id=? AND e.none_rr_rank IN (1,2,3)
                   ORDER BY e.none_rr_rank""",
                (tid,),
            ) as cur_nr:
                nr_result = {}
                for row in await cur_nr.fetchall():
                    if row["rank"] not in nr_result:
                        nr_result[row["rank"]] = row["name"]
            if nr_result:
                results_map[tid] = nr_result

        elif qt in HEAT_TOURNAMENT_TYPES:
            # 決勝トーナメント（bracket）が完了していれば bracket_slot_ranks から取得
            ht_result = {}
            async with db.execute(
                """SELECT bsr.rank, br.round_type, r.name
                   FROM bracket_slot_ranks bsr
                   JOIN bracket_slots bs ON bs.id=bsr.slot_id
                   JOIN entries e ON e.id=bs.entry_id
                   JOIN racers r ON r.id=e.racer_id
                   JOIN bracket_groups bg ON bg.id=bsr.group_id
                   JOIN bracket_rounds br ON br.id=bg.round_id
                   WHERE br.tournament_id=? AND bsr.rank IN (1,2,3)
                   ORDER BY CASE br.round_type WHEN 'final' THEN 1 WHEN 'third' THEN 2 ELSE 3 END, bsr.rank""",
                (tid,),
            ) as cur_br:
                for br in await cur_br.fetchall():
                    if br["round_type"] == "final":
                        if br["rank"] == 1 and 1 not in ht_result:
                            ht_result[1] = br["name"]
                        elif br["rank"] == 2 and 2 not in ht_result:
                            ht_result[2] = br["name"]
                        elif br["rank"] == 3 and 3 not in ht_result:
                            ht_result[3] = br["name"]
                    elif br["round_type"] == "third":
                        if br["rank"] == 1 and 3 not in ht_result:
                            ht_result[3] = br["name"]
            # 3位決定戦（third）の勝者を bracket_results から補完
            if 3 not in ht_result:
                async with db.execute(
                    """SELECT ra.name
                       FROM bracket_results bres
                       JOIN bracket_slots bs ON bs.id=bres.winner_slot_id
                       JOIN bracket_groups bg ON bg.id=bres.group_id
                       JOIN bracket_rounds br ON br.id=bg.round_id
                       JOIN entries e ON e.id=bs.entry_id
                       JOIN racers ra ON ra.id=e.racer_id
                       WHERE br.tournament_id=? AND br.round_type='third'
                       LIMIT 1""",
                    (tid,),
                ) as cur_third_ht:
                    third_ht = await cur_third_ht.fetchone()
                    if third_ht:
                        ht_result[3] = third_ht["name"]
            if ht_result:
                results_map[tid] = ht_result
        else:
            # それ以外 → bracket_slot_ranks から取得
            # finalを先に、次にthird、normalは後
            async with db.execute(
                """SELECT bsr.rank, br.round_type, br.round_no, r.name
                   FROM bracket_slot_ranks bsr
                   JOIN bracket_slots bs ON bs.id=bsr.slot_id
                   JOIN entries e ON e.id=bs.entry_id
                   JOIN racers r ON r.id=e.racer_id
                   JOIN bracket_groups bg ON bg.id=bsr.group_id
                   JOIN bracket_rounds br ON br.id=bg.round_id
                   WHERE br.tournament_id=? AND bsr.rank IN (1,2,3)
                   ORDER BY CASE br.round_type
                     WHEN 'final' THEN 1
                     WHEN 'third' THEN 2
                     ELSE 3
                   END, br.round_no DESC, bsr.rank""",
                (tid,),
            ) as cur_br:
                bracket_rows = await cur_br.fetchall()

            br_result = {}
            by_round = {}
            for r in bracket_rows:
                by_round.setdefault(r["round_no"], []).append(r)

            # finalラウンドから1-3位を取得
            for r in bracket_rows:
                if r["round_type"] == "final":
                    if r["rank"] not in br_result:
                        br_result[r["rank"]] = r["name"]
                elif r["round_type"] == "third":
                    if r["rank"] == 1 and 3 not in br_result:
                        br_result[3] = r["name"]

            # 3位決定戦（third）の勝者を bracket_results から補完
            # third ラウンドは setWinner（○ボタン）で保存されるため bracket_slot_ranks に入らない
            if 3 not in br_result:
                async with db.execute(
                    """SELECT ra.name
                       FROM bracket_results bres
                       JOIN bracket_slots bs ON bs.id=bres.winner_slot_id
                       JOIN bracket_groups bg ON bg.id=bres.group_id
                       JOIN bracket_rounds br ON br.id=bg.round_id
                       JOIN entries e ON e.id=bs.entry_id
                       JOIN racers ra ON ra.id=e.racer_id
                       WHERE br.tournament_id=? AND br.round_type='third'
                       LIMIT 1""",
                    (tid,),
                ) as cur_third:
                    third_winner = await cur_third.fetchone()
                    if third_winner:
                        br_result[3] = third_winner["name"]

            # finalに結果がない（round_type='normal'のみ）場合：最大round_noを決勝扱い
            if not br_result and by_round:
                max_rno = max(by_round.keys())
                for r in by_round[max_rno]:
                    if r["rank"] not in br_result:
                        br_result[r["rank"]] = r["name"]

            # 3位がない場合：準決勝（max_rnoの一個前）のrank=2を3位に
            if 3 not in br_result and by_round and len(by_round) >= 2:
                max_rno = max(by_round.keys())
                prev_rnos = sorted([rno for rno in by_round if rno < max_rno], reverse=True)
                if prev_rnos:
                    for r in by_round[prev_rnos[0]]:
                        if r["rank"] == 2 and 3 not in br_result:
                            br_result[3] = r["name"]

            # それでも3位がない場合：準決勝グループの勝者(rank=1のslot_id)以外のスロット選手を3位に
            if 3 not in br_result and by_round and len(by_round) >= 2:
                max_rno = max(by_round.keys())
                prev_rnos = sorted([rno for rno in by_round if rno < max_rno], reverse=True)
                if prev_rnos:
                    prev_rno = prev_rnos[0]
                    async with db.execute(
                        """SELECT ra.name
                           FROM bracket_slots bs
                           JOIN bracket_groups bg ON bg.id=bs.group_id
                           JOIN bracket_rounds br ON br.id=bg.round_id
                           JOIN entries e ON e.id=bs.entry_id
                           JOIN racers ra ON ra.id=e.racer_id
                           WHERE br.tournament_id=? AND br.round_no=?
                             AND bs.id NOT IN (
                               SELECT bsr2.slot_id
                               FROM bracket_slot_ranks bsr2
                               JOIN bracket_groups bg2 ON bg2.id=bsr2.group_id
                               JOIN bracket_rounds br2 ON br2.id=bg2.round_id
                               WHERE br2.tournament_id=? AND br2.round_no=? AND bsr2.rank=1
                             )
                           ORDER BY bg.group_no, bs.slot_no LIMIT 1""",
                        (tid, prev_rno, tid, prev_rno),
                    ) as cur3:
                        third_row = await cur3.fetchone()
                        if third_row:
                            br_result[3] = third_row["name"]

            results_map[tid] = br_result

    import json as _json
    all_tournaments_json = _json.dumps([
        {
            "id": t["id"],
            "name": t["name"],
            "date": t["date"],
            "time_slot": t["time_slot"] or "",
            "status": t["status"] or "",
            "regulation": t["regulation"] or "",
        }
        for t in tournaments
    ], ensure_ascii=False)

    return templates.TemplateResponse("admin/tournaments.html", {
        "request": request,
        "tournaments": tournaments,
        "results_map": results_map,
        "status_labels": STATUS_LABELS,
        "time_slot_labels": TIME_SLOT_LABELS,
        "regulation_labels": await get_regulation_labels(db),
        "qualifying_labels": QUALIFYING_LABELS,
        "all_tournaments_json": all_tournaments_json,
    })


@router.get("/new", response_class=HTMLResponse)
async def tournament_new(request: Request, db: aiosqlite.Connection = Depends(get_db), date_param: str = Query(None, alias="date")):
    initial_date = date_param if date_param else date.today().isoformat()
    reg_labels = await get_regulation_labels(db)
    async with db.execute("SELECT value FROM app_settings WHERE key='default_qualifying'") as cur:
        dq_row = await cur.fetchone()
    default_qualifying = dq_row["value"] if dq_row else "heat_tournament"
    garappa_enabled = (await _get_store1_name(db)).strip() == GARAPPA_STORE_NAME
    return templates.TemplateResponse("admin/tournament_form.html", {
        "request": request,
        "today": initial_date,
        "tournament": None,
        "time_slot_labels": TIME_SLOT_LABELS,
        "regulation_labels": reg_labels,
        "qualifying_labels": QUALIFYING_LABELS,
        "default_qualifying": default_qualifying,
        "garappa_enabled": garappa_enabled,
        "race_assets": {k: [] for k in _ASSET_KINDS},
    })


@router.post("/add")
async def tournament_add(
    request: Request,
    name: str = Form(...),
    date: str = Form(...),
    time_slot: str = Form("day"),
    time_slot_free: str = Form(""),
    regulation: str = Form("open"),
    qualifying_type: str = Form("heat_tournament"),
    final_type: str = Form("tournament"),
    lane_count: int = Form(3),
    note: str = Form(""),
    time_schedule: str = Form(""),
    qual_heat_count: int = Form(1),
    qual_heat_advance: int = Form(2),
    qual_group_count: int = Form(1),
    qual_group_advance: int = Form(2),
    qual_heat_final: int = Form(0),
    qual_heat_final_advance: int = Form(1),
    qual_final_advance: int = Form(2),
    point_1st: int = Form(3),
    point_2nd: int = Form(2),
    point_3rd: int = Form(1),
    point_co: int = Form(0),
    qual_round_count: int = Form(1),
    qual_heat_exclude: int = Form(0),
    order_round_mode: str = Form("free"),
    order_round_count: int = Form(3),
    order_free_max_runs: int = Form(0),
    use_racer_master: int = Form(1),
    pre_entry: int = Form(0),
    pre_entry_method: str = Form(""),
    pre_entry_deadline: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    # 事前エントリー設定の整合（作成時に確定）
    #   ON のとき：レーサーマスタは強制 OFF、方法は manual / form のみ許可
    #   OFF のとき：方法は NULL、マスタ使用は受け取った値を尊重
    if pre_entry == 1:
        use_racer_master = 0
        method = pre_entry_method if pre_entry_method in ("manual", "form") else "manual"
        # 締切日時は form 方式のときのみ採用（manual は締切の概念なし）
        deadline = pre_entry_deadline.strip() if (method == "form" and pre_entry_deadline) else None
    else:
        pre_entry = 0
        method = None
        deadline = None

    cur_ins = await db.execute(
        """INSERT INTO tournaments
           (name, date, time_slot, time_slot_free, regulation,
            qualifying_type, final_type, lane_count, note, time_schedule,
            qual_heat_count, qual_heat_advance,
            qual_group_count, qual_group_advance,
            qual_heat_final, qual_heat_final_advance, qual_final_advance,
            point_1st, point_2nd, point_3rd, point_co, qual_round_count, qual_heat_exclude,
            order_round_mode, order_round_count, order_free_max_runs,
            use_racer_master, pre_entry, pre_entry_method, pre_entry_deadline)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, date, time_slot, time_slot_free, regulation,
         qualifying_type, final_type, lane_count, note, time_schedule,
         qual_heat_count, qual_heat_advance,
         qual_group_count, qual_group_advance,
         qual_heat_final, qual_heat_final_advance, qual_final_advance,
         point_1st, point_2nd, point_3rd, point_co, qual_round_count, qual_heat_exclude,
         order_round_mode, order_round_count, order_free_max_runs,
         use_racer_master, pre_entry, method, deadline),
    )
    new_tid = cur_ins.lastrowid
    # レース情報の画像アセット（コースレイアウト/タイムスケジュール/備考）を保存
    await _save_race_assets(new_tid, await request.form(), db)
    # 並び順（勝ち抜け）の段階設定を保存
    if qualifying_type == "order_winner":
        stages = parse_order_winner_stages(await request.form())
        await save_order_winner_stages(new_tid, stages, db)
    await db.commit()
    return RedirectResponse(url="/admin/tournaments/", status_code=303)


def _resize_data_uri_if_big(data_uri: str, max_edge: int = 667):
    """data_uri の画像が長辺 max_edge を超える場合のみ縮小した data_uri を返す。不要なら None。"""
    try:
        from PIL import Image
        import io, base64
        _, b64 = data_uri.split(",", 1)
        raw = base64.b64decode(b64)
        with Image.open(io.BytesIO(raw)) as im:
            w, h = im.size
        if max(w, h) <= max_edge:
            return None
        result = _resize_image(raw, max_edge)  # (content_type, bytes) or None
        if not result:
            return None
        ctype, out = result
        return "data:" + ctype + ";base64," + base64.b64encode(out).decode("ascii")
    except Exception:
        return None


@router.get("/tools/resize-assets", response_class=HTMLResponse)
async def resize_race_assets_tool(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """既存のレース情報画像（コースレイアウト/タイムスケジュール/備考）のうち、長辺が667pxを
       超えるものを一括で縮小する。対象は現在の店舗DB。667px以下は再エンコードしないので
       何度実行しても安全（画質を無駄に劣化させない）。管理者のみ（/admin配下）。"""
    async with db.execute("SELECT id, data_uri FROM race_assets") as cur:
        rows = await cur.fetchall()
    total = len(rows)
    resized = 0
    for r in rows:
        new_uri = _resize_data_uri_if_big(r["data_uri"], 667)
        if new_uri:
            await db.execute("UPDATE race_assets SET data_uri=? WHERE id=?", (new_uri, r["id"]))
            resized += 1
    if resized:
        await db.commit()
    return HTMLResponse(
        "<div style='font-family:sans-serif;padding:24px;font-size:15px;color:#2c3e50'>"
        "<h3>画像の一括リサイズ 完了</h3>"
        f"<p>対象 {total} 件中、<b>{resized} 件</b>を長辺667pxに縮小しました"
        "（すでに667px以下の画像はそのまま）。</p>"
        "<p><a href='/admin/'>管理トップへ戻る</a></p></div>"
    )


@router.get("/{tid}", response_class=HTMLResponse)
async def tournament_detail(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url="/admin/tournaments/")

    async with db.execute(
        """SELECT e.id, e.car_class, e.status, e.entry_order, e.pre_seq_no,
                  r.name as racer_name, r.yomi
           FROM entries e
           JOIN racers r ON r.id = e.racer_id
           WHERE e.tournament_id = ?
           ORDER BY r.yomi, r.name""",
        (tid,),
    ) as cur:
        entries = await cur.fetchall()

    t_dict = dict(t)
    is_junior = _is_junior_tournament(t_dict.get("regulation"))

    # 事前エントリーON時：前段階テーブル pre_entries を取得（未受付のみ・連番順）
    #   受付済み（checked_in=1）は本エントリーへ昇格済みのため一覧には出さない
    #   （受付済みは「エントリー済みレーサー」側で確認できる）。連番との対応はDBに保持。
    pre_entries = []
    if t_dict.get("pre_entry"):
        async with db.execute(
            """SELECT id, seq_no, name, yomi, is_child, prefecture,
                      contact_type, contact, is_representative, checked_in, is_walkin
               FROM pre_entries
               WHERE tournament_id = ? AND COALESCE(checked_in,0) = 0
               ORDER BY seq_no""",
            (tid,),
        ) as cur:
            pre_entries = await cur.fetchall()

    async with db.execute(
        """SELECT r.id, r.name, r.yomi, r.is_child, r.is_regular FROM racers r
           WHERE COALESCE(r.ephemeral,0) = 0
             AND r.id NOT IN (
               SELECT racer_id FROM entries WHERE tournament_id=?
           )
           {jr_filter}
           ORDER BY r.yomi, r.name""".format(
            jr_filter="AND r.is_child=1" if is_junior else ""
        ),
        (tid,),
    ) as cur:
        available_racers = await cur.fetchall()

    finalists = calc_finalists(t_dict.get("qualifying_type", ""), t_dict)
    is_finalized = await _is_result_finalized(tid, db)

    # 決勝進出レーサーが1人でも確定しているか（予選未実施なら決勝管理ボタンを隠す判定に使う）
    async with db.execute(
        "SELECT COUNT(*) AS n FROM entries WHERE tournament_id=? AND COALESCE(advanced,0)=1",
        (tid,),
    ) as cur:
        has_finalists = (await cur.fetchone())["n"] > 0

    # 1〜3位を取得（確定済みの場合）
    cert_results = {}
    if is_finalized:
        qt_val = dict(t).get("qualifying_type", "")
        if qt_val == "none_roundrobin":
            async with db.execute(
                """SELECT e.none_rr_rank as rank, r.name
                   FROM entries e JOIN racers r ON r.id=e.racer_id
                   WHERE e.tournament_id=? AND e.none_rr_rank IN (1,2,3)
                   ORDER BY e.none_rr_rank""", (tid,)
            ) as cur:
                for row in await cur.fetchall():
                    cert_results[row["rank"]] = row["name"]
        else:
            async with db.execute(
                """SELECT bsr.rank, br.round_type, r.name
                   FROM bracket_slot_ranks bsr
                   JOIN bracket_slots bs ON bs.id=bsr.slot_id
                   JOIN bracket_groups bg ON bg.id=bs.group_id
                   JOIN bracket_rounds br ON br.id=bg.round_id
                   JOIN entries e ON e.id=bs.entry_id
                   JOIN racers r ON r.id=e.racer_id
                   WHERE br.tournament_id=? AND bsr.rank IN (1,2,3)
                   ORDER BY CASE br.round_type WHEN 'final' THEN 1 WHEN 'third' THEN 2 ELSE 3 END, bsr.rank""", (tid,)
            ) as cur:
                for row in await cur.fetchall():
                    if row["round_type"] == "final":
                        if row["rank"] not in cert_results:
                            cert_results[row["rank"]] = row["name"]
                    elif row["round_type"] == "third":
                        if row["rank"] == 1 and 3 not in cert_results:
                            cert_results[3] = row["name"]
            # 3位決定戦はsetWinner（○ボタン）で保存されるためbracket_slot_ranksに入らない→bracket_resultsから補完
            if 3 not in cert_results:
                async with db.execute(
                    """SELECT ra.name
                       FROM bracket_results bres
                       JOIN bracket_slots bs ON bs.id=bres.winner_slot_id
                       JOIN bracket_groups bg ON bg.id=bres.group_id
                       JOIN bracket_rounds br ON br.id=bg.round_id
                       JOIN entries e ON e.id=bs.entry_id
                       JOIN racers ra ON ra.id=e.racer_id
                       WHERE br.tournament_id=? AND br.round_type='third'
                       LIMIT 1""", (tid,)
                ) as cur:
                    third_row = await cur.fetchone()
                    if third_row:
                        cert_results[3] = third_row["name"]

    # 参加者向け公開フォームのベースURL（フォーム方式の案内に使用）
    #   クラウド版：PUBLIC_BASE_URL + 店舗prefix（絶対URL）
    #   オンプレ版：空文字（相対パス＝同一ホストの /entry/{id}）
    from app.config import IS_CLOUD, PUBLIC_BASE_URL
    _store = getattr(request.state, "store", None)
    _pfx = _store.prefix if _store else ""
    public_base = (f"{PUBLIC_BASE_URL}{_pfx}" if (IS_CLOUD and PUBLIC_BASE_URL) else _pfx)

    return templates.TemplateResponse("admin/tournament_detail.html", {
        "request": request,
        "t": t,
        "race_assets": await _load_race_assets(tid, db),
        "entries": entries,
        "available_racers": available_racers,
        "pre_entries": pre_entries,
        "public_base": public_base,
        "is_junior": is_junior,
        "status_labels": STATUS_LABELS,
        "time_slot_labels": TIME_SLOT_LABELS,
        "regulation_labels": await get_regulation_labels(db),
        "qualifying_labels": QUALIFYING_LABELS,
        "finalists": finalists,
        "is_finalized": is_finalized,
        "has_finalists": has_finalists,
        "cert_results": cert_results,
        "post_templates": await _get_post_templates(db),
    })


async def _get_post_templates(db) -> list:
    """ポストテンプレート一覧を取得"""
    async with db.execute("SELECT id, name, body FROM post_templates ORDER BY id") as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── レース情報の画像アセット（コースレイアウト/タイムスケジュール/備考）──
_ASSET_KINDS = ("course", "schedule", "remarks")
_MAX_ASSET_IMG = 4


async def _load_race_assets(tid: int, db) -> dict:
    """{'course':[{seq,name,data_uri}], 'schedule':[...], 'remarks':[...]} を返す"""
    out = {k: [] for k in _ASSET_KINDS}
    async with db.execute(
        "SELECT kind, seq, name, data_uri FROM race_assets WHERE tournament_id=? ORDER BY kind, seq",
        (tid,),
    ) as cur:
        async for r in cur:
            if r["kind"] in out:
                out[r["kind"]].append({"seq": r["seq"], "name": r["name"] or "", "data_uri": r["data_uri"]})
    return out


async def _save_race_assets(tid: int, form, db) -> None:
    """作成/編集フォームのmultipartから画像アセットを保存（各種別 最大4枚・base64でDB格納）。
       スロットごとに：削除チェック→削除 / 新ファイルあり→置換 / なし→名前のみ更新。
       画像は保存時に長辺 667px（iPhone SE の縦）に収まるようリサイズする。"""
    import base64
    for kind in _ASSET_KINDS:
        for i in range(_MAX_ASSET_IMG):
            if form.get(f"{kind}_remove_{i}"):
                await db.execute(
                    "DELETE FROM race_assets WHERE tournament_id=? AND kind=? AND seq=?", (tid, kind, i)
                )
                continue
            name = (form.get(f"{kind}_name_{i}") or "").strip()
            up = form.get(f"{kind}_img_{i}")
            if up is not None and getattr(up, "filename", ""):
                data = await up.read()
                if data:
                    resized = _resize_image(data, 667)
                    if resized:
                        ctype, out = resized
                    else:
                        ctype = getattr(up, "content_type", None) or "image/png"
                        out = data
                    data_uri = "data:" + ctype + ";base64," + base64.b64encode(out).decode("ascii")
                    await db.execute(
                        "INSERT INTO race_assets (tournament_id, kind, seq, name, data_uri) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(tournament_id, kind, seq) DO UPDATE SET name=excluded.name, data_uri=excluded.data_uri",
                        (tid, kind, i, name, data_uri),
                    )
                    continue
            # ファイルなし → 既存があれば名前だけ更新
            await db.execute(
                "UPDATE race_assets SET name=? WHERE tournament_id=? AND kind=? AND seq=?",
                (name, tid, kind, i),
            )


def _resize_image(data: bytes, max_edge: int = 667):
    """画像を長辺 max_edge に収まるようリサイズして (content_type, bytes) を返す。
       画像として開けない場合は None。"""
    try:
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(data))
        fmt = (img.format or "PNG").upper()
        w, h = img.size
        longest = max(w, h)
        if longest > max_edge:
            scale = max_edge / float(longest)
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = _io.BytesIO()
        if fmt in ("JPEG", "JPG"):
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=85, optimize=True)
            return "image/jpeg", buf.getvalue()
        if fmt == "WEBP":
            img.save(buf, format="WEBP", quality=85)
            return "image/webp", buf.getvalue()
        img.save(buf, format="PNG", optimize=True)
        return "image/png", buf.getvalue()
    except Exception:
        return None


async def _is_result_finalized(tid: int, db) -> bool:
    """1位が決まっているか（結果確定済み）"""
    # bracket_slot_ranksに1位がある
    async with db.execute(
        """SELECT 1 FROM bracket_slot_ranks bsr
           JOIN bracket_slots bs ON bs.id=bsr.slot_id
           JOIN bracket_groups bg ON bg.id=bsr.group_id
           JOIN bracket_rounds br ON br.id=bg.round_id
           WHERE br.tournament_id=? AND bsr.rank=1 LIMIT 1""",
        (tid,),
    ) as cur:
        if await cur.fetchone():
            return True
    # ※ ヒートトーナメントの ht 決勝は「予選」であり、本来の結果確定（優勝確定）は
    #    決勝（bracket）の1位で判定する。ヒート決勝が決まっただけでエントリーをロックしない。
    #    （heat_roundrobin等の他形式は ht_rounds を使わないため影響なし）
    async with db.execute("SELECT qualifying_type FROM tournaments WHERE id=?", (tid,)) as cur:
        _qt_row = await cur.fetchone()
    _is_heat_tour = bool(_qt_row and _qt_row["qualifying_type"] in HEAT_TOURNAMENT_TYPES)
    if not _is_heat_tour:
        # ht_slot_ranksに1位がある（ヒートトーナメント以外で ht を使う形式の保険）
        async with db.execute(
            """SELECT 1 FROM ht_slot_ranks hsr
               JOIN ht_groups hg ON hg.id=hsr.group_id
               JOIN ht_rounds hr ON hr.id=hg.round_id
               WHERE hr.tournament_id=? AND hsr.rank=1
                 AND hr.round_type='final' LIMIT 1""",
            (tid,),
        ) as cur:
            if await cur.fetchone():
                return True
    # none_roundrobin: status='complete' で確定済み
    async with db.execute(
        "SELECT 1 FROM tournaments WHERE id=? AND status='complete' AND qualifying_type='none_roundrobin' LIMIT 1",
        (tid,),
    ) as cur:
        if await cur.fetchone():
            return True
    return False


@router.post("/{tid}/add-entry")
async def add_entry(
    tid: int,
    racer_id: int = Form(...),
    car_class: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    if await _is_result_finalized(tid, db):
        return RedirectResponse(url=f"/admin/tournaments/{tid}?error=finalized", status_code=303)
    # ジュニアレースはJrレーサーのみ
    async with db.execute("SELECT regulation FROM tournaments WHERE id=?", (tid,)) as cur:
        t_row = await cur.fetchone()
    if t_row and _is_junior_tournament(t_row["regulation"]):
        async with db.execute("SELECT is_child FROM racers WHERE id=?", (racer_id,)) as cur:
            r = await cur.fetchone()
        if not r or not r["is_child"]:
            return RedirectResponse(url=f"/admin/tournaments/{tid}?error=not_junior", status_code=303)
    async with db.execute(
        "SELECT COALESCE(MAX(entry_order),0)+1 FROM entries WHERE tournament_id=?", (tid,)
    ) as cur:
        row = await cur.fetchone()
        order = row[0]
    await db.execute(
        "INSERT INTO entries (tournament_id, racer_id, car_class, entry_order) VALUES (?,?,?,?)",
        (tid, racer_id, car_class, order),
    )
    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)


@router.post("/{tid}/add-entries-bulk")
async def add_entries_bulk(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """チェックボックスで選択した複数レーサーを一括追加"""
    if await _is_result_finalized(tid, db):
        return RedirectResponse(url=f"/admin/tournaments/{tid}?error=finalized", status_code=303)
    # ジュニアレース判定
    async with db.execute("SELECT regulation FROM tournaments WHERE id=?", (tid,)) as cur:
        t_row = await cur.fetchone()
    is_junior = t_row and _is_junior_tournament(t_row["regulation"])
    form = await request.form()
    racer_ids = [int(v) for k, v in form.multi_items() if k == "racer_ids"]
    for racer_id in racer_ids:
        # ジュニアレースはJrレーサーのみ
        if is_junior:
            async with db.execute("SELECT is_child FROM racers WHERE id=?", (racer_id,)) as cur:
                r = await cur.fetchone()
            if not r or not r["is_child"]:
                continue
        async with db.execute(
            "SELECT COALESCE(MAX(entry_order),0)+1 FROM entries WHERE tournament_id=?", (tid,)
        ) as cur:
            order = (await cur.fetchone())[0]
        await db.execute(
            "INSERT INTO entries (tournament_id, racer_id, car_class, entry_order) VALUES (?,?,?,?)",
            (tid, racer_id, "", order),
        )
    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)


@router.post("/{tid}/remove-entry/{entry_id}")
async def remove_entry(tid: int, entry_id: int, db: aiosqlite.Connection = Depends(get_db)):
    if await _is_result_finalized(tid, db):
        return RedirectResponse(url=f"/admin/tournaments/{tid}?error=finalized", status_code=303)

    # 削除対象の本エントリー情報（連番・紐づく隠しレーサー）を取得
    async with db.execute(
        "SELECT racer_id, pre_seq_no FROM entries WHERE id=? AND tournament_id=?",
        (entry_id, tid),
    ) as cur:
        ent = await cur.fetchone()

    if ent is None:
        return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)

    pre_seq_no = ent["pre_seq_no"]
    racer_id = ent["racer_id"]

    # 連番がある（事前エントリー／飛び込み由来）の場合は、対応する事前エントリーを
    # 受付前（checked_in=0）へ差し戻し、事前エントリー一覧へ戻す。
    if pre_seq_no is not None:
        await db.execute(
            "UPDATE pre_entries SET checked_in=0 WHERE tournament_id=? AND seq_no=?",
            (tid, pre_seq_no),
        )

    # 本エントリーを削除
    await db.execute("DELETE FROM entries WHERE id=?", (entry_id,))

    # 紐づく隠しレーサー（このレース専用）が他に使われていなければ後始末
    if racer_id is not None:
        async with db.execute(
            "SELECT COALESCE(ephemeral,0) AS eph FROM racers WHERE id=?", (racer_id,)
        ) as cur:
            r = await cur.fetchone()
        if r and r["eph"] == 1:
            async with db.execute(
                "SELECT COUNT(*) AS c FROM entries WHERE racer_id=?", (racer_id,)
            ) as cur:
                still = (await cur.fetchone())["c"]
            if still == 0:
                await db.execute("DELETE FROM racers WHERE id=?", (racer_id,))

    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)


@router.get("/{tid}/copyable-races")
async def copyable_races(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """エントリーコピー元の候補レース一覧（自身を除く同日含む過去5件）をJSONで返す。
    各レースの開催日・レース名・エントリー数を含む。"""
    async with db.execute(
        "SELECT date, use_racer_master FROM tournaments WHERE id=?", (tid,)
    ) as cur:
        cur_row = await cur.fetchone()
    if not cur_row:
        return JSONResponse({"ok": False, "error": "レースが見つかりません。", "races": []})
    base_date = cur_row["date"]
    # レーサーマスタ使用区分（NULL は使用=1 とみなす）。
    # コピー候補は「使用状況（use_racer_master）が現在レースと同じ」レースのみに限定する。
    cur_use_master = cur_row["use_racer_master"] if cur_row["use_racer_master"] is not None else 1
    # 自身を除き、開催日が基準日以前（同日含む）かつマスタ使用区分が一致するレースを、
    # 開催日降順→id降順で5件
    async with db.execute(
        """SELECT t.id, t.name, t.date,
                  (SELECT COUNT(*) FROM entries e WHERE e.tournament_id = t.id) AS entry_count
           FROM tournaments t
           WHERE t.id != ? AND t.date <= ?
                 AND COALESCE(t.use_racer_master, 1) = ?
           ORDER BY t.date DESC, t.id DESC
           LIMIT 5""",
        (tid, base_date, cur_use_master),
    ) as cur:
        rows = await cur.fetchall()
    races = [
        {"id": r["id"], "name": r["name"], "date": r["date"], "entry_count": r["entry_count"]}
        for r in rows
    ]
    return JSONResponse({"ok": True, "races": races})


@router.post("/{tid}/copy-entries/{src_tid}")
async def copy_entries(tid: int, src_tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """別レース（src_tid）のエントリーレーサーを現在レース（tid）へコピー追加する。
    - 既存エントリーは残し、コピー元を追加（既に居る人はスキップ）
    - 現在レースがJr.限定の場合、Jr.でないレーサーは除外
    """
    if await _is_result_finalized(tid, db):
        return JSONResponse({"ok": False, "error": "結果確定済みのため変更できません。"})

    # 現在レースのJr.判定とマスタ使用区分
    async with db.execute(
        "SELECT regulation, use_racer_master FROM tournaments WHERE id=?", (tid,)
    ) as cur:
        t_row = await cur.fetchone()
    if not t_row:
        return JSONResponse({"ok": False, "error": "コピー先レースが見つかりません。"})
    is_junior = _is_junior_tournament(t_row["regulation"])

    # レーサーマスタ使用区分が一致しないレースからのコピーは禁止
    # （NULL は使用=1 とみなす）。候補一覧でも除外しているが、直接POST対策の二重ガード。
    dst_use_master = t_row["use_racer_master"] if t_row["use_racer_master"] is not None else 1
    async with db.execute(
        "SELECT use_racer_master FROM tournaments WHERE id=?", (src_tid,)
    ) as cur:
        src_row = await cur.fetchone()
    if not src_row:
        return JSONResponse({"ok": False, "error": "コピー元レースが見つかりません。"})
    src_use_master = src_row["use_racer_master"] if src_row["use_racer_master"] is not None else 1
    if src_use_master != dst_use_master:
        return JSONResponse({
            "ok": False,
            "error": "レーサーマスタの使用状況が異なるレースからは追加できません。",
        })

    # コピー元のエントリーレーサー（entry_order順）
    async with db.execute(
        "SELECT racer_id FROM entries WHERE tournament_id=? ORDER BY entry_order, id",
        (src_tid,),
    ) as cur:
        src_racer_ids = [r["racer_id"] for r in await cur.fetchall()]

    # 現在レースに既にエントリー済みのレーサー（重複スキップ用）
    async with db.execute(
        "SELECT racer_id FROM entries WHERE tournament_id=?", (tid,)
    ) as cur:
        existing = {r["racer_id"] for r in await cur.fetchall()}

    added = 0
    skipped_dup = 0
    skipped_jr = 0
    for racer_id in src_racer_ids:
        if racer_id in existing:
            skipped_dup += 1
            continue
        if is_junior:
            async with db.execute("SELECT is_child FROM racers WHERE id=?", (racer_id,)) as cur:
                rc = await cur.fetchone()
            if not rc or not rc["is_child"]:
                skipped_jr += 1
                continue
        async with db.execute(
            "SELECT COALESCE(MAX(entry_order),0)+1 FROM entries WHERE tournament_id=?", (tid,)
        ) as cur:
            order = (await cur.fetchone())[0]
        await db.execute(
            "INSERT INTO entries (tournament_id, racer_id, car_class, entry_order) VALUES (?,?,?,?)",
            (tid, racer_id, "", order),
        )
        existing.add(racer_id)
        added += 1
    await db.commit()
    return JSONResponse({
        "ok": True,
        "added": added,
        "skipped_dup": skipped_dup,
        "skipped_jr": skipped_jr,
    })


@router.post("/{tid}/add-listed-entries")
async def add_listed_entries(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """レーサーマスタ非使用レース（use_racer_master=0）向け：
    改行区切りテキストで入力されたレーサー名を、そのレース専用の隠しレーサー
    （ephemeral=1, owner_tournament_id=tid）として racers に登録し、entries に紐づける。
    既にこのレースに同名の隠しレーサーがいる場合はスキップする（仕様書12.1）。"""
    if await _is_result_finalized(tid, db):
        return RedirectResponse(url=f"/admin/tournaments/{tid}?error=finalized", status_code=303)

    # マスタ非使用レースのみ対象
    async with db.execute("SELECT use_racer_master FROM tournaments WHERE id=?", (tid,)) as cur:
        t_row = await cur.fetchone()
    if not t_row:
        return RedirectResponse(url="/admin/tournaments/", status_code=303)
    use_master = t_row["use_racer_master"] if t_row["use_racer_master"] is not None else 1
    if use_master:
        # マスタ使用レースでは本エンドポイントは無効
        return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)

    form = await request.form()
    raw = form.get("racer_names", "") or ""
    # 改行区切り → 各行trim → 空行除外、入力順を保持しつつ同一入力内の重複は除外
    names = []
    seen_in_input = set()
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        nm = line.strip()
        if nm and nm not in seen_in_input:
            names.append(nm)
            seen_in_input.add(nm)

    # このレースに既に紐づく隠しレーサー名（重複登録防止）
    async with db.execute(
        """SELECT r.name FROM racers r
           JOIN entries e ON e.racer_id = r.id
           WHERE e.tournament_id = ? AND COALESCE(r.ephemeral,0) = 1""",
        (tid,),
    ) as cur:
        existing_names = {r["name"] for r in await cur.fetchall()}

    for nm in names:
        if nm in existing_names:
            continue
        # 隠しレーサーを作成
        cur = await db.execute(
            "INSERT INTO racers (name, yomi, is_child, is_regular, ephemeral, owner_tournament_id, uid) "
            "VALUES (?,?,?,?,?,?,?)",
            (nm, "", 0, 0, 1, tid, str(uuid.uuid4())),
        )
        new_racer_id = cur.lastrowid
        # エントリー紐付け
        async with db.execute(
            "SELECT COALESCE(MAX(entry_order),0)+1 FROM entries WHERE tournament_id=?", (tid,)
        ) as c2:
            order = (await c2.fetchone())[0]
        await db.execute(
            "INSERT INTO entries (tournament_id, racer_id, car_class, entry_order) VALUES (?,?,?,?)",
            (tid, new_racer_id, "", order),
        )
        existing_names.add(nm)
    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)


@router.post("/{tid}/add-pre-entries")
async def add_pre_entries(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """事前エントリーON（pre_entry=1）レース向け：
    改行区切りテキストで入力されたレーサー名を pre_entries（前段階テーブル）に登録する。
    連番 seq_no はレース内の登録順に自動採番する（エントリーカードのバーコード値）。
    ステップ1では name のみ登録（よみがな・連絡先等は後続ステップで対応）。
    同一レース内に同名の事前エントリーが既にある場合はスキップする。"""
    if await _is_result_finalized(tid, db):
        return RedirectResponse(url=f"/admin/tournaments/{tid}?error=finalized", status_code=303)

    # 事前エントリーONのレースのみ対象
    async with db.execute("SELECT pre_entry FROM tournaments WHERE id=?", (tid,)) as cur:
        t_row = await cur.fetchone()
    if not t_row:
        return RedirectResponse(url="/admin/tournaments/", status_code=303)
    pre_on = t_row["pre_entry"] if t_row["pre_entry"] is not None else 0
    if not pre_on:
        return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)

    form = await request.form()
    raw = form.get("racer_names", "") or ""
    # 改行区切り → 各行trim → 空行除外、入力順を保持しつつ同一入力内の重複は除外
    names = []
    seen_in_input = set()
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        nm = line.strip()
        if nm and nm not in seen_in_input:
            names.append(nm)
            seen_in_input.add(nm)

    # このレースに既に登録済みの事前エントリー名（重複登録防止）
    async with db.execute(
        "SELECT name FROM pre_entries WHERE tournament_id=?", (tid,)
    ) as cur:
        existing_names = {r["name"] for r in await cur.fetchall()}

    # 連番の現在最大値
    async with db.execute(
        "SELECT COALESCE(MAX(seq_no),0) AS mx FROM pre_entries WHERE tournament_id=?", (tid,)
    ) as cur:
        seq = (await cur.fetchone())["mx"]

    for nm in names:
        if nm in existing_names:
            continue
        seq += 1
        await db.execute(
            "INSERT INTO pre_entries (tournament_id, seq_no, name) VALUES (?,?,?)",
            (tid, seq, nm),
        )
        existing_names.add(nm)
    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)


@router.post("/{tid}/remove-pre-entry/{pre_id}")
async def remove_pre_entry(tid: int, pre_id: int, db: aiosqlite.Connection = Depends(get_db)):
    """事前エントリーを1件削除する。連番（seq_no）は欠番のまま維持する
    （バーコードとの対応を崩さないため再採番しない）。"""
    if await _is_result_finalized(tid, db):
        return RedirectResponse(url=f"/admin/tournaments/{tid}?error=finalized", status_code=303)
    await db.execute(
        "DELETE FROM pre_entries WHERE id=? AND tournament_id=?", (pre_id, tid)
    )
    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)


@router.get("/{tid}/entry-cards", response_class=HTMLResponse)
async def entry_cards(
    tid: int,
    request: Request,
    extra: int = Query(10),
    db: aiosqlite.Connection = Depends(get_db),
):
    """エントリーカード印刷ページ。
    事前エントリー（pre_entries）のカードに加え、飛び込み用の空白カードを
    extra 枚ぶん、事前エントリーの続き番号で採番して出力する。
    各カードには大会名・QR/CODE128・10桁コード（数字併記）・レーサー名を載せる。
    QR/CODE128 の選択はクライアント側で切り替える。"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url="/admin/tournaments/")

    # 事前エントリー（連番順）
    async with db.execute(
        "SELECT seq_no, name FROM pre_entries WHERE tournament_id=? ORDER BY seq_no",
        (tid,),
    ) as cur:
        pre_rows = await cur.fetchall()

    cards = []
    for r in pre_rows:
        seq = r["seq_no"]
        cards.append({
            "seq_no": seq,
            "name": r["name"],
            "code": build_code(tid, seq),
            "blank": False,
        })

    # 飛び込み用の空白カード：事前エントリーの続き番号から採番
    max_seq = pre_rows[-1]["seq_no"] if pre_rows else 0
    try:
        extra_n = max(0, min(int(extra), 9999))
    except (TypeError, ValueError):
        extra_n = 0
    for i in range(1, extra_n + 1):
        seq = max_seq + i
        if seq > 9999:
            break
        cards.append({
            "seq_no": seq,
            "name": "",
            "code": build_code(tid, seq),
            "blank": True,
        })

    # カード印刷テンプレート（任意適用。未選択なら従来の標準レイアウト）
    import json as _json
    async with db.execute(
        "SELECT id, name, card_size, code_type, layout_json FROM card_templates ORDER BY id"
    ) as cur:
        ct_rows = await cur.fetchall()
    card_templates = []
    for row in ct_rows:
        d = dict(row)
        lj = (d.get("layout_json") or "").strip()
        layout = {}
        if lj and lj not in ("", "{}", "null"):
            try:
                parsed = _json.loads(lj)
                if isinstance(parsed, dict):
                    layout = parsed
            except Exception:
                pass
        d["layout"] = layout
        d.pop("layout_json", None)
        card_templates.append(d)

    return templates.TemplateResponse("admin/entry_cards.html", {
        "request": request,
        "t": t,
        "cards": cards,
        "extra": extra_n,
        "card_templates": card_templates,
    })


async def _promote_pre_entry_to_entry(tid: int, name: str, db, is_child: int = 0, seq_no: int = None) -> int:
    """名前を隠しレーサー（ephemeral）として racers に登録し、entries に紐付けて
    本エントリーへ昇格させる。seq_no を渡すとエントリーカードのバーコード番号として記録する。
    既に同名の隠しレーサーがこのレースにいる場合はそれを使う。
    戻り値: entry_id"""
    # 既存の隠しレーサーを探す
    async with db.execute(
        """SELECT r.id FROM racers r
           JOIN entries e ON e.racer_id = r.id
           WHERE e.tournament_id = ? AND COALESCE(r.ephemeral,0) = 1 AND r.name = ?
           LIMIT 1""",
        (tid, name),
    ) as cur:
        existing = await cur.fetchone()
    if existing:
        return None  # 既にエントリー済み

    cur = await db.execute(
        "INSERT INTO racers (name, yomi, is_child, is_regular, ephemeral, owner_tournament_id, uid) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, "", is_child, 0, 1, tid, str(uuid.uuid4())),
    )
    new_racer_id = cur.lastrowid
    async with db.execute(
        "SELECT COALESCE(MAX(entry_order),0)+1 FROM entries WHERE tournament_id=?", (tid,)
    ) as c2:
        order = (await c2.fetchone())[0]
    cur = await db.execute(
        "INSERT INTO entries (tournament_id, racer_id, car_class, entry_order, pre_seq_no) VALUES (?,?,?,?,?)",
        (tid, new_racer_id, "", order, seq_no),
    )
    return cur.lastrowid


@router.post("/{tid}/scan-reception")
async def scan_reception(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """受付スキャン（非同期）。スキャンされた10桁コードを検証し、
    該当する事前エントリーを本エントリーへ昇格＆受付済みにする。
    結果は JSON で返す（status と表示用メッセージ）。

    status の種類:
      ok           : 受付完了（事前エントリー→本エントリー昇格）
      already      : 受付済み（二重スキャン）
      blank        : 空白カード＝飛び込み（クライアントで名前入力を促す）
      invalid      : チェックディジット等の読み取りエラー
      other_race   : 別レースのカード
      not_found    : レース不明等
    """
    from app.services.barcode import parse_code
    form = await request.form()
    code = (form.get("code", "") or "").strip()

    parsed = parse_code(code)
    if not parsed["valid"]:
        return JSONResponse({"status": "invalid", "message": parsed["reason"], "code": code})

    if parsed["race_id"] != tid:
        return JSONResponse({
            "status": "other_race",
            "message": f"別レースのカードです（レースID {parsed['race_id']}）",
            "code": code,
        })

    seq = parsed["seq_no"]
    # 該当する事前エントリーを検索
    async with db.execute(
        "SELECT id, name, checked_in FROM pre_entries WHERE tournament_id=? AND seq_no=?",
        (tid, seq),
    ) as cur:
        pe = await cur.fetchone()

    if pe is None:
        # 事前エントリーに存在しない連番＝飛び込み用空白カード
        return JSONResponse({
            "status": "blank",
            "message": "空白カード（飛び込み）です。名前を入力してください。",
            "code": code, "seq_no": seq,
        })

    if pe["checked_in"]:
        return JSONResponse({
            "status": "already",
            "message": f"受付済みです（連番{seq:04d} {pe['name']}）",
            "code": code, "seq_no": seq, "name": pe["name"],
        })

    # 本エントリーへ昇格
    await _promote_pre_entry_to_entry(tid, pe["name"], db, seq_no=seq)
    await db.execute("UPDATE pre_entries SET checked_in=1 WHERE id=?", (pe["id"],))
    await db.commit()
    return JSONResponse({
        "status": "ok",
        "message": f"受付完了（連番{seq:04d} {pe['name']}）",
        "code": code, "seq_no": seq, "name": pe["name"],
    })


@router.post("/{tid}/scan-walkin")
async def scan_walkin(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """飛び込み受付（非同期）。空白カードのコード＋手入力された名前を受け取り、
    pre_entries に受付済みで登録しつつ、本エントリーへ昇格させる。"""
    from app.services.barcode import parse_code
    form = await request.form()
    code = (form.get("code", "") or "").strip()
    name = (form.get("name", "") or "").strip()

    if not name:
        return JSONResponse({"status": "invalid", "message": "名前が空です", "code": code})

    parsed = parse_code(code)
    if not parsed["valid"] or parsed["race_id"] != tid:
        return JSONResponse({"status": "invalid", "message": "コードが不正です", "code": code})

    seq = parsed["seq_no"]
    # 既に同じ連番の事前エントリーがあれば二重登録しない
    async with db.execute(
        "SELECT id, checked_in FROM pre_entries WHERE tournament_id=? AND seq_no=?",
        (tid, seq),
    ) as cur:
        pe = await cur.fetchone()
    if pe and pe["checked_in"]:
        return JSONResponse({"status": "already", "message": f"受付済みです（連番{seq:04d}）", "code": code})

    if pe is None:
        # 飛び込みを pre_entries に受付済み・飛び込みフラグ付きで登録
        await db.execute(
            "INSERT INTO pre_entries (tournament_id, seq_no, name, checked_in, is_walkin) VALUES (?,?,?,1,1)",
            (tid, seq, name),
        )
    else:
        await db.execute(
            "UPDATE pre_entries SET name=?, checked_in=1 WHERE id=?", (name, pe["id"])
        )

    # 本エントリーへ昇格
    await _promote_pre_entry_to_entry(tid, name, db, seq_no=seq)
    await db.commit()
    return JSONResponse({
        "status": "ok",
        "message": f"飛び込み受付完了（連番{seq:04d} {name}）",
        "code": code, "seq_no": seq, "name": name,
    })


@router.post("/{tid}/pre-entries/{pre_id}/rename")
async def rename_pre_entry(tid: int, pre_id: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """事前エントリーのレーサー名を変更する（受付前のみ）。"""
    if await _is_result_finalized(tid, db):
        return JSONResponse({"ok": False, "message": "確定済みのため変更できません"})
    form = await request.form()
    name = (form.get("name", "") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "message": "名前が空です"})
    async with db.execute(
        "SELECT id, checked_in FROM pre_entries WHERE tournament_id=? AND id=?",
        (tid, pre_id),
    ) as cur:
        pe = await cur.fetchone()
    if pe is None:
        return JSONResponse({"ok": False, "message": "対象が見つかりません"})
    if pe["checked_in"]:
        return JSONResponse({"ok": False, "message": "受付済みのため変更できません"})
    await db.execute("UPDATE pre_entries SET name=? WHERE id=?", (name, pre_id))
    await db.commit()
    return JSONResponse({"ok": True, "name": name})


@router.post("/{tid}/pre-entries/{pre_id}/check-in")
async def checkin_pre_entry(tid: int, pre_id: int, db: aiosqlite.Connection = Depends(get_db)):
    """事前エントリーを本エントリーへ昇格＆受付済みにする（コードスキャン受付と同義）。"""
    if await _is_result_finalized(tid, db):
        return JSONResponse({"ok": False, "message": "確定済みのため受付できません"})
    async with db.execute(
        "SELECT id, seq_no, name, checked_in FROM pre_entries WHERE tournament_id=? AND id=?",
        (tid, pre_id),
    ) as cur:
        pe = await cur.fetchone()
    if pe is None:
        return JSONResponse({"ok": False, "message": "対象が見つかりません"})
    if pe["checked_in"]:
        return JSONResponse({
            "ok": False, "status": "already",
            "message": f"受付済みです（連番{pe['seq_no']:04d} {pe['name']}）",
        })
    await _promote_pre_entry_to_entry(tid, pe["name"], db, seq_no=pe["seq_no"])
    await db.execute("UPDATE pre_entries SET checked_in=1 WHERE id=?", (pe["id"],))
    await db.commit()
    return JSONResponse({
        "ok": True, "status": "ok", "seq_no": pe["seq_no"], "name": pe["name"],
        "message": f"受付完了（連番{pe['seq_no']:04d} {pe['name']}）",
    })


@router.post("/{tid}/entries/{entry_id}/rename-racer")
async def rename_entry_racer(tid: int, entry_id: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """エントリー済みレーサーの名前を変更する（レーサーマスタ未使用＝ephemeralレーサーのみ）。"""
    if await _is_result_finalized(tid, db):
        return JSONResponse({"ok": False, "message": "確定済みのため変更できません"})
    form = await request.form()
    name = (form.get("name", "") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "message": "名前が空です"})
    async with db.execute(
        """SELECT r.id AS racer_id, COALESCE(r.ephemeral,0) AS ephemeral
           FROM entries e JOIN racers r ON r.id = e.racer_id
           WHERE e.tournament_id=? AND e.id=?""",
        (tid, entry_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return JSONResponse({"ok": False, "message": "対象が見つかりません"})
    if not row["ephemeral"]:
        return JSONResponse({"ok": False, "message": "マスタ登録レーサーはここでは変更できません"})
    await db.execute("UPDATE racers SET name=? WHERE id=?", (name, row["racer_id"]))
    await db.commit()
    return JSONResponse({"ok": True, "name": name})


@router.post("/{tid}/status")
async def update_status(
    tid: int,
    status: str = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
):
    await db.execute("UPDATE tournaments SET status=? WHERE id=?", (status, tid))
    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)


@router.post("/{tid}/delete")
async def tournament_delete(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    async with db.execute("SELECT status FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url="/admin/tournaments/", status_code=303)
    if await _is_result_finalized(tid, db):
        return RedirectResponse(url=f"/admin/tournaments/{tid}?error=finalized", status_code=303)
    async with db.execute("SELECT id FROM heats WHERE tournament_id=?", (tid,)) as cur:
        hids = [r["id"] for r in await cur.fetchall()]
    if hids:
        ph = ",".join("?" * len(hids))
        await db.execute(f"DELETE FROM heat_results WHERE heat_lane_id IN (SELECT id FROM heat_lanes WHERE heat_id IN ({ph}))", hids)
        await db.execute(f"DELETE FROM heat_lanes WHERE heat_id IN ({ph})", hids)
        await db.execute(f"DELETE FROM heats WHERE id IN ({ph})", hids)
    await db.execute("DELETE FROM entries WHERE tournament_id=?", (tid,))
    # 並び順（勝ち抜け・v6.0b）専用テーブル
    await db.execute("DELETE FROM order_winner_racers WHERE tournament_id=?", (tid,))
    await db.execute("DELETE FROM order_winner_stages WHERE tournament_id=?", (tid,))
    # 並び順（ポイント制・v5.6）待機列
    await db.execute("DELETE FROM order_queue WHERE tournament_id=?", (tid,))
    # 決勝トーナメント設定
    await db.execute("DELETE FROM brackets WHERE tournament_id=?", (tid,))
    # マスタ非使用レースの隠しレーサー（このレース専用）も後始末（仕様書12.1）
    await db.execute(
        "DELETE FROM racers WHERE COALESCE(ephemeral,0)=1 AND owner_tournament_id=?", (tid,)
    )
    # 事前エントリー（前段階テーブル）も後始末
    await db.execute("DELETE FROM pre_entries WHERE tournament_id=?", (tid,))
    await db.execute("DELETE FROM tournaments WHERE id=?", (tid,))
    await db.commit()
    return RedirectResponse(url="/admin/tournaments/", status_code=303)


@router.get("/{tid}/edit", response_class=HTMLResponse)
async def tournament_edit_form(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url="/admin/tournaments/")
    if await _is_result_finalized(tid, db):
        return RedirectResponse(url=f"/admin/tournaments/{tid}?error=finalized", status_code=303)
    async with db.execute("SELECT COUNT(*) AS cnt FROM entries WHERE tournament_id=?", (tid,)) as cur:
        entry_count = (await cur.fetchone())["cnt"]
    # 並び順（勝ち抜け）の既存段階設定（編集画面の初期描画用にJSONで渡す）
    ow_stages = await load_order_winner_stages(tid, db)
    garappa_enabled = ((await _get_store1_name(db)).strip() == GARAPPA_STORE_NAME) \
        or (t["qualifying_type"] == "heat_tournament_garappa")
    return templates.TemplateResponse("admin/tournament_edit.html", {
        "request": request,
        "t": t,
        "entry_count": entry_count,
        "time_slot_labels": TIME_SLOT_LABELS,
        "regulation_labels": await get_regulation_labels(db),
        "qualifying_labels": QUALIFYING_LABELS,
        "order_winner_stages_json": json.dumps(ow_stages, ensure_ascii=False),
        "garappa_enabled": garappa_enabled,
        "race_assets": await _load_race_assets(tid, db),
    })


@router.post("/{tid}/edit")
async def tournament_edit_save(
    tid: int,
    request: Request,
    name: str = Form(...),
    time_slot: str = Form("day"),
    time_slot_free: str = Form(""),
    regulation: str = Form("open"),
    qualifying_type: str = Form("heat_tournament"),
    final_type: str = Form("tournament"),
    note: str = Form(""),
    qual_heat_count: int = Form(1),
    qual_heat_advance: int = Form(2),
    qual_group_count: int = Form(1),
    qual_group_advance: int = Form(2),
    qual_heat_final: int = Form(0),
    qual_heat_final_advance: int = Form(1),
    qual_final_advance: int = Form(2),
    point_1st: int = Form(3),
    point_2nd: int = Form(2),
    point_3rd: int = Form(1),
    point_co: int = Form(0),
    qual_round_count: int = Form(1),
    qual_heat_exclude: int = Form(0),
    order_round_mode: str = Form("free"),
    order_round_count: int = Form(3),
    order_free_max_runs: int = Form(0),
    use_racer_master: int = Form(1),
    time_schedule: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    async with db.execute("SELECT status FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url="/admin/tournaments/", status_code=303)
    if await _is_result_finalized(tid, db):
        return RedirectResponse(url=f"/admin/tournaments/{tid}?error=finalized", status_code=303)

    # マスタ使用フラグの切替は、エントリーが無いときのみ許可する。
    # 既にエントリーがある場合は整合性のため現状維持。
    async with db.execute("SELECT COUNT(*) AS cnt FROM entries WHERE tournament_id=?", (tid,)) as cur:
        entry_cnt = (await cur.fetchone())["cnt"]
    if entry_cnt > 0:
        async with db.execute("SELECT use_racer_master FROM tournaments WHERE id=?", (tid,)) as cur:
            cur_um = await cur.fetchone()
        use_racer_master = cur_um["use_racer_master"] if cur_um and cur_um["use_racer_master"] is not None else 1

    await db.execute(
        """UPDATE tournaments SET
           name=?, time_slot=?, time_slot_free=?, regulation=?,
           qualifying_type=?, final_type=?, note=?, time_schedule=?,
           qual_heat_count=?, qual_heat_advance=?,
           qual_group_count=?, qual_group_advance=?,
           qual_heat_final=?, qual_heat_final_advance=?, qual_final_advance=?,
           point_1st=?, point_2nd=?, point_3rd=?, point_co=?,
           qual_round_count=?, qual_heat_exclude=?,
           order_round_mode=?, order_round_count=?, order_free_max_runs=?, use_racer_master=?
           WHERE id=?""",
        (name, time_slot, time_slot_free, regulation,
         qualifying_type, final_type, note, time_schedule,
         qual_heat_count, qual_heat_advance,
         qual_group_count, qual_group_advance,
         qual_heat_final, qual_heat_final_advance, qual_final_advance,
         point_1st, point_2nd, point_3rd, point_co, qual_round_count, qual_heat_exclude,
         order_round_mode, order_round_count, order_free_max_runs, use_racer_master, tid),
    )
    # レース情報の画像アセットを保存
    await _save_race_assets(tid, await request.form(), db)
    # 並び順（勝ち抜け）の段階設定を保存（段階数の増減に対応。全削除→入れ直し）
    if qualifying_type == "order_winner":
        stages = parse_order_winner_stages(await request.form())
        await save_order_winner_stages(tid, stages, db)
    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)


@router.post("/{tid}/edit-final")
async def tournament_edit_final(
    tid: int,
    final_type: str = Form("tournament"),
    db: aiosqlite.Connection = Depends(get_db),
):
    async with db.execute("SELECT status FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or t["status"] != "qualifying":
        return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)
    await db.execute("UPDATE tournaments SET final_type=? WHERE id=?", (final_type, tid))
    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}", status_code=303)


@router.get("/{tid}/certificate", response_class=HTMLResponse)
async def tournament_certificate(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """賞状印刷ページ（テンプレート切り替え対応）"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(f"/admin/tournaments/{tid}")

    qt = dict(t).get("qualifying_type", "")

    # 1〜3位を取得
    results = {}
    if qt == "none_roundrobin":
        async with db.execute(
            """SELECT e.none_rr_rank as rank, r.name
               FROM entries e JOIN racers r ON r.id=e.racer_id
               WHERE e.tournament_id=? AND e.none_rr_rank IN (1,2,3)
               ORDER BY e.none_rr_rank""", (tid,)
        ) as cur:
            for row in await cur.fetchall():
                results[row["rank"]] = row["name"]
    else:
        async with db.execute(
            """SELECT bsr.rank, r.name
               FROM bracket_slot_ranks bsr
               JOIN bracket_slots bs ON bs.id=bsr.slot_id
               JOIN bracket_groups bg ON bg.id=bs.group_id
               JOIN bracket_rounds br ON br.id=bg.round_id
               JOIN entries e ON e.id=bs.entry_id
               JOIN racers r ON r.id=e.racer_id
               WHERE br.tournament_id=? AND bsr.rank IN (1,2,3)
               ORDER BY bsr.rank""", (tid,)
        ) as cur:
            for row in await cur.fetchall():
                if row["rank"] not in results:
                    results[row["rank"]] = row["name"]

    # テンプレート一覧を取得（layout_jsonをPythonオブジェクトとしてパース済みで渡す）
    import json as _json
    async with db.execute(
        "SELECT id, name, paper_size, orientation, layout_json FROM certificate_templates ORDER BY id"
    ) as cur:
        raw_templates = await cur.fetchall()

    cert_templates = []
    for row in raw_templates:
        d = dict(row)
        lj = (d.get("layout_json") or "").strip()
        layout = {}
        if lj and lj not in ("", "{}", "null"):
            try:
                parsed = _json.loads(lj)
                if isinstance(parsed, dict):
                    layout = parsed
            except Exception:
                pass
        d["layout"] = layout
        cert_templates.append(d)

    from datetime import date
    today = date.today()

    return templates.TemplateResponse("admin/certificate.html", {
        "request": request,
        "t": t,
        "results": results,
        "today": today,
        "cert_templates": cert_templates,
    })
