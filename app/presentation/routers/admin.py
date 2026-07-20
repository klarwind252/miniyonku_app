from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import aiosqlite
import os

from app.infrastructure.db.connection import get_db
from app.core.config import IS_CLOUD, PUBLIC_BASE_URL, PUBLIC_HTML_DIR

router = APIRouter()

# ---- BGM（ヘッダー再生・設定登録） ----
# 設定画面のシーン選択プルダウンの選択肢（先頭は未選択）。
BGM_SCENES = ["受付", "練習", "予選", "決勝", "選手入場", "優勝決定戦", "昼休み", "終了"]
BGM_MAX = 10                          # 登録できる最大曲数
BGM_MAX_BYTES = 30 * 1024 * 1024      # 1曲あたり最大30MB


# アプリの静的ファイル配信ディレクトリ（main.py が /static としてマウントする app/static）。
# 本モジュールは app/presentation/routers/ 配下にあるため、2つ上へ遡る必要がある。
# （v6.0c のクリーンアーキテクチャ移設で app/routers → app/presentation/routers へ
#   移動した際、"../static" のままだと app/presentation/static を指してしまい、
#   保存はできるが /static/ では配信されず、画像が404になっていた）
_STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../static"))


def _static_subdir(name: str) -> str:
    """app/static/<name> の絶対パスを返す（存在しなければ作成する）。"""
    path = os.path.join(_STATIC_DIR, name)
    os.makedirs(path, exist_ok=True)
    return path


def _bgm_dir(request) -> str | None:
    """BGM mp3 の保存先ディレクトリを返す。

    - クラウド版：{store.public_dir or PUBLIC_HTML_DIR}/bgm （nginx 配信ではなくアプリ経由で返す）
    - オンプレ版：app/static/bgm （賞状背景と同じくファイル名固定で上書き）

    未設定（クラウドで公開ディレクトリ不明）のときは None。
    """
    if IS_CLOUD:
        store = getattr(request.state, "store", None)
        public_dir = getattr(store, "public_dir", None) if store is not None else None
        if not public_dir:
            public_dir = PUBLIC_HTML_DIR
        if not public_dir:
            return None
        return os.path.join(public_dir, "bgm")
    return _static_subdir("bgm")


def _bgm_slot_name(slot: int) -> str:
    """スロット番号（1..10）→ 固定ファイル名（bgm-01.mp3 …）。"""
    return f"bgm-{int(slot):02d}.mp3"


def _read_store_icon_ver(db_path: str) -> str:
    """指定店舗のDBから共通アイコンのバージョン（pwa_icon_ver）を読む。無ければ空。"""
    import sqlite3
    if not db_path:
        return ""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            r = con.execute(
                "SELECT value FROM app_settings WHERE key='pwa_icon_ver'"
            ).fetchone()
            return r[0] if r and r[0] else ""
        finally:
            con.close()
    except Exception:
        return ""
