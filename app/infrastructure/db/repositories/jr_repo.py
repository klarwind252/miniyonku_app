"""ジュニアレーサー判定用の一覧（★20261117）。

予選順位表・決勝進出レーサーで名前の後に「Jr.」を付けるために使う。
順位や決勝進出の行は形式ごとに作り方が違い entry_id を持たない行もあるので、
entry_id と レーサー名 の両方の集合を返す（テンプレートでどちらかに一致すれば Jr.）。
ジュニア区分は racers.is_child。
"""


async def jr_sets(db, tid: int):
    eids, names = set(), set()
    try:
        async with db.execute(
            "SELECT e.id AS eid, r.name FROM entries e JOIN racers r ON r.id=e.racer_id "
            "WHERE e.tournament_id=? AND COALESCE(r.is_child,0)=1", (tid,)) as cur:
            for row in await cur.fetchall():
                eids.add(row["eid"])
                names.add(row["name"])
    except Exception:
        pass
    return eids, names


def short_base(name: str) -> str:
    """（ または ( より前の名前（前後空白除去）。"""
    n = str(name or "")
    for sep in ("（", "("):
        n = n.split(sep)[0]
    return n.strip()


async def short_name_map(db, tid: int) -> dict:
    """★20261117 予選・決勝画面の表示用：正式名→省略名（（地域）を省略）の対応表。
    そのレースの全エントリーで「（ より前の名前」が同じ人が他にいなければ省略、2人以上なら省略しない。
    省略しない人・省略しても変わらない人は対応表に入れない。"""
    names = []
    try:
        async with db.execute(
            "SELECT DISTINCT r.name FROM entries e JOIN racers r ON r.id=e.racer_id "
            "WHERE e.tournament_id=?", (tid,)) as cur:
            names = [row["name"] for row in await cur.fetchall() if row["name"]]
    except Exception:
        return {}
    cnt = {}
    for n in names:
        b = short_base(n)
        cnt[b] = cnt.get(b, 0) + 1
    out = {}
    for n in names:
        b = short_base(n)
        if b and b != n and cnt.get(b, 0) == 1:
            out[n] = b
    return out
