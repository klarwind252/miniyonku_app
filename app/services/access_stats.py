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
_MAX_META_PER_STORE = 2000   # 端末メタの店舗別上限（メモリ枯渇＝DoS対策）
_lock = threading.Lock()

_live: dict = {}        # (store_id, tid) -> {cid: last_seen_ts}
_peak: dict = {}        # (store_id, tid) -> int
_uniq: dict = {}        # (store_id, tid) -> set(cid)

# 端末ごとの詳細（有効端末一覧の表示用）。store_id -> {cid: {...}}
# 保持内容: first(初回接続) / last(最終心拍) / tid(直近の大会ID) / ua(UA要約) / hits(心拍数)
_meta: dict = {}


def record_hit(store_id, tid, cid: str, ua: str = "", via: str = "") -> None:
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
        # --- 端末メタ更新 ---
        md = _meta.get(store_id)
        if md is None:
            md = {}
            _meta[store_id] = md
        m = md.get(cid)
        if m is None:
            # 新規端末を追加する前に、上限超過なら最も古い心拍のものから間引く。
            # /api/telop は公開エンドポイントのため、ランダムcidの大量送信で
            # メタが無限に膨らむのを防ぐ（DoS対策）。
            if len(md) >= _MAX_META_PER_STORE:
                overflow = len(md) - _MAX_META_PER_STORE + 1
                for old_cid in sorted(md, key=lambda c: md[c].get("last", 0))[:overflow]:
                    md.pop(old_cid, None)
            m = {"first": now, "last": now, "tid": tid, "ua": _summarize_ua(ua), "hits": 0}
            md[cid] = m
        m["last"] = now
        m["tid"] = tid
        m["hits"] = m.get("hits", 0) + 1
        if via:
            m["via"] = via
        if ua:
            m["ua"] = _summarize_ua(ua)
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
        if len(u) < _MAX_META_PER_STORE:
            u.add(cid)   # 上限到達後は頭打ち（DoS対策・延べ人数は概算で十分）
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


def _summarize_ua(ua: str) -> str:
    """User-Agent を「OS / ブラウザ」程度のごく短い表記に丸める（一覧表示用）。"""
    if not ua:
        return ""
    u = ua
    ul = u.lower()
    # OS
    if "iphone" in ul or "ipad" in ul or ("mac os" in ul and "mobile" in ul):
        os_name = "iOS"
    elif "android" in ul:
        os_name = "Android"
    elif "windows" in ul:
        os_name = "Windows"
    elif "mac os" in ul or "macintosh" in ul:
        os_name = "Mac"
    elif "linux" in ul:
        os_name = "Linux"
    else:
        os_name = "その他"
    # ブラウザ（順序に注意：Edge/Chrome/Safari）
    if "edg/" in ul or "edga" in ul or "edgios" in ul:
        br = "Edge"
    elif "crios" in ul or "chrome" in ul:
        br = "Chrome"
    elif "firefox" in ul or "fxios" in ul:
        br = "Firefox"
    elif "safari" in ul:
        br = "Safari"
    else:
        br = "ブラウザ"
    return f"{os_name} / {br}"


def live_devices(store_id, window: int | None = None) -> list[dict]:
    """現在アクセス有効（直近 window 秒以内に心拍あり）の端末一覧を返す。

    各要素: {cid, tid, first, last, ua, hits, idle}
      - first/last : epoch 秒（表示側で整形）
      - idle       : 最終心拍からの経過秒
    最終心拍が新しい順に並べて返す。
    """
    win = _WINDOW if window is None else window
    now = time.time()
    out: list[dict] = []
    with _lock:
        md = _meta.get(store_id, {})
        # 掃除も兼ねる：十分古い端末（window*3 と 300秒 の大きい方を超過）はメタから落とす。
        # 小さな window で照会しても、まだ有効な端末のメタを誤って消さないようにする。
        purge_after = max(win * 3, 300)
        stale = [c for c, m in md.items() if now - m.get("last", 0) > purge_after]
        for c in stale:
            md.pop(c, None)
        for cid, m in md.items():
            idle = now - m.get("last", 0)
            if idle <= win:
                out.append({
                    "cid": cid,
                    "tid": m.get("tid", 0),
                    "first": m.get("first", 0),
                    "last": m.get("last", 0),
                    "ua": m.get("ua", ""),
                    "hits": m.get("hits", 0),
                    "via": m.get("via", ""),
                    "idle": int(idle),
                })
    out.sort(key=lambda x: x["last"], reverse=True)
    return out
