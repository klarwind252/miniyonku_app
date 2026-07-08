from fastapi import APIRouter, Request, Depends, Form, Query, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
import aiosqlite
import os
import io
import csv
import uuid
from datetime import date, datetime

import json as _json
from app.models.database import get_db
from app.routers.tournaments import _is_result_finalized

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))
from app.config import inject_globals
inject_globals(templates)

VALID_SORT = {"name": "name", "yomi": "yomi", "created_at": "created_at",
              "is_regular": "is_regular"}
VALID_ORDER = {"asc": "ASC", "desc": "DESC"}
# DB列ではなくPython側で計算してから並べ替える列（前回入賞/前回優勝/前回来店）
COMPUTED_SORT = {"podium": "last_podium_date",
                 "win": "last_win_date",
                 "entry": "last_entry_date"}

# 五十音「行」フィルタ用（先頭1文字一致）。濁点/半濁点/小書きも各行に含める。
KANA_ROWS = {
    "あ": "あいうえおぁぃぅぇぉゔ",
    "か": "かきくけこがぎぐげごゕゖ",
    "さ": "さしすせそざじずぜぞ",
    "た": "たちつてとだぢづでどっ",
    "な": "なにぬねの",
    "は": "はひふへほばびぶべぼぱぴぷぺぽ",
    "ま": "まみむめも",
    "や": "やゆよゃゅょ",
    "ら": "らりるれろ",
    "わ": "わをんゎ",
}
KANA_ROW_ORDER = ["あ", "か", "さ", "た", "な", "は", "ま", "や", "ら", "わ"]
_CHAR2ROW = {ch: row for row, chars in KANA_ROWS.items() for ch in chars}


def _kana_row_of(s: str | None):
    """文字列の先頭1文字が属する五十音「行」を返す（該当なしは None）。
    カタカナはひらがなに正規化して判定する。"""
    if not s:
        return None
    ch = s[0]
    o = ord(ch)
    if 0x30A1 <= o <= 0x30F6:      # カタカナ → ひらがな
        ch = chr(o - 0x60)
    return _CHAR2ROW.get(ch)


def _is_junior_tournament(regulation: str | None) -> bool:
    """レギュレーション文字列に「ジュニア」「junior」「Jr」が含まれるか"""
    if not regulation:
        return False
    reg = regulation.lower()
    return "ジュニア" in regulation or "junior" in reg or "jr" in reg


async def _get_today_context(db: aiosqlite.Connection):
    """本日のレース・エントリー状況を取得してテンプレート用データを返す"""
    today = date.today().isoformat()

    # 本日のレース一覧（regulation も取得）
    async with db.execute(
        """SELECT id, name, regulation
           FROM tournaments
           WHERE date = ? AND status != 'finished'
           ORDER BY id""",
        (today,),
    ) as cur:
        today_tournaments = await cur.fetchall()

    # racer_id -> {tournament_id: entry_at(str)} の辞書
    entry_map: dict[int, dict[int, str]] = {}
    entered_racer_ids: set[int] = set()

    if today_tournaments:
        t_ids = [t["id"] for t in today_tournaments]
        placeholders = ",".join("?" * len(t_ids))
        async with db.execute(
            f"""SELECT e.racer_id, e.tournament_id,
                       COALESCE(e.entry_at, '') as entry_at
                FROM entries e
                WHERE e.tournament_id IN ({placeholders})""",
            t_ids,
        ) as cur:
            for row in await cur.fetchall():
                rid = row["racer_id"]
                tid = row["tournament_id"]
                entry_map.setdefault(rid, {})[tid] = row["entry_at"]
                entered_racer_ids.add(rid)

    # 本日来店したレーサーのみ（last_visit_at が本日）
    async with db.execute(
        """SELECT id, name, yomi, is_child, last_visit_at
           FROM racers
           WHERE last_visit_at >= ? AND last_visit_at < ?
             AND COALESCE(ephemeral,0) = 0
           ORDER BY last_visit_at""",
        (today + " 00:00:00", today + " 23:59:59"),
    ) as cur:
        today_racers = await cur.fetchall()

    # 結果確定済みのレースIDセット
    finalized_tids = set()
    for t in today_tournaments:
        if await _is_result_finalized(t["id"], db):
            finalized_tids.add(t["id"])

    return {
        "today_tournaments": today_tournaments,
        "today_racers": today_racers,
        "entry_map": entry_map,
        "finalized_tids": finalized_tids,
    }


