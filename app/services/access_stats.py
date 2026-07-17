"""参加者html のアクセス統計（大会ごとの 現在同時接続 / ピーク / 延べ視聴者）。

参加者htmlは静的配信のためアプリに直接アクセスが来ない。そこで、テロップ確認の
/api/telop ポーリング（30秒ごと）に端末ID(cid)と大会ID(tid)を相乗りさせ、その
リクエストを心拍として集計する（新規の通信は増やさない）。

集計はメモリ保持（アプリ再起動でリセット）。大会当日は再起動しない前提で、当日の
ピーク・延べは保持される。店舗ごとに分離する。
"""
from __future__ import annotations

import threading
import time

_WINDOW = 90            # この秒数内に心拍のあった端末を「現在接続中」とみなす
_lock = threading.Lock()

_live: dict = {}        # (store_id, tid) -> {cid: last_seen_ts}
_peak: dict = {}        # (store_id, tid) -> int
_uniq: dict = {}        # (store_id, tid) -> set(cid)


def record_hit(store_id, tid, cid: str) -> None:
    """参加者htmlからの心拍を1件記録する。cid が空なら無視（＝viewや無効値）。"""
    if not cid:
        return
    try:
        tid = int(tid or 0)
    except (TypeError, ValueError):
        tid = 0
    now = time.time()
    key = (store_id, tid)
    with _lock:
        d = _live.get(key)
        if d is None:
            d = {}
            _live[key] = d
        d[cid] = now
        # 期限切れ端末を掃除
        stale = [c for c, ts in d.items() if now - ts > _WINDOW]
        for c in stale:
            del d[c]
        u = _uniq.get(key)
        if u is None:
            u = set()
            _uniq[key] = u
        u.add(cid)
        cur = len(d)
        if cur > _peak.get(key, 0):
            _peak[key] = cur


def _current(key, now) -> int:
    d = _live.get(key)
    if not d:
        return 0
    return sum(1 for ts in d.values() if now - ts <= _WINDOW)


def snapshot(store_id) -> dict:
    """{tid: {'current':c, 'peak':p, 'uniq':u}} を返す（指定店舗分のみ）。"""
    now = time.time()
    out: dict = {}
    with _lock:
        tids = set()
        for src in (_live, _peak, _uniq):
            for (sid, tid) in src.keys():
                if sid == store_id:
                    tids.add(tid)
        for tid in tids:
            key = (store_id, tid)
            out[tid] = {
                "current": _current(key, now),
                "peak": _peak.get(key, 0),
                "uniq": len(_uniq.get(key, ())),
            }
    return out
