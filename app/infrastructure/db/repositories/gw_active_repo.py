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


# ── 自動反映の「上書き確認」保留（20261110）─────────────────────────────────
#  自動反映先に既に結果がある場合、上書きせずこのテーブルへ保留し、admin画面の
#  PIPがポーリングしてダイアログを出す（上書き→force反映／キャンセル→dismiss）。
#  dismissed=1 は「同じ計測レースは再通知しない」フラグ（キャンセル後の再ナグ防止）。
async def ensure_pending(db) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS gw_auto_pending ("
        " id INTEGER PRIMARY KEY CHECK (id = 1),"
        " kind TEXT, ref_id INTEGER, race_id INTEGER,"
        " apply_path TEXT, label TEXT,"
        " dismissed INTEGER DEFAULT 0, updated_at TEXT)"
    )


async def set_auto_pending(db, kind, ref_id, race_id, apply_path, label) -> None:
    await ensure_pending(db)
    # 同じ計測レースを一度キャンセル済みなら再通知しない。
    async with db.execute(
        "SELECT race_id, dismissed FROM gw_auto_pending WHERE id = 1") as cur:
        row = await cur.fetchone()
    if row and row["race_id"] == int(race_id) and (row["dismissed"] or 0) == 1:
        return
    now = _dt.datetime.now().isoformat(timespec="seconds")
    await db.execute(
        "INSERT INTO gw_auto_pending (id, kind, ref_id, race_id, apply_path, label, dismissed, updated_at) "
        "VALUES (1, ?, ?, ?, ?, ?, 0, ?) "
        "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, ref_id=excluded.ref_id, "
        "race_id=excluded.race_id, apply_path=excluded.apply_path, label=excluded.label, "
        "dismissed=0, updated_at=excluded.updated_at",
        (kind, int(ref_id), int(race_id), apply_path, label or "", now),
    )
    await db.commit()


async def get_auto_pending(db) -> dict | None:
    """未応答の保留（dismissed=0）を返す。無ければ None。"""
    await ensure_pending(db)
    async with db.execute(
        "SELECT kind, ref_id, race_id, apply_path, label FROM gw_auto_pending "
        "WHERE id = 1 AND dismissed = 0") as cur:
        row = await cur.fetchone()
    if not row or not row["kind"]:
        return None
    return {"kind": row["kind"], "ref_id": row["ref_id"], "race_id": row["race_id"],
            "apply_path": row["apply_path"], "label": row["label"] or ""}