@router.get("/", response_class=HTMLResponse)
async def racer_list(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    sort: str = Query("yomi"),
    order: str = Query("asc"),
    q: str = Query(""),
    kana: str = Query(""),
    regular: str = Query(""),
):
    # 計算列（前回入賞/優勝/来店）でのソートはSQLでは並べられないため、
    # SQL段階では既定（yomi）で取得し、値を計算した後にPython側で並べ替える。
    is_computed_sort = sort in COMPUTED_SORT
    sort_col = "yomi" if is_computed_sort else VALID_SORT.get(sort, "yomi")
    sort_ord = VALID_ORDER.get(order, "ASC")
    next_order = "desc" if order == "asc" else "asc"

    if q:
        sql = f"SELECT * FROM racers WHERE COALESCE(ephemeral,0)=0 AND (name LIKE ? OR yomi LIKE ?) ORDER BY {sort_col} {sort_ord}"
        params = (f"%{q}%", f"%{q}%")
    else:
        sql = f"SELECT * FROM racers WHERE COALESCE(ephemeral,0)=0 ORDER BY {sort_col} {sort_ord}"
        params = ()

    async with db.execute(sql, params) as cur:
        racers = await cur.fetchall()

    # 「常連」フィルタは先頭文字（kana）とは独立。regular指定時はkanaを無視する。
    if regular == "1":
        kana = ""

    # 五十音「行」フィルタ（先頭1文字一致 / name・yomi いずれか）
    if kana in KANA_ROWS:
        racers = [
            r for r in racers
            if kana in (_kana_row_of(r["name"]), _kana_row_of(r["yomi"]))
        ]
    else:
        kana = ""

    # 「常連」フィルタ（is_regular=1 のみ抽出）
    if regular == "1":
        racers = [r for r in racers if r["is_regular"]]
    else:
        regular = ""

    today_ctx = await _get_today_context(db)

    # 前回優勝日（決勝1位）・前回入賞日（決勝3位以内）を算出して各行へ付与
    # Row は読み取り専用のため dict 化してキーを追加する
    racer_ids = [r["id"] for r in racers]
    award_map = await _last_award_dates(racer_ids, db)
    racers = [dict(r) for r in racers]
    for r in racers:
        a = award_map.get(r["id"], {})
        r["last_podium_date"] = a.get("podium")
        r["last_win_date"] = a.get("win")
        r["last_entry_date"] = a.get("entry")

    # 計算列（前回入賞/前回優勝/前回来店）でのソート。
    # 日付なし（None/空）は昇順・降順いずれでも常に末尾に固める。
    if is_computed_sort:
        key_field = COMPUTED_SORT[sort]
        reverse = (sort_ord == "DESC")
        present = [r for r in racers if r.get(key_field) not in (None, "")]
        absent = [r for r in racers if r.get(key_field) in (None, "")]
        present.sort(key=lambda row: row.get(key_field) or "", reverse=reverse)
        racers = present + absent

    # 料金計算用: 来店日の曜日/祝日タイプを判定
    try:
        import jpholiday as _jph
        _jph_available = True
    except ImportError:
        _jph_available = False
    from datetime import date as _date
    day_types = {}
    for r in racers:
        vd = (r["last_visit_at"] or "")[:10]
        if vd and vd not in day_types:
            try:
                d = _date.fromisoformat(vd)
                if _jph_available and _jph.is_holiday(d):
                    day_types[vd] = "holiday"
                elif d.weekday() == 5:
                    day_types[vd] = "saturday"
                elif d.weekday() == 6:
                    day_types[vd] = "sunday"
                else:
                    day_types[vd] = "weekday"
            except Exception:
                day_types[vd] = "weekday"

    # entry_map と today_tournament_ids をJSONに変換
    entry_map_for_js = {
        str(rid): {str(tid): ts for tid, ts in entries.items()}
        for rid, entries in today_ctx.get("entry_map", {}).items()
    }
    today_tid_list = [t["id"] for t in today_ctx.get("today_tournaments", [])]

    return templates.TemplateResponse("admin/racers.html", {
        "request": request,
        "racers": racers,
        "sort": sort,
        "order": order,
        "next_order": next_order,
        "q": q,
        "kana": kana,
        "kana_rows": KANA_ROW_ORDER,
        "regular": regular,
        "day_types": day_types,
        "entry_map_js": entry_map_for_js,
        "today_tournament_ids": today_tid_list,
        **today_ctx,
    })


