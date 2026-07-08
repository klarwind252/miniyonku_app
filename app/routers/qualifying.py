"""
予選管理ルーター
- ヒート制：レーン数に応じた自動スケジュール・周回/タイム入力・ポイント集計
- 総当たり：C(n,2)の全対戦自動生成・○×+タイム入力・勝ち数集計
"""
from fastapi import APIRouter, Request, Depends, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import aiosqlite
import itertools
import random
import os
import json

from app.models.database import get_db
from app.routers.tournaments import QUALIFYING_LABELS, calc_finalists

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "../templates"))
from app.config import inject_globals
inject_globals(templates)


# ======================================================================
# ヒート単位ロック（ヒート（トーナメント）専用）
#   仕様: あるヒートのヒート決勝で決勝進出レーサーが全員確定した時点でロック。
#         ロック中はそのヒート全体の編集（結果保存・再生成・リセット・レーン編集・
#         ヒート決勝の生成/リセット）を一切禁止する。解除は「結果を取消」ボタンのみ。
#         リセットでは解除されない。取消は結果データを保持しロックだけ外す。
#   実装: tournaments.heat_locks (TEXT/JSON) に {"<heat_no>": true} を保持。
# ======================================================================
async def _get_heat_locks(tid: int, db) -> dict:
    """tournaments.heat_locks を dict で返す（{heat_no(str): bool}）。"""
    async with db.execute("SELECT heat_locks FROM tournaments WHERE id=?", (tid,)) as cur:
        row = await cur.fetchone()
    if not row:
        return {}
    raw = row["heat_locks"] if "heat_locks" in row.keys() else None
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


async def _is_heat_locked(tid: int, heat_no: int, db) -> bool:
    locks = await _get_heat_locks(tid, db)
    return bool(locks.get(str(heat_no)))


async def _set_heat_lock(tid: int, heat_no: int, locked: bool, db) -> None:
    locks = await _get_heat_locks(tid, db)
    if locked:
        locks[str(heat_no)] = True
    else:
        locks.pop(str(heat_no), None)
    await db.execute(
        "UPDATE tournaments SET heat_locks=? WHERE id=?", (json.dumps(locks), tid)
    )
    await db.commit()


async def _ht_heat_finalized(tid: int, heat_no: int, db) -> bool:
    """そのヒート（トーナメント）のヒート決勝で決勝進出レーサーが全員確定したか。
    qual_heat_advance 名ぶんの本戦進出者が is_advance 付きで揃っていれば確定とみなす。"""
    async with db.execute(
        "SELECT qualifying_type, qual_heat_final, qual_heat_advance FROM tournaments WHERE id=?",
        (tid,),
    ) as cur:
        t = await cur.fetchone()
    if not t or t["qualifying_type"] != "heat_tournament":
        return False
    heat_advance = int((t["qual_heat_advance"] or 1))
    if bool(t["qual_heat_final"]):
        advs = await _ht_get_heatfinal_advancers(tid, heat_no, heat_advance, db)
        confirmed = [a for a in advs if a.get("is_advance") and a.get("entry_id")]
        return len(confirmed) >= heat_advance
    # ヒート決勝なし設定: 各グループ通過者が確定していれば確定扱い
    async with db.execute("SELECT qual_group_advance FROM tournaments WHERE id=?", (tid,)) as cur:
        gr = await cur.fetchone()
    group_advance = int((gr["qual_group_advance"] if gr else 1) or 1)
    ok, _msg = await _ht_all_groups_ranked(tid, heat_no, group_advance, db)
    return bool(ok)


async def _maybe_lock_heat(tid: int, heat_no: int, db) -> None:
    """ヒート決勝で進出確定していれば、そのヒートを自動ロックする。"""
    if await _ht_heat_finalized(tid, heat_no, db):
        if not await _is_heat_locked(tid, heat_no, db):
            await _set_heat_lock(tid, heat_no, True, db)


def _locked_json_response():
    return JSONResponse(
        {"ok": False, "locked": True,
         "error": "このヒートは結果確定済みのためロックされています。編集するには「結果を取消」してください。"},
        status_code=409,
    )


POINT_TABLE = {1: 10, 2: 7, 3: 5, 4: 3, 5: 2, 6: 1}

def calc_points(rank: int) -> int:
    return POINT_TABLE.get(rank, 0)

def is_roundrobin(t) -> bool:
    return dict(t).get("qualifying_type") == "roundrobin"


# ── 総当たりスケジュール生成 ──────────────────────────────
def generate_roundrobin_schedule(entry_ids: list[int]) -> list[tuple[int,int,int,int]]:
    """
    C(n,2)の全対戦ペアを生成。総当たりは 1vs1 のため常に 1コース・2コースの
    2レーン固定で対戦する（設定レーン数に関わらず3コース目は使わない）。
    優先順位:
      ① 連続走行の回避（数学的に不可能なときのみ連続を許容）
      ② コースは可能な限り前回と異なるコースにする（次点でコース使用回数の偏りも縮小）

    戻り値: [(race_no, lane_A, entry_id_A, lane_B, entry_id_B), ...]
    """
    n = len(entry_ids)
    if n < 2:
        return []

    # 全ペア生成
    pairs = list(itertools.combinations(entry_ids, 2))

    # ── ① 連続走行を避ける順序決定（貪欲＋1手先読み） ──
    ordered = _order_pairs_no_consecutive(pairs)

    # ── ② コース割り当て（前回と別コースを最優先、次に使用回数の偏り最小化） ──
    result = _assign_lanes_alternating(ordered, entry_ids)
    return result


def _order_pairs_no_consecutive(pairs: list[tuple]) -> list[tuple]:
    """連続走行を最小化する並び替え。
    「直前のレースと同じ人が出ない」順序を最優先で探索する。
    まずバックトラック探索で連続0の順序を探し、見つからない／規模が大きい場合は
    貪欲＋先読みのヒューリスティックにフォールバックする（避けられない分だけ連続を許容）。"""
    pairs = list(pairs)
    m = len(pairs)
    if m <= 1:
        return pairs

    # 各ペアに対し「共通の人がいない（＝連続にならない）ペア」の隣接リストを作る
    disjoint = [[] for _ in range(m)]
    for i in range(m):
        si = set(pairs[i])
        for j in range(m):
            if i != j and not (si & set(pairs[j])):
                disjoint[i].append(j)

    # ── ① バックトラックで「連続0」を探索（規模が大きすぎる場合はスキップ） ──
    # ステップ予算で打ち切り、現実的な計算量に収める。
    budget = [200000]
    best_order = None

    def dfs(path, used):
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        if len(path) == m:
            return True
        last = path[-1]
        # Warnsdorff風: 次に進める非連続候補のうち、さらに先の選択肢が少ない順に試す
        cands = [j for j in disjoint[last] if not used[j]]
        cands.sort(key=lambda j: sum(1 for k in disjoint[j] if not used[k]))
        for j in cands:
            used[j] = True
            path.append(j)
            if dfs(path, used):
                return True
            path.pop()
            used[j] = False
        return False

    if m <= 220:  # 総当たり推奨上限20名（=190試合）まで連続0探索を行う
        for start in range(m):
            # 開始点も先の自由度が高いものから（薄く）試す
            used = [False] * m
            used[start] = True
            path = [start]
            if dfs(path, used):
                best_order = [pairs[k] for k in path]
                break
            if budget[0] <= 0:
                break

    if best_order is not None:
        return best_order

    # ── ② フォールバック：貪欲＋1手先読み（避けられない連続のみ許容） ──
    remaining = list(range(m))
    order_idx: list[int] = []
    prev: set = set()
    while remaining:
        non_consec = [i for i in remaining if not (set(pairs[i]) & prev)]
        cands = non_consec if non_consec else remaining

        def lookahead_score(i):
            ap = set(pairs[i])
            return sum(1 for k in remaining if k != i and not (set(pairs[k]) & ap))

        best = max(cands, key=lambda i: (lookahead_score(i), -remaining.index(i)))
        order_idx.append(best)
        remaining.remove(best)
        prev = set(pairs[best])
    return [pairs[k] for k in order_idx]


def _assign_lanes_alternating(ordered_pairs: list[tuple], entry_ids: list[int]) -> list[tuple]:
    """各対戦の2人を1コース/2コースへ割り当てる。
    ① 各レーサーが前回走ったコースと違うコースになる配置を最優先
    ② 次にコース使用回数の偏りが小さい配置を選ぶ
    戻り値: [(race_no, 1, eid_course1, 2, eid_course2), ...]"""
    last_lane: dict = {}                                   # {eid: 直前のコース番号}
    lane_count = {e: {1: 0, 2: 0} for e in entry_ids}      # {eid: {コース: 回数}}

    result = []
    for i, (ea, eb) in enumerate(ordered_pairs):
        best = None  # (score, la, lb)
        for la, lb in ((1, 2), (2, 1)):
            same_prev = (1 if last_lane.get(ea) == la else 0) + \
                        (1 if last_lane.get(eb) == lb else 0)
            imbalance = lane_count[ea][la] + lane_count[eb][lb]
            score = (same_prev, imbalance)  # まず連続コース回避、次に偏り
            if best is None or score < best[0]:
                best = (score, la, lb)
        _, la, lb = best
        last_lane[ea], last_lane[eb] = la, lb
        lane_count[ea][la] += 1
        lane_count[eb][lb] += 1
        # コース番号順（1コース側, 2コース側）で返す
        if la == 1:
            result.append((i + 1, 1, ea, 2, eb))
        else:
            result.append((i + 1, 1, eb, 2, ea))
    return result


def _order_pairs(pairs: list[tuple]) -> list[tuple]:
    """貪欲法：直前のレースと同じ人が出ないように並び替え（旧実装・後方互換のため残置）"""
    if not pairs:
        return []
    ordered = [pairs[0]]
    remaining = pairs[1:]
    while remaining:
        last = set(ordered[-1])
        # 直前と被らないペアを優先
        no_overlap = [p for p in remaining if not (set(p) & last)]
        nxt = no_overlap[0] if no_overlap else remaining[0]
        ordered.append(nxt)
        remaining.remove(nxt)
    return ordered


# ── ヒート制スケジュール生成 ──────────────────────────────
def generate_heat_roundrobin_schedule(
    entry_ids: list[int],
    heat_count: int,
    group_count: int,
    lane_count: int,
) -> list[dict]:
    """
    ヒート（総当たり）のスケジュール生成。
    - 総当たりは 1vs1 のため常に 1コース・2コースの2レーン固定で対戦する
      （設定レーン数に関わらず3コース目は使わない）。
    - コース偏りを最小化：各レーサーが1コース/2コースに均等に入るよう割り当て
    - 連続出走を回避：直前のレースに出たレーサーを次のレースに入れない
    戻り値: [{"round_no": int, "group_no": int, "heat_no": int, "slots": [entry_id, ...]}, ...]
    """
    import random
    from itertools import permutations

    # 総当たりは 1vs1 固定。使用レーンは常に2本（1コース・2コース）にする。
    lane_count = 2

    n = len(entry_ids)
    base, rem = divmod(n, group_count)

    # コース履歴: {entry_id: {lane_no: count}}
    lane_history = {e: {l: 0 for l in range(1, lane_count + 1)} for e in entry_ids}
    # 各レーサーが直前に走ったコース: {entry_id: lane_no}
    last_lane = {}
    # 直前レース出走者
    prev_race_eids = set()

    races = []
    race_no = 1

    for rnd in range(1, heat_count + 1):
        # ヒートごとにグループを再編成（ランダム）
        reshuffled = entry_ids[:]
        random.shuffle(reshuffled)
        rnd_groups = []
        idx = 0
        for g in range(group_count):
            size = base + (1 if g < rem else 0)
            rnd_groups.append(reshuffled[idx:idx+size])
            idx += size

        for g_no, group in enumerate(rnd_groups, 1):
            m = len(group)
            # 全対戦ペアを生成
            pairs = [(group[i], group[j]) for i in range(m) for j in range(i+1, m)]
            # 連続走行を最小化する並び替え（平の総当たりと同じ探索を使用）。
            # グループ内は別レーサー同士なので、この並びで連続を最小化できる。
            ordered_pairs = _order_pairs_no_consecutive(pairs)
            if ordered_pairs:
                prev_race_eids = set(ordered_pairs[-1])

            for pair in ordered_pairs:
                p = list(pair)
                # コース割り当て：lane_count本から2本を選び、
                # ① 前回と同じコースになる人数が少ない配置を最優先
                # ② 次にコース使用回数の偏りが小さい配置を選ぶ
                from itertools import combinations as _comb
                all_lane_options = list(range(1, lane_count + 1))
                best_slots = None
                best_score = None
                best_lanes = None
                for chosen_lanes in _comb(all_lane_options, len(p)):
                    for perm in permutations(p):
                        same_prev = sum(
                            1 for eid, lane in zip(perm, chosen_lanes)
                            if last_lane.get(eid) == lane
                        )
                        imbalance = sum(lane_history[eid][lane]
                                        for eid, lane in zip(perm, chosen_lanes))
                        score = (same_prev, imbalance)
                        if best_score is None or score < best_score:
                            best_score = score
                            best_slots = list(perm)
                            best_lanes = list(chosen_lanes)
                # コース履歴・直前コースを更新
                for eid, lane in zip(best_slots, best_lanes):
                    lane_history[eid][lane] += 1
                    last_lane[eid] = lane
                # slots をコース番号順に配置（空きはNone）
                slots = [None] * lane_count
                for eid, lane in zip(best_slots, best_lanes):
                    slots[lane - 1] = eid
                races.append({
                    "round_no": rnd,
                    "group_no": g_no,
                    "heat_no": race_no,
                    "slots": slots,
                })
                race_no += 1
                prev_race_eids = set(best_slots)

    return races


def generate_point_schedule(entry_ids: list, round_count: int, lane_count: int) -> list:
    """
    ポイント制：round_count回分のスケジュール生成。
    - 毎回全員出走
    - 同コース被りを最小化（レーサー個人のレーン履歴ベース）
    - 過去ラウンドの対戦相手との再対戦を最小化（組全体のペア合計で評価）
    戻り値: [ラウンド1グループリスト, ラウンド2グループリスト, ...]
            各グループは lane_no 昇順（index0 = lane1）に並んだ entry_id のリスト
            （2人組は 1・2 レーン固定、3人組は 1・2・3 を個人履歴で最適化）
    """
    import math
    import itertools

    n = len(entry_ids)
    if n == 0:
        return []

    def split_groups(ids, lanes):
        """先頭を大きく、末尾を小さく分割（1人組は作らない）。"""
        cnt = len(ids)
        if cnt == 0:
            return []
        n_groups = max(1, math.ceil(cnt / lanes))
        groups = []
        i = 0
        for g in range(n_groups):
            remaining = cnt - i
            remaining_groups = n_groups - g
            size = math.ceil(remaining / remaining_groups)
            size = min(size, lanes)
            groups.append(ids[i:i + size])
            i += size
        # 末尾が1人組になった場合は手前の組に吸収（1人組禁止）
        if len(groups) >= 2 and len(groups[-1]) == 1:
            groups[-2].extend(groups[-1])
            groups.pop()
        return groups

    def group_pair_cost(group, opponents):
        """組内の全ペアの過去対戦回数の合計（小さいほど良い）。"""
        cost = 0
        for a, b in itertools.combinations(group, 2):
            cost += opponents.get((min(a, b), max(a, b)), 0)
        return cost

    def order_candidates(opponents):
        """
        対戦回数の少ないペアが同じ組になりやすいよう貪欲に並べる。
        直前1人ではなく、確定途中グループ内の全員との合計対戦回数を見る。
        """
        remaining = entry_ids[:]
        random.shuffle(remaining)
        ordered = []
        current_group = []
        while remaining:
            if not current_group:
                pick = remaining.pop(0)
            else:
                # current_group 全員との対戦回数合計が最小の候補を選ぶ
                pick = min(
                    remaining,
                    key=lambda e: (
                        sum(opponents.get((min(m, e), max(m, e)), 0) for m in current_group),
                        random.random(),
                    ),
                )
                remaining.remove(pick)
            ordered.append(pick)
            current_group.append(pick)
            if len(current_group) >= lane_count:
                current_group = []
        return ordered

    def assign_lanes(group, lane_history):
        """
        組内の各レーサーを、個人のレーン履歴合計コストが最小になるよう
        レーンへ割り当てる。組は最大 lane_count 人なので全順列で厳密最適化。
        2人組は lanes=[1,2] のみ使用（物理レーン固定）、3人組は [1,2,3]。
        戻り値: lane_no 昇順に並んだ entry_id のリスト。
        """
        size = len(group)
        lanes = list(range(1, size + 1))
        best_perm = None
        best_cost = None
        for perm in itertools.permutations(group):
            cost = sum(lane_history[perm[i]].count(lanes[i]) for i in range(size))
            # タイブレークに微小ランダムを足して偏りを防ぐ
            cost = cost + random.random() * 0.01
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best_perm = perm
        result = list(best_perm)  # result[i] が lanes[i] (= i+1 レーン)
        for i, e in enumerate(result):
            lane_history[e].append(lanes[i])
        return result  # index0 = lane1, index1 = lane2, ...

    all_schedule = []
    opponents = {}                              # {(a,b): 対戦回数}
    lane_history = {e: [] for e in entry_ids}   # {entry_id: [lane_no, ...]}

    TRIALS = 24  # 各ラウンドの組分け試行回数

    for rno in range(round_count):
        # --- 1) 対戦相手回避：複数回試行して最良の組分けを採用 ---
        best_groups = None
        best_total = None
        for _ in range(TRIALS):
            candidates = order_candidates(opponents)
            groups = split_groups(candidates, lane_count)
            total = sum(group_pair_cost(g, opponents) for g in groups)
            if best_total is None or total < best_total:
                best_total = total
                best_groups = groups
                if best_total == 0:
                    break  # 完全回避できたら打ち切り

        # --- 2) レーン回避：各組内でレーンを最適割当 ---
        assigned_groups = []
        for group in best_groups:
            ordered_group = assign_lanes(group, lane_history)
            assigned_groups.append(ordered_group)

        # --- 3) 対戦履歴を更新 ---
        for group in assigned_groups:
            for a, b in itertools.combinations(group, 2):
                key = (min(a, b), max(a, b))
                opponents[key] = opponents.get(key, 0) + 1

        all_schedule.append(assigned_groups)

    return all_schedule


def generate_heat_schedule(entry_ids: list[int], lane_count: int) -> list[list[int]]:
    n = len(entry_ids)
    if n == 0:
        return []
    if n <= lane_count:
        return [entry_ids[:]]
    heats = []
    seen = set()
    for r in range(lane_count):
        rotated = entry_ids[r:] + entry_ids[:r]
        for i in range(0, n, lane_count):
            group = rotated[i:i + lane_count]
            if len(group) == 1:
                if heats:
                    heats[-1].append(group[0])
            else:
                key = frozenset(group)
                if key not in seen:
                    seen.add(key)
                    heats.append(group)
    return heats


# ── 予選トップ画面 ────────────────────────────────────────
@router.get("/{tid}/qualifying", response_class=HTMLResponse)
async def qualifying_top(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url="/admin/tournaments/")

    async with db.execute(
        """SELECT e.id as entry_id, e.entry_order, r.id as racer_id, r.name
           FROM entries e JOIN racers r ON r.id=e.racer_id
           WHERE e.tournament_id=? AND e.status='active'
           ORDER BY e.entry_order""",
        (tid,),
    ) as cur:
        entries = await cur.fetchall()

    async with db.execute(
        "SELECT * FROM heats WHERE tournament_id=? ORDER BY group_no, heat_no", (tid,),
    ) as cur:
        heats = await cur.fetchall()

    heat_lanes_map = {}
    if heats:
        hids = [h["id"] for h in heats]
        ph = ",".join("?" * len(hids))
        async with db.execute(
            f"""SELECT hl.heat_id, hl.lane_no, hl.entry_id, hl.id as lane_id, r.name,
                       COALESCE(r.yomi,'') as yomi
                FROM heat_lanes hl
                JOIN entries e ON e.id=hl.entry_id
                JOIN racers r ON r.id=e.racer_id
                WHERE hl.heat_id IN ({ph}) ORDER BY hl.lane_no""",
            hids,
        ) as cur:
            for row in await cur.fetchall():
                heat_lanes_map.setdefault(row["heat_id"], []).append(row)

    async with db.execute(
        """SELECT DISTINCT hl.heat_id FROM heat_results hr
           JOIN heat_lanes hl ON hl.id=hr.heat_lane_id
           JOIN heats h ON h.id=hl.heat_id
           WHERE h.tournament_id=?""", (tid,),
    ) as cur:
        done_heat_ids = {r["heat_id"] for r in await cur.fetchall()}

    rr = is_roundrobin(t)
    hr = dict(t).get("qualifying_type") == "heat_roundrobin"
    nr = dict(t).get("qualifying_type") == "none_roundrobin"
    od = dict(t).get("qualifying_type") == "order"
    ow = dict(t).get("qualifying_type") == "order_winner"

    # ヒート（トーナメント）の場合はheat_tournament画面へ
    if dict(t).get("qualifying_type") == "heat_tournament":
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying/heat-tournament/1", status_code=303)
    standings = await _calc_standings_none_rr(tid, db) if nr else (await _calc_standings_rr(tid, db) if rr else await _calc_standings(tid, db))
    finalist_n = calc_finalists(dict(t).get("qualifying_type",""), dict(t))

    # ポイント制: 決勝進出ボーダーラインの同率グループにフラグ付与
    if not nr and not rr and finalist_n and standings:
        cutoff_st = standings[finalist_n - 1] if finalist_n <= len(standings) else None
        if cutoff_st:
            cutoff_rank = cutoff_st["rank"]
            # cutoff_rank の同率グループ全員の数
            cutoff_group = [st for st in standings if st["rank"] == cutoff_rank]
            count_above = sum(1 for st in standings if st["rank"] < cutoff_rank)
            # 進出確定数がfinalist_nに満たず、かつ同率グループがはみ出す場合だけハイライト
            is_border = count_above < finalist_n < count_above + len(cutoff_group)
            for st in standings:
                st["is_tied_cutoff"] = is_border and st["rank"] == cutoff_rank

    hoshitori_entries, hoshitori_matrix = (await _calc_hoshitori(tid, db)) if (rr or nr) else ([], {})

    # none_roundrobin: 確定済みかどうか + top3 + 決定戦グループ
    is_confirmed_nr = False
    nr_can_confirm = False
    top3_nr = []
    deciding_groups_nr = []   # [{start_pos, tied_players, decide_positions, decided}]
    if nr:
        is_confirmed_nr = dict(t).get("status") == "complete"
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM heats WHERE tournament_id=? AND COALESCE(deciding_position,0)=0 AND status!='done'",
            (tid,),
        ) as cur:
            nr_pending = (await cur.fetchone())["cnt"]

        # 決定済み順位を取得（entries.none_rr_rank で管理）
        async with db.execute(
            "SELECT id as entry_id, none_rr_rank FROM entries WHERE tournament_id=? AND none_rr_rank IS NOT NULL",
            (tid,),
        ) as cur:
            decided_map = {r["entry_id"]: r["none_rr_rank"] for r in await cur.fetchall()}

        # standings に decided_rank を付与してからランクを上書き
        for st in standings:
            if st["entry_id"] in decided_map:
                st["decided_rank"] = decided_map[st["entry_id"]]
            else:
                st["decided_rank"] = None

        # 決定戦グループを計算（1〜3位の同率グループのみ）
        processed_ranks = set()
        for pos in (1, 2, 3):
            if pos in processed_ranks:
                continue
            tied = [st for st in standings if st["rank"] == pos]
            if len(tied) >= 2:
                n = len(tied)
                max_decide = min(pos + n - 2, 3)   # 決定戦で決める最高順位
                decide_positions = list(range(pos, max_decide + 1))
                # すでに決定済みのエントリーを確認
                decided = {p: next((st for st in tied if st.get("decided_rank") == p), None)
                           for p in decide_positions}
                deciding_groups_nr.append({
                    "start_pos": pos,
                    "tied_players": tied,
                    "decide_positions": decide_positions,
                    "decided": decided,
                    "all_decided": all(decided[p] is not None for p in decide_positions),
                })
                for p in range(pos, pos + n):
                    processed_ranks.add(p)

        # 確定可能条件: 全ヒート完了 + 全決定戦グループが決定済み
        all_decided = all(g["all_decided"] for g in deciding_groups_nr) if deciding_groups_nr else True
        nr_can_confirm = nr_pending == 0 and all_decided

        # 同率グループの「残り」を自動で次順位に割り当てたマップを作成
        _final_preview = {}
        _proc = set()
        for pos in (1, 2, 3):
            if pos in _proc:
                continue
            tied = [st for st in standings if st["rank"] == pos]
            if len(tied) >= 2:
                n = len(tied)
                max_dec = min(pos + n - 2, 3)
                dec_pos = list(range(pos, max_dec + 1))
                dec_eids = set()
                for dp in dec_pos:
                    for st in tied:
                        if st.get("decided_rank") == dp:
                            _final_preview[st["entry_id"]] = dp
                            dec_eids.add(st["entry_id"])
                next_r = max_dec + 1
                for st in tied:
                    if st["entry_id"] not in dec_eids:
                        _final_preview[st["entry_id"]] = next_r
                for p in range(pos, pos + n):
                    _proc.add(p)
            else:
                for st in tied:
                    _final_preview[st["entry_id"]] = pos
                _proc.add(pos)
        # 4位以下
        for st in standings:
            if st["entry_id"] not in _final_preview:
                _final_preview[st["entry_id"]] = st.get("decided_rank") or st["rank"]

        for pos in (1, 2, 3):
            matched = [st for st in standings if _final_preview.get(st["entry_id"]) == pos]
            top3_nr.append(matched[0] if matched else None)

    # ヒート総当たりのグループ別スタンディング
    group_standings = {}
    if hr:
        # グループ番号一覧
        async with db.execute(
            "SELECT DISTINCT group_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY group_no",
            (tid,),
        ) as cur:
            group_nos = [r["group_no"] for r in await cur.fetchall()]
        for gno in group_nos:
            group_standings[gno] = await _calc_standings_group(tid, gno, db)
        # グループ内星取表（グループ全体）
        group_hoshitori = {}
        for gno in group_nos:
            entries_g, matrix_g = await _calc_hoshitori_group(tid, gno, db)
            group_hoshitori[gno] = {"entries": entries_g, "matrix": matrix_g}

    # ヒート×グループ単位の成績（heat_roundrobinのみ）
    heat_group_standings = {}  # {(round_no, group_no): [standings]}
    heat_group_hoshitori = {}  # {(round_no, group_no): {entries, matrix}}
    if hr:
        async with db.execute(
            "SELECT DISTINCT round_no, group_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY round_no, group_no",
            (tid,),
        ) as cur:
            rg_pairs = [(r["round_no"], r["group_no"]) for r in await cur.fetchall()]
        for rno, gno in rg_pairs:
            heat_group_standings[(rno, gno)] = await _calc_standings_group_round(tid, gno, rno, db)
            entries_g, matrix_g = await _calc_hoshitori_group_round(tid, gno, rno, db)
            heat_group_hoshitori[(rno, gno)] = {"entries": entries_g, "matrix": matrix_g}
    # 総合成績（全ヒート・全グループの合算）
    overall_standings = await _calc_standings_overall(tid, db) if hr else []

    # ヒート×グループの通過者リスト（管理画面の進出レーサー一覧用）
    # ヒート除外フラグ関連変数（テンプレート用）
    hr_heat_exclude = bool(dict(t).get("qual_heat_exclude", 0)) if hr else False
    hr_heat_total_count = int(dict(t).get("qual_heat_count", 1) or 1) if hr else 1
    if hr:
        async with db.execute(
            "SELECT DISTINCT round_no FROM heats WHERE tournament_id=? AND group_no>0",
            (tid,),
        ) as cur:
            hr_generated_round_nos = {r["round_no"] for r in await cur.fetchall()}
    else:
        hr_generated_round_nos = set()

    hr_advanced_by_heat = {}  # {round_no: [{group_no, name, rank}]}
    if hr:
        group_advance = int(dict(t).get("qual_group_advance", 1) or 1)
        qual_heat_exclude_flag = bool(dict(t).get("qual_heat_exclude", 0))
        qual_heat_final_flag = bool(dict(t).get("qual_heat_final", 0))
        seen_eids: set = set()
        async with db.execute(
            "SELECT DISTINCT round_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY round_no",
            (tid,),
        ) as cur:
            heat_round_nos = [r["round_no"] for r in await cur.fetchall()]
        for rno in heat_round_nos:
            # そのヒートの全heat_idを取得し、全て完了済みかチェック
            async with db.execute(
                "SELECT id FROM heats WHERE tournament_id=? AND round_no=? AND group_no>0",
                (tid, rno),
            ) as cur:
                rno_heat_ids = {r["id"] for r in await cur.fetchall()}
            if not rno_heat_ids or not rno_heat_ids.issubset(done_heat_ids):
                continue  # 通常ヒート未完了はスキップ

            # 優勝トーナメントありの場合：winner未設定ならスキップ（優勝トーナメント未完了）
            if qual_heat_final_flag:
                async with db.execute(
                    """SELECT COUNT(*) as cnt FROM heat_finals
                       WHERE tournament_id=? AND round_no=? AND group_no=0
                         AND final_type='heat' AND winner_entry_id IS NOT NULL""",
                    (tid, rno),
                ) as cur:
                    winner_cnt = (await cur.fetchone())["cnt"]
                if winner_cnt == 0:
                    continue  # 優勝トーナメント未完了はスキップ

            slots = []

            if qual_heat_final_flag:
                # ── ヒート優勝トーナメントあり：heat_finals の勝者を進出者とする ──
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
                if not heat_winners:
                    continue  # 優勝トーナメント未完了はスキップ
                for i, hw in enumerate(heat_winners):
                    if qual_heat_exclude_flag and hw["entry_id"] in seen_eids:
                        continue
                    slots.append({"group_no": 0, "rank": i + 1, "name": hw["name"], "entry_id": hw["entry_id"]})
                    if qual_heat_exclude_flag:
                        seen_eids.add(hw["entry_id"])
            else:
                # ── ヒート優勝トーナメントなし：グループ上位N名を進出者とする ──
                async with db.execute(
                    "SELECT DISTINCT group_no FROM heats WHERE tournament_id=? AND round_no=? ORDER BY group_no",
                    (tid, rno),
                ) as cur:
                    g_nos = [r["group_no"] for r in await cur.fetchall()]
                for gno in g_nos:
                    st = heat_group_standings.get((rno, gno), [])

                    # プレーオフ勝者を確認（同率解消済みの場合はそちらを優先）
                    async with db.execute(
                        """SELECT hf.entry_id, r.name
                           FROM heat_finals hf
                           JOIN entries e ON e.id=hf.entry_id
                           JOIN racers r ON r.id=e.racer_id
                           WHERE hf.tournament_id=? AND hf.round_no=? AND hf.group_no=?
                             AND hf.final_type='playoff' AND hf.winner_entry_id IS NOT NULL""",
                        (tid, rno, gno),
                    ) as cur:
                        po_winners = {r["entry_id"]: r["name"] for r in await cur.fetchall()}

                    picked = 0
                    added_eids: set = set()
                    if st:
                        # ボーダーラインの勝数を確認
                        border_wins = st[min(group_advance, len(st)) - 1]["wins"] if len(st) >= group_advance else -1
                        for p in st:
                            if picked >= group_advance:
                                break
                            eid = p["entry_id"]
                            if qual_heat_exclude_flag and eid in seen_eids:
                                continue
                            # ボーダーライン上で同率タイ → プレーオフがある場合はプレーオフ勝者のみ
                            if p["wins"] == border_wins and po_winners:
                                if eid not in po_winners:
                                    continue  # プレーオフ敗者はスキップ
                            slots.append({"group_no": gno, "rank": picked + 1, "name": p["name"], "entry_id": eid})
                            added_eids.add(eid)
                            if qual_heat_exclude_flag:
                                seen_eids.add(eid)
                            picked += 1

            hr_advanced_by_heat[rno] = slots

    # インライン表示用：(heat_id, entry_id) -> {win, best_time, lap_count}
    inline_results = {}
    if heats:
        hids = [h["id"] for h in heats]
        ph = ",".join("?" * len(hids))
        async with db.execute(
            f"""SELECT hl.heat_id, hl.entry_id, hl.id as lane_id,
                       hr.win, hr.best_time, hr.lap_count, hr.rank, hr.points,
                       COALESCE(hr.is_co, 0) as is_co
                FROM heat_lanes hl
                JOIN heats h ON h.id=hl.heat_id
                LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
                WHERE hl.heat_id IN ({ph})""",
            hids,
        ) as cur:
            for row in await cur.fetchall():
                inline_results[(row["heat_id"], row["entry_id"])] = dict(row)

    # heat_lanes_map に lane_id も追加（保存ボタン用）
    for hid, lanes in heat_lanes_map.items():
        for lane in lanes:
            lane = dict(lane)

    from app.routers.tournaments import _is_result_finalized
    is_finalized = await _is_result_finalized(tid, db)

    hr_complete_round_nos = set(hr_advanced_by_heat.keys())

    # ── order（並び順）専用コンテキスト ──────────────────────────
    order_ctx = {}
    if od:
        cur_round = await _order_current_round(tid, t, db)
        order_pending = await _order_queue_pending(tid, cur_round, db)
        async with db.execute(
            "SELECT DISTINCT round_no FROM heats WHERE tournament_id=? ORDER BY round_no",
            (tid,),
        ) as cur:
            order_round_nos = [r["round_no"] for r in await cur.fetchall()]
        total_entries = len(entries)
        async with db.execute(
            "SELECT COUNT(*) AS n FROM order_queue WHERE tournament_id=? AND round_no=?",
            (tid, cur_round),
        ) as cur:
            scanned_this_round = (await cur.fetchone())["n"] or 0

        # このラウンドで既にスキャン済み（待機 or 出走済み）の entry_id 集合
        async with db.execute(
            "SELECT entry_id FROM order_queue WHERE tournament_id=? AND round_no=?",
            (tid, cur_round),
        ) as cur:
            scanned_entry_ids = {r["entry_id"] for r in await cur.fetchall()}

        # 走行回数（consumed=1 を1走行とみなす）を entry_id ごとに集計
        #   フリー走行制・制限ありのとき「制限到達者を除外」する判定に使う
        async with db.execute(
            "SELECT entry_id, COUNT(*) AS n FROM order_queue "
            "WHERE tournament_id=? AND round_no=? AND consumed=1 GROUP BY entry_id",
            (tid, cur_round),
        ) as cur:
            run_counts = {r["entry_id"]: r["n"] for r in await cur.fetchall()}

        _omode = dict(t).get("order_round_mode") or "free"
        _max_runs = dict(t).get("order_free_max_runs") or 0

        # 本エントリー（active）を受付番号・レーサー名つきで entry_order 順に取得
        async with db.execute(
            """SELECT e.id AS entry_id, e.pre_seq_no, r.name,
                      COALESCE(r.yomi,'') AS yomi
               FROM entries e JOIN racers r ON r.id=e.racer_id
               WHERE e.tournament_id=? AND e.status='active'
               ORDER BY e.entry_order""",
            (tid,),
        ) as cur:
            _all_active = [dict(row) for row in await cur.fetchall()]

        if _omode == "round":
            # ラウンド制：このラウンドで未スキャンのレーサー
            order_unscanned = [e for e in _all_active
                               if e["entry_id"] not in scanned_entry_ids]
        elif _max_runs and _max_runs > 0:
            # フリー走行制・制限あり：走行回数が制限未満のレーサーのみ（制限到達者は除外）
            order_unscanned = [e for e in _all_active
                               if run_counts.get(e["entry_id"], 0) < _max_runs]
        else:
            # フリー走行制・制限なし：本エントリー全員を常時表示
            order_unscanned = list(_all_active)

        order_ctx = {
            "order_mode": dict(t).get("order_round_mode") or "free",
            "order_round_count": dict(t).get("order_round_count") or 1,
            "order_status": dict(t).get("order_status") or "",
            "order_current_round": cur_round,
            "order_pending": order_pending,
            "order_round_nos": order_round_nos,
            "order_total_entries": total_entries,
            "order_scanned_this_round": scanned_this_round,
            "order_unscanned": order_unscanned,
            "order_free_max_runs": _max_runs,
        }

    # ── order_winner（並び順（勝ち抜け））専用コンテキスト ──────────────
    order_winner_ctx = {}
    if ow:
        ow_stage_no = await _ow_current_stage(tid, t, db)
        ow_stage = await _ow_stage_row(tid, ow_stage_no, db) or {}
        ow_stage_count = await _ow_stage_count(tid, t, db)
        # 全段階の設定（タブ表示・進捗用）
        async with db.execute(
            """SELECT stage_no, win_target, max_runs, advance_count, status
               FROM order_winner_stages WHERE tournament_id=? ORDER BY stage_no""",
            (tid,),
        ) as cur:
            ow_stages = [dict(r) for r in await cur.fetchall()]
        # 現段階の待機列
        ow_pending = await _ow_queue_pending(tid, ow_stage_no, db)
        # 現段階の未完了の組（結果入力対象）＋各組のレーン
        async with db.execute(
            """SELECT id, heat_no, status FROM heats
               WHERE tournament_id=? AND round_no=? ORDER BY heat_no""",
            (tid, ow_stage_no),
        ) as cur:
            ow_heats_all = [dict(r) for r in await cur.fetchall()]
        ow_open_heats = []  # 未確定（status!='done'）の組
        for h in ow_heats_all:
            if h["status"] == "done":
                continue
            async with db.execute(
                """SELECT hl.entry_id, r.name
                   FROM heat_lanes hl JOIN entries e ON e.id=hl.entry_id
                   JOIN racers r ON r.id=e.racer_id
                   WHERE hl.heat_id=? ORDER BY hl.lane_no""",
                (h["id"],),
            ) as cur:
                members = [dict(r) for r in await cur.fetchall()]
            ow_open_heats.append({"heat_id": h["id"], "heat_no": h["heat_no"], "members": members})
        # 現段階の通過者数
        ow_passed = await _ow_passed_count(tid, ow_stage_no, db)
        # 現段階のレーサー状態（通過者一覧・敗退者数など）
        async with db.execute(
            """SELECT owr.entry_id, owr.wins, owr.runs, owr.status, owr.passed_seq, r.name
               FROM order_winner_racers owr
               JOIN entries e ON e.id=owr.entry_id
               JOIN racers r ON r.id=e.racer_id
               WHERE owr.tournament_id=? AND owr.stage_no=?
               ORDER BY owr.passed_seq IS NULL, owr.passed_seq, r.yomi""",
            (tid, ow_stage_no),
        ) as cur:
            ow_racers = [dict(r) for r in await cur.fetchall()]
        ow_passed_list = [x for x in ow_racers if x["status"] == "passed"]
        ow_eliminated_count = sum(1 for x in ow_racers if x["status"] == "eliminated")

        # 未スキャン一覧（現段階の対象者で、まだ待機列にも組にもいない racing/未登場）
        #   1次: 全active、2次以降: 前段階passed。通過・敗退済みは除外。
        if ow_stage_no <= 1:
            async with db.execute(
                """SELECT e.id AS entry_id, e.pre_seq_no, r.name, COALESCE(r.yomi,'') AS yomi
                   FROM entries e JOIN racers r ON r.id=e.racer_id
                   WHERE e.tournament_id=? AND e.status='active' ORDER BY e.entry_order""",
                (tid,),
            ) as cur:
                ow_target_all = [dict(r) for r in await cur.fetchall()]
        else:
            async with db.execute(
                """SELECT e.id AS entry_id, e.pre_seq_no, r.name, COALESCE(r.yomi,'') AS yomi
                   FROM entries e JOIN racers r ON r.id=e.racer_id
                   JOIN order_winner_racers owr ON owr.entry_id=e.id
                        AND owr.tournament_id=e.tournament_id
                   WHERE e.tournament_id=? AND e.status='active'
                         AND owr.stage_no=? AND owr.status='passed'
                   ORDER BY e.entry_order""",
                (tid, ow_stage_no - 1),
            ) as cur:
                ow_target_all = [dict(r) for r in await cur.fetchall()]
        # 現段階で「通過・敗退・待機中・出走中」の entry を除外
        ow_status_map = {x["entry_id"]: x["status"] for x in ow_racers}
        ow_in_queue = {p["entry_id"] for p in ow_pending}
        ow_in_open = {m["entry_id"] for h in ow_open_heats for m in h["members"]}
        ow_unscanned = [
            e for e in ow_target_all
            if ow_status_map.get(e["entry_id"]) not in ("passed", "eliminated")
            and e["entry_id"] not in ow_in_queue
            and e["entry_id"] not in ow_in_open
        ]

        order_winner_ctx = {
            "ow_stage_no": ow_stage_no,
            "ow_stage": ow_stage,
            "ow_stage_count": ow_stage_count,
            "ow_stages": ow_stages,
            "ow_is_last_stage": ow_stage_no >= ow_stage_count,
            "ow_pending": ow_pending,
            "ow_open_heats": ow_open_heats,
            "ow_passed": ow_passed,
            "ow_advance_count": ow_stage.get("advance_count") or 0,
            "ow_max_runs": ow_stage.get("max_runs") or 0,
            "ow_win_target": ow_stage.get("win_target") or 1,
            "ow_stage_status": ow_stage.get("status") or "pending",
            "ow_passed_list": ow_passed_list,
            "ow_eliminated_count": ow_eliminated_count,
            "ow_unscanned": ow_unscanned,
        }

    return templates.TemplateResponse("admin/qualifying.html", {
        "request": request,
        "t": t,
        "entries": entries,
        "heats": heats,
        "heat_lanes_map": heat_lanes_map,
        "done_heat_ids": done_heat_ids,
        "is_finalized": is_finalized,
        "standings": standings,
        "lane_range": list(range(1, t["lane_count"] + 1)),
        "qualifying_labels": QUALIFYING_LABELS,
        "finalists": finalist_n,
        "is_roundrobin": rr,
        "is_heat_roundrobin": hr,
        "is_none_roundrobin": nr,
        "is_order": od,
        "is_order_winner": ow,
        "is_confirmed_nr": is_confirmed_nr,
        "nr_can_confirm": nr_can_confirm,
        "top3_nr": top3_nr,
        "deciding_groups_nr": deciding_groups_nr,
        "group_standings": group_standings,
        "group_hoshitori": group_hoshitori if hr else {},
        "heat_group_standings": heat_group_standings,
        "heat_group_hoshitori": heat_group_hoshitori,
        "overall_standings": overall_standings,
        "hr_advanced_by_heat": hr_advanced_by_heat if hr else {},
        "heat_exclude": hr_heat_exclude,
        "heat_total_count": hr_heat_total_count,
        "generated_round_nos": hr_generated_round_nos,
        "complete_round_nos": hr_complete_round_nos,
        "has_any_result": len(done_heat_ids) > 0,
        "heat_final_data": await _get_heat_final_data(tid, db) if hr else {"rounds": [], "advance": 0},
        "playoff_data": await _get_playoff_data(tid, db) if hr else {},
        "hoshitori_entries": hoshitori_entries,
        "hoshitori_matrix": hoshitori_matrix,
        "inline_results": inline_results,
        **order_ctx,
        **order_winner_ctx,
    })


