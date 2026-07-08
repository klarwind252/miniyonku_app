from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
import os, time, asyncio, uuid
import aiosqlite

from app.models.database import init_db, DB_PATH, get_db
from app.routers import admin, racers, tournaments
from app.routers.viewer import router as viewer_router
from app.routers.qualifying import router as qualifying_router
from app.routers.bracket import router as bracket_router
from app.config import IS_CLOUD, DEPLOY_MODE, PUBLIC_BASE_URL, ADMIN_TOKEN, VIEW_TOKEN
from app.auth import add_auth
from app.middleware_store import add_store_resolver

app = FastAPI(title="ミニ四駆レース管理システム", version="1.0.0")

# クラウド版のみ：固定トークン認証（店舗別）を有効化（オンプレ版では無効＝従来挙動）。
# ミドルウェアは「後に追加したものが外側（先に実行）」。店舗リゾルバを認証より外側に
# 置く必要があるため、add_auth を先、add_store_resolver を後に呼ぶ。
add_auth(app)
add_store_resolver(app)

BASE_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# 複数店舗（クラウド版）：スラッグ付き /static を配信する。
# BaseHTTPMiddleware による scope 書き換えは Mount(StaticFiles) のルーティングに
# 届かず 404 になるため、/{slug}/static/{path} を明示ルートで受けて
# アプリ共通の app/static から直接返す（/static は全店舗共通という既存方針どおり）。
_STATIC_ROOT = os.path.join(BASE_DIR, "static")

@app.get("/{slug}/static/{path:path}")
async def store_static(slug: str, path: str):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    full = os.path.normpath(os.path.join(_STATIC_ROOT, path))
    # ディレクトリトラバーサル防止（_STATIC_ROOT 配下のみ許可）
    if not (full == _STATIC_ROOT or full.startswith(_STATIC_ROOT + os.sep)):
        raise HTTPException(status_code=404)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404)
    return FileResponse(full)

# 賞状背景画像保存先（/static/cert_bg/ で配信されるため別マウント不要）
os.makedirs(os.path.join(BASE_DIR, "static", "cert_bg"), exist_ok=True)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

app.include_router(admin.router, prefix="/admin")
app.include_router(qualifying_router, prefix="/admin/tournaments")
app.include_router(bracket_router, prefix="/admin/tournaments")
app.include_router(racers.router, prefix="/admin/racers")
app.include_router(tournaments.router, prefix="/admin/tournaments")
app.include_router(viewer_router, prefix="/view")

# 複数店舗化（クラウド版）：店舗マスタ管理（店舗1のみ操作可・ルーター内でガード）
from app.routers.stores import router as stores_router
app.include_router(stores_router, prefix="/admin/stores")


# ---- ホーム画面アイコン（Webアプリ）: admin / view の動的 manifest ----
# 認証ミドルウェア下（/admin・/view プレフィックス）にあるため、認証済みセッション
# のみが取得できる。Cookie のトークンを読み取り、key 埋め込み起動が有効なら
# start_url に ?key=<token> を埋める（iOS のスタンドアロン起動時に再認証される）。
def _serve_manifest(request: Request, screen: str):
    from fastapi.responses import JSONResponse, Response
    from app import pwa
    from app.config import admin_cookie_name, view_cookie_name
    if not IS_CLOUD:
        return Response(status_code=404)
    settings = pwa.get_pwa_settings(request)
    if settings.get("pwa_enabled") != "1":
        return Response(status_code=404)
    store = getattr(request.state, "store", None)
    slug = store.slug if store else ""
    sid = store.id if store else None
    key = None
    if screen == "admin" and settings.get("pwa_keylaunch_admin") == "1":
        key = request.cookies.get(admin_cookie_name(sid), "") or None
    elif screen == "view" and settings.get("pwa_keylaunch_view") == "1":
        # view 画面は view トークン優先。無ければ admin トークン（admin でも観覧可のため）。
        key = (request.cookies.get(view_cookie_name(sid), "")
               or request.cookies.get(admin_cookie_name(sid), "")) or None
    data = pwa.build_manifest_dict(screen, settings, slug=slug, key=key)
    return JSONResponse(data, media_type="application/manifest+json")


