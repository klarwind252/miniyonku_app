"""ブラケットスロットの不整合を起動時に自動修正する（旧 main.py から移設・無変更）。

- シードスロットが勝者で上書きされた場合の復元
- 勝者が正しいスロットに配置されていない場合の修正
"""
import aiosqlite

from app.infrastructure.db.connection import DB_PATH


def _spread_indices(n_items, n_slots):
    if n_items <= 0 or n_slots <= 0: return []
    if n_items >= n_slots: return [i % n_slots for i in range(n_items)]
    if n_items == 1: return [0]
    return [round(i * (n_slots - 1) / (n_items - 1)) for i in range(n_items)]


async def fix_bracket_slots_on_startup(db_path: str = None):
    if db_path is None:
        db_path = DB_PATH

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT id, bracket_mode FROM tournaments") as cur:
            tournaments = await cur.fetchall()

        for t in tournaments:
            tid = t["id"]
            b_mode = t["bracket_mode"] or "third_place"
            if b_mode == "revival":
                continue

            async with db.execute(
                "SELECT id, round_no, round_type FROM bracket_rounds WHERE tournament_id=? ORDER BY round_no",
                (tid,),
            ) as cur:
                rounds = await cur.fetchall()

            for i in range(1, len(rounds)):
                prev_rnd = rounds[i - 1]
                curr_rnd = rounds[i]
                if curr_rnd["round_type"] == "revival":
                    continue

                # 前ラウンドの勝者
                async with db.execute("""
                    SELECT bg.group_no, bs.entry_id FROM bracket_results bres
                    JOIN bracket_groups bg ON bg.id=bres.group_id
                    JOIN bracket_slots bs ON bs.id=bres.winner_slot_id
                    WHERE bg.round_id=? ORDER BY bg.group_no
                """, (prev_rnd["id"],)) as cur:
                    winners = await cur.fetchall()
                if not winners:
                    continue

                # 前ラウンド出場entry_id
                async with db.execute(
                    "SELECT DISTINCT bs.entry_id FROM bracket_slots bs "
                    "JOIN bracket_groups bg ON bg.id=bs.group_id "
                    "WHERE bg.round_id=? AND bs.entry_id IS NOT NULL",
                    (prev_rnd["id"],),
                ) as cur:
                    prev_eids = {r["entry_id"] for r in await cur.fetchall()}

                async with db.execute(
                    "SELECT entry_id FROM ht_finalist_seeds WHERE seeded=1"
                ) as cur:
                    ht_seed_eids = {r["entry_id"] for r in await cur.fetchall()}

                async with db.execute(
                    "SELECT id, group_no FROM bracket_groups WHERE round_id=? ORDER BY group_no",
                    (curr_rnd["id"],),
                ) as cur:
                    curr_groups = await cur.fetchall()

                async with db.execute("""
                    SELECT bs.id as slot_id, bg.group_no, bs.slot_no, bs.entry_id,
                           COALESCE(bs.seed_reserved, 0) as seed_reserved
                    FROM bracket_slots bs JOIN bracket_groups bg ON bg.id=bs.group_id
                    WHERE bg.round_id=? ORDER BY bg.group_no, bs.slot_no
                """, (curr_rnd["id"],)) as cur:
                    curr_slots = [dict(r) for r in await cur.fetchall()]

                # 各グループの最後のスロット（シード配置先）
                group_max_slot = {}
                for s in curr_slots:
                    gno = s["group_no"]
                    if gno not in group_max_slot or s["slot_no"] > group_max_slot[gno]["slot_no"]:
                        group_max_slot[gno] = dict(s)

                # シード選手（seeded=1/2 で前ラウンド未出場）
                async with db.execute("""
                    SELECT e.id, e.entry_order FROM entries e
                    WHERE e.tournament_id=? AND e.status='active' AND e.seeded IN (1, 2)
                    ORDER BY e.entry_order
                """, (tid,)) as cur:
                    seeded_entries = await cur.fetchall()

                seeds_not_in_prev = [s for s in seeded_entries if s["id"] not in prev_eids]
                seeded_in_curr = {
                    s["entry_id"] for s in curr_slots
                    if s["entry_id"] and (s["entry_id"] in ht_seed_eids
                                          or s.get("seed_reserved")
                                          or s["entry_id"] not in prev_eids)
                }
                missing_seeds = [s for s in seeds_not_in_prev if s["id"] not in seeded_in_curr]

                changed = False

                # シードを復元
                if missing_seeds:
                    n_seeds = len(seeds_not_in_prev)
                    n_groups = len(curr_groups)
                    seed_indices = _spread_indices(n_seeds, n_groups)
                    for si, seed in enumerate(seeds_not_in_prev):
                        if seed["id"] in seeded_in_curr:
                            continue
                        target_gno = curr_groups[seed_indices[si]]["group_no"]
                        target = group_max_slot.get(target_gno)
                        if target:
                            await db.execute(
                                "UPDATE bracket_slots SET entry_id=? WHERE id=?",
                                (seed["id"], target["slot_id"]),
                            )
                            changed = True

                    # スロット再取得
                    async with db.execute("""
                        SELECT bs.id as slot_id, bg.group_no, bs.slot_no, bs.entry_id,
                               COALESCE(bs.seed_reserved, 0) as seed_reserved
                        FROM bracket_slots bs JOIN bracket_groups bg ON bg.id=bs.group_id
                        WHERE bg.round_id=? ORDER BY bg.group_no, bs.slot_no
                    """, (curr_rnd["id"],)) as cur:
                        curr_slots = [dict(r) for r in await cur.fetchall()]

                # 非シードスロットへの勝者配置を確認・修正
                non_seed_slots = [
                    s for s in curr_slots
                    if not (s.get("seed_reserved") or
                            (s["entry_id"] and s["entry_id"] in ht_seed_eids))
                ]

                needs_fix = any(
                    gi < len(non_seed_slots) and non_seed_slots[gi]["entry_id"] != winners[gi]["entry_id"]
                    for gi in range(len(winners))
                )

                if needs_fix:
                    placed: set = set()
                    for gi, w in enumerate(winners):
                        if gi >= len(non_seed_slots):
                            break
                        target = non_seed_slots[gi]
                        if w["entry_id"] not in placed:
                            await db.execute(
                                "UPDATE bracket_slots SET entry_id=? WHERE id=?",
                                (w["entry_id"], target["slot_id"]),
                            )
                            placed.add(w["entry_id"])
                    # 余剰スロットをNULLに
                    for gi in range(len(winners), len(non_seed_slots)):
                        await db.execute(
                            "UPDATE bracket_slots SET entry_id=NULL WHERE id=?",
                            (non_seed_slots[gi]["slot_id"],),
                        )
                    # このラウンドの結果をクリア
                    for grp in curr_groups:
                        await db.execute("DELETE FROM bracket_results WHERE group_id=?", (grp["id"],))
                        await db.execute("DELETE FROM bracket_slot_ranks WHERE group_id=?", (grp["id"],))
                    changed = True

                if changed:
                    await db.commit()