# ── スケジュール生成 ──────────────────────────────────────

@router.post("/{tid}/qualifying/generate-heat/{round_no}")
async def qualifying_generate_heat(
    tid: int, round_no: int, db: aiosqlite.Connection = Depends(get_db)
):
    """heat_roundrobin + qual_heat_exclude=1 用：指定ヒートのみ生成（進出済みを除外）"""
    from fastapi.responses import JSONResponse as _JR
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)

    t_dict = dict(t)
    group_count  = int(t_dict.get("qual_group_count", 1) or 1)
    lane_count   = int(t_dict.get("lane_count", 3) or 3)
    heat_count   = int(t_dict.get("qual_heat_count", 1) or 1)

    # 指定ヒートに既に結果入力済みなら拒否
    async with db.execute(
        "SELECT id FROM heats WHERE tournament_id=? AND round_no=? AND status='done'",
        (tid, round_no),
    ) as cur:
        if await cur.fetchone():
            return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)

    # 指定ヒートの既存未入力ヒートを削除
    async with db.execute(
        "SELECT id FROM heats WHERE tournament_id=? AND round_no=?", (tid, round_no)
    ) as cur:
        old_ids = [r["id"] for r in await cur.fetchall()]
    if old_ids:
        ph = ",".join("?" * len(old_ids))
        await db.execute(f"DELETE FROM heat_results WHERE heat_lane_id IN (SELECT id FROM heat_lanes WHERE heat_id IN ({ph}))", old_ids)
        await db.execute(f"DELETE FROM heat_lanes WHERE heat_id IN ({ph})", old_ids)
        await db.execute(f"DELETE FROM heats WHERE id IN ({ph})", old_ids)

    # 全エントリー取得
    async with db.execute(
        "SELECT e.id FROM entries e WHERE e.tournament_id=? AND e.status='active' ORDER BY e.entry_order",
        (tid,),
    ) as cur:
        all_entry_ids = [r["id"] for r in await cur.fetchall()]

    # 前ヒートまでの進出確定者を除外
    # 1) heat_finals 勝者（qual_heat_final=1 の場合）
    # 2) advanced=1 のエントリー
    excluded: set = set()
    if t_dict.get("qual_heat_final"):
        async with db.execute(
            """SELECT DISTINCT entry_id FROM heat_finals
               WHERE tournament_id=? AND group_no=0 AND final_type='heat'
                 AND winner_entry_id IS NOT NULL""",
            (tid,),
        ) as cur:
            for r in await cur.fetchall():
                excluded.add(r["entry_id"])
    else:
        async with db.execute(
            "SELECT id FROM entries WHERE tournament_id=? AND advanced=1",
            (tid,),
        ) as cur:
            for r in await cur.fetchall():
                excluded.add(r["id"])

    # 対象エントリー（除外後）
    entry_ids = [e for e in all_entry_ids if e not in excluded]
    import random as _rand
    _rand.shuffle(entry_ids)

    # ヒートスケジュール生成
    schedule_rr = generate_heat_roundrobin_schedule(entry_ids, 1, group_count, lane_count)

    # 現在の最大heat_noを取得
    async with db.execute(
        "SELECT COALESCE(MAX(heat_no),0) as mx FROM heats WHERE tournament_id=?", (tid,)
    ) as cur:
        max_heat_no = (await cur.fetchone())["mx"]

    global_heat_no = max_heat_no + 1
    for item in schedule_rr:
        await db.execute(
            "INSERT INTO heats (tournament_id, heat_no, group_no, round_no, status) VALUES (?,?,?,?,?)",
            (tid, global_heat_no, item["group_no"], round_no, "pending"),
        )
        async with db.execute("SELECT last_insert_rowid() as id") as cur:
            heat_id = (await cur.fetchone())["id"]
        for lane_no, eid in enumerate(item["slots"], 1):
            if eid is None:
                continue
            await db.execute(
                "INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)",
                (heat_id, lane_no, eid),
            )
        global_heat_no += 1

    await db.commit()
    # 参加者向けHTML配信（自動更新）
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass

    return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)


@router.post("/{tid}/qualifying/generate-round/{round_no}")
async def qualifying_generate_round(
    tid: int, round_no: int, db: aiosqlite.Connection = Depends(get_db)
):
    """ポイント制：指定ラウンドのみ生成（入力済みラウンドは保持）"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)

    # 指定ラウンドに入力済みのheatがあればスキップ
    async with db.execute(
        "SELECT id FROM heats WHERE tournament_id=? AND round_no=? AND status='done'",
        (tid, round_no),
    ) as cur:
        if await cur.fetchone():
            return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)

    # 指定ラウンドの既存heats（未入力）を削除
    async with db.execute(
        "SELECT id FROM heats WHERE tournament_id=? AND round_no=?", (tid, round_no)
    ) as cur:
        old_heats = [r["id"] for r in await cur.fetchall()]
    if old_heats:
        ph = ",".join("?" * len(old_heats))
        await db.execute(f"DELETE FROM heat_results WHERE heat_lane_id IN (SELECT id FROM heat_lanes WHERE heat_id IN ({ph}))", old_heats)
        await db.execute(f"DELETE FROM heat_lanes WHERE heat_id IN ({ph})", old_heats)
        await db.execute(f"DELETE FROM heats WHERE id IN ({ph})", old_heats)

    # 全アクティブエントリーでそのラウンドを生成
    async with db.execute(
        "SELECT e.id FROM entries e WHERE e.tournament_id=? AND e.status='active' ORDER BY e.entry_order",
        (tid,),
    ) as cur:
        entry_ids = [r["id"] for r in await cur.fetchall()]

    lane_count = int(t["lane_count"] or 3)
    # 対戦履歴を過去ラウンドから取得
    opponents = {}
    lane_history = {e: [] for e in entry_ids}

    async with db.execute(
        "SELECT h.id FROM heats h WHERE h.tournament_id=? AND h.round_no < ? AND h.status='done'",
        (tid, round_no),
    ) as cur:
        done_heats = [r["id"] for r in await cur.fetchall()]

    if done_heats:
        ph2 = ",".join("?" * len(done_heats))
        async with db.execute(
            f"SELECT hl.heat_id, hl.lane_no, hl.entry_id FROM heat_lanes hl WHERE hl.heat_id IN ({ph2}) ORDER BY hl.heat_id, hl.lane_no",
            done_heats,
        ) as cur:
            for row in await cur.fetchall():
                eid = row["entry_id"]
                if eid in lane_history:
                    lane_history[eid].append(row["lane_no"])

    # 1ラウンド分生成
    one_round = generate_point_schedule(entry_ids, 1, lane_count)
    if one_round:
        groups = one_round[0]
        # 最大heat_noを取得
        async with db.execute(
            "SELECT COALESCE(MAX(heat_no),0) as mx FROM heats WHERE tournament_id=?", (tid,)
        ) as cur:
            max_heat = (await cur.fetchone())["mx"]

        heat_no = max_heat + 1
        for group in groups:
            await db.execute(
                "INSERT INTO heats (tournament_id, heat_no, group_no, round_no, status) VALUES (?,?,?,?,?)",
                (tid, heat_no, 0, round_no, "pending"),
            )
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                heat_id = (await cur.fetchone())["id"]
            for lane_no, eid in enumerate(group, 1):
                await db.execute(
                    "INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)",
                    (heat_id, lane_no, eid),
                )
            heat_no += 1

    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)


@router.get("/{tid}/qualifying/heat/{heat_id}/edit", response_class=HTMLResponse)
async def heat_edit_form(tid: int, heat_id: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """手動割り当て：未入力ヒートのレーン編集フォーム"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    async with db.execute("SELECT * FROM heats WHERE id=? AND tournament_id=?", (heat_id, tid)) as cur:
        heat = await cur.fetchone()
    if not heat or heat["status"] == "done":
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)

    async with db.execute(
        """SELECT hl.id as lane_id, hl.lane_no, hl.entry_id, r.name
           FROM heat_lanes hl
           LEFT JOIN entries e ON e.id=hl.entry_id
           LEFT JOIN racers r ON r.id=e.racer_id
           WHERE hl.heat_id=? ORDER BY hl.lane_no""",
        (heat_id,),
    ) as cur:
        lanes = [dict(r) for r in await cur.fetchall()]

    async with db.execute(
        """SELECT e.id, r.name FROM entries e JOIN racers r ON r.id=e.racer_id
           WHERE e.tournament_id=? AND e.status='active' ORDER BY r.yomi, r.name""",
        (tid,),
    ) as cur:
        all_entries = [dict(r) for r in await cur.fetchall()]

    from fastapi.templating import Jinja2Templates as _T
    import os as _os
    _tmpl = _T(directory=_os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "templates"))
    return _tmpl.TemplateResponse("admin/heat_edit.html", {
        "request": request, "t": t, "heat": heat, "lanes": lanes, "all_entries": all_entries,
    })


@router.post("/{tid}/qualifying/heat/{heat_id}/edit")
async def heat_edit_save(tid: int, heat_id: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """手動割り当て保存"""
    async with db.execute("SELECT status FROM heats WHERE id=? AND tournament_id=?", (heat_id, tid)) as cur:
        heat = await cur.fetchone()
    if not heat or heat["status"] == "done":
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)

    form = await request.form()
    async with db.execute("SELECT id as lane_id, lane_no FROM heat_lanes WHERE heat_id=? ORDER BY lane_no", (heat_id,)) as cur:
        lanes = [dict(r) for r in await cur.fetchall()]

    for lane in lanes:
        eid_str = form.get(f"entry_{lane['lane_no']}", "")
        eid = int(eid_str) if eid_str else None
        if eid is None:
            # （空き）に変更：レーン行自体を削除する。
            # heat_lanes.entry_id は NOT NULL のため NULL 更新は不可。
            # また「空きレーン＝行が存在しない」が生成時の表現と一致する。
            # 紐づく結果（heat_results）も合わせて削除する。
            await db.execute("DELETE FROM heat_results WHERE heat_lane_id=?", (lane["lane_id"],))
            await db.execute("DELETE FROM heat_lanes WHERE id=?", (lane["lane_id"],))
        else:
            await db.execute("UPDATE heat_lanes SET entry_id=? WHERE id=?", (eid, lane["lane_id"]))

    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)

@router.post("/{tid}/qualifying/generate")
async def qualifying_generate(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()

    async with db.execute(
        "SELECT e.id FROM entries e WHERE e.tournament_id=? AND e.status='active' ORDER BY e.entry_order",
        (tid,),
    ) as cur:
        entry_ids = [r["id"] for r in await cur.fetchall()]

    if not entry_ids:
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)

    # シャッフルしてランダム順に（毎回異なる組み合わせ）
    random.shuffle(entry_ids)

    # 既存データ削除
    async with db.execute("SELECT id FROM heats WHERE tournament_id=?", (tid,)) as cur:
        old_heats = [r["id"] for r in await cur.fetchall()]
    if old_heats:
        ph = ",".join("?" * len(old_heats))
        await db.execute(f"DELETE FROM heat_results WHERE heat_lane_id IN (SELECT id FROM heat_lanes WHERE heat_id IN ({ph}))", old_heats)
        await db.execute(f"DELETE FROM heat_lanes WHERE heat_id IN ({ph})", old_heats)
        await db.execute(f"DELETE FROM heats WHERE id IN ({ph})", old_heats)
    # ヒート優勝トーナメントもリセット
    await db.execute("DELETE FROM heat_finals WHERE tournament_id=?", (tid,))

    qual_type = dict(t).get("qualifying_type", "")

    if is_roundrobin(t):
        # 総当たり：全ペア生成
        schedule = generate_roundrobin_schedule(entry_ids)
        for race_no, lane_a, eid_a, lane_b, eid_b in schedule:
            await db.execute(
                "INSERT INTO heats (tournament_id, heat_no, group_no, status) VALUES (?,?,?,?)",
                (tid, race_no, 0, "pending"),
            )
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                heat_id = (await cur.fetchone())["id"]
            await db.execute("INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)", (heat_id, lane_a, eid_a))
            await db.execute("INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)", (heat_id, lane_b, eid_b))
    elif qual_type == "heat_roundrobin":
        # ヒート（総当たり）：ヒートN→グループ1→2→3の順
        heat_count  = dict(t).get("qual_heat_count", 1)
        group_count = dict(t).get("qual_group_count", 1)
        heat_exclude = bool(dict(t).get("qual_heat_exclude", 0))
        lane_count_rr = int(t["lane_count"] or 3)

        if heat_exclude and heat_count > 1:
            # qual_heat_exclude=1 のとき：1ヒート目のみ生成。
            # 2ヒート目以降は前ヒート完了後に generate-heat/{round_no} エンドポイントで個別生成。
            schedule_rr = generate_heat_roundrobin_schedule(
                list(entry_ids), 1, group_count, lane_count_rr
            )
            global_heat_no = 1
            for item in schedule_rr:
                await db.execute(
                    "INSERT INTO heats (tournament_id, heat_no, group_no, round_no, status) VALUES (?,?,?,?,?)",
                    (tid, global_heat_no, item["group_no"], 1, "pending"),
                )
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    heat_id = (await cur.fetchone())["id"]
                for lane_no, eid in enumerate(item["slots"], 1):
                    if eid is None:
                        continue
                    await db.execute(
                        "INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)",
                        (heat_id, lane_no, eid),
                    )
                global_heat_no += 1
        else:
            schedule = generate_heat_roundrobin_schedule(
                entry_ids, heat_count, group_count, lane_count_rr
            )
            for item in schedule:
                await db.execute(
                    "INSERT INTO heats (tournament_id, heat_no, group_no, round_no, status) VALUES (?,?,?,?,?)",
                    (tid, item["heat_no"], item["group_no"], item["round_no"], "pending"),
                )
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    heat_id = (await cur.fetchone())["id"]
                for lane_no, eid in enumerate(item["slots"], 1):
                    if eid is None:
                        continue
                    await db.execute(
                        "INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)",
                        (heat_id, lane_no, eid),
                    )
    elif qual_type == "point":
        # ポイント制：複数ラウンド
        round_count = int(dict(t).get("qual_round_count") or 1)
        lane_count  = int(t["lane_count"] or 3)
        all_schedule = generate_point_schedule(entry_ids, round_count, lane_count)
        heat_no = 1
        for rno, groups in enumerate(all_schedule, 1):
            for group in groups:
                await db.execute(
                    "INSERT INTO heats (tournament_id, heat_no, group_no, round_no, status) VALUES (?,?,?,?,?)",
                    (tid, heat_no, 0, rno, "pending"),
                )
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    heat_id = (await cur.fetchone())["id"]
                for lane_no, eid in enumerate(group, 1):
                    await db.execute("INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)", (heat_id, lane_no, eid))
                heat_no += 1
    elif qual_type == "none_roundrobin":
        # 即決勝（総当たり）: roundrobin と同じ 1v1 ペア生成
        schedule = generate_roundrobin_schedule(entry_ids)
        for race_no, lane_a, eid_a, lane_b, eid_b in schedule:
            await db.execute(
                "INSERT INTO heats (tournament_id, heat_no, group_no, status) VALUES (?,?,?,?)",
                (tid, race_no, 0, "pending"),
            )
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                heat_id = (await cur.fetchone())["id"]
            await db.execute("INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)", (heat_id, lane_a, eid_a))
            await db.execute("INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)", (heat_id, lane_b, eid_b))
    else:
        # ヒート制（トーナメント等）
        schedule = generate_heat_schedule(entry_ids, t["lane_count"])
        for heat_no, group in enumerate(schedule, 1):
            await db.execute(
                "INSERT INTO heats (tournament_id, heat_no, group_no, status) VALUES (?,?,?,?)",
                (tid, heat_no, 0, "pending"),
            )
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                heat_id = (await cur.fetchone())["id"]
            for lane_no, entry_id in enumerate(group, 1):
                await db.execute("INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)", (heat_id, lane_no, entry_id))

    await db.commit()

    # 参加者向けHTML配信（自動更新）
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass
    return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)


@router.post("/{tid}/qualifying/racer-add")
async def qualifying_racer_add(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """レーサー追加（途中参加）：現在の結果を維持したまま、まだ対戦が割り当てられていない
    エントリー（＝レース情報で後から追加したレーサー）ぶんの不足対戦を補填生成する。

    対応形式：総当たり（roundrobin）／ヒート（総当たり）（heat_roundrobin）／ポイント制（point）。
    各追加レーサー対既存全員の1vs1対戦を「未入力」で生成し、レーンは自動でバランスを取る。
    既存の対戦・結果（heat_results）は一切変更しない。
    """
    from app.routers.tournaments import _is_result_finalized
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url="/admin/tournaments/", status_code=303)

    qual_type = dict(t).get("qualifying_type", "")
    # 対象形式のみ（即決勝・ヒートトーナメントは対象外）
    if qual_type not in ("roundrobin", "heat_roundrobin", "point"):
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying?error=racer_add_unsupported", status_code=303)

    # 結果確定済み（優勝確定）なら不可
    if await _is_result_finalized(tid, db):
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying?error=finalized", status_code=303)

    lane_count = int(t["lane_count"] or 3)

    # 全アクティブエントリー
    async with db.execute(
        "SELECT id FROM entries WHERE tournament_id=? AND status='active' ORDER BY entry_order",
        (tid,),
    ) as cur:
        all_ids = [r["id"] for r in await cur.fetchall()]

    # 既に対戦（heat_lanes）に登場しているエントリー
    async with db.execute(
        """SELECT DISTINCT hl.entry_id FROM heat_lanes hl
           JOIN heats h ON h.id=hl.heat_id
           WHERE h.tournament_id=? AND hl.entry_id IS NOT NULL""",
        (tid,),
    ) as cur:
        existing_ids = {r["entry_id"] for r in await cur.fetchall()}

    new_ids = [e for e in all_ids if e not in existing_ids]
    if not new_ids:
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying?info=no_new_racer", status_code=303)

    # スケジュール未生成（既存対戦ゼロ）の場合は通常生成に委ねる
    if not existing_ids:
        return await qualifying_generate(tid, db)

    opponents = [e for e in all_ids if e not in new_ids]  # 既存対戦相手

    if qual_type in ("roundrobin",):
        await _supplement_roundrobin(tid, new_ids, opponents, db)
    elif qual_type == "heat_roundrobin":
        await _supplement_heat_roundrobin(tid, new_ids, opponents, lane_count, db)
    elif qual_type == "point":
        await _supplement_point(tid, new_ids, opponents, lane_count, db)

    await db.commit()
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass
    return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying?info=racer_added", status_code=303)


async def _next_race_no(tid: int, db) -> int:
    """総当たり/ポイント制で使う heat_no の次番号（末尾追加用）。"""
    async with db.execute(
        "SELECT COALESCE(MAX(heat_no),0) as m FROM heats WHERE tournament_id=?", (tid,)
    ) as cur:
        return int((await cur.fetchone())["m"]) + 1


async def _lane_usage(tid: int, db) -> dict:
    """各エントリーの使用レーン回数 {entry_id: {lane_no: count}} を返す（バランス用）。"""
    usage: dict = {}
    async with db.execute(
        """SELECT hl.entry_id, hl.lane_no FROM heat_lanes hl
           JOIN heats h ON h.id=hl.heat_id WHERE h.tournament_id=? AND hl.entry_id IS NOT NULL""",
        (tid,),
    ) as cur:
        for r in await cur.fetchall():
            usage.setdefault(r["entry_id"], {})
            usage[r["entry_id"]][r["lane_no"]] = usage[r["entry_id"]].get(r["lane_no"], 0) + 1
    return usage


def _pick_two_lanes(eid_a, eid_b, usage, lane_count):
    """2名の対戦で、双方がこれまで使用回数の少ないレーンを選ぶ（偏り低減）。"""
    lanes = list(range(1, max(2, lane_count) + 1))
    ua = usage.get(eid_a, {})
    ub = usage.get(eid_b, {})
    la = min(lanes, key=lambda L: ua.get(L, 0))
    lb = min([L for L in lanes if L != la], key=lambda L: ub.get(L, 0))
    usage.setdefault(eid_a, {}); usage.setdefault(eid_b, {})
    usage[eid_a][la] = usage[eid_a].get(la, 0) + 1
    usage[eid_b][lb] = usage[eid_b].get(lb, 0) + 1
    return la, lb


async def _supplement_roundrobin(tid, new_ids, opponents, db):
    """総当たり：各追加レーサー対既存全員＋追加レーサー同士の1vs1を末尾に補填。"""
    usage = await _lane_usage(tid, db)
    lane_count = 3
    async with db.execute("SELECT lane_count FROM tournaments WHERE id=?", (tid,)) as cur:
        row = await cur.fetchone()
        if row and row["lane_count"]:
            lane_count = int(row["lane_count"])
    race_no = await _next_race_no(tid, db)
    # 追加×既存 と 追加×追加 の全ペア
    pairs = [(n, o) for n in new_ids for o in opponents]
    for i in range(len(new_ids)):
        for j in range(i + 1, len(new_ids)):
            pairs.append((new_ids[i], new_ids[j]))
    for eid_a, eid_b in pairs:
        la, lb = _pick_two_lanes(eid_a, eid_b, usage, lane_count)
        await db.execute(
            "INSERT INTO heats (tournament_id, heat_no, group_no, status) VALUES (?,?,?,?)",
            (tid, race_no, 0, "pending"),
        )
        async with db.execute("SELECT last_insert_rowid() as id") as cur:
            heat_id = (await cur.fetchone())["id"]
        await db.execute("INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)", (heat_id, la, eid_a))
        await db.execute("INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)", (heat_id, lb, eid_b))
        race_no += 1


async def _supplement_heat_roundrobin(tid, new_ids, opponents, lane_count, db):
    """ヒート（総当たり）：各ヒート(round_no)・各追加レーサー対既存全員の1vs1を、
    既存のヒート番号体系の末尾 group_no として補填する。
    実装方針：既存の各 round_no（ヒート）ごとに、追加レーサーの対戦を補填する。
    round_no を持たない構成では単一ヒートとして扱う。"""
    usage = await _lane_usage(tid, db)
    async with db.execute(
        "SELECT DISTINCT round_no FROM heats WHERE tournament_id=? ORDER BY round_no", (tid,)
    ) as cur:
        round_nos = [r["round_no"] for r in await cur.fetchall()]
    if not round_nos:
        round_nos = [1]
    next_heat_no = await _next_race_no(tid, db)
    for rno in round_nos:
        pairs = [(n, o) for n in new_ids for o in opponents]
        for i in range(len(new_ids)):
            for j in range(i + 1, len(new_ids)):
                pairs.append((new_ids[i], new_ids[j]))
        for eid_a, eid_b in pairs:
            la, lb = _pick_two_lanes(eid_a, eid_b, usage, lane_count)
            await db.execute(
                "INSERT INTO heats (tournament_id, heat_no, group_no, round_no, status) VALUES (?,?,?,?,?)",
                (tid, next_heat_no, 0, rno if rno is not None else 1, "pending"),
            )
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                heat_id = (await cur.fetchone())["id"]
            await db.execute("INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)", (heat_id, la, eid_a))
            await db.execute("INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)", (heat_id, lb, eid_b))
            next_heat_no += 1


async def _supplement_point(tid, new_ids, opponents, lane_count, db):
    """ポイント制：各ラウンド(round_no)へ追加レーサーの走行を補填する。
    そのラウンドで既に走行済み（結果入力済み含む）のレーサーは相手候補から除外し、
    同一ラウンド・同一グループへの再出場を防ぐ。
    追加レーサーは、まず未入力2人グループへ挿入して3人化を優先し、
    余りは追加レーサー同士で2〜3人グループを作る。最後の1人は単独走とする。
    既存の結果（heat_results）は一切変更しない。"""
    usage = await _lane_usage(tid, db)
    async with db.execute(
        "SELECT DISTINCT round_no FROM heats WHERE tournament_id=? ORDER BY round_no", (tid,)
    ) as cur:
        round_nos = [r["round_no"] for r in await cur.fetchall()]
    if not round_nos:
        round_nos = [1]
    next_heat_no = await _next_race_no(tid, db)
    lc = max(2, lane_count)
    for rno in round_nos:
        # このラウンドで既に走行（heat_lanes 登場）しているエントリーを取得
        async with db.execute(
            """SELECT DISTINCT hl.entry_id FROM heat_lanes hl
               JOIN heats h ON h.id=hl.heat_id
               WHERE h.tournament_id=? AND COALESCE(h.round_no,1)=? AND hl.entry_id IS NOT NULL""",
            (tid, rno if rno is not None else 1),
        ) as cur:
            used_in_round = {r["entry_id"] for r in await cur.fetchall()}
        # このラウンドでまだ走っていない追加レーサーのみ補填対象
        queue = [e for e in new_ids if e not in used_in_round]
        if not queue:
            continue
        # lc 人ずつのグループに分割（3人化を優先・最後の1人は単独走を許容）
        groups = [queue[i:i + lc] for i in range(0, len(queue), lc)]
        for group in groups:
            await db.execute(
                "INSERT INTO heats (tournament_id, heat_no, group_no, round_no, status) VALUES (?,?,?,?,?)",
                (tid, next_heat_no, 0, rno if rno is not None else 1, "pending"),
            )
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                heat_id = (await cur.fetchone())["id"]
            for lane_no, eid in enumerate(group, 1):
                await db.execute("INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)", (heat_id, lane_no, eid))
                usage.setdefault(eid, {}); usage[eid][lane_no] = usage[eid].get(lane_no, 0) + 1
            next_heat_no += 1