@app.get("/admin/manifest.webmanifest")
async def admin_manifest(request: Request):
    return _serve_manifest(request, "admin")


@app.get("/view/manifest.webmanifest")
async def view_manifest(request: Request):
    return _serve_manifest(request, "view")


@app.post("/api/admin-heartbeat")
async def admin_heartbeat():
    return {"ok": True}


@app.on_event("startup")
async def startup():
    if IS_CLOUD:
        # 複数店舗化：レジストリ（control.db）を用意し、店舗1（既定店舗）を移行登録。
        # .env の ADMIN_TOKEN / VIEW_TOKEN は店舗1の初期トークンとして流用する。
        from app import registry
        registry.init_registry(default_admin_token=ADMIN_TOKEN, default_view_token=VIEW_TOKEN)
        stores = registry.list_stores(include_disabled=True)
        for st in stores:
            await init_db(st.db_path)
            await _fix_bracket_slots_on_startup(st.db_path)
            print(f"[APP] 店舗 init: id={st.id} slug='{st.slug or '(default)'}' "
                  f"name={st.name} db={st.db_path}", flush=True)
        print(f"[APP] 起動完了（クラウド版 / 複数店舗 {len(stores)}件 / "
              f"DEPLOY_MODE={DEPLOY_MODE}）"
              f"{' / ' + PUBLIC_BASE_URL if PUBLIC_BASE_URL else ''}", flush=True)
    else:
        # オンプレ版：従来どおり単一DB。
        await init_db()
        await _fix_bracket_slots_on_startup()
        print("[APP] 起動完了 → http://localhost:8000/admin/")


