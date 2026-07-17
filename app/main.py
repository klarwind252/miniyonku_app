"""Composition Root。

責務は3つだけ：ミドルウェアの組立、ルーターの登録、起動処理の呼び出し。
業務ロジック・SQL・HTML生成はここには一切置かない。
"""
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import IS_CLOUD, DEPLOY_MODE, PUBLIC_BASE_URL, ADMIN_TOKEN, VIEW_TOKEN
from app.infrastructure.db.connection import DB_PATH
from app.infrastructure.db.schema import init_db
from app.infrastructure.db.bracket_repair import fix_bracket_slots_on_startup
from app.presentation.auth import add_auth
from app.presentation.onprem_auth import add_onprem_auth
from app.presentation.middleware.store_resolver import add_store_resolver
from app.presentation.middleware.security import add_security_headers

# ルーター（未移行モジュールは旧パスのまま＝互換シム経由で動作）
from app.routers import admin, tournaments
from app.routers.viewer import router as viewer_router
from app.routers.qualifying import router as qualifying_router
from app.routers.bracket import router as bracket_router
from app.routers.racers import router as racers_router
from app.presentation.routers.stores import router as stores_router
from app.presentation.routers.public_entry import router as public_entry_router
from app.presentation.routers.public_misc import router as public_misc_router
from app.presentation.routers.pwa_manifest import router as pwa_manifest_router

app = FastAPI(title="ミニ四駆レース管理システム", version="1.0.0")

# クラウド版のみ：固定トークン認証（店舗別）を有効化（オンプレ版では無効＝従来挙動）。
# ミドルウェアは「後に追加したものが外側（先に実行）」。店舗リゾルバを認証より外側に
# 置く必要があるため、add_auth を先、add_store_resolver を後に呼ぶ。
add_auth(app)
# オンプレ版のLAN公開時：ONPREM_ADMIN_PIN 設定時のみ /admin をPIN保護（既定は無効＝従来挙動）。
add_onprem_auth(app)
add_store_resolver(app)
# 最外周（最後に追加＝先に実行）：HTTPS強制とセキュリティヘッダを全応答へ。
add_security_headers(app)

BASE_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# 複数店舗（クラウド版）：スラッグ付き /static を配信する。
_STATIC_ROOT = os.path.join(BASE_DIR, "static")


@app.get("/{slug}/static/{path:path}")
async def store_static(slug: str, path: str):
    full = os.path.normpath(os.path.join(_STATIC_ROOT, path))
    # ディレクトリトラバーサル防止（_STATIC_ROOT 配下のみ許可）
    if not (full == _STATIC_ROOT or full.startswith(_STATIC_ROOT + os.sep)):
        raise HTTPException(status_code=404)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404)
    return FileResponse(full)


# 賞状背景画像保存先（/static/cert_bg/ で配信されるため別マウント不要）
os.makedirs(os.path.join(BASE_DIR, "static", "cert_bg"), exist_ok=True)

app.include_router(admin.router, prefix="/admin")
app.include_router(qualifying_router, prefix="/admin/tournaments")
app.include_router(bracket_router, prefix="/admin/tournaments")
app.include_router(racers_router, prefix="/admin/racers")
app.include_router(tournaments.router, prefix="/admin/tournaments")
app.include_router(viewer_router, prefix="/view")
app.include_router(stores_router, prefix="/admin/stores")
app.include_router(pwa_manifest_router)
app.include_router(public_entry_router)
app.include_router(public_misc_router)


@app.on_event("startup")
async def startup():
    if IS_CLOUD:
        # 複数店舗化：レジストリ（control.db）を用意し、店舗1（既定店舗）を移行登録。
        from app import registry
        registry.init_registry(default_admin_token=ADMIN_TOKEN, default_view_token=VIEW_TOKEN)
        stores = registry.list_stores(include_disabled=True)
        for st in stores:
            await init_db(st.db_path)
            await fix_bracket_slots_on_startup(st.db_path)
            print(f"[APP] 店舗 init: id={st.id} slug='{st.slug or '(default)'}' "
                  f"name={st.name} db={st.db_path}", flush=True)
        # DB自動バックアップ（毎晩03:30 JST・14世代保持）をクラウド版で開始
        from app.services import backup_scheduler
        backup_scheduler.launch()
        print(f"[APP] 起動完了（クラウド版 / 複数店舗 {len(stores)}件 / "
              f"DEPLOY_MODE={DEPLOY_MODE}）"
              f"{' / ' + PUBLIC_BASE_URL if PUBLIC_BASE_URL else ''}", flush=True)
    else:
        # オンプレ版：従来どおり単一DB。
        await init_db()
        await fix_bracket_slots_on_startup()
        print("[APP] 起動完了 → http://localhost:8000/admin/")