# ── 結果入力フォーム ──────────────────────────────────────
@router.get("/{tid}/qualifying/heat/{heat_id}", response_class=HTMLResponse)
async def heat_result_form(tid: int, heat_id: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    async with db.execute("SELECT * FROM heats WHERE id=? AND tournament_id=?", (heat_id, tid)) as cur:
        heat = await cur.fetchone()
    if not heat:
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying")

    async with db.execute(
        """SELECT hl.id as lane_id, hl.lane_no, hl.entry_id,
                  r.name,
                  hr.best_time, hr.rank, hr.points, hr.win
           FROM heat_lanes hl
           JOIN entries e ON e.id=hl.entry_id
           JOIN racers r ON r.id=e.racer_id
           LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
           WHERE hl.heat_id=?
           ORDER BY hl.lane_no""",
        (heat_id,),
    ) as cur:
        lanes = await cur.fetchall()

    rr = is_roundrobin(t)
    hr = dict(t).get("qualifying_type") == "heat_roundrobin"
    tmpl = "admin/race_result.html" if rr else "admin/heat_result.html"
    return templates.TemplateResponse(tmpl, {
        "request": request,
        "t": t,
        "heat": heat,
        "lanes": lanes,
        "is_roundrobin": rr,
    })


# ── 結果保存 ──────────────────────────────────────────────
@router.get("/{tid}/qualifying/standings-json")
async def qualifying_standings_json(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """順位表をJSONで返す（保存後のリアルタイム更新用）"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return JSONResponse({"ok": False})
    rr = is_roundrobin(t)
    nr = dict(t).get("qualifying_type") == "none_roundrobin"
    standings = await _calc_standings_none_rr(tid, db) if nr else (await _calc_standings_rr(tid, db) if rr else await _calc_standings(tid, db))
    finalist_n = calc_finalists(dict(t).get("qualifying_type", ""), dict(t))
    if not nr and not rr and finalist_n and standings:
        cutoff_st = standings[finalist_n - 1] if finalist_n <= len(standings) else None
        if cutoff_st:
            cutoff_rank = cutoff_st.get("rank", None)
            cutoff_group = [s for s in standings if s.get("rank") == cutoff_rank]
            count_above = sum(1 for s in standings if s.get("rank", 99) < cutoff_rank)
            is_border = count_above < finalist_n < count_above + len(cutoff_group)
            for s in standings:
                s["is_tied_cutoff"] = is_border and s.get("rank") == cutoff_rank
    return JSONResponse({"ok": True, "standings": [dict(s) for s in standings]})


@router.post("/{tid}/qualifying/heat/{heat_id}/save-rank")
async def heat_result_save_rank(tid: int, heat_id: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """インライン順位入力の保存（ポイント制）"""
    form = await request.form()

    # レースのポイント設定を取得
    async with db.execute("SELECT point_1st, point_2nd, point_3rd, point_co FROM tournaments WHERE id=?", (tid,)) as cur:
        pt = await cur.fetchone()
    pt = dict(pt) if pt else {}
    point_map = {
        1: int(pt.get("point_1st") or 3),
        2: int(pt.get("point_2nd") or 2),
        3: int(pt.get("point_3rd") or 1),
        0: int(pt.get("point_co") or 0),
    }

    # lane情報を取得
    async with db.execute(
        "SELECT hl.id as lane_id, hl.entry_id FROM heat_lanes hl WHERE hl.heat_id=? ORDER BY hl.lane_no",
        (heat_id,),
    ) as cur:
        lanes = [dict(r) for r in await cur.fetchall()]

    for lane in lanes:
        eid = lane["entry_id"]
        co_val = form.get(f"co_{eid}", "0")
        rank_val = form.get(f"rank_{eid}")
        is_co = 1 if co_val == "1" else 0

        # COまたはrank指定がある場合のみ保存
        if rank_val is None and not is_co:
            continue

        rank = int(rank_val) if rank_val else 0
        points = point_map.get(0 if is_co else rank, 0)
        win = 1 if (rank == 1 and not is_co) else 0

        await db.execute("DELETE FROM heat_results WHERE heat_lane_id=?", (lane["lane_id"],))
        await db.execute(
            "INSERT INTO heat_results (heat_lane_id, win, best_time, lap_count, rank, points, is_co) VALUES (?,?,0,0,?,?,?)",
            (lane["lane_id"], win, rank, points, is_co),
        )

    # 全レーンの結果が保存済みなら完了
    async with db.execute(
        """SELECT COUNT(*) as total FROM heat_lanes WHERE heat_id=?""", (heat_id,)
    ) as cur:
        total = (await cur.fetchone())["total"]
    async with db.execute(
        """SELECT COUNT(*) as done FROM heat_results hr
           JOIN heat_lanes hl ON hl.id=hr.heat_lane_id WHERE hl.heat_id=?""", (heat_id,)
    ) as cur:
        done = (await cur.fetchone())["done"]
    if done >= total:
        await db.execute("UPDATE heats SET status='done' WHERE id=?", (heat_id,))
    await db.commit()

    # 参加者向けHTML配信（自動更新）
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass

    return JSONResponse({"ok": True, "status": "done"})


@router.post("/{tid}/qualifying/heat/{heat_id}/reset")
async def heat_result_reset(tid: int, heat_id: int, db: aiosqlite.Connection = Depends(get_db)):
    """レース結果をリセット"""
    async with db.execute("SELECT id FROM heat_lanes WHERE heat_id=?", (heat_id,)) as cur:
        lane_ids = [r["id"] for r in await cur.fetchall()]
    for lid in lane_ids:
        await db.execute("DELETE FROM heat_results WHERE heat_lane_id=?", (lid,))
    await db.execute("UPDATE heats SET status='prepare' WHERE id=?", (heat_id,))
    await db.commit()

    return JSONResponse({"ok": True})


@router.post("/{tid}/qualifying/heat/{heat_id}/save")
async def heat_result_save(tid: int, heat_id: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()

    form = await request.form()
    async with db.execute(
        "SELECT hl.id as lane_id, hl.lane_no FROM heat_lanes hl WHERE hl.heat_id=? ORDER BY hl.lane_no",
        (heat_id,),
    ) as cur:
        lanes = await cur.fetchall()

    if is_roundrobin(t):
        # 総当たり：○×＋タイム
        assert len(lanes) == 2, "総当たりは2レーンのみ"
        l0, l1 = lanes[0]["lane_id"], lanes[1]["lane_id"]
        win0_raw = form.get(f"win_{l0}", "")
        win0 = 1 if win0_raw == "1" else (0 if win0_raw == "0" else None)
        win1 = (1 - win0) if win0 is not None else None
        t0_raw = form.get(f"time_{l0}", "").strip()
        t1_raw = form.get(f"time_{l1}", "").strip()
        time0 = float(t0_raw) if t0_raw else None
        time1 = float(t1_raw) if t1_raw else None

        for lid, win, best_time in [(l0, win0, time0), (l1, win1, time1)]:
            await db.execute("DELETE FROM heat_results WHERE heat_lane_id=?", (lid,))
            await db.execute(
                "INSERT INTO heat_results (heat_lane_id, win, best_time, lap_count, rank, points) VALUES (?,?,?,0,0,0)",
                (lid, win, best_time),
            )
    else:
        # ヒート制：周回数＋タイム→順位→ポイント
        results = []
        for lane in lanes:
            lid = lane["lane_id"]
            lap_count = int(form.get(f"lap_{lid}") or 0)
            t_raw = form.get(f"time_{lid}", "").strip()
            best_time = float(t_raw) if t_raw else None
            results.append({"lane_id": lid, "lap_count": lap_count, "best_time": best_time})

        results_sorted = sorted(results, key=lambda r: (-(r["lap_count"] or 0), r["best_time"] or 99999))
        for rank, r in enumerate(results_sorted, 1):
            r["rank"] = rank
            r["points"] = calc_points(rank)

        for r in results:
            await db.execute("DELETE FROM heat_results WHERE heat_lane_id=?", (r["lane_id"],))
            await db.execute(
                "INSERT INTO heat_results (heat_lane_id, lap_count, best_time, rank, points) VALUES (?,?,?,?,?)",
                (r["lane_id"], r["lap_count"], r["best_time"], r["rank"], r["points"]),
            )

    await db.execute("UPDATE heats SET status='done' WHERE id=?", (heat_id,))
    await db.commit()
    # 参加者向けHTML配信（自動更新）
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass

    return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying", status_code=303)


# ── 順位計算（ヒート制）────────────────────────────────────
@router.post("/{tid}/qualifying/heat/{heat_id}/save-inline")
async def heat_result_save_inline(tid: int, heat_id: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """インライン保存（JSONレスポンス）"""
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    form = await request.form()
    async with db.execute(
        "SELECT hl.id as lane_id, hl.lane_no FROM heat_lanes hl WHERE hl.heat_id=? ORDER BY hl.lane_no",
        (heat_id,),
    ) as cur:
        lanes = await cur.fetchall()

    nr_type = dict(t).get("qualifying_type") == "none_roundrobin"
    if is_roundrobin(t) or dict(t).get("qualifying_type") in ("heat_roundrobin", "none_roundrobin"):
        # 2名以上のレーンに対応（none_roundrobin決定戦は2名以上の可能性あり）
        if len(lanes) < 2:
            return JSONResponse({"ok": False, "error": "lane error"})
        l0, l1 = lanes[0]["lane_id"], lanes[1]["lane_id"]
        win0_raw = form.get(f"win_{l0}", "")
        co0 = form.get(f"co_{l0}", "0") == "1"
        win0 = 1 if win0_raw == "1" else (0 if win0_raw == "0" else None)
        win1_raw = form.get(f"win_{l1}", "")
        co1 = form.get(f"co_{l1}", "0") == "1"
        # 相手のwin値: 明示的に送られてくる場合はそれを使う
        if win1_raw != "":
            win1 = 1 if win1_raw == "1" else 0
        else:
            win1 = (1 - win0) if win0 is not None else None
        t0 = form.get(f"time_{l0}", "").strip()
        t1 = form.get(f"time_{l1}", "").strip()
        time0 = float(t0) if t0 else None
        time1 = float(t1) if t1 else None
        for lid, win, bt, is_co in [(l0, win0, time0, co0), (l1, win1, time1, co1)]:
            await db.execute("DELETE FROM heat_results WHERE heat_lane_id=?", (lid,))
            await db.execute(
                "INSERT INTO heat_results (heat_lane_id, win, best_time, lap_count, rank, points, is_co) VALUES (?,?,?,0,0,0,?)",
                (lid, win, bt, 1 if is_co else 0),
            )
        status = "done" if win0 is not None else "pending"
        await db.execute("UPDATE heats SET status=? WHERE id=?", (status, heat_id))
        await db.commit()
        return JSONResponse({"ok": True, "status": status})
    else:
        results = []
        for lane in lanes:
            lid = lane["lane_id"]
            lap_count = int(form.get(f"lap_{lid}") or 0)
            t_raw = form.get(f"time_{lid}", "").strip()
            best_time = float(t_raw) if t_raw else None
            results.append({"lane_id": lid, "lap_count": lap_count, "best_time": best_time})
        results_sorted = sorted(results, key=lambda r: (-(r["lap_count"] or 0), r["best_time"] or 99999))
        for rank, r in enumerate(results_sorted, 1):
            r["rank"] = rank
            r["points"] = calc_points(rank)
        for r in results:
            await db.execute("DELETE FROM heat_results WHERE heat_lane_id=?", (r["lane_id"],))
            await db.execute(
                "INSERT INTO heat_results (heat_lane_id, lap_count, best_time, rank, points) VALUES (?,?,?,?,?)",
                (r["lane_id"], r["lap_count"], r["best_time"], r["rank"], r["points"]),
            )
        await db.execute("UPDATE heats SET status='done' WHERE id=?", (heat_id,))
        await db.commit()

    # 参加者向けHTML配信（自動更新）
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass
        return JSONResponse({"ok": True, "status": "done"})


@router.post("/{tid}/qualifying/auto-advanced")
async def auto_advanced(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """予選順位から決勝進出を自動集計してDBに保存"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return JSONResponse({"ok": False})

    from app.routers.tournaments import calc_finalists as calc_n
    t_dict = dict(t)
    finalist_n = calc_n(t_dict.get("qualifying_type",""), t_dict)

    # 現在の順位取得
    nr = t_dict.get("qualifying_type") == "none_roundrobin"
    rr = t_dict.get("qualifying_type") == "roundrobin"
    standings = await _calc_standings_none_rr(tid, db) if nr else (await _calc_standings_rr(tid, db) if rr else await _calc_standings(tid, db))

    if not standings:
        return JSONResponse({"ok": False, "error": "standings or finalist_n not set"})

    # none_roundrobin は全員が決勝対象
    if finalist_n is None:
        finalist_n = len(standings)

    # 上位finalist_n位のrankを確認
    # finalist_n番目のrankを取得
    cutoff_rank = standings[finalist_n - 1]["rank"] if finalist_n <= len(standings) else None

    results = []
    for s in standings:
        entry_id = s["entry_id"]
        rank = s["rank"]
        if cutoff_rank is None:
            val = None
        elif rank < cutoff_rank:
            # 明確に上位 → ○
            val = 1
        elif rank == cutoff_rank:
            # 同率境界 → 上位finalist_n名ちょうどか確認
            # cutoff_rankのレーサーが何名いるか
            same_rank_count = sum(1 for x in standings if x["rank"] == cutoff_rank)
            # cutoff_rankより上の確定人数
            confirmed_above = sum(1 for x in standings if x["rank"] < cutoff_rank)
            remaining_slots = finalist_n - confirmed_above
            if same_rank_count == remaining_slots:
                # 同率の全員がちょうど収まる → ○
                val = 1
            else:
                # 同率で絞れない → 未入力（手動で設定してください）
                val = None
        else:
            # 明確に下位 → ×
            val = 0
        await db.execute(
            "UPDATE entries SET advanced=? WHERE id=? AND tournament_id=?",
            (val, entry_id, tid),
        )
        results.append({"entry_id": entry_id, "advanced": val})

    await db.commit()

    # 参加者向けHTML配信（自動更新）
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass
    return JSONResponse({"ok": True, "results": results})


@router.post("/{tid}/qualifying/set-advanced/{entry_id}")
async def set_advanced(
    tid: int,
    entry_id: int,
    advanced: int = Form(...),  # 1=進出, 0=敗退, -1=未設定
    db: aiosqlite.Connection = Depends(get_db),
):
    val = None if advanced == -1 else advanced
    await db.execute(
        "UPDATE entries SET advanced=? WHERE id=? AND tournament_id=?",
        (val, entry_id, tid),
    )
    await db.commit()
    return JSONResponse({"ok": True, "advanced": val})


async def _calc_standings(tid: int, db: aiosqlite.Connection):
    async with db.execute(
        """SELECT e.id as entry_id, e.advanced, r.name, COALESCE(r.yomi,'') as yomi,
                  COALESCE(SUM(hr.points),0) as total_points,
                  MIN(CASE WHEN hr.best_time > 0 THEN hr.best_time END) as best_time,
                  COUNT(hr.id) as race_count,
                  COALESCE(SUM(CASE WHEN COALESCE(hr.is_co,0)=0 AND hr.rank > 0 THEN 1 ELSE 0 END),0) as finish_count
           FROM entries e
           JOIN racers r ON r.id=e.racer_id
           LEFT JOIN heat_lanes hl ON hl.entry_id=e.id
             AND hl.heat_id IN (SELECT id FROM heats WHERE tournament_id=?)
           LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
           WHERE e.tournament_id=? AND e.status='active'
           GROUP BY e.id
           ORDER BY total_points DESC, best_time ASC NULLS LAST""",
        (tid, tid),
    ) as cur:
        rows = await cur.fetchall()
    # 同率処理: 同ポイントは同rank
    standings = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0:
            prev = standings[-1]
            if row["total_points"] == prev["total_points"]:
                rank = prev["rank"]
            else:
                rank = i + 1
        standings.append({**dict(row), "rank": rank})
    return standings


# ── 順位計算（総当たり）────────────────────────────────────
async def _calc_standings_rr(tid: int, db: aiosqlite.Connection):
    """勝ち数多い順→同率あり（タイムは順位に影響しない）"""
    async with db.execute(
        """SELECT e.id as entry_id, e.advanced, r.name, COALESCE(r.yomi,'') as yomi,
                  COALESCE(SUM(hr.win),0) as wins,
                  COUNT(CASE WHEN hr.win IS NOT NULL THEN 1 END) as races
           FROM entries e
           JOIN racers r ON r.id=e.racer_id
           LEFT JOIN heat_lanes hl ON hl.entry_id=e.id
           LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
           LEFT JOIN heats h ON h.id=hl.heat_id AND h.tournament_id=?
           WHERE e.tournament_id=? AND e.status='active'
           GROUP BY e.id
           ORDER BY wins DESC""",
        (tid, tid),
    ) as cur:
        rows = await cur.fetchall()

    # 同率処理: 勝ち数が同じなら同率
    standings = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0:
            prev = standings[-1]
            if row["wins"] == prev["wins"]:
                rank = prev["rank"]
            else:
                rank = i + 1
        standings.append({**dict(row), "rank": rank})
    return standings


async def _calc_standings_none_rr(tid: int, db: aiosqlite.Connection):
    """即決勝総当たり専用: 勝ち数多い → 負け数少ない → 同率（COは集計のみ）"""
    async with db.execute(
        """SELECT e.id as entry_id, e.advanced, r.name, COALESCE(r.yomi,'') as yomi,
                  COALESCE(SUM(hr.win),0)                              as wins,
                  COALESCE(SUM(CASE WHEN hr.win=0 THEN 1 ELSE 0 END),0) as losses,
                  COALESCE(SUM(COALESCE(hr.is_co,0)),0)                as cos,
                  COUNT(CASE WHEN hr.win IS NOT NULL THEN 1 END)       as races
           FROM entries e
           JOIN racers r ON r.id=e.racer_id
           LEFT JOIN heat_lanes hl ON hl.entry_id=e.id
           LEFT JOIN heat_results hr ON hr.heat_lane_id=hl.id
           LEFT JOIN heats h ON h.id=hl.heat_id
             AND h.tournament_id=?
             AND h.deciding_position IS NULL
           WHERE e.tournament_id=? AND e.status='active'
           GROUP BY e.id
           ORDER BY wins DESC, losses ASC""",
        (tid, tid),
    ) as cur:
        rows = await cur.fetchall()

    standings = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0:
            prev = standings[-1]
            # 勝ち・負けが同じ → 同率（COは順位に影響しない）
            if (row["wins"] == prev["wins"]
                    and row["losses"] == prev["losses"]):
                rank = prev["rank"]
            else:
                rank = i + 1
        standings.append({**dict(row), "rank": rank})
    return standings


@router.post("/{tid}/qualifying/playoff/generate")
async def playoff_generate(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """決勝進出決定戦（プレーオフ）のスロットを生成"""
    form = await request.form()
    round_no = int(form.get("round_no", 0))
    group_no = int(form.get("group_no", 0))

    # 既存削除
    await db.execute(
        "DELETE FROM heat_finals WHERE tournament_id=? AND round_no=? AND group_no=? AND final_type='playoff'",
        (tid, round_no, group_no),
    )

    # 同率の人を取得
    standings = await _calc_standings_group_round(tid, group_no, round_no, db)
    async with db.execute("SELECT qual_group_advance, qual_heat_final FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    t_dict_po = dict(t) if t else {}
    # ヒート決勝あり → 1位同率を対象。なし → qual_group_advance 位同率を対象
    if t_dict_po.get("qual_heat_final"):
        group_advance = 1
    else:
        group_advance = t_dict_po.get("qual_group_advance", 1) or 1

    if not standings:
        return JSONResponse({"ok": False})

    border_wins = standings[group_advance - 1]["wins"] if len(standings) >= group_advance else 0
    tied = [s for s in standings if s["wins"] == border_wins]

    for slot_no, s in enumerate(tied, 1):
        await db.execute(
            "INSERT INTO heat_finals (tournament_id, round_no, group_no, slot_no, entry_id, final_type) VALUES (?,?,?,?,?,?)",
            (tid, round_no, group_no, slot_no, s["entry_id"], "playoff"),
        )

    await db.commit()

    # 参加者向けHTML配信（自動更新）
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass
    return JSONResponse({"ok": True})


@router.post("/{tid}/qualifying/playoff/save")
async def playoff_save(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """決勝進出決定戦の結果保存（○トグル）"""
    form = await request.form()
    round_no = int(form.get("round_no", 0))
    group_no = int(form.get("group_no", 0))
    entry_id = form.get("entry_id")
    if entry_id is None:
        return JSONResponse({"ok": False})
    entry_id = int(entry_id)

    async with db.execute("SELECT qual_group_advance, qual_heat_final FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    t_dict_ps = dict(t) if t else {}
    # ヒート決勝あり → 1位争い（常に1枠）。なし → qual_group_advance 枠
    if t_dict_ps.get("qual_heat_final"):
        group_advance = 1
    else:
        group_advance = t_dict_ps.get("qual_group_advance", 1) or 1

    # 確定済みの上位者数（同率プレーオフで争う枠数を計算）
    standings = await _calc_standings_group_round(tid, group_no, round_no, db)
    if not standings or len(standings) < group_advance:
        playoff_spots = 1
    else:
        border_wins = standings[group_advance - 1]["wins"]
        already_through = len([s for s in standings if s["wins"] > border_wins])
        playoff_spots = max(1, group_advance - already_through)

    # トグル
    async with db.execute(
        "SELECT winner_entry_id FROM heat_finals WHERE tournament_id=? AND round_no=? AND group_no=? AND entry_id=? AND final_type='playoff'",
        (tid, round_no, group_no, entry_id),
    ) as cur:
        slot = await cur.fetchone()
    already_winner = slot and slot["winner_entry_id"] is not None and int(slot["winner_entry_id"]) == int(entry_id)

    if already_winner:
        await db.execute(
            "UPDATE heat_finals SET winner_entry_id=NULL WHERE tournament_id=? AND round_no=? AND group_no=? AND entry_id=? AND final_type='playoff'",
            (tid, round_no, group_no, entry_id),
        )
    else:
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM heat_finals WHERE tournament_id=? AND round_no=? AND group_no=? AND final_type='playoff' AND winner_entry_id IS NOT NULL",
            (tid, round_no, group_no),
        ) as cur:
            cnt = (await cur.fetchone())["cnt"]
        if cnt >= playoff_spots:
            return JSONResponse({"ok": False, "error": f"上限{playoff_spots}名に達しています"})
        await db.execute(
            "UPDATE heat_finals SET winner_entry_id=entry_id WHERE tournament_id=? AND round_no=? AND group_no=? AND entry_id=? AND final_type='playoff'",
            (tid, round_no, group_no, entry_id),
        )

    await _recalc_advanced_heat_roundrobin(tid, db)

    # 参加者向けHTML配信（自動更新）
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass
    return JSONResponse({"ok": True})


@router.post("/{tid}/qualifying/heat-final/save")
async def heat_final_save(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ヒート優勝トーナメントの結果保存（○×トグル）"""
    form = await request.form()
    round_no = int(form.get("round_no", 0))
    entry_id = form.get("entry_id")
    if entry_id is None:
        return JSONResponse({"ok": False, "error": "entry_id missing"})
    entry_id = int(entry_id)

    # 上位通過人数を取得
    async with db.execute("SELECT qual_heat_final_advance FROM tournaments WHERE id=?", (tid,)) as cur:
        t_row = await cur.fetchone()
    advance_n = dict(t_row).get("qual_heat_final_advance", 1) if t_row else 1

    # トグル：既に○なら取消、そうでなければ追加（final_type='heat'のみ対象）
    async with db.execute(
        "SELECT winner_entry_id FROM heat_finals WHERE tournament_id=? AND round_no=? AND entry_id=? AND (final_type='heat' OR final_type IS NULL)",
        (tid, round_no, entry_id),
    ) as cur:
        slot = await cur.fetchone()
    already_winner = slot is not None and slot["winner_entry_id"] is not None and int(slot["winner_entry_id"]) == int(entry_id)

    if already_winner:
        await db.execute(
            "UPDATE heat_finals SET winner_entry_id=NULL WHERE tournament_id=? AND round_no=? AND entry_id=? AND (final_type='heat' OR final_type IS NULL)",
            (tid, round_no, entry_id),
        )
    else:
        # ヒート優勝トーナメントの勝者のみカウント（プレーオフは除く）
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM heat_finals WHERE tournament_id=? AND round_no=? AND winner_entry_id IS NOT NULL AND (final_type='heat' OR final_type IS NULL)",
            (tid, round_no),
        ) as cur:
            current_count = (await cur.fetchone())["cnt"]
        if current_count >= advance_n:
            return JSONResponse({"ok": False, "error": f"上限{advance_n}名に達しています"})
        await db.execute(
            "UPDATE heat_finals SET winner_entry_id=entry_id WHERE tournament_id=? AND round_no=? AND entry_id=? AND (final_type='heat' OR final_type IS NULL)",
            (tid, round_no, entry_id),
        )

    await _recalc_advanced_heat_roundrobin(tid, db)
    return JSONResponse({"ok": True})


@router.post("/{tid}/qualifying/heat-final/rank")
async def heat_final_rank(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ヒート優勝トーナメントの順位入力（順位選択方式）"""
    form = await request.form()
    round_no = int(form.get("round_no", 0))
    entry_id = int(form.get("entry_id", 0))
    rank = form.get("rank", "")
    rank_val = int(rank) if rank else None

    # heat_finalsのdeciding_rankカラムに順位を保存
    # カラムが存在しない場合は追加（マイグレーション）
    async with db.execute("PRAGMA table_info(heat_finals)") as cur:
        cols = [r[1] for r in await cur.fetchall()]
    if "deciding_rank" not in cols:
        await db.execute("ALTER TABLE heat_finals ADD COLUMN deciding_rank INTEGER")
        await db.commit()

    # 同じ順位が既に別エントリーに設定されていれば解除
    if rank_val:
        await db.execute(
            "UPDATE heat_finals SET deciding_rank=NULL WHERE tournament_id=? AND round_no=? AND deciding_rank=? AND (final_type='heat' OR final_type IS NULL)",
            (tid, round_no, rank_val),
        )
    await db.execute(
        "UPDATE heat_finals SET deciding_rank=? WHERE tournament_id=? AND round_no=? AND entry_id=? AND (final_type='heat' OR final_type IS NULL)",
        (rank_val, tid, round_no, entry_id),
    )

    # advanced を deciding_rank から再計算
    async with db.execute("SELECT qual_heat_final_advance FROM tournaments WHERE id=?", (tid,)) as cur:
        t_row = await cur.fetchone()
    advance_n = dict(t_row).get("qual_heat_final_advance", 1) if t_row else 1
    heat_count = dict(t_row).get("qual_heat_count", 1) if t_row else 1

    # 全ヒートの決勝進出者（deciding_rank <= advance_n）を取得してadvancedを更新
    await db.execute("UPDATE entries SET advanced=NULL WHERE tournament_id=?", (tid,))
    async with db.execute(
        "SELECT entry_id FROM heat_finals WHERE tournament_id=? AND deciding_rank<=? AND (final_type='heat' OR final_type IS NULL)",
        (tid, advance_n),
    ) as cur:
        adv_rows = await cur.fetchall()
    for row in adv_rows:
        if row["entry_id"]:
            await db.execute("UPDATE entries SET advanced=1 WHERE id=?", (row["entry_id"],))

    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/{tid}/qualifying/heat-final/generate")
async def heat_final_generate(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ヒート優勝トーナメントのスロットを生成（上位者を自動配置）"""
    form = await request.form()
    round_no = int(form.get("round_no", 0))

    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    # ヒート決勝へ進出するのは各グループから1名のみ（グループ優勝者）
    # qual_group_advance は直接決勝進出（heat_finalなし）の場合に使用
    group_advance = 1

    async with db.execute(
        "SELECT DISTINCT group_no FROM heats WHERE tournament_id=? AND round_no=? ORDER BY group_no",
        (tid, round_no),
    ) as cur:
        group_nos = [r["group_no"] for r in await cur.fetchall()]

    # 既存データのうち、ヒート優勝トーナメント（final_type='heat'）のみ削除
    # プレーオフ（final_type='playoff'）は残す
    await db.execute(
        "DELETE FROM heat_finals WHERE tournament_id=? AND round_no=? AND (final_type='heat' OR final_type IS NULL)",
        (tid, round_no),
    )

    # 各グループから1名のグループ代表を決定
    # 優先順位: 1) プレーオフ勝者 2) 単独1位 3) 同率（プレーオフ未実施）→スキップ
    candidates = []
    seen = set()

    for gno in group_nos:
        # まずプレーオフ勝者を確認
        async with db.execute(
            """SELECT hf.entry_id FROM heat_finals hf
               WHERE hf.tournament_id=? AND hf.round_no=? AND hf.group_no=?
                 AND hf.final_type='playoff' AND hf.winner_entry_id IS NOT NULL
               LIMIT 1""",
            (tid, round_no, gno),
        ) as cur:
            po_winner = await cur.fetchone()
        if po_winner:
            eid = po_winner["entry_id"]
            if eid not in seen:
                candidates.append(eid)
                seen.add(eid)
            continue

        # プレーオフ勝者なし → グループ順位を確認
        standings = await _calc_standings_group_round(tid, gno, round_no, db)
        if not standings:
            continue
        if len(standings) == 1 or standings[0]["wins"] > standings[1]["wins"]:
            # 単独1位 → グループ代表
            eid = standings[0]["entry_id"]
            if eid not in seen:
                candidates.append(eid)
                seen.add(eid)
        # 同率1位でプレーオフ未実施 → スキップ（決定戦が必要）

    # ヒート優勝トーナメントに配置
    for slot_no, entry_id in enumerate(candidates, 1):
        await db.execute(
            "INSERT INTO heat_finals (tournament_id, round_no, group_no, slot_no, entry_id, final_type) VALUES (?,?,?,?,?,'heat')",
            (tid, round_no, 0, slot_no, entry_id),
        )

    await db.commit()
    return JSONResponse({"ok": True})


async def _calc_standings_group(tid: int, group_no: int, db: aiosqlite.Connection) -> list[dict]:
    """ヒート総当たりのグループ別スタンディング（勝ち数→タイム合計）"""
    async with db.execute(
        """SELECT e.id as entry_id, e.advanced, r.name,
                  COALESCE(SUM(sub.win), 0) as wins,
                  COUNT(CASE WHEN sub.win IS NOT NULL THEN 1 END) as races,
                  SUM(CASE WHEN sub.best_time IS NOT NULL THEN sub.best_time END) as time_sum
           FROM entries e
           JOIN racers r ON r.id=e.racer_id
           LEFT JOIN (
               SELECT hl2.entry_id, hr2.win, hr2.best_time
               FROM heat_lanes hl2
               JOIN heats h2 ON h2.id=hl2.heat_id
               LEFT JOIN heat_results hr2 ON hr2.heat_lane_id=hl2.id
               WHERE h2.tournament_id=? AND h2.group_no=?
           ) sub ON sub.entry_id=e.id
           WHERE e.tournament_id=? AND e.status='active'
             AND EXISTS (
               SELECT 1 FROM heat_lanes hl3
               JOIN heats h3 ON h3.id=hl3.heat_id
               WHERE hl3.entry_id=e.id AND h3.tournament_id=? AND h3.group_no=?
             )
           GROUP BY e.id
           ORDER BY wins DESC, time_sum ASC""",
        (tid, group_no, tid, tid, group_no),
    ) as cur:
        rows = await cur.fetchall()

    standings = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0:
            prev = rows[i-1]
            if row["wins"] != prev["wins"]:
                rank = i + 1
        standings.append({**dict(row), "rank": rank})
    return standings


async def _recalc_advanced_heat_roundrobin(tid: int, db: aiosqlite.Connection):
    """
    ヒート総当たりのadvancedを全再計算。
    ヒート決勝あり（qual_heat_final=1）: ヒート決勝勝者のみ
    ヒート決勝なし: 各グループ確定通過者 + プレーオフ勝者
    """
    await db.execute("UPDATE entries SET advanced=NULL WHERE tournament_id=?", (tid,))

    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return
    t_dict = dict(t)
    group_advance = t_dict.get("qual_group_advance", 1)
    has_heat_final = bool(t_dict.get("qual_heat_final", 0))

    if has_heat_final:
        # ヒート決勝あり → ヒート決勝（final_type='heat'）の勝者のみ決勝進出
        async with db.execute(
            """SELECT DISTINCT entry_id FROM heat_finals
               WHERE tournament_id=? AND winner_entry_id IS NOT NULL
               AND (final_type='heat' OR final_type IS NULL) AND group_no=0""",
            (tid,),
        ) as cur:
            winners = [r["entry_id"] for r in await cur.fetchall() if r["entry_id"]]
        for wid in winners:
            await db.execute("UPDATE entries SET advanced=1 WHERE id=?", (wid,))
    else:
        # ヒート決勝なし → 各グループ×ヒートの上位者を集計
        # 重複可（qual_heat_exclude=0）: 同一人が複数ヒートで通過→advanced にスロット数を加算
        # 重複不可（qual_heat_exclude=1）: 進出済みはスキップして次点を繰り上げ
        async with db.execute(
            "SELECT DISTINCT round_no, group_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY round_no, group_no",
            (tid,),
        ) as cur:
            rg_pairs = [(r["round_no"], r["group_no"]) for r in await cur.fetchall()]

        qual_heat_exclude = bool(t_dict.get("qual_heat_exclude", 0))
        # advanced_count: {entry_id: 通過スロット数}
        advanced_count: dict = {}

        for rno, gno in rg_pairs:
            standings = await _calc_standings_group_round(tid, gno, rno, db)
            if not standings:
                continue

            # プレーオフ勝者を確認
            async with db.execute(
                """SELECT entry_id FROM heat_finals
                   WHERE tournament_id=? AND round_no=? AND group_no=?
                     AND final_type='playoff' AND winner_entry_id IS NOT NULL""",
                (tid, rno, gno),
            ) as cur:
                po_winner_ids = {r["entry_id"] for r in await cur.fetchall()}

            spots = group_advance
            selected = 0
            # ボーダーラインの勝数
            border_wins = standings[min(spots, len(standings)) - 1]["wins"] if len(standings) >= spots else -1
            for s in standings:
                if selected >= spots:
                    break
                eid = s["entry_id"]
                if qual_heat_exclude and eid in advanced_count:
                    continue
                # ボーダーライン上で同率タイ → プレーオフがある場合はプレーオフ勝者のみ
                if s["wins"] == border_wins and po_winner_ids:
                    if eid not in po_winner_ids:
                        continue
                advanced_count[eid] = advanced_count.get(eid, 0) + 1
                selected += 1

        # advanced カラムにスロット数を書き込む（1以上 = 進出）
        for eid, count in advanced_count.items():
            await db.execute("UPDATE entries SET advanced=? WHERE id=?", (count, eid))

        # プレーオフ勝者も追加
        async with db.execute(
            "SELECT DISTINCT entry_id FROM heat_finals WHERE tournament_id=? AND winner_entry_id IS NOT NULL AND final_type='playoff'",
            (tid,),
        ) as cur:
            playoff_winners = [r["entry_id"] for r in await cur.fetchall() if r["entry_id"]]
        for wid in playoff_winners:
            await db.execute("UPDATE entries SET advanced=1 WHERE id=?", (wid,))

    await db.commit()


async def _get_playoff_data(tid: int, db: aiosqlite.Connection) -> dict:
    """
    各ヒート×グループで同率により決勝進出決定戦が必要なケースのデータを返す。
    {(round_no, group_no): {"tied": [standings], "slots": [heat_finals rows], "all_done": bool}}
    """
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return {}

    t_dict_pd = dict(t)
    # ヒート決勝あり → 1位争い（1枠）。なし → qual_group_advance 枠
    if t_dict_pd.get("qual_heat_final"):
        group_advance = 1
    else:
        group_advance = t_dict_pd.get("qual_group_advance", 1) or 1

    async with db.execute(
        "SELECT DISTINCT round_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY round_no",
        (tid,),
    ) as cur:
        round_nos = [r["round_no"] for r in await cur.fetchall()]

    result = {}
    for rno in round_nos:
        async with db.execute(
            "SELECT DISTINCT group_no FROM heats WHERE tournament_id=? AND round_no=? ORDER BY group_no",
            (tid, rno),
        ) as cur:
            group_nos = [r["group_no"] for r in await cur.fetchall()]

        for gno in group_nos:
            standings = await _calc_standings_group_round(tid, gno, rno, db)
            if not standings:
                continue

            # 上位通過ラインの勝数を確認
            if len(standings) <= group_advance:
                continue

            # group_advance番目の人の勝数
            border_wins = standings[group_advance - 1]["wins"] if len(standings) >= group_advance else 0
            # border_wins以上の人数
            above_count = sum(1 for s in standings if s["wins"] >= border_wins)

            # 同率で通過人数を超える場合のみプレーオフが必要
            if above_count <= group_advance:
                continue

            # 同率の人たち（border_winsと同じ勝数）
            tied = [s for s in standings if s["wins"] == border_wins]

            # すでに確定している上位者（border_winsより多い）
            already_through = [s for s in standings if s["wins"] > border_wins]
            # プレーオフで争う枠数
            playoff_spots = group_advance - len(already_through)

            # このヒート×グループのプレーオフスロットを取得
            async with db.execute(
                """SELECT hf.slot_no, hf.entry_id, hf.winner_entry_id, r.name
                   FROM heat_finals hf
                   LEFT JOIN entries e ON e.id=hf.entry_id
                   LEFT JOIN racers r ON r.id=e.racer_id
                   WHERE hf.tournament_id=? AND hf.round_no=? AND hf.group_no=?
                     AND hf.final_type='playoff'
                   ORDER BY hf.slot_no""",
                (tid, rno, gno),
            ) as cur:
                slots = [dict(r) for r in await cur.fetchall()]

            # このヒートの全レースが完了しているか
            async with db.execute(
                "SELECT COUNT(*) as total FROM heats WHERE tournament_id=? AND round_no=? AND group_no>0", (tid, rno)
            ) as cur2:
                total = (await cur2.fetchone())["total"]
            async with db.execute(
                "SELECT COUNT(*) as done FROM heats WHERE tournament_id=? AND round_no=? AND group_no>0 AND status='done'", (tid, rno)
            ) as cur2:
                done = (await cur2.fetchone())["done"]

            result[(rno, gno)] = {
                "round_no": rno,
                "group_no": gno,
                "tied": tied,
                "slots": slots,
                "playoff_spots": playoff_spots,
                "already_through": already_through,
                "all_done": total > 0 and total == done,
            }

    return result


async def _get_heat_final_data(tid: int, db: aiosqlite.Connection) -> dict:
    """
    ヒート優勝トーナメントのデータを取得。
    round_no（ヒート番号）ごとにグループ別の出場者と結果を返す。
    """
    async with db.execute(
        "SELECT DISTINCT round_no FROM heats WHERE tournament_id=? AND group_no>0 ORDER BY round_no",
        (tid,),
    ) as cur:
        round_nos = [r["round_no"] for r in await cur.fetchall()]

    async with db.execute(
        "SELECT * FROM tournaments WHERE id=?", (tid,)
    ) as cur:
        t = await cur.fetchone()

    if not t or not dict(t).get("qual_heat_final"):
        return {"rounds": [], "advance": 0}

    # ヒート決勝へ進出するのは各グループから1名のみ（グループ優勝者）
    group_advance_for_final = 1
    # 決勝進出枠はqual_heat_final_advance
    heat_final_advance = int(dict(t).get("qual_heat_final_advance", 1) or 1)

    result = []
    for rno in round_nos:
        # このラウンドのグループ別上位者を取得
        async with db.execute(
            "SELECT DISTINCT group_no FROM heats WHERE tournament_id=? AND round_no=? ORDER BY group_no",
            (tid, rno),
        ) as cur:
            group_nos = [r["group_no"] for r in await cur.fetchall()]

        groups_data = []
        for gno in group_nos:
            # このラウンドのグループ1位のみ表示（ヒート決勝進出者）
            rno_standings = await _calc_standings_group_round(tid, gno, rno, db)
            top_entries = [s for s in rno_standings if s["rank"] <= group_advance_for_final]
            groups_data.append({
                "group_no": gno,
                "top_entries": top_entries,
            })

        # deciding_rank カラムの自動マイグレーション
        async with db.execute("PRAGMA table_info(heat_finals)") as cur:
            cols = [r[1] for r in await cur.fetchall()]
        if "deciding_rank" not in cols:
            await db.execute("ALTER TABLE heat_finals ADD COLUMN deciding_rank INTEGER")
            await db.commit()

        # ヒート優勝DBから結果取得
        async with db.execute(
            """SELECT hf.slot_no, hf.entry_id, hf.winner_entry_id, hf.rank,
                      hf.deciding_rank, r.name
               FROM heat_finals hf
               LEFT JOIN entries e ON e.id=hf.entry_id
               LEFT JOIN racers r ON r.id=e.racer_id
               WHERE hf.tournament_id=? AND hf.round_no=? AND hf.group_no=0
               ORDER BY hf.slot_no""",
            (tid, rno),
        ) as cur:
            final_slots = [dict(r) for r in await cur.fetchall()]

        # このラウンドの全グループヒート（group_no>0）が完了しているか
        # heats.status='done' ではなく heat_results の存在で判定（save-inline の非同期保存に対応）
        async with db.execute(
            "SELECT COUNT(*) as total FROM heats WHERE tournament_id=? AND round_no=? AND group_no>0", (tid, rno)
        ) as cur:
            total = (await cur.fetchone())["total"]
        async with db.execute(
            """SELECT COUNT(DISTINCT h.id) as done
               FROM heats h
               WHERE h.tournament_id=? AND h.round_no=? AND h.group_no>0
                 AND h.status='done'""", (tid, rno)
        ) as cur:
            done = (await cur.fetchone())["done"]

        result.append({
            "round_no": rno,
            "groups_data": groups_data,
            "final_slots": final_slots,
            "all_done": total > 0 and total == done,
        })

    return {"rounds": result, "advance": heat_final_advance}


async def _calc_standings_group_round(tid: int, group_no: int, round_no: int, db: aiosqlite.Connection) -> list[dict]:
    """特定ラウンドのグループ内成績"""
    async with db.execute(
        """SELECT e.id as entry_id, r.name,
                  COALESCE(SUM(sub.win), 0) as wins,
                  COUNT(CASE WHEN sub.win IS NOT NULL THEN 1 END) as races
           FROM entries e
           JOIN racers r ON r.id=e.racer_id
           LEFT JOIN (
               SELECT hl2.entry_id, hr2.win
               FROM heat_lanes hl2
               JOIN heats h2 ON h2.id=hl2.heat_id
               LEFT JOIN heat_results hr2 ON hr2.heat_lane_id=hl2.id
               WHERE h2.tournament_id=? AND h2.group_no=? AND h2.round_no=?
           ) sub ON sub.entry_id=e.id
           WHERE e.tournament_id=? AND e.status='active'
             AND EXISTS (
               SELECT 1 FROM heat_lanes hl3
               JOIN heats h3 ON h3.id=hl3.heat_id
               WHERE hl3.entry_id=e.id AND h3.tournament_id=? AND h3.group_no=? AND h3.round_no=?
             )
           GROUP BY e.id
           ORDER BY wins DESC""",
        (tid, group_no, round_no, tid, tid, group_no, round_no),
    ) as cur:
        rows = await cur.fetchall()

    standings = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0 and row["wins"] != rows[i-1]["wins"]:
            rank = i + 1
        standings.append({**dict(row), "rank": rank})
    return standings


async def _calc_hoshitori_group_round(tid: int, group_no: int, round_no: int, db: aiosqlite.Connection):
    """特定ヒート・グループの星取表"""
    async with db.execute(
        """SELECT DISTINCT hl.entry_id, r.name
           FROM heat_lanes hl
           JOIN heats h ON h.id=hl.heat_id
           JOIN entries e ON e.id=hl.entry_id
           JOIN racers r ON r.id=e.racer_id
           WHERE h.tournament_id=? AND h.group_no=? AND h.round_no=?
           ORDER BY hl.entry_id""",
        (tid, group_no, round_no),
    ) as cur:
        entries = [dict(r) for r in await cur.fetchall()]

    from collections import defaultdict
    heat_map = defaultdict(list)
    async with db.execute(
        """SELECT h.id as heat_id, hl.entry_id, hr.win
           FROM heats h
           JOIN heat_lanes hl ON hl.heat_id=h.id
           JOIN heat_results hr ON hr.heat_lane_id=hl.id
           WHERE h.tournament_id=? AND h.group_no=? AND h.round_no=? AND hr.win IS NOT NULL
           ORDER BY h.id, hl.lane_no""",
        (tid, group_no, round_no),
    ) as cur:
        for r in await cur.fetchall():
            heat_map[r["heat_id"]].append((r["entry_id"], r["win"]))

    matrix = {}
    for hid, results in heat_map.items():
        if len(results) != 2: continue
        (eid_a, win_a), (eid_b, win_b) = results
        matrix.setdefault((eid_a, eid_b), []).append("win" if win_a==1 else "lose")
        matrix.setdefault((eid_b, eid_a), []).append("win" if win_b==1 else "lose")

    return entries, matrix


async def _calc_standings_overall(tid: int, db: aiosqlite.Connection) -> list[dict]:
    """全ヒート・全グループの合算成績"""
    async with db.execute(
        """SELECT e.id as entry_id, e.advanced, r.name,
                  COALESCE(SUM(sub.win), 0) as wins,
                  COUNT(CASE WHEN sub.win IS NOT NULL THEN 1 END) as races,
                  SUM(CASE WHEN sub.best_time IS NOT NULL THEN sub.best_time END) as time_sum
           FROM entries e
           JOIN racers r ON r.id=e.racer_id
           LEFT JOIN (
               SELECT hl2.entry_id, hr2.win, hr2.best_time
               FROM heat_lanes hl2
               JOIN heats h2 ON h2.id=hl2.heat_id
               LEFT JOIN heat_results hr2 ON hr2.heat_lane_id=hl2.id
               WHERE h2.tournament_id=?
           ) sub ON sub.entry_id=e.id
           WHERE e.tournament_id=? AND e.status='active'
           GROUP BY e.id
           ORDER BY wins DESC, time_sum ASC""",
        (tid, tid),
    ) as cur:
        rows = await cur.fetchall()

    standings = []
    rank = 1
    for i, row in enumerate(rows):
        if i > 0:
            prev = rows[i-1]
            if row["wins"] != prev["wins"]:
                rank = i + 1
        standings.append({**dict(row), "rank": rank})
    return standings


async def _calc_hoshitori_group(tid: int, group_no: int, db: aiosqlite.Connection):
    """ヒート総当たりのグループ内星取表データを生成"""
    # グループ内エントリー取得
    async with db.execute(
        """SELECT DISTINCT hl.entry_id, r.name
           FROM heat_lanes hl
           JOIN heats h ON h.id=hl.heat_id
           JOIN entries e ON e.id=hl.entry_id
           JOIN racers r ON r.id=e.racer_id
           WHERE h.tournament_id=? AND h.group_no=?
           ORDER BY hl.entry_id""",
        (tid, group_no),
    ) as cur:
        entries = [dict(r) for r in await cur.fetchall()]

    # 結果マトリックス
    async with db.execute(
        """SELECT hl.entry_id, hr.win
           FROM heat_lanes hl
           JOIN heats h ON h.id=hl.heat_id
           JOIN heat_results hr ON hr.heat_lane_id=hl.id
           WHERE h.tournament_id=? AND h.group_no=? AND hr.win IS NOT NULL
           ORDER BY h.id, hl.lane_no""",
        (tid, group_no),
    ) as cur:
        all_results = await cur.fetchall()

    from collections import defaultdict
    heat_map = defaultdict(list)
    async with db.execute(
        """SELECT h.id as heat_id, hl.entry_id, hr.win
           FROM heats h
           JOIN heat_lanes hl ON hl.heat_id=h.id
           JOIN heat_results hr ON hr.heat_lane_id=hl.id
           WHERE h.tournament_id=? AND h.group_no=? AND hr.win IS NOT NULL
           ORDER BY h.id, hl.lane_no""",
        (tid, group_no),
    ) as cur:
        for r in await cur.fetchall():
            heat_map[r["heat_id"]].append((r["entry_id"], r["win"]))

    matrix = {}
    for hid, results in heat_map.items():
        if len(results) != 2:
            continue
        (eid_a, win_a), (eid_b, win_b) = results
        key = (eid_a, eid_b)
        if key not in matrix:
            matrix[key] = []
        if (eid_b, eid_a) not in matrix:
            matrix[(eid_b, eid_a)] = []
        matrix[(eid_a, eid_b)].append("win" if win_a == 1 else "lose")
        matrix[(eid_b, eid_a)].append("win" if win_b == 1 else "lose")

    return entries, matrix


async def _calc_hoshitori(tid: int, db: aiosqlite.Connection):
    """
    星取表データを生成する。
    戻り値:
      entries_order: [{"entry_id":..., "name":...}, ...]  （順番固定）
      matrix: {(winner_entry_id, loser_entry_id): "win"|"lose"|None}
              row=対象レーサー、col=対戦相手
    """
    # エントリー順固定
    async with db.execute(
        """SELECT e.id as entry_id, r.name
           FROM entries e JOIN racers r ON r.id=e.racer_id
           WHERE e.tournament_id=? AND e.status='active'
           ORDER BY e.entry_order""",
        (tid,),
    ) as cur:
        entries_order = [dict(r) for r in await cur.fetchall()]

    entry_ids = [e["entry_id"] for e in entries_order]

    # 全レースの結果を取得
    async with db.execute(
        """SELECT hl.entry_id, hr.win
           FROM heats h
           JOIN heat_lanes hl ON hl.heat_id=h.id
           JOIN heat_results hr ON hr.heat_lane_id=hl.id
           WHERE h.tournament_id=? AND hr.win IS NOT NULL""",
        (tid,),
    ) as cur:
        rows = await cur.fetchall()

    # heat_id ごとにグループ化して対戦ペアを復元
    async with db.execute(
        """SELECT h.id as heat_id, hl.entry_id, hr.win
           FROM heats h
           JOIN heat_lanes hl ON hl.heat_id=h.id
           JOIN heat_results hr ON hr.heat_lane_id=hl.id
           WHERE h.tournament_id=? AND hr.win IS NOT NULL
           ORDER BY h.id, hl.lane_no""",
        (tid,),
    ) as cur:
        all_results = await cur.fetchall()

    # heat_id → [(entry_id, win), ...]
    from collections import defaultdict
    heat_map = defaultdict(list)
    for r in all_results:
        heat_map[r["heat_id"]].append((r["entry_id"], r["win"]))

    # matrix[(row_entry_id, col_entry_id)] = "win" or "lose"
    # row=自分、col=対戦相手、self→self は None（斜線）
    matrix = {}
    for hid, results in heat_map.items():
        if len(results) != 2:
            continue
        (eid_a, win_a), (eid_b, win_b) = results
        # eid_a から見た eid_b との結果
        matrix[(eid_a, eid_b)] = "win" if win_a == 1 else "lose"
        # eid_b から見た eid_a との結果
        matrix[(eid_b, eid_a)] = "win" if win_b == 1 else "lose"

    return entries_order, matrix


def _time_eq(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) < 0.001


# ── ヒート（トーナメント）専用 ──────────────────────────────

def _ht_combinations_2_3(n: int) -> list[list[int]]:
    """2と3の組み合わせでnを作るパターン"""
    result = []
    for threes in range(n // 3 + 1):
        remainder = n - threes * 3
        if remainder >= 0 and remainder % 2 == 0:
            twos = remainder // 2
            result.append([3] * threes + [2] * twos)
    return sorted(result, key=lambda x: len(x))


def _ht_seed_assign(entry_ids: list, group_sizes: list, racer_map: dict = None) -> list[list]:
    """エントリーをグループに割り当て（ランダム）
    racer_map: {entry_id: racer_id} 同一racer_idが同グループに入らないよう制御
    """
    import random

    MAX_RETRY = 200
    for attempt in range(MAX_RETRY):
        shuffled = entry_ids[:]
        random.shuffle(shuffled)
        assigned = []
        idx = 0
        for sz in group_sizes:
            assigned.append(shuffled[idx:idx+sz])
            idx += sz

        # racer_mapがある場合、同一racer_idが同グループに入っていないかチェック
        if racer_map:
            ok = True
            for grp in assigned:
                racer_ids_in_grp = [racer_map.get(eid) for eid in grp if racer_map.get(eid)]
                if len(racer_ids_in_grp) != len(set(racer_ids_in_grp)):
                    ok = False
                    break
            if not ok:
                continue  # リトライ
        break  # チェックOK または racer_mapなし

    return assigned


@router.get("/{tid}/qualifying/heat-tournament", response_class=HTMLResponse)
@router.get("/{tid}/qualifying/heat-tournament/{heat_no}", response_class=HTMLResponse)
async def ht_top(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db), heat_no: int = 1):
    """全ヒートのトーナメント管理画面（縦並び）"""
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return RedirectResponse(url="/admin/tournaments/")

    t_dict = dict(t)
    is_hr_final = (t_dict.get("qualifying_type") == "heat_roundrobin" and
                   bool(t_dict.get("qual_heat_final")))

    if is_hr_final:
        # heat_roundrobin ヒート決勝: このヒート(heat_no)の各グループ上位 group_advance 名を参加者とする
        group_advance = int(t_dict.get("qual_group_advance") or 1)
        group_count = int(t_dict.get("qual_group_count") or 1)
        finalist_entry_ids = []
        seen = set()
        for gno in range(1, group_count + 1):
            grp_standings = await _calc_standings_group_round(tid, gno, heat_no, db)
            count = 0
            for s in grp_standings:
                if count >= group_advance:
                    break
                if s["entry_id"] not in seen:
                    finalist_entry_ids.append(s["entry_id"])
                    seen.add(s["entry_id"])
                    count += 1
        if finalist_entry_ids:
            placeholders = ",".join("?" * len(finalist_entry_ids))
            async with db.execute(
                f"""SELECT e.id as entry_id, r.name, e.racer_id
                   FROM entries e JOIN racers r ON r.id=e.racer_id
                   WHERE e.id IN ({placeholders}) ORDER BY r.yomi, r.name""",
                finalist_entry_ids,
            ) as cur:
                entries = [dict(r) for r in await cur.fetchall()]
        else:
            entries = []
    else:
        async with db.execute(
            """SELECT e.id as entry_id, r.name, e.racer_id
               FROM entries e JOIN racers r ON r.id=e.racer_id
               WHERE e.tournament_id=? AND e.status='active' ORDER BY r.yomi, r.name""",
            (tid,),
        ) as cur:
            entries = [dict(r) for r in await cur.fetchall()]
    heat_count = int(t_dict.get("qual_heat_count") or 1)
    heat_advance = int(t_dict.get("qual_heat_advance") or 1)
    group_count = int(t_dict.get("qual_group_count") or 1)
    group_advance = int(t_dict.get("qual_group_advance") or heat_advance)
    heat_exclude = bool(t_dict.get("qual_heat_exclude", 0))
    n = len(entries)
    all_entry_ids = [e["entry_id"] for e in entries]

    # heat_roundrobin ヒート決勝: 参加者数に応じた自動処理
    if is_hr_final:
        heat_final_advance = int(t_dict.get("qual_heat_final_advance") or 1)
        # このヒート(heat_no)のht_roundsを確認
        async with db.execute(
            "SELECT COUNT(*) FROM ht_rounds WHERE tournament_id=? AND heat_no=?", (tid, heat_no)
        ) as cur:
            existing_rounds = (await cur.fetchone())[0]

        if not existing_rounds:
            if n == 0:
                # 参加者なし
                pass
            elif n == 1:
                # 1人: そのまま決定（ht_roundsに決勝ラウンドを自動生成してrank=1をセット）
                import random as _r
                await db.execute(
                    "INSERT INTO ht_rounds (tournament_id, heat_no, round_no, round_type, section_no) VALUES (?,?,?,?,?)",
                    (tid, heat_no, 1, "final", 1)
                )
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    rid = (await cur.fetchone())["id"]
                await db.execute("INSERT INTO ht_groups (round_id, group_no) VALUES (?,?)", (rid, 1))
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    gid = (await cur.fetchone())["id"]
                eid = entries[0]["entry_id"]
                await db.execute("INSERT INTO ht_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (gid, 1, eid))
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    sid = (await cur.fetchone())["id"]
                await db.execute("INSERT INTO ht_slot_ranks (group_id, slot_id, rank) VALUES (?,?,?)", (gid, sid, 1))
                await db.commit()
                return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying/heat-tournament/{heat_no}", status_code=303)
            elif n <= 3:
                # 2〜3人: 直接決勝ラウンドを自動生成（パターン選択不要）
                import random as _r
                shuffled = [e["entry_id"] for e in entries]
                _r.shuffle(shuffled)
                await db.execute(
                    "INSERT INTO ht_rounds (tournament_id, heat_no, round_no, round_type, section_no) VALUES (?,?,?,?,?)",
                    (tid, heat_no, 1, "final", 1)
                )
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    rid = (await cur.fetchone())["id"]
                await db.execute("INSERT INTO ht_groups (round_id, group_no) VALUES (?,?)", (rid, 1))
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    gid = (await cur.fetchone())["id"]
                for sno, eid in enumerate(shuffled, 1):
                    await db.execute("INSERT INTO ht_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (gid, sno, eid))
                await db.commit()
                return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying/heat-tournament/{heat_no}", status_code=303)
            # 4人以上: 通常のパターン選択UIへ（以下の処理でパターンを表示）

        # is_hr_final のとき heat_count=1（このヒートのみ管理）
        heat_count = 1

    # 全ヒート分のデータを収集
    all_heats = []
    excluded_entry_ids: set = set()  # 進出済みで除外されたエントリーID
    _heat_locks_map = await _get_heat_locks(tid, db)  # ヒート単位ロック状態

    heat_nos_to_process = [heat_no] if is_hr_final else range(1, heat_count + 1)
    for hno in heat_nos_to_process:
        async with db.execute(
            "SELECT * FROM ht_rounds WHERE tournament_id=? AND heat_no=? ORDER BY COALESCE(section_no,1), round_no",
            (tid, hno),
        ) as cur:
            rounds = [dict(r) for r in await cur.fetchall()]

        # セクション別にデータを収集
        section_nos = sorted(set(r["section_no"] for r in rounds)) if rounds else []
        sections_data = []
        groups_data_all = []
        for sec_no in section_nos:
            sec_rounds = [r for r in rounds if r["section_no"] == sec_no]
            sec_groups_data = []
            for rnd in sec_rounds:
                async with db.execute(
                    "SELECT * FROM ht_groups WHERE round_id=? ORDER BY group_no", (rnd["id"],)
                ) as cur:
                    groups = [dict(r) for r in await cur.fetchall()]
                for g in groups:
                    async with db.execute(
                        """SELECT hs.id as slot_id, hs.slot_no, hs.entry_id, r.name
                           FROM ht_slots hs LEFT JOIN entries e ON e.id=hs.entry_id
                           LEFT JOIN racers r ON r.id=e.racer_id
                           WHERE hs.group_id=? ORDER BY hs.slot_no""",
                        (g["id"],),
                    ) as cur:
                        slots = [dict(r) for r in await cur.fetchall()]
                    async with db.execute(
                        "SELECT * FROM ht_results WHERE group_id=?", (g["id"],)
                    ) as cur:
                        result = await cur.fetchone()
                    async with db.execute(
                        "SELECT slot_id, rank FROM ht_slot_ranks WHERE group_id=? ORDER BY rank",
                        (g["id"],),
                    ) as cur:
                        ranks = {r["slot_id"]: r["rank"] for r in await cur.fetchall()}
                    entry = {
                        "round": rnd,
                        "group": g,
                        "slots": slots,
                        "result": dict(result) if result else None,
                        "ranks": ranks,
                    }
                    sec_groups_data.append(entry)
                    groups_data_all.append(entry)
            sections_data.append({
                "section_no": sec_no,
                "label": chr(64 + sec_no) if sec_no > 0 else "F",  # A,B,... or F(inal) for heat-final
                "rounds": sec_rounds,
                "groups_data": sec_groups_data,
            })

        adv_per = group_advance  # qual_group_advance が正（group_count=1でも同じ）
        advanced = await _ht_get_advanced(tid, hno, adv_per, db)
        for a in advanced:
            a["heat_no"] = hno
            a["rank"] = a.get("overall_rank", "")

        if heat_exclude and hno > 1:
            heat_entry_ids = [e for e in all_entry_ids if e not in excluded_entry_ids]
        else:
            heat_entry_ids = all_entry_ids
        heat_n = len(heat_entry_ids)

        if not rounds:
            if group_count > 1:
                # グループごとの人数と選択可能パターンを計算
                base, rem = divmod(heat_n, group_count)
                section_patterns = []
                for s in range(group_count):
                    sz = base + (1 if s < rem else 0)
                    pats = _ht_combinations_2_3(sz)
                    section_patterns.append({
                        "section_no": s + 1,
                        "label": chr(65 + s),
                        "size": sz,
                        "patterns": pats,
                    })
                patterns_for_heat = section_patterns
            else:
                patterns_for_heat = _ht_combinations_2_3(heat_n)
        else:
            patterns_for_heat = []

        prev_heat_done = True

        # ── ヒート決勝（section_no=0）の状態フラグ ──
        hf_enabled = bool(t_dict.get("qual_heat_final", 0))
        # 全グループの順位確定（通過人数分）→ 生成可能か
        hf_can_generate = False
        if hf_enabled:
            _ok, _msg = await _ht_all_groups_ranked(tid, hno, group_advance, db)
            hf_can_generate = _ok
        # ヒート決勝として扱う section_no（複数グループ=0 / 単一グループ=唯一の非0セクション）
        hf_sec = await _ht_heat_final_section_no(tid, hno, db)
        # ヒート決勝が生成済みか（対象 section が存在するか）
        hf_generated = any(
            (sd.get("section_no") == hf_sec) for sd in sections_data
        ) if sections_data else False
        # ヒート決勝が完了済みか（対象 section の final で順位確定）
        hf_done = False
        if hf_generated:
            async with db.execute(
                """SELECT COUNT(*) FROM ht_slot_ranks sr
                   JOIN ht_groups hg ON hg.id=sr.group_id
                   JOIN ht_rounds hr ON hr.id=hg.round_id
                   WHERE hr.tournament_id=? AND hr.heat_no=? AND COALESCE(hr.section_no,1)=?
                     AND hr.round_type='final'""",
                (tid, hno, hf_sec),
            ) as cur:
                hf_done = (await cur.fetchone())[0] > 0

        # ── グループ通過者（ヒート決勝の手前に表示）とヒート決勝通過者（上部に表示）──
        # heat_tournament ではヒート決勝の決勝進出人数は qual_heat_advance を使う
        # （calc_finalists・編集UIと同じカラム。qual_heat_final_advance は総当たり用）。
        heat_final_advance = int(t_dict.get("qual_heat_advance") or 1)
        group_advancers = await _ht_get_group_advancers(tid, hno, group_advance, db)
        hf_advancers = await _ht_get_heatfinal_advancers(tid, hno, heat_final_advance, db)

        # 前ヒート完了判定（heat_exclude時、次ヒートを生成してよいか）
        #   ヒート決勝あり → 前ヒートのヒート決勝が完了していること
        #   ヒート決勝なし → 前ヒートのグループ通過者が出そろっていること
        if heat_exclude and hno > 1:
            prev_heat = next((h for h in all_heats if h["heat_no"] == hno - 1), None)
            if hf_enabled:
                prev_heat_done = bool(prev_heat and prev_heat.get("hf_done"))
            else:
                prev_adv = prev_heat.get("advanced", []) if prev_heat else []
                prev_heat_done = len(prev_adv) >= max(1, group_advance)

        all_heats.append({
            "heat_no": hno,
            "rounds": rounds,
            "groups_data": groups_data_all,
            "sections_data": sections_data,
            "patterns": patterns_for_heat if prev_heat_done else [],
            "advanced": advanced,
            "heat_n": heat_n,
            "prev_heat_done": prev_heat_done,
            "hf_enabled": hf_enabled,
            "hf_can_generate": hf_can_generate,
            "hf_generated": hf_generated,
            "hf_done": hf_done,
            "group_advancers": group_advancers,
            "hf_advancers": hf_advancers,
            "locked": bool(_heat_locks_map.get(str(hno))),
        })

        if heat_exclude:
            if hf_enabled:
                # ヒート決勝あり：本戦進出者（ヒート決勝 上位 qual_heat_final_advance 名）のみ除外
                for a in hf_advancers:
                    if a.get("is_advance") and a.get("entry_id"):
                        excluded_entry_ids.add(a["entry_id"])
            else:
                # ヒート決勝なし：グループ通過者が直接決勝進出
                for a in advanced:
                    if a.get("entry_id"):
                        excluded_entry_ids.add(a["entry_id"])

    # 予選順位（ポイント制）を取得
    from app.routers.bracket import _get_all_standings
    all_standings_raw = await _get_all_standings(tid, db)
    all_standings = []
    rank = 1
    prev_pts = None
    for i, s in enumerate(all_standings_raw):
        pts = s.get("total_points", -1)
        if pts != prev_pts:
            rank = i + 1
        all_standings.append({**s, "rank": rank})
        prev_pts = pts

    return templates.TemplateResponse("admin/heat_tournament.html", {
        "request": request,
        "t": t,
        "heat_count": heat_count,
        "heat_advance": heat_advance,
        "group_count": group_count,
        "group_advance": group_advance,
        "entries": entries,
        "all_heats": all_heats,
        "all_standings": all_standings,
    })


async def _ht_get_group_advancers(tid: int, heat_no: int, group_advance: int, db) -> dict:
    """各グループ(section>0)の上位 group_advance 名を取得する。
    戻り値: { section_no: [ {rank, name, entry_id}, ... ], ... }（rank昇順）
    final で 1..(decided), 3位決定戦(third) があれば 3位以降を補完。"""
    from collections import defaultdict
    async with db.execute(
        """SELECT hsr.rank, hr.round_type, COALESCE(hr.section_no,1) as section_no,
                  hs.entry_id, r.name
           FROM ht_slot_ranks hsr
           JOIN ht_slots hs ON hs.id=hsr.slot_id
           JOIN ht_groups hg ON hg.id=hsr.group_id
           JOIN ht_rounds hr ON hr.id=hg.round_id
           LEFT JOIN entries e ON e.id=hs.entry_id
           LEFT JOIN racers r ON r.id=e.racer_id
           WHERE hr.tournament_id=? AND hr.heat_no=? AND COALESCE(hr.section_no,1) > 0
             AND hr.round_type IN ('final','third')
           ORDER BY COALESCE(hr.section_no,1), hr.round_type, hsr.rank""",
        (tid, heat_no),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    sec_map = defaultdict(dict)  # section -> {overall_rank: row}
    for r in rows:
        sec = r["section_no"]
        if r["round_type"] == "final":
            orank = r["rank"]
        else:  # third: 1位→3位, 2位→4位
            orank = 2 + r["rank"]
        if r["entry_id"] and orank not in sec_map[sec]:
            sec_map[sec][orank] = {"rank": orank, "name": r["name"], "entry_id": r["entry_id"]}
    result = {}
    for sec in sorted(sec_map.keys()):
        ranked = [sec_map[sec][k] for k in sorted(sec_map[sec].keys()) if k <= group_advance]
        result[sec] = ranked
    return result


async def _ht_heat_final_section_no(tid: int, heat_no: int, db) -> int:
    """ヒート決勝として扱う section_no を返す。
    複数グループ（qual_group_count>1）ではヒート決勝は section_no=0。
    単一グループ（=1）では section_0 が作られず、その唯一のセクション(通常1)の
    final がヒート最終順位になるため、存在する最小の非0セクションを返す。"""
    async with db.execute(
        "SELECT qual_group_count FROM tournaments WHERE id=?", (tid,)
    ) as cur:
        row = await cur.fetchone()
    gc = int((dict(row).get("qual_group_count") if row else 1) or 1)
    if gc > 1:
        return 0
    async with db.execute(
        "SELECT DISTINCT COALESCE(section_no,1) AS sn FROM ht_rounds WHERE tournament_id=? AND heat_no=?",
        (tid, heat_no),
    ) as cur:
        secs = sorted({r["sn"] for r in await cur.fetchall()})
    nz = [s for s in secs if s and s > 0]
    return nz[0] if nz else 0


async def _ht_get_heatfinal_advancers(tid: int, heat_no: int, heat_final_advance: int, db) -> list:
    """ヒート決勝の最終順位を返す。
    複数グループは section_no=0、単一グループはその唯一セクションの final を対象にする。
    決勝(final)グループに並んだ人数分の順位（1〜n位）をすべて返し、3位決定戦(third)が
    あれば3位以降を補完する。各要素に is_advance（上位 heat_final_advance 名＝本戦進出か）を付与。
    戻り値: [ {rank, name, entry_id, is_advance}, ... ]（rank昇順）。未確定なら空。"""
    hf_sec = await _ht_heat_final_section_no(tid, heat_no, db)
    by_rank = {}
    async with db.execute(
        """SELECT hsr.rank, hr.round_type, hs.entry_id, r.name, COALESCE(r.yomi,'') as yomi
           FROM ht_slot_ranks hsr
           JOIN ht_slots hs ON hs.id=hsr.slot_id
           JOIN ht_groups hg ON hg.id=hsr.group_id
           JOIN ht_rounds hr ON hr.id=hg.round_id
           LEFT JOIN entries e ON e.id=hs.entry_id
           LEFT JOIN racers r ON r.id=e.racer_id
           WHERE hr.tournament_id=? AND hr.heat_no=? AND COALESCE(hr.section_no,1)=?
             AND hr.round_type IN ('final','third')
           ORDER BY hr.round_type, hsr.rank""",
        (tid, heat_no, hf_sec),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        if r["round_type"] == "final":
            orank = r["rank"]
        else:  # 3位決定戦: 1位→3位, 2位→4位
            orank = 2 + r["rank"]
        if r["entry_id"] and orank not in by_rank:
            by_rank[orank] = {"rank": orank, "name": r["name"], "entry_id": r["entry_id"], "yomi": r.get("yomi", "")}
    result = []
    for k in sorted(by_rank.keys()):
        item = by_rank[k]
        item["is_advance"] = (k <= max(1, heat_final_advance))
        result.append(item)
    return result


async def _ht_get_advanced(tid: int, heat_no: int, heat_advance: int, db) -> list[dict]:
    """このヒートの決勝進出者を返す。セクション（グループ）ごとに heat_advance 名ずつ収集。"""
    async with db.execute(
        """SELECT hsr.rank, hr.round_type, COALESCE(hr.section_no, 1) as section_no,
                  hs.entry_id, r.name, COALESCE(r.yomi,'') as yomi, e.racer_id
           FROM ht_slot_ranks hsr
           JOIN ht_slots hs ON hs.id=hsr.slot_id
           JOIN ht_groups hg ON hg.id=hsr.group_id
           JOIN ht_rounds hr ON hr.id=hg.round_id
           LEFT JOIN entries e ON e.id=hs.entry_id
           LEFT JOIN racers r ON r.id=e.racer_id
           WHERE hr.tournament_id=? AND hr.heat_no=?
             AND hr.round_type IN ('final','third')
           ORDER BY COALESCE(hr.section_no,1), hr.round_type, hsr.rank""",
        (tid, heat_no),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    from collections import defaultdict
    sec_finals = defaultdict(list)
    sec_thirds = defaultdict(list)
    for r in rows:
        sec = r["section_no"]
        if r["round_type"] == "final":
            sec_finals[sec].append(r)
        else:
            sec_thirds[sec].append(r)

    section_nos = sorted(set(r["section_no"] for r in rows)) if rows else [1]
    result = []
    for sec in section_nos:
        finals = sorted(sec_finals[sec], key=lambda x: x["rank"])
        thirds = sorted(sec_thirds[sec], key=lambda x: x["rank"])
        for r in finals:
            if r["rank"] <= heat_advance:
                r["overall_rank"] = r["rank"]
                r["section_no"] = sec
                result.append(r)
        if heat_advance >= 3:
            for r in thirds:
                if r["rank"] == 1:
                    r["overall_rank"] = 3
                    r["section_no"] = sec
                    result.append(r)

    return result


@router.get("/{tid}/qualifying/heat-roundrobin/{heat_no}/bracket/html", response_class=HTMLResponse)
async def hr_bracket_html(tid: int, heat_no: int, db: aiosqlite.Connection = Depends(get_db)):
    """ヒート制総当たりの優勝トーナメントHTML"""
    from app.routers.bracket import _render_html_bracket

    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or not dict(t).get("qual_heat_final"):
        return HTMLResponse('<div style="padding:16px;color:#555;font-size:12px;text-align:center">トーナメント未設定</div>')

    group_advance = dict(t).get("qual_group_advance", 1) or 1

    # このヒート(round_no)の final_slots を取得
    async with db.execute(
        """SELECT hf.slot_no, hf.entry_id, hf.winner_entry_id, hf.rank, r.name
           FROM heat_finals hf
           LEFT JOIN entries e ON e.id=hf.entry_id
           LEFT JOIN racers r ON r.id=e.racer_id
           WHERE hf.tournament_id=? AND hf.round_no=? AND hf.group_no=0
           ORDER BY hf.slot_no""",
        (tid, heat_no),
    ) as cur:
        final_slots = [dict(r) for r in await cur.fetchall()]

    if not final_slots:
        return HTMLResponse('<div style="padding:16px;color:#555;font-size:12px;text-align:center">トーナメント未開始</div>')

    # svg_data 形式に変換（1グループの1ラウンドとして）
    slots = [{"slot_id": None, "slot_no": fs["slot_no"], "entry_id": fs["entry_id"],
               "name": fs["name"] or "", "rank": fs["rank"], "is_seed_slot": False}
             for fs in final_slots]
    winner_sid = None
    winner_eid = next((fs["winner_entry_id"] for fs in final_slots if fs["winner_entry_id"]), None)
    result = {"winner_slot_id": None, "winner_entry_id": winner_eid} if winner_eid else None

    svg_data = {"rounds": [{"label": "優勝トーナメント", "round_type": "final",
                             "groups": [{"group_id": None, "slots": slots, "result": result}]}],
                "third_rounds": []}
    html = _render_html_bracket(svg_data, tid=tid, winner_js_func="", winner_js_extra_args="")
    return HTMLResponse(html)


@router.get("/{tid}/qualifying/heat-tournament/{heat_no}/bracket/html", response_class=HTMLResponse)
async def ht_bracket_html(tid: int, heat_no: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ヒートトーナメントのビジュアルブラケットHTML"""
    section_no = int(request.query_params.get("section_no", 0))  # 0=全体(旧動作), 1以上=セクション指定
    from app.routers.bracket import _render_html_bracket, combinations_2_3

    async with db.execute(
        "SELECT * FROM ht_rounds WHERE tournament_id=? AND heat_no=? ORDER BY round_no",
        (tid, heat_no),
    ) as cur:
        ht_rounds = [dict(r) for r in await cur.fetchall()]

    # section_no指定があればそのセクションのみ（0=ヒート決勝も正しくフィルタ）
    # ただし section_no=0（全体/単一グループ表示）で section0 のラウンドが無い場合は、
    # 存在する単一セクション（通常 section_no=1）にフォールバックして表示する。
    if section_no == 0:
        sec0 = [r for r in ht_rounds if r.get("section_no", 0) == 0]
        if sec0:
            ht_rounds = sec0
        else:
            nonzero_secs = sorted({r.get("section_no", 0) for r in ht_rounds if r.get("section_no", 0) != 0})
            target_sec = nonzero_secs[0] if nonzero_secs else 0
            ht_rounds = [r for r in ht_rounds if r.get("section_no", 0) == target_sec]
    else:
        ht_rounds = [r for r in ht_rounds if r.get("section_no", 0) == section_no]

    if not ht_rounds:
        return HTMLResponse('<div style="padding:20px;color:#95a5a6;text-align:center">組み合わせ未生成</div>')

    # svg_data形式に変換（third=3位決定戦はメインブラケットから除外）
    ordered_rounds = []
    for rnd in ht_rounds:
        if rnd.get("round_type") == "third":
            continue
        async with db.execute(
            "SELECT * FROM ht_groups WHERE round_id=? ORDER BY group_no", (rnd["id"],)
        ) as cur:
            groups = [dict(g) for g in await cur.fetchall()]
        grp_list = []
        for g in groups:
            async with db.execute(
                """SELECT hs.id as slot_id, hs.slot_no, hs.entry_id,
                          COALESCE(r.name,'') as name,
                          hsr.rank
                   FROM ht_slots hs
                   LEFT JOIN entries e ON e.id=hs.entry_id
                   LEFT JOIN racers r ON r.id=e.racer_id
                   LEFT JOIN ht_slot_ranks hsr ON hsr.slot_id=hs.id AND hsr.group_id=?
                   WHERE hs.group_id=? ORDER BY hs.slot_no""",
                (g["id"], g["id"]),
            ) as cur:
                slots = [dict(s) for s in await cur.fetchall()]

            async with db.execute(
                "SELECT winner_slot_id FROM ht_results WHERE group_id=?", (g["id"],)
            ) as cur:
                res = await cur.fetchone()

            grp_list.append({
                "slots": [
                    {"slot_id": s["slot_id"], "slot_no": s["slot_no"],
                     "name": s["name"] or None, "entry_id": s["entry_id"],
                     "rank": s["rank"], "qual_rank": None, "is_seed_slot": False}
                    for s in slots
                ],
                "result": {"winner_slot_id": res["winner_slot_id"]} if res else None,
                "group_id": g["id"],
            })
        ordered_rounds.append({
            "round_no": rnd["round_no"],
            "round_type": rnd["round_type"],
            "label": "",  # 後で付与
            "groups": grp_list,
        })

    # ラベル付与
    sorted_rnos = sorted([r["round_no"] for r in ordered_rounds], reverse=True)
    for i, rno in enumerate(sorted_rnos):
        r = next(x for x in ordered_rounds if x["round_no"] == rno)
        rt = r.get("round_type", "normal")
        if rt == "final":
            r["label"] = "決勝"
        elif rt == "third":
            r["label"] = "3位決定戦"
        elif i == 1:
            r["label"] = "準決勝"
        elif i == 2:
            r["label"] = "準々決勝"
        else:
            r["label"] = f"ラウンド{rno}"

    # 決勝まで全ラウンドを表示（未来ラウンドは空枠）
    # ラベルをround_noの大きい順に付与
    sorted_rnos = sorted([r["round_no"] for r in ordered_rounds], reverse=True)
    for i, rno in enumerate(sorted_rnos):
        r = next(x for x in ordered_rounds if x["round_no"] == rno)
        rt = r.get("round_type", "normal")
        if rt == "final":
            r["label"] = "決勝"
        elif rt == "third":
            r["label"] = "3位決定戦"
        elif i == 1:
            r["label"] = "準決勝"
        elif i == 2:
            r["label"] = "準々決勝"
        else:
            r["label"] = f"ラウンド{rno}"

    # DBにないラウンドを空枠で補完（決勝まで）
    has_final = any(r.get("round_type") == "final" for r in ordered_rounds)
    if not has_final and ordered_rounds:
        existing_rnos = {r["round_no"] for r in ordered_rounds}
        last_rno = max(r["round_no"] for r in ordered_rounds)
        last_groups_count = len([r for r in ordered_rounds if r["round_no"] == last_rno][0]["groups"])
        n = last_groups_count
        rno = last_rno + 1
        while n > 1:
            pats = combinations_2_3(n)
            if not pats: break
            pat = pats[0]
            is_final = len(pat) == 1
            future_groups = []
            for sz in pat:
                slots = [{"slot_id": None, "slot_no": s, "name": None, "entry_id": None,
                          "rank": None, "qual_rank": None, "is_seed_slot": False}
                         for s in range(1, sz+1)]
                future_groups.append({"slots": slots, "result": None, "group_id": None})
            # ラベル決定
            if is_final:
                lbl = "決勝"
                rtype = "final"
            else:
                # 既存+追加後の総ラウンド数から判断
                future_count = len(ordered_rounds) + 1
                if future_count == 2: lbl, rtype = "準決勝", "normal"
                elif future_count == 3: lbl, rtype = "準々決勝", "normal"
                else: lbl, rtype = f"ラウンド{rno}", "normal"
            ordered_rounds.append({
                "round_no": rno,
                "round_type": rtype,
                "label": lbl,
                "groups": future_groups,
            })
            n = len(pat)
            rno += 1
            if is_final: break

    # 最終的にラベルを再付与（全ラウンド揃った状態で）
    # normalラウンド（決勝含む）のみでカウントして決勝からの距離でラベルを決定
    normal_only = [r for r in ordered_rounds if r.get("round_type", "normal") not in ("third", "revival")]
    total_normal_q = len(normal_only)
    sorted_rnos2 = sorted([r["round_no"] for r in ordered_rounds], reverse=True)
    for rno2 in sorted_rnos2:
        r2 = next(x for x in ordered_rounds if x["round_no"] == rno2)
        rt2 = r2.get("round_type", "normal")
        if rt2 == "final":
            r2["label"] = "決勝"
        elif rt2 == "third":
            r2["label"] = "3位決定戦"
        elif rt2 == "revival":
            r2["label"] = "敗者復活戦"
        else:
            diff2 = total_normal_q - rno2
            if diff2 == 1:
                r2["label"] = "準決勝"
            elif diff2 == 2:
                r2["label"] = "準々決勝"
            elif diff2 >= 3:
                r2["label"] = f"ラウンド{rno2}"
            else:
                r2["label"] = "決勝"

    # 3位決定戦データを取得（DBにある場合）
    third_rounds_data = []
    async with db.execute(
        """SELECT * FROM ht_rounds WHERE tournament_id=? AND heat_no=? AND round_type='third'
           ORDER BY round_no""",
        (tid, heat_no),
    ) as cur:
        ht_third_rounds = [dict(r) for r in await cur.fetchall()]

    for trd in ht_third_rounds:
        async with db.execute(
            "SELECT * FROM ht_groups WHERE round_id=? ORDER BY group_no", (trd["id"],)
        ) as cur:
            tgroups = [dict(g) for g in await cur.fetchall()]
        tgrp_list = []
        for tg in tgroups:
            async with db.execute(
                """SELECT hs.id as slot_id, hs.slot_no, hs.entry_id,
                          COALESCE(r.name,'') as name, hsr.rank
                   FROM ht_slots hs
                   LEFT JOIN entries e ON e.id=hs.entry_id
                   LEFT JOIN racers r ON r.id=e.racer_id
                   LEFT JOIN ht_slot_ranks hsr ON hsr.slot_id=hs.id AND hsr.group_id=?
                   WHERE hs.group_id=? ORDER BY hs.slot_no""",
                (tg["id"], tg["id"]),
            ) as cur:
                tslots = [dict(s) for s in await cur.fetchall()]
            async with db.execute(
                "SELECT winner_slot_id FROM ht_results WHERE group_id=?", (tg["id"],)
            ) as cur:
                tres = await cur.fetchone()
            tgrp_list.append({
                "slots": [{"slot_id": s["slot_id"], "slot_no": s["slot_no"],
                           "name": s["name"] or None, "entry_id": s["entry_id"],
                           "rank": s["rank"], "qual_rank": None, "is_seed_slot": False}
                          for s in tslots],
                "result": {"winner_slot_id": tres["winner_slot_id"]} if tres else None,
                "group_id": tg["id"],
            })
        third_rounds_data.append({
            "round_no": trd["round_no"],
            "round_type": "third",
            "label": "3位決定戦",
            "groups": tgrp_list,
        })

    # 3位決定戦がDBになくて、準決勝完了かつ決勝が2枠なら空枠で補完
    has_final_rnd = any(r.get("round_type") == "final" for r in ordered_rounds)
    if not third_rounds_data and has_final_rnd:
        final_rnd = next((r for r in ordered_rounds if r.get("round_type") == "final"), None)
        if final_rnd and len(final_rnd["groups"]) == 1 and len(final_rnd["groups"][0]["slots"]) == 2:
            # 準決勝の直前のラウンドが2グループなら3位決定戦を追加
            semifinal = next((r for r in ordered_rounds if r.get("label") == "準決勝"), None)
            if semifinal and len(semifinal["groups"]) == 2:
                third_slots = [
                    {"slot_id": None, "slot_no": s, "name": None, "entry_id": None,
                     "rank": None, "qual_rank": None, "is_seed_slot": False}
                    for s in range(1, 3)
                ]
                third_rounds_data.append({
                    "round_no": semifinal["round_no"],
                    "round_type": "third",
                    "label": "3位決定戦",
                    "groups": [{"slots": third_slots, "result": None, "group_id": None}],
                })

    svg_data = {"rounds": ordered_rounds, "third_rounds": []}  # 3位決定戦はグループカードで表示
    html = _render_html_bracket(
        svg_data, tid=tid,
        winner_js_func="setHtWinner",
        winner_js_extra_args=str(heat_no),
    )
    return HTMLResponse(html)


@router.post("/{tid}/qualifying/heat-tournament/{heat_no}/reset")
async def ht_reset(tid: int, heat_no: int, db: aiosqlite.Connection = Depends(get_db)):
    """ヒートNのトーナメントをリセット"""
    if await _is_heat_locked(tid, heat_no, db):
        return _locked_json_response()
    async with db.execute(
        "SELECT id FROM ht_rounds WHERE tournament_id=? AND heat_no=?", (tid, heat_no)
    ) as cur:
        old_rounds = [r["id"] for r in await cur.fetchall()]
    for rid in old_rounds:
        async with db.execute("SELECT id FROM ht_groups WHERE round_id=?", (rid,)) as cur:
            old_groups = [r["id"] for r in await cur.fetchall()]
        for gid in old_groups:
            await db.execute("DELETE FROM ht_slot_ranks WHERE group_id=?", (gid,))
            await db.execute("DELETE FROM ht_results WHERE group_id=?", (gid,))
            await db.execute("DELETE FROM ht_slots WHERE group_id=?", (gid,))
        await db.execute("DELETE FROM ht_groups WHERE round_id=?", (rid,))
    await db.execute("DELETE FROM ht_rounds WHERE tournament_id=? AND heat_no=?", (tid, heat_no))
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/{tid}/qualifying/heat-tournament/{heat_no}/unlock")
async def ht_unlock(tid: int, heat_no: int, db: aiosqlite.Connection = Depends(get_db)):
    """「結果を取消」：ロックされたヒートのロックを解除し、再編集を可能にする。
    結果データ（ht_slot_ranks 等）は保持し、ロックフラグのみ解除する。
    解除後は通常編集（再生成・リセットを含む）が可能になる。"""
    if not await _is_heat_locked(tid, heat_no, db):
        return JSONResponse({"ok": True, "locked": False})
    await _set_heat_lock(tid, heat_no, False, db)
    return JSONResponse({"ok": True, "locked": False})


@router.post("/{tid}/qualifying/heat-tournament/{heat_no}/heat-final/generate")
async def heat_final_tournament_generate(
    tid: int, heat_no: int, request: Request, db: aiosqlite.Connection = Depends(get_db)
):
    """全グループの順位確定後、ヒート決勝トーナメント（section_no=0）を生成・流入する。
    ボタン押下で呼ばれる。既に存在する場合は作り直す（リセット→再生成）。"""
    if await _is_heat_locked(tid, heat_no, db):
        return _locked_json_response()
    async with db.execute(
        "SELECT qual_heat_final, qual_group_count, qual_group_advance, qual_heat_advance FROM tournaments WHERE id=?",
        (tid,),
    ) as cur:
        t = await cur.fetchone()
    if not t:
        return JSONResponse({"ok": False, "error": "レースが見つかりません。"}, status_code=404)
    if not bool(t["qual_heat_final"]):
        return JSONResponse({"ok": False, "error": "このレースはヒート決勝を行わない設定です。"}, status_code=400)

    group_count = int(t["qual_group_count"] or 1)
    group_advance = int(t["qual_group_advance"] or 1)
    heat_final_advance = int(t["qual_heat_advance"] or 1)

    # 全グループの通過人数分の順位が確定しているか
    ok, msg = await _ht_all_groups_ranked(tid, heat_no, group_advance, db)
    if not ok:
        return JSONResponse({"ok": False, "error": "全グループの対戦結果が出ていません。" + msg}, status_code=400)

    # 既存の section_no=0 を削除（作り直し）
    async with db.execute(
        "SELECT id FROM ht_rounds WHERE tournament_id=? AND heat_no=? AND section_no=0",
        (tid, heat_no),
    ) as cur:
        old_rids = [r["id"] for r in await cur.fetchall()]
    for rid in old_rids:
        async with db.execute("SELECT id FROM ht_groups WHERE round_id=?", (rid,)) as cur:
            gids = [r["id"] for r in await cur.fetchall()]
        for gid in gids:
            await db.execute("DELETE FROM ht_slot_ranks WHERE group_id=?", (gid,))
            await db.execute("DELETE FROM ht_results WHERE group_id=?", (gid,))
            await db.execute("DELETE FROM ht_slots WHERE group_id=?", (gid,))
        await db.execute("DELETE FROM ht_groups WHERE round_id=?", (rid,))
    await db.execute(
        "DELETE FROM ht_rounds WHERE tournament_id=? AND heat_no=? AND section_no=0",
        (tid, heat_no),
    )
    await db.commit()

    # 生成 → 流入
    await _ht_build_heat_final_rounds(tid, heat_no, group_count, group_advance, heat_final_advance, db)
    await _ht_fill_heat_final(tid, heat_no, db)
    await _ht_update_advanced(tid, db)
    return JSONResponse({"ok": True})


@router.post("/{tid}/qualifying/heat-tournament/{heat_no}/heat-final/reset")
async def heat_final_tournament_reset(
    tid: int, heat_no: int, db: aiosqlite.Connection = Depends(get_db)
):
    """ヒート決勝トーナメント（section_no=0）を削除する。"""
    if await _is_heat_locked(tid, heat_no, db):
        return _locked_json_response()
    async with db.execute(
        "SELECT id FROM ht_rounds WHERE tournament_id=? AND heat_no=? AND section_no=0",
        (tid, heat_no),
    ) as cur:
        old_rids = [r["id"] for r in await cur.fetchall()]
    for rid in old_rids:
        async with db.execute("SELECT id FROM ht_groups WHERE round_id=?", (rid,)) as cur:
            gids = [r["id"] for r in await cur.fetchall()]
        for gid in gids:
            await db.execute("DELETE FROM ht_slot_ranks WHERE group_id=?", (gid,))
            await db.execute("DELETE FROM ht_results WHERE group_id=?", (gid,))
            await db.execute("DELETE FROM ht_slots WHERE group_id=?", (gid,))
        await db.execute("DELETE FROM ht_groups WHERE round_id=?", (rid,))
    await db.execute(
        "DELETE FROM ht_rounds WHERE tournament_id=? AND heat_no=? AND section_no=0",
        (tid, heat_no),
    )
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/{tid}/qualifying/heat-tournament/{heat_no}/generate")
async def ht_generate(tid: int, heat_no: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ヒートNのトーナメントを生成"""
    if await _is_heat_locked(tid, heat_no, db):
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying/heat-tournament/{heat_no}?error=locked", status_code=303)
    form = await request.form()
    pattern_str = form.get("pattern", "")
    is_auto = pattern_str == "auto"
    pattern = [] if is_auto else [int(x) for x in pattern_str.split(",") if x.strip()]
    # 全ラウンド事前選択（即決勝と同方式）。"3,3,3|3,2|2" のように R1|R2|...|決勝。
    all_patterns_str = (form.get("all_patterns") or "").strip()
    ht_bracket_mode = form.get("bracket_mode", "third_place")

    # グループ別パターン（pattern_1, pattern_2, ...）
    section_patterns = {}
    for key, val in form.items():
        if key.startswith("pattern_") and key[8:].isdigit():
            sec = int(key[8:])
            section_patterns[sec] = [int(x) for x in val.split(",") if x.strip()]

    use_section_patterns = bool(section_patterns)

    if not pattern and not is_auto and not use_section_patterns:
        return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying/heat-tournament/{heat_no}", status_code=303)

    # 既存データ削除
    async with db.execute(
        "SELECT id FROM ht_rounds WHERE tournament_id=? AND heat_no=?", (tid, heat_no)
    ) as cur:
        old_rounds = [r["id"] for r in await cur.fetchall()]
    for rid in old_rounds:
        async with db.execute("SELECT id FROM ht_groups WHERE round_id=?", (rid,)) as cur:
            old_groups = [r["id"] for r in await cur.fetchall()]
        for gid in old_groups:
            await db.execute("DELETE FROM ht_slot_ranks WHERE group_id=?", (gid,))
            await db.execute("DELETE FROM ht_results WHERE group_id=?", (gid,))
            await db.execute("DELETE FROM ht_slots WHERE group_id=?", (gid,))
        await db.execute("DELETE FROM ht_groups WHERE round_id=?", (rid,))
    await db.execute("DELETE FROM ht_rounds WHERE tournament_id=? AND heat_no=?", (tid, heat_no))

    # エントリー取得
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t_row = await cur.fetchone()
    heat_exclude = bool(dict(t_row).get("qual_heat_exclude", 0)) if t_row else False
    group_count = int(dict(t_row).get("qual_group_count") or 1)
    is_hr_final = (dict(t_row).get("qualifying_type") == "heat_roundrobin" and
                   bool(dict(t_row).get("qual_heat_final")))

    if is_hr_final:
        # heat_roundrobin ヒート決勝: このヒートの各グループ上位 group_advance 名のみ
        group_advance = int(dict(t_row).get("qual_group_advance") or 1)
        finalist_entry_ids = []
        seen = set()
        for gno in range(1, group_count + 1):
            grp_standings = await _calc_standings_group_round(tid, gno, heat_no, db)
            count = 0
            for s in grp_standings:
                if count >= group_advance:
                    break
                if s["entry_id"] not in seen:
                    finalist_entry_ids.append(s["entry_id"])
                    seen.add(s["entry_id"])
                    count += 1
        async with db.execute(
            "SELECT id as entry_id, racer_id FROM entries WHERE id IN ({}) ORDER BY entry_order".format(
                ",".join("?" * len(finalist_entry_ids))
            ) if finalist_entry_ids else "SELECT id as entry_id, racer_id FROM entries WHERE 0",
            finalist_entry_ids if finalist_entry_ids else [],
        ) as cur:
            entries = [dict(r) for r in await cur.fetchall()]
    else:
        async with db.execute(
            "SELECT id as entry_id, racer_id FROM entries WHERE tournament_id=? AND status='active' ORDER BY entry_order",
            (tid,),
        ) as cur:
            entries = [dict(r) for r in await cur.fetchall()]

    # qual_heat_exclude=1 かつ heat_no>1 の場合、前ヒートで「決勝進出が確定した」レーサーを除外。
    #   ヒート決勝あり → 前ヒートのヒート決勝で上位 qual_heat_final_advance 名（＝本戦進出者）のみ除外。
    #     （ヒート決勝で敗退した人はまだ決勝進出していないので次ヒートに出場できる）
    #   ヒート決勝なし → 各グループ上位 qual_group_advance 名が直接決勝進出なので、それを除外。
    if heat_exclude and heat_no > 1:
        hf_enabled_ex = bool(dict(t_row).get("qual_heat_final", 0))
        excluded_entry_ids = set()
        for prev_hno in range(1, heat_no):
            if hf_enabled_ex:
                hfa = int(dict(t_row).get("qual_heat_advance") or 1)
                adv = await _ht_get_heatfinal_advancers(tid, prev_hno, hfa, db)
                for a in adv:
                    if a.get("is_advance") and a.get("entry_id"):
                        excluded_entry_ids.add(a["entry_id"])
            else:
                group_advance_ex = int(dict(t_row).get("qual_group_advance") or 1)
                adv = await _ht_get_advanced(tid, prev_hno, group_advance_ex, db)
                for a in adv:
                    if a.get("entry_id"):
                        excluded_entry_ids.add(a["entry_id"])
        entries = [e for e in entries if e["entry_id"] not in excluded_entry_ids]

    entry_ids = [e["entry_id"] for e in entries]
    racer_map = {e["entry_id"]: e["racer_id"] for e in entries}

    import random

    if group_count > 1:
        # グループ分け：均等に section_no=1,2,... に分割してシャッフル
        shuffled = entry_ids[:]
        random.shuffle(shuffled)
        base, rem = divmod(len(shuffled), group_count)
        sections = []
        idx = 0
        for s in range(group_count):
            sz = base + (1 if s < rem else 0)
            sections.append(shuffled[idx:idx+sz])
            idx += sz

        for sec_no, sec_entries in enumerate(sections, 1):
            # セクション別パターン指定があればそれを使用、なければ自動決定
            if use_section_patterns and sec_no in section_patterns:
                sec_pattern = section_patterns[sec_no]
            else:
                pats = _ht_combinations_2_3(len(sec_entries))
                sec_pattern = pats[0] if pats else [len(sec_entries)]
            assigned = _ht_seed_assign(sec_entries, sec_pattern, racer_map=racer_map)
            r1_type = "final" if len(sec_pattern) == 1 else "normal"
            await db.execute(
                "INSERT INTO ht_rounds (tournament_id, heat_no, round_no, round_type, section_no) VALUES (?,?,?,?,?)",
                (tid, heat_no, 1, r1_type, sec_no),
            )
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                round_id = (await cur.fetchone())["id"]
            for gno, members in enumerate(assigned, 1):
                random.shuffle(members)
                await db.execute("INSERT INTO ht_groups (round_id, group_no) VALUES (?,?)", (round_id, gno))
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    gid = (await cur.fetchone())["id"]
                for sno, eid in enumerate(members, 1):
                    await db.execute("INSERT INTO ht_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (gid, sno, eid))

        # ※ ヒート決勝（section_no=0）はここでは生成しない。
        #    全グループの順位確定後、ユーザーが「ヒート決勝トーナメントを生成」ボタンを
        #    押したタイミングで heat_final_tournament_generate により生成・流入する。
    else:
        assigned = _ht_seed_assign(entry_ids, pattern, racer_map=racer_map)
        # 全ラウンド事前生成（all_patterns 指定時）：即決勝と同じく、R1は実レーサー、
        # R2以降は空枠で先に全ラウンドを作る。結果入力で prefill が順次埋める。
        all_round_pats = [(1, pattern)]
        if all_patterns_str:
            raw = all_patterns_str.split("|")
            for i, rp in enumerate(raw[1:], 2):
                p = [int(x) for x in rp.split(",") if x.strip()]
                if p and all(2 <= s <= 3 for s in p):
                    all_round_pats.append((i, p))
            # 決勝モードに応じた最終ラウンド付与（準決勝の次に決勝）
            last_rno, last_pat = all_round_pats[-1]
            last_n = len(last_pat)  # 準決勝グループ数 = 勝者数
            if last_n > 1:
                final_rno = last_rno + 1
                if ht_bracket_mode == "semi_3group":
                    all_round_pats.append((final_rno, [3] if last_n == 3 else [last_n]))
                elif ht_bracket_mode == "revival":
                    rn = last_n + (1 if last_n == 2 else 0)
                    all_round_pats.append((final_rno, [rn if rn >= 2 else last_n]))
                else:  # third_place / none
                    all_round_pats.append((final_rno, [last_n]))

        if len(all_round_pats) > 1:
            # ── 全ラウンド事前生成 ──
            total_rounds = len(all_round_pats)
            _rno_to_rid = {}
            for rno, pat in all_round_pats:
                is_final = (rno == all_round_pats[-1][0]) and len(pat) == 1
                r_type = "final" if is_final else "normal"
                await db.execute(
                    "INSERT INTO ht_rounds (tournament_id, heat_no, round_no, round_type, section_no) VALUES (?,?,?,?,?)",
                    (tid, heat_no, rno, r_type, 1),
                )
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    round_id = (await cur.fetchone())["id"]
                _rno_to_rid[rno] = round_id
                if rno == 1:
                    # R1は実レーサーを配置
                    for gno, members in enumerate(assigned, 1):
                        random.shuffle(members)
                        await db.execute("INSERT INTO ht_groups (round_id, group_no) VALUES (?,?)", (round_id, gno))
                        async with db.execute("SELECT last_insert_rowid() as id") as cur:
                            gid = (await cur.fetchone())["id"]
                        for sno, eid in enumerate(members, 1):
                            await db.execute("INSERT INTO ht_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (gid, sno, eid))
                else:
                    # R2以降は空枠
                    for gno, sz in enumerate(pat, 1):
                        await db.execute("INSERT INTO ht_groups (round_id, group_no) VALUES (?,?)", (round_id, gno))
                        async with db.execute("SELECT last_insert_rowid() as id") as cur:
                            gid = (await cur.fetchone())["id"]
                        for sno in range(1, sz + 1):
                            await db.execute("INSERT INTO ht_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (gid, sno, None))

            # 【固定リンク焼き付け・v5.7+】事前生成した隣接ラウンド間で、
            # 現ラウンドのグループ（group_no順）→ 次ラウンドの空枠
            # （entry_id IS NULL・seed_rank IS NULL、group_no・slot_no順）を
            # 先頭から1対1で結び ht_groups.advance_to_slot_id へ保存する。
            # 受け皿が多い場合の余り末尾枠（敗者復活戦勝者用）はリンクしない。
            _sorted_rnos = sorted(_rno_to_rid.keys())
            for _i in range(len(_sorted_rnos) - 1):
                _cur_rid = _rno_to_rid[_sorted_rnos[_i]]
                _next_rid = _rno_to_rid[_sorted_rnos[_i + 1]]
                async with db.execute(
                    "SELECT id FROM ht_groups WHERE round_id=? ORDER BY group_no", (_cur_rid,)
                ) as cur:
                    _cur_gids = [r["id"] for r in await cur.fetchall()]
                async with db.execute(
                    """SELECT s.id FROM ht_slots s JOIN ht_groups g ON g.id=s.group_id
                       WHERE g.round_id=? AND s.entry_id IS NULL AND s.seed_rank IS NULL
                       ORDER BY g.group_no, s.slot_no""",
                    (_next_rid,),
                ) as cur:
                    _dest = [r["id"] for r in await cur.fetchall()]
                for _gi, _gid in enumerate(_cur_gids):
                    if _gi >= len(_dest):
                        break
                    await db.execute(
                        "UPDATE ht_groups SET advance_to_slot_id=? WHERE id=?",
                        (_dest[_gi], _gid),
                    )
        else:
            # ── 従来動作（R1のみ。R2以降は結果入力時に自動生成）──
            r1_type = "final" if len(pattern) == 1 else "normal"
            await db.execute(
                "INSERT INTO ht_rounds (tournament_id, heat_no, round_no, round_type, section_no) VALUES (?,?,?,?,?)",
                (tid, heat_no, 1, r1_type, 1),
            )
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                round_id = (await cur.fetchone())["id"]
            for gno, members in enumerate(assigned, 1):
                random.shuffle(members)
                await db.execute("INSERT INTO ht_groups (round_id, group_no) VALUES (?,?)", (round_id, gno))
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    gid = (await cur.fetchone())["id"]
                for sno, eid in enumerate(members, 1):
                    await db.execute("INSERT INTO ht_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (gid, sno, eid))

    await db.commit()
    return RedirectResponse(url=f"/admin/tournaments/{tid}/qualifying/heat-tournament/{heat_no}", status_code=303)


@router.post("/{tid}/qualifying/heat-tournament/{heat_no}/group/{group_id}/save")
async def ht_save_result(tid: int, heat_no: int, group_id: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ヒートトーナメントの結果保存"""
    if await _is_heat_locked(tid, heat_no, db):
        return _locked_json_response()
    form = await request.form()

    async with db.execute(
        """SELECT hr.round_type, hr.round_no, hr.id as round_id, COALESCE(hr.section_no,1) as section_no
           FROM ht_groups hg JOIN ht_rounds hr ON hr.id=hg.round_id
           WHERE hg.id=?""",
        (group_id,),
    ) as cur:
        rnd = await cur.fetchone()
    if not rnd:
        return JSONResponse({"ok": False})

    is_final_rnd = rnd["round_type"] in ("final", "third")

    # heat_advanceを取得して方式を決定
    async with db.execute("SELECT qual_heat_advance FROM tournaments WHERE id=?", (tid,)) as cur:
        t_row = await cur.fetchone()
    heat_advance = int((t_row["qual_heat_advance"] if t_row else None) or 1)

    # 決勝グループのスロット数を確認
    async with db.execute("SELECT COUNT(*) as cnt FROM ht_slots WHERE group_id=?", (group_id,)) as cur:
        n_slots = (await cur.fetchone())["cnt"]

    # 順位入力の出し分け（確定仕様: 決勝グループに並んだ人数ぶん順位入力できる）
    #   順位決定ラウンド(final/third) かつ グループ3名以上 → 順位選択（1〜n位）
    #   2名グループ → ○×（勝者=1位 / 敗者=2位）
    use_rank = is_final_rnd and n_slots >= 3

    # フォームに winner_slot_id が含まれる場合は○×方式として扱う（テンプレバージョン互換）
    # ※ 古いテンプレが winner_slot_id を送信してきた場合にも正しく保存できるようにする
    if use_rank and form.get("winner_slot_id"):
        use_rank = False

    if use_rank:
        # 順位選択式（rank_<slot_id>パラメータ）
        await db.execute("DELETE FROM ht_slot_ranks WHERE group_id=?", (group_id,))
        async with db.execute("SELECT id FROM ht_slots WHERE group_id=? ORDER BY slot_no", (group_id,)) as cur:
            slots = await cur.fetchall()
        winner_slot_id = None
        for slot in slots:
            rank_val = form.get(f"rank_{slot['id']}")
            if rank_val:
                rv = int(rank_val)
                # 同じ順位が既にある場合はクリア
                await db.execute(
                    "DELETE FROM ht_slot_ranks WHERE group_id=? AND rank=? AND slot_id!=?",
                    (group_id, rv, slot["id"]),
                )
                # 現在のスロットに同じ順位を再押ししたらリセット
                async with db.execute(
                    "SELECT id FROM ht_slot_ranks WHERE group_id=? AND slot_id=? AND rank=?",
                    (group_id, slot["id"], rv),
                ) as cur:
                    existing_rank = await cur.fetchone()
                if existing_rank:
                    await db.execute("DELETE FROM ht_slot_ranks WHERE id=?", (existing_rank["id"],))
                else:
                    await db.execute(
                        "INSERT INTO ht_slot_ranks (group_id, slot_id, rank) VALUES (?,?,?)",
                        (group_id, slot["id"], rv),
                    )
                    if rv == 1:
                        winner_slot_id = slot["id"]
        # 1位が決まったらresultに反映
        async with db.execute(
            "SELECT slot_id FROM ht_slot_ranks WHERE group_id=? AND rank=1", (group_id,)
        ) as cur:
            w = await cur.fetchone()
        winner_slot_id = w["slot_id"] if w else None
        await db.execute("DELETE FROM ht_results WHERE group_id=?", (group_id,))
        if winner_slot_id:
            await db.execute("INSERT INTO ht_results (group_id, winner_slot_id) VALUES (?,?)", (group_id, winner_slot_id))
    else:
        # ○×方式（通常ラウンド or heat_advance=1の決勝）
        winner_slot_id = form.get("winner_slot_id")
        if not winner_slot_id:
            return JSONResponse({"ok": False})
        winner_slot_id = int(winner_slot_id)
        async with db.execute("SELECT winner_slot_id FROM ht_results WHERE group_id=?", (group_id,)) as cur:
            existing = await cur.fetchone()
        if existing and existing["winner_slot_id"] == winner_slot_id:
            await db.execute("DELETE FROM ht_results WHERE group_id=?", (group_id,))
            await db.execute("DELETE FROM ht_slot_ranks WHERE group_id=?", (group_id,))
            winner_slot_id = None
        else:
            await db.execute("DELETE FROM ht_results WHERE group_id=?", (group_id,))
            await db.execute("DELETE FROM ht_slot_ranks WHERE group_id=?", (group_id,))
            await db.execute("INSERT INTO ht_results (group_id, winner_slot_id) VALUES (?,?)", (group_id, winner_slot_id))
            # 全スロットに自動で順位を設定（1位=勝者、2位以降=その他）
            async with db.execute(
                "SELECT id FROM ht_slots WHERE group_id=? ORDER BY slot_no", (group_id,)
            ) as cur:
                all_slots = await cur.fetchall()
            rank = 1
            for s in all_slots:
                r = 1 if s["id"] == winner_slot_id else rank + 1 if rank == 1 else rank
                if s["id"] != winner_slot_id:
                    rank += 1
                await db.execute(
                    "INSERT INTO ht_slot_ranks (group_id, slot_id, rank) VALUES (?,?,?)",
                    (group_id, s["id"], 1 if s["id"] == winner_slot_id else 2),
                )

    await db.commit()

    # 次ラウンドが既に存在する場合、現ラウンド全グループの勝者で対応スロットを再構成する。
    # （勝者の取り消し・変更も毎回反映されるよう、winner_slot_id の有無で分岐しない）
    if rnd["section_no"] == 0:
        # ── ヒート決勝（段階シード）専用: 勝者を次ラウンドの seed_rank=NULL 枠へ ──
        await _ht_advance_heat_final_winner(
            tid, heat_no, rnd["round_no"], group_id, winner_slot_id, db
        )
    else:
        await _ht_advance_section_winner(
            tid, heat_no, rnd["round_no"], rnd["round_id"], rnd["section_no"],
            group_id, winner_slot_id, db
        )

    # ②敗者復活戦の勝者を決勝（同一round_noのfinal）の空き枠へ
    # 【固定リンク対応】準決勝勝者用に焼き付けられたリンク先スロットは避け、
    # 復活戦勝者用に残された枠（どのグループの advance_to_slot_id でもない枠）へ入れる。
    # リンクの無い既存データでは NOT IN が実質無効となり従来どおり先頭の空き枠に入る。
    if winner_slot_id and rnd["round_type"] == "revival":
        async with db.execute(
            "SELECT id FROM ht_rounds WHERE tournament_id=? AND heat_no=? AND round_no=? AND round_type='final' ORDER BY id LIMIT 1",
            (tid, heat_no, rnd["round_no"]),
        ) as cur:
            fin = await cur.fetchone()
        if fin:
            async with db.execute("SELECT entry_id FROM ht_slots WHERE id=?", (winner_slot_id,)) as cur:
                we = await cur.fetchone()
            async with db.execute(
                """SELECT s.id FROM ht_slots s JOIN ht_groups g ON g.id=s.group_id
                   WHERE g.round_id=? AND s.entry_id IS NULL
                     AND s.id NOT IN (
                       SELECT advance_to_slot_id FROM ht_groups
                       WHERE advance_to_slot_id IS NOT NULL
                     )
                   ORDER BY s.slot_no LIMIT 1""",
                (fin["id"],),
            ) as cur:
                empty = await cur.fetchone()
            if we and empty:
                await db.execute("UPDATE ht_slots SET entry_id=? WHERE id=?", (we["entry_id"], empty["id"]))
                await db.commit()

    # 全グループ完了したら次ラウンド生成
    advanced = await _ht_try_advance(tid, heat_no, rnd["round_id"], rnd["round_no"], db)

    # ── セクション決勝完了時: ヒート決勝（section_no=0）へ上位者を流し込む ──
    # セクション（section_no > 0）の final が終わったとき、そのセクションの
    # ※ ヒート決勝（section_no=0）への自動流入は行わない。
    #    全グループの順位確定後、ユーザーがボタンを押したタイミングで
    #    heat_final_tournament_generate がまとめて生成・流入する（自動連動なし）。

    await _ht_update_advanced(tid, db)
    # このラウンドが全グループ完了したか（＝次ラウンドのカードを表示すべきタイミング）
    async with db.execute("SELECT id FROM ht_groups WHERE round_id=?", (rnd["round_id"],)) as cur:
        _rg = [r["id"] for r in await cur.fetchall()]
    round_done = False
    if _rg:
        async with db.execute(
            "SELECT COUNT(*) FROM ht_results WHERE group_id IN ({})".format(",".join("?" * len(_rg))),
            _rg,
        ) as cur:
            _dc = (await cur.fetchone())[0]
        round_done = _dc >= len(_rg)
    # 決勝または3位決定戦が完了した場合はフラグを返す
    is_final_done = rnd["round_type"] in ("final", "third")
    # ヒート決勝で進出が全員確定したら、このヒートを自動ロックする
    await _maybe_lock_heat(tid, heat_no, db)
    locked_now = await _is_heat_locked(tid, heat_no, db)
    return JSONResponse({"ok": True, "advanced": bool(advanced), "is_final_done": is_final_done, "round_done": round_done, "locked": locked_now})


async def _ht_all_groups_ranked(tid: int, heat_no: int, group_advance: int, db) -> tuple[bool, str]:
    """ヒート内の全グループ(section>0)で、通過人数分(1〜group_advance位)の順位が
    すべて確定しているか判定する。final と（あれば）third の ht_slot_ranks を見る。

    戻り値: (判定, メッセージ)
    """
    async with db.execute(
        "SELECT DISTINCT section_no FROM ht_rounds WHERE tournament_id=? AND heat_no=? AND section_no>0 ORDER BY section_no",
        (tid, heat_no),
    ) as cur:
        sec_nos = [r["section_no"] for r in await cur.fetchall()]
    if not sec_nos:
        return False, "グループが生成されていません。"

    N = max(1, group_advance)
    for sec in sec_nos:
        got_ranks = set()
        # final
        async with db.execute(
            """SELECT hr.id FROM ht_rounds hr
               WHERE hr.tournament_id=? AND hr.heat_no=? AND hr.section_no=? AND hr.round_type='final'
               ORDER BY hr.round_no DESC LIMIT 1""",
            (tid, heat_no, sec),
        ) as cur:
            fr = await cur.fetchone()
        if fr:
            async with db.execute(
                """SELECT sr.rank FROM ht_slot_ranks sr
                   JOIN ht_groups hg ON hg.id=sr.group_id
                   WHERE hg.round_id=?""",
                (fr["id"],),
            ) as cur:
                for r in await cur.fetchall():
                    got_ranks.add(r["rank"])
        # third（3位決定戦は3位以降を補完）
        async with db.execute(
            """SELECT hr.id FROM ht_rounds hr
               WHERE hr.tournament_id=? AND hr.heat_no=? AND hr.section_no=? AND hr.round_type='third'
               ORDER BY hr.round_no DESC LIMIT 1""",
            (tid, heat_no, sec),
        ) as cur:
            tr = await cur.fetchone()
        if tr:
            async with db.execute(
                """SELECT sr.rank FROM ht_slot_ranks sr
                   JOIN ht_groups hg ON hg.id=sr.group_id
                   WHERE hg.round_id=?""",
                (tr["id"],),
            ) as cur:
                for r in await cur.fetchall():
                    # 3位決定戦の1位→3位, 2位→4位 として補完
                    got_ranks.add(2 + r["rank"])
        # 1〜N 位がすべて揃っているか
        need = set(range(1, N + 1))
        if not need.issubset(got_ranks):
            return False, f"グループ{sec}の順位（上位{N}名）が未確定です。"
    return True, ""


def _ht_split_2_3(n: int) -> list[int]:
    """n名を「原則2名・割り切れない場合のみ3名」でグループ分けしたサイズ列を返す。
    例: 2→[2] / 3→[3] / 4→[2,2] / 5→[3,2] / 6→[2,2,2] / 7→[3,2,2]
    1以下は[ n ]（呼び出し側で扱う）。"""
    if n <= 1:
        return [n] if n > 0 else []
    if n % 2 == 0:
        return [2] * (n // 2)
    # 奇数 → 3を1つだけ作り残りを2に
    return [3] + [2] * ((n - 3) // 2)


async def _ht_build_heat_final_rounds(
    tid: int, heat_no: int, group_count: int, group_advance: int,
    heat_final_advance: int, db,
) -> None:
    """ヒート決勝（section_no=0）の段階シード枠を事前生成する。

    順位群を最下位(tier=group_advance)→最上位(tier=1)へ積み上げる:
      - tier=group_advance の group_count 名でR1
      - 前ラウンド勝者1名 + tier の group_count 名 で次ラウンド
      - tier=1 を加えた最終ラウンドが優勝決定戦(final)
    各ラウンドは原則2名/グループ（不可なら3名）。空スロットには seed_rank=tier を
    記録し、セクション決勝完了時に _ht_fill_heat_final が順位群ごとに流し込む。

    通過人数 heat_final_advance に応じて順位決定ラウンドを付与:
      - 決勝が2名 かつ advance>=3 → 準決勝敗者で3位決定戦(third)
      - 決勝が3名 → 決勝内で1〜3位（3位決定戦は作らない）
      - advance=1 → 1位のみ。順位決定の追加段は作らない
    """
    C = max(1, group_count)
    N = max(1, group_advance)
    if C * N < 2:
        return  # ヒート決勝が成立しない

    # tier を N(最下位)→1(最上位) の順に処理。各ラウンドの構成を組み立てる。
    # rounds: [(round_no, round_type, [(group_size, [seed_rank or None,...]), ...]), ...]
    rounds = []
    carry = 0          # 前ラウンドから上がってくる勝者数（R1は0）
    round_no = 0
    for tier in range(N, 0, -1):
        round_no += 1
        people = carry + C            # このラウンドの人数
        # 各人の「枠の出所」: carry 人は勝者繰り上がり(None)、残り C 人は tier 群
        members_src = [None] * carry + [tier] * C
        sizes = _ht_split_2_3(people)
        # members_src を順番に sizes へ割り当て
        groups = []
        idx = 0
        for sz in sizes:
            groups.append(members_src[idx:idx + sz])
            idx += sz
        is_last_tier = (tier == 1)
        # 次ラウンドへ上がる勝者数 = このラウンドのグループ数
        next_carry = len(sizes)
        round_type = "normal"
        if is_last_tier and next_carry == 1:
            round_type = "final"     # 1グループに収束＝優勝決定戦
        rounds.append((round_no, round_type, groups, next_carry))
        carry = next_carry
        if is_last_tier:
            break

    # 最終ラウンドが final になっていない（最上位群を加えても複数グループ）場合、
    # さらに勝者を集約する決勝ラウンドを追加する。
    last_rno, last_type, last_groups, last_carry = rounds[-1]
    if last_type != "final" and last_carry >= 2:
        round_no += 1
        fsizes = _ht_split_2_3(last_carry)
        fgroups = [[None] * sz for sz in fsizes]
        ftype = "final" if len(fsizes) == 1 else "normal"
        rounds.append((round_no, ftype, fgroups, len(fsizes)))
        last_rno, last_type, last_groups, last_carry = rounds[-1]

    # 決勝の人数を確定
    final_size = sum(len(g) for g in last_groups) if last_groups else 0

    # ── DBへ書き込み ──
    for rno, rtype, groups, _carry in rounds:
        await db.execute(
            "INSERT INTO ht_rounds (tournament_id, heat_no, round_no, round_type, section_no) VALUES (?,?,?,?,?)",
            (tid, heat_no, rno, rtype, 0),
        )
        async with db.execute("SELECT last_insert_rowid() as id") as cur:
            rid = (await cur.fetchone())["id"]
        for gno, gmembers in enumerate(groups, 1):
            await db.execute("INSERT INTO ht_groups (round_id, group_no) VALUES (?,?)", (rid, gno))
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                gid = (await cur.fetchone())["id"]
            for sno, seed_rank in enumerate(gmembers, 1):
                await db.execute(
                    "INSERT INTO ht_slots (group_id, slot_no, entry_id, seed_rank) VALUES (?,?,?,?)",
                    (gid, sno, None, seed_rank),
                )

    # ── 3位決定戦(third)の付与 ──
    # 決勝が2名 かつ 通過人数>=3 のとき、準決勝（決勝の1つ前）敗者2名で3位決定戦。
    # 決勝が3名以上なら決勝内で順位を取れるため不要。通過1名なら不要。
    if heat_final_advance >= 3 and final_size == 2 and len(rounds) >= 2:
        prev_rno = rounds[-2][0]
        # 準決勝（決勝直前ラウンド）のグループ数が2なら3位決定戦を1組（2名）作る
        prev_groups = rounds[-2][2]
        if len(prev_groups) == 2:
            await db.execute(
                "INSERT INTO ht_rounds (tournament_id, heat_no, round_no, round_type, section_no) VALUES (?,?,?,?,?)",
                (tid, heat_no, last_rno, "third", 0),
            )
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                trid = (await cur.fetchone())["id"]
            await db.execute("INSERT INTO ht_groups (round_id, group_no) VALUES (?,?)", (trid, 1))
            async with db.execute("SELECT last_insert_rowid() as id") as cur:
                tgid = (await cur.fetchone())["id"]
            for sno in range(1, 3):
                await db.execute(
                    "INSERT INTO ht_slots (group_id, slot_no, entry_id, seed_rank) VALUES (?,?,?,?)",
                    (tgid, sno, None, None),
                )


async def _ht_fill_heat_final(tid: int, heat_no: int, db) -> None:
    """全セクションの決勝が確定している前提で、ヒート決勝（section_no=0）の
    seed_rank 付き空きスロットへ、各セクションの該当順位の選手を流し込む。

    - seed_rank=tier のスロット群へ、各セクション final の tier 位の選手を配置。
    - セクション側 final が未確定の場合、そのセクション分は流入を保留（後で再実行）。
    - 重複・既配置は上書きしない（冪等）。
    """
    async with db.execute("SELECT qual_group_advance FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return
    group_advance = int(t["qual_group_advance"] or 1)

    # セクション一覧（section_no>0）
    async with db.execute(
        "SELECT DISTINCT section_no FROM ht_rounds WHERE tournament_id=? AND heat_no=? AND section_no>0 ORDER BY section_no",
        (tid, heat_no),
    ) as cur:
        sec_nos = [r["section_no"] for r in await cur.fetchall()]

    # 各セクションの順位 → entry_id を集める  rank_map[tier] = [entry_id, ...]（セクション順）
    rank_map: dict[int, list] = {tier: [] for tier in range(1, group_advance + 1)}
    for sec in sec_nos:
        async with db.execute(
            """SELECT hr.id as round_id FROM ht_rounds hr
               WHERE hr.tournament_id=? AND hr.heat_no=? AND hr.section_no=? AND hr.round_type='final'
               ORDER BY hr.round_no DESC LIMIT 1""",
            (tid, heat_no, sec),
        ) as cur:
            fin_rnd = await cur.fetchone()
        if not fin_rnd:
            continue
        # final グループの順位を取得（通常1グループ）
        async with db.execute(
            "SELECT id FROM ht_groups WHERE round_id=? ORDER BY group_no", (fin_rnd["round_id"],)
        ) as cur:
            fin_gids = [r["id"] for r in await cur.fetchall()]
        # rank→entry_id（final）＋ 3位決定戦(third)があればそれも加味
        sec_rank_entry: dict[int, int] = {}
        for gid in fin_gids:
            async with db.execute(
                """SELECT sr.rank, hs.entry_id FROM ht_slot_ranks sr
                   JOIN ht_slots hs ON hs.id=sr.slot_id
                   WHERE sr.group_id=? ORDER BY sr.rank""",
                (gid,),
            ) as cur:
                for row in await cur.fetchall():
                    if row["entry_id"] and row["rank"] not in sec_rank_entry:
                        sec_rank_entry[row["rank"]] = row["entry_id"]
        # 3位決定戦(third)からの順位補完（セクション側に3位決定戦がある構成向け）
        async with db.execute(
            """SELECT hr.id as round_id FROM ht_rounds hr
               WHERE hr.tournament_id=? AND hr.heat_no=? AND hr.section_no=? AND hr.round_type='third'
               ORDER BY hr.round_no DESC LIMIT 1""",
            (tid, heat_no, sec),
        ) as cur:
            third_rnd = await cur.fetchone()
        if third_rnd:
            async with db.execute(
                "SELECT id FROM ht_groups WHERE round_id=? ORDER BY group_no", (third_rnd["round_id"],)
            ) as cur:
                third_gids = [r["id"] for r in await cur.fetchall()]
            base = 2  # 決勝で1,2位確定 → 3位決定戦は3位から
            for gid in third_gids:
                async with db.execute(
                    """SELECT sr.rank, hs.entry_id FROM ht_slot_ranks sr
                       JOIN ht_slots hs ON hs.id=sr.slot_id
                       WHERE sr.group_id=? ORDER BY sr.rank""",
                    (gid,),
                ) as cur:
                    for row in await cur.fetchall():
                        tier = base + row["rank"]
                        if row["entry_id"] and tier not in sec_rank_entry:
                            sec_rank_entry[tier] = row["entry_id"]
        # rank_map へ反映
        for tier in range(1, group_advance + 1):
            eid = sec_rank_entry.get(tier)
            if eid is not None:
                rank_map[tier].append(eid)

    # seed_rank 付きの空きスロットへ tier ごとに配置
    for tier in range(1, group_advance + 1):
        entries_for_tier = rank_map.get(tier, [])
        if not entries_for_tier:
            continue
        async with db.execute(
            """SELECT hs.id as slot_id FROM ht_slots hs
               JOIN ht_groups hg ON hg.id=hs.group_id
               JOIN ht_rounds hr ON hr.id=hg.round_id
               WHERE hr.tournament_id=? AND hr.heat_no=? AND hr.section_no=0
                 AND hs.seed_rank=? AND hs.entry_id IS NULL
               ORDER BY hr.round_no, hg.group_no, hs.slot_no""",
            (tid, heat_no, tier),
        ) as cur:
            empty_slots = [r["slot_id"] for r in await cur.fetchall()]
        for slot_id, entry_id in zip(empty_slots, entries_for_tier):
            await db.execute("UPDATE ht_slots SET entry_id=? WHERE id=?", (entry_id, slot_id))
    await db.commit()


async def _ht_advance_section_winner(
    tid: int, heat_no: int, round_no: int, round_id: int, section_no: int,
    group_id: int, winner_slot_id: int, db,
) -> None:
    """セクション内トーナメント(section_no>0)の勝者を次ラウンドへ繰り上げる。

    【再入力対応】個別グループの「空き枠」を1つ探して書く旧方式は、次ラウンドの
    枠が既に埋まっていると更新されず、前ラウンドの結果を変えても次ラウンドが
    固定されてしまうバグがあった。これを解消するため、引数のグループ単体ではなく
    現ラウンドの全グループの確定済み勝者を毎回集め直し、次ラウンドの対応スロットへ
    位置対応で一括上書きする（空き／既存にかかわらず上書き）。"""
    async with db.execute(
        """SELECT id FROM ht_rounds
           WHERE tournament_id=? AND heat_no=? AND round_no=? AND COALESCE(section_no,1)=?
             AND round_type!='third'
           ORDER BY id LIMIT 1""",
        (tid, heat_no, round_no + 1, section_no),
    ) as cur:
        next_rnd = await cur.fetchone()
    if not next_rnd:
        return

    # 現ラウンドの全グループ（group_no順）
    async with db.execute(
        "SELECT id, advance_to_slot_id FROM ht_groups WHERE round_id=? ORDER BY group_no", (round_id,)
    ) as cur:
        all_group_rows = [dict(r) for r in await cur.fetchall()]
    all_groups = [g["id"] for g in all_group_rows]
    if not all_groups or group_id not in all_groups:
        return

    # ── 新方式（固定リンク・v5.7+）──────────────────────────
    # 次ラウンド生成時に焼き付けられた advance_to_slot_id があれば、
    # 各グループの勝者をリンク先スロットへ書くだけで反映する。
    # 勝者未確定（取消）はリンク先を NULL に戻す。組み合わせは変化しない。
    # リンクの無い既存データは従来の位置対応（後方互換）で処理する。
    if any(g.get("advance_to_slot_id") for g in all_group_rows):
        for g in all_group_rows:
            _adv = g.get("advance_to_slot_id")
            if not _adv:
                continue
            async with db.execute(
                """SELECT hs.entry_id
                   FROM ht_results hr JOIN ht_slots hs ON hs.id=hr.winner_slot_id
                   WHERE hr.group_id=?""",
                (g["id"],),
            ) as cur:
                _w = await cur.fetchone()
            await db.execute(
                "UPDATE ht_slots SET entry_id=? WHERE id=?",
                (_w["entry_id"] if _w else None, _adv),
            )
        await db.commit()
        return

    # 各グループの確定済み勝者 entry_id を group_no順に収集（未確定は None）
    winners: list = []
    for gid in all_groups:
        async with db.execute(
            """SELECT hs.entry_id
               FROM ht_results hr JOIN ht_slots hs ON hs.id=hr.winner_slot_id
               WHERE hr.group_id=?""",
            (gid,),
        ) as cur:
            w = await cur.fetchone()
        winners.append(w["entry_id"] if w else None)

    # 次ラウンドの「勝者を受ける枠」を group_no, slot_no順に取得。
    # 段階シード以外のセクション内トーナメントでは seed_rank は使われないが、
    # 念のためシード枠（seed_rank IS NOT NULL）は除外して前ラウンド勝者用の枠だけを対象にする。
    async with db.execute(
        """SELECT hs.id as slot_id
           FROM ht_slots hs JOIN ht_groups hg ON hg.id=hs.group_id
           WHERE hg.round_id=? AND hs.seed_rank IS NULL
           ORDER BY hg.group_no, hs.slot_no""",
        (next_rnd["id"],),
    ) as cur:
        target_slots = [r["slot_id"] for r in await cur.fetchall()]
    if not target_slots:
        return

    # winners[i] を target_slots[i] へ位置対応で配置（既存値があっても上書き）。
    # 勝者未確定（None）の枠は NULL クリアし、勝者取り消しも次ラウンドへ反映する。
    # 同一エントリの二重配置を避けつつ、対象枠数を超える分は無視する。
    placed: set = set()
    for i, eid in enumerate(winners):
        if i >= len(target_slots):
            break
        if eid is None:
            await db.execute(
                "UPDATE ht_slots SET entry_id=NULL WHERE id=?",
                (target_slots[i],),
            )
            continue
        if eid in placed:
            await db.execute(
                "UPDATE ht_slots SET entry_id=NULL WHERE id=?",
                (target_slots[i],),
            )
            continue
        await db.execute(
            "UPDATE ht_slots SET entry_id=? WHERE id=?",
            (eid, target_slots[i]),
        )
        placed.add(eid)
    await db.commit()


async def _ht_advance_heat_final_winner(
    tid: int, heat_no: int, round_no: int, group_id: int, winner_slot_id: int, db,
) -> None:
    """ヒート決勝（section_no=0・段階シード）の勝者を次ラウンドの勝者枠へ繰り上げる。

    【再入力対応】「空きスロットを探して書く」旧方式は、次ラウンドが既に埋まっていると
    前ラウンドの結果変更が反映されなかった。現ラウンドの全グループ勝者を毎回集め直し、
    次ラウンドの勝者枠（seed_rank IS NULL）へ group_no 順の位置対応で一括上書きする。"""
    async with db.execute(
        """SELECT id FROM ht_rounds
           WHERE tournament_id=? AND heat_no=? AND round_no=? AND section_no=0
             AND round_type!='third'
           ORDER BY id LIMIT 1""",
        (tid, heat_no, round_no + 1),
    ) as cur:
        next_rnd = await cur.fetchone()
    if not next_rnd:
        return

    # 現ラウンド（section_no=0）の全グループを group_no順に取得
    async with db.execute(
        """SELECT hg.id, hg.advance_to_slot_id
           FROM ht_groups hg JOIN ht_rounds hr ON hr.id=hg.round_id
           WHERE hr.tournament_id=? AND hr.heat_no=? AND hr.round_no=? AND hr.section_no=0
             AND hr.round_type!='third'
           ORDER BY hg.group_no""",
        (tid, heat_no, round_no),
    ) as cur:
        cur_group_rows = [dict(r) for r in await cur.fetchall()]
    cur_groups = [g["id"] for g in cur_group_rows]
    if not cur_groups or group_id not in cur_groups:
        return

    # ── 新方式（固定リンク・v5.7+）: リンクが焼かれていればそれを辿るだけ ──
    if any(g.get("advance_to_slot_id") for g in cur_group_rows):
        for g in cur_group_rows:
            _adv = g.get("advance_to_slot_id")
            if not _adv:
                continue
            async with db.execute(
                """SELECT hs.entry_id
                   FROM ht_results hr JOIN ht_slots hs ON hs.id=hr.winner_slot_id
                   WHERE hr.group_id=?""",
                (g["id"],),
            ) as cur:
                _w = await cur.fetchone()
            await db.execute(
                "UPDATE ht_slots SET entry_id=? WHERE id=?",
                (_w["entry_id"] if _w else None, _adv),
            )
        await db.commit()
        return

    # 各グループの確定済み勝者 entry_id を group_no順に収集（未確定は None）
    winners: list = []
    for gid in cur_groups:
        async with db.execute(
            """SELECT hs.entry_id
               FROM ht_results hr JOIN ht_slots hs ON hs.id=hr.winner_slot_id
               WHERE hr.group_id=?""",
            (gid,),
        ) as cur:
            w = await cur.fetchone()
        winners.append(w["entry_id"] if w else None)

    # 次ラウンドの「勝者枠（seed_rank IS NULL）」を group_no, slot_no順に取得
    async with db.execute(
        """SELECT hs.id as slot_id FROM ht_slots hs
           JOIN ht_groups hg ON hg.id=hs.group_id
           WHERE hg.round_id=? AND hs.seed_rank IS NULL
           ORDER BY hg.group_no, hs.slot_no""",
        (next_rnd["id"],),
    ) as cur:
        winner_slots = [r["slot_id"] for r in await cur.fetchall()]
    if not winner_slots:
        return

    # winners[i] を winner_slots[i] へ位置対応で配置（既存値があっても上書き）。
    # 勝者未確定（None）の枠は NULL クリアし、勝者取り消しも次ラウンドへ反映する。
    placed: set = set()
    for i, eid in enumerate(winners):
        if i >= len(winner_slots):
            break
        if eid is None or eid in placed:
            await db.execute("UPDATE ht_slots SET entry_id=NULL WHERE id=?", (winner_slots[i],))
            continue
        await db.execute("UPDATE ht_slots SET entry_id=? WHERE id=?", (eid, winner_slots[i]))
        placed.add(eid)
    await db.commit()


async def _ht_try_advance(tid: int, heat_no: int, round_id: int, round_no: int, db) -> bool:
    """全グループ完了したら次ラウンドを生成（同一セクション内のみ）"""
    import random as _random

    async with db.execute("SELECT * FROM ht_rounds WHERE id=?", (round_id,)) as cur:
        current_round = dict(await cur.fetchone())

    sec_no = current_round.get("section_no", 1)

    async with db.execute("SELECT * FROM ht_groups WHERE round_id=? ORDER BY group_no", (round_id,)) as cur:
        groups = [dict(r) for r in await cur.fetchall()]
    if not groups:
        return False
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM ht_results WHERE group_id IN ({})".format(",".join("?" * len(groups))),
        [g["id"] for g in groups],
    ) as cur:
        done = (await cur.fetchone())["cnt"]
    if done < len(groups):
        return False  # まだ全グループ完了していない

    # 勝者・敗者を収集
    winners = []
    losers = []
    for g in groups:
        async with db.execute(
            "SELECT winner_slot_id FROM ht_results WHERE group_id=?", (g["id"],)
        ) as cur:
            res = await cur.fetchone()
        if not res:
            continue
        async with db.execute(
            "SELECT id, entry_id FROM ht_slots WHERE group_id=? ORDER BY slot_no", (g["id"],)
        ) as cur:
            slots = [dict(r) for r in await cur.fetchall()]
        for s in slots:
            if s["id"] == res["winner_slot_id"]:
                winners.append(s["entry_id"])
            else:
                losers.append(s["entry_id"])

    if len(winners) <= 1:
        return False

    patterns = _ht_combinations_2_3(len(winners))
    if not patterns:
        return False
    pat = patterns[0]
    is_final = len(pat) == 1
    next_rno = round_no + 1

    # 既に同一セクションの次ラウンドが生成済みか確認
    async with db.execute(
        "SELECT id FROM ht_rounds WHERE tournament_id=? AND heat_no=? AND round_no=? AND COALESCE(section_no,1)=? AND round_type!=?",
        (tid, heat_no, next_rno, sec_no, "third"),
    ) as cur:
        existing_next = await cur.fetchone()

    # 決勝直前（準決勝）完了時：事前生成された決勝のスロット数で②/③を判定
    #   準決勝2組(勝者2) × 決勝3枠 → ②敗者復活戦（3枠目=復活戦勝者）
    #   準決勝2組(勝者2) × 決勝2枠 → ③3位決定戦
    need_third = False
    need_revival = False
    if is_final and len(winners) == 2 and len(losers) >= 2:
        final_slots = 0
        if existing_next:
            async with db.execute(
                "SELECT COUNT(*) c FROM ht_slots s JOIN ht_groups g ON g.id=s.group_id WHERE g.round_id=?",
                (existing_next["id"],),
            ) as cur:
                final_slots = (await cur.fetchone())["c"]
        # 上位通過人数を取得（1〜2人なら3位決定戦は不要＝④決勝2名のみ）
        async with db.execute("SELECT qual_group_advance FROM tournaments WHERE id=?", (tid,)) as cur:
            adv_row = await cur.fetchone()
        grp_adv = int((adv_row["qual_group_advance"] if adv_row else 1) or 1)
        if final_slots >= 3:
            need_revival = True       # ② 敗者復活戦
        elif grp_adv >= 3:
            need_third = True         # ③ 3位決定戦（3位以下を決める必要がある場合のみ）
        # grp_adv <= 2 → ④ 決勝2名のみ（3位決定戦なし）

    if existing_next:
        if need_third:
            async with db.execute(
                "SELECT id FROM ht_rounds WHERE tournament_id=? AND heat_no=? AND round_no=? AND COALESCE(section_no,1)=? AND round_type=?",
                (tid, heat_no, next_rno, sec_no, "third"),
            ) as cur:
                existing_third = await cur.fetchone()
            if not existing_third:
                await db.execute(
                    "INSERT INTO ht_rounds (tournament_id, heat_no, round_no, round_type, section_no) VALUES (?,?,?,?,?)",
                    (tid, heat_no, next_rno, "third", sec_no),
                )
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    third_round_id = (await cur.fetchone())["id"]
                await db.execute("INSERT INTO ht_groups (round_id, group_no) VALUES (?,?)", (third_round_id, 1))
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    third_gid = (await cur.fetchone())["id"]
                third_players = losers[:2]
                _random.shuffle(third_players)
                for sno, eid in enumerate(third_players, 1):
                    await db.execute("INSERT INTO ht_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (third_gid, sno, eid))
                await db.commit()
        elif need_revival:
            # ②敗者復活戦：準決勝敗者全員で1グループ→勝者1名が決勝の空き枠へ
            async with db.execute(
                "SELECT id FROM ht_rounds WHERE tournament_id=? AND heat_no=? AND round_no=? AND COALESCE(section_no,1)=? AND round_type=?",
                (tid, heat_no, next_rno, sec_no, "revival"),
            ) as cur:
                existing_rev = await cur.fetchone()
            if not existing_rev:
                await db.execute(
                    "INSERT INTO ht_rounds (tournament_id, heat_no, round_no, round_type, section_no) VALUES (?,?,?,?,?)",
                    (tid, heat_no, next_rno, "revival", sec_no),
                )
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    rev_round_id = (await cur.fetchone())["id"]
                await db.execute("INSERT INTO ht_groups (round_id, group_no) VALUES (?,?)", (rev_round_id, 1))
                async with db.execute("SELECT last_insert_rowid() as id") as cur:
                    rev_gid = (await cur.fetchone())["id"]
                rev_players = losers[:]
                _random.shuffle(rev_players)
                for sno, eid in enumerate(rev_players, 1):
                    await db.execute("INSERT INTO ht_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (rev_gid, sno, eid))
                await db.commit()
        return False

    # 次ラウンド生成（同一セクション）
    await db.execute(
        "INSERT INTO ht_rounds (tournament_id, heat_no, round_no, round_type, section_no) VALUES (?,?,?,?,?)",
        (tid, heat_no, next_rno, "final" if is_final else "normal", sec_no),
    )
    async with db.execute("SELECT last_insert_rowid() as id") as cur:
        next_round_id = (await cur.fetchone())["id"]

    shuffled = winners[:]
    wi = 0
    for gno, sz in enumerate(pat, 1):
        await db.execute("INSERT INTO ht_groups (round_id, group_no) VALUES (?,?)", (next_round_id, gno))
        async with db.execute("SELECT last_insert_rowid() as id") as cur:
            gid = (await cur.fetchone())["id"]
        for sno in range(1, sz + 1):
            eid = shuffled[wi] if wi < len(shuffled) else None
            await db.execute("INSERT INTO ht_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (gid, sno, eid))
            wi += 1

    # 【固定リンク焼き付け・v5.7+】現ラウンドの各グループ（group_no順）と、
    # いま生成した次ラウンドのスロット（group_no・slot_no順）を1対1で結び、
    # ht_groups.advance_to_slot_id に保存する。
    # 以後、勝者の変更・取消は _ht_advance_section_winner がこのリンクを
    # 辿って反映するため、組み合わせは生成後一切変化しない。
    async with db.execute(
        """SELECT s.id FROM ht_slots s JOIN ht_groups g ON g.id=s.group_id
           WHERE g.round_id=? AND s.seed_rank IS NULL
           ORDER BY g.group_no, s.slot_no""",
        (next_round_id,),
    ) as cur:
        _dest_slot_ids = [r["id"] for r in await cur.fetchall()]
    for _gi, _g in enumerate(groups):
        if _gi >= len(_dest_slot_ids):
            break
        await db.execute(
            "UPDATE ht_groups SET advance_to_slot_id=? WHERE id=?",
            (_dest_slot_ids[_gi], _g["id"]),
        )

    if need_third:
        await db.execute(
            "INSERT INTO ht_rounds (tournament_id, heat_no, round_no, round_type, section_no) VALUES (?,?,?,?,?)",
            (tid, heat_no, next_rno, "third", sec_no),
        )
        async with db.execute("SELECT last_insert_rowid() as id") as cur:
            third_round_id = (await cur.fetchone())["id"]
        await db.execute("INSERT INTO ht_groups (round_id, group_no) VALUES (?,?)", (third_round_id, 1))
        async with db.execute("SELECT last_insert_rowid() as id") as cur:
            third_gid = (await cur.fetchone())["id"]
        third_players = losers[:2]
        _random.shuffle(third_players)
        for sno, eid in enumerate(third_players, 1):
            await db.execute("INSERT INTO ht_slots (group_id, slot_no, entry_id) VALUES (?,?,?)", (third_gid, sno, eid))

    await db.commit()
    return True


async def _ht_update_advanced(tid: int, db):
    """全ヒートの決勝結果を集計してadvanced=1を設定
    ヒート決勝あり → 各ヒートのヒート決勝 上位 qual_heat_advance 名（本戦進出者）のみ。
    ヒート決勝なし → 各グループ上位 qual_group_advance 名（グループ通過者）。
    """
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t:
        return
    t_dict = dict(t)
    heat_advance = int(t_dict.get("qual_heat_advance") or 1)
    group_count = int(t_dict.get("qual_group_count") or 1)
    group_advance = int(t_dict.get("qual_group_advance") or heat_advance)
    has_heat_final = bool(t_dict.get("qual_heat_final", 0))

    await db.execute("UPDATE entries SET advanced=NULL WHERE tournament_id=?", (tid,))

    async with db.execute(
        "SELECT DISTINCT heat_no FROM ht_rounds WHERE tournament_id=? ORDER BY heat_no", (tid,)
    ) as cur:
        heat_nos = [r["heat_no"] for r in await cur.fetchall()]

    for hno in heat_nos:
        if has_heat_final:
            # 本戦進出者（ヒート決勝 上位 heat_advance 名）のみ
            advs = await _ht_get_heatfinal_advancers(tid, hno, heat_advance, db)
            ids = [a["entry_id"] for a in advs if a.get("is_advance") and a.get("entry_id")]
        else:
            advanced = await _ht_get_advanced(tid, hno, group_advance, db)
            ids = [a["entry_id"] for a in advanced if a["entry_id"]]
        for eid in ids:
            await db.execute("UPDATE entries SET advanced=1 WHERE id=?", (eid,))
    await db.commit()


@router.post("/{tid}/qualifying/none-rr/deciding/generate")
async def none_rr_deciding_generate(
    tid: int,
    position: int = Form(...),   # 1=1位決定戦, 2=2位決定戦, 3=3位決定戦
    db: aiosqlite.Connection = Depends(get_db),
):
    """即決勝総当たり: 同率解消のための決定戦ヒートを生成"""
    if position not in (1, 2, 3):
        return JSONResponse({"ok": False, "error": "position must be 1, 2, or 3"})

    # 現在のスタンディングを取得
    standings = await _calc_standings_rr(tid, db)

    # 対象ポジションの同率エントリーを抽出
    tied = [s for s in standings if s["rank"] == position]
    if len(tied) < 2:
        return JSONResponse({"ok": False, "error": f"{position}位同率の選手が2名未満です"})

    # 既存の決定戦ヒートを削除（再生成）
    async with db.execute(
        "SELECT id FROM heats WHERE tournament_id=? AND deciding_position=?", (tid, position)
    ) as cur:
        old_heats = [r["id"] for r in await cur.fetchall()]
    for hid in old_heats:
        await db.execute("DELETE FROM heat_results WHERE heat_lane_id IN (SELECT id FROM heat_lanes WHERE heat_id=?)", (hid,))
        await db.execute("DELETE FROM heat_lanes WHERE heat_id=?", (hid,))
        await db.execute("DELETE FROM heats WHERE id=?", (hid,))

    # 最大round_noを取得（決定戦は最終ラウンドとして追加）
    async with db.execute("SELECT COALESCE(MAX(round_no),0) as maxr FROM heats WHERE tournament_id=?", (tid,)) as cur:
        maxr = (await cur.fetchone())["maxr"]
    new_round = maxr + 1

    # 決定戦ヒートを生成（1ヒート）
    lane_count_row = await (await db.execute("SELECT lane_count FROM tournaments WHERE id=?", (tid,))).fetchone()
    lane_count = lane_count_row["lane_count"] if lane_count_row else 2

    await db.execute(
        "INSERT INTO heats (tournament_id, round_no, heat_no, group_no, deciding_position) VALUES (?,?,1,0,?)",
        (tid, new_round, position),
    )
    heat_id = (await (await db.execute("SELECT last_insert_rowid() as id")).fetchone())["id"]

    # レーンに対象選手を追加
    for slot, entry in enumerate(tied):
        lane_no = (slot % lane_count) + 1
        await db.execute(
            "INSERT INTO heat_lanes (heat_id, entry_id, lane_no) VALUES (?,?,?)",
            (heat_id, entry["entry_id"], lane_no),
        )

    await db.commit()
    return JSONResponse({"ok": True, "heat_id": heat_id, "position": position, "count": len(tied)})


@router.post("/{tid}/qualifying/none-rr/deciding/save")
async def none_rr_deciding_save(
    tid: int,
    heat_id: int = Form(...),
    entry_id: int = Form(...),
    win: int = Form(...),   # 1=勝, 0=負
    db: aiosqlite.Connection = Depends(get_db),
):
    """即決勝総当たり: 決定戦の勝敗保存"""
    # heat_lane_id を取得
    async with db.execute(
        "SELECT id FROM heat_lanes WHERE heat_id=? AND entry_id=?", (heat_id, entry_id)
    ) as cur:
        lane = await cur.fetchone()
    if not lane:
        return JSONResponse({"ok": False, "error": "lane not found"})
    lane_id = lane["id"]

    # 既存結果を確認
    async with db.execute("SELECT id FROM heat_results WHERE heat_lane_id=?", (lane_id,)) as cur:
        existing = await cur.fetchone()
    if existing:
        await db.execute("UPDATE heat_results SET win=? WHERE heat_lane_id=?", (win, lane_id))
    else:
        await db.execute(
            "INSERT INTO heat_results (heat_lane_id, win) VALUES (?,?)", (lane_id, win)
        )
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/{tid}/qualifying/none-rr/decide-rank")
async def none_rr_decide_rank(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """即決勝総当たり: 決定戦の順位を保存（プルダウン形式）"""
    form = await request.form()
    # フォーム: rank_{entry_id} = decided_rank
    saved = 0
    for key, val in form.items():
        if key.startswith("rank_") and val:
            try:
                eid = int(key[5:])
                rank = int(val)
                await db.execute(
                    "UPDATE entries SET none_rr_rank=? WHERE id=? AND tournament_id=?",
                    (rank, eid, tid),
                )
                saved += 1
            except (ValueError, TypeError):
                pass
    await db.commit()
    return JSONResponse({"ok": True, "saved": saved})


@router.post("/{tid}/qualifying/none-rr/decide-rank/reset")
async def none_rr_decide_rank_reset(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """即決勝総当たり: 決定戦の順位をリセット"""
    await db.execute("UPDATE entries SET none_rr_rank=NULL WHERE tournament_id=?", (tid,))
    await db.commit()
    return JSONResponse({"ok": True})

@router.post("/{tid}/qualifying/none-rr/confirm")
async def none_rr_confirm(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """即決勝総当たり: 結果を確定しトーナメントを完了状態にする"""
    standings = await _calc_standings_none_rr(tid, db)
    # 同率チェック: decided_mapが埋まっているか確認
    async with db.execute(
        "SELECT id as entry_id, none_rr_rank FROM entries WHERE tournament_id=? AND none_rr_rank IS NOT NULL",
        (tid,),
    ) as cur:
        decided_map = {r["entry_id"]: r["none_rr_rank"] for r in await cur.fetchall()}

    # 決定戦グループを再計算して未決定チェック
    processed_ranks = set()
    for pos in (1, 2, 3):
        if pos in processed_ranks:
            continue
        tied = [s for s in standings if s["rank"] == pos]
        if len(tied) >= 2:
            n = len(tied)
            max_decide = min(pos + n - 2, 3)
            decide_positions = list(range(pos, max_decide + 1))
            for dp in decide_positions:
                if not any(decided_map.get(st["entry_id"]) == dp for st in tied):
                    return JSONResponse({"ok": False, "error": f"{dp}位の決定戦が未完了です。"})
            for p in range(pos, pos + n):
                processed_ranks.add(p)
    # 全ヒート完了チェック（deciding除く）
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM heats WHERE tournament_id=? AND COALESCE(deciding_position,0)=0 AND status!='done'",
        (tid,),
    ) as cur:
        pending = (await cur.fetchone())["cnt"]
    if pending > 0:
        return JSONResponse({"ok": False, "error": f"未完了のヒートが{pending}件あります。"})

    # 最終順位を確定
    # 同率グループの「残り（未決定）」は decide_positions の末尾+1 を自動付与
    final_rank_map = {}
    processed_ranks2 = set()
    for pos in (1, 2, 3):
        if pos in processed_ranks2:
            continue
        tied = [s for s in standings if s["rank"] == pos]
        if len(tied) >= 2:
            n = len(tied)
            max_decide = min(pos + n - 2, 3)
            decide_positions = list(range(pos, max_decide + 1))
            # decided_map で決まった人
            decided_eids = set()
            for dp in decide_positions:
                for st in tied:
                    if decided_map.get(st["entry_id"]) == dp:
                        final_rank_map[st["entry_id"]] = dp
                        decided_eids.add(st["entry_id"])
            # 残りの人は max_decide+1 位
            next_rank = max_decide + 1
            for st in tied:
                if st["entry_id"] not in decided_eids:
                    final_rank_map[st["entry_id"]] = next_rank
            for p in range(pos, pos + n):
                processed_ranks2.add(p)
        else:
            for st in tied:
                final_rank_map[st["entry_id"]] = pos
            processed_ranks2.add(pos)

    # 4位以下（decided_map未設定 & final_rank_map未設定）
    for st in standings:
        eid = st["entry_id"]
        if eid not in final_rank_map:
            final_rank_map[eid] = decided_map.get(eid, st["rank"])

    for st in standings:
        eid = st["entry_id"]
        final_rank = final_rank_map.get(eid, st["rank"])
        adv = 1 if final_rank <= 3 else 0
        await db.execute(
            "UPDATE entries SET advanced=?, none_rr_rank=? WHERE id=? AND tournament_id=?",
            (adv, final_rank if final_rank <= 3 else None, eid, tid),
        )
    await db.execute("UPDATE tournaments SET status='complete' WHERE id=?", (tid,))
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/{tid}/qualifying/none-rr/unconfirm")
async def none_rr_unconfirm(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """即決勝総当たり: 確定を取り消す（再編集可能にする）"""
    await db.execute("UPDATE tournaments SET status='qualifying' WHERE id=?", (tid,))
    await db.execute(
        "UPDATE entries SET none_rr_rank=NULL WHERE tournament_id=?", (tid,)
    )
    await db.commit()

    # 参加者向けHTML配信（自動更新）
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass
    return JSONResponse({"ok": True})


# ══════════════════════════════════════════════════════════════════════
#  order（並び順（ポイント制））予選管理
#    - 出走スキャンで待機列(order_queue)へ先着順追加
#    - スキャン都度、確定できる分を自動で組(heats/heat_lanes)に切り出す
#    - 組は必ず2〜3人（1人組は禁止）。3人優先・端数2人。
#    - 結果入力・ポイント付与・積算順位は既存pointロジックを流用
#    - 区切り: free（フリー走行制）/ round（ラウンド制）
# ══════════════════════════════════════════════════════════════════════

def _order_chunk_sizes(q: int, lane_count: int, force_all: bool = False) -> list[int]:
    """待機列 q 人を、1人組を作らずに切り出すサイズ列を返す（3優先・端数2）。
    lane_count=2 のときは常に2人組のみ。
    force_all=False（スキャン都度）:
        3人組を優先。残り2人は次の人を待つ。ただし残り4人は2+2で切り出す。
        → q=2: 待機 / q=3: [3] / q=4: [2,2] / q=5: [3]残2 / q=6: [3,3] / q=7: [3,2,2] ...
    force_all=True（終了時）:
        端数2も切り出す。全員を可能な限り組む。
    q<2 のときは常に空。
    """
    if q < 2:
        return []
    if lane_count < 3:
        return [2] * (q // 2)
    # 3レーン共通ロジック（force_all の差は q=2 のみ）
    sizes = []
    remaining = q
    while remaining >= 2:
        if remaining == 2:
            if force_all:
                sizes.append(2)
                remaining -= 2
            else:
                break  # 次の人を待つ
        elif remaining == 4:
            sizes.append(2)
            sizes.append(2)
            remaining -= 4
        elif remaining >= 3:
            sizes.append(3)
            remaining -= 3
    return sizes


async def _order_current_round(tid: int, t, db) -> int:
    """現在進行中のラウンド番号を返す。
    free: 常に1。
    round: tournaments.order_current_round カラムの値。
    """
    mode = dict(t).get("order_round_mode") or "free"
    if mode != "round":
        return 1
    return dict(t).get("order_current_round") or 1


async def _order_queue_pending(tid: int, round_no: int, db) -> list[dict]:
    """指定ラウンドの未消化の待機列（先着順）を返す。"""
    async with db.execute(
        """SELECT oq.id, oq.entry_id, oq.scan_seq, r.name, COALESCE(r.yomi,'') AS yomi
           FROM order_queue oq
           JOIN entries e ON e.id=oq.entry_id
           JOIN racers r ON r.id=e.racer_id
           WHERE oq.tournament_id=? AND oq.round_no=? AND oq.consumed=0
           ORDER BY oq.scan_seq""",
        (tid, round_no),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _order_create_heat(tid: int, round_no: int, entry_ids: list[int], db) -> int:
    """order用の1組(heat)を作成。出走確定順を heat_no に採番、round_no を記録。
    レーン割当は来た順（scan順）に 1,2,3。
    """
    race_no = await _next_race_no(tid, db)
    cur = await db.execute(
        "INSERT INTO heats (tournament_id, heat_no, group_no, round_no, status) "
        "VALUES (?,?,?,?, 'pending')",
        (tid, race_no, 0, round_no),
    )
    heat_id = cur.lastrowid
    for lane_no, eid in enumerate(entry_ids, 1):
        await db.execute(
            "INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)",
            (heat_id, lane_no, eid),
        )
        # 待機列側に heat_id を記録（あれば）
        await db.execute(
            "UPDATE order_queue SET heat_id=? WHERE tournament_id=? AND entry_id=? "
            "AND round_no=? AND consumed=1 AND heat_id IS NULL",
            (heat_id, tid, eid, round_no),
        )
    return heat_id


async def _order_make_groups(tid: int, t, round_no: int, db, force_all: bool = False) -> int:
    """待機列の未消化分から、組める分を heats/heat_lanes に切り出す。
    通常（force_all=False）: 3人溜まったら3人組確定。2人以下は待機。
    force_all=True（終了・手動確定時）: 端数2も含め可能な限り切り出す。
    戻り値: 新規作成した組数。
    """
    lane_count = dict(t).get("lane_count") or 2
    pending = await _order_queue_pending(tid, round_no, db)
    q = len(pending)
    made = 0

    if q < 2 and not force_all:
        return 0

    sizes = _order_chunk_sizes(q, lane_count, force_all=force_all)
    idx = 0
    for size in sizes:
        members = pending[idx:idx + size]
        idx += size
        ids = [m["id"] for m in members]
        ph = ",".join("?" * len(ids))
        await db.execute(
            f"UPDATE order_queue SET consumed=1 WHERE id IN ({ph})", ids
        )
        await _order_create_heat(tid, round_no, [m["entry_id"] for m in members], db)
        made += 1

    # force_all時の残り1人合流処理（直前未走組へ）
    if force_all:
        pending_after = pending[idx:]
        if len(pending_after) == 1 and lane_count >= 3:
            last_one = pending_after[0]
            async with db.execute(
                """SELECT h.id, COUNT(hl.id) AS n
                   FROM heats h
                   LEFT JOIN heat_lanes hl ON hl.heat_id=h.id
                   WHERE h.tournament_id=? AND h.round_no=? AND h.status!='done'
                   GROUP BY h.id HAVING n < 3
                   ORDER BY h.id DESC LIMIT 1""",
                (tid, round_no),
            ) as cur:
                target = await cur.fetchone()
            if target:
                async with db.execute(
                    "SELECT COALESCE(MAX(lane_no),0) AS mx FROM heat_lanes WHERE heat_id=?",
                    (target["id"],),
                ) as cur:
                    mx = (await cur.fetchone())["mx"] or 0
                await db.execute(
                    "INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)",
                    (target["id"], mx + 1, last_one["entry_id"]),
                )
                await db.execute(
                    "UPDATE order_queue SET consumed=1, heat_id=? WHERE id=?",
                    (target["id"], last_one["id"]),
                )

    await db.commit()
    return made



@router.post("/{tid}/qualifying/order/scan")
async def order_scan(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """出走スキャン。10桁コードまたは連番(seq_no)を受け取り、
    該当する本エントリーを待機列へ先着順追加 → 自動で組を切り出す。"""
    from fastapi.responses import JSONResponse
    from app.services.barcode import parse_code

    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order":
        return JSONResponse({"ok": False, "message": "対象外のレースです"})
    if dict(t).get("order_status") == "closed":
        return JSONResponse({"ok": False, "message": "予選は終了しています"})

    form = await request.form()
    raw_eid = (form.get("entry_id") or "").strip()
    raw = (form.get("code") or "").strip()

    if raw_eid:
        # entry_id 直指定（未スキャン一覧のクリックなど）。pre_seq_no が無くても確実に解決できる。
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
    else:
        if not raw:
            return JSONResponse({"ok": False, "message": "コードが空です"})

        # コード解析：10桁ならparse_code、数字短い場合は連番とみなす
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

        # 連番から本エントリーを特定（受付スキャンで pre_seq_no が記録済み）
        async with db.execute(
            """SELECT e.id AS entry_id, r.name
               FROM entries e JOIN racers r ON r.id=e.racer_id
               WHERE e.tournament_id=? AND e.pre_seq_no=? AND e.status='active'""",
            (tid, seq_no),
        ) as cur:
            ent = await cur.fetchone()
        if not ent:
            return JSONResponse({"ok": False, "message": f"連番{seq_no:04d} は未受付です"})

    entry_id = ent["entry_id"]
    mode = (dict(t).get("order_round_mode") or "free")
    round_no = await _order_current_round(tid, t, db)

    if mode == "round":
        # 【ラウンド制】同一ラウンドで既に待機 or 出走済みなら弾く（従来どおり）
        async with db.execute(
            "SELECT consumed FROM order_queue WHERE tournament_id=? AND entry_id=? AND round_no=?",
            (tid, entry_id, round_no),
        ) as cur:
            exist = await cur.fetchone()
        if exist:
            msg = "既に出走済みです" if exist["consumed"] else "既に待機列にいます"
            return JSONResponse({"ok": False, "message": f"{ent['name']}：{msg}"})
    else:
        # 【フリー走行制】再スキャン（積算）を許可する。
        #   ・待機列に未消化(consumed=0)の行があれば二重並び不可
        #   ・未完了(未done)の組に在籍中なら不可（前の走行が終わるまで）
        #   ・制限ありなら、組確定(consumed=1)済み回数が上限に達していれば不可
        async with db.execute(
            "SELECT 1 FROM order_queue WHERE tournament_id=? AND entry_id=? AND round_no=? AND consumed=0 LIMIT 1",
            (tid, entry_id, round_no),
        ) as cur:
            if await cur.fetchone():
                return JSONResponse({"ok": False, "message": f"{ent['name']}：既に待機列にいます"})
        async with db.execute(
            """SELECT 1 FROM heat_lanes hl JOIN heats h ON h.id=hl.heat_id
               WHERE h.tournament_id=? AND hl.entry_id=? AND h.round_no=? AND h.status!='done' LIMIT 1""",
            (tid, entry_id, round_no),
        ) as cur:
            if await cur.fetchone():
                return JSONResponse({"ok": False, "message": f"{ent['name']}：前の走行が未完了です"})
        max_runs = dict(t).get("order_free_max_runs") or 0
        if max_runs and max_runs > 0:
            async with db.execute(
                "SELECT COUNT(*) AS n FROM order_queue "
                "WHERE tournament_id=? AND entry_id=? AND round_no=? AND consumed=1",
                (tid, entry_id, round_no),
            ) as cur:
                done_runs = (await cur.fetchone())["n"] or 0
            if done_runs >= max_runs:
                return JSONResponse({"ok": False, "message": f"{ent['name']}：規定回数（{max_runs}回）走行済みです"})

    # 待機列へ追加
    async with db.execute(
        "SELECT COALESCE(MAX(scan_seq),0) AS mx FROM order_queue WHERE tournament_id=? AND round_no=?",
        (tid, round_no),
    ) as cur:
        next_seq = ((await cur.fetchone())["mx"] or 0) + 1
    await db.execute(
        "INSERT INTO order_queue (tournament_id, entry_id, scan_seq, round_no, consumed) "
        "VALUES (?,?,?,?,0)",
        (tid, entry_id, next_seq, round_no),
    )
    await db.commit()

    made = 0
    remaining = None
    if mode == "round":
        # 【ラウンド制】残り人数で自動確定ゲート（従来どおり）
        async with db.execute(
            "SELECT COUNT(*) AS n FROM entries WHERE tournament_id=? AND status='active'", (tid,)
        ) as cur:
            total_entries = (await cur.fetchone())["n"] or 0
        async with db.execute(
            "SELECT COUNT(*) AS n FROM order_queue WHERE tournament_id=? AND round_no=?",
            (tid, round_no),
        ) as cur:
            scanned = (await cur.fetchone())["n"] or 0
        remaining = total_entries - scanned
        # 残り10人以下なら自動確定せず待機列に溜める（管理者が手動で2/3人組を確定）
        if remaining > 10:
            made = await _order_make_groups(tid, t, round_no, db)
    else:
        # 【フリー走行制】残り人数ゲートなし。3人溜まれば常に自動確定。
        made = await _order_make_groups(tid, t, round_no, db)

    pending = await _order_queue_pending(tid, round_no, db)
    return JSONResponse({
        "ok": True,
        "message": f"{ent['name']} を追加しました",
        "made_groups": made,
        "queue_count": len(pending),
        "remaining": remaining,
        "reload": True,
    })


async def _order_maybe_advance_round(tid: int, t, round_no: int, db) -> bool:
    """ラウンド制：当該ラウンドで本エントリー全員がスキャン済み（待機列入り）なら、
    残った端数も組にして当ラウンドを締める（1人組禁止）。"""
    async with db.execute(
        "SELECT COUNT(*) AS n FROM entries WHERE tournament_id=? AND status='active'",
        (tid,),
    ) as cur:
        total = (await cur.fetchone())["n"] or 0
    async with db.execute(
        "SELECT COUNT(*) AS n FROM order_queue WHERE tournament_id=? AND round_no=?",
        (tid, round_no),
    ) as cur:
        scanned = (await cur.fetchone())["n"] or 0
    if total > 0 and scanned >= total:
        await _order_make_groups(tid, t, round_no, db, force_all=True)
        return True
    return False


@router.post("/{tid}/qualifying/order/make-group")
async def order_make_group(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """手動で待機列から組を確定する。
    form: size=2 または size=3 で人数指定。未指定時は force_all で自動。
    残り10人以下のとき管理者が人数を選んで確定するために使う。
    """
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order":
        return JSONResponse({"ok": False})
    round_no = await _order_current_round(tid, t, db)
    form = await request.form()
    size_raw = form.get("size", "")

    if size_raw in ("2", "3"):
        # 人数指定：待機列の先頭から size 人だけ切り出して1組確定
        size = int(size_raw)
        # レーン数を超える人数は不可（2レーンのレースに3人組を作らせない）
        lane_count = dict(t).get("lane_count") or 2
        if size > lane_count:
            return JSONResponse({"ok": False, "message": f"このレースは最大{lane_count}人組です"})
        pending = await _order_queue_pending(tid, round_no, db)
        if len(pending) < size:
            return JSONResponse({"ok": False, "message": f"待機列が{size}人未満です"})
        members = pending[:size]
        ids = [m["id"] for m in members]
        ph = ",".join("?" * len(ids))
        await db.execute(f"UPDATE order_queue SET consumed=1 WHERE id IN ({ph})", ids)
        await _order_create_heat(tid, round_no, [m["entry_id"] for m in members], db)
        await db.commit()
        return JSONResponse({"ok": True, "made_groups": 1})
    else:
        # 人数未指定：force_all で残り全部を自動処理
        made = await _order_make_groups(tid, t, round_no, db, force_all=True)
        return JSONResponse({"ok": True, "made_groups": made})


@router.post("/{tid}/qualifying/order/round-end")
async def order_round_end(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """ラウンド終了（ラウンド制）/ 予選終了（フリー制）。
    form: mode='round'（このラウンドを締めて次へ） / mode='close'（予選全体を終了）"""
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order":
        return JSONResponse({"ok": False})
    form = await request.form()
    mode = form.get("mode") or "round"
    round_no = await _order_current_round(tid, t, db)

    # 残った待機列を組にする（1人組禁止のまま掃き出し）
    await _order_make_groups(tid, t, round_no, db, force_all=True)

    if mode == "close" or (dict(t).get("order_round_mode") or "free") == "free":
        await db.execute("UPDATE tournaments SET order_status='closed' WHERE id=?", (tid,))
        await db.commit()
        return JSONResponse({"ok": True, "closed": True})

    # ラウンド制：規定回数に達していれば締め、未達なら次ラウンド開放
    round_count = dict(t).get("order_round_count") or 1
    if round_no >= round_count:
        await db.execute("UPDATE tournaments SET order_status='closed' WHERE id=?", (tid,))
        await db.commit()
        return JSONResponse({"ok": True, "closed": True})
    await db.commit()
    await db.execute(
        "UPDATE tournaments SET order_current_round=? WHERE id=?",
        (round_no + 1, tid)
    )
    await db.commit()
    return JSONResponse({"ok": True, "next_round": round_no + 1})


@router.post("/{tid}/qualifying/order/add-round")
async def order_add_round(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """ラウンド制：規定回数を1つ増やして『もう1回』追加する。
    予選終了状態なら再開する。"""
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order":
        return JSONResponse({"ok": False})
    new_count = (dict(t).get("order_round_count") or 1) + 1
    await db.execute(
        "UPDATE tournaments SET order_round_count=?, order_status=NULL WHERE id=?",
        (new_count, tid),
    )
    await db.commit()
    return JSONResponse({"ok": True, "round_count": new_count})


@router.post("/{tid}/qualifying/order/reopen")
async def order_reopen(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """予選終了状態(order_status='closed')を解除して予選中に戻す。
    入力済みの結果・ラウンド数・現在ラウンドはすべて維持する。"""
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order":
        return JSONResponse({"ok": False})
    await db.execute(
        "UPDATE tournaments SET order_status=NULL WHERE id=?", (tid,)
    )
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/{tid}/qualifying/order/reset")
async def order_reset(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """order予選を全リセット（待機列・組・結果を全削除）。"""
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT id FROM heats WHERE tournament_id=?", (tid,)) as cur:
        hids = [r["id"] for r in await cur.fetchall()]
    if hids:
        ph = ",".join("?" * len(hids))
        await db.execute(
            f"DELETE FROM heat_results WHERE heat_lane_id IN "
            f"(SELECT id FROM heat_lanes WHERE heat_id IN ({ph}))", hids
        )
        await db.execute(f"DELETE FROM heat_lanes WHERE heat_id IN ({ph})", hids)
    await db.execute("DELETE FROM heats WHERE tournament_id=?", (tid,))
    await db.execute("DELETE FROM order_queue WHERE tournament_id=?", (tid,))
    await db.execute(
        "UPDATE tournaments SET order_status=NULL, order_current_round=1 WHERE id=?", (tid,)
    )
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/{tid}/qualifying/order/cancel-queue")
async def order_cancel_queue(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """待機列にいるレーサー1人の走行受付を取り消す。
    対象は未消化(consumed=0)の order_queue 行のみ。組確定済みは対象外。
    取り消した本人はラウンド制なら自動的に「このラウンド未スキャン（再受付可能）」へ戻る
    （order_queue 行を削除するため）。"""
    from fastapi.responses import JSONResponse

    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order":
        return JSONResponse({"ok": False, "message": "対象外のレースです"})
    if dict(t).get("order_status") == "closed":
        return JSONResponse({"ok": False, "message": "予選は終了しています"})

    form = await request.form()
    raw_qid = (form.get("queue_id") or "").strip()
    if not raw_qid.isdigit():
        return JSONResponse({"ok": False, "message": "queue_id が不正です"})
    queue_id = int(raw_qid)

    # 対象が「待機列・未消化」であることを確認（組確定済み consumed=1 は取り消し不可）
    async with db.execute(
        """SELECT oq.id, r.name
           FROM order_queue oq
           JOIN entries e ON e.id=oq.entry_id
           JOIN racers r ON r.id=e.racer_id
           WHERE oq.id=? AND oq.tournament_id=? AND oq.consumed=0""",
        (queue_id, tid),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return JSONResponse({"ok": False, "message": "対象が待機列にいません（既に組確定済みの可能性）"})

    await db.execute("DELETE FROM order_queue WHERE id=?", (queue_id,))
    await db.commit()

    round_no = await _order_current_round(tid, t, db)
    pending = await _order_queue_pending(tid, round_no, db)
    return JSONResponse({
        "ok": True,
        "message": f"{row['name']} の受付を取り消しました",
        "queue_count": len(pending),
        "reload": True,
    })


@router.post("/{tid}/qualifying/order/cancel-member")
async def order_cancel_member(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """結果未確定の組からレーサー1人の走行受付を取り消し、
    同ラウンドの『未確定の組＋待機列』を scan_seq 順で1列に並べ直して前詰め再編成する。

    仕様（案A）:
      ・対象：取消対象と同ラウンドの未確定組(status!='done')のメンバー全員 ＋ 待機列(consumed=0)
      ・確定済み(done)の組は一切動かさない
      ・取消本人は order_queue 行を削除（→ ラウンド制では「未スキャン＝再受付可能」に戻る）
      ・残りを scan_seq 昇順で1プール化し、先頭から (3レーン=3人 / 2レーン=2人) ちょうど
        組める分だけ組を作る。端数(1〜2人)は待機列に残す（1人組は発生しない）
      ・未確定組の heat_no は詰め直した順で振り直す（確定済みの heat_no は維持）
    """
    from fastapi.responses import JSONResponse

    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order":
        return JSONResponse({"ok": False, "message": "対象外のレースです"})
    if dict(t).get("order_status") == "closed":
        return JSONResponse({"ok": False, "message": "予選は終了しています"})

    form = await request.form()
    raw_hid = (form.get("heat_id") or "").strip()
    raw_eid = (form.get("entry_id") or "").strip()
    if not raw_hid.isdigit() or not raw_eid.isdigit():
        return JSONResponse({"ok": False, "message": "パラメータが不正です"})
    heat_id = int(raw_hid)
    entry_id = int(raw_eid)

    # 対象ヒート：当レースのもので、結果未確定であること
    async with db.execute(
        "SELECT id, round_no, status FROM heats WHERE id=? AND tournament_id=?",
        (heat_id, tid),
    ) as cur:
        heat = await cur.fetchone()
    if not heat:
        return JSONResponse({"ok": False, "message": "組が見つかりません"})
    if heat["status"] == "done":
        return JSONResponse({
            "ok": False,
            "message": "この組は結果確定済みです。先に「結果を取消」してください",
        })
    round_no = heat["round_no"]

    # 取消対象レーサー名（この組に在籍していること）
    async with db.execute(
        """SELECT r.name
           FROM heat_lanes hl
           JOIN entries e ON e.id=hl.entry_id
           JOIN racers r ON r.id=e.racer_id
           WHERE hl.heat_id=? AND hl.entry_id=?""",
        (heat_id, entry_id),
    ) as cur:
        target = await cur.fetchone()
    if not target:
        return JSONResponse({"ok": False, "message": "対象レーサーがこの組にいません"})

    # 1) 取消本人を order_queue から削除（このラウンド・この組ぶんの行）
    await db.execute(
        "DELETE FROM order_queue WHERE tournament_id=? AND entry_id=? AND round_no=? AND heat_id=?",
        (tid, entry_id, round_no, heat_id),
    )

    # 2) 同ラウンドの「未確定の組」一覧を取得（これらをバラして再編成する）
    async with db.execute(
        "SELECT id FROM heats WHERE tournament_id=? AND round_no=? AND status!='done'",
        (tid, round_no),
    ) as cur:
        non_done_heat_ids = [r["id"] for r in await cur.fetchall()]

    # 3) プール作成：scan_seq 昇順で
    #    ・待機列(consumed=0) の行
    #    ・未確定組に属する(consumed=1 かつ heat_id が non_done) の行
    #    （取消本人は 1) で削除済みなので自動的に含まれない）
    if non_done_heat_ids:
        ph_nd = ",".join("?" * len(non_done_heat_ids))
        pool_sql = (
            "SELECT oq.id AS queue_id, oq.entry_id "
            "FROM order_queue oq "
            "WHERE oq.tournament_id=? AND oq.round_no=? "
            f"AND (oq.consumed=0 OR oq.heat_id IN ({ph_nd})) "
            "ORDER BY oq.scan_seq"
        )
        pool_args = [tid, round_no, *non_done_heat_ids]
    else:
        pool_sql = (
            "SELECT oq.id AS queue_id, oq.entry_id "
            "FROM order_queue oq "
            "WHERE oq.tournament_id=? AND oq.round_no=? AND oq.consumed=0 "
            "ORDER BY oq.scan_seq"
        )
        pool_args = [tid, round_no]

    async with db.execute(pool_sql, pool_args) as cur:
        pool = [dict(r) for r in await cur.fetchall()]

    # 4) 未確定の組（heats / heat_lanes / 念のため heat_results）を解体
    if non_done_heat_ids:
        ph_nd = ",".join("?" * len(non_done_heat_ids))
        await db.execute(
            f"DELETE FROM heat_results WHERE heat_lane_id IN "
            f"(SELECT id FROM heat_lanes WHERE heat_id IN ({ph_nd}))",
            non_done_heat_ids,
        )
        await db.execute(f"DELETE FROM heat_lanes WHERE heat_id IN ({ph_nd})", non_done_heat_ids)
        await db.execute(f"DELETE FROM heats WHERE id IN ({ph_nd})", non_done_heat_ids)

    # 5) プール行をいったん全て待機列状態へ戻す（consumed=0・heat_id解除）
    if pool:
        ph_q = ",".join("?" * len(pool))
        await db.execute(
            f"UPDATE order_queue SET consumed=0, heat_id=NULL WHERE id IN ({ph_q})",
            [p["queue_id"] for p in pool],
        )

    # 6) 先頭から「ちょうど組める分」だけ組を作る（案A：端数は待機列に残す）
    lane_count = dict(t).get("lane_count") or 2
    group_size = 3 if lane_count >= 3 else 2
    n_groups = len(pool) // group_size

    made = 0
    for g in range(n_groups):
        members = pool[g * group_size:(g + 1) * group_size]
        # 新しい組(heat)を作成（heat_no は末尾採番＝確定済みの後ろに付く）
        race_no = await _next_race_no(tid, db)
        cur = await db.execute(
            "INSERT INTO heats (tournament_id, heat_no, group_no, round_no, status) "
            "VALUES (?,?,?,?, 'pending')",
            (tid, race_no, 0, round_no),
        )
        new_heat_id = cur.lastrowid
        for lane_no, m in enumerate(members, 1):
            await db.execute(
                "INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)",
                (new_heat_id, lane_no, m["entry_id"]),
            )
            await db.execute(
                "UPDATE order_queue SET consumed=1, heat_id=? WHERE id=?",
                (new_heat_id, m["queue_id"]),
            )
        made += 1

    await db.commit()

    leftover = len(pool) - n_groups * group_size
    pending = await _order_queue_pending(tid, round_no, db)
    return JSONResponse({
        "ok": True,
        "message": f"{target['name']} を取り消し、未確定の組と待機列を再編成しました"
                   f"（{made}組・待機{leftover}名）",
        "queue_count": len(pending),
        "reload": True,
    })


# ============================================================================
# 並び順（勝ち抜け）  order_winner
#   多段予選（1次/2次/3次…）。各段階ごとに win_target / max_runs / advance_count。
#   1組から1着は最大1人（0人＝全員COもあり）。規定勝利数で通過、上限走行で敗退。
#   通過者が advance_count に達した瞬間に段階を強制終了。
#   ここでは 1-a（スキャン受付・組確定）までを実装する。
# ============================================================================

async def _ow_current_stage(tid: int, t, db) -> int:
    """進行中の段階番号を返す（tournaments.order_winner_current_stage、既定1）。"""
    return dict(t).get("order_winner_current_stage") or 1


async def _ow_stage_row(tid: int, stage_no: int, db) -> dict | None:
    """指定段階の設定行（order_winner_stages）を返す。"""
    async with db.execute(
        """SELECT stage_no, win_target, max_runs, advance_count, status
           FROM order_winner_stages WHERE tournament_id=? AND stage_no=?""",
        (tid, stage_no),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def _ow_get_racer(tid: int, stage_no: int, entry_id: int, db) -> dict | None:
    """段階×レーサーの状態行（order_winner_racers）を返す。無ければ None。"""
    async with db.execute(
        """SELECT id, wins, runs, status, passed_seq
           FROM order_winner_racers
           WHERE tournament_id=? AND stage_no=? AND entry_id=?""",
        (tid, stage_no, entry_id),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def _ow_ensure_racer(tid: int, stage_no: int, entry_id: int, db) -> dict:
    """段階×レーサーの状態行を取得。無ければ racing / wins0 / runs0 で作成して返す。"""
    row = await _ow_get_racer(tid, stage_no, entry_id, db)
    if row:
        return row
    await db.execute(
        """INSERT INTO order_winner_racers
           (tournament_id, stage_no, entry_id, wins, runs, status)
           VALUES (?,?,?,0,0,'racing')""",
        (tid, stage_no, entry_id),
    )
    return await _ow_get_racer(tid, stage_no, entry_id, db)


async def _ow_stage_eligible(tid: int, stage_no: int, entry_id: int, db) -> bool:
    """そのレーサーが当該段階の参加対象か。
    1次（stage_no=1）: 全 active エントリー。
    2次以降: 直前段階で status='passed' の者のみ。"""
    if stage_no <= 1:
        async with db.execute(
            "SELECT 1 FROM entries WHERE tournament_id=? AND id=? AND status='active' LIMIT 1",
            (tid, entry_id),
        ) as cur:
            return (await cur.fetchone()) is not None
    async with db.execute(
        """SELECT 1 FROM order_winner_racers
           WHERE tournament_id=? AND stage_no=? AND entry_id=? AND status='passed' LIMIT 1""",
        (tid, stage_no - 1, entry_id),
    ) as cur:
        return (await cur.fetchone()) is not None


async def _ow_passed_count(tid: int, stage_no: int, db) -> int:
    """当該段階で通過（status='passed'）済みの人数。"""
    async with db.execute(
        "SELECT COUNT(*) AS n FROM order_winner_racers "
        "WHERE tournament_id=? AND stage_no=? AND status='passed'",
        (tid, stage_no),
    ) as cur:
        return (await cur.fetchone())["n"] or 0


async def _ow_queue_pending(tid: int, stage_no: int, db) -> list[dict]:
    """当該段階の未消化の待機列（先着順）。order_queue.stage_no で区別する。"""
    async with db.execute(
        """SELECT oq.id, oq.entry_id, oq.scan_seq, r.name, COALESCE(r.yomi,'') AS yomi
           FROM order_queue oq
           JOIN entries e ON e.id=oq.entry_id
           JOIN racers r ON r.id=e.racer_id
           WHERE oq.tournament_id=? AND oq.stage_no=? AND oq.consumed=0
           ORDER BY oq.scan_seq""",
        (tid, stage_no),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _ow_create_heat(tid: int, stage_no: int, entry_ids: list[int], db) -> int:
    """勝ち抜け用の1組(heat)を作成。round_no に stage_no を流用して段階を記録。
    レーン割当は scan 順に 1,2,3。組確定した各レーサーの runs を +1 する。"""
    race_no = await _next_race_no(tid, db)
    cur = await db.execute(
        "INSERT INTO heats (tournament_id, heat_no, group_no, round_no, status) "
        "VALUES (?,?,?,?, 'pending')",
        (tid, race_no, 0, stage_no),
    )
    heat_id = cur.lastrowid
    for lane_no, eid in enumerate(entry_ids, 1):
        await db.execute(
            "INSERT INTO heat_lanes (heat_id, lane_no, entry_id) VALUES (?,?,?)",
            (heat_id, lane_no, eid),
        )
        await db.execute(
            "UPDATE order_queue SET heat_id=? WHERE tournament_id=? AND entry_id=? "
            "AND stage_no=? AND consumed=1 AND heat_id IS NULL",
            (heat_id, tid, eid, stage_no),
        )
        # runs を +1（この組で1回走る）。状態行が無ければ作る。
        await _ow_ensure_racer(tid, stage_no, eid, db)
        await db.execute(
            "UPDATE order_winner_racers SET runs = runs + 1 "
            "WHERE tournament_id=? AND stage_no=? AND entry_id=?",
            (tid, stage_no, eid),
        )
    return heat_id


async def _ow_make_groups(tid: int, t, stage_no: int, db, force_all: bool = False) -> int:
    """待機列の未消化分から組を切り出す。
    自動（force_all=False）: 残り5人超のときのみ 3人優先・端数2 で切り出す（残り5人以下は手動へ）。
    手動/終了（force_all=True）: 端数含め可能な限り切り出す（1人残りは呼び出し側の方針に従う）。
    戻り値: 新規作成した組数。
    """
    lane_count = dict(t).get("lane_count") or 3
    pending = await _ow_queue_pending(tid, stage_no, db)
    q = len(pending)
    if q < 2:
        return 0
    # 勝ち抜けの自動確定ゲート：残り5人以下は自動で組まない（手動に委ねる）
    if not force_all and q <= 5:
        return 0

    sizes = _order_chunk_sizes(q, lane_count, force_all=force_all)
    idx = 0
    made = 0
    for size in sizes:
        members = pending[idx:idx + size]
        idx += size
        ids = [m["id"] for m in members]
        ph = ",".join("?" * len(ids))
        await db.execute(f"UPDATE order_queue SET consumed=1 WHERE id IN ({ph})", ids)
        await _ow_create_heat(tid, stage_no, [m["entry_id"] for m in members], db)
        made += 1
    return made


@router.post("/{tid}/qualifying/order-winner/scan")
async def order_winner_scan(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """勝ち抜け：出走スキャン。10桁コード / 連番 / entry_id 直指定を受け付け、
    当該段階の待機列へ先着順追加 → 自動で組を切り出す（残り5人超のとき）。"""
    from fastapi.responses import JSONResponse
    from app.services.barcode import parse_code

    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order_winner":
        return JSONResponse({"ok": False, "message": "対象外のレースです"})

    stage_no = await _ow_current_stage(tid, t, db)
    stage = await _ow_stage_row(tid, stage_no, db)
    if not stage:
        return JSONResponse({"ok": False, "message": "段階設定が見つかりません"})
    if stage.get("status") == "closed":
        return JSONResponse({"ok": False, "message": f"{stage_no}次予選は終了しています"})

    form = await request.form()
    raw_eid = (form.get("entry_id") or "").strip()
    raw = (form.get("code") or "").strip()

    # ---- エントリー特定（既存 order/scan と同じ解決ロジック）----
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
    else:
        if not raw:
            return JSONResponse({"ok": False, "message": "コードが空です"})
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

    entry_id = ent["entry_id"]

    # ---- 参加資格チェック（段階対象か／通過・敗退済みでないか）----
    if not await _ow_stage_eligible(tid, stage_no, entry_id, db):
        if stage_no <= 1:
            return JSONResponse({"ok": False, "message": f"{ent['name']}：エントリーされていません"})
        return JSONResponse({"ok": False, "message": f"{ent['name']}：前の予選を通過していません"})

    rc = await _ow_get_racer(tid, stage_no, entry_id, db)
    if rc:
        if rc["status"] == "passed":
            return JSONResponse({"ok": False, "message": f"{ent['name']}：既に通過しています"})
        if rc["status"] == "eliminated":
            return JSONResponse({"ok": False, "message": f"{ent['name']}：走行回数を使い切っています（敗退）"})
        if (rc["runs"] or 0) >= (stage["max_runs"] or 0):
            return JSONResponse({"ok": False, "message": f"{ent['name']}：規定回数（{stage['max_runs']}回）走行済みです"})

    # ---- 二重並びチェック（当該段階で未消化の待機列にいる / 未完了の組にいる）----
    async with db.execute(
        "SELECT 1 FROM order_queue WHERE tournament_id=? AND entry_id=? AND stage_no=? AND consumed=0 LIMIT 1",
        (tid, entry_id, stage_no),
    ) as cur:
        if await cur.fetchone():
            return JSONResponse({"ok": False, "message": f"{ent['name']}：既に待機列にいます"})
    async with db.execute(
        """SELECT 1 FROM heat_lanes hl JOIN heats h ON h.id=hl.heat_id
           WHERE h.tournament_id=? AND hl.entry_id=? AND h.round_no=? AND h.status!='done' LIMIT 1""",
        (tid, entry_id, stage_no),
    ) as cur:
        if await cur.fetchone():
            return JSONResponse({"ok": False, "message": f"{ent['name']}：前の走行が未完了です"})

    # ---- 状態行を用意（初回スキャンで作成）し、待機列へ追加 ----
    await _ow_ensure_racer(tid, stage_no, entry_id, db)
    async with db.execute(
        "SELECT COALESCE(MAX(scan_seq),0) AS mx FROM order_queue WHERE tournament_id=? AND stage_no=?",
        (tid, stage_no),
    ) as cur:
        next_seq = ((await cur.fetchone())["mx"] or 0) + 1
    await db.execute(
        "INSERT INTO order_queue (tournament_id, entry_id, scan_seq, round_no, stage_no, consumed) "
        "VALUES (?,?,?,?,?,0)",
        (tid, entry_id, next_seq, stage_no, stage_no),
    )
    await db.commit()

    # ---- 自動組確定（残り5人超のときのみ）----
    made = await _ow_make_groups(tid, t, stage_no, db)
    await db.commit()

    pending = await _ow_queue_pending(tid, stage_no, db)
    return JSONResponse({
        "ok": True,
        "message": f"{ent['name']} を追加しました",
        "made_groups": made,
        "queue_count": len(pending),
        "stage_no": stage_no,
        "reload": True,
    })


@router.post("/{tid}/qualifying/order-winner/make-group")
async def order_winner_make_group(tid: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """勝ち抜け：手動で「今の待機列で組む」。残り5人以下でも端数含め切り出す。
    1人だけ残る場合は、原則2/3人で割り切れるよう手前で調整されるが、
    やむを得ない場合は単走（1人組）を許可する（force_single=1 のとき）。"""
    from fastapi.responses import JSONResponse

    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order_winner":
        return JSONResponse({"ok": False, "message": "対象外のレースです"})

    stage_no = await _ow_current_stage(tid, t, db)
    stage = await _ow_stage_row(tid, stage_no, db)
    if not stage or stage.get("status") == "closed":
        return JSONResponse({"ok": False, "message": "この段階は終了しています"})

    form = await request.form()
    force_single = (form.get("force_single") or "") in ("1", "true", "on")

    pending = await _ow_queue_pending(tid, stage_no, db)
    q = len(pending)
    if q == 0:
        return JSONResponse({"ok": False, "message": "待機列が空です"})

    if q == 1:
        if not force_single:
            return JSONResponse({
                "ok": False,
                "need_confirm_single": True,
                "message": "待機列が1人です。単走で走らせますか？（やむを得ない場合のみ）",
            })
        # 単走を明示許可 → 1人組を作成
        m = pending[0]
        await db.execute("UPDATE order_queue SET consumed=1 WHERE id=?", (m["id"],))
        await _ow_create_heat(tid, stage_no, [m["entry_id"]], db)
        await db.commit()
        pending = await _ow_queue_pending(tid, stage_no, db)
        return JSONResponse({
            "ok": True,
            "message": f"{m['name']} を単走で確定しました",
            "made_groups": 1,
            "queue_count": len(pending),
            "reload": True,
        })

    # q>=2：端数含め可能な限り組む
    made = await _ow_make_groups(tid, t, stage_no, db, force_all=True)
    await db.commit()
    pending = await _ow_queue_pending(tid, stage_no, db)
    return JSONResponse({
        "ok": True,
        "message": f"{made}組を確定しました",
        "made_groups": made,
        "queue_count": len(pending),
        "reload": True,
    })


# ---- 1-b: 1着入力（〇×）・通過判定・枠到達で自動締め ----

async def _ow_recount_and_close_if_full(tid: int, stage_no: int, stage: dict, db) -> bool:
    """当該段階の通過者数が advance_count に達していれば段階を closed にする。
    戻り値: closed にしたら True。"""
    passed = await _ow_passed_count(tid, stage_no, db)
    if passed >= (stage["advance_count"] or 0):
        await db.execute(
            "UPDATE order_winner_stages SET status='closed' WHERE tournament_id=? AND stage_no=?",
            (tid, stage_no),
        )
        return True
    return False


async def _ow_mark_eliminated_for_heat(tid: int, stage_no: int, stage: dict, entry_ids: list[int], db) -> None:
    """組の各レーサーのうち、通過しておらず走行上限に達した者を eliminated にする。"""
    max_runs = stage["max_runs"] or 0
    for eid in entry_ids:
        rc = await _ow_get_racer(tid, stage_no, eid, db)
        if not rc:
            continue
        if rc["status"] == "passed":
            continue
        if (rc["runs"] or 0) >= max_runs:
            await db.execute(
                "UPDATE order_winner_racers SET status='eliminated' "
                "WHERE tournament_id=? AND stage_no=? AND entry_id=?",
                (tid, stage_no, eid),
            )


@router.post("/{tid}/qualifying/order-winner/heat/{heat_id}/win")
async def order_winner_set_win(tid: int, heat_id: int, request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """勝ち抜け：組の1着を入力する（〇×）。
    form:
      win_entry_id : 1着のentry_id。'0' または空/'none' の場合は「該当者なし（全員CO）」。
    処理:
      heat_results更新 → 1着のwins+1 → 通過判定(passed/passed_seq) →
      枠到達で段階closed → 敗退判定(runs>=max_runsをeliminated) → heat done。
    """
    from fastapi.responses import JSONResponse

    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order_winner":
        return JSONResponse({"ok": False, "message": "対象外のレースです"})

    # heat と段階の整合（heats.round_no に stage_no を格納している）
    async with db.execute(
        "SELECT id, round_no, status FROM heats WHERE id=? AND tournament_id=?",
        (heat_id, tid),
    ) as cur:
        heat = await cur.fetchone()
    if not heat:
        return JSONResponse({"ok": False, "message": "組が見つかりません"})
    stage_no = heat["round_no"]
    stage = await _ow_stage_row(tid, stage_no, db)
    if not stage:
        return JSONResponse({"ok": False, "message": "段階設定が見つかりません"})

    # 組のレーン（entry_id）一覧
    async with db.execute(
        "SELECT hl.id AS lane_id, hl.entry_id FROM heat_lanes hl WHERE hl.heat_id=? ORDER BY hl.lane_no",
        (heat_id,),
    ) as cur:
        lanes = [dict(r) for r in await cur.fetchall()]
    if not lanes:
        return JSONResponse({"ok": False, "message": "組にレーサーがいません"})
    lane_entry_ids = [ln["entry_id"] for ln in lanes]

    form = await request.form()
    raw_win = (form.get("win_entry_id") or "").strip().lower()
    win_eid = None
    if raw_win and raw_win not in ("0", "none", "co", "null"):
        if not raw_win.isdigit():
            return JSONResponse({"ok": False, "message": "win_entry_id が不正です"})
        win_eid = int(raw_win)
        if win_eid not in lane_entry_ids:
            return JSONResponse({"ok": False, "message": "1着指定がこの組にいません"})

    # ---- heat_results を更新（1着=win1/rank1、他=win0。該当者なしは全員 is_co=1）----
    for ln in lanes:
        eid = ln["entry_id"]
        if win_eid is not None and eid == win_eid:
            win, rank, is_co = 1, 1, 0
        else:
            # 1着以外。該当者なし（全員CO）のときは is_co=1、通常敗はrank0/is_co0
            win, rank = 0, 0
            is_co = 1 if win_eid is None else 0
        await db.execute("DELETE FROM heat_results WHERE heat_lane_id=?", (ln["lane_id"],))
        await db.execute(
            "INSERT INTO heat_results (heat_lane_id, win, best_time, lap_count, rank, points, is_co) "
            "VALUES (?,?,0,0,?,0,?)",
            (ln["lane_id"], win, rank, is_co),
        )

    passed_now = None
    stage_closed = False

    # ---- 1着の wins+1 → 通過判定 ----
    if win_eid is not None:
        await _ow_ensure_racer(tid, stage_no, win_eid, db)
        await db.execute(
            "UPDATE order_winner_racers SET wins = wins + 1 "
            "WHERE tournament_id=? AND stage_no=? AND entry_id=?",
            (tid, stage_no, win_eid),
        )
        rc = await _ow_get_racer(tid, stage_no, win_eid, db)
        if rc and rc["status"] == "racing" and (rc["wins"] or 0) >= (stage["win_target"] or 1):
            # 通過確定。passed_seq を採番（当該段階の既存最大+1）
            async with db.execute(
                "SELECT COALESCE(MAX(passed_seq),0) AS mx FROM order_winner_racers "
                "WHERE tournament_id=? AND stage_no=?",
                (tid, stage_no),
            ) as cur:
                next_pseq = ((await cur.fetchone())["mx"] or 0) + 1
            await db.execute(
                "UPDATE order_winner_racers SET status='passed', passed_seq=? "
                "WHERE tournament_id=? AND stage_no=? AND entry_id=?",
                (next_pseq, tid, stage_no, win_eid),
            )
            passed_now = win_eid
            # 枠到達判定
            stage_closed = await _ow_recount_and_close_if_full(tid, stage_no, stage, db)

    # ---- 敗退判定（通過していない者で走行上限に達していれば eliminated）----
    await _ow_mark_eliminated_for_heat(tid, stage_no, stage, lane_entry_ids, db)

    # ---- heat 完了 ----
    await db.execute("UPDATE heats SET status='done' WHERE id=?", (heat_id,))
    await db.commit()

    # 観覧HTML自動更新
    try:
        from app.services.public_html import export_current_html
        import asyncio
        asyncio.create_task(export_current_html(db))
    except Exception:
        pass

    passed_count = await _ow_passed_count(tid, stage_no, db)
    # 1着レーサー名（通過表示用）
    passed_name = None
    if passed_now is not None:
        async with db.execute(
            "SELECT r.name FROM entries e JOIN racers r ON r.id=e.racer_id WHERE e.id=?",
            (passed_now,),
        ) as cur:
            row = await cur.fetchone()
            passed_name = row["name"] if row else None

    return JSONResponse({
        "ok": True,
        "stage_no": stage_no,
        "passed_entry_id": passed_now,
        "passed_name": passed_name,
        "passed_count": passed_count,
        "advance_count": stage["advance_count"],
        "stage_closed": stage_closed,
        "message": (
            f"{passed_name} が通過しました（{passed_count}/{stage['advance_count']}）"
            if passed_now is not None else
            ("該当者なしで確定しました" if win_eid is None else "結果を保存しました")
        ) + ("　★通過枠が埋まり、この段階を終了しました" if stage_closed else ""),
        "reload": True,
    })


# ---- 1-c: もう1周追加（枠割れ救済）・次段階遷移・リセット系 ----

@router.post("/{tid}/qualifying/order-winner/add-run")
async def order_winner_add_run(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """勝ち抜け：枠割れ救済「もう1周追加」。
    現段階の max_runs を +1 し、まだ通過していない全員（racing / eliminated）を
    racing に戻して再スキャン可能にする。段階が closed でも running に戻す。
    """
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order_winner":
        return JSONResponse({"ok": False, "message": "対象外のレースです"})

    stage_no = await _ow_current_stage(tid, t, db)
    stage = await _ow_stage_row(tid, stage_no, db)
    if not stage:
        return JSONResponse({"ok": False, "message": "段階設定が見つかりません"})

    new_max = (stage["max_runs"] or 0) + 1
    await db.execute(
        "UPDATE order_winner_stages SET max_runs=?, status='running' "
        "WHERE tournament_id=? AND stage_no=?",
        (new_max, tid, stage_no),
    )
    # 未通過者（eliminated含む）を racing に戻す（passed はそのまま）
    await db.execute(
        "UPDATE order_winner_racers SET status='racing' "
        "WHERE tournament_id=? AND stage_no=? AND status='eliminated'",
        (tid, stage_no),
    )
    await db.commit()
    return JSONResponse({
        "ok": True,
        "stage_no": stage_no,
        "max_runs": new_max,
        "message": f"{stage_no}次予選の最大走行回数を {new_max} 回に増やしました。敗退者が再スキャン可能です。",
        "reload": True,
    })


async def _ow_stage_count(tid: int, t, db) -> int:
    """このレースの予選段階数（tournaments.order_winner_stage_count、既定は段階行数）。"""
    n = dict(t).get("order_winner_stage_count")
    if n:
        return n
    async with db.execute(
        "SELECT COUNT(*) AS n FROM order_winner_stages WHERE tournament_id=?", (tid,)
    ) as cur:
        return (await cur.fetchone())["n"] or 1


@router.post("/{tid}/qualifying/order-winner/next-stage")
async def order_winner_next_stage(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """勝ち抜け：現段階が closed（通過確定）なら次段階へ進む。
    最終段階なら決勝進出者確定として entries.advanced を立てる。"""
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order_winner":
        return JSONResponse({"ok": False, "message": "対象外のレースです"})

    stage_no = await _ow_current_stage(tid, t, db)
    stage = await _ow_stage_row(tid, stage_no, db)
    if not stage:
        return JSONResponse({"ok": False, "message": "段階設定が見つかりません"})
    if stage.get("status") != "closed":
        # 通過枠が埋まっていない段階は進めない（枠割れなら add-run で救済すべき）
        passed = await _ow_passed_count(tid, stage_no, db)
        return JSONResponse({
            "ok": False,
            "message": f"{stage_no}次予選はまだ確定していません（通過 {passed}/{stage['advance_count']}）。"
                       f"枠が埋まらない場合は「もう1周追加」で走行を続けてください。",
        })

    total_stages = await _ow_stage_count(tid, t, db)

    if stage_no >= total_stages:
        # 最終段階 → 決勝進出者確定。passed のエントリーに advanced=1 を立てる。
        async with db.execute(
            "SELECT entry_id FROM order_winner_racers "
            "WHERE tournament_id=? AND stage_no=? AND status='passed' "
            "ORDER BY passed_seq",
            (tid, stage_no),
        ) as cur:
            finalists = [r["entry_id"] for r in await cur.fetchall()]
        for eid in finalists:
            await db.execute(
                "UPDATE entries SET advanced=1 WHERE id=? AND tournament_id=?",
                (eid, tid),
            )
        await db.commit()
        return JSONResponse({
            "ok": True,
            "finished": True,
            "finalist_count": len(finalists),
            "message": f"全予選が完了しました。決勝進出 {len(finalists)} 名を確定しました。",
            "reload": True,
        })

    # 次段階へ
    next_stage_no = stage_no + 1
    await db.execute(
        "UPDATE tournaments SET order_winner_current_stage=? WHERE id=?",
        (next_stage_no, tid),
    )
    # 次段階を running に（未設定なら pending のまま→ここで running 化）
    await db.execute(
        "UPDATE order_winner_stages SET status='running' "
        "WHERE tournament_id=? AND stage_no=?",
        (tid, next_stage_no),
    )
    await db.commit()
    return JSONResponse({
        "ok": True,
        "finished": False,
        "next_stage_no": next_stage_no,
        "message": f"{next_stage_no}次予選に進みました。通過者を改めてスキャンしてください。",
        "reload": True,
    })


@router.post("/{tid}/qualifying/order-winner/reopen")
async def order_winner_reopen(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """現段階の closed を解除して running に戻す（結果・通過状況は維持）。
    誤って締まった場合の復帰用。"""
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order_winner":
        return JSONResponse({"ok": False})
    stage_no = await _ow_current_stage(tid, t, db)
    await db.execute(
        "UPDATE order_winner_stages SET status='running' "
        "WHERE tournament_id=? AND stage_no=?",
        (tid, stage_no),
    )
    await db.commit()
    return JSONResponse({"ok": True, "stage_no": stage_no})


async def _ow_delete_stage_heats(tid: int, stage_no: int, db) -> None:
    """当該段階（heats.round_no=stage_no）の組・レーン・結果を削除する。"""
    async with db.execute(
        "SELECT id FROM heats WHERE tournament_id=? AND round_no=?", (tid, stage_no)
    ) as cur:
        hids = [r["id"] for r in await cur.fetchall()]
    if hids:
        ph = ",".join("?" * len(hids))
        await db.execute(
            f"DELETE FROM heat_results WHERE heat_lane_id IN "
            f"(SELECT id FROM heat_lanes WHERE heat_id IN ({ph}))", hids
        )
        await db.execute(f"DELETE FROM heat_lanes WHERE heat_id IN ({ph})", hids)
        await db.execute(f"DELETE FROM heats WHERE id IN ({ph})", hids)


@router.post("/{tid}/qualifying/order-winner/reset-stage")
async def order_winner_reset_stage(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """現段階のみリセット（待機列・組・結果・racers状態を現段階分だけ削除）。
    段階設定（win_target/max_runs/advance_count）は維持。"""
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT * FROM tournaments WHERE id=?", (tid,)) as cur:
        t = await cur.fetchone()
    if not t or dict(t).get("qualifying_type") != "order_winner":
        return JSONResponse({"ok": False})
    stage_no = await _ow_current_stage(tid, t, db)
    await _ow_delete_stage_heats(tid, stage_no, db)
    await db.execute(
        "DELETE FROM order_queue WHERE tournament_id=? AND stage_no=?", (tid, stage_no)
    )
    await db.execute(
        "DELETE FROM order_winner_racers WHERE tournament_id=? AND stage_no=?", (tid, stage_no)
    )
    await db.execute(
        "UPDATE order_winner_stages SET status='running' "
        "WHERE tournament_id=? AND stage_no=?",
        (tid, stage_no),
    )
    await db.commit()
    return JSONResponse({"ok": True, "stage_no": stage_no, "reload": True})


@router.post("/{tid}/qualifying/order-winner/reset")
async def order_winner_reset(tid: int, db: aiosqlite.Connection = Depends(get_db)):
    """勝ち抜け予選を全リセット（全段階の待機列・組・結果・racers状態を削除）。
    段階設定行（order_winner_stages）は status を pending に戻し、進行段階を1へ。"""
    from fastapi.responses import JSONResponse
    async with db.execute("SELECT id FROM heats WHERE tournament_id=?", (tid,)) as cur:
        hids = [r["id"] for r in await cur.fetchall()]
    if hids:
        ph = ",".join("?" * len(hids))
        await db.execute(
            f"DELETE FROM heat_results WHERE heat_lane_id IN "
            f"(SELECT id FROM heat_lanes WHERE heat_id IN ({ph}))", hids
        )
        await db.execute(f"DELETE FROM heat_lanes WHERE heat_id IN ({ph})", hids)
    await db.execute("DELETE FROM heats WHERE tournament_id=?", (tid,))
    await db.execute("DELETE FROM order_queue WHERE tournament_id=?", (tid,))
    await db.execute("DELETE FROM order_winner_racers WHERE tournament_id=?", (tid,))
    await db.execute(
        "UPDATE order_winner_stages SET status='pending' WHERE tournament_id=?", (tid,)
    )
    await db.execute(
        "UPDATE tournaments SET order_winner_current_stage=1 WHERE id=?", (tid,)
    )
    # 決勝進出フラグも解除
    await db.execute(
        "UPDATE entries SET advanced=NULL WHERE tournament_id=?", (tid,)
    )
    await db.commit()
    return JSONResponse({"ok": True, "reload": True})
