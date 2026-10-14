"""参加者向けHTML（観覧ページ）への即時push用の軽量 pub/sub（SSE基盤）。

目的:
    従来は観覧ページが 10秒ごとにポーリング（/enter?api=content・status・telop）して
    更新を拾っていた。本モジュールは「アドミンが更新した瞬間」にサーバーから
    観覧端末へ Server-Sent Events(SSE) で合図を送り、端末はその場で更新確認を走らせる。
    これにより体感の反映遅延が最大10秒 → ほぼ即時（1秒未満）になる。

設計:
    - プロセス内メモリのみ（外部ブローカー不要）。store_id ごとに購読者集合を持つ。
    - 購読者1人 = 1本のSSE接続 = 1つの asyncio.Queue。
    - publish(store_id, event) は該当店舗の全キューへ非ブロッキング投入する。
      キューが詰まっている（＝受信が遅い）端末は取りこぼしても、観覧ページ側の
      フォールバック・ポーリング（SSE断時は10秒間隔に戻る）で必ず追従するため、
      ここでは「落としても壊れない」方針で put_nowait のみ行う。
    - 1店舗あたりの同時接続数は MAX_PER_STORE で上限。公開エンドポイントのため
      ランダム接続の大量生成（DoS）でメモリ枯渇しないようにする。上限超過の購読は
      None を返し、その接続はポーリングのみで動作する（機能縮退で安全側）。

スレッド/ループ:
    FastAPI(uvicorn) の単一イベントループ上で subscribe/publish とも呼ばれる前提。
    put_nowait は同期APIなので publish は同期関数でよい（await 不要）。
"""
from __future__ import annotations

import asyncio

# 1店舗あたりの同時SSE接続の上限（DoS・メモリ枯渇対策）。到達後の新規購読は拒否。
MAX_PER_STORE = 500
# 各購読キューの最大保持イベント数。受信が遅い端末向けの小さなバッファ。
_QUEUE_MAX = 16

# store_id -> set[asyncio.Queue]
_subs: dict[int, set[asyncio.Queue]] = {}


def subscribe(store_id: int) -> asyncio.Queue | None:
    """SSE接続1本ぶんの購読キューを作る。上限超過時は None。"""
    sid = int(store_id or 0)
    conns = _subs.setdefault(sid, set())
    if len(conns) >= MAX_PER_STORE:
        return None
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    conns.add(q)
    return q


def unsubscribe(store_id: int, q: asyncio.Queue) -> None:
    """接続終了時に購読を解除する。"""
    sid = int(store_id or 0)
    conns = _subs.get(sid)
    if not conns:
        return
    conns.discard(q)
    if not conns:
        _subs.pop(sid, None)


def publish(store_id: int, event: str = "update") -> int:
    """該当店舗の全購読者へイベントを配る。配れた件数を返す（非ブロッキング）。

    event は SSE の event 名（"update" / "telop" など）。キュー満杯の端末は
    取りこぼすが、観覧側のフォールバック・ポーリングで追従するため問題ない。
    """
    sid = int(store_id or 0)
    conns = _subs.get(sid)
    if not conns:
        return 0
    sent = 0
    for q in list(conns):
        try:
            q.put_nowait(event)
            sent += 1
        except asyncio.QueueFull:
            # 受信が遅い端末。取りこぼしてもフォールバックで追従するので無視。
            pass
    return sent


def subscriber_count(store_id: int | None = None) -> int:
    """診断用：購読中（＝SSE接続中）の数。store_id 省略で全店舗合計。"""
    if store_id is None:
        return sum(len(s) for s in _subs.values())
    return len(_subs.get(int(store_id or 0), set()))
