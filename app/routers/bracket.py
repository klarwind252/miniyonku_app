"""
決勝トーナメント管理ルーター
- 2と3の組み合わせパターン提示
- ラウンド生成・シード配置
- 結果入力（通常:○×、決勝:1-2-3位）
- 3位決定戦（決勝参加4名以上）
"""
from fastapi import APIRouter, Request, Depends, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import aiosqlite
import os
import math
from itertools import product

from app.models.database import get_db

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))
from app.config import inject_globals, HEAT_TOURNAMENT_TYPES
inject_globals(templates)


# ── 2と3の組み合わせ生成 ──────────────────────────────────
def _spread_indices(n_items: int, n_slots: int) -> list[int]:
    """
    n_items 個を n_slots のスロットに均等間隔で配置するインデックスリスト。
    各 item が互いに最大間隔になるよう配置する。
    例:
      1個, 2スロット → [0]
      2個, 3スロット → [0, 2]  （最大間隔）
      2個, 4スロット → [0, 3]
      3個, 4スロット → [0, 1, 3]
      2個, 2スロット → [0, 1]
    """
    if n_items <= 0 or n_slots <= 0:
        return []
    if n_items >= n_slots:
        # グループ数を超える場合はラップアラウンド（同グループに複数シードも許容）
        return [i % n_slots for i in range(n_items)]
    if n_items == 1:
        return [0]
    # 0 から n_slots-1 の範囲に n_items 個を等間隔配置
    return [round(i * (n_slots - 1) / (n_items - 1)) for i in range(n_items)]


def _block_of(flat_index: int, next_sizes: list[int]) -> int:
    """
    順次ブロック集約トポロジにおける「次ラウンドのグループ番号」を返す。
    あるラウンドのグループ index（= 勝者の通し番号）が、次ラウンドの
    どのグループに合流するかを、次ラウンドのグループサイズ列から算出する。
    例: next_sizes=[3,3,3,3,3] のとき flat_index 0..2→0, 3..5→1, ...
        next_sizes=[3,2]       のとき flat_index 0..2→0, 3..4→1
    """
    if not next_sizes:
        return 0
    cum = 0
    for gi, sz in enumerate(next_sizes):
        cum += sz
        if flat_index < cum:
            return gi
    return len(next_sizes) - 1


def _choose_super_seed_groups(
    n_super: int,
    r3_sizes: list[int],
    r4_sizes: list[int],
    seed_r2_group_indices: list[int],
    r3_dest_of_r2: list[int] | None = None,
) -> list[int]:
    """
    スーパーシードを配置するR3グループ index を選ぶ。
    回避優先順位（シード/スーパーシードが登場ラウンド〜その次ラウンドで対戦しないこと）:
      1. スーパーシード同士が同じR3グループにならない
      2. 流れ込むシードが少ないR3グループを優先（シード×スーパーシードのR3回避）
      3. スーパーシード同士が同じR4グループに合流しない
      4. シードが合流するR4グループを避ける
      5. 最大間隔アンカーに近いグループ（前後の分散を維持）
    遵守できない場合（例：全R3グループにシードが流れ込む等）は上記の優先順で
    可能な範囲で最良の配置にフォールバックする。
    返り値は必ず有効な範囲 [0, len(r3_sizes)) の index で、長さ n_super。
    """
    n_r3g = len(r3_sizes)
    if n_super <= 0 or n_r3g <= 0:
        return []

    # 各R3グループに「シードを含むR2グループ」が何個流れ込むかを件数で数える。
    # （真偽だけだと全グループにシードが流れ込む構成で差がつかず、最もシード密度の
    #   高いグループを避けられない＝シードと同ラウンドに着席してしまう問題を防ぐ）
    if r3_dest_of_r2 is None:
        r3_dest_of_r2 = []
    seed_feed_count = {g: 0 for g in range(n_r3g)}
    for g in seed_r2_group_indices:
        dest = r3_dest_of_r2[g] if g < len(r3_dest_of_r2) else _block_of(g, r3_sizes)
        if 0 <= dest < n_r3g:
            seed_feed_count[dest] += 1
    seed_r3_groups = {g for g, c in seed_feed_count.items() if c > 0}
    seed_r4_groups = set(_block_of(s, r4_sizes) for s in seed_r3_groups) if r4_sizes else set()

    anchors = _spread_indices(n_super, n_r3g)  # 各スーパーシードの基準位置（最大間隔）

    chosen: list[int] = []
    used_r3: set[int] = set()
    used_r4: set[int] = set()
    for i in range(n_super):
        anchor = anchors[i] if i < len(anchors) else 0

        def _key(g):
            g_r4 = _block_of(g, r4_sizes) if r4_sizes else -1
            return (
                # 1) スーパーシード同士は別のR3グループ（最重要）
                0 if g not in used_r3 else 1,
                # 2) 基準位置（最大間隔アンカー）に近いグループを優先
                #    ← 「隣の組に配置しない」がシード回避より優先（仕様）
                abs(g - anchor),
                # 3) 流れ込むシードが少ないグループを優先（シード×スーパーシードのR3回避）
                seed_feed_count.get(g, 0),
                # 4) スーパーシード同士は別のR4グループ
                0 if (not r4_sizes or g_r4 not in used_r4) else 1,
                # 5) シードが合流するR4グループを回避（できれば）
                0 if (not r4_sizes or g_r4 not in seed_r4_groups) else 1,
                g,
            )

        best_g = min(range(n_r3g), key=_key)
        chosen.append(best_g)
        used_r3.add(best_g)
        if r4_sizes:
            used_r4.add(_block_of(best_g, r4_sizes))
    return chosen




# ── 裏トーナメント（敗者復活 / ルーザーズブラケット） ───────────────
# 裏ラウンドの round_no は表（1..N）と衝突させないため LOSERS_ROUND_BASE+ を使う。
# round_type='losers' で表ラウンドと区別。既存の生成/進行/結果入力フローを流用する。
LOSERS_ROUND_BASE = 100


async def _lb_enabled(tid: int, db: aiosqlite.Connection):
    """裏トーナメント設定を返す: (有効か, 復活先ラウンド番号 or None)"""
    async with db.execute(
        "SELECT losers_bracket, revival_target_round FROM tournaments WHERE id=?", (tid,)
    ) as cur:
        row = await cur.fetchone()
    if not row or not row["losers_bracket"]:
        return (False, None)
    return (True, row["revival_target_round"])


async def _collect_round_losers(round_id: int, db: aiosqlite.Connection):
    """round_id 内の各グループの敗者（勝者以外の全エントリー）を返す。
    再戦回避のため同組だった相手 opponents も持つ。
    返り値: [{"entry_id":int, "opponents":[int,...]}]"""
    losers = []
    async with db.execute(
        "SELECT id FROM bracket_groups WHERE round_id=? ORDER BY group_no", (round_id,)
    ) as cur:
        gids = [r["id"] for r in await cur.fetchall()]
    for gid in gids:
        async with db.execute(
            "SELECT winner_slot_id FROM bracket_results WHERE group_id=?", (gid,)
        ) as cur:
            wr = await cur.fetchone()
        winner_slot = wr["winner_slot_id"] if wr else None
        async with db.execute(
            "SELECT id AS slot_id, entry_id FROM bracket_slots "
            "WHERE group_id=? AND entry_id IS NOT NULL ORDER BY slot_no", (gid,)
        ) as cur:
            slots = await cur.fetchall()
        members = [s["entry_id"] for s in slots]
        for s in slots:
            if winner_slot is not None and s["slot_id"] == winner_slot:
                continue
            losers.append({
                "entry_id": s["entry_id"],
                "opponents": [m for m in members if m != s["entry_id"]],
            })
    return losers


async def _group_winners(round_id: int, db: aiosqlite.Connection):
    """round_id の各グループの勝者 entry_id を返す（全グループ確定済み前提）。"""
    winners = []
    async with db.execute(
        "SELECT id FROM bracket_groups WHERE round_id=? ORDER BY group_no", (round_id,)
    ) as cur:
        gids = [r["id"] for r in await cur.fetchall()]
    for gid in gids:
        async with db.execute(
            "SELECT bs.entry_id FROM bracket_results br "
            "JOIN bracket_slots bs ON bs.id=br.winner_slot_id WHERE br.group_id=?", (gid,)
        ) as cur:
            r = await cur.fetchone()
        if r and r["entry_id"] is not None:
            winners.append(r["entry_id"])
    return winners


async def _round_complete(round_id: int, db: aiosqlite.Connection) -> bool:
    """round_id の全グループに勝者が設定済みか。"""
    async with db.execute(
        "SELECT COUNT(*) AS n FROM bracket_groups WHERE round_id=?", (round_id,)
    ) as cur:
        n = (await cur.fetchone())["n"]
    if n == 0:
        return False
    async with db.execute(
        "SELECT COUNT(*) AS w FROM bracket_groups bg "
        "JOIN bracket_results br ON br.group_id=bg.id "
        "WHERE bg.round_id=? AND br.winner_slot_id IS NOT NULL", (round_id,)
    ) as cur:
        w = (await cur.fetchone())["w"]
    return w == n


async def _build_opponents_map(tid: int, db: aiosqlite.Connection):
    """この大会で同組になったことのある相手の対応表 entry_id -> set(entry_id)。
    再戦回避に使う（表・裏すべてのグループを走査）。"""
    opp: dict[int, set] = {}
    async with db.execute(
        "SELECT bg.id FROM bracket_groups bg "
        "JOIN bracket_rounds br ON br.id=bg.round_id WHERE br.tournament_id=?", (tid,)
    ) as cur:
        gids = [r["id"] for r in await cur.fetchall()]
    for gid in gids:
        async with db.execute(
            "SELECT entry_id FROM bracket_slots WHERE group_id=? AND entry_id IS NOT NULL", (gid,)
        ) as cur:
            members = [r["entry_id"] for r in await cur.fetchall()]
        for e in members:
            opp.setdefault(e, set()).update(m for m in members if m != e)
    return opp