async def _fix_bracket_slots_on_startup(db_path: str = None):
    """
    ブラケットスロットの不整合を起動時に自動修正する。
    - シードスロットが勝者で上書きされた場合の復元
    - 勝者が正しいスロットに配置されていない場合の修正
    """
    if db_path is None:
        db_path = DB_PATH

    def _spread_indices(n_items, n_slots):
        if n_items <= 0 or n_slots <= 0: return []
        if n_items >= n_slots: return [i % n_slots for i in range(n_items)]
        if n_items == 1: return [0]
        return [round(i * (n_slots - 1) / (n_items - 1)) for i in range(n_items)]

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT id, bracket_mode FROM tournaments") as cur:
            tournaments = await cur.fetchall()

        for t in tournaments:
            tid = t["id"]
            b_mode = t["bracket_mode"] or "third_place"
            if b_mode == "revival":
                continue

            async with db.execute(
                "SELECT id, round_no, round_type FROM bracket_rounds WHERE tournament_id=? ORDER BY round_no",
                (tid,),
            ) as cur:
                rounds = await cur.fetchall()

            for i in range(1, len(rounds)):
                prev_rnd = rounds[i - 1]
                curr_rnd = rounds[i]
                if curr_rnd["round_type"] == "revival":
                    continue

                # 前ラウンドの勝者
                async with db.execute("""
                    SELECT bg.group_no, bs.entry_id FROM bracket_results bres
                    JOIN bracket_groups bg ON bg.id=bres.group_id
                    JOIN bracket_slots bs ON bs.id=bres.winner_slot_id
                    WHERE bg.round_id=? ORDER BY bg.group_no
                """, (prev_rnd["id"],)) as cur:
                    winners = await cur.fetchall()
                if not winners:
                    continue

                # 前ラウンド出場entry_id
                async with db.execute(
                    "SELECT DISTINCT bs.entry_id FROM bracket_slots bs "
                    "JOIN bracket_groups bg ON bg.id=bs.group_id "
                    "WHERE bg.round_id=? AND bs.entry_id IS NOT NULL",
                    (prev_rnd["id"],),
                ) as cur:
                    prev_eids = {r["entry_id"] for r in await cur.fetchall()}

                async with db.execute(
                    "SELECT entry_id FROM ht_finalist_seeds WHERE seeded=1"
                ) as cur:
                    ht_seed_eids = {r["entry_id"] for r in await cur.fetchall()}

                async with db.execute(
                    "SELECT id, group_no FROM bracket_groups WHERE round_id=? ORDER BY group_no",
                    (curr_rnd["id"],),
                ) as cur:
                    curr_groups = await cur.fetchall()

                async with db.execute("""
                    SELECT bs.id as slot_id, bg.group_no, bs.slot_no, bs.entry_id,
                           COALESCE(bs.seed_reserved, 0) as seed_reserved
                    FROM bracket_slots bs JOIN bracket_groups bg ON bg.id=bs.group_id
                    WHERE bg.round_id=? ORDER BY bg.group_no, bs.slot_no
                """, (curr_rnd["id"],)) as cur:
                    curr_slots = [dict(r) for r in await cur.fetchall()]

                # 各グループの最後のスロット（シード配置先）
                group_max_slot = {}
                for s in curr_slots:
                    gno = s["group_no"]
                    if gno not in group_max_slot or s["slot_no"] > group_max_slot[gno]["slot_no"]:
                        group_max_slot[gno] = dict(s)

                # シード選手（seeded=1 シード または seeded=2 スーパーシード で前ラウンド未出場）
                async with db.execute("""
                    SELECT e.id, e.entry_order FROM entries e
                    WHERE e.tournament_id=? AND e.status='active' AND e.seeded IN (1, 2)
                    ORDER BY e.entry_order
                """, (tid,)) as cur:
                    seeded_entries = await cur.fetchall()

                seeds_not_in_prev = [s for s in seeded_entries if s["id"] not in prev_eids]
                # seed_reserved=1 のスロット = 確定シード枠
                seeded_in_curr = {
                    s["entry_id"] for s in curr_slots
                    if s["entry_id"] and (s["entry_id"] in ht_seed_eids
                                          or s.get("seed_reserved")
                                          or s["entry_id"] not in prev_eids)
                }
                missing_seeds = [s for s in seeds_not_in_prev if s["id"] not in seeded_in_curr]

                changed = False

                # シードを復元
                if missing_seeds:
                    n_seeds = len(seeds_not_in_prev)
                    n_groups = len(curr_groups)
                    seed_indices = _spread_indices(n_seeds, n_groups)
                    for si, seed in enumerate(seeds_not_in_prev):
                        if seed["id"] in seeded_in_curr:
                            continue
                        target_gno = curr_groups[seed_indices[si]]["group_no"]
                        target = group_max_slot.get(target_gno)
                        if target:
                            await db.execute(
                                "UPDATE bracket_slots SET entry_id=? WHERE id=?",
                                (seed["id"], target["slot_id"]),
                            )
                            changed = True

                    # スロット再取得
                    async with db.execute("""
                        SELECT bs.id as slot_id, bg.group_no, bs.slot_no, bs.entry_id,
                               COALESCE(bs.seed_reserved, 0) as seed_reserved
                        FROM bracket_slots bs JOIN bracket_groups bg ON bg.id=bs.group_id
                        WHERE bg.round_id=? ORDER BY bg.group_no, bs.slot_no
                    """, (curr_rnd["id"],)) as cur:
                        curr_slots = [dict(r) for r in await cur.fetchall()]

                # 非シードスロットへの勝者配置を確認・修正
                non_seed_slots = [
                    s for s in curr_slots
                    if not (s.get("seed_reserved") or
                            (s["entry_id"] and s["entry_id"] in ht_seed_eids))
                ]

                needs_fix = any(
                    gi < len(non_seed_slots) and non_seed_slots[gi]["entry_id"] != winners[gi]["entry_id"]
                    for gi in range(len(winners))
                )

                if needs_fix:
                    placed: set = set()
                    for gi, w in enumerate(winners):
                        if gi >= len(non_seed_slots):
                            break
                        target = non_seed_slots[gi]
                        if w["entry_id"] not in placed:
                            await db.execute(
                                "UPDATE bracket_slots SET entry_id=? WHERE id=?",
                                (w["entry_id"], target["slot_id"]),
                            )
                            placed.add(w["entry_id"])
                    # 余剰スロットをNULLに
                    for gi in range(len(winners), len(non_seed_slots)):
                        await db.execute(
                            "UPDATE bracket_slots SET entry_id=NULL WHERE id=?",
                            (non_seed_slots[gi]["slot_id"],),
                        )
                    # このラウンドの結果をクリア
                    for grp in curr_groups:
                        await db.execute("DELETE FROM bracket_results WHERE group_id=?", (grp["id"],))
                        await db.execute("DELETE FROM bracket_slot_ranks WHERE group_id=?", (grp["id"],))
                    changed = True

                if changed:
                    await db.commit()