async def _racer_list_response(request, db, error=None, form_name="", form_yomi="", form_is_child=0, form_is_regular=0):
    today_ctx = await _get_today_context(db)
    async with db.execute("SELECT * FROM racers WHERE COALESCE(ephemeral,0)=0 ORDER BY yomi, name") as cur:
        racers = await cur.fetchall()
    ctx = {
        "request": request,
        "racers": racers,
        "sort": "yomi",
        "order": "asc",
        "next_order": "desc",
        "q": "",
        "kana": "",
        "kana_rows": KANA_ROW_ORDER,
        "regular": "",
        **today_ctx,
    }
    if error:
        ctx.update({"error": error, "form_name": form_name,
                    "form_yomi": form_yomi, "form_is_child": form_is_child,
                    "form_is_regular": form_is_regular})
    return templates.TemplateResponse("admin/racers.html", ctx)


@router.post("/add")
async def racer_add(
    request: Request,
    name: str = Form(...),
    yomi: str = Form(""),
    is_child: str = Form(""),
    is_regular: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    name = name.strip()
    yomi = yomi.strip()
    is_child_val = 1 if is_child == "1" else 0
    is_regular_val = 1 if is_regular == "1" else 0

    async with db.execute("SELECT id FROM racers WHERE name = ?", (name,)) as cur:
        if await cur.fetchone():
            return await _racer_list_response(
                request, db,
                error=f"「{name}」は既に登録されています。",
                form_name=name, form_yomi=yomi, form_is_child=is_child_val,
                form_is_regular=is_regular_val,
            )

    await db.execute(
        "INSERT INTO racers (name, yomi, is_child, is_regular, uid) VALUES (?, ?, ?, ?, ?)",
        (name, yomi, is_child_val, is_regular_val, str(uuid.uuid4())),
    )
    await db.commit()
    return RedirectResponse(url="/admin/racers/", status_code=303)


@router.post("/delete/{racer_id}")
async def racer_delete(racer_id: int, db: aiosqlite.Connection = Depends(get_db)):
    await db.execute("DELETE FROM racers WHERE id = ?", (racer_id,))
    await db.commit()
    return RedirectResponse(url="/admin/racers/", status_code=303)


@router.post("/edit/{racer_id}")
async def racer_edit(
    request: Request,
    racer_id: int,
    name: str = Form(...),
    yomi: str = Form(""),
    is_child: str = Form(""),
    is_regular: str = Form(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    name = name.strip()
    yomi = yomi.strip()
    is_child_val = 1 if is_child == "1" else 0
    is_regular_val = 1 if is_regular == "1" else 0

    async with db.execute(
        "SELECT id FROM racers WHERE name = ? AND id != ?", (name, racer_id)
    ) as cur:
        if await cur.fetchone():
            return JSONResponse({"ok": False, "error": f"「{name}」は既に登録されています。"})

    await db.execute(
        "UPDATE racers SET name=?, yomi=?, is_child=?, is_regular=? WHERE id=?",
        (name, yomi, is_child_val, is_regular_val, racer_id),
    )
    await db.commit()
    return JSONResponse({"ok": True, "is_child": is_child_val, "is_regular": is_regular_val})


@router.post("/entry-single/{racer_id}/{tournament_id}")
async def entry_single(
    racer_id: int,
    tournament_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """エントリー表から1件だけエントリー追加"""
    async with db.execute(
        "SELECT id FROM entries WHERE tournament_id = ? AND racer_id = ?",
        (tournament_id, racer_id),
    ) as cur:
        if await cur.fetchone():
            return JSONResponse({"ok": False, "error": "既にエントリー済みです"})

    async with db.execute(
        "SELECT COALESCE(MAX(entry_order),0)+1 FROM entries WHERE tournament_id=?",
        (tournament_id,),
    ) as cur:
        order = (await cur.fetchone())[0]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO entries (tournament_id, racer_id, entry_order, entry_at) VALUES (?,?,?,?)",
        (tournament_id, racer_id, order, now),
    )
    await db.commit()
    return JSONResponse({"ok": True, "timestamp": now})


@router.post("/remove-entry-single/{racer_id}/{tournament_id}")
async def remove_entry_single(
    racer_id: int,
    tournament_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """エントリー表から1件だけエントリー取消"""
    await db.execute(
        "DELETE FROM entries WHERE tournament_id = ? AND racer_id = ?",
        (tournament_id, racer_id),
    )
    await db.commit()
    return JSONResponse({"ok": True})


@router.get("/visit-data-api")
async def visit_data_api(db: aiosqlite.Connection = Depends(get_db)):
    """料金計算用：来店状況・エントリー状況・曜日情報をJSON返却"""
    today = date.today().isoformat()

    # 本日のレース
    async with db.execute(
        "SELECT id FROM tournaments WHERE date = ? AND status != 'finished'", (today,)
    ) as cur:
        today_tids = [r["id"] for r in await cur.fetchall()]

    # エントリー状況
    entry_map = {}
    if today_tids:
        placeholders = ",".join("?" * len(today_tids))
        async with db.execute(
            f"SELECT racer_id, tournament_id FROM entries WHERE tournament_id IN ({placeholders})",
            today_tids,
        ) as cur:
            for row in await cur.fetchall():
                rid = str(row["racer_id"])
                tid = str(row["tournament_id"])
                entry_map.setdefault(rid, {})[tid] = True

    # 来店日の曜日/祝日判定
    try:
        import jpholiday as _jph
        _jph_ok = True
    except ImportError:
        _jph_ok = False

    from datetime import date as _date
    day_types = {}
    try:
        d = _date.fromisoformat(today)
        if _jph_ok and _jph.is_holiday(d):
            day_types[today] = "holiday"
        elif d.weekday() == 5:
            day_types[today] = "saturday"
        elif d.weekday() == 6:
            day_types[today] = "sunday"
        else:
            day_types[today] = "weekday"
    except Exception:
        day_types[today] = "weekday"

    # today_day_type（サーバー保存値を優先、なければ自動判定）
    from datetime import date as _date2
    _today = _date2.today().isoformat()
    async with db.execute("SELECT value FROM app_settings WHERE key='today_day_type'") as cur:
        row = await cur.fetchone()
        saved = row["value"] if row else ""
    if saved and ":" in saved:
        saved_date, saved_type = saved.split(":", 1)
        if saved_date == _today:
            day_types[_today] = saved_type

    return JSONResponse({
        "day_types": day_types,
        "entry_map": entry_map,
        "today_tournament_ids": [str(t) for t in today_tids],
        "today_day_type": day_types.get(_today, "weekday"),
    })


@router.post("/cancel-visit/{racer_id}")
async def cancel_visit(
    racer_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """来店取消: last_visit_at をNULLに戻し、本日のレースエントリーをすべて削除する"""
    today = date.today().isoformat()

    # last_visit_at をリセット
    await db.execute(
        "UPDATE racers SET last_visit_at = NULL WHERE id = ?",
        (racer_id,),
    )

    # 本日のレースエントリーを削除
    async with db.execute(
        "SELECT id FROM tournaments WHERE date = ?", (today,)
    ) as cur:
        today_tids = [r["id"] for r in await cur.fetchall()]

    if today_tids:
        placeholders = ",".join("?" * len(today_tids))
        await db.execute(
            f"DELETE FROM entries WHERE racer_id = ? AND tournament_id IN ({placeholders})",
            [racer_id] + today_tids,
        )

    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/entry-today/{racer_id}")
async def racer_entry_today(
    racer_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """本日開催予定のレースにエントリーする。
    - ジュニアレース（regulation に「ジュニア/junior/Jr」を含む）は is_child=1 のみ
    - それ以外は全員
    """
    today = date.today().isoformat()

    # レーサーの is_child を取得
    async with db.execute("SELECT is_child FROM racers WHERE id = ?", (racer_id,)) as cur:
        racer = await cur.fetchone()
    if not racer:
        return JSONResponse({"ok": False, "error": "レーサーが見つかりません"}, status_code=404)
    is_child = bool(racer["is_child"])

    # 本日のレース（regulation 付き）
    async with db.execute(
        "SELECT id, name, regulation FROM tournaments WHERE date = ? AND status != 'finished'",
        (today,),
    ) as cur:
        today_tournaments = await cur.fetchall()

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 来店日時を更新
    await db.execute(
        "UPDATE racers SET last_visit_at = ? WHERE id = ?",
        (now, racer_id),
    )

    entered = []
    skipped_junior = []

    for t in today_tournaments:
        tid = t["id"]
        is_junior = _is_junior_tournament(t["regulation"])

        # ジュニアレース かつ Jr.でない → スキップ
        if is_junior and not is_child:
            skipped_junior.append(t["name"])
            continue

        # 既エントリー確認
        async with db.execute(
            "SELECT id FROM entries WHERE tournament_id = ? AND racer_id = ?",
            (tid, racer_id),
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            continue

        async with db.execute(
            "SELECT COALESCE(MAX(entry_order),0)+1 FROM entries WHERE tournament_id=?",
            (tid,),
        ) as cur:
            order = (await cur.fetchone())[0]

        await db.execute(
            """INSERT INTO entries (tournament_id, racer_id, entry_order, entry_at)
               VALUES (?, ?, ?, ?)""",
            (tid, racer_id, order, now),
        )
        entered.append(t["name"])

    await db.commit()
    return JSONResponse({
        "ok": True,
        "entered": len(entered),
        "entered_names": entered,
        "skipped_junior": skipped_junior,
        "timestamp": now,
        "visit_at": now,
    })


# =====================================================================
#  レーサーマスタ インポート／エクスポート（第12.2章）
#  CSV列構成: uid, name, yomigana, is_junior （ヘッダー行あり）
#    - yomigana は内部カラム yomi に対応
#    - is_junior は内部カラム is_child に対応（0/1）
#  identity のみを対象とし、来店・料金・エントリー・隠しレーサーは対象外
# =====================================================================

async def _racers_has_column(db: aiosqlite.Connection, col: str) -> bool:
    async with db.execute("PRAGMA table_info(racers)") as cur:
        return col in {r["name"] async for r in cur}


def _norm(s) -> str:
    """突合用の正規化（前後空白除去）。"""
    return (s or "").strip()


@router.get("/export")
async def racers_export(db: aiosqlite.Connection = Depends(get_db)):
    """レーサーマスタ（隠しレーサーを除く）を identity のみCSVでダウンロードする。
    文字コード: BOM付きUTF-8 / 改行: CRLF / ファイル名: racers_YYYYMMDD.csv
    """
    # 隠しレーサー（ephemeral=1）が存在する構成なら除外する
    if await _racers_has_column(db, "ephemeral"):
        sql = ("SELECT uid, name, yomi, is_child FROM racers "
               "WHERE COALESCE(ephemeral, 0) = 0 ORDER BY yomi, name")
    else:
        sql = "SELECT uid, name, yomi, is_child FROM racers ORDER BY yomi, name"

    async with db.execute(sql) as cur:
        rows = await cur.fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")  # CRLF（Excel互換）
    writer.writerow(["uid", "name", "yomigana", "is_junior"])
    for r in rows:
        uid = r["uid"] or str(uuid.uuid4())  # 万一uid未設定でも空欄にしない
        writer.writerow([uid, r["name"], r["yomi"] or "", 1 if r["is_child"] else 0])

    # BOM付きUTF-8でエンコード
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    fname = f"racers_{date.today().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _decode_csv_bytes(raw: bytes) -> str:
    """インポートCSVを寛容にデコードする。
    BOM付き/なしUTF-8・ShiftJIS(cp932) のいずれも受理する。
    """
    # UTF-8 BOM
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    # UTF-8 を優先的に試し、ダメなら cp932
    for enc in ("utf-8", "cp932", "shift_jis"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_import_csv(text: str):
    """CSVテキストを行リストに変換する。ヘッダーの揺れも吸収する。
    返り値: [{"uid","name","yomigana","is_junior"}, ...], errors:list[str]
    """
    errors = []
    # 先頭のBOM残りを除去
    text = text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(text))
    try:
        all_rows = list(reader)
    except Exception as e:
        return [], [f"CSVの解析に失敗しました: {e}"]
    if not all_rows:
        return [], ["CSVが空です。"]

    header = [h.strip().lower() for h in all_rows[0]]
    # 列インデックスを名前で解決（順不同・余剰列に耐える）
    def idx(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    i_uid = idx("uid")
    i_name = idx("name", "名前", "氏名")
    i_yomi = idx("yomigana", "yomi", "よみがな", "よみ")
    i_jr = idx("is_junior", "is_child", "ジュニア", "junior")

    if i_name is None:
        return [], ["CSVに name 列が見つかりません。ヘッダー行（uid,name,yomigana,is_junior）を確認してください。"]

    rows = []
    for ln, raw in enumerate(all_rows[1:], start=2):
        if not any((c or "").strip() for c in raw):
            continue  # 空行スキップ
        def get(i):
            return raw[i].strip() if (i is not None and i < len(raw)) else ""
        name = get(i_name)
        if not name:
            errors.append(f"{ln}行目: name が空のためスキップしました。")
            continue
        jr_raw = get(i_jr)
        is_junior = 1 if jr_raw in ("1", "true", "True", "はい", "ジュニア") else 0
        rows.append({
            "uid": get(i_uid),
            "name": name,
            "yomigana": get(i_yomi),
            "is_junior": is_junior,
            "line": ln,
        })
    return rows, errors


async def _build_preview(db: aiosqlite.Connection, rows):
    """各行を既存レーサーと突合し、状態（new/id_match/name_match/conflict）を判定する。
    突合優先順位: uid一致 → よみがな+名前一致 → 新規
    """
    # 既存レーサーを取得（隠しレーサーは突合対象外）
    async with db.execute("SELECT id, uid, name, yomi, is_child FROM racers WHERE COALESCE(ephemeral,0)=0") as cur:
        existing = await cur.fetchall()
    by_uid = {}
    by_namekey = {}
    for e in existing:
        if e["uid"]:
            by_uid[e["uid"]] = e
        key = (_norm(e["yomi"]), _norm(e["name"]))
        by_namekey.setdefault(key, e)

    preview = []
    for row in rows:
        uid = _norm(row["uid"])
        name = _norm(row["name"])
        yomi = _norm(row["yomigana"])
        is_jr = row["is_junior"]
        state = "new"
        existing_match = None
        default_action = "import"  # new の既定は取り込む

        if uid and uid in by_uid:
            em = by_uid[uid]
            existing_match = em
            # uid一致。氏名等が食い違うなら競合
            if _norm(em["name"]) != name or _norm(em["yomi"]) != yomi or int(em["is_child"] or 0) != is_jr:
                state = "conflict"
                default_action = "skip"  # 競合の既定はスキップ（要確認）
            else:
                state = "id_match"
                default_action = "keep"  # 既存維持
        else:
            key = (yomi, name)
            if key in by_namekey:
                state = "name_match"
                existing_match = by_namekey[key]
                default_action = "skip"  # 名前のみ一致は安全側：既定で取り込まない
            else:
                state = "new"
                default_action = "import"

        preview.append({
            "line": row["line"],
            "uid": uid,
            "name": name,
            "yomigana": yomi,
            "is_junior": is_jr,
            "state": state,
            "default_action": default_action,
            "existing_id": existing_match["id"] if existing_match else None,
            "existing_name": existing_match["name"] if existing_match else None,
            "existing_yomi": (existing_match["yomi"] or "") if existing_match else None,
            "existing_is_junior": (1 if (existing_match and existing_match["is_child"]) else 0) if existing_match else None,
        })
    return preview


@router.post("/import-preview")
async def racers_import_preview(
    file: UploadFile = File(...),
    db: aiosqlite.Connection = Depends(get_db),
):
    """CSVを受け取り、突合結果のプレビューをJSONで返す（まだDBには反映しない）。"""
    raw = await file.read()
    if not raw:
        return JSONResponse({"ok": False, "error": "ファイルが空です。"}, status_code=400)
    text = _decode_csv_bytes(raw)
    rows, errors = _parse_import_csv(text)
    if not rows and errors:
        return JSONResponse({"ok": False, "error": " / ".join(errors)}, status_code=400)
    preview = await _build_preview(db, rows)
    counts = {"new": 0, "id_match": 0, "name_match": 0, "conflict": 0}
    for p in preview:
        counts[p["state"]] = counts.get(p["state"], 0) + 1
    return JSONResponse({
        "ok": True,
        "preview": preview,
        "counts": counts,
        "warnings": errors,
    })


@router.post("/import-commit")
async def racers_import_commit(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    """プレビューでユーザーが選択した行のみを racers へ反映する。
    受信JSON: {"rows": [{uid,name,yomigana,is_junior,action,existing_id}, ...]}
      action: "import"（新規登録）/ "overwrite"（既存を上書き）/ "skip"（無視）
    identity のみ反映。来店・料金・エントリーには一切影響しない。
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "リクエストの解析に失敗しました。"}, status_code=400)

    rows = payload.get("rows", [])
    added = 0
    updated = 0
    skipped = 0

    for row in rows:
        action = (row.get("action") or "skip").strip()
        name = _norm(row.get("name"))
        yomi = _norm(row.get("yomigana"))
        is_jr = 1 if int(row.get("is_junior") or 0) == 1 else 0
        uid = _norm(row.get("uid"))
        existing_id = row.get("existing_id")

        if action == "skip" or not name:
            skipped += 1
            continue

        if action == "overwrite" and existing_id:
            # 既存レコードを identity のみ上書き（uidは既存を維持）
            await db.execute(
                "UPDATE racers SET name=?, yomi=?, is_child=? WHERE id=?",
                (name, yomi, is_jr, existing_id),
            )
            updated += 1
        elif action == "import":
            # 新規登録。uidが空、または既存と衝突する場合は新規採番
            new_uid = uid
            if new_uid:
                async with db.execute("SELECT id FROM racers WHERE uid=?", (new_uid,)) as cur:
                    if await cur.fetchone():
                        new_uid = str(uuid.uuid4())
            else:
                new_uid = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO racers (name, yomi, is_child, uid) VALUES (?, ?, ?, ?)",
                (name, yomi, is_jr, new_uid),
            )
            added += 1
        else:
            skipped += 1

    await db.commit()
    return JSONResponse({"ok": True, "added": added, "updated": updated, "skipped": skipped})


# ===== レーサー実績（レース成績一覧） =====
async def _race_podium_racer_ids(tid: int, qualifying_type: str, db: aiosqlite.Connection) -> dict:
    """確定済みレースの 1〜3 位の racer_id を {rank: racer_id} で返す。
    賞状の順位算出（tournaments.py cert_results）と同じロジックを racer_id ベースで再現する。"""
    podium = {}
    if qualifying_type == "none_roundrobin":
        async with db.execute(
            """SELECT e.none_rr_rank AS rank, e.racer_id
               FROM entries e
               WHERE e.tournament_id=? AND e.none_rr_rank IN (1,2,3)
               ORDER BY e.none_rr_rank""", (tid,)
        ) as cur:
            for row in await cur.fetchall():
                podium.setdefault(row["rank"], row["racer_id"])
    else:
        async with db.execute(
            """SELECT bsr.rank, br.round_type, e.racer_id
               FROM bracket_slot_ranks bsr
               JOIN bracket_slots bs ON bs.id=bsr.slot_id
               JOIN bracket_groups bg ON bg.id=bs.group_id
               JOIN bracket_rounds br ON br.id=bg.round_id
               JOIN entries e ON e.id=bs.entry_id
               WHERE br.tournament_id=? AND bsr.rank IN (1,2,3)
               ORDER BY CASE br.round_type WHEN 'final' THEN 1 WHEN 'third' THEN 2 ELSE 3 END, bsr.rank""",
            (tid,)
        ) as cur:
            for row in await cur.fetchall():
                if row["round_type"] == "final":
                    podium.setdefault(row["rank"], row["racer_id"])
                elif row["round_type"] == "third":
                    if row["rank"] == 1 and 3 not in podium:
                        podium[3] = row["racer_id"]
        if 3 not in podium:
            async with db.execute(
                """SELECT e.racer_id
                   FROM bracket_results bres
                   JOIN bracket_slots bs ON bs.id=bres.winner_slot_id
                   JOIN bracket_groups bg ON bg.id=bres.group_id
                   JOIN bracket_rounds br ON br.id=bg.round_id
                   JOIN entries e ON e.id=bs.entry_id
                   WHERE br.tournament_id=? AND br.round_type='third'
                   LIMIT 1""", (tid,)
            ) as cur:
                tr = await cur.fetchone()
                if tr:
                    podium[3] = tr["racer_id"]
    return podium


async def _last_award_dates(racer_ids: list[int], db: aiosqlite.Connection) -> dict:
    """指定レーサー群について、前回優勝日（決勝1位）・前回入賞日（決勝3位以内）・
    前回来店日（エントリーした開催日の最新）を
    {racer_id: {"win": date|None, "podium": date|None, "entry": date|None}} で返す。
    確定済みレースを開催日降順で走査し、各レーサー最初に当たった日付を採用する。
    実績画面（racer_achievements）と同一の判定（_race_podium_racer_ids）を使用。"""
    result = {rid: {"win": None, "podium": None, "entry": None} for rid in racer_ids}
    if not racer_ids:
        return result
    target = set(racer_ids)

    placeholders = ",".join("?" for _ in racer_ids)

    # 前回来店（=最新エントリー開催日）: 確定状態に関係なく、各レーサーが
    # エントリーしているレースの開催日の最大値を取る
    async with db.execute(
        f"""SELECT e.racer_id AS rid, MAX(t.date) AS d
            FROM entries e
            JOIN tournaments t ON t.id = e.tournament_id
            WHERE e.racer_id IN ({placeholders})
            GROUP BY e.racer_id""",
        tuple(racer_ids),
    ) as cur:
        for row in await cur.fetchall():
            result[row["rid"]]["entry"] = row["d"]

    # 対象レーサーの誰かがエントリーしているレースのみ、開催日降順で取得
    async with db.execute(
        f"""SELECT DISTINCT t.id, t.date, t.qualifying_type
            FROM tournaments t
            JOIN entries e ON e.tournament_id = t.id
            WHERE e.racer_id IN ({placeholders})
            ORDER BY t.date DESC, t.id DESC""",
        tuple(racer_ids),
    ) as cur:
        cand = await cur.fetchall()

    # 全レーサーの win/podium が埋まったら打ち切るためのカウント
    need_win = set(target)
    need_podium = set(target)

    for t in cand:
        if not need_win and not need_podium:
            break
        if not await _is_result_finalized(t["id"], db):
            continue
        podium = await _race_podium_racer_ids(t["id"], t["qualifying_type"], db)
        # rank: racer_id を racer_id: rank に反転（対象レーサーのみ）
        for rk in (1, 2, 3):
            rid = podium.get(rk)
            if rid is None or rid not in target:
                continue
            if rid in need_podium:
                result[rid]["podium"] = t["date"]
                need_podium.discard(rid)
            if rk == 1 and rid in need_win:
                result[rid]["win"] = t["date"]
                need_win.discard(rid)
    return result


@router.get("/achievements/{racer_id}", response_class=HTMLResponse)
async def racer_achievements(
    racer_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    start: str = Query(""),
    end: str = Query(""),
):
    """レーサーのレース実績（別ウィンドウ表示）。
    結果確定済みのレースのみを対象とし、開催日（tournaments.date）で期間を絞る。
    期間の初期値は今日を基準に過去 1 年。"""
    # レーサー取得（隠しレーサーは対象外）
    async with db.execute(
        "SELECT id, name, yomi FROM racers WHERE id=? AND COALESCE(ephemeral,0)=0", (racer_id,)
    ) as cur:
        racer = await cur.fetchone()
    if not racer:
        return HTMLResponse("<p>レーサーが見つかりません。</p>", status_code=404)

    # 期間の既定値：今日を基準に過去1年
    today = date.today()
    if not end:
        end = today.isoformat()
    if not start:
        try:
            start = today.replace(year=today.year - 1).isoformat()
        except ValueError:
            # 2/29 対策
            start = today.replace(year=today.year - 1, day=28).isoformat()

    # 対象レーサーがエントリーしている全レース（期間内・開催日でフィルタ）
    async with db.execute(
        """SELECT t.id, t.name, t.date, t.qualifying_type
           FROM tournaments t
           JOIN entries e ON e.tournament_id = t.id
           WHERE e.racer_id = ? AND t.date >= ? AND t.date <= ?
           ORDER BY t.date DESC, t.id DESC""",
        (racer_id, start, end),
    ) as cur:
        cand = await cur.fetchall()

    rows = []          # 表示用 [{date, name, result_label, rank}]
    finalized_count = 0
    win_count = 0
    podium_count = 0
    last_win = None    # {date, name}

    for t in cand:
        # 結果確定済みのみ対象
        if not await _is_result_finalized(t["id"], db):
            continue
        finalized_count += 1
        podium = await _race_podium_racer_ids(t["id"], t["qualifying_type"], db)
        my_rank = None
        for rk in (1, 2, 3):
            if podium.get(rk) == racer_id:
                my_rank = rk
                break
        if my_rank == 1:
            win_count += 1
            podium_count += 1
            label = "1位"
            if last_win is None:  # cand は日付降順なので最初に当たったものが最新
                last_win = {"date": t["date"], "name": t["name"]}
        elif my_rank in (2, 3):
            podium_count += 1
            label = f"{my_rank}位"
        else:
            label = "━"
        rows.append({"date": t["date"], "name": t["name"], "result": label})

    win_rate = round(win_count / finalized_count * 100) if finalized_count else 0
    podium_rate = round(podium_count / finalized_count * 100) if finalized_count else 0

    return templates.TemplateResponse("admin/racer_achievements.html", {
        "request": request,
        "racer": racer,
        "start": start,
        "end": end,
        "rows": rows,
        "race_count": finalized_count,
        "win_rate": win_rate,
        "podium_rate": podium_rate,
        "last_win": last_win,
    })