async def dismiss_auto_pending(db) -> None:
    """ダイアログをキャンセル／処理済み＝通知を閉じる（race_idは残し再ナグ防止）。"""
    await ensure_pending(db)
    await db.execute("UPDATE gw_auto_pending SET dismissed = 1 WHERE id = 1")
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
    """予選（heatsテーブル）系の自動アクティブ遷移。絶対ルール＝同一ラウンド内が全確定するまで
       次へ進まない。予選タイプで「ラウンド」の単位が異なるため分岐する（20261110 確定ルール）：
         - heat_roundrobin（ヒート総当たり）：ヒート(heat_no)単位で完結。
             ヒート内は (round_no, group_no) 順に巡回→全確定で次ヒート。
         - order_winner（勝ち抜け）：段階制のため自動アクティブ対象外（手動のみ）→ None扱いだが
             呼び出し側(advance_after_confirm)で解除せず据え置く。
         - その他（point/order/roundrobin/none_roundrobin 等）：round_no単位。
             同じround_no内をheat_no順に巡回→全確定で次のround_no→全確定で待機。
       確定＝status='done' または heat_results あり（反映/手入力で結果が入った）。"""
    async with db.execute(
        "SELECT h.tournament_id AS tid, t.qualifying_type AS qtype "
        "FROM heats h JOIN tournaments t ON t.id = h.tournament_id WHERE h.id = ?",
        (int(heat_id),)) as c:
        h = await c.fetchone()
    if not h:
        return None
    tid = h["tid"]
    qtype = h["qtype"] or ""
    if qtype == "order_winner":
        return None   # 勝ち抜けは手動のみ（据え置きは呼び出し側で担保）

    async with db.execute(
        "SELECT h.id AS id, h.heat_no AS heat_no, h.round_no AS round_no, h.group_no AS group_no, "
        "       h.status AS status, "
        "  (SELECT COUNT(*) FROM heat_results hr JOIN heat_lanes hl ON hl.id = hr.heat_lane_id "
        "   WHERE hl.heat_id = h.id) AS nres "
        "FROM heats h WHERE h.tournament_id = ?", (tid,)) as c:
        rows = await c.fetchall()

    def _done(r):
        return (r["status"] or "") == "done" or (r["nres"] or 0) > 0

    unconf = [r for r in rows if not _done(r)]
    if not unconf:
        return None
    cur = next((r for r in rows if r["id"] == int(heat_id)), None)

    if qtype in ("none_roundrobin", "roundrobin"):
        # ★即決勝総当たり/総当たり：巡（画面表示と同一）＝ heat_no 昇順で per(=有効人数//2)件ごとの
        #   チャンク。巡内を heat_no 順に巡回→全確定で次の巡。スキップは巡内の未消化#へ戻る。
        #   （データ上 round_no は 0 固定で、巡は位置から算出する）
        async with db.execute(
            "SELECT COUNT(*) AS n FROM entries WHERE tournament_id = ? AND status='active'",
            (tid,)) as c:
            _n = (await c.fetchone())["n"]
        per = max(1, (_n or 0) // 2)
        ordered_all = sorted(rows, key=lambda r: (r["heat_no"] or 0, r["id"]))
        posmap = {r["id"]: i for i, r in enumerate(ordered_all)}
        ordered = sorted(unconf, key=lambda r: posmap.get(r["id"], 0))
        if cur is not None and cur["id"] in posmap:
            cj = posmap[cur["id"]] // per
            within = [r for r in ordered if posmap[r["id"]] // per == cj]
            if within:
                after = [r for r in within if posmap[r["id"]] > posmap[cur["id"]]]
                return (KIND_HEAT, (after[0] if after else within[0])["id"])
        return (KIND_HEAT, ordered[0]["id"])

    if qtype == "heat_roundrobin":
        # ★ヒートごとに完結：ヒート(heat_no)が「ラウンド」。内側は (round_no=巡, group_no) 順。
        ordered = sorted(unconf, key=lambda r: (r["heat_no"] or 0, r["round_no"] or 0,
                                                r["group_no"] or 0, r["id"]))
        if cur is not None:
            within = [r for r in ordered if (r["heat_no"] or 0) == (cur["heat_no"] or 0)]
            if within:
                ck = (cur["round_no"] or 0, cur["group_no"] or 0, cur["id"])
                after = [r for r in within
                         if (r["round_no"] or 0, r["group_no"] or 0, r["id"]) > ck]
                return (KIND_HEAT, (after[0] if after else within[0])["id"])
        return (KIND_HEAT, ordered[0]["id"])

    # その他：round_no が「ラウンド」。内側は heat_no 順。
    ordered = sorted(unconf, key=lambda r: (r["round_no"] or 0, r["heat_no"] or 0, r["id"]))
    if cur is not None:
        within = [r for r in ordered if (r["round_no"] or 0) == (cur["round_no"] or 0)]
        if within:
            ck = (cur["heat_no"] or 0, cur["id"])
            after = [r for r in within if (r["heat_no"] or 0, r["id"]) > ck]
            return (KIND_HEAT, (after[0] if after else within[0])["id"])
    return (KIND_HEAT, ordered[0]["id"])


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


async def _group_next_active(db, kind, group_id, groups_tbl, rounds_tbl, slots_tbl, ranks_tbl,
                             results_tbl=None, heat_col=None, section_col=None):
    """ht_group / bracket_group 共通：トーナメント全体を優先度順に並べて巡回。
       絶対ルール：同一ラウンド（＝同一 round_id）内がすべて確定するまで次ラウンドへ進まない。
       順序キー＝(heat_no, ラウンド種別, round_no, group_no)。
         - heat_col 指定時（ヒートトーナメント）は heat_no を最優先＝各ヒート内で完結してから次ヒート。
         - bracket は heat_no 無し（0固定）＝従来どおりラウンド順。
       種別は通常→敗者復活→3位決定→決勝。全確定→None（待機）。
       確定判定＝そのグループに ranks_tbl もしくは results_tbl の行が1件でもあるか。
       （★通常ラウンドの手動確定は bracket_results＝勝者だけ書き bracket_slot_ranks は書かないため、
         results_tbl も見ないと「確定済みなのに未確定」と誤判定し、確定済み組へ巡回してしまう。）"""
    # 確定グループの所属トーナメント
    async with db.execute(
        f"SELECT rd.tournament_id FROM {groups_tbl} g JOIN {rounds_tbl} rd ON rd.id = g.round_id "
        f"WHERE g.id = ?", (int(group_id),)) as c:
        g0 = await c.fetchone()
    if not g0:
        return None
    tid = g0["tournament_id"]
    # 確定状況＝ranks_tbl または results_tbl に行があるか
    _res_sql = (f" + (SELECT COUNT(*) FROM {results_tbl} rs WHERE rs.group_id = gg.id)"
                if results_tbl else "")
    _heat_sql = f"rd.{heat_col}" if heat_col else "0"
    _sec_sql = f"COALESCE(rd.{section_col},0)" if section_col else "0"
    # 同トーナメントの全グループ＋ヒート/セクション/種別/ラウンド/確定状況（round_id＝ラウンド単位）
    async with db.execute(
        f"SELECT gg.id AS id, gg.group_no AS group_no, gg.round_id AS round_id, "
        f"       rd.round_no AS round_no, rd.round_type AS round_type, "
        f"       {_heat_sql} AS heat_no, {_sec_sql} AS section_no, "
        f"       ((SELECT COUNT(*) FROM {ranks_tbl} rr WHERE rr.group_id = gg.id){_res_sql}) AS ranked "
        f"FROM {groups_tbl} gg JOIN {rounds_tbl} rd ON rd.id = gg.round_id "
        f"WHERE rd.tournament_id = ?", (tid,)) as c:
        rows = await c.fetchall()
    if not rows:
        return None

    # 並び順：ヒート → (総合決勝=section0は最後) → ラウンド番号 → 種別 → セクション(A→B) → 組番号。
    #   ラウンドを section より先に置くので「各ラウンドを グループA→グループB の順に消化してから
    #   次のラウンドへ」。ラウンド1(A全組→B全組)→ラウンド2(A→B)→…→決勝(A→B)→ヒート総合決勝。
    #   総合決勝(section_no=0)だけはヒート内の最後に回す。
    def _key(r):
        sec = r["section_no"] or 0
        return (r["heat_no"] or 0, 1 if sec == 0 else 0,
                r["round_no"] or 0, _rtype_pri(r["round_type"]),
                sec, r["group_no"] or 0, r["id"])

    ordered = sorted(rows, key=_key)
    unconf = [r for r in ordered if not r["ranked"]]
    if not unconf:
        return None
    gid = int(group_id)
    cur = next((r for r in ordered if r["id"] == gid), None)
    if cur is not None:
        # ★絶対ルール：同一ラウンド（同一 round_id）内がすべて確定するまで次へ進まない。
        #   同ラウンド内に未確定が残っていれば、その中だけで巡回（group_no昇順で次、末尾なら先頭へ）。
        #   htは round_id にヒート/セクションが織り込まれるため、ヒートをまたがない。
        within = [r for r in unconf if r["round_id"] == cur["round_id"]]
        if within:
            after = [r for r in within if (r["group_no"] or 0) > (cur["group_no"] or 0)]
            return (kind, (after[0] if after else within[0])["id"])
    # 現ラウンド全確定（または確定グループ不明）→ 順序キーで最初の未確定へ。
    #   ht：同ヒートの次ラウンド→無ければ次ヒート。bracket：種別優先＋round_no順で次ラウンド。
    return (kind, unconf[0]["id"])


async def advance_after_confirm(db, kind: str, ref_id: int) -> None:
    """確定したレース(kind,ref_id)を基準に、アクティブを次へ自動移動（無ければ解除）。
       結果書き込み＆status更新のコミット後に呼ぶこと。例外は握りつぶす（本処理を妨げない）。"""
    try:
        await ensure(db)
        if kind == KIND_HEAT:
            # 勝ち抜け(order_winner)は手動のみ＝自動アクティブを動かさない（解除もしない）。
            async with db.execute(
                "SELECT t.qualifying_type AS qt FROM heats h "
                "JOIN tournaments t ON t.id = h.tournament_id WHERE h.id = ?", (int(ref_id),)) as c:
                _hr = await c.fetchone()
            if _hr and (_hr["qt"] or "") == "order_winner":
                return
            nxt = await _heat_next_active(db, ref_id)
        elif kind == KIND_HT_GROUP:
            nxt = await _group_next_active(db, KIND_HT_GROUP, ref_id,
                                           "ht_groups", "ht_rounds", "ht_slots", "ht_slot_ranks",
                                           results_tbl="ht_results", heat_col="heat_no",
                                           section_col="section_no")
        elif kind == KIND_BRACKET_GROUP:
            nxt = await _group_next_active(db, KIND_BRACKET_GROUP, ref_id,
                                           "bracket_groups", "bracket_rounds", "bracket_slots", "bracket_slot_ranks",
                                           results_tbl="bracket_results")
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
                results_tbl = "ht_results"
                heat_sql = "rd.heat_no"   # ヒートトーナメントはヒート単位で完結
                section_sql = "COALESCE(rd.section_no,0)"   # グループA/B単位
            else:
                groups_tbl, rounds_tbl, ranks_tbl = "bracket_groups", "bracket_rounds", "bracket_slot_ranks"
                results_tbl = "bracket_results"
                heat_sql = "0"            # bracket は heat_no 無し
                section_sql = "0"         # bracket はセクション無し
            async with db.execute(
                f"SELECT rd.tournament_id AS tid FROM {groups_tbl} g "
                f"JOIN {rounds_tbl} rd ON rd.id = g.round_id WHERE g.id = ?", (ref_id,)) as c:
                g0 = await c.fetchone()
            if not g0:
                return {"active": False, "kind": kind, "ref_id": ref_id, "order": []}
            # 確定判定＝ranks または results に行があるか（手動確定の通常ラウンドは results のみ）
            async with db.execute(
                f"SELECT gg.id AS id, gg.group_no AS group_no, rd.round_no AS round_no, "
                f"       rd.round_type AS round_type, {heat_sql} AS heat_no, {section_sql} AS section_no, "
                f"       ((SELECT COUNT(*) FROM {ranks_tbl} rr WHERE rr.group_id = gg.id) "
                f"        + (SELECT COUNT(*) FROM {results_tbl} rs WHERE rs.group_id = gg.id)) AS ranked "
                f"FROM {groups_tbl} gg JOIN {rounds_tbl} rd ON rd.id = gg.round_id "
                f"WHERE rd.tournament_id = ?", (g0["tid"],)) as c:
                rows = await c.fetchall()
            items = [r for r in rows if not r["ranked"]]
            # 並び順は _group_next_active と完全一致：
            #   ヒート → (総合決勝=section0は最後) → ラウンド番号 → 種別 → セクション(A→B) → 組番号。
            #   ラウンドを section より先に置くので「各ラウンドを グループA→B 消化→次ラウンド」。
            #   これで admin のアクティブ自動遷移とスマホの「次のレースまで」が一致する。
            def _ord_key(r):
                sec = r["section_no"] or 0
                return (r["heat_no"] or 0, 1 if sec == 0 else 0,
                        r["round_no"] or 0, _rtype_pri(r["round_type"]),
                        sec, r["group_no"] or 0, r["id"])
            items.sort(key=_ord_key)
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