def _assign_losers_groups(participants: list[int], sizes: list[int], opp_map: dict,
                          survivors: set | None = None) -> list[list[int]]:
    """participants を sizes（2/3の並び）に割り当てる。
    - 表/裏で当たった相手との同組（再戦）を避ける。
    - survivors（前裏ラウンドの勝ち残り）を各ヒートになるべく均等配分し、
      新規ドロップ（表ラウンド敗者）と混ぜる（勝ち残りの偏りを防ぐ）。
    バックトラッキングで「勝ち残り均等＋再戦ゼロ」を探索し、無ければ
    「再戦ゼロのみ」→「貪欲詰め」の順にフォールバックする。"""
    if not sizes:
        return []
    survivors = set(survivors or [])
    pset = set(participants)
    G = len(sizes)
    n = len(participants)

    def clash_in(group, cand):
        opp = opp_map.get(cand, set())
        return any(m in opp for m in group)

    # 衝突次数の高い順に割り当てると枝刈りが効きやすい
    degree = {p: len(opp_map.get(p, set()) & pset) for p in participants}
    order = sorted(participants, key=lambda p: -degree[p])

    # 勝ち残り（survivor）の各グループ割当数 quota を均等に決める。
    # floor=S//G, rem=S%G → rem 個のグループが floor+1。大きいグループに多めを配分。
    S = sum(1 for p in participants if p in survivors)
    base, rem = (S // G, S % G) if G else (0, 0)
    quota = [base] * G
    for j in sorted(range(G), key=lambda i: -sizes[i])[:rem]:
        quota[j] += 1
    # サイズ超過の補正（quota[i] <= sizes[i]）
    overflow = 0
    for i in range(G):
        if quota[i] > sizes[i]:
            overflow += quota[i] - sizes[i]
            quota[i] = sizes[i]
    i = 0
    while overflow > 0 and i < G:
        room = sizes[i] - quota[i]
        if room > 0:
            add = min(room, overflow)
            quota[i] += add
            overflow -= add
        i += 1

    def search(use_quota):
        groups: list[list[int]] = [[] for _ in sizes]
        surv_cnt = [0] * G
        found = [None]
        calls = [0]
        LIMIT = 300000

        def bt(idx):
            if found[0] is not None or calls[0] > LIMIT:
                return
            calls[0] += 1
            if idx == n:
                found[0] = [g[:] for g in groups]
                return
            cand = order[idx]
            is_surv = cand in survivors
            tried = set()
            for gi in range(G):
                if len(groups[gi]) >= sizes[gi]:
                    continue
                if use_quota:
                    if is_surv and surv_cnt[gi] >= quota[gi]:
                        continue
                    if not is_surv:
                        # survivor 用に確保すべき残り枠を侵さない
                        remaining_surv_slots = quota[gi] - surv_cnt[gi]
                        free = sizes[gi] - len(groups[gi])
                        if free <= remaining_surv_slots:
                            continue
                # 対称性削減：同容量・同survivor数・同状態のグループは1つだけ試す
                key = (sizes[gi], surv_cnt[gi], tuple(groups[gi]))
                if key in tried:
                    continue
                tried.add(key)
                if clash_in(groups[gi], cand):
                    continue
                groups[gi].append(cand)
                if is_surv:
                    surv_cnt[gi] += 1
                bt(idx + 1)
                groups[gi].pop()
                if is_surv:
                    surv_cnt[gi] -= 1
                if found[0] is not None:
                    return

        bt(0)
        return found[0]

    # ① 勝ち残り均等＋再戦ゼロ → ② 再戦ゼロのみ
    res = search(True)
    if res is None:
        res = search(False)
    if res is not None:
        return res

    # ③ いずれも不能 → 貪欲法で再戦最小化して詰める
    groups = [[] for _ in sizes]
    remaining = list(participants)
    for gi, sz in enumerate(sizes):
        while len(groups[gi]) < sz and remaining:
            pick_idx = None
            for idx, cand in enumerate(remaining):
                if not clash_in(groups[gi], cand):
                    pick_idx = idx
                    break
            if pick_idx is None:
                pick_idx = 0  # 全員と当たり済み等 → 遵守不能、先頭を詰める
            groups[gi].append(remaining.pop(pick_idx))
    for r in remaining:
        groups[-1].append(r)
    return groups


def _pick_losers_sizes(n: int) -> list[int]:
    """n人を2-3で分割。可能なら3人優先（グループ数最小）。
    分割不能（n=1）は[1]、n=0は[]。"""
    if n <= 0:
        return []
    if n == 1:
        return [1]  # 1人＝不戦勝（次段へ繰り上げ）
    pats = combinations_2_3(n)
    if not pats:
        # 2/3で割れない端数（理論上 n>=2 では起きないが保険）
        sizes = []
        while n > 3:
            sizes.append(3); n -= 3
        sizes.append(n)
        return sizes
    # グループ数が最小（=3人が多い）パターンを選ぶ
    return min(pats, key=lambda p: (len(p), -sum(1 for x in p if x == 3)))


async def _create_losers_round(tid: int, k: int, participants: list[int], db: aiosqlite.Connection,
                               survivors: set | None = None):
    """裏ラウンド k（round_no=BASE+k, type='losers'）を生成。participants を 2-3 で組む。
    survivors（前裏ラウンドの勝ち残り）は各ヒートに均等配分する。
    1人だけの場合はそのまま1人グループ（不戦勝）で作る。"""
    sizes = _pick_losers_sizes(len(participants))
    if not sizes:
        return None
    opp_map = await _build_opponents_map(tid, db)
    groups = _assign_losers_groups(participants, sizes, opp_map, survivors)
    cur = await db.execute(
        "INSERT INTO bracket_rounds (tournament_id, round_no, round_type) VALUES (?,?,?)",
        (tid, LOSERS_ROUND_BASE + k, "losers"),
    )
    rid = cur.lastrowid
    for gno, members in enumerate(groups, start=1):
        gcur = await db.execute(
            "INSERT INTO bracket_groups (round_id, group_no) VALUES (?,?)", (rid, gno)
        )
        gid = gcur.lastrowid
        for sno, eid in enumerate(members, start=1):
            await db.execute(
                "INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)",
                (gid, sno, eid),
            )
        # 1人グループ（不戦勝）は即勝者扱い
        if len(members) == 1:
            async with db.execute(
                "SELECT id FROM bracket_slots WHERE group_id=? ORDER BY slot_no", (gid,)
            ) as c:
                only = await c.fetchone()
            if only:
                await db.execute(
                    "INSERT INTO bracket_results (group_id, winner_slot_id) VALUES (?,?)",
                    (gid, only["id"]),
                )
                await db.execute(
                    "INSERT INTO bracket_slot_ranks (group_id, slot_id, rank) VALUES (?,?,1)",
                    (gid, only["id"]),
                )
    await db.commit()
    return rid


async def _sync_losers_bracket(tid: int, db: aiosqlite.Connection):
    """表ラウンドの確定状況に応じて裏ラウンドを必要なだけ生成する（冪等）。
    モデル（仕様書§4.1）:
      裏L1 = 表R1敗者
      裏Lk = 裏L(k-1)勝者 + 表Rk敗者   (k <= 流入ラウンド数)
      流入を消費後、勝者が2人以上なら純減ラウンドを続け1人に収束させる
    復活先ラウンド未満の表ラウンドのみ流入対象（以降の敗者は対象外＝復活はしない）。
    """
    enabled, target = await _lb_enabled(tid, db)
    if not enabled:
        return
    # 表のnormalラウンド（round_no順）
    async with db.execute(
        "SELECT round_no, id FROM bracket_rounds "
        "WHERE tournament_id=? AND round_type='normal' ORDER BY round_no", (tid,)
    ) as cur:
        w_rounds = [(r["round_no"], r["id"]) for r in await cur.fetchall()]
    # 流入対象 = すべての表normalラウンド（表Rk敗者 → 裏Rk）。
    # 復活先ラウンド（target）に依存せず、各表ラウンドの敗者を対応する裏ラウンドへ取り込む。
    #   裏L1 = 表R1敗者
    #   裏Lk = 裏L(k-1)勝者 + 表Rk敗者
    #   流入を消費後は純減ラウンドを続け1人に収束させ、復活先ラウンドへ差し込む。
    feeding = list(w_rounds)
    if not feeding:
        return

    # 既存の裏ラウンド（round_no順）
    async with db.execute(
        "SELECT round_no, id FROM bracket_rounds "
        "WHERE tournament_id=? AND round_type='losers' ORDER BY round_no", (tid,)
    ) as cur:
        l_rounds = [(r["round_no"] - LOSERS_ROUND_BASE, r["id"]) for r in await cur.fetchall()]

    # 既存の最後の裏ラウンドが未完了なら何もしない（先に結果入力が必要）
    if l_rounds:
        last_k, last_lid = l_rounds[-1]
        if not await _round_complete(last_lid, db):
            return

    next_k = (l_rounds[-1][0] + 1) if l_rounds else 1

    # 次の裏ラウンドの参加者を決める
    survivors: set = set()  # 勝ち残り（前裏ラウンド勝者）= 各ヒートに均等配分する対象
    if next_k == 1:
        # L1 = 表R1敗者（流入1番目の表ラウンドが完了している必要）
        feed_rno, feed_rid = feeding[0]
        if not await _round_complete(feed_rid, db):
            return
        losers = await _collect_round_losers(feed_rid, db)
        participants = [x["entry_id"] for x in losers]
    elif next_k <= len(feeding):
        # Lk = L(k-1)勝者 + 表Rk敗者
        prev_lid = l_rounds[-1][1]
        feed_rno, feed_rid = feeding[next_k - 1]
        if not await _round_complete(feed_rid, db):
            return
        prev_winners = await _group_winners(prev_lid, db)
        new_losers = [x["entry_id"] for x in await _collect_round_losers(feed_rid, db)]
        participants = prev_winners + new_losers
        survivors = set(prev_winners)  # 勝ち残りを各ヒートへ分散
    else:
        # 流入消費後の純減ラウンド：L(k-1)勝者のみ
        prev_lid = l_rounds[-1][1]
        prev_winners = await _group_winners(prev_lid, db)
        participants = prev_winners

    # 1人以下に収束 → 裏ラウンドは作らない（復活差し込みに委ねる）
    if len(participants) <= 1:
        return
    await _create_losers_round(tid, next_k, participants, db, survivors)


async def _losers_champion(tid: int, db: aiosqlite.Connection):
    """裏ブラケットが1名に収束していれば、その entry_id を返す。まだなら None。"""
    enabled, target = await _lb_enabled(tid, db)
    if not enabled:
        return None
    async with db.execute(
        "SELECT id FROM bracket_rounds WHERE tournament_id=? AND round_type='losers' "
        "ORDER BY round_no DESC LIMIT 1", (tid,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    last_lid = row["id"]
    if not await _round_complete(last_lid, db):
        return None
    winners = await _group_winners(last_lid, db)
    # 全流入を消費済みで勝者1名なら確定。複数なら純減継続中。
    if len(winners) == 1:
        return winners[0]
    return None


async def _try_insert_reviver(tid: int, db: aiosqlite.Connection):
    """裏優勝者が確定したら、復活先ラウンドのグループへ差し込む（再戦回避・2→3拡張）。
    既に差し込み済みなら何もしない。"""
    enabled, target = await _lb_enabled(tid, db)
    if not enabled:
        return False
    # 流入対象が残っている間は確定しない（純減で1名に収束してから）
    champ = await _losers_champion(tid, db)
    if champ is None:
        return False
    # 復活先ラウンド
    async with db.execute(
        "SELECT MAX(round_no) AS m FROM bracket_rounds "
        "WHERE tournament_id=? AND round_type IN ('final','normal')", (tid,)
    ) as cur:
        last_no = (await cur.fetchone())["m"]
    target_no = target if target else (last_no or 0)
    async with db.execute(
        "SELECT id FROM bracket_rounds WHERE tournament_id=? AND round_no=?", (tid, target_no)
    ) as cur:
        tr = await cur.fetchone()
    if not tr:
        return False
    target_rid = tr["id"]
    # 既に差し込み済み？（target round にchampが居る）
    async with db.execute(
        "SELECT COUNT(*) AS c FROM bracket_slots bs "
        "JOIN bracket_groups bg ON bg.id=bs.group_id "
        "WHERE bg.round_id=? AND bs.entry_id=?", (target_rid, champ)
    ) as cur:
        if (await cur.fetchone())["c"] > 0:
            return False
    # 差し込み先グループを選ぶ：空きスロット(entry_id IS NULL)を持つグループ優先、
    # なければ2人組を3人に拡張。再戦回避を加味。
    opp_map = await _build_opponents_map(tid, db)
    async with db.execute(
        "SELECT id, group_no FROM bracket_groups WHERE round_id=? ORDER BY group_no", (target_rid,)
    ) as cur:
        groups = await cur.fetchall()

    def clash(gid_members):
        return any(m in opp_map.get(champ, set()) for m in gid_members)

    # 各グループの状況を集計
    cand_empty = []   # 空きスロットありのグループ
    cand_two = []     # 2人組（拡張で3人にできる）
    for g in groups:
        async with db.execute(
            "SELECT id, slot_no, entry_id FROM bracket_slots WHERE group_id=? ORDER BY slot_no", (g["id"],)
        ) as c:
            slots = await c.fetchall()
        members = [s["entry_id"] for s in slots if s["entry_id"] is not None]
        empty = [s for s in slots if s["entry_id"] is None]
        info = {"gid": g["id"], "members": members, "empty": empty,
                "filled": len(members), "clash": clash(members)}
        if empty:
            cand_empty.append(info)
        elif len(members) == 2:
            cand_two.append(info)

    def best(cands):
        # 再戦なし → 優先、次に人数が少ない
        return sorted(cands, key=lambda x: (x["clash"], x["filled"]))[0] if cands else None

    chosen = best(cand_empty) or best(cand_two)
    if chosen:
        # 空きスロット（予約枠）or 2人組（→3人に拡張）に入れる。いずれも2〜3を維持。
        if chosen.get("empty"):
            slot = chosen["empty"][0]
            await db.execute("UPDATE bracket_slots SET entry_id=? WHERE id=?", (champ, slot["id"]))
        else:
            async with db.execute(
                "SELECT COALESCE(MAX(slot_no),0)+1 AS nx FROM bracket_slots WHERE group_id=?", (chosen["gid"],)
            ) as c:
                nx = (await c.fetchone())["nx"]
            await db.execute(
                "INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)",
                (chosen["gid"], nx, champ),
            )
        await db.commit()
        return True

    # ここに到達＝全グループが3人で空き枠なし。
    # 「1組2〜3人」を絶対に守るため、4人組は作らない。
    # いずれかの3人組から1名を新グループへ移し、復活者と2人組を作る
    # （元の組は2人になる。復活者と再戦しない相手を優先して移動）。
    pool = []
    for g in groups:
        async with db.execute(
            "SELECT id, slot_no, entry_id FROM bracket_slots WHERE group_id=? AND entry_id IS NOT NULL ORDER BY slot_no",
            (g["id"],)
        ) as c:
            slots = await c.fetchall()
        members = [s["entry_id"] for s in slots]
        # 末尾メンバーを移動候補に（復活者と再戦になる相手は避ける）
        for s in reversed(slots):
            mv_clash = 1 if s["entry_id"] in opp_map.get(champ, set()) else 0
            pool.append({"slot_id": s["id"], "entry_id": s["entry_id"],
                         "src_gid": g["id"], "clash": mv_clash})
    # 再戦にならない相手を優先
    pool.sort(key=lambda x: x["clash"])
    mv = pool[0]
    # 新グループを作成（group_no は最大+1）
    async with db.execute(
        "SELECT COALESCE(MAX(group_no),0)+1 AS gn FROM bracket_groups WHERE round_id=?", (target_rid,)
    ) as c:
        new_gno = (await c.fetchone())["gn"]
    await db.execute("INSERT INTO bracket_groups (round_id, group_no) VALUES (?,?)", (target_rid, new_gno))
    async with db.execute("SELECT last_insert_rowid() AS id") as c:
        new_gid = (await c.fetchone())["id"]
    # 移動メンバーを新グループへ、復活者も新グループへ（2人組）
    await db.execute("UPDATE bracket_slots SET group_id=?, slot_no=1 WHERE id=?", (new_gid, mv["slot_id"]))
    await db.execute("INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (new_gid, 2, champ))
    await db.commit()
    return True


async def _teardown_losers(tid: int, db: aiosqlite.Connection, from_k: int = 1):
    """裏トーナメントのうち裏R{from_k}以降を削除し、復活先(決勝)に差し込まれた復活者も
    取り除く。上流(表ラウンド)の結果が取消/変更されたときに呼び、その後
    _sync_losers_bracket で現在の表から作り直す前提（下流の不整合・空スロット残りを防ぐ）。
    from_k=1 なら裏を全削除。表Rk を取り消した場合は from_k=k（裏R1..R(k-1)は表R1..は不変なので残す）。"""
    enabled, target = await _lb_enabled(tid, db)
    if not enabled:
        return
    # 復活者(裏優勝者)が復活先に差し込まれていれば取り除く（裏を作り直すので無効になる）
    champ = await _losers_champion(tid, db)
    if champ is not None:
        async with db.execute(
            "SELECT MAX(round_no) AS m FROM bracket_rounds "
            "WHERE tournament_id=? AND round_type IN ('final','normal')", (tid,)
        ) as cur:
            last_no = (await cur.fetchone())["m"]
        target_no = target if target else (last_no or 0)
        async with db.execute(
            "SELECT id FROM bracket_rounds WHERE tournament_id=? AND round_no=?", (tid, target_no)
        ) as cur:
            tr = await cur.fetchone()
        if tr:
            async with db.execute(
                "SELECT id FROM bracket_groups WHERE round_id=?", (tr["id"],)
            ) as cur:
                tgroups = [r["id"] for r in await cur.fetchall()]
            await db.execute(
                "DELETE FROM bracket_slots WHERE entry_id=? AND group_id IN "
                "(SELECT id FROM bracket_groups WHERE round_id=?)", (champ, tr["id"])
            )
            for gid in tgroups:
                await db.execute("DELETE FROM bracket_results WHERE group_id=?", (gid,))
                await db.execute("DELETE FROM bracket_slot_ranks WHERE group_id=?", (gid,))
            for gid in tgroups:
                cnt = (await (await db.execute(
                    "SELECT COUNT(*) AS c FROM bracket_slots WHERE group_id=? AND entry_id IS NOT NULL", (gid,)
                )).fetchone())["c"]
                if cnt == 0:
                    await db.execute("DELETE FROM bracket_slots WHERE group_id=?", (gid,))
                    await db.execute("DELETE FROM bracket_groups WHERE id=?", (gid,))
    # 裏R{from_k}以降を削除（round_no >= BASE+from_k）
    min_no = LOSERS_ROUND_BASE + max(1, from_k)
    async with db.execute(
        "SELECT id FROM bracket_rounds WHERE tournament_id=? AND round_type='losers' AND round_no>=?",
        (tid, min_no)
    ) as cur:
        lrs = [r["id"] for r in await cur.fetchall()]
    for rid in lrs:
        async with db.execute("SELECT id FROM bracket_groups WHERE round_id=?", (rid,)) as cur:
            gids = [r["id"] for r in await cur.fetchall()]
        for gid in gids:
            await db.execute("DELETE FROM bracket_slot_ranks WHERE group_id=?", (gid,))
            await db.execute("DELETE FROM bracket_results WHERE group_id=?", (gid,))
            await db.execute("DELETE FROM bracket_slots WHERE group_id=?", (gid,))
            await db.execute("DELETE FROM bracket_groups WHERE id=?", (gid,))
        await db.execute("DELETE FROM bracket_rounds WHERE id=?", (rid,))
    await db.commit()


async def _normal_round_index(tid: int, round_no: int, db: aiosqlite.Connection) -> int:
    """表normalラウンドの中で round_no が何番目か（1始まり）。= 対応する裏ラウンドのk。"""
    async with db.execute(
        "SELECT COUNT(*) AS c FROM bracket_rounds "
        "WHERE tournament_id=? AND round_type='normal' AND round_no<=?", (tid, round_no)
    ) as cur:
        return (await cur.fetchone())["c"]



# ── メイン画面 ────────────────────────────────────────────
from fastapi import Query as QParam

@router.get("/{tid}/bracket", response_class=HTMLResponse)
async def bracket_top(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url="/admin/tournaments/")

    # レース設定の決勝進出予定人数
    from app.routers.tournaments import calc_finalists as calc_n
    t_dict = dict(t)
    finalist_n = calc_n(t_dict.get("qualifying_type",""), t_dict) or 0

    # heat_roundrobin: 全ヒート完了チェック
    qualifying_incomplete = False
    qualifying_incomplete_msg = ""

    # heat_tournament でヒート決勝ありの場合: 全ヒートのヒート決勝が完了しているか
    if t_dict.get("qualifying_type") in HEAT_TOURNAMENT_TYPES and bool(t_dict.get("qual_heat_final", 0)):
        from app.routers.qualifying import _ht_heat_final_section_no
        heat_count = int(t_dict.get("qual_heat_count") or 1)
        for hno in range(1, heat_count + 1):
            # ヒート決勝として扱う section（複数グループ=0 / 単一グループ=唯一の非0セクション）
            hf_sec = await _ht_heat_final_section_no(tid, hno, db)
            # 対象 section の final で順位が確定しているか
            async with db.execute(
                """SELECT COUNT(*) FROM ht_slot_ranks sr
                   JOIN ht_groups hg ON hg.id=sr.group_id
                   JOIN ht_rounds hr ON hr.id=hg.round_id
                   WHERE hr.tournament_id=? AND hr.heat_no=? AND COALESCE(hr.section_no,1)=?
                     AND hr.round_type='final'""",
                (tid, hno, hf_sec),
            ) as cur:
                hf_done_cnt = (await cur.fetchone())[0]
            # そのヒートにラウンドが存在するか（存在しないヒートはスキップ）
            async with db.execute(
                "SELECT COUNT(*) FROM ht_rounds WHERE tournament_id=? AND heat_no=?",
                (tid, hno),
            ) as cur:
                has_groups = (await cur.fetchone())[0] > 0
            if has_groups and hf_done_cnt == 0:
                qualifying_incomplete = True
                qualifying_incomplete_msg = (
                    f"ヒート{hno}のヒート決勝トーナメントが未生成または未完了です。"
                    "予選管理画面でヒート決勝トーナメントを生成し、順位を確定してください。"
                )
                break

    if t_dict.get("qualifying_type") == "heat_roundrobin" and finalist_n > 0:
        # heatsの完了状況で判断（advanced=1ではなく）
        async with db.execute(
            "SELECT COUNT(*) as total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done FROM heats WHERE tournament_id=?", (tid,)
        ) as cur:
            heat_row = dict(await cur.fetchone())
        total_heats = heat_row.get("total") or 0
        done_heats = heat_row.get("done") or 0
        if total_heats == 0 or done_heats < total_heats:
            # advanced=1で判断にフォールバック
            async with db.execute(
                "SELECT COUNT(*) FROM entries WHERE tournament_id=? AND advanced=1", (tid,)
            ) as cur:
                advanced_count = (await cur.fetchone())[0]
            if advanced_count < finalist_n and (total_heats == 0 or done_heats < total_heats):
                qualifying_incomplete = True
                qualifying_incomplete_msg = (
                    f"全ヒートが完了していません。"
                    f"現在 {done_heats}/{total_heats} ヒートが完了しています。"
                    f"全ヒートが完了してから決勝管理を利用してください。"
                )

    # advanced=1（決勝進出○）のレーサーを取得
    # ※ heat_tournament でまだ決勝トーナメント未生成の場合、entries.advanced が
    #   旧ロジック（グループ通過者）のまま残っていることがあるため、ここで最新化する。
    _has_rounds_chk = await _get_rounds(tid, db)
    if dict(t).get("qualifying_type") in HEAT_TOURNAMENT_TYPES and not [r for r in _has_rounds_chk if r["round_type"] != "losers"]:
        try:
            from app.routers.qualifying import _ht_update_advanced
            await _ht_update_advanced(tid, db)
        except Exception:
            pass
    advanced_entries = await _get_advanced_entries(tid, db)
    all_standings = await _get_all_standings(tid, db)

    # 既存ラウンド（裏トーナメントは表の描画パイプラインから分離）
    all_rounds = await _get_rounds(tid, db)
    rounds = [r for r in all_rounds if r["round_type"] != "losers"]
    losers_rounds = [r for r in all_rounds if r["round_type"] == "losers"]

    finalists = []
    tie_selection_needed = False
    tie_candidates = []
    patterns = []
    lottery = None  # くじ割り当てデータ（lottery_pending時のみ構築）

    # none_roundrobin は最優先でリダイレクト
    if dict(t).get("qualifying_type") == "none_roundrobin":
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)

    if rounds:
        # ラウンド生成済み → スロットからfinalists復元
        async with db.execute(
            """SELECT DISTINCT bs.entry_id, r.name
               FROM bracket_rounds br
               JOIN bracket_groups bg ON bg.round_id=br.id
               JOIN bracket_slots bs ON bs.group_id=bg.id
               JOIN entries e ON e.id=bs.entry_id
               JOIN racers r ON r.id=e.racer_id
               WHERE br.tournament_id=? AND br.round_no=1
               ORDER BY bg.group_no, bs.slot_no""",
            (tid,),
        ) as cur:
            finalists = [dict(r) for r in await cur.fetchall()]
        # くじ割り当て中なら区分ごとの番号付き枠データを構築
        if dict(t).get("lottery_pending"):
            # 未割り当て候補を区分(seeded:0/1/2)込みで取得
            async with db.execute(
                """SELECT e.id AS entry_id, r.name, COALESCE(e.pre_seq_no, 0) AS seq,
                          COALESCE(e.seeded,0) AS seeded
                   FROM entries e JOIN racers r ON r.id=e.racer_id
                   WHERE e.tournament_id=? AND e.status='active'
                     AND e.id NOT IN (
                        SELECT bs.entry_id FROM bracket_slots bs
                        JOIN bracket_groups bg ON bg.id=bs.group_id
                        JOIN bracket_rounds br ON br.id=bg.round_id
                        WHERE br.tournament_id=? AND bs.entry_id IS NOT NULL
                     )
                   ORDER BY e.entry_order""",
                (tid, tid),
            ) as cur:
                unassigned_all = [dict(r) for r in await cur.fetchall()]
            # round_no と seeded の対応： R1=通常(0) / R2=シード(1) / R3=スーパーシード(2)
            round_to_seeded = {1: 0, 2: 1, 3: 2}

            lottery_rounds = []
            for rno in (1, 2, 3):
                async with db.execute(
                    """SELECT bs.lottery_no, bs.entry_id, r.name AS racer_name
                       FROM bracket_slots bs
                       JOIN bracket_groups bg ON bg.id=bs.group_id
                       JOIN bracket_rounds br ON br.id=bg.round_id
                       LEFT JOIN entries e ON e.id=bs.entry_id
                       LEFT JOIN racers r ON r.id=e.racer_id
                       WHERE br.tournament_id=? AND br.round_no=? AND bs.lottery_no IS NOT NULL
                       ORDER BY bs.lottery_no""",
                    (tid, rno),
                ) as cur:
                    slots = [dict(r) for r in await cur.fetchall()]
                if slots:
                    label = {1: "通常（R1）", 2: "シード（R2）", 3: "スーパーシード（R3）"}.get(rno, f"R{rno}")
                    filled = sum(1 for s in slots if s["entry_id"] is not None)
                    want = round_to_seeded.get(rno, 0)
                    cand = [u for u in unassigned_all if u["seeded"] == want]
                    lottery_rounds.append({
                        "round_no": rno, "label": label,
                        "slots": slots, "filled": filled, "total": len(slots),
                        "candidates": cand,
                    })
            total_slots = sum(r["total"] for r in lottery_rounds)
            total_filled = sum(r["filled"] for r in lottery_rounds)
            lottery = {
                "rounds": lottery_rounds,
                "total_slots": total_slots,
                "total_filled": total_filled,
                "complete": total_filled == total_slots and total_slots > 0,
            }
    elif advanced_entries or dict(t).get("qualifying_type") in HEAT_TOURNAMENT_TYPES:
        # advanced=1 が未設定でも heat_tournament はランクから直接進出者を再構成（レース管理を開かなくても反映）
        if dict(t).get("qualifying_type") in HEAT_TOURNAMENT_TYPES:
            # ヒートトーナメント：ht_per_heat_advancedを使って重複込みでフラット化
            ht_adv_flat = await _get_ht_per_heat_advanced(tid, db)
            finalists = []
            for heat in ht_adv_flat:
                for a in heat["advanced"]:
                    finalists.append({"entry_id": a["entry_id"], "name": a["name"], "seeded": 0, "heat_no": heat["heat_no"], "overall_rank": a.get("overall_rank", 99)})
            # ht_per_heat_advancedが空の場合（予選未完了）はadvanced_entriesにフォールバック
            if not finalists:
                finalists = advanced_entries
        elif dict(t).get("qualifying_type") == "heat_roundrobin" and t_dict.get("qual_heat_final"):
            # ヒート決勝あり: heat_finals.deciding_rank から進出者を取得（heat_no付き）
            heat_final_advance = int(t_dict.get("qual_heat_final_advance") or 1)
            finalists = []
            async with db.execute("""
                SELECT hf.entry_id, r.name, hf.round_no as heat_no, hf.deciding_rank as overall_rank
                FROM heat_finals hf
                LEFT JOIN entries e ON e.id=hf.entry_id
                LEFT JOIN racers r ON r.id=e.racer_id
                WHERE hf.tournament_id=? AND hf.deciding_rank IS NOT NULL AND hf.deciding_rank<=?
                  AND (hf.final_type='heat' OR hf.final_type IS NULL)
                ORDER BY hf.round_no, hf.deciding_rank
            """, (tid, heat_final_advance)) as cur:
                for row in await cur.fetchall():
                    row = dict(row)
                    if row["entry_id"]:
                        finalists.append({"entry_id": row["entry_id"], "name": row["name"], "seeded": 0,
                                          "heat_no": row["heat_no"], "overall_rank": row["overall_rank"]})
        else:
            finalists = advanced_entries
        patterns = combinations_2_3(len(finalists))  # 後でseeded_ids確定後に再計算
    elif dict(t).get("qualifying_type") == "none":
        # 予選なし → 全エントリーを決勝進出者として扱う
        async with db.execute(
            """SELECT e.id as entry_id, e.racer_id, r.name, e.seeded, 0 as score1, 0 as score2
               FROM entries e JOIN racers r ON r.id=e.racer_id
               WHERE e.tournament_id=? AND e.status='active'
               ORDER BY e.entry_order""",
            (tid,),
        ) as cur:
            finalists = [dict(r) for r in await cur.fetchall()]
        patterns = combinations_2_3(len(finalists))
    else:
        # advancedが誰も設定されていない → 上位N名・同率チェック
        if dict(t).get("qualifying_type") == "heat_roundrobin" and finalist_n > 0:
            has_heat_final = bool(t_dict.get("qual_heat_final", 0))
            heat_advance_v = int(t_dict.get("qual_heat_advance") or 1)
            group_advance_v = int(t_dict.get("qual_group_advance") or heat_advance_v)
            heat_count_v = int(t_dict.get("qual_heat_count") or 1)
            group_count_v = int(t_dict.get("qual_group_count") or 1)
            finalists = []
            seen = set()

            if has_heat_final:
                # ヒート決勝あり: 各ヒートの ht_* セクション0（ヒート決勝）の
                # 上位 qual_heat_final_advance 名を本戦進出者として取得（heat_no付き）。
                from app.routers.qualifying import _ht_get_heatfinal_advancers
                heat_final_advance = int(t_dict.get("qual_heat_advance") or 1)
                for hno in range(1, heat_count_v + 1):
                    advs = await _ht_get_heatfinal_advancers(tid, hno, heat_final_advance, db)
                    for a in advs:
                        if a.get("is_advance") and a.get("entry_id") and a["entry_id"] not in seen:
                            finalists.append({"entry_id": a["entry_id"], "name": a["name"], "seeded": 0,
                                              "heat_no": hno, "overall_rank": a["rank"]})
                            seen.add(a["entry_id"])
            else:
                # ヒート決勝なし: グループ総当たり順位からグループごとに上位 group_advance 名を取得
                from app.routers.qualifying import _calc_standings_group_round
                for hno in range(1, heat_count_v + 1):
                    for gno in range(1, group_count_v + 1):
                        grp_standings = await _calc_standings_group_round(tid, gno, hno, db)
                        for s in grp_standings:
                            if s.get("rank", 99) <= group_advance_v and s["entry_id"] not in seen:
                                finalists.append({"entry_id": s["entry_id"], "name": s["name"], "seeded": 0})
                                seen.add(s["entry_id"])

            if finalists:
                patterns = combinations_2_3(len(finalists))
            else:
                finalists = []
                patterns = []
        elif finalist_n > 0 and all_standings:
            # 締め切りは「順位の値」ではなく「順位の位置」で判定する。
            # （旧実装は rank<=finalist_n で判定していたため、結果未入力で全員同率1位のとき
            #   1<=定員 が全員成立して全エントリーが決勝進出になっていた）
            if len(all_standings) <= finalist_n:
                # エントリー数が定員以下 → 全員進出
                finalists = all_standings
                patterns = combinations_2_3(len(finalists))
            else:
                border_rank = all_standings[finalist_n - 1]["rank"]   # 定員ちょうどの位置の順位
                definitely_in = [r for r in all_standings if r["rank"] < border_rank]   # 確定進出
                tie_at_border = [r for r in all_standings if r["rank"] == border_rank]  # 締め切り順位の同率者
                if len(definitely_in) + len(tie_at_border) > finalist_n:
                    # 締め切り順位が同率でオーバーフロー
                    if definitely_in:
                        # 一部は確定・残りを同率者から選択（同率選択UI）
                        tie_selection_needed = True
                        tie_candidates = tie_at_border
                        finalists = definitely_in
                        patterns = []
                    else:
                        # 全員が最上位で同率（＝予選結果が未入力/未確定のケースを含む）
                        # 自動で進出者を作らず、予選管理で進出（○）を設定させる
                        finalists = []
                        patterns = []
                else:
                    # 同率が締め切りをまたがない → そのまま確定
                    finalists = definitely_in + tie_at_border
                    patterns = combinations_2_3(len(finalists))
        else:
            finalists = all_standings
            patterns = combinations_2_3(len(finalists))

    # グループ・スロット・結果
    # 固定リンク未焼き付けの既存トーナメントを描画前に自己修復（安全な場合のみ）
    await _ensure_advance_links_baked(tid, db)
    groups_data = []
    for rnd in rounds:
        async with db.execute(
            "SELECT * FROM bracket_groups WHERE round_id=? ORDER BY group_no", (rnd["id"],)
        ) as cur:
            groups = await cur.fetchall()
        for g in groups:
            slots, result, ranks = await _get_group_detail(g["id"], db)
            groups_data.append({
                "round": dict(rnd),
                "group": dict(g),
                "slots": slots,
                "result": result,
                "ranks": ranks,
            })

    total_rounds = len({g["round"]["round_no"] for g in groups_data}) if groups_data else 0

    # 同率時に「あと何名必要か」
    needed_from_tie = 0
    if tie_selection_needed:
        needed_from_tie = finalist_n - len(finalists)

    # トーナメント図用データ生成（将来のラウンドもシミュレート）
    svg_data = _build_svg_data(groups_data)
    # 将来ラウンドをsv_data.roundsに追加（決勝まで枠表示）
    if svg_data["rounds"] and not any(r["label"] == "決勝" for r in svg_data["rounds"]):
        future = _simulate_future_rounds(groups_data, len(finalists))
        svg_data["rounds"].extend(future)

    # 全ラウンドのラベルを再計算（normalのみをカウント対象に）
    normal_rounds = [r for r in svg_data["rounds"] if r.get("round_type", "normal") not in ("third", "revival")]
    total_for_label = len(normal_rounds)
    for r in svg_data["rounds"]:
        rt = r.get("round_type", "normal")
        if rt == "final" or r.get("label") == "決勝":
            r["label"] = "決勝"
            r["round_type"] = "final"
        elif rt in ("third", "revival"):
            pass  # _build_svg_dataで既に正しいラベルが付いている
        else:
            r["label"] = round_label(r["round_no"], total_for_label, "normal")

    # groups_dataの各ラウンドにもラベルを付与（テンプレート表示用）
    # third/revival は svg_data["third_rounds"] のラベルを使う
    round_label_map = {r["round_no"]: r["label"] for r in svg_data["rounds"] if r.get("round_type", "normal") not in ("third", "revival")}
    third_label_map = {r["round_no"]: r["label"] for r in svg_data.get("third_rounds", [])}
    for gd in groups_data:
        rno = gd["round"]["round_no"]
        rt = gd["round"].get("round_type", "normal")
        if rt in ("third", "revival"):
            gd["round"]["label"] = third_label_map.get(rno, "3位決定戦" if rt == "third" else "敗者復活戦")
        else:
            gd["round"]["label"] = round_label_map.get(rno, f"R={rno}")

    # round_type=normalでもラベルが「決勝」なら決勝として扱う
    # DBも更新して永続化する
    for gd in groups_data:
        if gd["round"]["label"] == "決勝" and gd["round"]["round_type"] == "normal":
            gd["round"]["round_type"] = "final"
            await db.execute(
                "UPDATE bracket_rounds SET round_type='final' WHERE id=?",
                (gd["round"]["id"],),
            )
    await db.commit()

    # heat_tournament / heat_roundrobin+heat_final：重複レーサー検出（同一entry_idが複数ヒートで進出）
    ht_duplicates = []
    dup_resolved = request.query_params.get("dup_resolved") == "1"
    is_hr_heat_final = (dict(t).get("qualifying_type") == "heat_roundrobin" and bool(t_dict.get("qual_heat_final")))
    if (dict(t).get("qualifying_type") in HEAT_TOURNAMENT_TYPES or is_hr_heat_final) and not rounds and not dup_resolved:
        if dict(t).get("qualifying_type") in HEAT_TOURNAMENT_TYPES:
            ht_adv = await _get_ht_per_heat_advanced(tid, db)
            from collections import defaultdict
            racer_heat_map = defaultdict(list)
            for heat in ht_adv:
                for a in heat["advanced"]:
                    racer_heat_map[a["entry_id"]].append({
                        "heat_no": heat["heat_no"],
                        "overall_rank": a.get("overall_rank", 99),
                        "name": a["name"],
                        "entry_id": a["entry_id"],
                    })
        else:
            # heat_roundrobin + heat_final: finalists から重複を検出
            from collections import defaultdict
            racer_heat_map = defaultdict(list)
            for f in finalists:
                racer_heat_map[f["entry_id"]].append({
                    "heat_no": f.get("heat_no", 0),
                    "overall_rank": f.get("overall_rank", 99),
                    "name": f["name"],
                    "entry_id": f["entry_id"],
                })
        for entry_id, appearances in racer_heat_map.items():
            if len(appearances) > 1:
                ht_duplicates.append({
                    "entry_id": entry_id,
                    "name": appearances[0]["name"],
                    "appearances": sorted(appearances, key=lambda x: x["overall_rank"]),
                })

    seeded_ids, super_seeded_ids = await _get_seeded_ids(tid, db)
    # ht複数回出場時は枠単位(entry_id+heat_no)でシード判定
    ht_seed_keys = set()
    is_ht_multi = ((dict(t).get("qualifying_type") in HEAT_TOURNAMENT_TYPES or is_hr_heat_final) and not dup_resolved)
    # dup_resolved=1（複数回出場確定後）かつdup処理でmultiを選んだ場合も枠単位を使う
    # シードIDが存在しかつfinalistsに重複entry_idがある場合 = 複数回出場+任意シード
    entry_id_counts = {}
    for f in finalists:
        entry_id_counts[f["entry_id"]] = entry_id_counts.get(f["entry_id"], 0) + 1
    has_ht_duplicates = any(v > 1 for v in entry_id_counts.values())
    use_ht_seed = dict(t).get("qualifying_type") in HEAT_TOURNAMENT_TYPES and has_ht_duplicates
    if use_ht_seed:
        ht_seed_keys = await _get_ht_finalist_seed_keys(tid, db)
    # finalistsにseededフラグを付与
    for f in finalists:
        if use_ht_seed:
            # 枠単位(ht_seed_keys) OR entry_id単位(seeded_ids)のどちらかでシード判定
            f["seeded"] = 2 if f["entry_id"] in super_seeded_ids else (1 if (f["entry_id"], f.get("heat_no", 0)) in ht_seed_keys or f["entry_id"] in seeded_ids else 0)
        else:
            f["seeded"] = 2 if f["entry_id"] in super_seeded_ids else (1 if f["entry_id"] in seeded_ids else 0)

    next_round_select = None  # 廃止
    # パターンをシード考慮で再計算（seeded_ids確定後）
    target_n = len(finalists)  # デフォルト値
    patterns_multi = []  # 複数回出場用
    patterns_single = []  # 1回のみ出場用
    target_n_multi = len(finalists)
    target_n_single = len(finalists)
    if not rounds:
        # seeded=1: シード（R2合流）、seeded=2: スーパーシード（R3合流）、0: 通常（R1から）
        seeded_in_finalists = sum(1 for f in finalists if f.get("seeded") in (1, 2))
        seeded_entry_ids_disp = {f["entry_id"] for f in finalists if f.get("seeded") in (1, 2)}

        # 複数回出場: seeded=0のスロットのみR1（重複込み）
        non_seeded_multi = [f for f in finalists if f.get("seeded") == 0]
        target_n_multi = len(non_seeded_multi)

        # 1回のみ出場: seededのentry_idの重複スロットも除外
        non_seeded_single = [f for f in finalists if f.get("seeded") == 0 and f["entry_id"] not in seeded_entry_ids_disp]
        target_n_single = len(non_seeded_single)

        patterns_multi = combinations_2_3(target_n_multi) if target_n_multi >= 2 else []
        patterns_single = combinations_2_3(target_n_single) if target_n_single >= 2 else []

        # デフォルト（シードなし or 複数回出場）
        target_n = target_n_multi
        patterns = patterns_multi

    # 決勝結果があるか（bracket or ht）
    async with db.execute(
        """SELECT 1 FROM bracket_slot_ranks bsr
           JOIN bracket_groups bg ON bg.id=bsr.group_id
           JOIN bracket_rounds br ON br.id=bg.round_id
           WHERE br.tournament_id=? AND br.round_type IN ('final','third','revival') AND bsr.rank=1 LIMIT 1""",
        (tid,),
    ) as cur:
        has_final_result = (await cur.fetchone()) is not None

    if not has_final_result:
        async with db.execute(
            """SELECT 1 FROM ht_slot_ranks hsr
               JOIN ht_groups hg ON hg.id=hsr.group_id
               JOIN ht_rounds hr ON hr.id=hg.round_id
               WHERE hr.tournament_id=? AND hr.round_type='final' AND hsr.rank=1 LIMIT 1""",
            (tid,),
        ) as cur:
            has_final_result = (await cur.fetchone()) is not None

    # heat_roundrobin: 決勝進出者リストを生成（qual_heat_final=1 なら heat_finals 勝者、なければグループ上位）
    hr_heats_data_br = []
    if dict(t).get("qualifying_type") == "heat_roundrobin":
        qual_heat_final_br = bool(t_dict.get("qual_heat_final", 0))
        qual_heat_exclude_br = bool(t_dict.get("qual_heat_exclude", 0))
        async with db.execute(
            "SELECT DISTINCT round_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY round_no",
            (tid,),
        ) as cur:
            heat_nos_br = [r["round_no"] for r in await cur.fetchall()]
        seen_br: set = set()
        for rno in heat_nos_br:
            slots_br = []
            if qual_heat_final_br:
                # 優勝トーナメント勝者
                async with db.execute(
                    """SELECT hf.slot_no, hf.entry_id, r.name
                       FROM heat_finals hf
                       JOIN entries e ON e.id=hf.entry_id
                       JOIN racers r ON r.id=e.racer_id
                       WHERE hf.tournament_id=? AND hf.round_no=? AND hf.group_no=0
                         AND hf.final_type='heat' AND hf.winner_entry_id IS NOT NULL
                       ORDER BY hf.slot_no""",
                    (tid, rno),
                ) as cur:
                    heat_winners_br = [dict(r) for r in await cur.fetchall()]
                for i, hw in enumerate(heat_winners_br):
                    eid = hw["entry_id"]
                    if qual_heat_exclude_br and eid in seen_br:
                        continue
                    slots_br.append({"group_no": 0, "rank": i + 1, "name": hw["name"]})
                    if qual_heat_exclude_br:
                        seen_br.add(eid)
            else:
                # グループ上位
                from app.routers.qualifying import _calc_standings_group_round
                group_advance_br = int(t_dict.get("qual_group_advance", 1) or 1)
                async with db.execute(
                    "SELECT DISTINCT round_no, group_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY round_no, group_no",
                    (tid,),
                ) as cur:
                    rg_pairs_br = [(r["round_no"], r["group_no"]) for r in await cur.fetchall()]
                group_nos_br = sorted(set(gno for _, gno in rg_pairs_br))
                for gno in group_nos_br:
                    if (rno, gno) not in rg_pairs_br:
                        continue
                    st_br = await _calc_standings_group_round(tid, gno, rno, db)
                    picked = 0
                    for p in st_br:
                        if picked >= group_advance_br:
                            break
                        eid = p["entry_id"]
                        if qual_heat_exclude_br and eid in seen_br:
                            continue
                        slots_br.append({"group_no": gno, "rank": p["rank"], "name": p["name"]})
                        if qual_heat_exclude_br:
                            seen_br.add(eid)
                        picked += 1
            hr_heats_data_br.append({"heat_no": rno, "advanced": slots_br})

    # 裏トーナメント（ルーザーズブラケット）の描画用データ
    losers_groups_data = []
    for lr in losers_rounds:
        async with db.execute(
            "SELECT * FROM bracket_groups WHERE round_id=? ORDER BY group_no", (lr["id"],)
        ) as cur:
            lgroups = await cur.fetchall()
        for g in lgroups:
            slots, result, ranks = await _get_group_detail(g["id"], db)
            rd = dict(lr)
            rd["label"] = round_label(lr["round_no"], 0, "losers")
            losers_groups_data.append({
                "round": rd, "group": dict(g), "slots": slots,
                "result": result, "ranks": ranks,
            })

    async with db.execute("SELECT id, name, body FROM post_templates ORDER BY id") as cur:
        post_templates = [dict(r) for r in await cur.fetchall()]

    return templates.TemplateResponse("admin/bracket.html", {
        "request": request,
        "t": t,
        "finalists": finalists,
        "finalist_n": finalist_n,
        "target_n": target_n,
        "target_n_multi": target_n_multi,
        "target_n_single": target_n_single,
        "patterns_multi": patterns_multi,
        "patterns_single": patterns_single,
        "all_standings": all_standings,
        "rounds": rounds,
        "groups_data": groups_data,
        "losers_groups_data": losers_groups_data,
        "patterns": patterns,
        "total_rounds": total_rounds,
        "svg_data": svg_data,
        "seeded_ids": list(seeded_ids),
        "super_seeded_ids": list(super_seeded_ids),
        "tie_selection_needed": tie_selection_needed,
        "tie_candidates": tie_candidates,
        "needed_from_tie": needed_from_tie,
        "has_final_result": has_final_result,
        "post_templates": post_templates,
        "qualifying_incomplete": qualifying_incomplete,
        "qualifying_incomplete_msg": qualifying_incomplete_msg,
        "ht_per_heat_advanced": await _get_ht_per_heat_advanced(tid, db) if dict(t).get("qualifying_type") in HEAT_TOURNAMENT_TYPES else [],
        "ht_duplicates": ht_duplicates,
        "hr_heats_data": hr_heats_data_br,
        "ht_seed_keys": list(ht_seed_keys),
        "use_ht_seed": use_ht_seed,
        "ht_seeded_count": sum(1 for f in finalists if f.get("seeded")),
        "lottery": lottery,
    })


@router.post("/{tid}/bracket/resolve-duplicates")
async def bracket_resolve_duplicates(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """heat_tournament重複レーサーの扱いを一括決定する
    action_all = "multi": 複数回出場（シード解除）
    action_all = "single": 1回のみ出場（シード権付与）
    """
    form = await request.form()
    action = form.get("action_all", "multi")

    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url=f"/admin/tournaments/{tid}/bracket", status_code=303)

    ht_adv = await _get_ht_per_heat_advanced(tid, db)
    from collections import defaultdict
    racer_heat_map = defaultdict(list)
    for heat in ht_adv:
        for a in heat["advanced"]:
            racer_heat_map[a["entry_id"]].append(a)

    for entry_id, appearances in racer_heat_map.items():
        if len(appearances) <= 1:
            continue
        if action == "single":
            await db.execute(
                "UPDATE entries SET seeded=1 WHERE id=? AND tournament_id=?",
                (entry_id, tid),
            )
        else:
            # 複数回出場：シード解除
            await db.execute(
                "UPDATE entries SET seeded=0 WHERE id=? AND tournament_id=?",
                (entry_id, tid),
            )

    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}/bracket?dup_resolved=1", status_code=303)


@router.post("/{tid}/bracket/select-tie")
async def bracket_select_tie(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """同率選択後にパターン選択画面へ"""
    form = await request.form()
    selected_ids = [int(v) for k, v in form.multi_items() if k == "selected_entry_id"]
    # セッション代わりにクエリパラメータで渡す
    ids_str = ",".join(str(i) for i in selected_ids)
    return RedirectResponse(url=f"/admin/tournaments/{tid}/bracket/confirm-finalists?ids={ids_str}", status_code=303)


@router.get("/{tid}/bracket/confirm-finalists", response_class=HTMLResponse)
async def bracket_confirm_finalists(
    tid: int, request: Request,
    ids: str = QParam(""),
    db: aiosqlite.Connection = Depends(get_db),
):
    """同率選択後の確認→パターン選択画面"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()

    from app.routers.tournaments import calc_finalists as calc_n
    t_dict = dict(t)
    finalist_n = calc_n(t_dict.get("qualifying_type",""), t_dict) or 0
    all_standings = await _get_all_standings(tid, db)

    selected_ids = [int(i) for i in ids.split(",") if i.strip()]
    # 決勝枠 finalist_n の境界rankを基準に「確定済み（同率調整不要）」を抽出する。
    # finalist_n が実人数を超える/0以下のとき all_standings[finalist_n-1] は IndexError に
    # なるため、範囲内のときだけ境界rankを求め、それ以外は全員を確定扱いとする。
    if all_standings and 0 < finalist_n <= len(all_standings):
        border_rank = all_standings[finalist_n - 1]["rank"]
        confirmed_base = [r for r in all_standings if r["rank"] < border_rank]
    else:
        confirmed_base = list(all_standings)
    confirmed_base_ids = {r["entry_id"] for r in confirmed_base}
    extra = [r for r in all_standings if r["entry_id"] in selected_ids and r["entry_id"] not in confirmed_base_ids]
    finalists = confirmed_base + extra
    patterns = combinations_2_3(len(finalists))

    return templates.TemplateResponse("admin/bracket.html", {
        "request": request,
        "t": t,
        "finalists": finalists,
        "finalist_n": finalist_n,
        "target_n": target_n,
        "target_n_multi": target_n_multi,
        "target_n_single": target_n_single,
        "patterns_multi": patterns_multi,
        "patterns_single": patterns_single,
        "all_standings": all_standings,
        "rounds": [],
        "groups_data": [],
        "patterns": patterns,
        "total_rounds": 0,
        "tie_selection_needed": False,
        "tie_candidates": [],
        "needed_from_tie": 0,
        "confirmed_ids": ",".join(str(i) for i in selected_ids),
    })


# ── ラウンド1生成 ─────────────────────────────────────────
@router.post("/{tid}/bracket/group/{group_id}/set-rank")
async def bracket_set_rank(
    tid: int,
    group_id: int,
    slot_id: int = Form(...),
    rank: int = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
):
    """決勝の順位を設定（1位=winner）"""
    # 同じ順位の既存設定をクリア
    await db.execute(
        "DELETE FROM bracket_slot_ranks WHERE group_id=? AND rank=?",
        (group_id, rank),
    )
    # 同じスロットの既存順位をクリア（再押しでリセット）
    async with db.execute(
        "SELECT id FROM bracket_slot_ranks WHERE group_id=? AND slot_id=? AND rank=?",
        (group_id, slot_id, rank),
    ) as cur:
        existing = await cur.fetchone()

    if existing:
        # 同じ順位を再押し→リセット（既にDELETEされているので何もしない）
        pass
    else:
        await db.execute(
            "INSERT INTO bracket_slot_ranks (group_id, slot_id, rank) VALUES (?,?,?)",
            (group_id, slot_id, rank),
        )

    # 1位が設定されたらwinner_slot_idも設定
    async with db.execute(
        "SELECT slot_id FROM bracket_slot_ranks WHERE group_id=? AND rank=1",
        (group_id,),
    ) as cur:
        winner_row = await cur.fetchone()

    if winner_row:
        # bracket_resultsを更新
        async with db.execute(
            "SELECT id FROM bracket_results WHERE group_id=?", (group_id,)
        ) as cur:
            existing_result = await cur.fetchone()
        if existing_result:
            await db.execute(
                "UPDATE bracket_results SET winner_slot_id=? WHERE group_id=?",
                (winner_row["slot_id"], group_id),
            )
        else:
            await db.execute(
                "INSERT INTO bracket_results (group_id, winner_slot_id) VALUES (?,?)",
                (group_id, winner_row["slot_id"]),
            )
        await db.commit()
        # 次ラウンド処理
        async with db.execute(
            "SELECT bg.round_id FROM bracket_groups bg WHERE bg.id=?", (group_id,)
        ) as cur:
            g = await cur.fetchone()
        if g:
            await _prefill_next_round(tid, g["round_id"],
                (await (await db.execute("SELECT round_no FROM bracket_rounds WHERE id=?", (g["round_id"],))).fetchone())["round_no"],
                db)
            advanced = await _try_advance_round(tid, g["round_id"], db)
            # 裏トーナメント（有効時のみ）：敗者ドロップイン同期＋復活差し込み
            await _sync_losers_bracket(tid, db)
            reviver_inserted = await _try_insert_reviver(tid, db)
            from app.routers.tournaments import _is_result_finalized
            finalized = await _is_result_finalized(tid, db)
            return JSONResponse({"ok": True, "advanced": advanced,
                                 "reviver_inserted": bool(reviver_inserted),
                                 "finalized": bool(finalized)})
    else:
        await db.commit()

    from app.routers.tournaments import _is_result_finalized
    finalized = await _is_result_finalized(tid, db)
    return JSONResponse({"ok": True, "advanced": False, "finalized": bool(finalized)})


@router.post("/{tid}/bracket/set-seeded/{entry_id}")
async def set_seeded(
    tid: int,
    entry_id: int,
    seeded: int = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
):
    """シード権のON/OFF切り替え"""
    await db.execute(
        "UPDATE entries SET seeded=? WHERE id=? AND tournament_id=?",
        (seeded, entry_id, tid),
    )
    await db.commit()
    return JSONResponse({"ok": True, "seeded": seeded})


@router.post("/{tid}/bracket/set-ht-seeded/{entry_id}/{heat_no}")
async def set_ht_seeded(
    tid: int,
    entry_id: int,
    heat_no: int,
    seeded: int = Form(...),
    db: aiosqlite.Connection = Depends(get_db),
):
    """ヒートトーナメント複数回出場時の枠単位シード切り替え"""
    if seeded:
        await db.execute(
            """INSERT INTO ht_finalist_seeds (tournament_id, entry_id, heat_no, seeded)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(tournament_id, entry_id, heat_no) DO UPDATE SET seeded=1""",
            (tid, entry_id, heat_no),
        )
    else:
        await db.execute(
            "DELETE FROM ht_finalist_seeds WHERE tournament_id=? AND entry_id=? AND heat_no=?",
            (tid, entry_id, heat_no),
        )
        # entries.seeded 由来（旧データ/別経路）のシードが残ると★が消えないため、合わせて解除する
        await db.execute(
            "UPDATE entries SET seeded=0 WHERE id=? AND tournament_id=?",
            (entry_id, tid),
        )
    await db.commit()
    return JSONResponse({"ok": True, "seeded": seeded})


@router.post("/{tid}/bracket/group/{group_id}/reset-final")
async def bracket_reset_final(
    tid: int,
    group_id: int,
    db: aiosqlite.Connection = Depends(get_db),
):
    """決勝（またはany）グループの結果を取り消す"""
    # このグループのラウンド情報を取得
    async with db.execute(
        """SELECT br.round_type, br.round_no, br.id as round_id
           FROM bracket_groups bg
           JOIN bracket_rounds br ON br.id=bg.round_id
           WHERE bg.id=?""",
        (group_id,),
    ) as cur:
        rnd = await cur.fetchone()

    # 結果・順位を削除
    await db.execute("DELETE FROM bracket_slot_ranks WHERE group_id=?", (group_id,))
    await db.execute("DELETE FROM bracket_results WHERE group_id=?", (group_id,))

    # 次ラウンドのスロットに流し込まれていた自分の勝者も消す（次ラウンドをリセット）
    if rnd:
        # このグループから流れ込んでいる勝者を次ラウンドのスロットから削除
        async with db.execute(
            """SELECT id FROM bracket_rounds WHERE tournament_id=? AND round_no=?
               AND round_type NOT IN ('third','revival')""",
            (tid, rnd["round_no"] + 1),
        ) as cur:
            next_rnd = await cur.fetchone()

        if next_rnd:
            # このグループの全スロットのentry_idを取得
            async with db.execute(
                "SELECT entry_id FROM bracket_slots WHERE group_id=? AND entry_id IS NOT NULL",
                (group_id,),
            ) as cur:
                my_entry_ids = [r["entry_id"] for r in await cur.fetchall()]

            if my_entry_ids:
                ph = ",".join("?" * len(my_entry_ids))
                # 次ラウンドのスロットから該当entry_idをNULLに
                await db.execute(
                    f"""UPDATE bracket_slots SET entry_id=NULL
                        WHERE group_id IN (
                            SELECT id FROM bracket_groups WHERE round_id=?
                        ) AND entry_id IN ({ph})""",
                    [next_rnd["id"]] + my_entry_ids,
                )
                # 次ラウンドの結果・順位も削除（無効になるため）
                async with db.execute(
                    "SELECT id FROM bracket_groups WHERE round_id=?", (next_rnd["id"],)
                ) as cur:
                    next_groups = [r["id"] for r in await cur.fetchall()]
                for ng_id in next_groups:
                    await db.execute("DELETE FROM bracket_results WHERE group_id=?", (ng_id,))
                    await db.execute("DELETE FROM bracket_slot_ranks WHERE group_id=?", (ng_id,))

    await db.commit()

    # 上流(表normal)の結果を取り消した場合、その敗者で構成される裏トーナメントは
    # 無効になるため作り直す（裏Rk以降を撤去→現在の表から再同期。裏R1..は表R1..が
    # 不変なら残す）。
    if rnd and rnd["round_type"] == "normal":
        lb_on, _ = await _lb_enabled(tid, db)
        if lb_on:
            from_k = await _normal_round_index(tid, rnd["round_no"], db)
            await _teardown_losers(tid, db, from_k)
            await _sync_losers_bracket(tid, db)
            await _try_insert_reviver(tid, db)

    return JSONResponse({"ok": True})


async def _bake_bracket_advance_links(tid: int, db: aiosqlite.Connection):
    """【固定リンク方式・v5.7+】生成直後に「各グループの勝者が進む先のスロット」を
    bracket_groups.advance_to_slot_id へ確定保存（焼き付け）する。

    以後の進行はこのリンクを辿るだけとなり、結果の入力順・取消・再入力に
    かかわらず組み合わせ（接続）は生成時から一切変化しない。

    リンク対象は normal / final ラウンド間のみ。
    third（3位決定戦）・revival（敗者復活戦）・losers（裏トーナメント）は
    「裏トーナメント勝者の決勝参加」「敗者復活戦勝者の決勝参加」という
    唯一許可された例外経路として、従来の動的処理のまま残す。

    対応規則（画面の接続線描画 drawBracketConnectors と同一）:
      現ラウンドのグループ（group_no順）の勝者
        → 次ラウンドの「勝者受け皿スロット」
          （entry_id IS NULL かつ seed_reserved=0、group_no・slot_no順）
          の先頭から順に1対1対応。
      受け皿がグループ数より多い場合、余った末尾枠はリンクしない
      （敗者復活戦勝者・裏トーナメント復活者用の枠として空けておく）。
    """
    async with db.execute(
        "SELECT id, round_no FROM bracket_rounds "
        "WHERE tournament_id=? AND round_type IN ('normal','final') ORDER BY round_no",
        (tid,),
    ) as cur:
        rounds = [dict(r) for r in await cur.fetchall()]
    for i in range(len(rounds) - 1):
        cur_rid = rounds[i]["id"]
        next_rid = rounds[i + 1]["id"]
        async with db.execute(
            "SELECT id FROM bracket_groups WHERE round_id=? ORDER BY group_no",
            (cur_rid,),
        ) as cur:
            cur_group_ids = [r["id"] for r in await cur.fetchall()]
        async with db.execute(
            """SELECT bs.id FROM bracket_slots bs
               JOIN bracket_groups bg ON bg.id=bs.group_id
               WHERE bg.round_id=? AND bs.entry_id IS NULL
                 AND COALESCE(bs.seed_reserved,0)=0
               ORDER BY bg.group_no, bs.slot_no""",
            (next_rid,),
        ) as cur:
            dest_slot_ids = [r["id"] for r in await cur.fetchall()]
        for gi, gid in enumerate(cur_group_ids):
            if gi >= len(dest_slot_ids):
                break
            await db.execute(
                "UPDATE bracket_groups SET advance_to_slot_id=? WHERE id=?",
                (dest_slot_ids[gi], gid),
            )
    await db.commit()


async def _ensure_advance_links_baked(tid: int, db: aiosqlite.Connection):
    """【後方互換・自己修復】固定リンク(advance_to_slot_id)が焼き付けられていない
    既存トーナメント（v5.7の焼き付け導入より前に生成された等）に対し、描画時に
    リンクを補完する。

    焼き付けの規則は生成時と同じで「次ラウンドの空き受け皿スロット(entry_id NULL
    かつ seed_reserved=0)」を辿るため、勝者が受け皿に入った後（＝結果入力後）に
    実行すると受け皿が減って誤ったリンクになる。したがって、

      ・normal/final ラウンドのいずれかのグループで advance_to_slot_id が未設定、かつ
      ・normal/final ラウンドに結果(bracket_results)が1件も無い（＝まだ勝ち上がりゼロ）

    の両方を満たすとき「のみ」安全に焼き付けを実行する。
    既に焼き付け済み、または進行中で結果があるトーナメントには一切触れない。
    """
    async with db.execute(
        """SELECT COUNT(*) AS n FROM bracket_groups bg
           JOIN bracket_rounds br ON br.id=bg.round_id
           WHERE br.tournament_id=? AND br.round_type IN ('normal','final')
             AND bg.advance_to_slot_id IS NULL""",
        (tid,),
    ) as cur:
        missing = (await cur.fetchone())["n"]
    if not missing:
        return  # 全て焼き付け済み → 何もしない
    async with db.execute(
        """SELECT COUNT(*) AS n FROM bracket_results r
           JOIN bracket_groups bg ON bg.id=r.group_id
           JOIN bracket_rounds br ON br.id=bg.round_id
           WHERE br.tournament_id=? AND br.round_type IN ('normal','final')""",
        (tid,),
    ) as cur:
        results = (await cur.fetchone())["n"]
    if results:
        return  # 進行中（結果あり）→ 受け皿が埋まっており安全に再構築できないため触れない
    await _bake_bracket_advance_links(tid, db)


@router.post("/{tid}/bracket/generate")
async def bracket_generate(
    tid: int,
    pattern: str = Form(...),
    all_patterns: str = Form(""),
    confirmed_ids: str = Form(""),
    bracket_mode: str = Form("third_place"),
    dup_resolved: str = Form(""),
    dup_mode: str = Form("multi"),
    losers_bracket: str = Form(""),
    revival_target: str = Form("final"),
    placement_method: str = Form("auto"),
    db: aiosqlite.Connection = Depends(get_db),
):
    # bracket_modeをDBに保存
    await db.execute(
        "UPDATE tournaments SET bracket_mode=? WHERE id=?",
        (bracket_mode, tid),
    )
    # 配置方法を正規化して保存（auto=自動配置 / lottery=くじ引き手動配置）
    placement_method = "lottery" if str(placement_method) == "lottery" else "auto"
    await db.execute(
        "UPDATE tournaments SET placement_method=? WHERE id=?",
        (placement_method, tid),
    )
    # 裏トーナメント設定を保存（復活先ラウンド番号は生成後に解決するためここではNULL）
    lb_on = 1 if str(losers_bracket) in ("1", "on", "true", "True") else 0
    await db.execute(
        "UPDATE tournaments SET losers_bracket=?, revival_target_round=NULL WHERE id=?",
        (lb_on, tid),
    )
    await _delete_bracket(tid, db)

    seeded_ids_gen, super_seeded_ids_gen = await _get_seeded_ids(tid, db)
    async with db.execute("SELECT qualifying_type FROM tournaments WHERE id=?", (tid,)) as cur:
        qt_row = await cur.fetchone()
    qt_gen = qt_row["qualifying_type"] if qt_row else ""

    if qt_gen in HEAT_TOURNAMENT_TYPES:
        # ヒートトーナメント：ht_per_heat_advancedをフラット化（重複込み）
        ht_adv_gen = await _get_ht_per_heat_advanced(tid, db)
        finalists = []
        for heat in ht_adv_gen:
            for a in heat["advanced"]:
                finalists.append({"entry_id": a["entry_id"], "name": a["name"], "heat_no": heat["heat_no"], "seeded": 1 if a["entry_id"] in seeded_ids_gen else 0})
        # 複数回出場（重複あり）の場合は枠単位シードに切り替え
        entry_id_counts_gen = {}
        for f in finalists:
            entry_id_counts_gen[f["entry_id"]] = entry_id_counts_gen.get(f["entry_id"], 0) + 1
        if any(v > 1 for v in entry_id_counts_gen.values()):
            ht_seed_keys_gen = await _get_ht_finalist_seed_keys(tid, db)
            for f in finalists:
                # 枠単位(ht_seed_keys) OR entry_id単位(seeded_ids)のどちらかでシード判定
                f["seeded"] = 2 if f["entry_id"] in super_seeded_ids_gen else (1 if (f["entry_id"], f.get("heat_no", 0)) in ht_seed_keys_gen or f["entry_id"] in seeded_ids_gen else 0)
        # ht_per_heat_advancedが空の場合はadvanced_entriesにフォールバック
        if not finalists:
            adv_fb = await _get_advanced_entries(tid, db)
            for f in adv_fb:
                f["seeded"] = 2 if f["entry_id"] in super_seeded_ids_gen else (1 if f["entry_id"] in seeded_ids_gen else 0)
            finalists = adv_fb
    else:
        adv_ordered = await _get_advanced_entries(tid, db)
        if adv_ordered:
            for f in adv_ordered:
                f["seeded"] = 2 if f["entry_id"] in super_seeded_ids_gen else (1 if f["entry_id"] in seeded_ids_gen else 0)
            finalists = adv_ordered
        elif qt_gen == "none":
            async with db.execute(
                """SELECT e.id as entry_id, r.name, e.seeded
                   FROM entries e JOIN racers r ON r.id=e.racer_id
                   WHERE e.tournament_id=? AND e.status='active' ORDER BY e.entry_order""",
                (tid,),
            ) as cur:
                finalists = [dict(r) for r in await cur.fetchall()]
            for f in finalists:
                f["seeded"] = 2 if f["entry_id"] in super_seeded_ids_gen else (1 if f["entry_id"] in seeded_ids_gen else 0)
        elif confirmed_ids.strip():
            id_list = [int(i) for i in confirmed_ids.split(",") if i.strip()]
            finalists = await _get_finalists(tid, db, selected_ids=id_list)
            for f in finalists:
                f["seeded"] = 2 if f["entry_id"] in super_seeded_ids_gen else (1 if f["entry_id"] in seeded_ids_gen else 0)
        else:
            from app.routers.tournaments import calc_finalists as calc_n
            async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
                t = await cur.fetchone()
            t_dict = dict(t) if t else {}
            finalist_n = calc_n(t_dict.get("qualifying_type", ""), t_dict) or 0
            all_standings = await _get_all_standings(tid, db)
            finalists = all_standings[:finalist_n] if finalist_n else all_standings
            for f in finalists:
                f["seeded"] = 2 if f["entry_id"] in super_seeded_ids_gen else (1 if f["entry_id"] in seeded_ids_gen else 0)

    group_sizes = [int(x) for x in pattern.split(",")]
    seeded_players       = [f for f in finalists if f.get("seeded") == 1]
    super_seeded_players = [f for f in finalists if f.get("seeded") == 2]
    all_seeded_players   = seeded_players + super_seeded_players
    seeded_entry_ids = {f["entry_id"] for f in all_seeded_players}
    # dup_mode: "single"=1回のみ出場（重複除外）, "multi"=複数回出場（重複残す）
    if dup_mode == "single" and seeded_entry_ids:
        non_seeded = [f for f in finalists if f.get("seeded") == 0 and f["entry_id"] not in seeded_entry_ids]
    else:
        non_seeded = [f for f in finalists if f.get("seeded") == 0]

    import logging
    suffix = "?dup_resolved=1" if dup_resolved else ""

    # ── 検証1: グループサイズは2または3のみ ──
    if any(sz < 2 or sz > 3 for sz in group_sizes):
        logging.warning(f"bracket_generate: invalid group sizes {group_sizes}")
        return RedirectResponse(url=f"/admin/tournaments/{tid}/bracket{suffix}", status_code=303)

    # ── 検証2: R1合計人数 = 非シード数（シードはR2から参加）──
    if sum(group_sizes) != len(non_seeded):
        logging.warning(f"bracket_generate mismatch: pattern={pattern} sum={sum(group_sizes)} non_seeded={len(non_seeded)} seeded={len(seeded_players)} super_seeded={len(super_seeded_players)} total={len(finalists)}")
        return RedirectResponse(url=f"/admin/tournaments/{tid}/bracket{suffix}", status_code=303)

    r1_groups = len(group_sizes)
    r2_total = r1_groups + len(seeded_players)

    # 全ラウンドパターンを計算
    # all_patternsが渡されていれば（JS側でユーザーが選択）それを使う
    # 形式: "3,3,3|3,2|2" (R1|R2|...|準決勝) ※R1は既にgroup_sizesで受け取り済み
    all_round_pats = [(1, group_sizes)]  # (round_no, pattern)
    n_seeds = len(seeded_players)

    user_pats = []  # R2以降のユーザー選択パターン
    if all_patterns.strip():
        raw_pats = all_patterns.strip().split("|")
        # all_patternsにはR1も含む場合があるので、R2以降を使う
        for i, rp in enumerate(raw_pats[1:], 2):  # R2から
            try:
                pat = [int(x) for x in rp.split(",") if x.strip()]
                if pat:
                    # グループサイズ検証（2〜3のみ）
                    if any(sz < 2 or sz > 3 for sz in pat):
                        logging.warning(f"user_pats: invalid group sizes {pat} in round {i}")
                        continue
                    # R2のシード数チェックは行わない（1グループに複数シードも許容）
                    user_pats.append((i, pat))
            except ValueError:
                pass

    def _pick_r2_pat(r2_total: int, n_seeds: int) -> list[int]:
        """R2パターンを選択。グループ数 >= n_seeds を満たす最小グループ数パターン優先。"""
        pats = combinations_2_3(r2_total)
        # グループ数 >= n_seeds のパターンのみ
        valid = [p for p in pats if len(p) >= n_seeds]
        if not valid:
            # 満たせない場合は全体から最大グループ数のパターン
            valid = sorted(pats, key=lambda p: -len(p))
        if not valid:
            return []
        # グループ数が多いほど各グループが小さく済む → まず最少グループ数で条件を満たすものを選択
        valid_sorted = sorted(valid, key=lambda p: (len(p), p))
        return valid_sorted[0]

    if user_pats:
        # ユーザーが選択したパターンを使用
        for rno, pat in user_pats:
            all_round_pats.append((rno, pat))
        # 最終パターンが準決勝→決勝スロットを追加
        last_rno, last_pat = all_round_pats[-1]
        last_n = len(last_pat)  # 準決勝グループ数 = 勝者数
        # スーパーシードはR3以降から参加し勝ち上がる（既に各ラウンドに着席済み）。
        # 決勝枠数には加算しない（加算すると二重計上で決勝が過大スロットになる）。
        final_n = last_n  # 決勝参加人数 = 準決勝勝者数（= 準決勝グループ数）
        final_rno = last_rno + 1
        if bracket_mode == "semi_3group":
            all_round_pats.append((final_rno, [3]))
        elif bracket_mode == "revival":
            # revival: 準決勝勝者 + 敗者復活戦勝者1名
            #   準決勝2グループ → 勝者2名 + 復活戦勝者1名 = 決勝3名
            #   準決勝3グループ → 勝者3名（復活戦勝者なし） = 決勝3名
            revival_n = final_n + (1 if last_n == 2 else 0)
            all_round_pats.append((final_rno, [revival_n] if revival_n >= 2 else [3]))
        else:
            # third_place / none: 準決勝勝者で決勝
            all_round_pats.append((final_rno, [final_n] if final_n >= 2 else [last_n]))
    elif bracket_mode == "semi_3group":
        # ①: 準決勝3グループ→決勝3名（自動計算）
        n = r2_total if r2_total > 1 else r1_groups
        rno = 2
        first_r2 = True
        while n > 1:
            pats = combinations_2_3(n)
            if not pats:
                break
            if first_r2:
                pat = _pick_r2_pat(n, n_seeds)
                first_r2 = False
            else:
                three_group_pats = [p for p in pats if len(p) == 3]
                two_group_pats   = [p for p in pats if len(p) == 2]
                one_group_pats   = [p for p in pats if len(p) == 1]
                if three_group_pats:
                    pat = three_group_pats[0]
                    all_round_pats.append((rno, pat))
                    rno += 1
                    all_round_pats.append((rno, [3]))
                    break
                elif one_group_pats:
                    all_round_pats.append((rno, one_group_pats[0]))
                    break
                else:
                    pat = two_group_pats[0] if two_group_pats else pats[0]
            if not pat:
                break
            all_round_pats.append((rno, pat))
            n = len(pat)
            rno += 1
            if len(pat) == 1:
                break
    else:
        # ② third_place / revival: 自動計算
        n = r2_total if r2_total > 1 else r1_groups
        rno = 2
        first_r2 = True
        while n > 1:
            pats = combinations_2_3(n)
            if not pats:
                break
            if first_r2:
                pat = _pick_r2_pat(n, n_seeds)
                first_r2 = False
            else:
                pat = pats[0]
            if not pat:
                break
            all_round_pats.append((rno, pat))
            n = len(pat)
            rno += 1
            if len(pat) == 1:
                break

    total_rounds = len(all_round_pats)

    def get_rtype_label(rno, pat):
        is_final = len(pat) == 1 and rno == all_round_pats[-1][0]
        if is_final:
            return "final", "決勝"
        # semi_3groupの場合、決勝直前の3グループは準決勝
        if bracket_mode == "semi_3group":
            remaining = total_rounds - rno
            if remaining == 1:
                return "normal", "準決勝"
            elif remaining == 2:
                return "normal", "準々決勝"
            else:
                return "normal", f"ラウンド{rno}"
        remaining = total_rounds - rno
        if remaining == 1:
            return "normal", "準決勝"
        elif remaining == 2:
            return "normal", "準々決勝"
        elif remaining == 3:
            return "normal", "準々々決勝"
        else:
            return "normal", f"ラウンド{rno}"

    # ── R2グループ順序の最適化（スーパーシード公平配置）──
    # スーパーシードはR3の両端グループに配置される（_choose_super_seed_groups の仕様）。
    # これに対応してR2グループ（=R3に流入するラウンド）の順序を
    # 「大きいグループが両端・小さいグループが中央」に並べ替える。
    # これにより連番マッピングで自動的に:
    #   R2G0(大) → R3G0(スーパーシード, cap=1)
    #   R2G1,G2(小) → R3G1(中間, cap=2)
    #   R2G3(大) → R3G2(スーパーシード, cap=1)
    # となり、ビジュアル・実際の流入ともに公平になる。
    if super_seeded_players:
        ss_rno = 3  # スーパーシードはR3に配置（bracket_generate の固定仕様）
        feeder_rno = ss_rno - 1  # R3に流入するのはR2
        def _reorder_ends_middle(sizes: list) -> list:
            """両端に大きいグループ、中央に小さいグループを配置するよう並べ替え"""
            sorted_desc = sorted(sizes, reverse=True)
            n = len(sorted_desc)
            result = [0] * n
            lo, hi = 0, n - 1
            for i, val in enumerate(sorted_desc):
                if i % 2 == 0:   # even: 左(lo)から埋める
                    result[lo] = val; lo += 1
                else:             # odd: 右(hi)から埋める
                    result[hi] = val; hi -= 1
            return result
        all_round_pats = [
            (rno, _reorder_ends_middle(pat) if rno == feeder_rno and len(pat) >= 2 else pat)
            for rno, pat in all_round_pats
        ]

    round_ids = {}
    for rno, pat in all_round_pats:
        rtype, _ = get_rtype_label(rno, pat)
        # 敗者復活戦モードで決勝が2スロットの場合→3スロットに拡張（復活戦勝者枠）
        actual_pat = list(pat)
        if bracket_mode == "revival" and rtype == "final" and actual_pat == [2]:
            actual_pat = [3]
        await db.execute(
            "INSERT INTO bracket_rounds (tournament_id, round_no, round_type) VALUES (?,?,?)",
            (tid, rno, rtype),
        )
        async with db.execute("SELECT last_insert_rowid() as id") as cur:
            rnd_id = (await cur.fetchone())["id"]
        round_ids[rno] = rnd_id
        for gno, sz in enumerate(actual_pat, 1):
            await db.execute("INSERT INTO bracket_groups (round_id, group_no) VALUES (?,?)", (rnd_id, gno))
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                gid = (await cur.fetchone())["id"]
            for sno in range(1, sz + 1):
                await db.execute(
                    "INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)",
                    (gid, sno, None),
                )

    # ── くじ引きモード：自動配置(STEP2/STEP3)を行わず「新規登場枠」へlottery_noを採番 ──
    # 区分 = 登場ラウンド： R1=非シード全枠 / R2=シードが座る席のみ / R3=スーパーシードが座る席のみ
    # R2/R3の「勝者が勝ち上がってくる席」には採番しない（くじ対象外）。
    if placement_method == "lottery":
        import random as _rnd_lot  # noqa

        async def _slots_in_round_ordered(rid):
            async with db.execute(
                """SELECT bs.id
                   FROM bracket_slots bs
                   JOIN bracket_groups bg ON bg.id=bs.group_id
                   WHERE bg.round_id=?
                   ORDER BY bg.group_no, bs.slot_no""",
                (rid,),
            ) as cur:
                return [r["id"] for r in await cur.fetchall()]

        async def _group_ids(rid):
            async with db.execute(
                "SELECT id as gid FROM bracket_groups WHERE round_id=? ORDER BY group_no",
                (rid,),
            ) as cur:
                return [r["gid"] for r in await cur.fetchall()]

        async def _tail_empty_slot(gid):
            # シードが座る席＝グループ末尾の空きスロット（自動配置STEP3と同じ規則）
            async with db.execute(
                "SELECT id FROM bracket_slots WHERE group_id=? AND lottery_no IS NULL "
                "ORDER BY slot_no DESC LIMIT 1",
                (gid,),
            ) as cur:
                row = await cur.fetchone()
            return row["id"] if row else None

        # R1：非シード全員分＝R1の全スロットが対象
        if 1 in round_ids:
            r1_slots = await _slots_in_round_ordered(round_ids[1])
            for i, sid in enumerate(r1_slots, 1):
                await db.execute(
                    "UPDATE bracket_slots SET lottery_no=?, entry_id=NULL, seed_reserved=1 WHERE id=?",
                    (i, sid),
                )

        # R2：シードが座る席のみ（自動配置と同じグループ選択・末尾スロット）
        seed_r2_group_indices = []
        if seeded_players and 2 in round_ids:
            r2_groups_db = await _group_ids(round_ids[2])
            n_r2g = len(r2_groups_db)
            seed_group_indices = _spread_indices(n_seeds, n_r2g)
            seed_r2_group_indices = list(seed_group_indices)
            no = 0
            for si in range(len(seeded_players)):
                gi = seed_group_indices[si] if si < len(seed_group_indices) else 0
                gid = r2_groups_db[gi]
                sid = await _tail_empty_slot(gid)
                if sid:
                    no += 1
                    await db.execute(
                        "UPDATE bracket_slots SET lottery_no=?, entry_id=NULL, seed_reserved=1 WHERE id=?",
                        (no, sid),
                    )

        # R3：スーパーシードが座る席のみ
        if super_seeded_players and 3 in round_ids:
            r3_groups_db = await _group_ids(round_ids[3])
            n_r3g = len(r3_groups_db)
            n_super = len(super_seeded_players)
            r3_sizes = []
            for gid in r3_groups_db:
                async with db.execute(
                    "SELECT COUNT(*) AS c FROM bracket_slots WHERE group_id=?", (gid,)
                ) as cur:
                    r3_sizes.append((await cur.fetchone())["c"])
            r4_sizes = []
            if 4 in round_ids:
                for gid in await _group_ids(round_ids[4]):
                    async with db.execute(
                        "SELECT COUNT(*) AS c FROM bracket_slots WHERE group_id=?", (gid,)
                    ) as cur:
                        r4_sizes.append((await cur.fetchone())["c"])
            super_group_indices = _choose_super_seed_groups(
                n_super, r3_sizes, r4_sizes, seed_r2_group_indices
            )
            if not super_group_indices:
                super_group_indices = _spread_indices(n_super, n_r3g)
            no = 0
            for si in range(n_super):
                gi = super_group_indices[si] if si < len(super_group_indices) else 0
                gid = r3_groups_db[gi]
                sid = await _tail_empty_slot(gid)
                if sid:
                    no += 1
                    await db.execute(
                        "UPDATE bracket_slots SET lottery_no=?, entry_id=NULL, seed_reserved=1 WHERE id=?",
                        (no, sid),
                    )

        await db.execute(
            "UPDATE tournaments SET lottery_pending=1 WHERE id=?", (tid,)
        )
        await db.commit()
        # 【固定リンク焼き付け】くじ引きモードでもトーナメントの木構造は
        # この時点で確定しているため、勝ち上がりリンクをここで焼き付ける。
        # （seed_reserved=1 の席は受け皿から除外されるため lottery でも正しく対応する）
        await _bake_bracket_advance_links(tid, db)
        suffix = "?dup_resolved=1" if dup_resolved else ""
        return RedirectResponse(
            url=f"/admin/tournaments/{tid}/bracket{suffix}", status_code=303
        )

    # ── STEP2: R1にレーサーを配置 ──（lottery時は上でreturn済み。以下は自動配置）
    import random as _random
    non_seeded_shuffled = non_seeded[:]
    _random.shuffle(non_seeded_shuffled)
    assigned = _seed_assign(non_seeded_shuffled, group_sizes)

    async with db.execute(
        "SELECT id as gid FROM bracket_groups WHERE round_id=? ORDER BY group_no",
        (round_ids[1],),
    ) as cur:
        r1_groups_db = [r["gid"] for r in await cur.fetchall()]

    for g_idx, members in enumerate(assigned):
        gid = r1_groups_db[g_idx]
        async with db.execute(
            "SELECT id FROM bracket_slots WHERE group_id=? ORDER BY slot_no", (gid,)
        ) as cur:
            slot_ids = [r["id"] for r in await cur.fetchall()]
        for s_idx, finalist in enumerate(members):
            if finalist and s_idx < len(slot_ids):
                await db.execute(
                    "UPDATE bracket_slots SET entry_id=? WHERE id=?",
                    (finalist["entry_id"], slot_ids[s_idx]),
                )

    # ── STEP3: R2にシード選手を配置 ──
    # 原則: 各グループに最大1シード（検証3で n_r2g >= n_seeds を保証済み）
    # グループ数 > シード数の場合 → シードなしのグループが出ることは正常
    seed_r2_group_indices: list[int] = []  # スーパーシード回避用に保持
    if seeded_players and 2 in round_ids:
        async with db.execute(
            "SELECT id as gid FROM bracket_groups WHERE round_id=? ORDER BY group_no",
            (round_ids[2],),
        ) as cur:
            r2_groups_db = [r["gid"] for r in await cur.fetchall()]

        n_r2g = len(r2_groups_db)
        # シードを最大間隔で分散（各グループに最大1シード）。枠（どのグループに入れるか）は固定。
        seed_group_indices = _spread_indices(n_seeds, n_r2g)
        seed_r2_group_indices = list(seed_group_indices)

        # 枠はそのままに、どのシード選手がどの枠へ入るかを毎回ランダムにする
        seeded_players_rand = seeded_players[:]
        _random.shuffle(seeded_players_rand)
        for si, seeded_player in enumerate(seeded_players_rand):
            gi = seed_group_indices[si] if si < len(seed_group_indices) else 0
            gid = r2_groups_db[gi]
            # 空きスロットの末尾に配置（先頭スロットをR1勝者用に残す）
            async with db.execute(
                "SELECT id FROM bracket_slots WHERE group_id=? AND entry_id IS NULL ORDER BY slot_no DESC LIMIT 1",
                (gid,),
            ) as cur:
                slot = await cur.fetchone()
            if slot:
                await db.execute(
                    "UPDATE bracket_slots SET entry_id=? WHERE id=?",
                    (seeded_player["entry_id"], slot["id"]),
                )

    # R3へスーパーシード（seeded=2）を配置
    if super_seeded_players and 3 in round_ids:
        async with db.execute(
            "SELECT id as gid FROM bracket_groups WHERE round_id=? ORDER BY group_no",
            (round_ids[3],),
        ) as cur:
            r3_groups_db = [r["gid"] for r in await cur.fetchall()]

        n_r3g = len(r3_groups_db)
        n_super = len(super_seeded_players)

        # R3・R4のグループサイズを取得（合流トポロジ算出用）
        r3_sizes = []
        for gid in r3_groups_db:
            async with db.execute(
                "SELECT COUNT(*) AS c FROM bracket_slots WHERE group_id=?", (gid,)
            ) as cur:
                r3_sizes.append((await cur.fetchone())["c"])
        r4_sizes = []
        if 4 in round_ids:
            async with db.execute(
                "SELECT id AS gid FROM bracket_groups WHERE round_id=? ORDER BY group_no",
                (round_ids[4],),
            ) as cur:
                r4_groups_db = [r["gid"] for r in await cur.fetchall()]
            for gid in r4_groups_db:
                async with db.execute(
                    "SELECT COUNT(*) AS c FROM bracket_slots WHERE group_id=?", (gid,)
                ) as cur:
                    r4_sizes.append((await cur.fetchone())["c"])

        # スーパーシードが登場するR3と、その次のR4の両ラウンドで
        # シード/スーパーシードと対戦しないようR3グループを選ぶ（不可ならフォールバック）
        super_group_indices = _choose_super_seed_groups(
            n_super, r3_sizes, r4_sizes, seed_r2_group_indices
        )
        if not super_group_indices:
            super_group_indices = _spread_indices(n_super, n_r3g)

        # 枠（回避ロジックで選ばれたグループ）はそのままに、
        # どのスーパーシード選手がどの枠へ入るかを毎回ランダムにする
        super_seeded_players_rand = super_seeded_players[:]
        _random.shuffle(super_seeded_players_rand)
        for si, sp in enumerate(super_seeded_players_rand):
            gi = super_group_indices[si] if si < len(super_group_indices) else 0
            gid = r3_groups_db[gi]
            async with db.execute(
                "SELECT id FROM bracket_slots WHERE group_id=? AND entry_id IS NULL ORDER BY slot_no DESC LIMIT 1",
                (gid,),
            ) as cur:
                slot = await cur.fetchone()
            if slot:
                await db.execute(
                    "UPDATE bracket_slots SET entry_id=? WHERE id=?",
                    (sp["entry_id"], slot["id"]),
                )

    await db.commit()
    # 【固定リンク焼き付け】シード/スーパーシード配置まで完了した時点で
    # 木構造が確定するため、勝ち上がりリンクをここで焼き付ける。
    # 以後、組み合わせは結果入力・取消によって一切変化しない。
    await _bake_bracket_advance_links(tid, db)
    # 裏トーナメント（案B）: 復活先は常に決勝（最終ラウンド）。
    # 準決勝の勝者が2名（=準決勝が2グループ）のときだけ裏を許可する。
    # それ以外（3グループ等）は決勝が4名になり不可のため裏をOFFにする。
    if lb_on:
        # 決勝・準決勝を round_no 順で特定（決勝=最後, 準決勝=その1つ前）
        async with db.execute(
            "SELECT round_no, id FROM bracket_rounds "
            "WHERE tournament_id=? AND round_type IN ('final','normal') ORDER BY round_no", (tid,)
        ) as cur:
            nf_rounds = await cur.fetchall()
        semi_groups = None
        if len(nf_rounds) >= 2:
            semi_rid = nf_rounds[-2]["id"]  # 決勝の1つ前 = 準決勝
            async with db.execute(
                "SELECT COUNT(*) AS c FROM bracket_groups WHERE round_id=?", (semi_rid,)
            ) as cur:
                semi_groups = (await cur.fetchone())["c"]
        if semi_groups == 2:
            # 復活先＝決勝固定（revival_target_round=NULL を最終ラウンド扱い）
            await db.execute(
                "UPDATE tournaments SET revival_target_round=NULL WHERE id=?", (tid,)
            )
        else:
            # 準決勝が2グループ以外 → 裏トーナメント不可。OFFにする。
            await db.execute(
                "UPDATE tournaments SET losers_bracket=0, revival_target_round=NULL WHERE id=?", (tid,)
            )
        await db.commit()
    suffix = "?dup_resolved=1" if dup_resolved else ""
    # 参加者向けHTML配信（自動更新）
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass

    return RedirectResponse(url=f"/admin/tournaments/{tid}/bracket{suffix}", status_code=303)


# ============================================================
#  くじ引き手動配置（lottery）
# ============================================================

async def _lottery_round_no_to_slots(tid: int, round_no: int, db):
    """指定ラウンドの (lottery_no -> slot行dict) マップを返す。"""
    async with db.execute(
        """SELECT bs.id, bs.lottery_no, bs.entry_id
           FROM bracket_slots bs
           JOIN bracket_groups bg ON bg.id=bs.group_id
           JOIN bracket_rounds br ON br.id=bg.round_id
           WHERE br.tournament_id=? AND br.round_no=? AND bs.lottery_no IS NOT NULL
           ORDER BY bs.lottery_no""",
        (tid, round_no),
    ) as cur:
        return {r["lottery_no"]: dict(r) for r in await cur.fetchall()}


@router.post("/{tid}/bracket/lottery/assign")
async def bracket_lottery_assign(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """くじ割り当て。区分(round_no)＋くじ番号(lottery_no)＋レーサー(entry_id or スキャンcode)を
       受けて該当スロットへ書き込む。スキャン・手動選択の共通入口。"""
    from fastapi.responses import JSONResponse
    from app.services.barcode import parse_code

    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return JSONResponse({"ok": False, "message": "レースが見つかりません"})
    t = dict(t)
    if not t.get("lottery_pending"):
        return JSONResponse({"ok": False, "message": "くじ割り当て中ではありません"})

    form = await request.form()
    try:
        round_no = int((form.get("round_no") or "").strip())
        lottery_no = int((form.get("lottery_no") or "").strip())
    except ValueError:
        return JSONResponse({"ok": False, "message": "区分または番号が不正です"})

    raw_eid = (form.get("entry_id") or "").strip()
    raw = (form.get("code") or "").strip()

    # ── レーサー(entry_id)の解決：手動はentry_id直指定、スキャンはcode→解決 ──
    if raw_eid:
        if not raw_eid.isdigit():
            return JSONResponse({"ok": False, "message": "entry_id が不正です"})
        async with db.execute(
            """SELECT e.id AS entry_id, r.name
               FROM entries e JOIN racers r ON r.id=e.racer_id
               WHERE e.tournament_id=? AND e.id=? AND e.status='active'""",
            (tid, int(raw_eid)),
        ) as cur:
            ent = await cur.fetchone()
        if not ent:
            return JSONResponse({"ok": False, "message": "対象エントリーが見つかりません"})
    elif raw:
        seq_no = None
        if raw.isdigit() and len(raw) >= 10:
            parsed = parse_code(raw)
            if not parsed.get("valid"):
                return JSONResponse({"ok": False, "message": parsed.get("reason") or "コードが不正です"})
            if parsed.get("race_id") != tid:
                return JSONResponse({"ok": False, "message": "他レースのカードです"})
            seq_no = parsed.get("seq_no")
        elif raw.isdigit():
            seq_no = int(raw)
        else:
            return JSONResponse({"ok": False, "message": "コード形式が不正です"})
        async with db.execute(
            """SELECT e.id AS entry_id, r.name
               FROM entries e JOIN racers r ON r.id=e.racer_id
               WHERE e.tournament_id=? AND e.pre_seq_no=? AND e.status='active'""",
            (tid, seq_no),
        ) as cur:
            ent = await cur.fetchone()
        if not ent:
            return JSONResponse({"ok": False, "message": f"連番{seq_no:04d} は未受付です"})
    else:
        return JSONResponse({"ok": False, "message": "レーサーが指定されていません"})

    ent = dict(ent)
    entry_id = ent["entry_id"]

    # ── バリデーション ──
    # (0) 区分検証：このレーサーの区分(seeded)と枠の区分(round_no)が一致するか
    #     通常(seeded=0)→R1 / シード(seeded=1)→R2 / スーパーシード(seeded=2)→R3
    async with db.execute(
        "SELECT COALESCE(seeded,0) AS seeded FROM entries WHERE id=? AND tournament_id=?",
        (entry_id, tid),
    ) as cur:
        srow = await cur.fetchone()
    racer_seeded = (dict(srow).get("seeded") if srow else 0) or 0
    expected_round = {0: 1, 1: 2, 2: 3}.get(racer_seeded, 1)
    kubun_name = {1: "通常", 2: "シード", 3: "スーパーシード"}
    if expected_round != round_no:
        return JSONResponse({
            "ok": False,
            "message": f"{ent['name']} は「{kubun_name.get(expected_round, 'R'+str(expected_round))}」枠のレーサーです。"
                       f"「{kubun_name.get(round_no, 'R'+str(round_no))}」枠には割り当てできません。",
        })

    # (a) 区分・番号が有効か
    slot_map = await _lottery_round_no_to_slots(tid, round_no, db)
    if lottery_no not in slot_map:
        return JSONResponse({"ok": False, "message": f"区分R{round_no}に番号{lottery_no}の枠がありません"})
    target = slot_map[lottery_no]

    # (b) 重複：その枠が既に埋まっていないか
    if target["entry_id"] is not None:
        return JSONResponse({"ok": False, "message": f"番号{lottery_no}の枠は既に埋まっています"})

    # (c) 二重登録：このレーサーが同じ決勝の別スロットに既にいないか
    async with db.execute(
        """SELECT bs.lottery_no, br.round_no
           FROM bracket_slots bs
           JOIN bracket_groups bg ON bg.id=bs.group_id
           JOIN bracket_rounds br ON br.id=bg.round_id
           WHERE br.tournament_id=? AND bs.entry_id=? LIMIT 1""",
        (tid, entry_id),
    ) as cur:
        dup = await cur.fetchone()
    if dup:
        dup = dict(dup)
        return JSONResponse({
            "ok": False,
            "message": f"{ent['name']} は既にR{dup['round_no']}番号{dup['lottery_no']}に割当済みです",
        })

    # ── 書き込み ──
    await db.execute(
        "UPDATE bracket_slots SET entry_id=? WHERE id=?",
        (entry_id, target["id"]),
    )
    await db.commit()
    return JSONResponse({
        "ok": True,
        "round_no": round_no,
        "lottery_no": lottery_no,
        "entry_id": entry_id,
        "name": ent["name"],
    })


@router.post("/{tid}/bracket/lottery/clear")
async def bracket_lottery_clear(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """くじ割り当てのクリア（1枠の割当解除）。"""
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT lottery_pending FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or not dict(t).get("lottery_pending"):
        return JSONResponse({"ok": False, "message": "くじ割り当て中ではありません"})

    form = await request.form()
    try:
        round_no = int((form.get("round_no") or "").strip())
        lottery_no = int((form.get("lottery_no") or "").strip())
    except ValueError:
        return JSONResponse({"ok": False, "message": "区分または番号が不正です"})

    slot_map = await _lottery_round_no_to_slots(tid, round_no, db)
    if lottery_no not in slot_map:
        return JSONResponse({"ok": False, "message": f"区分R{round_no}に番号{lottery_no}の枠がありません"})
    await db.execute(
        "UPDATE bracket_slots SET entry_id=NULL WHERE id=?",
        (slot_map[lottery_no]["id"],),
    )
    await db.commit()
    return JSONResponse({"ok": True, "round_no": round_no, "lottery_no": lottery_no})


@router.post("/{tid}/bracket/lottery/confirm")
async def bracket_lottery_confirm(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """くじ割り当ての確定。全枠充足を検証し、充足していればトーナメントを開始する。"""
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return JSONResponse({"ok": False, "message": "レースが見つかりません"})
    t = dict(t)
    if not t.get("lottery_pending"):
        return JSONResponse({"ok": False, "message": "くじ割り当て中ではありません"})

    # ── 抜けチェック：lottery_noが振られた全スロットが埋まっているか ──
    async with db.execute(
        """SELECT br.round_no, bs.lottery_no
           FROM bracket_slots bs
           JOIN bracket_groups bg ON bg.id=bs.group_id
           JOIN bracket_rounds br ON br.id=bg.round_id
           WHERE br.tournament_id=? AND bs.lottery_no IS NOT NULL AND bs.entry_id IS NULL
           ORDER BY br.round_no, bs.lottery_no""",
        (tid,),
    ) as cur:
        empties = [dict(r) for r in await cur.fetchall()]
    if empties:
        labels = "、".join(f"R{e['round_no']}番号{e['lottery_no']}" for e in empties[:10])
        more = "" if len(empties) <= 10 else f" 他{len(empties)-10}枠"
        return JSONResponse({
            "ok": False,
            "message": f"未割り当ての枠があります（{labels}{more}）。すべて埋めてから確定してください。",
        })

    # ── 確定：くじ待ち解除。裏トーナメントの後処理を生成時と同じ仕様で実行 ──
    await db.execute("UPDATE tournaments SET lottery_pending=0 WHERE id=?", (tid,))

    if t.get("losers_bracket"):
        async with db.execute(
            "SELECT round_no, id FROM bracket_rounds "
            "WHERE tournament_id=? AND round_type IN ('final','normal') ORDER BY round_no", (tid,)
        ) as cur:
            nf_rounds = await cur.fetchall()
        semi_groups = None
        if len(nf_rounds) >= 2:
            semi_rid = nf_rounds[-2]["id"]
            async with db.execute(
                "SELECT COUNT(*) AS c FROM bracket_groups WHERE round_id=?", (semi_rid,)
            ) as cur:
                semi_groups = (await cur.fetchone())["c"]
        if semi_groups == 2:
            await db.execute("UPDATE tournaments SET revival_target_round=NULL WHERE id=?", (tid,))
        else:
            await db.execute(
                "UPDATE tournaments SET losers_bracket=0, revival_target_round=NULL WHERE id=?", (tid,)
            )
    await db.commit()

    # 参加者向けHTML配信（生成時と同じ）
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass

    # 複数店舗（クラウド版）対応：JSON応答は StoreResolverMiddleware の本文書き換え
    # 対象外（text/html のみ書き換え）のため、redirect には現在店舗の prefix を
    # サーバー側で明示的に前置する。オンプレ／既定店舗（slug=""）では prefix は空。
    _store = getattr(request.state, "store", None)
    _prefix = _store.prefix if _store else ""
    return JSONResponse({"ok": True, "redirect": f"{_prefix}/admin/tournaments/{tid}/bracket"})


@router.post("/{tid}/bracket/group/{group_id}/save")
async def bracket_save(
    tid: int,
    group_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    form = await request.form()
    winner_slot_id = form.get("winner_slot_id")
    if not winner_slot_id:
        return JSONResponse({"ok": False, "error": "winner not set"})

    winner_slot_id = int(winner_slot_id)

    # ラウンドタイプ確認
    async with db.execute(
        """SELECT br.round_type, br.id as round_id, br.round_no
           FROM bracket_groups bg
           JOIN bracket_rounds br ON br.id=bg.round_id
           WHERE bg.id=?""",
        (group_id,),
    ) as cur:
        rnd = await cur.fetchone()

    is_final = rnd and rnd["round_type"] == "final"

    # 決勝/3位決定戦の場合は全順位を保存
    await db.execute("DELETE FROM bracket_slot_ranks WHERE group_id=?", (group_id,))
    if is_final:
        async with db.execute(
            "SELECT id FROM bracket_slots WHERE group_id=? ORDER BY slot_no", (group_id,)
        ) as cur:
            slots = await cur.fetchall()
        for slot in slots:
            rank_val = form.get(f"rank_{slot['id']}")
            if rank_val:
                await db.execute(
                    "INSERT INTO bracket_slot_ranks (group_id, slot_id, rank) VALUES (?,?,?)",
                    (group_id, slot["id"], int(rank_val)),
                )

    # 上流(表normal)の勝者が変わる場合、その敗者で構成される裏トーナメントは
    # 無効になるため作り直す（保存後の _sync_losers_bracket で再構成される）。
    if rnd and rnd["round_type"] == "normal":
        old_w = await (await db.execute(
            "SELECT winner_slot_id FROM bracket_results WHERE group_id=?", (group_id,)
        )).fetchone()
        if old_w and old_w["winner_slot_id"] is not None and old_w["winner_slot_id"] != winner_slot_id:
            lb_on, _ = await _lb_enabled(tid, db)
            if lb_on:
                from_k = await _normal_round_index(tid, rnd["round_no"], db)
                await _teardown_losers(tid, db, from_k)

    # 勝者保存
    await db.execute("DELETE FROM bracket_results WHERE group_id=?", (group_id,))
    await db.execute(
        "INSERT INTO bracket_results (group_id, winner_slot_id) VALUES (?,?)",
        (group_id, winner_slot_id),
    )
    await db.commit()

    # 完了済みグループの勝者を次ラウンドの対応スロットに仮反映
    if rnd:
        await _prefill_next_round(tid, rnd["round_id"], rnd["round_no"], db)

    # 全グループが完了したら次ラウンド生成チェック
    advanced = await _try_advance_round(tid, rnd["round_id"] if rnd else None, db)
    # 裏トーナメント（有効時のみ）：敗者ドロップイン同期＋復活差し込み
    await _sync_losers_bracket(tid, db)
    reviver_inserted = await _try_insert_reviver(tid, db)

    # 参加者向けHTML配信（自動更新）
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass

    from app.routers.tournaments import _is_result_finalized
    finalized = await _is_result_finalized(tid, db)
    return JSONResponse({"ok": True, "advanced": advanced,
                         "reviver_inserted": bool(reviver_inserted),
                         "finalized": bool(finalized)})


# ── 次ラウンド自動生成 ────────────────────────────────────
async def _try_advance_round(tid: int, round_id: int, db: aiosqlite.Connection):
    """現ラウンドの全グループが完了したら次ラウンドを生成"""
    if not round_id:
        return False

    async with db.execute(
        "SELECT * FROM bracket_rounds WHERE id=?", (round_id,)
    ) as cur:
        rnd = await cur.fetchone()
    if not rnd:
        return False

    # 裏トーナメント（losers）の進行は _sync_losers_bracket が専任。
    # 汎用の勝ち上がり生成を通すと余分な決勝などが作られるためスキップ。
    if rnd["round_type"] == "losers":
        return False

    # 現ラウンドのグループと結果確認
    async with db.execute(
        "SELECT bg.id FROM bracket_groups bg WHERE bg.round_id=?", (round_id,)
    ) as cur:
        groups = await cur.fetchall()

    async with db.execute(
        "SELECT group_id FROM bracket_results WHERE group_id IN "
        f"({','.join('?' * len(groups))})",
        [g["id"] for g in groups],
    ) as cur:
        done = await cur.fetchall()

    if len(done) < len(groups):
        return False  # まだ未完了グループあり

    # 次ラウンドが既に存在する場合は勝者を書き込んでreturn
    next_round_no_check = rnd["round_no"] + 1
    async with db.execute(
        "SELECT id FROM bracket_rounds WHERE tournament_id=? AND round_no=? AND round_type NOT IN ('third','revival')",
        (tid, next_round_no_check),
    ) as cur:
        existing_next = await cur.fetchone()
    if existing_next:
        # 全グループの結果が揃っていたら次ラウンドへ進む
        # ※ _prefill_next_roundが既に各グループ完了時に勝者を流し込んでいる
        # revival(敗者復活戦)モードの場合、決勝の3枠目は復活戦勝者用に空けておく
        async with db.execute(
            "SELECT round_type FROM bracket_rounds WHERE id=?", (existing_next["id"],)
        ) as cur:
            next_rt_row = await cur.fetchone()
        next_rt = next_rt_row["round_type"] if next_rt_row else "normal"

        # revivalモードで次が決勝の場合は_prefill_next_roundに任せてここでは何もしない
        # それ以外は _prefill_next_round を再実行して全スロットを正しい順序で更新する
        # （旧実装のシャッフルは順序が不定になるバグがあったため廃止）
        async with db.execute("SELECT bracket_mode FROM tournaments WHERE id=?", (tid,)) as cur:
            bm_chk = await cur.fetchone()
        b_mode_chk = bm_chk["bracket_mode"] if bm_chk else "third_place"
        if next_rt != "final" or b_mode_chk != "revival":
            await _prefill_next_round(tid, round_id, rnd["round_no"], db)
        # 敗者復活戦の生成チェック（準決勝→復活戦）
        async with db.execute(
            "SELECT bracket_mode FROM tournaments WHERE id=?", (tid,)
        ) as cur:
            bm_row = await cur.fetchone()
        b_mode = bm_row["bracket_mode"] if bm_row else "third_place"
        _lb_on_g, _lb_t_g = await _lb_enabled(tid, db)  # 案B: 裏ON時は別途の敗者復活戦を作らない
        if (not _lb_on_g) and b_mode == "revival" and next_rt == "final":
            # 復活戦が未生成なら生成する
            revival_rno = rnd["round_no"] + 1
            async with db.execute(
                "SELECT id FROM bracket_rounds WHERE tournament_id=? AND round_type='revival'",
                (tid,)
            ) as cur:
                existing_revival = await cur.fetchone()
            if not existing_revival:
                # 準決勝の敗者を集める（winner_slot_idのスロットを除く）
                losers = []
                for g in groups:
                    async with db.execute(
                        "SELECT winner_slot_id FROM bracket_results WHERE group_id=?", (g["id"],)
                    ) as cur:
                        br_row = await cur.fetchone()
                    winner_sid = br_row["winner_slot_id"] if br_row else None
                    if winner_sid is None:
                        continue  # 勝者未確定はスキップ
                    async with db.execute(
                        """SELECT bs.entry_id FROM bracket_slots bs
                           WHERE bs.group_id=? AND bs.entry_id IS NOT NULL AND bs.id != ?""",
                        (g["id"], winner_sid)
                    ) as cur:
                        for row in await cur.fetchall():
                            if row["entry_id"]:
                                losers.append(row["entry_id"])
                if len(losers) >= 2:
                    revival_pat = combinations_2_3(len(losers))
                    if not revival_pat:
                        revival_pat = [[len(losers)]]
                    # 4名以上は複数グループに分ける（例: 4名→[2,2]の2グループ）
                    # 最初は1ラウンドで全員収める
                    await db.execute(
                        "INSERT INTO bracket_rounds (tournament_id, round_no, round_type) VALUES (?,?,?)",
                        (tid, revival_rno, "revival"),
                    )
                    async with db.execute("SELECT last_insert_rowid() as id") as cur:
                        revival_rnd_id = (await cur.fetchone())["id"]
                    li = 0
                    for gno, sz in enumerate(revival_pat[0], 1):
                        await db.execute("INSERT INTO bracket_groups (round_id, group_no) VALUES (?,?)", (revival_rnd_id, gno))
                        async with db.execute("SELECT last_insert_rowid() as id") as cur:
                            revival_gid = (await cur.fetchone())["id"]
                        for sno in range(1, sz + 1):
                            eid = losers[li] if li < len(losers) else None
                            await db.execute(
                                "INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)",
                                (revival_gid, sno, eid)
                            )
                            li += 1
        await db.commit()

        # R2が全スロット埋まった場合、3位決定戦が必要か判定
        # R2グループ数が1 → それが決勝。2名なら3位決定戦必要
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM bracket_groups WHERE round_id=?",
            (existing_next["id"],),
        ) as cur:
            r2_group_count = (await cur.fetchone())["cnt"]

        _lb_on_t, _lb_t_t = await _lb_enabled(tid, db)  # 案B: 裏ON時は別途の3位決定戦を作らない
        if (not _lb_on_t) and r2_group_count == 1:
            # R2が決勝（1グループ）→ 敗者数確認して3位決定戦生成
            # R1の全グループが完了済みか確認
            async with db.execute(
                "SELECT COUNT(*) as cnt FROM bracket_results WHERE group_id IN "
                "(SELECT id FROM bracket_groups WHERE round_id=?)", (round_id,)
            ) as cur:
                done_count = (await cur.fetchone())["cnt"]
            if done_count == len(groups):
                # 全完了 → 3位決定戦生成
                losers = []
                for g in groups:
                    async with db.execute(
                        """SELECT bs.entry_id FROM bracket_slots bs
                           WHERE bs.group_id=? AND bs.id != (
                               SELECT winner_slot_id FROM bracket_results WHERE group_id=?
                           )""",
                        (g["id"], g["id"]),
                    ) as cur:
                        for row in await cur.fetchall():
                            if row["entry_id"]: losers.append(row["entry_id"])
                # 勝者数の確認
                async with db.execute(
                    """SELECT COUNT(*) as cnt FROM bracket_slots bs
                       JOIN bracket_groups bg ON bg.id=bs.group_id
                       WHERE bg.round_id=?""",
                    (existing_next["id"],),
                ) as cur:
                    final_slot_count = (await cur.fetchone())["cnt"]
                n_final = final_slot_count
                if len(losers) >= 2 and n_final != 3:
                    # 3位決定戦を生成
                    third_rno = rnd["round_no"] + 1
                    async with db.execute(
                        "SELECT id FROM bracket_rounds WHERE tournament_id=? AND round_no=? AND round_type='third'",
                        (tid, third_rno),
                    ) as cur:
                        if not await cur.fetchone():
                            lp = combinations_2_3(len(losers))
                            if lp:
                                await db.execute(
                                    "INSERT INTO bracket_rounds (tournament_id, round_no, round_type) VALUES (?,?,?)",
                                    (tid, third_rno, "third"),
                                )
                                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                                    trid = (await cur.fetchone())["id"]
                                li = 0
                                for gno, sz in enumerate(lp[0], 1):
                                    await db.execute(
                                        "INSERT INTO bracket_groups (round_id, group_no) VALUES (?,?)",
                                        (trid, gno),
                                    )
                                    async with db.execute("SELECT last_insert_rowid() as id") as cur:
                                        tgid = (await cur.fetchone())["id"]
                                    for sno in range(1, sz + 1):
                                        eid = losers[li] if li < len(losers) else None
                                        await db.execute(
                                            "INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)",
                                            (tgid, sno, eid),
                                        )
                                        li += 1
                                await db.commit()
        return True  # 進行した

    # 勝者リスト（グループ順）
    winners = []
    for g in groups:
        async with db.execute(
            """SELECT bs.entry_id FROM bracket_results br
               JOIN bracket_slots bs ON bs.id=br.winner_slot_id
               WHERE br.group_id=?""",
            (g["id"],),
        ) as cur:
            w = await cur.fetchone()
        if w:
            winners.append(w["entry_id"])

    if rnd["round_type"] == "final":
        return False  # 決勝完了

    n_winners = len(winners)

    # 3位決定戦ラウンドの場合：勝者が1名になれば終わり、複数なら次の3位決定戦ラウンドへ
    if rnd["round_type"] in ("third", "revival"):
        # 敗者復活戦の場合：勝者1名なら決勝へ、複数なら次の復活戦ラウンドへ
        if rnd["round_type"] == "revival":
            if n_winners == 1:
                # 復活戦完了：勝者を決勝の空きスロットに追加
                winner_eid = winners[0] if winners else None
                if winner_eid:
                    async with db.execute(
                        """SELECT bs.id FROM bracket_slots bs
                           JOIN bracket_groups bg ON bg.id=bs.group_id
                           JOIN bracket_rounds br ON br.id=bg.round_id
                           WHERE br.tournament_id=? AND br.round_type='final'
                             AND bs.entry_id IS NULL
                           ORDER BY bg.group_no, bs.slot_no LIMIT 1""",
                        (tid,),
                    ) as cur:
                        empty_final_slot = await cur.fetchone()
                    if empty_final_slot:
                        await db.execute(
                            "UPDATE bracket_slots SET entry_id=? WHERE id=?",
                            (winner_eid, empty_final_slot["id"]),
                        )
                await db.commit()
                return True
            # 複数グループのrevivalが完了 → 勝者同士で次の復活戦ラウンド（決定戦）を生成
            if n_winners >= 2:
                next_revival_rno = rnd["round_no"] + 1
                async with db.execute(
                    "SELECT id FROM bracket_rounds WHERE tournament_id=? AND round_no=? AND round_type='revival'",
                    (tid, next_revival_rno),
                ) as cur:
                    existing_next_revival = await cur.fetchone()
                if not existing_next_revival:
                    revival_pat = combinations_2_3(n_winners)
                    if not revival_pat:
                        revival_pat = [[n_winners]]
                    await db.execute(
                        "INSERT INTO bracket_rounds (tournament_id, round_no, round_type) VALUES (?,?,?)",
                        (tid, next_revival_rno, "revival"),
                    )
                    async with db.execute("SELECT last_insert_rowid() as id") as cur:
                        next_revival_rnd_id = (await cur.fetchone())["id"]
                    li = 0
                    for gno, sz in enumerate(revival_pat[0], 1):
                        await db.execute("INSERT INTO bracket_groups (round_id, group_no) VALUES (?,?)", (next_revival_rnd_id, gno))
                        async with db.execute("SELECT last_insert_rowid() as id") as cur:
                            next_revival_gid = (await cur.fetchone())["id"]
                        for sno in range(1, sz + 1):
                            eid = winners[li] if li < len(winners) else None
                            await db.execute(
                                "INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)",
                                (next_revival_gid, sno, eid)
                            )
                            li += 1
                    await db.commit()
                return True
            return False

        # 3位決定戦：次ラウンドへ進める（複数グループある場合）
        if n_winners <= 1:
            return False
        next_rno = rnd["round_no"] + 1
        async with db.execute(
            "SELECT id FROM bracket_rounds WHERE tournament_id=? AND round_no=? AND round_type='third'",
            (tid, next_rno),
        ) as cur:
            if await cur.fetchone():
                return False
        patterns = combinations_2_3(n_winners)
        if not patterns:
            return False
        np = patterns[0]
        await db.execute(
            "INSERT INTO bracket_rounds (tournament_id, round_no, round_type) VALUES (?,?,?)",
            (tid, next_rno, "third"),
        )
        async with db.execute("SELECT last_insert_rowid() as id") as cur:
            nrid = (await cur.fetchone())["id"]
        widx = 0
        for gno, sz in enumerate(np, 1):
            await db.execute("INSERT INTO bracket_groups (round_id, group_no) VALUES (?,?)", (nrid, gno))
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                gid = (await cur.fetchone())["id"]
            for sno in range(1, sz + 1):
                eid = winners[widx] if widx < len(winners) else None
                await db.execute(
                    "INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (gid, sno, eid)
                )
                widx += 1
        await db.commit()
        return True

    # 勝者数に応じた次ラウンド判定

    if n_winners == 1:
        return False  # 優勝者決定

    # 3位決定戦が必要か（現ラウンドが準決勝 = 次が決勝かつ元々4名以上）
    need_third = False
    losers = []
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM bracket_rounds WHERE tournament_id=?", (tid,)
    ) as cur:
        round_count = (await cur.fetchone())["cnt"]

    # 次ラウンドが既に生成済み（スケルトン）の場合は勝者を流し込む
    next_round_no = rnd["round_no"] + 1
    async with db.execute(
        "SELECT id, round_type FROM bracket_rounds WHERE tournament_id=? AND round_no=? AND round_type IN ('normal','final')",
        (tid, next_round_no),
    ) as cur:
        next_round = await cur.fetchone()

    if next_round:
        # スケルトンあり → 空スロットに流し込む（グループ順・スロット順で1対1マッピング）
        async with db.execute(
            """SELECT bs.id as slot_id, bg.group_no, bs.slot_no FROM bracket_slots bs
               JOIN bracket_groups bg ON bg.id=bs.group_id
               WHERE bg.round_id=? AND bs.entry_id IS NULL
               ORDER BY bg.group_no, bs.slot_no""",
            (next_round["id"],),
        ) as cur:
            empty_slots = [r["slot_id"] for r in await cur.fetchall()]

        # winnersをグループ順（シャッフルせず）でそのまま流し込む
        for i, winner_eid in enumerate(winners):
            if i >= len(empty_slots): break
            await db.execute("UPDATE bracket_slots SET entry_id=? WHERE id=?", (winner_eid, empty_slots[i]))

        # 3位決定戦チェック：次ラウンドが決勝（1グループ）で現ラウンドが複数グループなら3位決定戦
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM bracket_groups WHERE round_id=?", (next_round["id"],)
        ) as cur:
            next_group_count = (await cur.fetchone())["cnt"]

        is_next_final = next_group_count == 1 or next_round["round_type"] == "final"
        # semi_3groupモードでは決勝3名なので3位決定戦不要
        async with db.execute("SELECT bracket_mode FROM tournaments WHERE id=?", (tid,)) as cur:
            bm_nd = await cur.fetchone()
        b_mode_nd = bm_nd["bracket_mode"] if bm_nd else "third_place"
        need_third = is_next_final and len(groups) > 1 and b_mode_nd != "semi_3group"
        if need_third:
            # 3位決定戦が未生成か確認
            async with db.execute(
                "SELECT id FROM bracket_rounds WHERE tournament_id=? AND round_no=? AND round_type='third'",
                (tid, next_round_no),
            ) as cur:
                existing_third = await cur.fetchone()

            if not existing_third:
                # 現ラウンド各グループの敗者を収集
                losers = []
                for g in groups:
                    async with db.execute(
                        """SELECT bs.entry_id FROM bracket_slots bs
                           WHERE bs.group_id=? AND bs.id != (
                               SELECT winner_slot_id FROM bracket_results WHERE group_id=?
                           ) AND bs.entry_id IS NOT NULL""",
                        (g["id"], g["id"]),
                    ) as cur:
                        for row in await cur.fetchall():
                            if row["entry_id"]: losers.append(row["entry_id"])

                if len(losers) >= 2:
                    lp = combinations_2_3(len(losers))
                    if lp:
                        # bracket_modeを取得
                        async with db.execute(
                            "SELECT bracket_mode FROM tournaments WHERE id=?", (tid,)
                        ) as cur:
                            bm_row = await cur.fetchone()
                        b_mode = bm_row["bracket_mode"] if bm_row else "third_place"
                        third_type = "revival" if b_mode == "revival" else "third"

                        await db.execute(
                            "INSERT INTO bracket_rounds (tournament_id, round_no, round_type) VALUES (?,?,?)",
                            (tid, next_round_no, third_type),
                        )
                        async with db.execute("SELECT last_insert_rowid() as id") as cur:
                            trid = (await cur.fetchone())["id"]
                        li = 0
                        for gno, sz in enumerate(lp[0], 1):
                            await db.execute("INSERT INTO bracket_groups (round_id, group_no) VALUES (?,?)", (trid, gno))
                            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                                tgid = (await cur.fetchone())["id"]
                            for sno in range(1, sz + 1):
                                eid = losers[li] if li < len(losers) else None
                                await db.execute("INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (tgid, sno, eid))
                                li += 1

        await db.commit()
        return True

    # スケルトンなし → 自動生成
    # R1完了時のみシード選手を次ラウンドに追加
    seeded_entries = []
    if rnd["round_no"] == 1:
        async with db.execute(
            """SELECT e.id as entry_id FROM entries e
               WHERE e.tournament_id=? AND e.seeded=1 AND e.status='active'""",
            (tid,),
        ) as cur:
            seeded_entries = [r["entry_id"] for r in await cur.fetchall()]

    total_next = n_winners + len(seeded_entries)
    patterns = combinations_2_3(total_next)
    if not patterns:
        await db.commit()
        return False

    next_pattern = patterns[0]
    is_next_final = len(next_pattern) == 1
    round_type = "final" if is_next_final else "normal"

    # 3位決定戦（次が決勝かつ現ラウンドが複数グループ）
    # semi_3groupモードでは決勝3名なので3位決定戦不要
    async with db.execute("SELECT bracket_mode FROM tournaments WHERE id=?", (tid,)) as cur:
        bm_nt = await cur.fetchone()
    b_mode_nt = bm_nt["bracket_mode"] if bm_nt else "third_place"
    need_third = is_next_final and len(groups) > 1 and total_next != 3 and b_mode_nt != "semi_3group"
    losers = []
    if need_third:
        for g in groups:
            async with db.execute(
                """SELECT bs.entry_id FROM bracket_slots bs
                   WHERE bs.group_id=? AND bs.id != (
                       SELECT winner_slot_id FROM bracket_results WHERE group_id=?
                   ) AND bs.entry_id IS NOT NULL""",
                (g["id"], g["id"]),
            ) as cur:
                for row in await cur.fetchall():
                    if row["entry_id"]: losers.append(row["entry_id"])

    if need_third and len(losers) >= 2:
        lp = combinations_2_3(len(losers))
        if lp:
            await db.execute("INSERT INTO bracket_rounds (tournament_id, round_no, round_type) VALUES (?,?,?)", (tid, next_round_no, "third"))
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                trid = (await cur.fetchone())["id"]
            li = 0
            for gno, sz in enumerate(lp[0], 1):
                await db.execute("INSERT INTO bracket_groups (round_id, group_no) VALUES (?,?)", (trid, gno))
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    tgid = (await cur.fetchone())["id"]
                for sno in range(1, sz + 1):
                    eid = losers[li] if li < len(losers) else None
                    await db.execute("INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (tgid, sno, eid))
                    li += 1

    await db.execute("INSERT INTO bracket_rounds (tournament_id, round_no, round_type) VALUES (?,?,?)", (tid, next_round_no, round_type))
    async with db.execute("SELECT last_insert_rowid() as id") as cur:
        next_round_id = (await cur.fetchone())["id"]

    # 勝者＋シード選手を合わせてシャッフルしてグループに配置
    import random as _r2
    all_next = winners + seeded_entries
    _r2.shuffle(all_next)
    w_idx = 0
    for gno, sz in enumerate(next_pattern, 1):
        await db.execute("INSERT INTO bracket_groups (round_id, group_no) VALUES (?,?)", (next_round_id, gno))
        async with db.execute("SELECT last_insert_rowid() as id") as cur:
            gid = (await cur.fetchone())["id"]
        for sno in range(1, sz + 1):
            eid = all_next[w_idx] if w_idx < len(all_next) else None
            await db.execute("INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (gid, sno, eid))
            w_idx += 1

    await db.commit()
    return True


# ── ヘルパー ──────────────────────────────────────────────
async def _get_ht_finalist_seed_keys(tid: int, db: aiosqlite.Connection) -> set:
    """ht_finalist_seeds から (entry_id, heat_no) のセットを返す"""
    async with db.execute(
        "SELECT entry_id, heat_no FROM ht_finalist_seeds WHERE tournament_id=? AND seeded=1", (tid,)
    ) as cur:
        return {(r["entry_id"], r["heat_no"]) async for r in cur}

async def _get_seeded_ids(tid: int, db: aiosqlite.Connection) -> tuple:
    """seeded=1のentry_idセット と seeded=2のentry_idセット をタプルで返す"""
    async with db.execute(
        "SELECT id FROM entries WHERE tournament_id=? AND seeded=1", (tid,)
    ) as cur:
        seeded_set = {r["id"] for r in await cur.fetchall()}
    async with db.execute(
        "SELECT id FROM entries WHERE tournament_id=? AND seeded=2", (tid,)
    ) as cur:
        super_seeded_set = {r["id"] for r in await cur.fetchall()}
    return seeded_set, super_seeded_set


async def _get_advanced_entries(tid: int, db: aiosqlite.Connection) -> list[dict]:
    """advanced=1（決勝進出○）のレーサーを予選順位順で返す"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return []
    qual_type = dict(t).get("qualifying_type", "")

    # advanced=1 のentry_idを取得
    async with db.execute(
        "SELECT id FROM entries WHERE tournament_id=? AND advanced>=1 AND status='active'",
        (tid,),
    ) as cur:
        adv_ids = {r["id"] for r in await cur.fetchall()}

    if not adv_ids:
        return []

    if qual_type == "roundrobin":
        async with db.execute(
            """SELECT e.id as entry_id, e.racer_id, r.name,
                      COALESCE(SUM(hr.win),0) as score1,
                      COALESCE(SUM(CASE WHEN hr.best_time IS NOT NULL THEN hr.best_time ELSE 0 END),0) as score2
               FROM entries e
               JOIN racers r ON r.id=e.racer_id
               LEFT JOIN heat_lanes hl ON hl.entry_id=e.id
               LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
               LEFT JOIN heats h ON h.id=hl.heat_id AND h.tournament_id=?
               WHERE e.tournament_id=? AND e.status='active' AND e.advanced>=1
               GROUP BY e.id
               ORDER BY score1 DESC, score2 ASC""",
            (tid, tid),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    elif qual_type == "heat_roundrobin":
        # ヒート総当たり：qual_heat_final=1 の場合は heat_finals 勝者、なければグループ上位
        t_dict_hr = dict(t)
        qual_heat_final_hr = bool(t_dict_hr.get("qual_heat_final", 0))
        qual_heat_exclude_hr = bool(t_dict_hr.get("qual_heat_exclude", 0))
        rows = []
        seen_hr: set = set()
        async with db.execute(
            "SELECT DISTINCT round_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY round_no",
            (tid,),
        ) as cur:
            heat_nos_hr2 = [r["round_no"] for r in await cur.fetchall()]
        if qual_heat_final_hr:
            # 優勝トーナメントあり：heat_finals の勝者順に取得
            for rno in heat_nos_hr2:
                async with db.execute(
                    """SELECT hf.slot_no, hf.entry_id, r.name
                       FROM heat_finals hf
                       JOIN entries e ON e.id=hf.entry_id
                       JOIN racers r ON r.id=e.racer_id
                       WHERE hf.tournament_id=? AND hf.round_no=? AND hf.group_no=0
                         AND hf.final_type='heat' AND hf.winner_entry_id IS NOT NULL
                       ORDER BY hf.slot_no""",
                    (tid, rno),
                ) as cur:
                    heat_winners = [dict(r) for r in await cur.fetchall()]
                for hw in heat_winners:
                    eid = hw["entry_id"]
                    if qual_heat_exclude_hr and eid in seen_hr:
                        continue
                    rows.append({"entry_id": eid, "racer_id": None, "name": hw["name"],
                                  "score1": 0, "score2": 0})
                    if qual_heat_exclude_hr:
                        seen_hr.add(eid)
        else:
            # 優勝トーナメントなし：グループ上位から取得
            from app.routers.qualifying import _calc_standings_group_round
            group_advance_hr = int(t_dict_hr.get("qual_group_advance", 1) or 1)
            async with db.execute(
                "SELECT DISTINCT round_no, group_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY round_no, group_no",
                (tid,),
            ) as cur:
                rg_pairs_hr = [(r["round_no"], r["group_no"]) for r in await cur.fetchall()]
            group_nos_hr2 = sorted(set(gno for _, gno in rg_pairs_hr))
            for rno in heat_nos_hr2:
                for gno in group_nos_hr2:
                    if (rno, gno) not in rg_pairs_hr:
                        continue
                    st_hr = await _calc_standings_group_round(tid, gno, rno, db)
                    picked = 0
                    for p in st_hr:
                        if picked >= group_advance_hr:
                            break
                        eid = p["entry_id"]
                        if qual_heat_exclude_hr and eid in seen_hr:
                            continue
                        rows.append({"entry_id": eid, "racer_id": None, "name": p["name"],
                                      "score1": p.get("wins", 0), "score2": 0})
                        if qual_heat_exclude_hr:
                            seen_hr.add(eid)
                        picked += 1
    elif qual_type in HEAT_TOURNAMENT_TYPES:
        # ヒート制トーナメント：ヒート1の1位→2位→3位→ヒート2の1位...の順
        from app.routers.qualifying import _ht_get_advanced as _htga
        t_row = dict(t)
        heat_advance = int(t_row.get("qual_heat_advance") or 1)
        group_advance = int(t_row.get("qual_group_advance") or heat_advance)
        heat_count = int(t_row.get("qual_heat_count") or 1)
        adv_per = group_advance
        rows = []
        seen_ids = set()
        for hno in range(1, heat_count + 1):
            adv = await _htga(tid, hno, adv_per, db)
            for a in sorted(adv, key=lambda x: x.get("overall_rank", 99)):
                if a.get("entry_id") and a["entry_id"] not in seen_ids and a["entry_id"] in adv_ids:
                    rows.append({"entry_id": a["entry_id"], "racer_id": None, "name": a["name"]})
                    seen_ids.add(a["entry_id"])
    elif qual_type == "none":
        async with db.execute(
            """SELECT e.id as entry_id, e.racer_id, r.name, e.entry_order as score1, 0 as score2
               FROM entries e JOIN racers r ON r.id=e.racer_id
               WHERE e.tournament_id=? AND e.status='active' AND e.advanced>=1
               ORDER BY e.entry_order""",
            (tid,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    elif qual_type == "order_winner":
        # 並び順（勝ち抜け）：最終段階の通過者を通過順（passed_seq）で並べる。
        #   決勝のシードは決勝管理側で設定するため、ここは通過順の表示のみ。
        async with db.execute(
            "SELECT COALESCE(order_winner_stage_count,1) AS sc FROM tournaments WHERE id=?",
            (tid,),
        ) as cur:
            _owsc_row = await cur.fetchone()
        _last_stage = (_owsc_row["sc"] if _owsc_row else 1) or 1
        async with db.execute(
            """SELECT e.id as entry_id, e.racer_id, r.name,
                      owr.passed_seq as score1, 0 as score2
               FROM order_winner_racers owr
               JOIN entries e ON e.id=owr.entry_id
               JOIN racers r ON r.id=e.racer_id
               WHERE owr.tournament_id=? AND owr.stage_no=? AND owr.status='passed'
                     AND e.status='active' AND e.advanced>=1
               ORDER BY owr.passed_seq""",
            (tid, _last_stage),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    else:
        async with db.execute(
            """SELECT e.id as entry_id, e.racer_id, r.name,
                      COALESCE(SUM(hr.points),0) as score1,
                      COALESCE(SUM(hr.lap_count),0) as score2
               FROM entries e
               JOIN racers r ON r.id=e.racer_id
               LEFT JOIN heat_lanes hl ON hl.entry_id=e.id
               LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
               LEFT JOIN heats h ON h.id=hl.heat_id AND h.tournament_id=?
               WHERE e.tournament_id=? AND e.status='active' AND e.advanced>=1
               GROUP BY e.id
               ORDER BY score1 DESC, score2 DESC""",
            (tid, tid),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    # rank付与
    # 並び順（ポイント制）order・ポイント制point は、予選順位表（_calc_standings）と同じく
    # 同ポイント＝同順位（1,1,3,3,…）にする。それ以外の形式は従来どおり連番。
    if qual_type in ("order", "point"):
        for i, row in enumerate(rows):
            if i > 0 and row["score1"] == rows[i - 1]["score1"]:
                row["rank"] = rows[i - 1]["rank"]
            else:
                row["rank"] = i + 1
    else:
        for i, row in enumerate(rows):
            row["rank"] = i + 1
    return rows


async def _get_ht_per_heat_advanced(tid: int, db) -> list[dict]:
    """heat_tournament用：ヒートごとの決勝進出者をoverall_rank付きで返す
    [{heat_no, advanced: [{name, overall_rank, entry_id}]}]

    ヒート決勝あり → 各ヒートのヒート決勝の上位 qual_heat_advance 名（＝本戦進出者）。
    ヒート決勝なし → 各グループ上位 qual_group_advance 名（グループ通過者）。
    """
    from app.routers.qualifying import _ht_get_advanced, _ht_get_heatfinal_advancers
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return []
    t_dict = dict(t)
    heat_advance = int(t_dict.get("qual_heat_advance") or 1)
    group_advance = int(t_dict.get("qual_group_advance") or heat_advance)
    heat_count = int(t_dict.get("qual_heat_count") or 1)
    has_heat_final = bool(t_dict.get("qual_heat_final", 0))

    result = []
    for hno in range(1, heat_count + 1):
        if has_heat_final:
            # ヒート決勝あり：本戦進出者（ヒート決勝 上位 heat_advance 名）
            advs = await _ht_get_heatfinal_advancers(tid, hno, heat_advance, db)
            advanced = [
                {"entry_id": a["entry_id"], "name": a["name"], "overall_rank": a["rank"]}
                for a in advs if a.get("is_advance") and a.get("entry_id")
            ]
        else:
            # ヒート決勝なし：グループ通過者
            advanced = await _ht_get_advanced(tid, hno, group_advance, db)
        if advanced:
            result.append({
                "heat_no": hno,
                "advanced": sorted(advanced, key=lambda x: x.get("overall_rank", 99)),
            })
    return result



async def _get_all_standings(tid: int, db: aiosqlite.Connection) -> list[dict]:
    """予選全順位を取得（同率rank付き）※bracket内部用"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return []
    qual_type = dict(t).get("qualifying_type", "")

    if qual_type == "roundrobin":
        async with db.execute(
            """SELECT e.id as entry_id, e.racer_id, r.name,
                      COALESCE(SUM(hr.win),0) as score1,
                      COALESCE(SUM(CASE WHEN hr.best_time IS NOT NULL THEN hr.best_time ELSE 0 END),0) as score2
               FROM entries e
               JOIN racers r ON r.id=e.racer_id
               LEFT JOIN heat_lanes hl ON hl.entry_id=e.id
               LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
               LEFT JOIN heats h ON h.id=hl.heat_id AND h.tournament_id=?
               WHERE e.tournament_id=? AND e.status='active'
               GROUP BY e.id
               ORDER BY score1 DESC, score2 ASC""",
            (tid, tid),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    elif qual_type == "heat_roundrobin":
        # ヒート総当たり：全ヒート・全グループの勝数合計（総合成績）
        async with db.execute(
            """SELECT e.id as entry_id, e.racer_id, r.name,
                      COALESCE(SUM(sub.win), 0) as score1,
                      COUNT(CASE WHEN sub.win IS NOT NULL THEN 1 END) as score2
               FROM entries e
               JOIN racers r ON r.id=e.racer_id
               LEFT JOIN (
                   SELECT hl2.entry_id, hr2.win
                   FROM heat_lanes hl2
                   JOIN heats h2 ON h2.id=hl2.heat_id
                   LEFT JOIN heat_results hr2 ON hr2.heat_lane_id=hl2.id
                   WHERE h2.tournament_id=?
               ) sub ON sub.entry_id=e.id
               WHERE e.tournament_id=? AND e.status='active'
               GROUP BY e.id
               ORDER BY score1 DESC, score2 ASC""",
            (tid, tid),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    elif qual_type in HEAT_TOURNAMENT_TYPES:
        # ヒート制トーナメント：1位=100pt, 2位=10pt, 3位=1pt × ヒート数の合計で順位決定
        async with db.execute(
            """SELECT hs.entry_id, r.name,
                      COALESCE(SUM(CASE
                          WHEN hr.round_type='final' AND hsr.rank=1 THEN 100
                          WHEN hr.round_type='final' AND hsr.rank=2 THEN 10
                          WHEN hr.round_type='final' AND hsr.rank=3 THEN 1
                          WHEN hr.round_type='third' AND hsr.rank=1 THEN 1
                          ELSE 0
                      END), 0) as total_points
               FROM entries e
               JOIN racers r ON r.id=e.racer_id
               LEFT JOIN ht_slots hs ON hs.entry_id=e.id
               LEFT JOIN ht_groups hg ON hg.id=hs.group_id
               LEFT JOIN ht_rounds hr ON hr.id=hg.round_id AND hr.tournament_id=? AND hr.round_type IN ('final','third')
               LEFT JOIN ht_slot_ranks hsr ON hsr.slot_id=hs.id AND hsr.group_id=hs.group_id
               WHERE e.tournament_id=? AND e.status='active'
               GROUP BY e.id
               ORDER BY total_points DESC, e.entry_order""",
            (tid, tid),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    elif qual_type == "none":
        async with db.execute(
            """SELECT e.id as entry_id, e.racer_id, r.name, e.entry_order as score1, 0 as score2
               FROM entries e JOIN racers r ON r.id=e.racer_id
               WHERE e.tournament_id=? AND e.status='active'
               ORDER BY e.entry_order""",
            (tid,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    else:
        async with db.execute(
            """SELECT e.id as entry_id, e.racer_id, r.name,
                      COALESCE(SUM(hr.points),0) as score1,
                      0 as score2
               FROM entries e
               JOIN racers r ON r.id=e.racer_id
               LEFT JOIN heat_lanes hl ON hl.entry_id=e.id
               LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
               LEFT JOIN heats h ON h.id=hl.heat_id AND h.tournament_id=?
               WHERE e.tournament_id=? AND e.status='active'
               GROUP BY e.id
               ORDER BY score1 DESC""",
            (tid, tid),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    # 同率処理: スコアが同じなら同rank。
    # ※ 予選形式により成績の列名が異なる（heat_tournament は total_points、
    #    それ以外は score1）。どちらでも動くようキーをフォールバックして参照する。
    def _score_of(r: dict):
        if "score1" in r:
            return r["score1"]
        return r.get("total_points", 0)

    for i, row in enumerate(rows):
        if i == 0:
            row["rank"] = 1
        else:
            prev = rows[i-1]
            if _score_of(row) == _score_of(prev):
                row["rank"] = prev["rank"]
            else:
                row["rank"] = i + 1
    return rows


async def _get_finalists(tid: int, db: aiosqlite.Connection,
                          selected_ids: list[int] | None = None) -> list[dict]:
    """決勝進出者を返す。selected_idsがあればその順で返す"""
    standings = await _get_all_standings(tid, db)
    if selected_ids is not None:
        id_set = set(selected_ids)
        return [r for r in standings if r["entry_id"] in id_set]
    return standings


async def _get_rounds(tid: int, db: aiosqlite.Connection):
    async with db.execute(
        "SELECT * FROM bracket_rounds WHERE tournament_id=? ORDER BY (round_type IN ('third','revival')), round_no",
        (tid,),
    ) as cur:
        return await cur.fetchall()


async def _get_group_detail(group_id: int, db: aiosqlite.Connection):
    async with db.execute(
        """SELECT bs.id as slot_id, bs.slot_no, bs.entry_id, bs.is_bye, r.name,
                  e.seeded as entry_seeded_val
           FROM bracket_slots bs
           LEFT JOIN entries e ON e.id=bs.entry_id
           LEFT JOIN racers r ON r.id=e.racer_id
           WHERE bs.group_id=? ORDER BY bs.slot_no""",
        (group_id,),
    ) as cur:
        slots = [dict(r) for r in await cur.fetchall()]

    # 決勝・3位決定戦・敗者復活戦ではis_seed_slotをFalseに
    async with db.execute(
        """SELECT br.round_type FROM bracket_groups bg
           JOIN bracket_rounds br ON br.id=bg.round_id
           WHERE bg.id=?""", (group_id,)
    ) as cur:
        rnd_row = await cur.fetchone()
    is_final_or_third = rnd_row and rnd_row["round_type"] in ("final", "third", "revival")

    # entry_seeded_val から is_seed_slot を直接設定（別クエリ不要）
    for s in slots:
        sv = int(s.get("entry_seeded_val") or 0)
        if sv == 2:
            s["is_seed_slot"] = 2  # スーパーシード → パープル（決勝でも表示）
        elif sv == 1 and not is_final_or_third:
            s["is_seed_slot"] = 1  # シード → イエロー（決勝では非表示）
        else:
            s["is_seed_slot"] = 0

    # 予選順位を付与（entry_id → 予選の何位か）
    if slots:
        tid_q = None
        for s in slots:
            if s["entry_id"]:
                async with db.execute(
                    "SELECT tournament_id, seeded FROM entries WHERE id=?", (s["entry_id"],)
                ) as cur2:
                    row = await cur2.fetchone()
                    if row:
                        tid_q = row["tournament_id"]
                        entry_seeded = int(row["seeded"]) if row["seeded"] else 0
                        if not entry_seeded and not is_final_or_third:
                            # ht_finalist_seeds も確認（枠単位シード管理）
                            # ただしslot_no=1はR1勝者用スロットなのでシード扱いしない
                            if s.get("slot_no", 1) > 1:
                                async with db.execute(
                                    "SELECT 1 FROM ht_finalist_seeds WHERE tournament_id=? AND entry_id=? AND seeded=1",
                                    (row["tournament_id"], s["entry_id"])
                                ) as hc:
                                    if await hc.fetchone():
                                        entry_seeded = 1
                        # 決勝でもスーパーシード(=2)はハイライト表示
                        s["is_seed_slot"] = (entry_seeded if entry_seeded == 2 else 0) if is_final_or_third else entry_seeded
                    else:
                        s["is_seed_slot"] = False
            else:
                s["is_seed_slot"] = False
        if tid_q:
            standings = await _get_all_standings(tid_q, db)
            rank_map = {st["entry_id"]: st["rank"] for st in standings}
            for s in slots:
                s["qual_rank"] = rank_map.get(s["entry_id"])
        else:
            for s in slots: s["qual_rank"] = None
    else:
        for s in slots: s["qual_rank"] = None; s["is_seed_slot"] = False

    async with db.execute(
        "SELECT * FROM bracket_results WHERE group_id=?", (group_id,)
    ) as cur:
        result = await cur.fetchone()

    async with db.execute(
        "SELECT slot_id, rank FROM bracket_slot_ranks WHERE group_id=? ORDER BY rank",
        (group_id,),
    ) as cur:
        ranks = {r["slot_id"]: r["rank"] for r in await cur.fetchall()}

    return slots, result, ranks


def _build_svg_data(groups_data: list) -> dict:
    """
    トーナメント図用データを構築する（JSON化できる形式に変換）。
    """
    rounds_map = {}   # normal rounds
    final_gd = None
    third_map = {}    # third rounds by round_no

    for gd in groups_data:
        rt = gd["round"]["round_type"]
        rno = gd["round"]["round_no"]
        clean_gd = {
            "slots": [
                {"slot_id": s["slot_id"], "slot_no": s["slot_no"],
                 "name": s["name"], "entry_id": s["entry_id"],
                 "qual_rank": s.get("qual_rank"),
                 "is_seed_slot": s.get("is_seed_slot", False),
                 "rank": gd.get("ranks", {}).get(s["slot_id"])}
                for s in gd["slots"]
            ],
            "result": {"winner_slot_id": gd["result"]["winner_slot_id"]} if gd["result"] else None,
            "group_id": gd["group"]["id"],
            # 生成時に焼き付けた「勝者の進む先スロット」（固定リンク）。
            # 結線・レイアウトはこのリンクを唯一の真実として辿る（v5.7+）。
            "advance_to_slot_id": gd["group"].get("advance_to_slot_id"),
        }
        if rt == "final":
            final_gd = clean_gd
            final_gd["_round_no"] = rno  # 実際のround_noを保存
        elif rt in ("third", "revival"):
            clean_gd["_revival"] = (rt == "revival")
            third_map.setdefault(rno, []).append(clean_gd)
        else:
            rounds_map.setdefault(rno, []).append(clean_gd)

    # normalラウンドの総数（決勝を含む）
    # normalラウンドのうち1グループのものは実質決勝として扱う
    # （先行生成でround_type=normalのまま決勝になるケースに対応）
    final_gd_from_normal = None
    filtered_rounds_map = {}
    for rno, groups in rounds_map.items():
        total_groups = len(groups)
        if total_groups == 1 and not final_gd:
            # 1グループのnormalラウンドは決勝扱い
            final_gd_from_normal = groups[0]
            final_gd_from_normal_rno = rno
        else:
            filtered_rounds_map[rno] = groups
    if final_gd_from_normal and not final_gd:
        final_gd = final_gd_from_normal

    # normalラウンド（決勝含む）の総数：これをもとにround_labelが決勝からの距離を計算
    total_normal = len(filtered_rounds_map) + (1 if final_gd else 0)
    ordered_rounds = []
    for rno in sorted(filtered_rounds_map.keys()):
        lbl = round_label(rno, total_normal, "normal")
        ordered_rounds.append({"round_no": rno, "label": lbl, "round_type": "normal", "groups": filtered_rounds_map[rno]})
    if final_gd:
        ordered_rounds.append({"round_no": final_gd.get("_round_no", 99), "label": "決勝", "round_type": "final", "groups": [final_gd]})

    # 3位決定戦・敗者復活戦をラウンド形式で整理
    # revival連番: 複数ラウンドある場合のみ R1, R2 を付与
    third_rounds_data = []
    revival_rnos = sorted([rno for rno in third_map if any(g.get("_revival") for g in third_map[rno])])
    third_rnos   = sorted([rno for rno in third_map if not any(g.get("_revival") for g in third_map[rno])])

    revival_count = len(revival_rnos)
    third_count   = len(third_rnos)

    for seq, rno in enumerate(revival_rnos, 1):
        label = f"敗者復活戦R{seq}" if revival_count > 1 else "敗者復活戦"
        third_rounds_data.append({
            "round_no": rno, "label": label,
            "round_type": "revival", "groups": third_map[rno],
        })
    for seq, rno in enumerate(third_rnos, 1):
        label = f"3位決定戦R{seq}" if third_count > 1 else "3位決定戦"
        third_rounds_data.append({
            "round_no": rno, "label": label,
            "round_type": "third", "groups": third_map[rno],
        })
    # round_no昇順に並べ直す
    third_rounds_data.sort(key=lambda x: x["round_no"])

    return {
        "rounds": ordered_rounds,
        "third_rounds": third_rounds_data,
    }


def _simulate_future_rounds(current_groups_data: list, total_finalists: int) -> list:
    """
    現在DBにある最終ラウンドの次から決勝まで、全ラウンドを空枠で返す。
    DBのラウンドに関わらず、グループ数から決勝まで全ラウンドを計算する。
    """
    if not current_groups_data:
        return []

    # DBにある全ラウンドのうち最後のnormalラウンドのグループ数を取得
    normal_rounds = [g for g in current_groups_data if g["round"]["round_type"] == "normal"]
    if not normal_rounds:
        return []

    last_round_no = max(g["round"]["round_no"] for g in normal_rounds)
    last_groups = [g for g in normal_rounds if g["round"]["round_no"] == last_round_no]
    n_winners = len(last_groups)

    if n_winners <= 1:
        return []

    # 決勝まで全ラウンドを生成
    future = []
    rno = last_round_no + 1
    n = n_winners
    while n > 1:
        patterns = combinations_2_3(n)
        if not patterns:
            break
        pat = patterns[0]
        groups = []
        for gno, sz in enumerate(pat, 1):
            slots = [{"slot_id": None, "slot_no": s, "name": None, "entry_id": None,
                      "rank": None, "qual_rank": None, "is_seed_slot": False}
                     for s in range(1, sz + 1)]
            groups.append({"slots": slots, "result": None})
        is_final = len(pat) == 1
        future.append({
            "round_no": rno,
            "label": "決勝" if is_final else f"ラウンド{rno}",
            "groups": groups,
            "round_type": "final" if is_final else "normal",
        })
        n = len(pat)
        rno += 1
        if is_final:
            break

    return future


def combinations_2_3(n: int) -> list[list[int]]:
    """
    n人を2と3だけで割り切れる組み合わせを列挙（重複なし・順序無視）
    例: n=8 → [[2,2,2,2],[2,3,3]]
        n=9 → [[3,3,3],[2,2,2,3]]
    """
    results = set()
    # a個の3、b個の2で 3a+2b=n を満たす非負整数解
    for a in range(n // 3 + 1):
        remainder = n - 3 * a
        if remainder >= 0 and remainder % 2 == 0:
            b = remainder // 2
            groups = sorted([3] * a + [2] * b, reverse=True)
            if groups:
                results.add(tuple(groups))
    return [list(r) for r in sorted(results, key=lambda x: (len(x), x))]


def _seed_assign(finalists: list[dict], group_sizes: list[int]) -> list[list[dict]]:
    """
    高シードと低シードを同グループに配置するシード割り当て。
    同一レーサー（racer_id）が同グループに入らないよう制御。
    （シードなしスロット同士の同一レーサー対戦を禁止）
    """
    n = len(finalists)
    n_groups = len(group_sizes)
    assigned = [[] for _ in range(n_groups)]
    cap = list(group_sizes)

    def racer_id(f):
        return f.get("racer_id") or f.get("entry_id")

    def group_has_racer(gi, f):
        rid = racer_id(f)
        return any(racer_id(x) == rid for x in assigned[gi])

    def find_group_spread(fi, prefer_order):
        """同一レーサーがいないグループを探す（できるだけ離れた位置に）"""
        f = finalists[fi]
        rid = racer_id(f)

        # 同一racer_idが既に入っているグループのインデックスを収集
        occupied_groups = [gi for gi in range(n_groups) if group_has_racer(gi, f)]

        if occupied_groups:
            # できるだけ離れたグループを優先（距離が最大のものから）
            def min_dist(gi):
                return min(abs(gi - og) for og in occupied_groups)
            candidates = sorted(
                [gi for gi in prefer_order if cap[gi] > 0 and not group_has_racer(gi, f)],
                key=lambda gi: -min_dist(gi)
            )
            if candidates:
                return candidates[0]
            # 回避不可なら通常の優先順序
            for gi in prefer_order:
                if cap[gi] > 0:
                    return gi
        else:
            # 初回配置は通常通り
            for gi in prefer_order:
                if cap[gi] > 0 and not group_has_racer(gi, f):
                    return gi
            for gi in prefer_order:
                if cap[gi] > 0:
                    return gi
        return None

    # 後方互換のエイリアス
    find_group = find_group_spread

    import random

    hi, lo = 0, n - 1
    while hi <= lo:
        # 高シードパス: G0→G1→...
        hi_order = list(range(n_groups))
        for _ in range(n_groups):
            if hi > lo: break
            gi = find_group(hi, hi_order)
            if gi is None: break
            assigned[gi].append(finalists[hi]); hi += 1; cap[gi] -= 1
            hi_order = [g for g in hi_order if g != gi] + [gi]

        # 低シードパス: G0→G1→...（高シードと同じ順）
        lo_order = list(range(n_groups))
        for _ in range(n_groups):
            if hi > lo: break
            gi = find_group(lo, lo_order)
            if gi is None: break
            assigned[gi].append(finalists[lo]); lo -= 1; cap[gi] -= 1
            lo_order = [g for g in lo_order if g != gi] + [gi]

    # 各グループ内のスロット順をランダムにシャッフル
    for g in assigned:
        random.shuffle(g)

    return assigned


async def _prefill_next_round(tid: int, round_id: int, round_no: int, db: aiosqlite.Connection):
    """
    現ラウンドの勝者を次ラウンドの対応スロットに仮反映する。
    R1グループiの勝者 → R2のi番目のnull（またはi番目のスロット）に入れる。
    """
    # 裏トーナメント（losers）は _sync_losers_bracket が専任。汎用の仮反映はしない。
    async with db.execute("SELECT round_type FROM bracket_rounds WHERE id=?", (round_id,)) as _c:
        _r = await _c.fetchone()
    if _r and _r["round_type"] == "losers":
        return

    # ── 新方式（固定リンク・v5.7+）──────────────────────────
    # 生成時に advance_to_slot_id が焼き付けられたトーナメントでは、
    # 各グループの勝者をリンク先スロットへ書き込むだけで進行する。
    # 組み合わせ（接続）は生成後一切変化しない。
    # 勝者未確定（取消）の場合はリンク先を NULL に戻す（決定論的）。
    # リンクが無い既存トーナメントは従来ロジック（後方互換）で処理する。
    async with db.execute(
        "SELECT id, advance_to_slot_id FROM bracket_groups WHERE round_id=? ORDER BY group_no",
        (round_id,),
    ) as cur:
        _link_groups = [dict(r) for r in await cur.fetchall()]
    if any(g.get("advance_to_slot_id") for g in _link_groups):
        for g in _link_groups:
            _adv = g.get("advance_to_slot_id")
            if not _adv:
                continue
            async with db.execute(
                """SELECT bs.entry_id FROM bracket_results br
                   JOIN bracket_slots bs ON bs.id=br.winner_slot_id
                   WHERE br.group_id=?""",
                (g["id"],),
            ) as cur:
                _w = await cur.fetchone()
            await db.execute(
                "UPDATE bracket_slots SET entry_id=? WHERE id=?",
                (_w["entry_id"] if _w else None, _adv),
            )
        await db.commit()
        return

    # 次ラウンドを取得
    async with db.execute(
        "SELECT id FROM bracket_rounds WHERE tournament_id=? AND round_no=? AND round_type NOT IN ('third','revival')",
        (tid, round_no + 1),
    ) as cur:
        next_round = await cur.fetchone()
    if not next_round:
        return  # 次ラウンド未生成

    # 現ラウンドの完了済みグループの勝者を収集（グループ順）
    async with db.execute(
        "SELECT * FROM bracket_groups WHERE round_id=? ORDER BY group_no", (round_id,)
    ) as cur:
        groups = await cur.fetchall()

    # グループ別勝者 (None=未確定)
    winners = []
    for g in groups:
        async with db.execute(
            """SELECT bs.entry_id FROM bracket_results br
               JOIN bracket_slots bs ON bs.id=br.winner_slot_id
               WHERE br.group_id=?""",
            (g["id"],),
        ) as cur:
            w = await cur.fetchone()
        winners.append(w["entry_id"] if w else None)

    # 次ラウンドの全スロットをグループ順・スロット順で取得
    async with db.execute(
        """SELECT bs.id as slot_id, bs.entry_id, bg.group_no, bs.slot_no
           FROM bracket_slots bs
           JOIN bracket_groups bg ON bg.id=bs.group_id
           WHERE bg.round_id=? ORDER BY bg.group_no, bs.slot_no""",
        (next_round["id"],),
    ) as cur:
        all_next_slots = [dict(r) for r in await cur.fetchall()]

    # ── シードスロットの判定 ──
    # 「現ラウンドに出場しているエントリーID」= 現ラウンドのスロットに存在するentry_id
    # シードスロット = 次ラウンドのスロットにいるが、現ラウンドには出場していないエントリー
    # （entries.seeded フラグは予選シード権であり、ブラケットのシード枠判定には使えない）
    async with db.execute(
        """SELECT DISTINCT bs.entry_id FROM bracket_slots bs
           JOIN bracket_groups bg ON bg.id=bs.group_id
           WHERE bg.round_id=? AND bs.entry_id IS NOT NULL""",
        (round_id,),
    ) as cur:
        current_round_eids = {r["entry_id"] for r in await cur.fetchall()}

    # ht_finalist_seedsも確認（heat_tournament形式のシード判定）
    ht_seed_eids: set = set()
    async with db.execute(
        "SELECT entry_id FROM ht_finalist_seeds WHERE seeded=1"
    ) as hc:
        for row in await hc.fetchall():
            ht_seed_eids.add(row["entry_id"])

    # entry.seeded 値を引いて is_seed_slot に設定（1=シード, 2=スーパーシード）
    _seeded_map = {}
    if all_next_slots:
        _eids = [s.get("entry_id") for s in all_next_slots if s.get("entry_id")]
        if _eids:
            _ph = ",".join("?" * len(_eids))
            async with db.execute(f"SELECT id, seeded FROM entries WHERE id IN ({_ph})", _eids) as _sc:
                for _r in await _sc.fetchall():
                    _seeded_map[_r["id"]] = _r["seeded"]

    for s in all_next_slots:
        eid = s.get("entry_id")
        if eid is None:
            s["is_seed_slot"] = 0
        elif eid in ht_seed_eids:
            s["is_seed_slot"] = _seeded_map.get(eid, 1)
        elif eid not in current_round_eids:
            # 現ラウンドに出場していない = ブラケットシード（直接次ラウンドから参加）
            s["is_seed_slot"] = _seeded_map.get(eid, 1)
        else:
            s["is_seed_slot"] = 0

    # シードでないスロット（グループ番号順・スロット番号順）= 現ラウンド勝者の配置先
    non_seed_slots = [s for s in all_next_slots if not s.get("is_seed_slot", 0)]

    if not non_seed_slots:
        await db.commit()
        return

    # revivalモードで次が決勝の場合の特別処理
    async with db.execute(
        "SELECT round_type FROM bracket_rounds WHERE id=?", (next_round["id"],)
    ) as cur:
        next_rt_row2 = await cur.fetchone()
    next_rt2 = next_rt_row2["round_type"] if next_rt_row2 else "normal"

    is_revival_to_final = False
    is_semi_to_revival_final = False
    if next_rt2 == "final":
        async with db.execute(
            "SELECT bracket_mode FROM tournaments WHERE id=?", (tid,)
        ) as cur:
            bm_row2 = await cur.fetchone()
        b_mode2 = bm_row2["bracket_mode"] if bm_row2 else "third_place"
        if b_mode2 == "revival":
            # 現ラウンドが復活戦か準決勝かで処理を分ける
            async with db.execute(
                "SELECT round_type FROM bracket_rounds WHERE id=?", (round_id,)
            ) as cur:
                cur_rt_row = await cur.fetchone()
            cur_rt = cur_rt_row["round_type"] if cur_rt_row else "normal"
            if cur_rt == "revival":
                is_revival_to_final = True   # 復活戦→決勝
            else:
                is_semi_to_revival_final = True  # 準決勝→決勝（revivalモード）

    if is_semi_to_revival_final:
        # ── 準決勝→決勝(revivalモード) ──
        # 最後のスロットは復活戦勝者用に予約し、残りに準決勝勝者を配置する。
        available_slots = all_next_slots[:-1]  # 最後の1枠を復活戦用に残す

        # 既に正しく配置済みのentry_id
        already_placed_eids = {
            s["entry_id"] for s in available_slots if s["entry_id"] is not None
        }

        # まだ配置されていない勝者のみ
        to_place = [
            eid for eid in winners
            if eid is not None and eid not in already_placed_eids
        ]

        # 空きスロットに順番に配置
        empty_slots = [s for s in available_slots if s["entry_id"] is None]
        for i, eid in enumerate(to_place):
            if i >= len(empty_slots):
                break
            await db.execute(
                "UPDATE bracket_slots SET entry_id=? WHERE id=? AND entry_id IS NULL",
                (eid, empty_slots[i]["slot_id"]),
            )
        await db.commit()
        return

    # ── 通常モード ──
    # 画面側の接続線描画（drawBracketConnectors）と同一の考え方に統一する。
    # すなわち「次ラウンドのグループごとの非シードスロット数」を基準に、
    # 現ラウンドのグループ勝者（group_no順）を先頭から順番に消費して割り当てる。
    #
    # 以前は non_seed_slots を全グループ分フラット化したうえで、
    # 現ラウンドのグループ番号 gi をそのままインデックスに使っていたため、
    # 各次グループの非シードスロット数が不均一な構成で対応がズレ、
    # 接続線（見た目）と実配置が食い違っていた。
    if is_revival_to_final:
        # 復活戦→決勝: 空きスロットの最後に配置（準決勝勝者のスロットは触らない）
        placed_this_run: set = set()
        for winner_eid in winners:
            if winner_eid is None or winner_eid in placed_this_run:
                continue
            empty_ns = [s for s in non_seed_slots if s["entry_id"] is None]
            if not empty_ns:
                continue
            target = empty_ns[-1]
            await db.execute(
                "UPDATE bracket_slots SET entry_id=? WHERE id=?",
                (winner_eid, target["slot_id"]),
            )
            placed_this_run.add(winner_eid)
        await db.commit()
        return

    # 次ラウンドの非シードスロットを group_no ごとにまとめる（group_no昇順・slot_no昇順）
    next_groups_nonseed: dict[int, list[dict]] = {}
    for s in non_seed_slots:
        next_groups_nonseed.setdefault(s["group_no"], []).append(s)
    for gno in next_groups_nonseed:
        next_groups_nonseed[gno].sort(key=lambda x: x.get("slot_no", 0))
    ordered_next_group_nos = sorted(next_groups_nonseed.keys())

    placed_this_run: set = set()
    winner_idx = 0  # winners（現ラウンドの group_no 順）の消費位置
    for next_gno in ordered_next_group_nos:
        for target in next_groups_nonseed[next_gno]:
            # 未確定(None)や既配置の勝者は読み飛ばし、次の有効な勝者を1名取り出す
            winner_eid = None
            while winner_idx < len(winners):
                candidate = winners[winner_idx]
                winner_idx += 1
                if candidate is not None and candidate not in placed_this_run:
                    winner_eid = candidate
                    break
            if winner_eid is None:
                continue
            # 上書き配置（結果変更にも対応するため NULL チェックなしで更新）
            await db.execute(
                "UPDATE bracket_slots SET entry_id=? WHERE id=?",
                (winner_eid, target["slot_id"]),
            )
            placed_this_run.add(winner_eid)

    await db.commit()


async def _fill_r2_slots(tid: int, r2_round_id: int, r1_groups: list, db: aiosqlite.Connection):
    """
    ラウンド1完了時に、先行生成済みのラウンド2のNullスロット（R1勝者枠）を埋める。
    R1の各グループの勝者をR2のNullスロットに順番に配置する。
    """
    # R1勝者リスト（グループ順）
    r1_winners = []
    for g in r1_groups:
        async with db.execute(
            """SELECT bs.entry_id FROM bracket_results br
               JOIN bracket_slots bs ON bs.id=br.winner_slot_id
               WHERE br.group_id=?""",
            (g["id"],),
        ) as cur:
            w = await cur.fetchone()
        if w:
            r1_winners.append(w["entry_id"])

    # R2のNullスロットを取得（entry_id IS NULL かつ is_bye=0）
    async with db.execute(
        """SELECT bg.group_no, bs.id as slot_id
           FROM bracket_groups bg
           JOIN bracket_slots bs ON bs.group_id=bg.id
           WHERE bg.round_id=? AND bs.entry_id IS NULL
           ORDER BY bg.group_no, bs.slot_no""",
        (r2_round_id,),
    ) as cur:
        null_slots = await cur.fetchall()

    # R2の全nullスロット（埋まっていないもの）を取得し直す
    async with db.execute(
        """SELECT bg.group_no, bs.id as slot_id, bs.slot_no
           FROM bracket_groups bg
           JOIN bracket_slots bs ON bs.group_id=bg.id
           WHERE bg.round_id=? AND bs.entry_id IS NULL
           ORDER BY bg.group_no, bs.slot_no""",
        (r2_round_id,),
    ) as cur:
        remaining_null_slots = [dict(r) for r in await cur.fetchall()]

    # R1の全nullスロット（元々nullだったスロット・グループ順）も取得
    # → 何番目のR1グループが何番目のnullスロットに対応するかを判定
    async with db.execute(
        """SELECT bg.group_no, bs.id as slot_id, bs.slot_no
           FROM bracket_groups bg
           JOIN bracket_slots bs ON bs.group_id=bg.id
           WHERE bg.round_id=?
           ORDER BY bg.group_no, bs.slot_no""",
        (r2_round_id,),
    ) as cur:
        all_r2_slots = [dict(r) for r in await cur.fetchall()]

    # nullになっているスロットのgroup_noを特定
    null_group_nos = [s["group_no"] for s in remaining_null_slots]

    # R1勝者をnullスロットのグループに対応付け
    # R1グループ番号とR2グループ番号の対応は生成順（G1→G1null, G2→G2null）
    null_gi = 0  # null_group_nosのインデックス
    for wi, winner_eid in enumerate(r1_winners):
        if null_gi >= len(remaining_null_slots):
            break
        if winner_eid is None:
            null_gi += 1
            continue
        # wi番目のR1グループの勝者をnull_gi番目のnullスロットに入れる
        # ただし既に埋まっているR1グループ（_prefill済み）はスキップ
        # → nullスロットが残っている数だけ未処理のR1グループがある
        slot = remaining_null_slots[null_gi]
        await db.execute(
            "UPDATE bracket_slots SET entry_id=? WHERE id=?",
            (winner_eid, slot["slot_id"]),
        )
        null_gi += 1


def _assign_r2_with_seeds(r1_winner_count: int, seeded_players: list, group_sizes: list) -> list:
    """
    ラウンド2のグループ配置。
    - シード者は必ず別グループに1人ずつ配置（同グループ厳禁）
    - R1勝者スロット（None）はグループに均等に配置
    """
    n_groups = len(group_sizes)
    n_seeds = len(seeded_players)

    # シードを各グループに1人ずつ循環配置
    # G0→G1→G2→...→G0→G1... の順で割り当て
    seed_in_group = {gi: [] for gi in range(n_groups)}
    for i, sp in enumerate(seeded_players):
        gi = i % n_groups  # 必ず異なるグループに入る
        seed_in_group[gi].append(sp)

    result = []
    for gi in range(n_groups):
        sz = group_sizes[gi]
        seeds_here = seed_in_group[gi]
        r1_here = sz - len(seeds_here)
        group = []
        # R1勝者プレースホルダーを前に
        for _ in range(r1_here):
            group.append(None)
        # シード者を後ろに
        for s in seeds_here:
            group.append(s)
        result.append(group)

    return result


def round_label(round_no: int, total_rounds: int, round_type: str) -> str:
    if round_type == 'final':
        return '決勝'
    if round_type == 'third':
        return '3位決定戦'
    if round_type == 'revival':
        return '敗者復活戦'
    if round_type == 'losers':
        return f'裏R{round_no - LOSERS_ROUND_BASE}'
    diff = total_rounds - round_no
    if diff == 1:
        return '準決勝'
    if diff == 2:
        return '準々決勝'
    return f'ラウンド{round_no}'


# ── 結果入力（インライン）────────────────────────────────
@router.post("/{tid}/bracket/next-round/generate")
async def bracket_next_round_generate(
    tid: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    """次ラウンドをパターン指定で生成"""
    form = await request.form()
    pattern_str = form.get("pattern", "")
    round_no = int(form.get("round_no", 0))
    winners_str = form.get("winners", "")
    losers_str = form.get("losers", "")

    if not pattern_str or not winners_str:
        return RedirectResponse(url=f"/admin/tournaments/{tid}/bracket", status_code=303)

    next_pattern = [int(x) for x in pattern_str.split(",") if x.strip()]
    winners = [int(x) for x in winners_str.split(",") if x.strip()]
    losers = [int(x) for x in losers_str.split(",") if x.strip()]

    is_next_final = len(next_pattern) == 1
    round_type = "final" if is_next_final else "normal"

    import random as _r
    _r.shuffle(winners)

    # 次ラウンド生成
    await db.execute(
        "INSERT INTO bracket_rounds (tournament_id, round_no, round_type) VALUES (?,?,?)",
        (tid, round_no, round_type),
    )
    async with db.execute("SELECT last_insert_rowid() as id") as cur:
        next_round_id = (await cur.fetchone())["id"]

    w_idx = 0
    for gno, sz in enumerate(next_pattern, 1):
        await db.execute("INSERT INTO bracket_groups (round_id, group_no) VALUES (?,?)", (next_round_id, gno))
        async with db.execute("SELECT last_insert_rowid() as id") as cur:
            gid = (await cur.fetchone())["id"]
        for sno in range(1, sz + 1):
            eid = winners[w_idx] if w_idx < len(winners) else None
            await db.execute("INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (gid, sno, eid))
            w_idx += 1

    # 3位決定戦（決勝2名かつ敗者2名以上）
    if is_next_final and len(next_pattern[0:1]) == 1 and next_pattern[0] == 2 and len(losers) >= 2:
        lp = combinations_2_3(len(losers))
        if lp:
            await db.execute(
                "INSERT INTO bracket_rounds (tournament_id, round_no, round_type) VALUES (?,?,?)",
                (tid, round_no, "third"),
            )
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                trid = (await cur.fetchone())["id"]
            li = 0
            for gno, sz in enumerate(lp[0], 1):
                await db.execute("INSERT INTO bracket_groups (round_id, group_no) VALUES (?,?)", (trid, gno))
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    tgid = (await cur.fetchone())["id"]
                for sno in range(1, sz + 1):
                    eid = losers[li] if li < len(losers) else None
                    await db.execute("INSERT INTO bracket_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (tgid, sno, eid))
                    li += 1

    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}/bracket", status_code=303)


async def _delete_bracket(tid: int, db: aiosqlite.Connection):
    async with db.execute(
        "SELECT id FROM bracket_rounds WHERE tournament_id=?", (tid,)
    ) as cur:
        rids = [r["id"] for r in await cur.fetchall()]
    if not rids:
        return
    ph = ",".join("?" * len(rids))
    async with db.execute(
        f"SELECT id FROM bracket_groups WHERE round_id IN ({ph})", rids
    ) as cur:
        gids = [r["id"] for r in await cur.fetchall()]
    if gids:
        gph = ",".join("?" * len(gids))
        await db.execute(f"DELETE FROM bracket_slot_ranks WHERE group_id IN ({gph})", gids)
        await db.execute(f"DELETE FROM bracket_results WHERE group_id IN ({gph})", gids)
        async with db.execute(
            f"SELECT id FROM bracket_slots WHERE group_id IN ({gph})", gids
        ) as cur:
            sids = [r["id"] for r in await cur.fetchall()]
        if sids:
            sph = ",".join("?" * len(sids))
            await db.execute(f"DELETE FROM bracket_slots WHERE id IN ({sph})", sids)
        await db.execute(f"DELETE FROM bracket_groups WHERE id IN ({gph})", gids)
    await db.execute(f"DELETE FROM bracket_rounds WHERE id IN ({ph})", rids)
    await db.commit()


@router.post("/{tid}/bracket/clear-final-result")
async def bracket_clear_final_result(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """決勝・3位決定戦の順位結果のみNULLに（ラウンド構成は残す）"""
    async with db.execute(
        """SELECT bg.id FROM bracket_groups bg
           JOIN bracket_rounds br ON br.id=bg.round_id
           WHERE br.tournament_id=? AND br.round_type IN ('final','third')""",
        (tid,),
    ) as cur:
        gids = [r["id"] for r in await cur.fetchall()]
    if gids:
        ph = ",".join("?" * len(gids))
        await db.execute(f"DELETE FROM bracket_slot_ranks WHERE group_id IN ({ph})", gids)
        await db.execute(f"DELETE FROM bracket_results WHERE group_id IN ({ph})", gids)
    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}/bracket", status_code=303)


@router.post("/{tid}/bracket/reset")
async def bracket_reset(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    await _delete_bracket(tid, db)
    # くじ待ち状態も解除（枠を消したので未確定フラグを落とす）
    await db.execute("UPDATE tournaments SET lottery_pending=0 WHERE id=?", (tid,))
    await db.commit()
    # bracket_modeはDBに保持したまま（次回生成時にテンプレートで pre-select される）
    return RedirectResponse(url=f"/admin/tournaments/{tid}/bracket", status_code=303)


# ── SVGトーナメント図生成 ─────────────────────────────────
from fastapi.responses import Response

@router.get("/{tid}/bracket/svg")
async def bracket_svg(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """SVG形式のトーナメント表を生成して返す"""
    rounds_db = await _get_rounds(tid, db)
    if not rounds_db:
        return Response(content='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="60"><text x="20" y="35" font-size="13" fill="#95a5a6">トーナメントが開始されていません</text></svg>', media_type="image/svg+xml")

    # 固定リンク未焼き付けの既存トーナメントを描画前に自己修復（安全な場合のみ）
    await _ensure_advance_links_baked(tid, db)
    groups_data = []
    for rnd in rounds_db:
        async with db.execute(
            "SELECT * FROM bracket_groups WHERE round_id=? ORDER BY group_no", (rnd["id"],)
        ) as cur:
            groups = await cur.fetchall()
        for g in groups:
            slots, result, ranks = await _get_group_detail(g["id"], db)
            groups_data.append({"round": dict(rnd), "group": dict(g), "slots": slots, "result": result, "ranks": ranks})

    svg_data = _build_svg_data(groups_data)
    # DBに決勝ラウンドがなく、かつ最終ラウンドが1グループでもない場合のみシミュレート
    # （DBに全ラウンドが揃っている場合はシミュレート不要）
    has_final = any(r.get("round_type") == "final" or r.get("label") == "決勝"
                    for r in svg_data["rounds"])
    last_rnd_single = svg_data["rounds"] and len(svg_data["rounds"][-1]["groups"]) == 1
    if svg_data["rounds"] and not has_final and not last_rnd_single:
        future = _simulate_future_rounds(groups_data, 0)
        svg_data["rounds"].extend(future)
    # 全ラウンドラベルを総ラウンド数から再計算
    total_all = len(svg_data["rounds"])
    for i, r in enumerate(svg_data["rounds"]):
        rt = r.get("round_type", "normal")
        if rt == "final" or r["label"] == "決勝":
            r["label"] = "決勝"
        else:
            r["label"] = round_label(r["round_no"], total_all, rt)

    svg_data["qualifying_type"] = dict(await (await db.execute("SELECT qualifying_type FROM tournaments WHERE id=?", (tid,))).fetchone()).get("qualifying_type","")
    svg = _render_svg(svg_data)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )




@router.get("/{tid}/bracket/html", response_class=HTMLResponse)
async def bracket_html(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """HTML/CSS形式のトーナメント表（フレックスボックス自動レイアウト）"""
    rounds_db_all = await _get_rounds(tid, db)
    rounds_db = [r for r in rounds_db_all if r["round_type"] != "losers"]
    losers_rounds_db = [r for r in rounds_db_all if r["round_type"] == "losers"]
    if not rounds_db:
        return HTMLResponse('<div style="padding:30px;text-align:center;color:#95a5a6">トーナメントが開始されていません</div>')

    # 固定リンク未焼き付けの既存トーナメントを描画前に自己修復（安全な場合のみ）
    await _ensure_advance_links_baked(tid, db)
    groups_data = []
    for rnd in rounds_db:
        async with db.execute(
            "SELECT * FROM bracket_groups WHERE round_id=? ORDER BY group_no", (rnd["id"],)
        ) as cur:
            groups = await cur.fetchall()
        for g in groups:
            slots, result, ranks = await _get_group_detail(g["id"], db)
            groups_data.append({"round": dict(rnd), "group": dict(g), "slots": slots, "result": result, "ranks": ranks})

    svg_data = _build_svg_data(groups_data)
    async with db.execute("SELECT COUNT(*) as cnt FROM entries WHERE tournament_id=? AND status='active'", (tid,)) as _c:
        _row = await _c.fetchone()
        svg_data["total_finalists"] = _row["cnt"] if _row else 0
    # ラベルを総ラウンド数から正しく付与（決勝・準決勝・準々決勝・ラウンドN）
    # ラベルを付与: round_noの最大値=決勝、その1つ前=準決勝、2つ前=準々決勝
    # ラベル：大きいround_no順に決勝・準決勝・準々決勝・ラウンドN
    sorted_rnos = sorted([r["round_no"] for r in svg_data["rounds"]], reverse=True)
    for i, rno in enumerate(sorted_rnos):
        r = next(x for x in svg_data["rounds"] if x["round_no"] == rno)
        if r.get("round_type") == "third":
            r["label"] = "3位決定戦"
        elif r.get("round_type") == "revival":
            r["label"] = "敗者復活戦"
        elif i == 0:
            r["label"] = "決勝"
            r["round_type"] = "final"
        elif i == 1:
            r["label"] = "準決勝"
        elif i == 2:
            r["label"] = "準々決勝"
        else:
            r["label"] = f"ラウンド{rno}"

    # 裏トーナメント（ルーザーズブラケット）を svg_data に付与
    losers_groups_data = []
    for rnd in losers_rounds_db:
        async with db.execute(
            "SELECT * FROM bracket_groups WHERE round_id=? ORDER BY group_no", (rnd["id"],)
        ) as cur:
            lgroups = await cur.fetchall()
        for g in lgroups:
            slots, result, ranks = await _get_group_detail(g["id"], db)
            losers_groups_data.append({"round": dict(rnd), "group": dict(g), "slots": slots, "result": result, "ranks": ranks})
    if losers_groups_data:
        lsvg = _build_svg_data(losers_groups_data)
        # _build_svg_data は勝者ブラケット用に round_no を再計算するため、裏ラウンド
        # （101,102,103…）では誤った round_no（例: 99→裏R-1）になる。
        # 実DBの round_no（losers_rounds_db を round_no 昇順）を並び順で割り当て直す。
        real_nos = [r["round_no"] for r in losers_rounds_db]
        for i, r in enumerate(lsvg["rounds"]):
            r["round_type"] = "losers"
            rno = real_nos[i] if i < len(real_nos) else r["round_no"]
            r["round_no"] = rno
            r["label"] = round_label(rno, 0, "losers")
        svg_data["losers_rounds"] = lsvg["rounds"]
    else:
        svg_data["losers_rounds"] = []

    return HTMLResponse(_render_html_bracket(svg_data, tid=tid))


def _render_html_bracket(svg_data: dict, tid: int = 0, winner_js_func: str = "setWinner", winner_js_extra_args: str = "", compact: bool = False) -> str:
    """flexboxベースのHTML/CSSトーナメント表（コネクタ線・勝ち上がり強調付き）

    compact=True のとき、表彰台（br-podium）を約半分の高さに縮小する（admin画面用）。
    """
    rounds = svg_data.get("rounds", [])
    third_rounds = svg_data.get("third_rounds", [])

    if not rounds:
        return '<div style="padding:30px;text-align:center;color:#95a5a6">データなし</div>'

    # ── 固定リンク（advance_to_slot_id）から「各グループの勝者が入る次ラウンドの
    #    グループ番号」を解決する。結線・レイアウトJSはこの対応を唯一の真実として使う。
    #    （従来のJS側ヒューリスティック＝非シード枠数からの再構成は、シード配分により
    #     実配置＝焼き付けリンクと食い違うことがあった。その不一致を根絶する。）
    _slot_pos: dict = {}  # slot_id -> (round_idx, group_idx)
    for _ri, _rnd in enumerate(rounds):
        for _gi, _g in enumerate(_rnd["groups"]):
            for _s in _g["slots"]:
                _sid = _s.get("slot_id")
                if _sid is not None:
                    _slot_pos[_sid] = (_ri, _gi)
    # (round_idx, group_idx) -> 次ラウンドのグループ番号（隣接する次ラウンドへのリンクのみ採用）
    _adv_group_of: dict = {}
    for _ri, _rnd in enumerate(rounds):
        for _gi, _g in enumerate(_rnd["groups"]):
            _adv = _g.get("advance_to_slot_id")
            if _adv is not None and _adv in _slot_pos:
                _tri, _tgi = _slot_pos[_adv]
                if _tri == _ri + 1:
                    _adv_group_of[(_ri, _gi)] = _tgi

    # ポジウム（1〜3位）を特定
    champion_name = runner_up_name = third_name = None
    if rounds:
        last_rnd = rounds[-1]
        for g in last_rnd["groups"]:
            for s in g["slots"]:
                r = s.get("rank")
                if r == 1 and not champion_name: champion_name = s.get("name")
                elif r == 2 and not runner_up_name: runner_up_name = s.get("name")
                elif r == 3 and not third_name: third_name = s.get("name")
    # 3位決定戦がある場合はそちらのrank=1が3位
    if third_rounds and not third_name:
        for tr in third_rounds:
            for g in tr["groups"]:
                for s in g["slots"]:
                    if s.get("rank") == 1 and not third_name:
                        third_name = s.get("name")

    def esc(n): return (n or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    # 表彰台サイズ。compact: 0=通常 / 1=約半分(admin) / 2=さらに小(viewer)
    _cl = int(compact) if not isinstance(compact, bool) else (1 if compact else 0)
    if _cl >= 2:
        _pod = {
            "mb": "6px", "gap": "6px",
            "ch_pad": "3px 10px", "ch_label": "9px", "ch_name": "13px", "ch_mt": "0px",
            "ru_pad": "3px 8px", "ru_label": "8px", "ru_name": "11px", "ru_mt": "0px",
        }
    elif _cl == 1:
        _pod = {
            "mb": "5px", "gap": "8px",
            "ch_pad": "2px 14px", "ch_label": "10px", "ch_name": "15px", "ch_mt": "1px",
            "ru_pad": "2px 12px", "ru_label": "9px", "ru_name": "13px", "ru_mt": "1px",
        }
    else:
        _pod = {
            "mb": "12px", "gap": "12px",
            "ch_pad": "7px 20px", "ch_label": "11px", "ch_name": "24px", "ch_mt": "4px",
            "ru_pad": "6px 18px", "ru_label": "11px", "ru_name": "20px", "ru_mt": "4px",
        }

    css = """
    <style>
    .br-wrap { background:#ffffff; color:#212529; padding:20px 16px; border-radius:8px; overflow-x:auto; overflow-y:visible; }
    /* ポジウム */
    .br-podium { display:flex; gap:%(gap)s; flex-wrap:wrap; margin-bottom:%(mb)s; }
    .br-champion { background:linear-gradient(135deg,#fff8d6,#ffe066); border:2px solid #d4a017; border-radius:10px; padding:%(ch_pad)s; flex:1; min-width:160px; text-align:center; box-shadow:0 4px 16px rgba(212,160,23,0.35); }
    .br-champion-label { font-size:%(ch_label)s; color:#a87a00; font-weight:bold; letter-spacing:2px; line-height:1; }
    .br-champion-name { font-size:%(ch_name)s; font-weight:900; color:#7a5500; margin-top:%(ch_mt)s; }
    .br-runner-up { background:linear-gradient(135deg,#f5f5f5,#dcdcdc); border:2px solid #999; border-radius:10px; padding:%(ru_pad)s; flex:1; min-width:140px; text-align:center; }
    .br-runner-up-label { font-size:%(ru_label)s; color:#666; font-weight:bold; }
    .br-runner-up-name { font-size:%(ru_name)s; font-weight:bold; color:#333; margin-top:%(ru_mt)s; }
    .br-third-pod { background:linear-gradient(135deg,#fdf0e0,#f5c97a); border:2px solid #cd7f32; border-radius:10px; padding:%(ru_pad)s; flex:1; min-width:140px; text-align:center; }
    .br-third-pod-label { font-size:%(ru_label)s; color:#7a4a10; font-weight:bold; }
    .br-third-pod-name { font-size:%(ru_name)s; font-weight:bold; color:#5a3a10; margin-top:%(ru_mt)s; }""" % _pod + """
    /* レイアウト */
    .bracket-outer { position:relative; overflow:visible; width:100%; }
    .bracket-html { display:flex; gap:0; padding:6px 2px; align-items:flex-start; position:relative; width:max-content; min-width:100%; }
    .br-round { display:flex; flex-direction:column; min-width:250px; max-width:300px; gap:0; flex:0 0 auto; position:relative; padding:0 24px; }
    @media(max-width:480px){ .bracket-html { padding:4px 0!important; } .br-round { min-width:172px!important; max-width:210px!important; padding:0 10px!important; } .br-round-label { font-size:14px!important; padding:4px 0 16px!important; } .br-group { padding:5px 3px 3px 10px!important; } .br-slot { padding:5px 9px!important; gap:6px!important; min-height:32px!important; } .br-slot-name { font-size:14px!important; min-width:0!important; overflow:hidden!important; text-overflow:ellipsis!important; white-space:nowrap!important; } .br-slot-no { font-size:12px!important; } }
    /* スマホ：表彰台（1・2・3位）を折り返さず横一列に収める */
    @media(max-width:480px){
      .br-podium { flex-wrap:nowrap!important; gap:6px!important; }
      .br-champion { min-width:0!important; padding:8px 4px!important; }
      .br-runner-up, .br-third-pod { min-width:0!important; padding:6px 4px!important; }
      .br-champion-name, .br-runner-up-name, .br-third-pod-name { font-size:15px!important; }
      .br-champion-label, .br-runner-up-label, .br-third-pod-label { font-size:10px!important; letter-spacing:1px!important; }
    }
    .br-round-label { font-size:18px; font-weight:bold; color:#2980b9 !important; text-align:center; padding:6px 0 22px; letter-spacing:0; line-height:1.3; position:relative; z-index:2; background:transparent; }
    .br-round-label.final { color:#d4a017 !important; font-size:20px; }
    .br-round-groups { display:flex; flex-direction:column; flex:1; justify-content:flex-start; gap:0; position:relative; min-height:90px; padding-top:10px; overflow:visible; }
    /* グループ枠 */
    .br-group { background:#fff; border:1px solid #ced4da; border-radius:9px; padding:6px 3px 3px 13px; display:flex; flex-direction:column; gap:3px; box-shadow:0 1px 3px rgba(0,0,0,0.07); position:absolute; left:0; right:0; overflow:visible; }
    .br-group.has-winner { border-color:#27ae60; border-width:2px; box-shadow:0 0 0 2px rgba(39,174,96,0.15); }
    .br-group.is-final { border-color:#d4a017; border-width:2px; box-shadow:0 0 0 2px rgba(212,160,23,0.15); }
    /* スロット共通 */
    .br-slot { display:flex; align-items:center; gap:9px; padding:6px 12px; background:#f8f9fa; border-radius:6px; font-size:18px; min-height:39px; border:1px solid #dee2e6; }
    .br-slot-no { display:none; }
    .br-slot-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:18px; color:#212529 !important; font-weight:500; }
    .br-slot-mark { font-size:20px; }
    /* 1位：濃い緑・白文字・強調 */
    /* シード（winnerより前に宣言してwinnerが上書きできるようにする） */
    .br-slot.seed { background:linear-gradient(90deg,#fff8e1,#ffe8a3); border-color:#d4a017; }
    .br-slot.super-seed { background:linear-gradient(90deg,#f5eeff,#e0b8ff); border-color:#8e44ad; }
    .br-slot.super-seed .br-slot-no::after { content:"◎"; color:#8e44ad; margin-left:2px; }
    .br-slot.seed .br-slot-no::after { content:"★"; color:#d4a017; margin-left:2px; }
    /* 勝者（seedより後に宣言して必ず優先） */
    .br-slot.winner { background:linear-gradient(90deg,#1e8449,#27ae60); border-color:#1a7a40; font-weight:bold; box-shadow:0 2px 8px rgba(39,174,96,0.45); }
    .br-slot.winner .br-slot-no { color:rgba(255,255,255,0.75) !important; }
    .br-slot.winner .br-slot-name { color:#ffffff !important; font-size:18px; font-weight:700; }
    .br-slot.winner .br-slot-mark { color:#ffffff; }
    /* シード勝者：winner色を維持しつつ★マークを表示 */
    .br-slot.winner.seed .br-slot-no::after { content:"★"; color:#ffe8a3; margin-left:2px; }
    .br-slot.winner.super-seed .br-slot-no::after { content:"◎"; color:#e0b8ff; margin-left:2px; }
    /* 2位：グレーアウト・取り消し線 */
    .br-slot.runner-up { background:#e9ecef; border-color:#ced4da; opacity:0.7; }
    .br-slot.runner-up .br-slot-name { color:#6c757d !important; }
    .br-slot.runner-up .br-slot-no { color:#adb5bd !important; }
    /* 3位：薄茶・取り消し線 */
    .br-slot.third { background:#f5e6d0; border-color:#cd7f32; opacity:0.75; }
    .br-slot.third .br-slot-name { color:#8a5c2a !important; }
    /* 空枠（進出者未確定） */
    .br-slot.empty { background:#f0f4f8; border-color:#dee2e6; border-style:dashed; }
    .br-slot.empty .br-slot-no { color:#ced4da !important; }
    .br-slot.empty .br-slot-name { color:#adb5bd !important; font-style:italic; letter-spacing:2px; }
    /* シード */
    /* コネクタSVG */
    .br-connector-svg { position:absolute; top:0; left:0; pointer-events:none; overflow:visible; }
    /* 3位決定戦 */
    .br-third-section { margin-top:32px; padding:20px 16px; border:2px dashed #cd7f32; border-radius:10px; background:#fefaf5; }
    .br-third-title { font-size:15px; font-weight:bold; color:#cd7f32; margin-bottom:14px; text-align:center; }
    /* 裏トーナメント（敗者復活）：グレー基調で表と区別 */
    .br-losers-section { margin-top:32px; padding:20px 16px; border:2px dashed #95a5a6; border-radius:10px; background:#f4f5f7; }
    .br-losers-title { font-size:16px; font-weight:bold; color:#5f6b76; margin-bottom:14px; text-align:center; letter-spacing:1px; }
    .br-losers-section .br-round-label { color:#6c757d !important; }
    .br-losers-section .br-group { background:#fbfbfc; }
    </style>
    """

    def render_slot(s, is_final, winner_sid=None, group_id=None):
        rank = s.get("rank")
        sid = s.get("slot_id")
        # 勝者判定: rank=1 OR winner_slot_idと一致
        is_winner = (rank == 1) or (winner_sid is not None and sid is not None and sid == winner_sid)
        # 敗者判定: rank>1（bracket_slot_ranksに記録済み）OR winner_sidがあり勝者でない
        is_loser = not is_winner and bool(s.get("name")) and (
            (rank is not None and rank > 1) or
            (winner_sid is not None)
        )
        cls = "br-slot"
        if is_winner:
            cls += " winner"
        elif is_loser:
            if rank == 2: cls += " runner-up"
            elif rank == 3: cls += " third"
            else: cls += " runner-up"
        if s.get("is_seed_slot") == 2: cls += " super-seed"
        elif s.get("is_seed_slot"): cls += " seed"
        if not s.get("name"): cls += " empty"
        is_seed_attr = ' data-is-seed="true"' if s.get("is_seed_slot") else ""
        name = esc(s.get("name")) if s.get("name") else "未確定"
        if is_winner:
            mark = '<span class="br-slot-mark">🏆</span>' if is_final else '<span class="br-slot-mark">✓</span>'
        elif rank == 2 and is_final:
            mark = '<span class="br-slot-mark">🥈</span>'
        elif rank == 3 and is_final:
            mark = '<span class="br-slot-mark">🥉</span>'
        else:
            mark = ""
        # クリックで勝者設定（名前があり、決勝以外のラウンドのみ）
        onclick_attr = ""
        slot_id_val = s.get("slot_id")
        if group_id and tid and slot_id_val and s.get("name") and not is_final:
            extra = ("," + winner_js_extra_args) if winner_js_extra_args else ""
            onclick_attr = ' onclick="%s(%d,%d,%d%s)" style="cursor:pointer"' % (winner_js_func, group_id, slot_id_val, tid, extra)
        return (
            '<div class="' + cls + '"' + onclick_attr + is_seed_attr + '>'
            + '<span class="br-slot-no">' + str(s.get("slot_no","")) + '</span>'
            + '<span class="br-slot-name">' + name + '</span>'
            + mark + '</div>'
        )

    def render_round(rnd, is_last, ri=None):
        is_final = rnd.get("round_type") == "final" or rnd.get("label") == "決勝"
        label_class = "final" if is_final else ""
        groups_html = []
        for gi, g in enumerate(rnd["groups"]):
            result = g.get("result")
            winner_sid = result["winner_slot_id"] if result else None
            group_id = g.get("group_id")
            # rank=1 または winner_slot_id一致で勝者とみなす
            has_winner = (winner_sid is not None) or any(s.get("rank") == 1 for s in g["slots"])
            slots_html = "".join(render_slot(s, is_final, winner_sid, group_id) for s in g["slots"])
            grp_cls = "br-group"
            if has_winner: grp_cls += " has-winner"
            if is_final: grp_cls += " is-final"
            # 勝者名をdata属性に付与（コネクタ線の色判定用）
            winner_name = ""
            if winner_sid:
                for s in g["slots"]:
                    if s.get("slot_id") == winner_sid and s.get("name"):
                        winner_name = esc(s.get("name",""))
                        break
            if not winner_name:
                for s in g["slots"]:
                    if s.get("rank") == 1 and s.get("name"):
                        winner_name = esc(s.get("name",""))
                        break
            data_winner = f' data-winner-name="{winner_name}"' if winner_name else ""
            gid_attr = f' id="vg-{group_id}"' if group_id else ""
            # 固定リンクによる「勝者の進む先グループ番号」（存在すれば）をJSへ渡す
            # ri=None（3位決定戦・裏トーナメント）ではリンク描画は使わない
            _adv_tgi = _adv_group_of.get((ri, gi)) if ri is not None else None
            adv_attr = f' data-advance-to-group="{_adv_tgi}"' if _adv_tgi is not None else ""
            grp_no = (f'<span style="position:absolute;top:-8px;left:-8px;width:22px;height:22px;border-radius:50%;background:#ffffff;color:#333333;font-size:14px;font-weight:bold;display:flex;align-items:center;justify-content:center;line-height:1;pointer-events:none;z-index:2;">{gi+1}</span>')
            groups_html.append(
                f'<div class="{grp_cls}" data-group-idx="{gi}"{data_winner}{gid_attr}{adv_attr}>{grp_no}{slots_html}</div>'
            )
        return (
            f'<div class="br-round" data-round-label="{esc(rnd.get("label",""))}">'
            f'<div class="br-round-label {label_class}">{esc(rnd.get("label",""))}</div>'
            f'<div class="br-round-groups">{"".join(groups_html)}</div>'
            f'</div>'
        )

    rounds_html = "".join(
        render_round(r, i == len(rounds) - 1, ri=i) for i, r in enumerate(rounds)
    )

    # コネクタ線をJSで描画
    connector_js = """
    <script>
    (function() {
      // グループの高さを取得するユーティリティ
      function getGroupH(g) {
        // offsetHeightをクローンで測定
        return g.offsetHeight || 60;
      }

      // ラウンド内の各グループの「占有スロット数」を返す
      function slotCounts(groups) {
        return groups.map(function(g) {
          var nonSeed = g.querySelectorAll('.br-slot:not([data-is-seed="true"])').length;
          return nonSeed > 0 ? nonSeed : 1;
        });
      }

      // 前ラウンド(curGroups)の各グループが次ラウンド(nextGroups)のどのグループへ
      // 合流するかを返す。戻り値は nextGroups と同じ長さの配列で、
      //   result[ni] = そのグループへ合流する前ラウンドグループ要素の配列。
      //
      // 【最優先】各グループに焼き付けられた固定リンク data-advance-to-group を使う。
      //   これは生成時に決めた実際の勝ち上がり先（＝レーサー配置と完全一致）であり、
      //   これを唯一の真実として辿ることで「配置は正しいのに結線だけ間違う」不整合を根絶する。
      // 【後方互換】リンク属性が1つも無い（旧トーナメント）場合のみ、従来の
      //   「次グループの非シード枠数で前ラウンドグループを先頭から順に割り当てる」
      //   ヒューリスティックへフォールバックする。
      function feederGroupsFor(curGroups, nextGroups) {
        var res = nextGroups.map(function(){ return []; });
        if (!curGroups.length || !nextGroups.length) return res;

        var hasLinks = false;
        for (var i = 0; i < curGroups.length; i++) {
          if (curGroups[i].getAttribute('data-advance-to-group') !== null) { hasLinks = true; break; }
        }

        if (hasLinks) {
          // 固定リンク：curGroups は group_no 順（DOM順）で並ぶので順に振り分ける
          for (var ci = 0; ci < curGroups.length; ci++) {
            var a = curGroups[ci].getAttribute('data-advance-to-group');
            if (a === null) continue;              // リンクなし（末尾の空き枠等）は接続しない
            var ni = parseInt(a, 10);
            if (ni >= 0 && ni < res.length) res[ni].push(curGroups[ci]);
          }
          return res;
        }

        // フォールバック：非シード枠数による逐次ブロック割り当て
        // 【重要】全シードグループ（非シード枠が0）は「勝者受け皿」を持たないため
        //   feeder を一切割り当てない。従来はここで 0 を 1 にクランプしていたため、
        //   全シードの組（例：準々決勝で2名ともシード）へ前ラウンド勝者を誤って
        //   結線し、本来つながるべき組が漏れていた（配置は正しいのに結線だけ誤る）。
        var counts = nextGroups.map(function(g) {
          return g.querySelectorAll('.br-slot:not([data-is-seed="true"])').length;
        });
        var curIdx = 0;
        for (var ni2 = 0; ni2 < nextGroups.length; ni2++) {
          var count = Math.min(counts[ni2], curGroups.length - curIdx);
          if (count < 0) count = 0;
          for (var k = 0; k < count && curIdx < curGroups.length; k++) {
            res[ni2].push(curGroups[curIdx]); curIdx++;
          }
        }
        // 端数（受け皿合計 < 前ラウンドグループ数）は、受け皿を持つ最後の組へ寄せる
        if (curIdx < curGroups.length) {
          var lastRecv = -1;
          for (var q = nextGroups.length - 1; q >= 0; q--) {
            if (counts[q] > 0) { lastRecv = q; break; }
          }
          if (lastRecv < 0) lastRecv = nextGroups.length - 1;
          while (curIdx < curGroups.length) {
            res[lastRecv].push(curGroups[curIdx]); curIdx++;
          }
        }
        return res;
      }

      // ラウンド1のグループを等間隔で配置し、以降のラウンドは前ラウンド対応グループ群の中央に配置
      function layoutRounds(container) {
        var rounds = Array.from(container.querySelectorAll('.br-round'));
        if (!rounds.length) return;
        var totalH = _layoutRoundGroup(rounds);
        if (totalH > 0) container.style.height = totalH + 'px';
      }

            function _layoutRoundGroup(rounds) {

        var GAP = 8; // グループ間の隙間px

        // 各ラウンドのグループ取得
        var allGroups = rounds.map(function(r) {
          return Array.from(r.querySelectorAll('.br-group'));
        });

        // R0（最初のラウンド）のグループを等間隔配置
        var r0groups = allGroups[0];
        if (!r0groups.length) return;

        // グループ自然高さを取得（absolute配置前に一時的に測定）
        var heights = allGroups.map(function(gs) {
          return gs.map(function(g) { return g.offsetHeight || 60; });
        });

        // 全ラウンド・全グループ中の最大高さを基準に一定間隔(ピッチ)を決める。
        // R0を「最大高さ＋GAP」の等間隔スロットに中央配置することで、後段ラウンドの
        // グループが背高でも重ならず、1対1対応の列は中心が揃い（コネクタ線が直線的）、
        // 合流（決勝など）はフィーダー中心の中点に来る。
        // ※従来は実高さで詰めて並べていたため、後段が前段より縦に高い構成で
        //   「中央寄せ→重なり押し下げ」のドリフトが発生し線がガタついていた。
        var maxGH = 0;
        heights.forEach(function(hs) { hs.forEach(function(v) { if (v > maxGH) maxGH = v; }); });
        var pitch = maxGH + GAP;

        // R0を等ピッチで各スロット中央へ配置
        var tops0 = [];
        for (var i = 0; i < r0groups.length; i++) {
          var c0 = pitch * i + maxGH / 2;
          tops0.push(c0 - heights[0][i] / 2);
        }
        var totalH = pitch * r0groups.length - GAP;

        // R0を配置
        for (var i = 0; i < r0groups.length; i++) {
          r0groups[i].style.top = tops0[i] + 'px';
        }
        rounds[0].querySelector('.br-round-groups').style.height = totalH + 'px';

        // R1以降：前ラウンド対応グループ群の中央にtopを合わせる
        // 各ラウンドの「どの前ラウンドグループ群に対応するか」を slotCounts で割り当て
        var prevTops = tops0;
        var prevHeights = heights[0];
        var prevGroups = r0groups;

        for (var ri = 1; ri < rounds.length; ri++) {
          var curGroups2 = allGroups[ri];
          var curHeights = heights[ri];
          if (!curGroups2.length) continue;

          // 前ラウンドのグループを今ラウンドのグループへ割り当てる。
          // 固定リンク(data-advance-to-group)があればそれを唯一の真実として使い、
          // 無い場合のみ従来の非シード枠数ヒューリスティックへフォールバックする。
          // → 実配置（焼き付けリンク）と結線・レイアウトが必ず一致する。
          var feederMap = feederGroupsFor(prevGroups, curGroups2);
          var newTops = [];
          var maxBottom = 0;

          for (var ni = 0; ni < curGroups2.length; ni++) {
            // 対応する前ラウンドの feeder 各組の「中央のY座標」を集め、その中央値に合わせる。
            var centers = [];
            var fdrs = feederMap[ni] || [];
            for (var fi = 0; fi < fdrs.length; fi++) {
              var pidx = prevGroups.indexOf(fdrs[fi]);
              if (pidx >= 0 && pidx < prevTops.length) {
                centers.push(prevTops[pidx] + (prevHeights[pidx] || 0) / 2);
              }
            }
            centers.sort(function(a, b){ return a - b; });
            var centerY;
            if (centers.length === 0) {
              // feeder が無い（シードのみのグループ等）→ 直前グループの直下を暫定中心にする
              if (ni > 0) {
                centerY = newTops[ni - 1] + curHeights[ni - 1] + GAP + curHeights[ni] / 2;
              } else {
                centerY = curHeights[ni] / 2;
              }
            } else if (centers.length % 2 === 1) {
              centerY = centers[(centers.length - 1) / 2];
            } else {
              centerY = (centers[centers.length / 2 - 1] + centers[centers.length / 2]) / 2;
            }
            var top = centerY - curHeights[ni] / 2;
            newTops.push(top);
            maxBottom = Math.max(maxBottom, top + curHeights[ni]);

            curGroups2[ni].style.top = top + 'px';
          }

          // 衝突回避（決定論・確実に重なりゼロ）：
          //  センタリングで決めた理想位置(idealTops)を基準に、上から順に
          //  「直前グループの下端＋GAP」を最低ラインとして押し下げるだけにする。
          //  下→上への引き上げ（理想位置への復帰）は行わない。
          //  引き上げは背の高いグループが直前グループへ再び食い込む往復バグの
          //  原因になり得るため廃止し、単調な下方押し下げで重なりを構造的に断つ。
          //  重なりが無い列は理想位置のまま（押し下げ条件を満たさない）＝縦ずれも出ない。
          var idealTops = newTops.slice();

          for (var cj = 1; cj < newTops.length; cj++) {
            var minTop = newTops[cj - 1] + curHeights[cj - 1] + GAP;
            if (newTops[cj] < minTop) newTops[cj] = minTop;
          }

          // 上端はみ出しのクランプ（全体を下げる）
          var minT = Infinity;
          for (var cj = 0; cj < newTops.length; cj++) if (newTops[cj] < minT) minT = newTops[cj];
          if (minT < 0) {
            for (var cj = 0; cj < newTops.length; cj++) newTops[cj] -= minT;
          }

          maxBottom = 0;
          for (var cj = 0; cj < curGroups2.length; cj++) {
            curGroups2[cj].style.top = newTops[cj] + 'px';
            maxBottom = Math.max(maxBottom, newTops[cj] + curHeights[cj]);
          }

          rounds[ri].querySelector('.br-round-groups').style.height = maxBottom + 'px';
          prevTops = newTops;
          prevHeights = curHeights;
          prevGroups = curGroups2;
        }

        // 上端はみ出し防止：いずれかのラウンドで top が負になり上に切れる場合、
        // 全グループ・全ラウンドを同量だけ下げて 0 始まりに正規化する
        // （全体を一律に下げるため、ラウンド間の中央揃え関係は崩れない）
        var minTop = Infinity;
        rounds.forEach(function(r) {
          r.querySelectorAll('.br-group').forEach(function(g) {
            var tp = parseFloat(g.style.top) || 0;
            if (tp < minTop) minTop = tp;
          });
        });
        if (minTop !== Infinity && minTop < 0) {
          var shift = -minTop;
          rounds.forEach(function(r) {
            r.querySelectorAll('.br-group').forEach(function(g) {
              g.style.top = ((parseFloat(g.style.top) || 0) + shift) + 'px';
            });
            var rg = r.querySelector('.br-round-groups');
            if (rg) rg.style.height = ((parseFloat(rg.style.height) || 0) + shift) + 'px';
          });
        }

        // 全ラウンドの .br-round-groups 高さを最大値に揃える
        var maxH = 0;
        rounds.forEach(function(r) {
          var rg = r.querySelector('.br-round-groups');
          maxH = Math.max(maxH, parseFloat(rg.style.height) || 0);
        });
        rounds.forEach(function(r) {
          r.querySelector('.br-round-groups').style.height = maxH + 'px';
        });
        // bracket-outer の高さを明示的にセット（position:relativeで絶対配置の子から高さを得られないため）
        var labelH = rounds[0].querySelector('.br-round-label') ? rounds[0].querySelector('.br-round-label').offsetHeight : 24;
        return maxH + labelH + 20;
      }

      // 裏トーナメント用：木レイアウトを使わず、各ラウンドのグループを単純に縦積みする。
      // （裏は「前ラウンド勝者＋表ラウンド敗者」の混在で木構造でないため、木配置だと
      //   グループが重なって消える。単純積みで全グループを必ず表示する。）
      function layoutRoundsSimple(container) {
        var GAP = 8;
        var rounds = Array.from(container.querySelectorAll('.br-round'));
        if (!rounds.length) return;
        var maxH = 0;
        rounds.forEach(function(r) {
          var gs = Array.from(r.querySelectorAll('.br-group'));
          var y = 0;
          gs.forEach(function(g) {
            g.style.top = y + 'px';
            y += (g.offsetHeight || 60) + GAP;
          });
          var h = y - GAP;
          var rg = r.querySelector('.br-round-groups');
          if (rg) rg.style.height = h + 'px';
          maxH = Math.max(maxH, h);
        });
        rounds.forEach(function(r) {
          var rg = r.querySelector('.br-round-groups');
          if (rg) rg.style.height = maxH + 'px';
        });
        var labelH = rounds[0].querySelector('.br-round-label') ? rounds[0].querySelector('.br-round-label').offsetHeight : 24;
        container.style.height = (maxH + labelH + 20) + 'px';
      }

      function drawConnectors(container) {
        container.querySelectorAll('.br-connector-svg').forEach(function(e){ e.remove(); });
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'br-connector-svg');
        svg.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;overflow:visible;';
        // style.height（layoutRoundsがセット済み）を優先して使う
        var cH = parseFloat(container.style.height) || Math.max(container.offsetHeight, container.scrollHeight);
        svg.style.width  = (container.scrollWidth  || container.offsetWidth)  + 'px';
        svg.style.height = cH + 'px';
        container.style.position = 'relative';
        container.appendChild(svg);
        var rounds = Array.from(container.querySelectorAll('.br-round'));
        if (rounds.length < 2) return;
        _drawBracketLines(svg, container, rounds, false);
      }

            function _drawBracketLines(svg, container, rounds, reverse) {
        var allGroups = rounds.map(function(r) {
          return Array.from(r.querySelectorAll('.br-group'));
        });
        for (var ri = 0; ri < rounds.length - 1; ri++) {
          var curGroups  = allGroups[ri];
          var nextGroups = allGroups[ri + 1];
          if (!curGroups.length || !nextGroups.length) continue;

          // 固定リンク(data-advance-to-group)から feeder→次グループの対応を得る。
          // 無い旧トーナメントは feederGroupsFor 内で従来ヒューリスティックへ自動フォールバック。
          var feederMap = feederGroupsFor(curGroups, nextGroups);

          for (var ni = 0; ni < nextGroups.length; ni++) {
            var feeders = feederMap[ni] || [];
            if (!feeders.length) continue;         // このグループへ合流する前ラウンドが無い（シードのみ等）

            var nextGroup = nextGroups[ni];
            var nRect     = getGroupRect(nextGroup, container);
            var nextMidY  = nRect.top + nRect.height / 2;

            if (!reverse) {
              var nextX = nRect.left;
              var midYs = [], rightX = 0;
              for (var ci = 0; ci < feeders.length; ci++) {
                var cr = getGroupRect(feeders[ci], container);
                var cy = cr.top + cr.height / 2;
                rightX = Math.max(rightX, cr.right);
                midYs.push(cy);
              }
              // 合流の縦線Xは、その合流ブロックの feeder 右端と次グループ左端の「中点」に置く。
              var gapX = nextX - rightX;            // feeder右端〜次グループ左端のすき間
              var vX;
              if (gapX >= 16) {
                vX = rightX + gapX / 2;             // 中点（毎ブロック同じ相対位置）
              } else {
                vX = rightX + Math.max(gapX / 2, 8);
              }
              // feeder 各組の右端から縦線Xまで横線
              for (var hi = 0; hi < feeders.length; hi++) {
                var hg = feeders[hi];
                var hr = getGroupRect(hg, container);
                var hy = hr.top + hr.height / 2;
                dline(svg, hr.right, hy, vX, hy, hg.classList.contains('has-winner') ? '#27ae60' : '#adb5bd');
              }
              if (midYs.length > 1) {
                dline(svg, vX, Math.min.apply(null,midYs), vX, Math.max.apply(null,midYs), '#adb5bd');
                dline(svg, vX, nextMidY, nextX, nextMidY, '#adb5bd');
              } else {
                // 単独合流でも、次グループの中心Yへ段差ルーティングする。
                // （従来は feeder の高さのまま真横に引いていたため、次グループが
                //   縦にずれて配置されると別グループへ刺さって見える結線バグになっていた）
                if (Math.abs(midYs[0] - nextMidY) >= 2) {
                  dline(svg, vX, Math.min(midYs[0], nextMidY), vX, Math.max(midYs[0], nextMidY), '#adb5bd');
                }
                dline(svg, vX, nextMidY, nextX, nextMidY, '#adb5bd');
              }
              // 次グループへ入る到達線（確定時は緑で色付け）。開始Xを vX 以上にクランプして、
              // 最左の次グループで nextX-24 がグループ左外へはみ出すのを防ぐ。
              var inX = Math.max(vX, nextX - 24);
              dline(svg, inX, nextMidY, nextX, nextMidY, nextGroup.classList.contains('has-winner') ? '#27ae60' : '#adb5bd');
            } else {
              var nextRight = nRect.right;
              var midYs = [], leftX = Infinity;
              for (var ci = 0; ci < feeders.length; ci++) {
                var cr = getGroupRect(feeders[ci], container);
                var cy = cr.top + cr.height / 2;
                leftX = Math.min(leftX, cr.left);
                midYs.push(cy);
              }
              var gapXr = leftX - nextRight;        // 次グループ右端〜feeder左端のすき間
              var vX;
              if (gapXr >= 16) {
                vX = leftX - gapXr / 2;             // 中点（毎ブロック同じ相対位置）
              } else {
                vX = leftX - Math.max(gapXr / 2, 8);
              }
              for (var hi = 0; hi < feeders.length; hi++) {
                var hg = feeders[hi];
                var hr = getGroupRect(hg, container);
                var hy = hr.top + hr.height / 2;
                dline(svg, hr.left, hy, vX, hy, hg.classList.contains('has-winner') ? '#27ae60' : '#adb5bd');
              }
              if (midYs.length > 1) {
                dline(svg, vX, Math.min.apply(null,midYs), vX, Math.max.apply(null,midYs), '#adb5bd');
                dline(svg, vX, nextMidY, nextRight, nextMidY, '#adb5bd');
              } else {
                // 単独合流でも、次グループの中心Yへ段差ルーティングする（正方向と同じ修正）
                if (Math.abs(midYs[0] - nextMidY) >= 2) {
                  dline(svg, vX, Math.min(midYs[0], nextMidY), vX, Math.max(midYs[0], nextMidY), '#adb5bd');
                }
                dline(svg, vX, nextMidY, nextRight, nextMidY, '#adb5bd');
              }
              var inXr = Math.min(vX, nextRight + 24);
              dline(svg, nextRight, nextMidY, inXr, nextMidY, nextGroup.classList.contains('has-winner') ? '#27ae60' : '#adb5bd');
            }
          }
        }
      }

      function _connectToFinal(svg, container, semiGroups, finalGroup, fromRight) {
        var fRect  = getGroupRect(finalGroup, container);
        var fMidY  = fRect.top + fRect.height / 2;
        var midYs  = [];
        if (!fromRight) {
          var rightX = 0;
          semiGroups.forEach(function(g) {
            var r = getGroupRect(g, container);
            var cy = r.top + r.height / 2;
            rightX = Math.max(rightX, r.right);
            midYs.push(cy);
            dline(svg, r.right, cy, r.right + 24, cy, g.classList.contains('has-winner') ? '#27ae60' : '#adb5bd');
          });
          var vX = rightX + 24;
          var centerY = midYs.reduce(function(a,b){return a+b;},0) / midYs.length;
          if (midYs.length > 1) dline(svg, vX, Math.min.apply(null,midYs), vX, Math.max.apply(null,midYs), '#adb5bd');
          dline(svg, vX, centerY, fRect.left, centerY, '#adb5bd');
          if (Math.abs(centerY - fMidY) > 2) dline(svg, fRect.left, Math.min(centerY,fMidY), fRect.left, Math.max(centerY,fMidY), '#adb5bd');
        } else {
          var leftX = Infinity;
          semiGroups.forEach(function(g) {
            var r = getGroupRect(g, container);
            var cy = r.top + r.height / 2;
            leftX = Math.min(leftX, r.left);
            midYs.push(cy);
            dline(svg, r.left - 24, cy, r.left, cy, g.classList.contains('has-winner') ? '#27ae60' : '#adb5bd');
          });
          var vX = leftX - 24;
          var centerY = midYs.reduce(function(a,b){return a+b;},0) / midYs.length;
          if (midYs.length > 1) dline(svg, vX, Math.min.apply(null,midYs), vX, Math.max.apply(null,midYs), '#adb5bd');
          dline(svg, fRect.right, centerY, vX, centerY, '#adb5bd');
          if (Math.abs(centerY - fMidY) > 2) dline(svg, fRect.right, Math.min(centerY,fMidY), fRect.right, Math.max(centerY,fMidY), '#adb5bd');
        }
      }

      function getGroupRect(g, container) {
        // br-group は position:absolute; top は style.top で管理
        // 親チェーンの offsetLeft/Top を積算してコンテナ基準の絶対座標を得る
        var gTop = parseFloat(g.style.top) || 0;
        var x = 0, y = 0;
        var el = g.parentElement; // br-round-groups
        while (el && el !== container) {
          x += el.offsetLeft || 0;
          y += el.offsetTop  || 0;
          el = el.offsetParent;
          if (!el || el === document.body) break;
        }
        y += gTop;
        var w = g.offsetWidth  || 250;
        var h = g.offsetHeight || (g.querySelectorAll('.br-slot').length * 48 + 10);
        return { left: x, right: x + w, top: y, bottom: y + h, width: w, height: h };
      }

      function dline(svg, x1, y1, x2, y2, color) {
        var l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        l.setAttribute('x1', x1); l.setAttribute('y1', y1);
        l.setAttribute('x2', x2); l.setAttribute('y2', y2);
        l.setAttribute('stroke', color); l.setAttribute('stroke-width', '3');
        svg.appendChild(l);
      }

      function init() {
        document.querySelectorAll('.bracket-outer').forEach(function(c){
          if (c.closest('.br-losers-section')) {
            // 裏トーナメントは木構造でないため単純縦積み＋コネクタ線なし
            layoutRoundsSimple(c);
          } else {
            layoutRounds(c);
            drawConnectors(c);
          }
        });
      }

      function initWithRetry() {
        init();
        // フォント・レイアウト確定後に再計算（初回描画ずれ対策）
        setTimeout(init, 300);
      }

      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initWithRetry);
      } else {
        setTimeout(initWithRetry, 50);
      }

      // リサイズ時に線を再計算（幅変化に追従）
      var _bracketResizeTimer = null;
      window.addEventListener('resize', function() {
        clearTimeout(_bracketResizeTimer);
        _bracketResizeTimer = setTimeout(init, 100);
      });

      // ResizeObserverで.bracket-outerの幅変化にも追従
      if (window.ResizeObserver) {
        document.querySelectorAll('.bracket-outer').forEach(function(c) {
          new ResizeObserver(function() {
            clearTimeout(_bracketResizeTimer);
            _bracketResizeTimer = setTimeout(init, 80);
          }).observe(c);
        });
      }

      window._bracketDrawConnectors = function() {
        // innerHTML差し替え後に呼ばれる場合、新しい.bracket-outerを再監視
        initWithRetry();
        if (window.ResizeObserver) {
          setTimeout(function() {
            document.querySelectorAll('.bracket-outer').forEach(function(c) {
              if (!c._roAttached) {
                c._roAttached = true;
                new ResizeObserver(function() {
                  clearTimeout(_bracketResizeTimer);
                  _bracketResizeTimer = setTimeout(init, 80);
                }).observe(c);
              }
            });
          }, 400);
        }
      };
    })();
    </script>
    """

    third_html = ""
    if third_rounds:
        third_inner = "".join(render_round(r, i == len(third_rounds) - 1) for i, r in enumerate(third_rounds))
        _third_type = third_rounds[0].get("round_type", "third") if third_rounds else "third"
        _third_title = "🏃 敗者復活戦" if _third_type == "revival" else "🥉 3位決定戦"
        third_html = (
            '<div class="br-third-section">'
            f'<div class="br-third-title">{_third_title}</div>'
            f'<div class="bracket-outer"><div class="bracket-html">{third_inner}</div></div>'
            '</div>'
        )

    podium_html = ""
    if champion_name or runner_up_name or third_name:
        parts = []
        if champion_name:
            parts.append(
                '<div class="br-champion">'
                '<div class="br-champion-label">🏆 第１位 🏆</div>'
                f'<div class="br-champion-name">{esc(champion_name)}</div>'
                '</div>'
            )
        if runner_up_name:
            parts.append(
                '<div class="br-runner-up">'
                '<div class="br-runner-up-label">🥈 第２位</div>'
                f'<div class="br-runner-up-name">{esc(runner_up_name)}</div>'
                '</div>'
            )
        if third_name:
            parts.append(
                '<div class="br-third-pod">'
                '<div class="br-third-pod-label">🥉 第３位</div>'
                f'<div class="br-third-pod-name">{esc(third_name)}</div>'
                '</div>'
            )
        podium_html = '<div class="br-podium">' + "".join(parts) + '</div>'

    losers_rounds_r = svg_data.get("losers_rounds", [])
    losers_html = ""
    if losers_rounds_r:
        losers_inner = "".join(render_round(r, i == len(losers_rounds_r) - 1) for i, r in enumerate(losers_rounds_r))
        losers_html = (
            '<div class="br-losers-section">'
            '<div class="br-losers-title">🔻 裏トーナメント（敗者復活）</div>'
            f'<div class="bracket-outer"><div class="bracket-html">{losers_inner}</div></div>'
            '</div>'
        )

    return (
        f'{css}'
        f'{connector_js}'
        f'<div class="br-wrap">'
        f'{podium_html}'
        f'<div class="bracket-outer">'
        f'<div class="bracket-html">{rounds_html}</div>'
        f'</div>'
        f'{third_html}'
        f'{losers_html}'
        f'</div>'
    )


def _render_svg(svg_data: dict) -> str:
    """SVGトーナメント表を生成"""
    rounds = svg_data.get("rounds", [])
    third_rounds = svg_data.get("third_rounds", [])

    BOX_W, BOX_H, GAP, COL_W = 240, 45, 12, 330
    PAD_X, PAD_Y, LABEL_H = 30, 60, 36

    # ラウンド0のスロット総数
    if not rounds:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="50"><text x="10" y="30" font-size="12" fill="#aaa">データなし</text></svg>'

    n0 = sum(len(g["slots"]) for g in rounds[0]["groups"])
    SLOT_H = BOX_H + GAP + 10

    # 3位決定戦の高さ
    third_extra = 0
    if third_rounds:
        tn0 = sum(len(g["slots"]) for g in third_rounds[0]["groups"])
        third_extra = 60 + tn0 * SLOT_H

    total_h = LABEL_H + PAD_Y + n0 * SLOT_H + third_extra + PAD_Y
    total_w = PAD_X + len(rounds) * COL_W + PAD_X + 30 + 180  # +180 for podium

    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{total_h}" style="background:#fff;font-family:\'Hiragino Kaku Gothic Pro\',Meiryo,sans-serif">']

    # clipPath（テキストが枠からはみ出さないようにする）
    clip_id_counter = [0]
    def make_clip(cx, cy, cw, ch):
        cid = f"clip{clip_id_counter[0]}"
        clip_id_counter[0] += 1
        lines.append(f'<clipPath id="{cid}"><rect x="{cx:.1f}" y="{cy:.1f}" width="{cw}" height="{ch}"/></clipPath>')
        return cid

    # r0スロットのY中心
    r0y = [LABEL_H + PAD_Y + i * SLOT_H + BOX_H/2 for i in range(n0)]

    def slot_ys(ri, gi):
        ni_total = sum(len(g["slots"]) for g in rounds[ri]["groups"])
        r0p = (n0 / ni_total) if ni_total else 0
        st = sum(len(rounds[ri]["groups"][g]["slots"]) for g in range(gi))
        ns = len(rounds[ri]["groups"][gi]["slots"])
        ys = []
        for s in range(ns):
            r0s = round((st+s)*r0p)
            r0e = min(round((st+s+1)*r0p)-1, len(r0y)-1)
            ys.append((r0y[max(0,r0s)] + r0y[min(r0e, len(r0y)-1)]) / 2)
        return ys

    clip_id_counter = [0]
    def make_clip(cx, cy, cw, ch):
        cid = f"clip{clip_id_counter[0]}"
        clip_id_counter[0] += 1
        lines.append(f'<clipPath id="{cid}"><rect x="{cx:.1f}" y="{cy:.1f}" width="{cw}" height="{ch}"/></clipPath>')
        return cid

    # スロット描画
    for ri, rnd in enumerate(rounds):
        x = PAD_X + ri * COL_W
        # ラベル
        lc = "#d4a017" if rnd["label"] == "決勝" else "#2980b9"
        if not any(s["name"] for g in rnd["groups"] for s in g["slots"]):
            lc = "#bdc3c7"
        lines.append(f'<text x="{x+BOX_W//2}" y="{LABEL_H-4}" text-anchor="middle" font-size="18" font-weight="bold" fill="{lc}">{rnd["label"]}</text>')

        for gi, gd in enumerate(rnd["groups"]):
            ys = slot_ys(ri, gi)
            win_sid = gd["result"]["winner_slot_id"] if gd["result"] else None

            for si, slot in enumerate(gd["slots"]):
                cy = ys[si]
                y = cy - BOX_H/2
                is_win = win_sid == slot["slot_id"]
                has_name = bool(slot["name"])
                # 背景
                if is_win:
                    lines.append(f'<rect x="{x}" y="{y:.1f}" width="{BOX_W}" height="{BOX_H}" rx="5" fill="#eafaf1" stroke="#27ae60" stroke-width="2"/>')
                    dot_c = "#27ae60"
                elif has_name:
                    lines.append(f'<rect x="{x}" y="{y:.1f}" width="{BOX_W}" height="{BOX_H}" rx="5" fill="white" stroke="#adb5bd" stroke-width="1.5"/>')
                    dot_c = "#bdc3c7"
                else:
                    lines.append(f'<rect x="{x}" y="{y:.1f}" width="{BOX_W}" height="{BOX_H}" rx="5" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5" stroke-dasharray="4,3"/>')
                    dot_c = "#dee2e6"
                # ドット
                lines.append(f'<circle cx="{x+12}" cy="{cy:.1f}" r="{5 if is_win else 4}" fill="{dot_c}"/>')
                # テキスト
                name = slot["name"] or "—"
                tc = "#27ae60" if is_win else ("#2c3e50" if has_name else "#bdc3c7")
                fw = "bold" if is_win else "normal"
                cid = make_clip(x+22, y-1, BOX_W-26, BOX_H+2)
                lines.append(f'<text x="{x+24}" y="{cy+4:.1f}" font-size="18" font-weight="{fw}" fill="{tc}" clip-path="url(#{cid})">{_esc(name)}</text>')
                # 予選順位（ヒート総当たり以外）
                qual_type = svg_data.get("qualifying_type", "")
                if slot.get("qual_rank") and has_name and qual_type not in ("heat_roundrobin", "none"):
                    lines.append(f'<text x="{x+BOX_W-4}" y="{cy+4:.1f}" text-anchor="end" font-size="15" fill="#95a5a6">予選{slot["qual_rank"]}位</text>')

            # 括弧線
            if len(ys) >= 2:
                bx = x + BOX_W + 6
                lines.append(f'<line x1="{bx}" y1="{ys[0]:.1f}" x2="{bx}" y2="{ys[-1]:.1f}" stroke="#6c757d" stroke-width="1.5"/>')
                for y2 in ys:
                    lines.append(f'<line x1="{bx-6}" y1="{y2:.1f}" x2="{bx}" y2="{y2:.1f}" stroke="#6c757d" stroke-width="1.5"/>')

    # コネクタ
    for ri in range(len(rounds)-1):
        x_from = PAD_X + ri * COL_W + BOX_W + 6
        x_to = PAD_X + (ri+1) * COL_W
        next_rnd = rounds[ri+1]
        # 次ラウンドの slot_id -> Y座標 マップ（固定リンクの接続先解決に使う）
        next_slot_y = {}
        for gi2, gd2 in enumerate(next_rnd["groups"]):
            ys2 = slot_ys(ri+1, gi2)
            for si2, sl2 in enumerate(gd2["slots"]):
                if sl2.get("slot_id") is not None:
                    next_slot_y[sl2["slot_id"]] = ys2[si2]

        # フォールバック用（旧トーナメント）：非シードの勝者待ち枠
        r1_dest_slots = []
        for gi2, gd2 in enumerate(next_rnd["groups"]):
            ys2 = slot_ys(ri+1, gi2)
            for si2, sl2 in enumerate(gd2["slots"]):
                is_seed = sl2.get("is_seed_slot", False) and sl2.get("name")
                if not is_seed:
                    r1_dest_slots.append({"y": ys2[si2], "name": sl2["name"]})

        # 現ラウンドの各グループの「出力Y」（勝者がいればそのY、なければグループ中心）
        cur_group_ys = []
        for gi, gd in enumerate(rounds[ri]["groups"]):
            ys = slot_ys(ri, gi)
            winner_sid = gd["result"]["winner_slot_id"] if gd.get("result") else None
            if winner_sid:
                win_y = next((ys[si] for si, sl in enumerate(gd["slots"]) if sl["slot_id"] == winner_sid), None)
                cur_group_ys.append(win_y if win_y else (ys[0]+ys[-1])/2 if len(ys)>1 else ys[0])
            else:
                cur_group_ys.append((ys[0]+ys[-1])/2 if len(ys)>1 else ys[0])

        # コネクタ：焼き付けリンク(advance_to_slot_id)があればそれを唯一の真実として辿る。
        # 無い旧トーナメントのみ従来の比例マッピングへフォールバック。
        cur_groups = rounds[ri]["groups"]
        has_links = any(g.get("advance_to_slot_id") for g in cur_groups)
        n_cur = len(cur_group_ys)
        n_dst = len(r1_dest_slots)
        for gi, from_y in enumerate(cur_group_ys):
            to_y = None
            if has_links:
                adv = cur_groups[gi].get("advance_to_slot_id")
                if adv is not None and adv in next_slot_y:
                    to_y = next_slot_y[adv]
                else:
                    continue  # リンクが次ラウンドに無い（末尾の空き枠等）→ 接続しない
            else:
                if n_dst <= 0:
                    continue
                dst_i = min(gi * n_dst // n_cur, n_dst - 1)
                to_y = r1_dest_slots[dst_i]["y"]
            mx = (x_from + x_to) / 2
            has_winner = cur_groups[gi].get("result") is not None
            stroke = "#495057" if has_winner else "#adb5bd"
            dash = "" if has_winner else ' stroke-dasharray="4,3"'
            lines.append(f'<polyline points="{x_from:.1f},{from_y:.1f} {mx:.1f},{from_y:.1f} {mx:.1f},{to_y:.1f} {x_to:.1f},{to_y:.1f}" fill="none" stroke="{stroke}" stroke-width="2"{dash}/>')

    # 3位決定戦
    if third_rounds:
        tn0 = sum(len(g["slots"]) for g in third_rounds[0]["groups"])
        ty_base = LABEL_H + PAD_Y + n0 * SLOT_H + 40
        ty_r0 = [ty_base + i * SLOT_H + BOX_H/2 for i in range(tn0)]

        def t_slot_ys(tri, tgi):
            ni_t = sum(len(g["slots"]) for g in third_rounds[tri]["groups"])
            r0p = (tn0 / ni_t) if ni_t else 0
            st = sum(len(third_rounds[tri]["groups"][g]["slots"]) for g in range(tgi))
            ns = len(third_rounds[tri]["groups"][tgi]["slots"])
            ys = []
            for s in range(ns):
                r0s = round((st+s)*r0p)
                r0e = min(round((st+s+1)*r0p)-1, len(ty_r0)-1)
                ys.append((ty_r0[max(0,r0s)] + ty_r0[min(r0e,len(ty_r0)-1)]) / 2)
            return ys

        for tri, trnd in enumerate(third_rounds):
            tx = PAD_X + tri * COL_W
            lbl = trnd["label"]
            lines.append(f'<text x="{tx+BOX_W//2}" y="{ty_base-12:.1f}" text-anchor="middle" font-size="17" font-weight="bold" fill="#7f8c8d">{lbl}</text>')
            for tgi, tgd in enumerate(trnd["groups"]):
                tys = t_slot_ys(tri, tgi)
                twin = tgd["result"]["winner_slot_id"] if tgd["result"] else None
                for si, slot in enumerate(tgd["slots"]):
                    cy = tys[si]; y = cy - BOX_H/2
                    is_win = twin == slot["slot_id"]
                    sc = "#27ae60" if is_win else "#dee2e6"
                    fc = "#eafaf1" if is_win else "white"
                    lines.append(f'<rect x="{tx}" y="{y:.1f}" width="{BOX_W}" height="{BOX_H}" rx="5" fill="{fc}" stroke="{sc}" stroke-width="{"1.5" if is_win else "0.8"}"/>')
                    lines.append(f'<circle cx="{tx+12}" cy="{cy:.1f}" r="4" fill="{"#27ae60" if is_win else "#bdc3c7"}"/>')
                    name = slot["name"] or "—"
                    tc = "#27ae60" if is_win else "#2c3e50"
                    lines.append(f'<text x="{tx+24}" y="{cy+4:.1f}" font-size="12" font-weight="{"bold" if is_win else "normal"}" fill="{tc}">{_esc(name)}</text>')
                if len(tys) >= 2:
                    bx = tx + BOX_W + 6
                    lines.append(f'<line x1="{bx}" y1="{tys[0]:.1f}" x2="{bx}" y2="{tys[-1]:.1f}" stroke="#6c757d" stroke-width="1.5"/>')
                    for ty2 in tys:
                        lines.append(f'<line x1="{bx-6}" y1="{ty2:.1f}" x2="{bx}" y2="{ty2:.1f}" stroke="#6c757d" stroke-width="1.5"/>')

    # ── 表彰台（決勝結果が確定したら右端に表示）──
    final_gd = next((g for r in svg_data.get("rounds",[]) if r["label"]=="決勝" for g in r["groups"]), None)
    if final_gd and final_gd.get("result"):
        win_sid = final_gd["result"]["winner_slot_id"]
        winner = next((s for s in final_gd["slots"] if s["slot_id"]==win_sid), None)
        runners = [s for s in final_gd["slots"] if s["slot_id"]!=win_sid]
        n_final = len(final_gd["slots"])

        # 3位決定戦の勝者を取得
        third_name = None
        third_runners = []  # 3位決定戦の敗者（4位以下）
        if svg_data.get("third_rounds"):
            last_third = svg_data["third_rounds"][-1]
            for grp in last_third.get("groups", []):
                if grp.get("result"):
                    t3w = grp["result"]["winner_slot_id"]
                    t3 = next((s for s in grp["slots"] if s["slot_id"]==t3w), None)
                    if t3: third_name = t3["name"]; break

        px = PAD_X + len(rounds) * COL_W + 16
        py = LABEL_H + PAD_Y

        # 背景パネル
        lines.append(f'<rect x="{px-8}" y="{py-8}" width="162" height="150" rx="8" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>')

        # トロフィー＋1位
        lines.append(f'<text x="{px}" y="{py+22}" font-size="22">🏆</text>')
        lines.append(f'<text x="{px+30}" y="{py+12}" font-size="15" font-weight="bold" fill="#d4a017">1位</text>')
        name1 = _esc(winner["name"]) if winner and winner.get("name") else "—"
        lines.append(f'<text x="{px+30}" y="{py+26}" font-size="13" font-weight="bold" fill="#d4a017">{name1}</text>')

        # 2位・3位（決勝3名の場合は敗者を2位・3位に）
        py_cur = py + 46
        if n_final == 3:
            # 決勝3名：rank順に2位・3位
            # rank情報があれば使用、なければ敗者を順番に2位・3位
            for rank_i, r in enumerate(runners, 2):
                rank_c = "#888" if rank_i == 2 else "#cd7f32"
                rname = _esc(r["name"]) if r.get("name") else "—"
                lines.append(f'<text x="{px}" y="{py_cur}" font-size="10" font-weight="bold" fill="{rank_c}">{rank_i}位</text>')
                lines.append(f'<text x="{px}" y="{py_cur+14}" font-size="12" fill="#2c3e50">{rname}</text>')
                py_cur += 32
        else:
            # 決勝2名：2位は敗者
            if runners:
                lines.append(f'<text x="{px}" y="{py_cur}" font-size="10" font-weight="bold" fill="#888">2位</text>')
                rname = _esc(runners[0]["name"]) if runners[0].get("name") else "—"
                lines.append(f'<text x="{px}" y="{py_cur+14}" font-size="12" fill="#2c3e50">{rname}</text>')
                py_cur += 32
            # 3位：3位決定戦の勝者
            if third_name:
                lines.append(f'<text x="{px}" y="{py_cur}" font-size="10" font-weight="bold" fill="#cd7f32">3位</text>')
                lines.append(f'<text x="{px}" y="{py_cur+14}" font-size="12" fill="#2c3e50">{_esc(third_name)}</text>')

    lines.append('</svg>')
    return '\n'.join(lines)


def _esc(s: str) -> str:
    return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')