@app.get("/")
async def root():
    return RedirectResponse(url="/admin/")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/enter")
async def participant_enter(request: Request):
    """参加者向け入口（QRが指すURL）。

    案①（クライアント側ソフト有効期限）:
      この入口にアクセスした時刻を localStorage に記録し、参加者向け観覧ページへ進む。
      観覧ページ側の判定JS（public_html が埋め込む）は、記録時刻から24時間を過ぎると
      自動更新を止めてオーバーレイ表示する。再びこのQR（/enter）を読み直すと時刻が
      更新され、新たな24時間が始まる（単純な再読込では復帰しない）。

    サーバー負荷: 観覧ページ自体は従来どおり nginx の静的配信。/enter は参加者1人あたり
      数回の極軽量アクセスのみで、負荷は実質増えない。
    """
    from starlette.responses import HTMLResponse
    store = getattr(request.state, "store", None)
    slug = store.slug if store else ""
    base = f"/{slug}/" if slug else "/"
    key = f"m4_pub_issued_{slug or 'default'}"
    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>読み込み中…</title></head><body>
<p style="font-family:sans-serif;text-align:center;margin-top:40vh;color:#555">読み込み中…</p>
<script>
try {{ localStorage.setItem({key!r}, String(Date.now())); }} catch(e) {{}}
location.replace({base!r});
</script></body></html>"""
    return HTMLResponse(html)


# ============================================================================
# 公開エントリーフォーム（/entry）
#   事前エントリー方式が「エントリーフォームから」(pre_entry_method='form') の
#   レースに対し、参加者自身が事前エントリーを申し込む公開ページ。
#   - 認証不要（auth.py の _PUBLIC_PREFIXES に /entry を登録済み）
#   - DB は現在店舗（request.state.store）の db_path に直接接続
#   - 締切（pre_entry_deadline）を過ぎたレースは受付終了
#   - 二重サブミットは entry_form_tokens の使い捨てトークンで防止
#   - 同一連絡先の短時間連投は created_at ベースでスロットル
# ============================================================================

def _entry_db_path(request: Request) -> str:
    """現在のリクエストが属する店舗のDBパス。未解決（オンプレ）なら既定DB。"""
    store = getattr(request.state, "store", None)
    return store.db_path if store else DB_PATH


def _entry_prefix(request: Request) -> str:
    """URL接頭辞（既定店舗は "" 、店舗2〜は "/store2" 等）。"""
    store = getattr(request.state, "store", None)
    return store.prefix if store else ""


def _parse_deadline(s: str):
    """'YYYY-MM-DDTHH:MM'（datetime-local）を datetime へ。失敗時 None。"""
    if not s:
        return None
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _deadline_passed(s: str) -> bool:
    """締切文字列が過去なら True（NULL/空・解釈不能は False＝締切なし扱い）。"""
    from datetime import datetime
    dt = _parse_deadline(s)
    if dt is None:
        return False
    return datetime.now() > dt


@app.get("/entry")
async def entry_select(request: Request):
    """フォーム方式・締切前のレース一覧を表示し、参加者にレースを選ばせる。"""
    db_path = _entry_db_path(request)
    prefix = _entry_prefix(request)

    rows = []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, name, date, time_slot, regulation, pre_entry_deadline, status
                 FROM tournaments
                WHERE pre_entry=1 AND pre_entry_method='form'
                ORDER BY date DESC, id DESC"""
        ) as cur:
            all_form = await cur.fetchall()
    # 締切を過ぎたものは一覧から除外（受付中のみ表示）
    for r in all_form:
        if _deadline_passed(r["pre_entry_deadline"]):
            continue
        rows.append(dict(r))

    return templates.TemplateResponse("entry_select.html", {
        "request": request,
        "races": rows,
        "prefix": prefix,
    })