from app.presentation.templates import templates


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    import json
    from datetime import date

    # ---- 集計 ----
    async with db.execute("SELECT COUNT(*) as cnt FROM tournaments") as cur:
        total_count = (await cur.fetchone())["cnt"]

    async with db.execute(
        "SELECT COUNT(*) as cnt FROM tournaments WHERE time_slot='day'"
    ) as cur:
        day_count = (await cur.fetchone())["cnt"]

    async with db.execute(
        "SELECT COUNT(*) as cnt FROM tournaments WHERE time_slot='night'"
    ) as cur:
        night_count = (await cur.fetchone())["cnt"]

    async with db.execute("SELECT COUNT(*) as cnt FROM racers WHERE COALESCE(ephemeral,0)=0") as cur:
        racer_count = (await cur.fetchone())["cnt"]

    # ---- カレンダー用：全レース(id, name, date, status) ----
    async with db.execute(
        "SELECT id, name, date, status, time_slot, regulation FROM tournaments ORDER BY date"
    ) as cur:
        all_tournaments = [dict(r) for r in await cur.fetchall()]

    # ---- 前回開催（全レギュレーション）----
    async with db.execute("SELECT value FROM app_settings WHERE key='regulations'") as cur:
        reg_row = await cur.fetchone()
    regulations = json.loads(reg_row["value"]) if reg_row else []

    last_held = {}  # { regulation_name: "YYYY/MM/DD" or None }
    for reg in regulations:
        async with db.execute(
            "SELECT date FROM tournaments WHERE regulation=? ORDER BY date DESC LIMIT 1",
            (reg,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            d = row["date"]  # "YYYY-MM-DD"
            try:
                parsed = date.fromisoformat(d)
                last_held[reg] = f"{parsed.year}/{parsed.month:02d}/{parsed.day:02d}"
            except Exception:
                last_held[reg] = d
        else:
            last_held[reg] = None

    return templates.TemplateResponse("admin/dashboard.html", {
        "request": request,
        "total_count": total_count,
        "day_count": day_count,
        "night_count": night_count,
        "racer_count": racer_count,
        "all_tournaments_json": json.dumps(all_tournaments, ensure_ascii=False),
        "last_held": last_held,
    })


QUALIFYING_LABELS = {
    "none":           "なし（即決勝トーナメント）",
    "none_roundrobin":"なし（即決勝総当たり）",
    "heat_tournament":"ヒート（トーナメント）",
    "heat_tournament_garappa":"ヒート（トーナメント）[がらっぱ堂]",
    "heat_roundrobin":"ヒート（総当たり）",
    "point":          "ポイント",
    "roundrobin":     "総当たり",
    "order":          "並び順（ポイント制）",
}


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """設定画面"""
    import json
    async with db.execute(
        "SELECT id, name, paper_size, orientation, updated_at FROM certificate_templates ORDER BY id"
    ) as cur:
        cert_templates = await cur.fetchall()

    async with db.execute(
        "SELECT id, name, card_size, code_type, updated_at FROM card_templates ORDER BY id"
    ) as cur:
        card_templates = await cur.fetchall()

    async with db.execute("SELECT value FROM app_settings WHERE key='regulations'") as cur:
        row = await cur.fetchone()
    regulations = json.loads(row["value"]) if row else []

    async with db.execute("SELECT value FROM app_settings WHERE key='default_qualifying'") as cur:
        dq_row = await cur.fetchone()
    default_qualifying = dq_row["value"] if dq_row else "heat_tournament"

    # 店舗名（オンプレ版で店舗1の名称を設定するための値。app_settings に保持）
    async with db.execute("SELECT value FROM app_settings WHERE key='store_name'") as cur:
        _sn_row = await cur.fetchone()
    onprem_store_name = (_sn_row["value"] if _sn_row else "") or ""

    # ポストテンプレートは1件目のbodyだけ使う
    async with db.execute("SELECT id, body FROM post_templates ORDER BY id LIMIT 1") as cur:
        pt_row = await cur.fetchone()
    post_template_body = pt_row["body"] if pt_row else ""
    post_template_id   = pt_row["id"]   if pt_row else None

    # 参加者向けHTML配信設定
    async with db.execute("SELECT value FROM app_settings WHERE key='public_html_enabled'") as cur:
        ph_row = await cur.fetchone()
    public_html_enabled = (ph_row["value"] == "1") if ph_row else False

    async with db.execute("SELECT value FROM app_settings WHERE key='public_html_gcs_bucket'") as cur:
        bucket_row = await cur.fetchone()
    public_html_gcs_bucket = bucket_row["value"] if bucket_row else ""

    async with db.execute("SELECT value FROM app_settings WHERE key='public_html_gcp_project'") as cur:
        proj_row = await cur.fetchone()
    public_html_gcp_project = proj_row["value"] if proj_row else ""

    # 参加者向けURL（クラウド=VPSライブ配信 / オンプレ=GCS）
    if IS_CLOUD:
        participant_url = f"{PUBLIC_BASE_URL}/enter" if PUBLIC_BASE_URL else ""
    else:
        participant_url = (
            f"https://storage.googleapis.com/{public_html_gcs_bucket}/index.html"
            if public_html_gcs_bucket else ""
        )

    # 料金設定
    import json as _json
    async with db.execute("SELECT value FROM app_settings WHERE key='pricing_enabled'") as cur:
        row = await cur.fetchone()
        pricing_enabled = row["value"] == "1" if row else False
    async with db.execute("SELECT value FROM app_settings WHERE key='pricing_rounding'") as cur:
        row = await cur.fetchone()
        pricing_rounding = row["value"] if row else "floor"
    async with db.execute("SELECT value FROM app_settings WHERE key='pricing_table'") as cur:
        row = await cur.fetchone()
        try:
            pricing_table = _json.loads(row["value"]) if row else {}
        except Exception:
            pricing_table = {}

    # 複数店舗展開（クラウド版・店舗1＝既定店舗のときだけ管理UIを表示）
    stores_info = []
    is_default_store = False
    max_stores = 5
    if IS_CLOUD:
        try:
            from app import registry
            cur_store = getattr(request.state, "store", None)
            is_default_store = bool(cur_store is not None and not cur_store.slug)
            max_stores = registry.MAX_STORES
            base = PUBLIC_BASE_URL
            for s in registry.list_stores(include_disabled=True):
                pfx = f"/{s.slug}" if s.slug else ""
                stores_info.append({
                    "id": s.id, "slug": s.slug, "name": s.name,
                    "enabled": s.enabled, "is_default": (not s.slug),
                    "admin_url": f"{base}{pfx}/admin/?key={s.admin_token}" if base else "",
                    "view_url": f"{base}{pfx}/view/?key={s.view_token}" if base else "",
                    "participant_url": f"{base}{pfx}/" if base else "",
                    "qr_url": f"{base}{pfx}/enter" if base else "",
                    "restrict_hours": s.restrict_hours,
                    "access_start": s.access_start or "",
                    "access_end": s.access_end or "",
                    "is_open": registry.is_store_open(s),
                    # 店舗別の共通アイコン（プレビュー・登録用）
                    "icon_prefix": pfx,
                    "icon_ver": _read_store_icon_ver(s.db_path),
                })
            # 現在店舗の参加者入口URL（/enter）に合わせて participant_url を上書き。
            # QR/共有URLは /enter を指す必要がある（スキャンで発行時刻を記録し、
            # 24時間ソフト有効期限ゲートを起動させるため）。
            if cur_store is not None:
                pfx = f"/{cur_store.slug}" if cur_store.slug else ""
                participant_url = f"{base}{pfx}/enter" if base else participant_url
        except Exception as e:
            print(f"[admin] stores_info error: {e}", flush=True)

    # ホーム画面アイコン（Webアプリ）設定
    from app import pwa as _pwa
    pwa_settings = _pwa.get_pwa_settings(request)
    # QR中央ロゴ・プレビューで使う「枠なし元アイコン」URL（未設定なら空）
    pwa_src_icon_url = _pwa.src_icon_url(request, 512) if IS_CLOUD else ""
    # 共通アイコンのプレビュー用（画面別・枠付き）URL群（未設定なら空）
    pwa_icon_prefix = _pwa.slug_prefix(request)
    pwa_icon_ver = pwa_settings.get("pwa_icon_ver", "")
    # アプリ用アイコン（単一画像・枠なし・長辺38px）のバージョン（未登録なら空）
    app_icon_ver = pwa_settings.get("app_icon_ver", "")
    # 待機画面の背景（view / html）設定
    bg_enabled = pwa_settings.get("bg_enabled", "0")
    bg_ver = pwa_settings.get("bg_ver", "")
    # 待機画面スライドショー（view / html）設定
    slideshow_enabled = pwa_settings.get("slideshow_enabled", "0")
    slideshow_ver = pwa_settings.get("slideshow_ver", "")
    slideshow_count = pwa_settings.get("slideshow_count", "0")
    # 設定画面プレビュー用（有効/無効に関係なく登録枚数分のURL）
    slideshow_previews = _pwa.slideshow_urls(request, respect_enabled=False) if IS_CLOUD else []

    # ---- BGM（設定画面：最大10曲の登録フォーム用） ----
    async with db.execute("SELECT value FROM app_settings WHERE key='bgm_tracks'") as cur:
        _bgm_row = await cur.fetchone()
    try:
        _bgm_list = json.loads(_bgm_row["value"]) if _bgm_row and _bgm_row["value"] else []
    except Exception:
        _bgm_list = []
    async with db.execute("SELECT value FROM app_settings WHERE key='bgm_ver'") as cur:
        _bgm_ver_row = await cur.fetchone()
    bgm_ver = _bgm_ver_row["value"] if _bgm_ver_row and _bgm_ver_row["value"] else ""
    _bgm_by_slot = {}
    for t in _bgm_list:
        try:
            _bgm_by_slot[int(t.get("slot"))] = t
        except Exception:
            pass
    # 1..BGM_MAX の固定行を作る（未登録スロットは空）
    bgm_rows = []
    for i in range(1, BGM_MAX + 1):
        t = _bgm_by_slot.get(i)
        bgm_rows.append({
            "slot": i,
            "name": (t.get("name") if t else "") or "",
            "note": (t.get("note") if t else "") or "",
            "scene": (t.get("scene") if t else "") or "",
            "registered": bool(t),
        })

    # 3画面QR（管理用＝admin鍵付き／観覧用＝view鍵付き／レーサー用＝/enter鍵なし）
    # 中央ロゴは画面別の枠付きアイコン（共通アイコン未登録なら無し）。
    qr_targets = []
    if IS_CLOUD and PUBLIC_BASE_URL:
        cur_store = getattr(request.state, "store", None)
        pfx = pwa_icon_prefix  # = slug_prefix（店舗1は ""）
        store_name = cur_store.name if cur_store else ""
        a_tok = cur_store.admin_token if cur_store else ""
        v_tok = cur_store.view_token if cur_store else ""
        # 中央ロゴ（画面別・枠付き）URL。アイコン未登録（ver空）なら空文字。
        def _logo(screen):
            if not pwa_icon_ver:
                return ""
            return _pwa.icon_url(pfx, f"icon-{screen}-512.png", pwa_icon_ver)
        qr_targets = [
            {
                "screen": "admin", "label": "管理用",
                "caption": f"{store_name} 管理用".strip(),
                "url": f"{PUBLIC_BASE_URL}{pfx}/admin/?key={a_tok}",
                "logo_url": _logo("admin"),
                "sensitive": True,
            },
            {
                "screen": "view", "label": "観覧用",
                "caption": f"{store_name} 観覧用".strip(),
                "url": f"{PUBLIC_BASE_URL}{pfx}/view/?key={v_tok}",
                "logo_url": _logo("view"),
                "sensitive": True,
            },
            {
                "screen": "html", "label": "レーサー用",
                "caption": f"{store_name} レーサー用".strip(),
                "url": f"{PUBLIC_BASE_URL}{pfx}/enter",
                "logo_url": _logo("html"),
                "sensitive": False,
            },
        ]

    return templates.TemplateResponse("admin/settings.html", {
        "request": request,
        "cert_templates": cert_templates,
        "card_templates": card_templates,
        "regulations": regulations,
        "default_qualifying": default_qualifying,
        "onprem_store_name": onprem_store_name,
        "qualifying_labels": QUALIFYING_LABELS,
        "post_template_body": post_template_body,
        "post_template_id": post_template_id,
        "pricing_enabled": pricing_enabled,
        "pricing_rounding": pricing_rounding,
        "pricing_table": pricing_table,
        "public_html_enabled": public_html_enabled,
        "public_html_gcs_bucket": public_html_gcs_bucket,
        "public_html_gcp_project": public_html_gcp_project,
        "participant_url": participant_url,
        "stores_info": stores_info,
        "is_default_store": is_default_store,
        "max_stores": max_stores,
        "pwa": pwa_settings,
        "pwa_src_icon_url": pwa_src_icon_url,
        "pwa_icon_prefix": pwa_icon_prefix,
        "pwa_icon_ver": pwa_icon_ver,
        "app_icon_ver": app_icon_ver,
        "bg_enabled": bg_enabled,
        "bg_ver": bg_ver,
        "slideshow_enabled": slideshow_enabled,
        "slideshow_ver": slideshow_ver,
        "slideshow_count": slideshow_count,
        "slideshow_previews": slideshow_previews,
        "qr_targets": qr_targets,
        "bgm_rows": bgm_rows,
        "bgm_ver": bgm_ver,
        "bgm_scenes": BGM_SCENES,
    })


@router.post("/settings/default-qualifying/save", response_class=HTMLResponse)
async def save_default_qualifying(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """予選形式の初期値を保存"""
    from fastapi.responses import RedirectResponse
    form = await request.form()
    val = form.get("default_qualifying", "heat_tournament")
    await db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('default_qualifying', ?)",
        (val,)
    )
    await db.commit()
    return RedirectResponse(url="/admin/settings#defaults", status_code=303)


# ---- Git アップデート（admin 画面を開いたときの確認・実行） ----

@router.get("/update/check")
async def update_check(request: Request):
    """更新の有無を返す（admin ページ読み込み時にフロントから呼ばれる）。

    クラウド版のみ有効。オンプレ版や git 未構成では available=false を返す。
    """
    from fastapi.responses import JSONResponse
    if not IS_CLOUD:
        return JSONResponse({"cloud": False, "available": False})
    try:
        from app.services import auto_update
        info = await auto_update._run_blocking(auto_update.check_available)
        race = await auto_update._run_blocking(auto_update.race_in_progress)
        return JSONResponse({
            "cloud": True,
            "available": bool(info.get("available")),
            "commit": info.get("commit", ""),
            "race": bool(race),
            "boot": auto_update.BOOT_ID,
        })
    except Exception as e:
        return JSONResponse({"cloud": True, "available": False, "error": str(e)})


@router.post("/update/run")
async def update_run(request: Request):
    """更新を実行する（更新があれば取得後に再起動）。

    応答を返してから再起動されるよう、実処理は少し遅らせて別タスクで走らせる。
    """
    from fastapi.responses import JSONResponse
    if not IS_CLOUD:
        return JSONResponse({"ok": False, "error": "クラウド版でのみ利用できます"})
    import asyncio
    from app.services import auto_update
    info = await auto_update._run_blocking(auto_update.check_available, True)
    if not info.get("available"):
        return JSONResponse({"ok": True, "willRestart": False, "updated": False})

    async def _later():
        # 応答が返り切ってから git pull → 再起動（このプロセスは終了する）
        await asyncio.sleep(1.0)
        await auto_update._run_blocking(auto_update.do_update)

    asyncio.create_task(_later())
    return JSONResponse({"ok": True, "willRestart": True, "commit": info.get("commit", "")})


@router.get("/update/ping")
async def update_ping(request: Request):
    """再起動検知用。プロセス起動ID（BOOT_ID）を返す。

    更新実行前後で boot が変われば、再起動が完了したと判断できる。
    """
    from fastapi.responses import JSONResponse
    try:
        from app.services import auto_update
        return JSONResponse({"ok": True, "boot": auto_update.BOOT_ID})
    except Exception:
        return JSONResponse({"ok": True, "boot": ""})


@router.post("/settings/store-name/save", response_class=HTMLResponse)
async def save_store_name(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """店舗1の店舗名を保存（オンプレ版設定画面用。app_settings 'store_name'）"""
    from fastapi.responses import RedirectResponse
    form = await request.form()
    name = (form.get("store_name") or "").strip()
    await db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('store_name', ?)",
        (name,)
    )
    await db.commit()
    return RedirectResponse(url="/admin/settings#store-name", status_code=303)


@router.post("/settings/post-template/save", response_class=HTMLResponse)
async def save_post_template(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ポストテンプレートをインラインで保存（1件のみ）"""
    from fastapi.responses import RedirectResponse
    form = await request.form()
    body = form.get("body", "")
    tid  = form.get("template_id", "")
    if tid:
        await db.execute(
            "UPDATE post_templates SET body=?, updated_at=datetime('now','localtime') WHERE id=?",
            (body, int(tid))
        )
    else:
        await db.execute(
            "INSERT INTO post_templates (name, body) VALUES ('デフォルト', ?)", (body,)
        )
    await db.commit()
    return RedirectResponse(url="/admin/settings#post", status_code=303)


# ---- ポストテンプレート CRUD ----

@router.post("/settings/pricing/save")
async def save_pricing(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    import json as _json
    from fastapi.responses import RedirectResponse
    form = await request.form()
    enabled = "1" if form.get("pricing_enabled") == "on" else "0"
    rounding = form.get("pricing_rounding", "floor")
    days = ["weekday", "saturday", "sunday", "holiday", "special"]
    types = ["hour", "free", "race"]
    groups = ["adult", "child"]
    table = {}
    for g in groups:
        table[g] = {}
        for d in days:
            table[g][d] = {}
            for t in types:
                val = form.get(f"pricing_{g}_{d}_{t}", "").strip()
                table[g][d][t] = val
    await db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('pricing_enabled', ?)", (enabled,))
    await db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('pricing_rounding', ?)", (rounding,))
    await db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('pricing_table', ?)", (_json.dumps(table, ensure_ascii=False),))
    await db.commit()
    return RedirectResponse(url="/admin/settings#pricing", status_code=303)


@router.get("/settings/pricing-api")
async def pricing_api(db: aiosqlite.Connection = Depends(get_db)):
    from fastapi.responses import JSONResponse as _JSONResponse
    import json as _json
    async with db.execute("SELECT value FROM app_settings WHERE key='pricing_enabled'") as cur:
        row = await cur.fetchone()
        enabled = row["value"] == "1" if row else False
    if not enabled:
        return _JSONResponse({"enabled": False})
    async with db.execute("SELECT value FROM app_settings WHERE key='pricing_rounding'") as cur:
        row = await cur.fetchone()
        rounding = row["value"] if row else "floor"
    async with db.execute("SELECT value FROM app_settings WHERE key='pricing_table'") as cur:
        row = await cur.fetchone()
        try:
            table = _json.loads(row["value"]) if row else {}
        except Exception:
            table = {}
    return _JSONResponse({"enabled": True, "rounding": rounding, "table": table})


@router.get("/settings/day-type-api")
async def get_day_type(db: aiosqlite.Connection = Depends(get_db)):
    """当日の料金区分を返す。未設定なら jpholiday で自動判定"""
    from fastapi.responses import JSONResponse as _JSONResponse
    from datetime import date as _date
    today = _date.today().isoformat()

    async with db.execute("SELECT value FROM app_settings WHERE key='today_day_type'") as cur:
        row = await cur.fetchone()
        saved = row["value"] if row else ""

    # 保存値が「今日の日付:区分」形式かチェック
    if saved and ":" in saved:
        saved_date, saved_type = saved.split(":", 1)
        if saved_date == today:
            return _JSONResponse({"day_type": saved_type})

    # 自動判定
    try:
        import jpholiday as _jph
        jph_ok = True
    except ImportError:
        jph_ok = False
    d = _date.fromisoformat(today)
    if jph_ok and _jph.is_holiday(d):
        day_type = "holiday"
    elif d.weekday() == 5:
        day_type = "saturday"
    elif d.weekday() == 6:
        day_type = "sunday"
    else:
        day_type = "weekday"
    return _JSONResponse({"day_type": day_type})


@router.post("/settings/day-type-api")
async def save_day_type(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """当日の料金区分を保存する"""
    from fastapi.responses import JSONResponse as _JSONResponse
    from datetime import date as _date
    body = await request.json()
    day_type = body.get("day_type", "weekday")
    today = _date.today().isoformat()
    if day_type == "__auto__":
        # 自動判定にリセット（値をクリア）
        await db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('today_day_type', ?)", ("",))
    else:
        value = f"{today}:{day_type}"
        await db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('today_day_type', ?)", (value,))
    await db.commit()
    return _JSONResponse({"ok": True, "day_type": day_type})


# ---- テロップ（お知らせ帯：admin から入力 → view / 参加者html に表示）----
# 保存先は app_settings（店舗ごとのDB）。キーは telop_text / telop_active / telop_updated_at。
# 操作は admin のみ。表示は view / 参加者html 側が /api/telop をポーリングして反映する。
async def _telop_val(db, key: str) -> str:
    async with db.execute("SELECT value FROM app_settings WHERE key=?", (key,)) as cur:
        row = await cur.fetchone()
    return (row["value"] if row and row["value"] is not None else "")


@router.get("/telop")
async def get_telop(db: aiosqlite.Connection = Depends(get_db)):
    """現在のテロップ（本文・表示中フラグ・更新時刻）を返す。ポップオーバーの初期表示用。"""
    from fastapi.responses import JSONResponse as _JSONResponse
    text = await _telop_val(db, "telop_text")
    active = (await _telop_val(db, "telop_active")) == "1"
    updated_at = await _telop_val(db, "telop_updated_at")
    return _JSONResponse({"active": active, "text": text, "updated_at": updated_at})


@router.post("/telop")
async def save_telop(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """テロップを保存する。action='show' で表示（本文必須）、action='clear' で消去。"""
    from fastapi.responses import JSONResponse as _JSONResponse
    from datetime import datetime as _dt
    body = await request.json()
    action = (body.get("action") or "show").strip()
    text = (body.get("text") or "").strip()[:200]   # 帯なので程々の長さに切り詰め
    active = "0" if (action == "clear" or not text) else "1"
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    await db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('telop_text', ?)", (text,))
    await db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('telop_active', ?)", (active,))
    await db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('telop_updated_at', ?)", (now,))
    await db.commit()
    return _JSONResponse({"ok": True, "active": active == "1", "text": text, "updated_at": now})


# ---- 稼働ヘルス（サーバー状態：設定画面からオンデマンド取得・admin専用）----
@router.get("/health")
async def admin_health(request: Request):
    """サーバー状態（稼働時間・ディスク残量・DBサイズ・最終バックアップ）をJSONで返す。
    設定画面『稼働ヘルス』のボタン押下時に1回だけ取得する（常時監視はしない）。
    公開の /health（liveness）とは別で、こちらは admin 配下＝認証済みのみ。"""
    import time as _time, os as _os, shutil as _shutil
    from datetime import datetime as _dt
    from fastapi.responses import JSONResponse as _JSONResponse
    from app import registry as _reg
    from app.core.config import DEPLOY_MODE as _MODE

    started = getattr(request.app.state, "started_at", None)
    uptime_sec = int(_time.time() - started) if started else None

    data_dir = _reg.DATA_DIR
    try:
        _du = _shutil.disk_usage(data_dir)
        disk = {"total": _du.total, "used": _du.used, "free": _du.free,
                "percent": round(_du.used / _du.total * 100, 1) if _du.total else None}
    except Exception:
        disk = None

    # メモリ（Linux/クラウド：/proc/meminfo。Windows/オンプレでは取得不可→None）
    memory = None
    try:
        _mi = {}
        with open("/proc/meminfo") as _f:
            for _line in _f:
                _k, _, _v = _line.partition(":")
                _parts = _v.split()
                if _parts:
                    _mi[_k] = int(_parts[0]) * 1024   # kB → bytes
        _mt = _mi.get("MemTotal"); _ma = _mi.get("MemAvailable")
        if _mt and _ma is not None:
            _mu = _mt - _ma
            memory = {"total": _mt, "available": _ma, "used": _mu,
                      "percent": round(_mu / _mt * 100, 1)}
    except Exception:
        memory = None

    # ロードアベレージ（Linuxのみ。取得不可なら None）
    try:
        _la = _os.getloadavg()
        load = {"1": round(_la[0], 2), "5": round(_la[1], 2), "15": round(_la[2], 2)}
    except Exception:
        load = None

    db_files = []
    total_db = 0

    def _addf(path, label):
        nonlocal total_db
        try:
            if path and _os.path.isfile(path):
                sz = _os.path.getsize(path)
                total_db += sz
                db_files.append({"name": label, "bytes": sz})
        except Exception:
            pass

    _addf(_reg.CONTROL_DB_PATH, "control.db")
    store_count = 0
    try:
        for st in _reg.list_stores(include_disabled=True):
            store_count += 1
            _addf(st.db_path, (st.slug or "(店舗1)"))
    except Exception:
        _addf(_reg.DEFAULT_DB_PATH, "(店舗1)")

    # #9 の自動バックアップ状況（最新日付・世代数）
    backups = {"latest": None, "generations": 0}
    try:
        _broot = _os.path.join(data_dir, "_backups")
        _dates = []
        for _d in _os.listdir(_broot):
            try:
                _dt.strptime(_d, "%Y-%m-%d"); _dates.append(_d)
            except ValueError:
                pass
        _dates.sort(reverse=True)
        backups = {"latest": (_dates[0] if _dates else None), "generations": len(_dates)}
    except Exception:
        pass

    return _JSONResponse({
        "ok": True,
        "server_time": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        "deploy_mode": _MODE,
        "uptime_seconds": uptime_sec,
        "disk": disk,
        "memory": memory,
        "load": load,
        "db": {"count": len(db_files), "total_bytes": total_db, "files": db_files},
        "store_count": store_count,
        "backups": backups,
    })


# ---- アクセス統計（参加者htmlの大会別 現在同時接続 / ピーク / 延べ視聴者）----
@router.get("/access-stats")
async def admin_access_stats(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """設定画面『アクセス統計』のボタン押下時に1回だけ取得する（常時更新しない）。
    集計はメモリ保持（起動以降・再起動でリセット）。大会名はDBから補完。"""
    from fastapi.responses import JSONResponse as _JSONResponse
    from app.services import access_stats

    store = getattr(request.state, "store", None)
    sid = getattr(store, "id", 0)
    snap = access_stats.snapshot(sid)

    names = {}
    try:
        async with db.execute("SELECT id, name FROM tournaments") as cur:
            async for row in cur:
                names[row["id"]] = row["name"]
    except Exception:
        pass

    rows = []
    for tid, s in snap.items():
        rows.append({
            "tid": tid,
            "name": (names.get(tid) or ("トップ／その他" if tid == 0 else ("大会#" + str(tid)))),
            "current": s["current"],
            "peak": s["peak"],
            "uniq": s["uniq"],
        })
    rows.sort(key=lambda r: (-r["current"], -r["peak"], -r["uniq"]))
    return _JSONResponse({"ok": True, "rows": rows})


# ---- 監査ログ（結果の入力・修正・取消の履歴。14日分・店舗ごと）----
@router.get("/audit-log/recent")
async def admin_audit_recent(request: Request, limit: int = 100):
    """設定画面のプレビュー用。要求元店舗の直近ログを新しい順で返す。"""
    from fastapi.responses import JSONResponse as _JSONResponse
    from app.services import audit_log
    store = getattr(request.state, "store", None)
    skey = (getattr(store, "slug", "") or "default")
    n = max(1, min(int(limit or 100), 500))
    entries = audit_log.read_entries(skey)[:n]
    return _JSONResponse({"ok": True, "count": len(entries), "entries": entries})


@router.get("/audit-log/download")
async def admin_audit_download(request: Request):
    """要求元店舗の直近14日分の監査ログをCSVで返す（Excel向けにBOM付き）。"""
    from fastapi.responses import Response as _Response
    from app.services import audit_log
    import csv
    import io
    from datetime import datetime as _dt
    store = getattr(request.state, "store", None)
    skey = (getattr(store, "slug", "") or "default")
    entries = audit_log.read_entries(skey)
    buf = io.StringIO()
    buf.write("\ufeff")  # Excelで文字化けしないようBOM
    w = csv.writer(buf)
    w.writerow(["日時", "店舗", "IP", "操作", "パス", "メソッド", "ステータス"])
    for e in entries:
        w.writerow([e.get("ts", ""), e.get("store", ""), e.get("ip", ""),
                    e.get("action", ""), e.get("path", ""), e.get("method", ""), e.get("status", "")])
    data = buf.getvalue().encode("utf-8")
    fname = "audit_%s_%s.csv" % (skey, _dt.now().strftime("%Y%m%d"))
    return _Response(content=data, media_type="text/csv; charset=utf-8",
                     headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/settings/post-templates/new", response_class=HTMLResponse)
async def new_post_template(request: Request):
    """ポストテンプレート新規作成画面"""
    return templates.TemplateResponse("admin/post_template_edit.html", {
        "request": request,
        "tpl": None,
    })


@router.post("/settings/post-templates/create", response_class=HTMLResponse)
async def create_post_template(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ポストテンプレート作成"""
    from fastapi.responses import RedirectResponse
    form = await request.form()
    name = form.get("name", "").strip() or "新規テンプレート"
    body = form.get("body", "")
    await db.execute(
        "INSERT INTO post_templates (name, body) VALUES (?, ?)",
        (name, body)
    )
    await db.commit()
    return RedirectResponse(url="/admin/settings#post", status_code=303)


@router.get("/settings/post-templates/{tid}/edit", response_class=HTMLResponse)
async def edit_post_template(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ポストテンプレート編集画面"""
    async with db.execute("SELECT * FROM post_templates WHERE id=?", (tid,)) as cur:
        tpl = await cur.fetchone()
    if not tpl:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/admin/settings#post", status_code=303)
    return templates.TemplateResponse("admin/post_template_edit.html", {
        "request": request,
        "tpl": dict(tpl),
    })


@router.post("/settings/post-templates/{tid}/update", response_class=HTMLResponse)
async def update_post_template(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ポストテンプレート更新"""
    from fastapi.responses import RedirectResponse
    form = await request.form()
    name = form.get("name", "").strip() or "新規テンプレート"
    body = form.get("body", "")
    await db.execute(
        "UPDATE post_templates SET name=?, body=?, updated_at=datetime('now','localtime') WHERE id=?",
        (name, body, tid)
    )
    await db.commit()
    return RedirectResponse(url="/admin/settings#post", status_code=303)


@router.post("/settings/post-templates/delete/{tid}", response_class=HTMLResponse)
async def delete_post_template(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ポストテンプレート削除"""
    from fastapi.responses import RedirectResponse
    await db.execute("DELETE FROM post_templates WHERE id=?", (tid,))
    await db.commit()
    return RedirectResponse(url="/admin/settings#post", status_code=303)


@router.post("/settings/regulations/save", response_class=HTMLResponse)
async def save_regulations(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """レギュレーション一覧を保存"""
    import json
    form = await request.form()
    raw = form.get("regulations_text", "")
    # 改行またはカンマで分割、空白除去、空行除去
    items = [s.strip() for s in raw.replace(",", "\n").splitlines() if s.strip()]
    await db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('regulations', ?)",
        (json.dumps(items, ensure_ascii=False),)
    )
    await db.commit()
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/settings#defaults", status_code=303)


@router.post("/settings/public-html/save", response_class=HTMLResponse)
async def save_public_html_settings(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """参加者向けHTML配信設定を保存"""
    from fastapi.responses import RedirectResponse
    if IS_CLOUD:
        # クラウドはGCS配信を使わない（常に無効固定）
        await db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('public_html_enabled', '0')"
        )
        await db.commit()
        return RedirectResponse(url="/admin/settings#public-html", status_code=303)
    form = await request.form()
    enabled  = "1" if form.get("public_html_enabled") == "on" else "0"
    bucket   = (form.get("public_html_gcs_bucket") or "").strip()
    project  = (form.get("public_html_gcp_project") or "").strip()
    await db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('public_html_enabled', ?)",
        (enabled,)
    )
    await db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('public_html_gcs_bucket', ?)",
        (bucket,)
    )
    await db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('public_html_gcp_project', ?)",
        (project,)
    )
    await db.commit()
    return RedirectResponse(url="/admin/settings#public-html", status_code=303)


# ---- ホーム画面アイコン（Webアプリ）設定 ----
@router.post("/settings/pwa/save")
async def save_pwa_settings(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ホーム画面アイコン（Webアプリ）の設定を保存（名称・テーマ色・key埋め込み起動・有効/無効）"""
    from fastapi.responses import RedirectResponse
    from app import pwa as _pwa
    form = await request.form()

    def _chk(name):
        return "1" if form.get(name) in ("on", "1", "true") else "0"

    enabled = _chk("pwa_enabled")
    values = {
        "pwa_enabled": enabled,
        "pwa_name_admin": (form.get("pwa_name_admin") or "管理用").strip()[:30],
        "pwa_name_view":  (form.get("pwa_name_view")  or "観覧用").strip()[:30],
        "pwa_name_html":  (form.get("pwa_name_html")  or "レーサー用").strip()[:30],
        "pwa_theme_admin": (form.get("pwa_theme_admin") or "#2c3e50").strip()[:9],
        "pwa_theme_view":  (form.get("pwa_theme_view")  or "#0f1923").strip()[:9],
        "pwa_theme_html":  (form.get("pwa_theme_html")  or "#0f1923").strip()[:9],
        "pwa_keylaunch_admin": _chk("pwa_keylaunch_admin"),
        "pwa_keylaunch_view":  _chk("pwa_keylaunch_view"),
    }
    for k, v in values.items():
        await db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (k, v)
        )
    await db.commit()

    # html（レーサー用）の静的 manifest を即時更新（次回 export を待たずに反映）
    if IS_CLOUD:
        try:
            from app.config import PUBLIC_HTML_DIR
            store = getattr(request.state, "store", None)
            out_dir = store.public_dir if store else PUBLIC_HTML_DIR
            slug = store.slug if store else ""
            settings = _pwa.get_pwa_settings(request)
            _pwa.write_static_html_manifest(out_dir, settings, slug)
        except Exception as e:
            print(f"[admin] pwa static manifest refresh skipped: {e}", flush=True)

    return RedirectResponse(url="/admin/settings#pwa", status_code=303)


@router.post("/settings/pwa/icon")
async def upload_pwa_icon(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """共通アイコンをアップロードし、現在店舗の公開ディレクトリへ
    枠なし元画像＋admin(ゴールド)/view(シルバー)/html(枠なし) の一式を生成して保存する。"""
    import os, uuid
    from fastapi.responses import RedirectResponse
    from app import pwa as _pwa
    try:
        form = await request.form()
        upload = form.get("icon")
        if not upload or not hasattr(upload, "filename") or not upload.filename:
            return RedirectResponse(url="/admin/settings#pwa", status_code=303)

        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            return RedirectResponse(url="/admin/settings#pwa", status_code=303)

        data = await upload.read()
        # サイズ上限（10MB）。巨大ファイル・画素爆弾によるメモリ枯渇を防ぐ
        MAX_ICON_BYTES = 10 * 1024 * 1024
        if len(data) > MAX_ICON_BYTES:
            return RedirectResponse(url="/admin/settings#pwa", status_code=303)

        # 現在店舗の公開ディレクトリ（nginx 直接配信のルート）へ書き出す
        store = getattr(request.state, "store", None)
        if store is not None and getattr(store, "public_dir", None):
            public_dir = store.public_dir
        else:
            public_dir = PUBLIC_HTML_DIR
        if not public_dir:
            raise ValueError("公開ディレクトリ（PUBLIC_HTML_DIR）が未設定です。")

        # 枠なし元画像＋画面別（枠付き）一式を生成
        _pwa.generate_icons(data, public_dir)

        ver = uuid.uuid4().hex[:8]
        await db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('pwa_icon_ver', ?)",
            (ver,),
        )
        await db.commit()

        # html の静的 manifest を更新（アイコン参照を反映）
        if IS_CLOUD:
            try:
                slug = store.slug if store else ""
                settings = _pwa.get_pwa_settings(request)
                _pwa.write_static_html_manifest(public_dir, settings, slug)
            except Exception as e:
                print(f"[admin] pwa static manifest refresh skipped: {e}", flush=True)

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[admin] pwa icon upload error: {e}", flush=True)

    return RedirectResponse(url="/admin/settings#pwa", status_code=303)


@router.post("/settings/pwa/app-icon")
async def upload_app_icon(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """アプリ用アイコン（単一画像・枠なし）をアップロードし、長辺38pxへ縮小して
    現在店舗の公開ディレクトリ（{public_dir}/pwa/icon-app-38.png）へ保存する。"""
    import os, uuid
    from fastapi.responses import RedirectResponse
    from app import pwa as _pwa
    try:
        form = await request.form()
        upload = form.get("app_icon")
        if not upload or not hasattr(upload, "filename") or not upload.filename:
            return RedirectResponse(url="/admin/settings#pwa", status_code=303)

        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            return RedirectResponse(url="/admin/settings#pwa", status_code=303)

        data = await upload.read()
        # サイズ上限（10MB）。巨大ファイル・画素爆弾によるメモリ枯渇を防ぐ
        MAX_ICON_BYTES = 10 * 1024 * 1024
        if len(data) > MAX_ICON_BYTES:
            return RedirectResponse(url="/admin/settings#pwa", status_code=303)

        # 現在店舗の公開ディレクトリ（nginx 直接配信のルート）へ書き出す
        store = getattr(request.state, "store", None)
        if store is not None and getattr(store, "public_dir", None):
            public_dir = store.public_dir
        else:
            public_dir = PUBLIC_HTML_DIR
        if not public_dir:
            raise ValueError("公開ディレクトリ（PUBLIC_HTML_DIR）が未設定です。")

        # 長辺38pxへ縮小して保存（枠なし・単一画像）
        _pwa.generate_app_icon(data, public_dir, 38)

        ver = uuid.uuid4().hex[:8]
        await db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('app_icon_ver', ?)",
            (ver,),
        )
        await db.commit()

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[admin] app icon upload error: {e}", flush=True)

    return RedirectResponse(url="/admin/settings#pwa", status_code=303)


@router.post("/settings/pwa/background")
async def upload_background(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """待機画面（view / html）の背景画像を登録／ON・OFFする。
    画像は1枚のみ。長辺最大1200pxへ縮小して {public_dir}/pwa/bg.png へ保存する。
    チェックボックス bg_enabled で有効／無効を切り替える（画像未添付でも切替のみ反映）。"""
    import os, uuid
    from fastapi.responses import RedirectResponse
    from app import pwa as _pwa
    try:
        form = await request.form()
        bg_enabled = "1" if form.get("bg_enabled") else "0"

        # 画像が添付されていれば長辺1200pxへ縮小して保存＋バージョン更新
        upload = form.get("bg_image")
        if upload is not None and hasattr(upload, "filename") and upload.filename:
            ext = os.path.splitext(upload.filename)[1].lower()
            if ext in (".png", ".jpg", ".jpeg", ".webp"):
                data = await upload.read()
                MAX_BG_BYTES = 20 * 1024 * 1024  # 20MB
                if len(data) <= MAX_BG_BYTES:
                    store = getattr(request.state, "store", None)
                    if store is not None and getattr(store, "public_dir", None):
                        public_dir = store.public_dir
                    else:
                        public_dir = PUBLIC_HTML_DIR
                    if not public_dir:
                        raise ValueError("公開ディレクトリ（PUBLIC_HTML_DIR）が未設定です。")
                    _pwa.generate_background(data, public_dir, 1200)
                    ver = uuid.uuid4().hex[:8]
                    await db.execute(
                        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('bg_ver', ?)",
                        (ver,),
                    )

        # ON/OFF は常に反映
        await db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('bg_enabled', ?)",
            (bg_enabled,),
        )
        await db.commit()

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[admin] background upload error: {e}", flush=True)

    return RedirectResponse(url="/admin/settings#pwa", status_code=303)


@router.post("/settings/pwa/slideshow")
async def upload_slideshow(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """待機画面（view / html）のスライドショーを登録／ON・OFFする。
    画像は最大10枚。各画像を長辺最大1200pxへ縮小して {public_dir}/pwa/slide-NN.png へ保存する。
    画像を選んだ場合は「登録済みを丸ごと差し替え」。チェックボックス slideshow_enabled で
    有効／無効を切り替える（画像未添付でも切替のみ反映）。"""
    import os, uuid
    from fastapi.responses import RedirectResponse
    from app import pwa as _pwa
    try:
        form = await request.form()
        slideshow_enabled = "1" if form.get("slideshow_enabled") else "0"

        # 添付された画像（複数）。name="slides" を複数取得
        uploads = form.getlist("slides") if hasattr(form, "getlist") else []
        valid = [u for u in uploads if u is not None and hasattr(u, "filename") and u.filename]

        if valid:
            store = getattr(request.state, "store", None)
            if store is not None and getattr(store, "public_dir", None):
                public_dir = store.public_dir
            else:
                public_dir = PUBLIC_HTML_DIR
            if not public_dir:
                raise ValueError("公開ディレクトリ（PUBLIC_HTML_DIR）が未設定です。")

            MAX_SLIDE_BYTES = 20 * 1024 * 1024  # 1枚あたり20MB
            images: list[bytes] = []
            for u in valid[:_pwa.SLIDESHOW_MAX]:
                ext = os.path.splitext(u.filename)[1].lower()
                if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                    continue
                data = await u.read()
                if 0 < len(data) <= MAX_SLIDE_BYTES:
                    images.append(data)

            if images:
                saved = _pwa.generate_slideshow(images, public_dir, 1200)
                ver = uuid.uuid4().hex[:8]
                await db.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('slideshow_count', ?)",
                    (str(saved),),
                )
                await db.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('slideshow_ver', ?)",
                    (ver,),
                )

        # ON/OFF は常に反映
        await db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('slideshow_enabled', ?)",
            (slideshow_enabled,),
        )
        await db.commit()

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[admin] slideshow upload error: {e}", flush=True)

    return RedirectResponse(url="/admin/settings#pwa", status_code=303)


@router.post("/settings/pwa/slideshow/clear")
async def clear_slideshow(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """スライドショー画像をすべて削除し、枚数を0にする（ON/OFF設定は変更しない）。"""
    from fastapi.responses import RedirectResponse
    from app import pwa as _pwa
    try:
        store = getattr(request.state, "store", None)
        if store is not None and getattr(store, "public_dir", None):
            public_dir = store.public_dir
        else:
            public_dir = PUBLIC_HTML_DIR
        if public_dir:
            _pwa.clear_slideshow(public_dir)
        await db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('slideshow_count', '0')"
        )
        await db.commit()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[admin] slideshow clear error: {e}", flush=True)

    return RedirectResponse(url="/admin/settings#pwa", status_code=303)


async def upload_pwa_icon_for_store(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """店舗1（既定店舗）の設定画面から、指定したスラッグ店舗の共通アイコンを登録する。
    対象店舗の公開ディレクトリへ枠なし＋画面別の一式を生成し、対象店舗のDBへ
    バージョン（pwa_icon_ver）を保存する。"""
    import os, uuid, sqlite3
    from fastapi.responses import RedirectResponse
    from app import pwa as _pwa
    from app import registry
    redirect = RedirectResponse(url="/admin/settings#stores", status_code=303)
    try:
        if not IS_CLOUD:
            return redirect
        # 店舗1（スラッグなし・既定店舗）からのみ操作を許可する
        cur_store = getattr(request.state, "store", None)
        if cur_store is not None and getattr(cur_store, "slug", ""):
            return redirect

        form = await request.form()
        try:
            store_id = int(form.get("store_id") or 0)
        except (TypeError, ValueError):
            return redirect
        target = registry.get_store_by_id(store_id)
        if target is None or not getattr(target, "public_dir", None):
            return redirect

        upload = form.get("icon")
        if not upload or not hasattr(upload, "filename") or not upload.filename:
            return redirect
        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            return redirect
        data = await upload.read()
        if len(data) > 10 * 1024 * 1024:   # 10MB 上限
            return redirect

        # 対象店舗の公開ディレクトリへ一式生成
        _pwa.generate_icons(data, target.public_dir)

        # 対象店舗のDBへ ver を保存（キャッシュバスター・QR中央ロゴ/manifest用）
        ver = uuid.uuid4().hex[:8]
        con = sqlite3.connect(target.db_path)
        try:
            con.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('pwa_icon_ver', ?)",
                (ver,),
            )
            con.commit()
        finally:
            con.close()

        # 対象店舗の html 静的 manifest を更新（アイコン参照を反映）
        try:
            settings = _pwa.get_pwa_settings(None, db_path=target.db_path)
            _pwa.write_static_html_manifest(target.public_dir, settings, target.slug)
        except Exception as e:
            print(f"[admin] per-store pwa manifest refresh skipped: {e}", flush=True)

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[admin] per-store pwa icon upload error: {e}", flush=True)

    return redirect


@router.post("/settings/public-html/toggle")
async def toggle_public_html(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ナビバーのトグルからON/OFFを切り替え"""
    from fastapi.responses import JSONResponse
    if IS_CLOUD:
        # クラウドはGCS配信を使わない（常に無効固定）
        await db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('public_html_enabled', '0')"
        )
        await db.commit()
        return JSONResponse({"ok": True, "enabled": False, "locked": True})
    body = await request.json()
    want_enable = body.get("enabled", False)

    # ONにしようとしている場合は必須項目チェック
    if want_enable:
        async with db.execute("SELECT value FROM app_settings WHERE key='public_html_gcp_project'") as cur:
            proj = await cur.fetchone()
        async with db.execute("SELECT value FROM app_settings WHERE key='public_html_gcs_bucket'") as cur:
            bucket = await cur.fetchone()
        missing = []
        if not proj or not (proj["value"] or "").strip():
            missing.append("GCPプロジェクトID")
        if not bucket or not (bucket["value"] or "").strip():
            missing.append("GCSバケット名")
        if missing:
            return JSONResponse({
                "ok": False,
                "error": "未入力の必須項目があります：" + "、".join(missing)
            })

    enabled = "1" if want_enable else "0"
    await db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('public_html_enabled', ?)",
        (enabled,)
    )
    await db.commit()
    return JSONResponse({"ok": True, "enabled": enabled == "1"})


@router.get("/settings/public-html/diagnose")
async def diagnose_public_html(db: aiosqlite.Connection = Depends(get_db)):
    """GCS接続診断"""
    from fastapi.responses import JSONResponse
    import os
    result = {}

    # 設定確認
    async with db.execute("SELECT value FROM app_settings WHERE key='public_html_enabled'") as cur:
        r = await cur.fetchone()
    result["enabled"] = r["value"] if r else "未設定"

    async with db.execute("SELECT value FROM app_settings WHERE key='public_html_gcs_bucket'") as cur:
        r = await cur.fetchone()
    result["bucket"] = r["value"] if r else "未設定"

    async with db.execute("SELECT value FROM app_settings WHERE key='public_html_gcp_project'") as cur:
        r = await cur.fetchone()
    result["project"] = r["value"] if r else "未設定"

    # キーファイル確認
    from app.services.public_html import _find_key_file
    key_path = _find_key_file()
    result["key_file"] = key_path or "見つからない"
    result["key_exists"] = os.path.exists(key_path) if key_path else False

    # GCS接続テスト
    if key_path and os.path.exists(key_path) and result["bucket"] not in ("未設定", ""):
        try:
            from google.cloud import storage
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_file(
                key_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            client = storage.Client(credentials=credentials)
            bucket = client.bucket(result["bucket"])
            blob = bucket.blob("_test_connection.txt")
            blob.upload_from_string(b"ok", content_type="text/plain")
            blob.delete()
            result["gcs_test"] = "✅ 接続OK"
        except Exception as e:
            result["gcs_test"] = f"❌ {e}"
    else:
        result["gcs_test"] = "スキップ（設定不足）"

    return JSONResponse(result)


@router.get("/settings/public-html/status")
async def public_html_status(db: aiosqlite.Connection = Depends(get_db)):
    """参加者向けHTML配信の現在状態をJSONで返す（ナビバー用）"""
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT value FROM app_settings WHERE key='public_html_enabled'") as cur:
        row = await cur.fetchone()
    enabled = row and row["value"] == "1"
    async with db.execute("SELECT value FROM app_settings WHERE key='public_html_gcs_bucket'") as cur:
        b = await cur.fetchone()
    bucket = b["value"] if b else ""
    return JSONResponse({"enabled": enabled, "bucket": bucket})


@router.post("/settings/public-html/publish-now")
async def publish_now(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """手動で参加者向けHTMLを今すぐ配信"""
    from fastapi.responses import JSONResponse
    # 設定確認
    async with db.execute("SELECT value FROM app_settings WHERE key='public_html_enabled'") as cur:
        row = await cur.fetchone()
    if not row or row["value"] != "1":
        return JSONResponse({"ok": False, "error": "配信機能が無効です"})
    # 配信実行
    print("[public_html] publish_now called", flush=True)
    from app.services.public_html import export_current_html
    success = await export_current_html(db)
    print(f"[public_html] publish_now result: {success}", flush=True)
    if success:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": "配信に失敗しました。設定を確認してください。"})


@router.post("/settings/certificate-templates/delete/{tid}", response_class=HTMLResponse)
async def delete_cert_template(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """賞状テンプレート削除"""
    await db.execute("DELETE FROM certificate_templates WHERE id=?", (tid,))
    await db.commit()
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/settings", status_code=303)


@router.get("/settings/certificate-templates/new", response_class=HTMLResponse)
async def new_cert_template(request: Request):
    """賞状テンプレート新規作成画面"""
    return templates.TemplateResponse("admin/certificate_template_edit.html", {
        "request": request,
        "tpl": None,
    })


@router.post("/settings/certificate-templates/create", response_class=HTMLResponse)
async def create_cert_template(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """賞状テンプレート作成"""
    form = await request.form()
    name = form.get("name", "").strip() or "新規テンプレート"
    paper_size = form.get("paper_size", "A4")
    orientation = form.get("orientation", "portrait")
    # 新規作成画面のエディタで配置したレイアウトも保存する。
    # （これを受け取らないと「配置して保存」した内容が破棄されていた）
    layout_json = form.get("layout_json", "{}")
    await db.execute(
        "INSERT INTO certificate_templates (name, paper_size, orientation, layout_json) "
        "VALUES (?, ?, ?, ?)",
        (name, paper_size, orientation, layout_json)
    )
    async with db.execute("SELECT last_insert_rowid() AS id") as cur:
        new_id = (await cur.fetchone())["id"]
    await db.commit()
    from fastapi.responses import RedirectResponse
    # 作成直後は編集画面へ遷移する（背景画像のアップロードはID確定後にのみ可能なため）
    return RedirectResponse(
        url=f"/admin/settings/certificate-templates/{new_id}/edit", status_code=303)


@router.get("/settings/certificate-templates/{tid}/edit", response_class=HTMLResponse)
async def edit_cert_template(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """賞状テンプレート編集画面"""
    import json as _json
    async with db.execute("SELECT * FROM certificate_templates WHERE id=?", (tid,)) as cur:
        tpl = await cur.fetchone()
    if not tpl:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/admin/settings", status_code=303)
    # layout_json を Python dict に変換してテンプレートに渡す
    tpl_dict = dict(tpl)
    lj = (tpl_dict.get("layout_json") or "").strip()
    layout = {}
    if lj and lj not in ("", "{}", "null"):
        try:
            parsed = _json.loads(lj)
            if isinstance(parsed, dict):
                layout = parsed
        except Exception:
            pass
    tpl_dict["layout"] = layout
    return templates.TemplateResponse("admin/certificate_template_edit.html", {
        "request": request,
        "tpl": tpl_dict,
    })


@router.post("/settings/certificate-templates/{tid}/update", response_class=HTMLResponse)
async def update_cert_template(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """賞状テンプレート更新（エディタのlayout_jsonも保存）"""
    form = await request.form()
    name = form.get("name", "").strip() or "新規テンプレート"
    paper_size = form.get("paper_size", "A4")
    orientation = form.get("orientation", "portrait")
    layout_json = form.get("layout_json", "{}")
    await db.execute(
        """UPDATE certificate_templates
           SET name=?, paper_size=?, orientation=?, layout_json=?,
               updated_at=datetime('now','localtime')
           WHERE id=?""",
        (name, paper_size, orientation, layout_json, tid)
    )
    await db.commit()
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/admin/settings/certificate-templates/{tid}/edit", status_code=303)


@router.post("/settings/certificate-templates/{tid}/upload-bg")
async def upload_cert_bg(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """賞状テンプレートの背景画像をアップロードして保存"""
    import uuid, os, json
    from fastapi.responses import JSONResponse
    try:
        form = await request.form()
        upload = form.get("file")
        if not upload or not hasattr(upload, "filename") or not upload.filename:
            return JSONResponse({"error": "ファイルがありません"}, status_code=400)

        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp"):
            return JSONResponse({"error": f"対応外の形式です ({ext})"}, status_code=400)

        fname    = f"cert_{tid}_{uuid.uuid4().hex[:8]}{ext}"
        base_dir = _static_subdir("cert_bg")
        dest     = os.path.join(base_dir, fname)

        # await で非同期読み込み → 同期書き込み
        data = await upload.read()
        # サイズ上限（10MB）。巨大ファイルによるディスク／メモリ枯渇を防ぐ
        MAX_BG_BYTES = 20 * 1024 * 1024  # 20MB
        if len(data) > MAX_BG_BYTES:
            return JSONResponse({"error": "ファイルが大きすぎます（上限20MB）"}, status_code=400)
        with open(dest, "wb") as fp:
            fp.write(data)

        url = f"/static/cert_bg/{fname}"

        # layout_json の bg_image_url だけ更新
        async with db.execute("SELECT layout_json FROM certificate_templates WHERE id=?", (tid,)) as cur:
            row = await cur.fetchone()
        layout = {}
        if row and row["layout_json"] and row["layout_json"] not in ("", "{}"):
            try:
                layout = json.loads(row["layout_json"])
            except Exception:
                pass
        layout["bg_image_url"] = url
        await db.execute(
            "UPDATE certificate_templates SET layout_json=?, updated_at=datetime('now','localtime') WHERE id=?",
            (json.dumps(layout, ensure_ascii=False), tid)
        )
        await db.commit()
        return JSONResponse({"url": url})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


# =====================================================================
# QR/バーコード印刷テンプレート（card_templates）
#   賞状テンプレート（certificate_templates）と同じ要領のCRUD＋背景アップロード
# =====================================================================
@router.post("/settings/card-templates/delete/{tid}", response_class=HTMLResponse)
async def delete_card_template(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """カードテンプレート削除"""
    await db.execute("DELETE FROM card_templates WHERE id=?", (tid,))
    await db.commit()
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/settings", status_code=303)


@router.get("/settings/card-templates/new", response_class=HTMLResponse)
async def new_card_template(request: Request):
    """カードテンプレート新規作成画面"""
    return templates.TemplateResponse("admin/card_template_edit.html", {
        "request": request,
        "tpl": None,
    })


@router.post("/settings/card-templates/create", response_class=HTMLResponse)
async def create_card_template(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """カードテンプレート作成"""
    form = await request.form()
    name = form.get("name", "").strip() or "新規テンプレート"
    card_size = form.get("card_size", "meishi")
    code_type = form.get("code_type", "qr")
    # 新規作成画面のエディタで配置したレイアウトも保存する。
    # （これを受け取らないと「配置して保存」した内容が破棄されていた）
    layout_json = form.get("layout_json", "{}")
    await db.execute(
        "INSERT INTO card_templates (name, card_size, code_type, layout_json) "
        "VALUES (?, ?, ?, ?)",
        (name, card_size, code_type, layout_json)
    )
    async with db.execute("SELECT last_insert_rowid() AS id") as cur:
        new_id = (await cur.fetchone())["id"]
    await db.commit()
    from fastapi.responses import RedirectResponse
    # 作成直後は編集画面へ遷移する（背景画像のアップロードはID確定後にのみ可能なため）
    return RedirectResponse(
        url=f"/admin/settings/card-templates/{new_id}/edit", status_code=303)


@router.get("/settings/card-templates/{tid}/edit", response_class=HTMLResponse)
async def edit_card_template(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """カードテンプレート編集画面"""
    import json as _json
    async with db.execute("SELECT * FROM card_templates WHERE id=?", (tid,)) as cur:
        tpl = await cur.fetchone()
    if not tpl:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/admin/settings", status_code=303)
    tpl_dict = dict(tpl)
    lj = (tpl_dict.get("layout_json") or "").strip()
    layout = {}
    if lj and lj not in ("", "{}", "null"):
        try:
            parsed = _json.loads(lj)
            if isinstance(parsed, dict):
                layout = parsed
        except Exception:
            pass
    tpl_dict["layout"] = layout
    return templates.TemplateResponse("admin/card_template_edit.html", {
        "request": request,
        "tpl": tpl_dict,
    })


@router.post("/settings/card-templates/{tid}/update", response_class=HTMLResponse)
async def update_card_template(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """カードテンプレート更新（エディタのlayout_jsonも保存）"""
    form = await request.form()
    name = form.get("name", "").strip() or "新規テンプレート"
    card_size = form.get("card_size", "meishi")
    code_type = form.get("code_type", "qr")
    layout_json = form.get("layout_json", "{}")
    await db.execute(
        """UPDATE card_templates
           SET name=?, card_size=?, code_type=?, layout_json=?,
               updated_at=datetime('now','localtime')
           WHERE id=?""",
        (name, card_size, code_type, layout_json, tid)
    )
    await db.commit()
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/admin/settings/card-templates/{tid}/edit", status_code=303)


@router.post("/settings/card-templates/{tid}/upload-bg")
async def upload_card_bg(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """カードテンプレートの背景画像をアップロードして保存"""
    import uuid, os, json
    from fastapi.responses import JSONResponse
    try:
        form = await request.form()
        upload = form.get("file")
        if not upload or not hasattr(upload, "filename") or not upload.filename:
            return JSONResponse({"error": "ファイルがありません"}, status_code=400)

        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp"):
            return JSONResponse({"error": f"対応外の形式です ({ext})"}, status_code=400)

        fname    = f"card_{tid}_{uuid.uuid4().hex[:8]}{ext}"
        base_dir = _static_subdir("card_bg")
        dest     = os.path.join(base_dir, fname)

        data = await upload.read()
        MAX_BG_BYTES = 20 * 1024 * 1024  # 20MB
        if len(data) > MAX_BG_BYTES:
            return JSONResponse({"error": "ファイルが大きすぎます（上限20MB）"}, status_code=400)
        with open(dest, "wb") as fp:
            fp.write(data)

        url = f"/static/card_bg/{fname}"

        async with db.execute("SELECT layout_json FROM card_templates WHERE id=?", (tid,)) as cur:
            row = await cur.fetchone()
        layout = {}
        if row and row["layout_json"] and row["layout_json"] not in ("", "{}"):
            try:
                layout = json.loads(row["layout_json"])
            except Exception:
                pass
        layout["bg_image_url"] = url
        await db.execute(
            "UPDATE card_templates SET layout_json=?, updated_at=datetime('now','localtime') WHERE id=?",
            (json.dumps(layout, ensure_ascii=False), tid)
        )
        await db.commit()
        return JSONResponse({"url": url})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


# ============================================================
# BGM（ヘッダー再生・設定登録）
# ============================================================

@router.post("/settings/bgm/save")
async def save_bgm(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """設定画面の BGM フォーム（最大10曲）を保存する。

    各行 i（1..BGM_MAX）について:
      - bgm_delete_i にチェック  → その行の mp3 とメタデータを削除
      - bgm_file_i に mp3 添付   → bgm-0i.mp3 へ上書き保存し、名前/備考/シーンを更新
      - ファイル未添付           → 既存行の備考/シーンのみ更新（曲は据え置き）
    メタデータは app_settings.bgm_tracks（JSON配列）に保存し、bgm_ver を更新する。
    """
    import os as _os, json as _json, uuid as _uuid
    from fastapi.responses import RedirectResponse

    try:
        form = await request.form()
        bgm_dir = _bgm_dir(request)
        if not bgm_dir:
            raise ValueError("BGMの保存先ディレクトリが未設定です（クラウド版は PUBLIC_HTML_DIR を確認）。")
        _os.makedirs(bgm_dir, exist_ok=True)

        # 既存メタデータを slot 辞書へ
        async with db.execute("SELECT value FROM app_settings WHERE key='bgm_tracks'") as cur:
            row = await cur.fetchone()
        try:
            existing = _json.loads(row["value"]) if row and row["value"] else []
        except Exception:
            existing = []
        by_slot = {}
        for t in existing:
            try:
                by_slot[int(t.get("slot"))] = dict(t)
            except Exception:
                pass

        for i in range(1, BGM_MAX + 1):
            delete = bool(form.get(f"bgm_delete_{i}"))
            note = (form.get(f"bgm_note_{i}") or "").strip()
            scene = (form.get(f"bgm_scene_{i}") or "").strip()
            if scene not in BGM_SCENES:
                scene = ""

            path = _os.path.join(bgm_dir, _bgm_slot_name(i))

            if delete:
                try:
                    if _os.path.exists(path):
                        _os.remove(path)
                except Exception:
                    pass
                by_slot.pop(i, None)
                continue

            up = form.get(f"bgm_file_{i}")
            has_file = up is not None and hasattr(up, "filename") and up.filename

            if has_file:
                ext = _os.path.splitext(up.filename)[1].lower()
                if ext != ".mp3":
                    # mp3 以外は無視（他の行は処理継続）
                    continue
                data = await up.read()
                if not (0 < len(data) <= BGM_MAX_BYTES):
                    continue
                with open(path, "wb") as f:
                    f.write(data)
                by_slot[i] = {
                    "slot": i,
                    "name": _os.path.basename(up.filename),
                    "note": note,
                    "scene": scene,
                }
            else:
                # 添付なし：既存行があればメタのみ更新
                if i in by_slot:
                    by_slot[i]["note"] = note
                    by_slot[i]["scene"] = scene

        tracks = [by_slot[k] for k in sorted(by_slot.keys())]
        ver = _uuid.uuid4().hex[:8]
        await db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('bgm_tracks', ?)",
            (_json.dumps(tracks, ensure_ascii=False),),
        )
        await db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('bgm_ver', ?)",
            (ver,),
        )
        await db.commit()

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[admin] bgm save error: {e}", flush=True)

    return RedirectResponse(url="/admin/settings#bgm", status_code=303)


@router.get("/bgm/list")
async def bgm_list(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ヘッダーのBGMポップアップ用：登録済み曲の一覧をJSONで返す。"""
    import json as _json
    from fastapi.responses import JSONResponse as _JSONResponse

    async with db.execute("SELECT value FROM app_settings WHERE key='bgm_tracks'") as cur:
        row = await cur.fetchone()
    try:
        tracks = _json.loads(row["value"]) if row and row["value"] else []
    except Exception:
        tracks = []
    async with db.execute("SELECT value FROM app_settings WHERE key='bgm_ver'") as cur:
        vrow = await cur.fetchone()
    ver = vrow["value"] if vrow and vrow["value"] else ""

    # 実ファイルが存在する曲だけ返す
    bgm_dir = _bgm_dir(request)
    out = []
    for t in tracks:
        try:
            slot = int(t.get("slot"))
        except Exception:
            continue
        if bgm_dir:
            import os as _os
            if not _os.path.exists(_os.path.join(bgm_dir, _bgm_slot_name(slot))):
                continue
        out.append({
            "slot": slot,
            "name": t.get("name") or f"BGM {slot}",
            "note": t.get("note") or "",
            "scene": t.get("scene") or "",
        })
    return _JSONResponse({"tracks": out, "ver": ver})


@router.get("/bgm/file/{slot}")
async def bgm_file(slot: int, request: Request):
    """登録済みBGM（mp3）を返す。オンプレ/クラウド共通でこのルート経由で配信する。"""
    import os as _os
    from fastapi.responses import FileResponse, Response

    if slot < 1 or slot > BGM_MAX:
        return Response(status_code=404)
    bgm_dir = _bgm_dir(request)
    if not bgm_dir:
        return Response(status_code=404)
    path = _os.path.join(bgm_dir, _bgm_slot_name(slot))
    if not _os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(path, media_type="audio/mpeg", filename=_bgm_slot_name(slot))
