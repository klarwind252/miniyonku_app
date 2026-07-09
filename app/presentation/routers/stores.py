"""
複数店舗展開：店舗マスタ管理ルーター（クラウド版）。

マウント: /admin/stores
権限: 店舗1（既定店舗・slug="")からのみ操作可能。店舗2〜から呼ばれた場合は 403。
       （オンプレ版では IS_CLOUD=False のため、そもそも店舗が解決されず弾かれる）

操作: 追加 / 編集（名前・スラッグ・有効無効）/ トークン再生成 / 削除（アーカイブ保持）。
いずれも処理後に /admin/settings#stores へリダイレクトする（既存の設定画面の流儀に合わせる）。
"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, PlainTextResponse

from app.core.config import IS_CLOUD

router = APIRouter()


def _guard_default_store(request) -> str | None:
    """店舗1以外／非クラウドなら拒否理由を返す。OKなら None。"""
    if not IS_CLOUD:
        return "複数店舗管理はクラウド版でのみ利用できます。"
    store = getattr(request.state, "store", None)
    if store is None or store.slug:
        return "店舗の管理は店舗1（既定店舗）からのみ行えます。"
    return None


def _redirect_back():
    # 設定画面の複数店舗セクションへ戻る（resolver が店舗1配下なのでスラッグ不要）
    return RedirectResponse(url="/admin/settings#stores", status_code=303)


@router.post("/add")
async def add_store(request: Request, name: str = Form(""), slug: str = Form("")):
    err = _guard_default_store(request)
    if err:
        return PlainTextResponse(err, status_code=403)
    from app import registry
    from app.infrastructure.db.schema import init_db
    try:
        new_store = registry.add_store(name=name, slug=slug)
        # 追加直後の店舗DBをここで初期化する。
        # （公開/管理エンドポイントは init_db を呼ばないため、作成時に確実に
        #   スキーマを用意しておく。再起動を待たずに admin から利用可能になる）
        await init_db(new_store.db_path)
    except ValueError as e:
        return PlainTextResponse(f"店舗を追加できません: {e}", status_code=400)
    return _redirect_back()


@router.post("/update")
async def update_store(request: Request, store_id: int = Form(...),
                       name: str = Form(""), slug: str = Form(""),
                       enabled: str = Form("1"),
                       restrict_hours: str = Form("0"),
                       access_start: str = Form(""),
                       access_end: str = Form("")):
    err = _guard_default_store(request)
    if err:
        return PlainTextResponse(err, status_code=403)
    from app import registry
    try:
        registry.update_store(
            store_id,
            name=name or None,
            slug=(slug or None),
            enabled=(enabled == "1"),
            restrict_hours=(restrict_hours == "1"),
            access_start=access_start or None,
            access_end=access_end or None,
        )
    except ValueError as e:
        return PlainTextResponse(f"店舗を更新できません: {e}", status_code=400)
    return _redirect_back()


@router.post("/regenerate-tokens")
async def regenerate_tokens(request: Request, store_id: int = Form(...)):
    err = _guard_default_store(request)
    if err:
        return PlainTextResponse(err, status_code=403)
    from app import registry
    try:
        registry.regenerate_tokens(store_id)
    except ValueError as e:
        return PlainTextResponse(f"トークンを再生成できません: {e}", status_code=400)
    return _redirect_back()


@router.post("/delete")
async def delete_store(request: Request, store_id: int = Form(...)):
    err = _guard_default_store(request)
    if err:
        return PlainTextResponse(err, status_code=403)
    from app import registry
    try:
        registry.delete_store(store_id)
    except ValueError as e:
        return PlainTextResponse(f"店舗を削除できません: {e}", status_code=400)
    return _redirect_back()