@app.get("/entry/{tid}")
async def entry_form(tid: int, request: Request):
    """選択したレースの入力フォームを表示。使い捨てトークンを発行して埋め込む。"""
    db_path = _entry_db_path(request)
    prefix = _entry_prefix(request)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, name, date, time_slot, regulation,
                      pre_entry, pre_entry_method, pre_entry_deadline
                 FROM tournaments WHERE id=?""", (tid,)
        ) as cur:
            t = await cur.fetchone()

        # 対象外（存在しない / 事前エントリーOFF / フォーム方式でない）は一覧へ
        if (not t) or (not t["pre_entry"]) or (t["pre_entry_method"] != "form"):
            return RedirectResponse(url=f"{prefix}/entry", status_code=303)

        closed = _deadline_passed(t["pre_entry_deadline"])

        token = ""
        if not closed:
            token = uuid.uuid4().hex
            await db.execute(
                "INSERT INTO entry_form_tokens (token, tournament_id) VALUES (?,?)",
                (token, tid),
            )
            await db.commit()

    return templates.TemplateResponse("entry_form.html", {
        "request": request,
        "race": dict(t),
        "token": token,
        "closed": closed,
        "prefix": prefix,
        "done": False,
        "error": request.query_params.get("error", ""),
    })


@app.post("/entry/{tid}")
async def entry_submit(tid: int, request: Request):
    """公開フォームの送信を受け、pre_entries へ複数名まとめて登録する。"""
    db_path = _entry_db_path(request)
    prefix = _entry_prefix(request)

    form = await request.form()
    token = (form.get("token", "") or "").strip()

    def _back_error(code: str):
        return RedirectResponse(url=f"{prefix}/entry/{tid}?error={code}", status_code=303)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # レース存在・方式・締切チェック
        async with db.execute(
            """SELECT id, name, pre_entry, pre_entry_method, pre_entry_deadline
                 FROM tournaments WHERE id=?""", (tid,)
        ) as cur:
            t = await cur.fetchone()
        if (not t) or (not t["pre_entry"]) or (t["pre_entry_method"] != "form"):
            return RedirectResponse(url=f"{prefix}/entry", status_code=303)
        if _deadline_passed(t["pre_entry_deadline"]):
            return _back_error("closed")

        # 二重サブミット防止：トークンが有効（存在・未使用・当該レース）か検証
        async with db.execute(
            "SELECT token, tournament_id, used FROM entry_form_tokens WHERE token=?",
            (token,),
        ) as cur:
            tok = await cur.fetchone()
        if (not tok) or (tok["tournament_id"] != tid) or (tok["used"] == 1):
            return _back_error("token")

        # ---- 入力パース（代表者＝1人目／2人目以降は同行フィールド）----
        # 代表者の連絡先・都道府県（全員で共有）
        rep_pref    = (form.get("prefecture", "") or "").strip()
        rep_ctype   = (form.get("contact_type", "") or "").strip()
        rep_contact = (form.get("contact", "") or "").strip()

        # 各参加者：name[]・yomi[]・is_child[]（同インデックスで対応）
        names    = [s.strip() for s in form.getlist("name")]
        yomis    = [s.strip() for s in form.getlist("yomi")]
        children = form.getlist("is_child")  # "0"/"1" の配列

        n = len(names)
        # 全項目必須チェック（代表者連絡先＋全員の名前・よみがな・区分）
        if not rep_pref or rep_ctype not in ("mail", "phone", "x") or not rep_contact:
            return _back_error("required")
        if n == 0 or len(yomis) != n or len(children) != n:
            return _back_error("required")
        for i in range(n):
            if not names[i] or not yomis[i]:
                return _back_error("required")
            if children[i] not in ("0", "1"):
                return _back_error("required")

        # 連投スロットル：同一連絡先で直近60秒以内の登録があればはじく
        async with db.execute(
            """SELECT COUNT(*) AS c FROM pre_entries
                WHERE tournament_id=? AND contact_type=? AND contact=?
                  AND created_at >= datetime('now','localtime','-60 seconds')""",
            (tid, rep_ctype, rep_contact),
        ) as cur:
            recent = (await cur.fetchone())["c"]
        if recent > 0:
            return _back_error("toofast")

        # 同一レース内の既存名（重複登録防止）
        async with db.execute(
            "SELECT name FROM pre_entries WHERE tournament_id=?", (tid,)
        ) as cur:
            existing = {r["name"] for r in await cur.fetchall()}

        # 連番の現在最大値
        async with db.execute(
            "SELECT COALESCE(MAX(seq_no),0) AS mx FROM pre_entries WHERE tournament_id=?",
            (tid,),
        ) as cur:
            seq = (await cur.fetchone())["mx"]

        # ---- 登録（1人目を代表者として連絡先を持たせる）----
        added = 0
        seen_in_input = set()
        for i in range(n):
            nm = names[i]
            if nm in existing or nm in seen_in_input:
                continue  # 既存・同一送信内重複はスキップ
            seen_in_input.add(nm)
            seq += 1
            is_rep = 1 if i == 0 else 0
            await db.execute(
                """INSERT INTO pre_entries
                     (tournament_id, seq_no, name, yomi, is_child,
                      prefecture, contact_type, contact, is_representative)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (tid, seq, nm, yomis[i], int(children[i]),
                 rep_pref, rep_ctype, rep_contact, is_rep),
            )
            existing.add(nm)
            added += 1

        # トークンを使用済みに（再送信を無効化）
        await db.execute(
            "UPDATE entry_form_tokens SET used=1 WHERE token=?", (token,)
        )
        await db.commit()

    # 完了画面（同テンプレートを done モードで描画）
    return templates.TemplateResponse("entry_form.html", {
        "request": request,
        "race": dict(t),
        "token": "",
        "closed": False,
        "prefix": prefix,
        "done": True,
        "added": added,
        "error": "",
    })


