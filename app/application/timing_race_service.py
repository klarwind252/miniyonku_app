"""レース結果の組み立て（application層）。

保存済みの通過イベントとレイアウトを読み、domain の race_builder で
ラップ・セクター・合計・順位を組み立てる。DB読み出しと domain の橋渡し。

★20261031 性能：組み立て結果をプロセス内でキャッシュする。
  - PIP（3秒ポーリング×タブ数）／結果一覧（1日分＝最大500レース）／GW受信1回につき
    3回（速度計算・ベスト更新・状態判定）と、同じレースを何度も組み立て直していた。
  - timing_events は追記専用（更新なし・削除はレース削除のCASCADEのみ）なので、
    「イベント件数＋最大id」と「組み立てに効くレース列」で指紋を取れば安全に再利用できる。
  - race 行（heat_id / applied_* / status 等は後から変わる）は毎回読み直し、キャッシュしない。
"""

from collections import OrderedDict

from app.infrastructure.db.repositories.timing_repository import (
    TimingRaceRepository,
    TimingLayoutRepository,
)
from app.domain.rotation import LayoutElement
from app.domain.race_builder import PassEvent, build_race

# (db_path, race_id, fingerprint) -> RaceResult|None。プロセス内 LRU（店舗横断で最大512件）。
_RESULT_CACHE: "OrderedDict[tuple, object]" = OrderedDict()
_RESULT_CACHE_MAX = 512


def _cache_get(key):
    v = _RESULT_CACHE.get(key, _MISS)
    if v is not _MISS:
        _RESULT_CACHE.move_to_end(key)
    return v


def _cache_put(key, value):
    _RESULT_CACHE[key] = value
    _RESULT_CACHE.move_to_end(key)
    while len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
        _RESULT_CACHE.popitem(last=False)


_MISS = object()


def _db_identity(db) -> str:
    """接続が指すDBファイル（店舗ごとに別DB）。取れなければ空文字＝同一扱いにしない。"""
    try:
        from app.infrastructure.db.connection import current_db_path
        return current_db_path()
    except Exception:
        return ""


async def _events_fingerprint(db, race_id: int):
    async with db.execute(
        "SELECT COUNT(*) AS n, MAX(id) AS mx FROM timing_events WHERE race_id = ?",
        (race_id,),
    ) as cur:
        r = await cur.fetchone()
    return (int(r["n"] or 0), int(r["mx"] or 0)) if r else (0, 0)


async def build_race_result(db, race_id: int):
    """race_id の結果を組み立てて返す。

    戻り値: (race_row, RaceResult) または (race_row, None)（レイアウト未設定など）
    """
    rrepo = TimingRaceRepository(db)
    lrepo = TimingLayoutRepository(db)

    # ★性能：レース行＋layout_json＋イベント指紋を 1 クエリで取る（aiosqlite は1クエリ=1スレッド往復）。
    #   列が無い旧DBなどで失敗したら従来の3クエリ経路へフォールバック。
    import json as _json
    race = None; _lj = None; n_ev = mx_ev = 0
    try:
        async with db.execute(
            "SELECT r.*, "
            "(SELECT COUNT(*) FROM timing_events e WHERE e.race_id = r.id) AS _n_ev, "
            "(SELECT MAX(id)  FROM timing_events e WHERE e.race_id = r.id) AS _mx_ev "
            "FROM timing_races r WHERE r.id = ?",
            (race_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None, None
        race = row
        _lj = row["layout_json"] if "layout_json" in row.keys() else None
        n_ev, mx_ev = int(row["_n_ev"] or 0), int(row["_mx_ev"] or 0)
    except Exception:
        race = await rrepo.get_race(race_id)
        if race is None:
            return None, None
        _lj = await rrepo.get_layout_json(race_id)
        n_ev, mx_ev = await _events_fingerprint(db, race_id)

    # レイアウト要素 → LayoutElement 列
    #   受信/作成時に固定した layout_json があれば最優先で使う（レイアウトが後から
    #   編集されても、そのレースは受信時点の構成で解釈する）。無ければ従来どおり
    #   layout_id から現在のレイアウトを引く（旧レコード後方互換）。

    # ---- キャッシュ照合（組み立てに効く入力だけで指紋を作る）----
    key = (
        _db_identity(db), race_id,
        race["layout_id"], race["target_laps"], race["green_t_us"], race["heat_tag"],
        _lj or "", n_ev, mx_ev,
    )
    cached = _cache_get(key)
    if cached is not _MISS:
        return race, cached

    layout = None
    if _lj:
        try:
            layout = [
                LayoutElement(kind=e["kind"], node_id=e.get("node_id"))
                for e in _json.loads(_lj)
            ]
        except Exception:
            layout = None
    if layout is None:
        layout_id = race["layout_id"]
        if layout_id is None:
            _cache_put(key, None)
            return race, None
        elems = await lrepo.get_elements(layout_id)
        layout = [
            LayoutElement(kind=e["kind"], node_id=e["node_id"])
            for e in elems
        ]
        # ⚠ layout_json 無し（旧レコード）はレイアウト編集で結果が変わり得るため、
        #    要素の並びも指紋に含める（編集されれば自動的に別キー）。
        key = key + (tuple((e.kind, e.node_id) for e in layout),)
        cached = _cache_get(key)
        if cached is not _MISS:
            return race, cached
    if not any(e.kind == "SG" for e in layout):
        _cache_put(key, None)
        return race, None  # S/Gが無ければ組み立て不能

    # 通過イベント → PassEvent 列
    ev_rows = await rrepo.get_events(race_id)
    events = [
        PassEvent(
            node_id=r["src"],
            lane=r["lane"],
            t_us=r["t_us"],
            t_us_b=r["t_us_b"],
            quality=r["quality"],
            seq=r["seq"],
            id=(r["id"] if "id" in r.keys() else None),   # ★C11：timing_events.id（旧は常にNone＝to_event_id が死んでいた）
        )
        for r in ev_rows
    ]

    result = build_race(
        layout,
        events,
        target_laps=race["target_laps"],
        green_t_us=race["green_t_us"],
        heat_id=race["heat_tag"],
    )
    _cache_put(key, result)
    return race, result


def invalidate_race_result_cache(race_id: int | None = None) -> None:
    """明示的な無効化（レース削除・サンプル再生成など）。race_id=None で全消し。"""
    if race_id is None:
        _RESULT_CACHE.clear()
        return
    for k in [k for k in _RESULT_CACHE if len(k) > 1 and k[1] == race_id]:
        _RESULT_CACHE.pop(k, None)
