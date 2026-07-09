"""
参加者向けHTML書き出しのデバウンス＋シングルフライト制御。

従来:
    結果入力・組確定・ラウンド操作のたびに
        asyncio.create_task(export_current_html(db))
    が呼ばれ、同一店舗の同じHTMLをフル再レンダリングするタスクが、デバウンスも
    排他もないまま何本も同時に走っていた。並び順（ポイント制）予選のように
    スキャン→組確定→結果入力が数秒間隔で連続する場面では、重い書き出しが
    5〜10本並走してCPUピークとレイテンシを悪化させ、さらに create_task の戻り値を
    保持しないため「タスクがGCで消えて観覧HTMLがたまに更新されない」不具合の
    温床にもなっていた。

本モジュール:
    schedule_publish() を何回呼んでも、
        「最後の呼び出しから DEBOUNCE_SEC 静かになった時点で1回だけ」書き出す。
    書き出し中に新たな変更要求が来た場合は、完了後にもう1回だけ実行して取りこぼしを防ぐ。
    店舗ごとに独立して制御する（複数店舗対応）。

店舗コンテキストの扱い:
    従来の create_task は「呼び出し時点の ContextVar(current_store) を自動コピー」して
    正しい店舗のDB/配信先で書き出していた。デバウンスは遅延実行のため、遅延後には
    元リクエストの ContextVar が失われている。そこで
        予約時に「現在の店舗ID」をキャプチャ → 実行時に registry から店舗を復元し、
        current_store.set() で明示的に文脈を張ってから export_current_html() を呼ぶ。
    オンプレ版（current_store が常に None）では store_id=0 として扱い、
    export_current_html() が従来どおり既定DBへ書き出す（挙動不変）。

観覧側への影響:
    参加者向けHTMLはもともと30秒ポーリング（ETag条件付きGET）で更新確認するため、
    最大 DEBOUNCE_SEC（0.8秒）の書き出し遅延は知覚されない。
"""
from __future__ import annotations
import asyncio

# 最後の変更要求からこの秒数だけ静かになったら1回書き出す。
DEBOUNCE_SEC = 0.8

_lock = asyncio.Lock()
_pending: dict[int, asyncio.Task] = {}   # store_id -> デバウンス待ちタスク
_running: dict[int, bool] = {}           # store_id -> 書き出し実行中フラグ
_dirty: dict[int, bool] = {}             # 実行中に来た再要求フラグ


def _current_store_id() -> int:
    """現在のリクエストが属する店舗ID。未解決（オンプレ／単一店舗）は 0。"""
    try:
        from app.store_context import current_store
        s = current_store.get()
        return s.id if s else 0
    except Exception:
        return 0


def schedule_publish() -> None:
    """現在の店舗の参加者向けHTML書き出しを予約する。

    何度呼んでも安全（冪等）。呼び出し側は結果を待たない（fire-and-forget）。
    イベントループが無い文脈（起動前など）では静かに何もしない。
    """
    key = _current_store_id()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_schedule(key))


async def _schedule(key: int) -> None:
    async with _lock:
        if _running.get(key):
            # 既に書き出し中 → 終了後にもう1回だけ実行させる
            _dirty[key] = True
            return
        old = _pending.get(key)
        if old and not old.done():
            old.cancel()                 # 直前のデバウンス待ちを延長（キャンセルして張り直し）
        _pending[key] = asyncio.get_running_loop().create_task(_debounced_run(key))


async def _debounced_run(key: int) -> None:
    try:
        await asyncio.sleep(DEBOUNCE_SEC)
    except asyncio.CancelledError:
        return                           # 後続の予約に置き換えられた → このタスクは破棄
    async with _lock:
        _pending.pop(key, None)
        _running[key] = True
        _dirty[key] = False

    try:
        await _do_export(key)
    except Exception as e:
        print(f"[publish] export error (store={key}): {e}", flush=True)
    finally:
        rerun = False
        async with _lock:
            _running[key] = False
            if _dirty.get(key):
                rerun = True
                _dirty[key] = False
        if rerun:
            # 書き出し中に入った変更を反映するため、もう1周だけ回す
            await _schedule(key)


async def _do_export(key: int) -> None:
    """店舗コンテキストを復元して export_current_html() を1回実行する。"""
    from app.services.public_html import export_current_html
    from app.store_context import current_store

    store = None
    if key:
        try:
            from app import registry
            store = registry.get_store_by_id(key)
        except Exception:
            store = None

    token = current_store.set(store)     # store=None（オンプレ）でも安全
    try:
        await export_current_html()
    finally:
        current_store.reset(token)