@app.get("/logo")
async def serve_logo():
    """ロゴ画像をno-cacheで返す（画像差し替えが即反映される）"""
    from fastapi.responses import FileResponse, Response
    path = os.path.join(BASE_DIR, "static", "logo_header.jpg")
    if not os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/race-asset/{tid}/{kind}/{seq}")
async def serve_race_asset(tid: int, kind: str, seq: int, db: aiosqlite.Connection = Depends(get_db)):
    """レース情報の画像（コースレイアウト/タイムスケジュール/備考）を配信HTMLとは別URLで返す。
       これにより配信HTMLへ画像を埋め込まずに済み、HTMLが軽量になる。公開（トークン不要）。
       DBは管理/観覧と同じ get_db（現在店舗DB）を使用する。"""
    from fastapi.responses import Response
    import base64
    if kind not in ("course", "schedule", "remarks"):
        return Response(status_code=404)
    async with db.execute(
        "SELECT data_uri FROM race_assets WHERE tournament_id=? AND kind=? AND seq=?",
        (tid, kind, seq),
    ) as cur:
        row = await cur.fetchone()
    if not row or not row["data_uri"]:
        return Response(status_code=404)
    try:
        header, b64 = row["data_uri"].split(",", 1)
        ctype = header.split(":", 1)[1].split(";", 1)[0] or "image/png"
        raw = base64.b64decode(b64)
    except Exception:
        return Response(status_code=404)
    return Response(content=raw, media_type=ctype, headers={"Cache-Control": "no-cache"})
