"""GW(TFT)に送る「アクティブレース」ポインタ（店舗DBに1つ・20261109）。

リスト形式（予選）/ ヒートトーナメント / ブラケットのどれか1つを指す。
admin が #（レース番号）やグループをクリックして設定・解除する。
GW は current_race_for_gw で、このポインタ先のレーン別レーサー名を取得する。

テーブルは遅延ensure（CREATE TABLE IF NOT EXISTS）。既存の店舗DBにも自動追加される。
"""
from __future__ import annotations
import datetime as _dt

# kind の値
KIND_HEAT = "heat"                 # 予選リスト（heats.id）
KIND_HT_GROUP = "ht_group"         # ヒートトーナメント（ht_groups.id）
KIND_BRACKET_GROUP = "bracket_group"  # ノックアウトブラケット（bracket_groups.id）
VALID_KINDS = {KIND_HEAT, KIND_HT_GROUP, KIND_BRACKET_GROUP}


async def ensure(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS gw_active_race ("
        " id INTEGER PRIMARY KEY CHECK (id = 1),"
        " kind TEXT,"
        " ref_id INTEGER,"
        " updated_at TEXT)"
    )


async def get(db) -> dict | None:
    """現在のアクティブ（{"kind":.., "ref_id":..}）。未設定は None。"""
    await ensure(db)
    async with db.execute("SELECT kind, ref_id FROM gw_active_race WHERE id = 1") as cur:
        row = await cur.fetchone()
    if not row or row["kind"] is None or row["ref_id"] is None:
        return None
    return {"kind": row["kind"], "ref_id": int(row["ref_id"])}


async def set_active(db, kind: str, ref_id: int) -> None:
    await ensure(db)
    now = _dt.datetime.now().isoformat(timespec="seconds")
    await db.execute(
        "INSERT INTO gw_active_race (id, kind, ref_id, updated_at) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET kind = excluded.kind, ref_id = excluded.ref_id, "
        "updated_at = excluded.updated_at",
        (kind, int(ref_id), now),
    )
    await db.commit()


async def clear(db) -> None:
    await ensure(db)
    await db.execute("DELETE FROM gw_active_race WHERE id = 1")
    await db.commit()


async def toggle(db, kind: str, ref_id: int) -> bool:
    """同じ対象を再クリック→解除（False）、別対象/未設定→その対象をアクティブ化（True）。"""
    cur = await get(db)
    if cur and cur["kind"] == kind and cur["ref_id"] == int(ref_id):
        await clear(db)
        return False
    await set_active(db, kind, ref_id)
    return True


# ── アクティブポインタ → レーン別レーサー名（GW送信／PIP共通）──────────────────
async def _lane_rows(db, kind: str, ref_id: int):
    """kind別に (lane_no, name, yomi) の行を lane 昇順で返す。"""
    if kind == KIND_HEAT:
        sql = ("SELECT hl.lane_no AS lane, r.name AS name, r.yomi AS yomi "
               "FROM heat_lanes hl LEFT JOIN entries e ON e.id = hl.entry_id "
               "LEFT JOIN racers r ON r.id = e.racer_id "
               "WHERE hl.heat_id = ? ORDER BY hl.lane_no")
    elif kind == KIND_HT_GROUP:
        sql = ("SELECT hs.slot_no AS lane, r.name AS name, r.yomi AS yomi "
               "FROM ht_slots hs LEFT JOIN entries e ON e.id = hs.entry_id "
               "LEFT JOIN racers r ON r.id = e.racer_id "
               "WHERE hs.group_id = ? ORDER BY hs.slot_no")
    elif kind == KIND_BRACKET_GROUP:
        sql = ("SELECT bs.slot_no AS lane, r.name AS name, r.yomi AS yomi "
               "FROM bracket_slots bs LEFT JOIN entries e ON e.id = bs.entry_id "
               "LEFT JOIN racers r ON r.id = e.racer_id "
               "WHERE bs.group_id = ? ORDER BY bs.slot_no")
    else:
        return []
    async with db.execute(sql, (int(ref_id),)) as cur:
        return await cur.fetchall()


async def _race_label(db, kind: str, ref_id: int) -> str:
    """表示用ラベル（PIP等）。失敗しても空文字で握りつぶす。"""
    try:
        if kind == KIND_HEAT:
            async with db.execute(
                "SELECT round_no, heat_no FROM heats WHERE id = ?", (int(ref_id),)) as cur:
                h = await cur.fetchone()
            if h:
                return f"予選{h['round_no']}回目 #{h['heat_no']}"
        elif kind == KIND_HT_GROUP:
            async with db.execute(
                "SELECT g.group_no, rd.round_type, rd.round_no "
                "FROM ht_groups g JOIN ht_rounds rd ON rd.id = g.round_id "
                "WHERE g.id = ?", (int(ref_id),)) as cur:
                g = await cur.fetchone()
            if g:
                return f"{g['round_type'] or ''} グループ{g['group_no']}".strip()
        elif kind == KIND_BRACKET_GROUP:
            async with db.execute(
                "SELECT g.group_no, rd.round_type, rd.round_no "
                "FROM bracket_groups g JOIN bracket_rounds rd ON rd.id = g.round_id "
                "WHERE g.id = ?", (int(ref_id),)) as cur:
                g = await cur.fetchone()
            if g:
                return f"{g['round_type'] or ''} グループ{g['group_no']}".strip()
    except Exception:
        pass
    return ""


# ── 自動アクティブ移動（結果確定時・20261109）────────────────────────────────
#  確定したレース番号Nを基準に、同ラウンド内でNの次の未確定→無ければ未確定最小へ巡回。
#  ラウンド全確定→次ラウンド先頭／全て確定→None（待機）。手動クリックはこれとは別に優先。
async def _heat_next_active(db, heat_id: int):
    async with db.execute(
        "SELECT tournament_id, round_no, heat_no FROM heats WHERE id = ?", (int(heat_id),)) as c:
        h = await c.fetchone()
    if not h:
        return None
    tid, rno, hno = h["tournament_id"], h["round_no"], h["heat_no"]
    async with db.execute(
        "SELECT id, heat_no, status FROM heats WHERE tournament_id = ? AND round_no = ? "
        "ORDER BY heat_no, id", (tid, rno)) as c:
        rows = await c.fetchall()
    unconf = [r for r in rows if (r["status"] or "") != "done"]
    after = [r for r in unconf if r["heat_no"] is not None and hno is not None and r["heat_no"] > hno]
    if after:
        return (KIND_HEAT, after[0]["id"])
    if unconf:
        return (KIND_HEAT, unconf[0]["id"])
    async with db.execute(
        "SELECT id FROM heats WHERE tournament_id = ? AND round_no > ? "
        "ORDER BY round_no, heat_no, id LIMIT 1", (tid, rno)) as c:
        nxt = await c.fetchone()
    return (KIND_HEAT, nxt["id"]) if nxt else None


# ラウンド種別の優先度（小さいほど先に走る）。
#  アクティブ移動の優先度：ラウンドが低い通常ラウンド(グループ番号順) ＞ 敗者復活戦 ＞ ３位決定戦 ＞ 決勝。
#  round_type の表記ゆれを吸収（normal/敗者復活=losers,revival/3位=third/決勝=final）。
_RTYPE_PRI = {
    "normal": 0, "": 0, None: 0,
    "losers": 1, "revival": 1, "敗者復活": 1, "敗者復活戦": 1, "裏": 1,
    "third": 2, "3位": 2, "３位": 2, "3位決定": 2, "3位決定戦": 2,
    "final": 3, "決勝": 3,
}


def _rtype_pri(rt) -> int:
    return _RTYPE_PRI.get(rt, 0)


async def _group_next_active(db, kind, group_id, groups_tbl, rounds_tbl, slots_tbl, ranks_tbl):
    """ht_group / bracket_group 共通：トーナメント全体を優先度順に並べて巡回。
       優先度＝(ラウンド種別, round_no, group_no)。種別は通常→敗者復活→3位決定→決勝。
       確定したグループの次の未確定へ、末尾なら先頭の未確定へ巡回。全確定→None（待機）。
       確定判定＝そのグループに結果（ranks_tbl）が1件でもあるか。"""
    # 確定グループの所属トーナメント
    async with db.execute(
        f"SELECT rd.tournament_id FROM {groups_tbl} g JOIN {rounds_tbl} rd ON rd.id = g.round_id "
        f"WHERE g.id = ?", (int(group_id),)) as c:
        g0 = await c.fetchone()
    if not g0:
        return None
    tid = g0["tournament_id"]
    # 同トーナメントの全グループ＋種別/ラウンド/確定状況
    async with db.execute(
        f"SELECT gg.id AS id, gg.group_no AS group_no, rd.round_no AS round_no, "
        f"       rd.round_type AS round_type, "
        f"       (SELECT COUNT(*) FROM {ranks_tbl} rr WHERE rr.group_id = gg.id) AS ranked "
        f"FROM {groups_tbl} gg JOIN {rounds_tbl} rd ON rd.id = gg.round_id "
        f"WHERE rd.tournament_id = ?", (tid,)) as c:
        rows = await c.fetchall()
    if not rows:
        return None

    def _key(r):
        return (_rtype_pri(r["round_type"]), r["round_no"] or 0, r["group_no"] or 0, r["id"])

    ordered = sorted(rows, key=_key)
    unconf = [r for r in ordered if not r["ranked"]]
    if not unconf:
        return None
    gid = int(group_id)
    idx = next((i for i, r in enumerate(ordered) if r["id"] == gid), None)
    if idx is not None:
        after = [r for r in ordered[idx + 1:] if not r["ranked"]]
        if after:
            return (kind, after[0]["id"])
    # 末尾（または確定グループが見つからない）→先頭の未確定へ巡回
    return (kind, unconf[0]["id"])


async def advance_after_confirm(db, kind: str, ref_id: int) -> None:
    """確定したレース(kind,ref_id)を基準に、アクティブを次へ自動移動（無ければ解除）。
       結果書き込み＆status更新のコミット後に呼ぶこと。例外は握りつぶす（本処理を妨げない）。"""
    try:
        await ensure(db)
        if kind == KIND_HEAT:
            nxt = await _heat_next_active(db, ref_id)
        elif kind == KIND_HT_GROUP:
            nxt = await _group_next_active(db, KIND_HT_GROUP, ref_id,
                                           "ht_groups", "ht_rounds", "ht_slots", "ht_slot_ranks")
        elif kind == KIND_BRACKET_GROUP:
            nxt = await _group_next_active(db, KIND_BRACKET_GROUP, ref_id,
                                           "bracket_groups", "bracket_rounds", "bracket_slots", "bracket_slot_ranks")
        else:
            return
        if nxt:
            await set_active(db, nxt[0], nxt[1])
        else:
            await clear(db)
    except Exception:
        pass


async def upcoming_order(db) -> dict:
    """アクティブレースを起点に、未確定レースを「実際に走る順（＝GW自動遷移と同一の
       巡回順）」で並べた配列を返す。スマホ観覧の「次のレースまでNレース」用。
       {"active": bool, "kind":.., "ref_id":.., "order":[{"names":[...]}, ...]}
       order[i] の i ＝「そのレースに出るレーサーの 次のレースまで = i レース」。
       i=0 がアクティブ（＝あなたの番）。アクティブより前にスキップされた未確定レースは
       巡回で末尾に回る（ids[idx:]+ids[:idx]）。アクティブ未設定→active:False, order:[]。
       例外時も active:False を返し、観覧側は従来のDOM文書順にフォールバックする。"""
    try:
        await ensure(db)
        cur = await get(db)
        if not cur:
            return {"active": False, "kind": None, "ref_id": None, "order": []}
        kind, ref_id = cur["kind"], cur["ref_id"]

        if kind == KIND_HEAT:
            async with db.execute(
                "SELECT tournament_id FROM heats WHERE id = ?", (ref_id,)) as c:
                h0 = await c.fetchone()
            if not h0:
                return {"active": False, "kind": kind, "ref_id": ref_id, "order": []}
            async with db.execute(
                "SELECT id, round_no, heat_no, status FROM heats WHERE tournament_id = ?",
                (h0["tournament_id"],)) as c:
                rows = await c.fetchall()
            items = [r for r in rows if (r["status"] or "") != "done"]
            items.sort(key=lambda r: (r["round_no"] or 0, r["heat_no"] or 0, r["id"]))
            ids = [r["id"] for r in items]
        elif kind in (KIND_HT_GROUP, KIND_BRACKET_GROUP):
            if kind == KIND_HT_GROUP:
                groups_tbl, rounds_tbl, ranks_tbl = "ht_groups", "ht_rounds", "ht_slot_ranks"
            else:
                groups_tbl, rounds_tbl, ranks_tbl = "bracket_groups", "bracket_rounds", "bracket_slot_ranks"
            async with db.execute(
                f"SELECT rd.tournament_id AS tid FROM {groups_tbl} g "
                f"JOIN {rounds_tbl} rd ON rd.id = g.round_id WHERE g.id = ?", (ref_id,)) as c:
                g0 = await c.fetchone()
            if not g0:
                return {"active": False, "kind": kind, "ref_id": ref_id, "order": []}
            async with db.execute(
                f"SELECT gg.id AS id, gg.group_no AS group_no, rd.round_no AS round_no, "
                f"       rd.round_type AS round_type, "
                f"       (SELECT COUNT(*) FROM {ranks_tbl} rr WHERE rr.group_id = gg.id) AS ranked "
                f"FROM {groups_tbl} gg JOIN {rounds_tbl} rd ON rd.id = gg.round_id "
                f"WHERE rd.tournament_id = ?", (g0["tid"],)) as c:
                rows = await c.fetchall()
            items = [r for r in rows if not r["ranked"]]
            items.sort(key=lambda r: (_rtype_pri(r["round_type"]), r["round_no"] or 0,
                                      r["group_no"] or 0, r["id"]))
            ids = [r["id"] for r in items]
        else:
            return {"active": False, "kind": kind, "ref_id": ref_id, "order": []}

        # アクティブが未確定集合に無い（既に確定済み等）→ 起点にできない＝フォールバック
        if ref_id not in ids:
            return {"active": False, "kind": kind, "ref_id": ref_id, "order": []}
        idx = ids.index(ref_id)
        rot = ids[idx:] + ids[:idx]   # アクティブ先頭、スキップ済み（前方の未確定）は末尾へ巡回
        order = []
        for rid in rot:
            lane_rows = await _lane_rows(db, kind, rid)
            names = [str(r["name"]).strip() for r in lane_rows if r["name"]]
            # レーサー未割当（空き）の未確定グループは「まだ走るレースではない」ので数に含めない。
            # ただしアクティブ自身は常に先頭(=あなたの番)として維持する。
            if not names and rid != ref_id:
                continue
            order.append({"names": names})
        return {"active": True, "kind": kind, "ref_id": ref_id, "order": order}
    except Exception:
        return {"active": False, "kind": None, "ref_id": None, "order": []}


async def resolve_lanes(db) -> dict:
    """現在アクティブなレースのレーン別名前を返す（GW／PIP共通）。
       {"active": bool, "kind":.., "ref_id":.., "race_label":.., "lanes":[{"lane","kana"}]}
       アクティブ無し→ active=False, lanes=[]。"""
    from app.domain.kana import gw_display_name
    cur = await get(db)
    if not cur:
        return {"active": False, "kind": None, "ref_id": None, "race_label": "", "lanes": []}
    rows = await _lane_rows(db, cur["kind"], cur["ref_id"])
    lanes = []
    for r in rows:
        ln = r["lane"]
        if ln is None or ln < 1 or ln > 5:
            continue
        lanes.append({"lane": int(ln), "kana": gw_display_name(r["name"], r["yomi"])})
    label = await _race_label(db, cur["kind"], cur["ref_id"])
    return {"active": True, "kind": cur["kind"], "ref_id": cur["ref_id"],
            "race_label": label, "lanes": lanes}
