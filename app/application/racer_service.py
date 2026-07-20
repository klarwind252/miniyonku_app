"""レーサー管理ユースケース。

責務：リポジトリの合成・トランザクション境界（commit）・ドメイン関数の適用。
HTTP（Request/Response/テンプレート）は一切知らない。
"""
import uuid
from datetime import date, datetime

from app.domain import racer_import
from app.domain.day_type import day_type_of
from app.domain.kana import KANA_ROWS, kana_row_of
from app.domain.regulation import is_junior_tournament
from app.infrastructure.db.repositories.racer_repository import RacerRepository
from app.infrastructure.db.repositories.entry_repository import EntryRepository
from app.infrastructure.db.repositories.result_repository import ResultRepository

# DB列ではなくPython側で計算してから並べ替える列（前回入賞/前回優勝/前回来店）
COMPUTED_SORT = {"podium": "last_podium_date",
                 "win": "last_win_date",
                 "entry": "last_entry_date"}


class RacerService:
    def __init__(self, db):
        self.db = db
        self.racers = RacerRepository(db)
        self.entries = EntryRepository(db)
        self.results = ResultRepository(db)

    # ---- 本日コンテキスト（一覧画面共通） ----
    async def get_today_context(self):
        today = date.today().isoformat()
        today_tournaments = await self.entries.list_today_tournaments(today)

        entry_map: dict[int, dict[int, str]] = {}
        if today_tournaments:
            t_ids = [t["id"] for t in today_tournaments]
            for row in await self.entries.list_entries_in(t_ids):
                entry_map.setdefault(row["racer_id"], {})[row["tournament_id"]] = row["entry_at"]

        today_racers = await self.racers.list_today_visitors(today)

        finalized_tids = set()
        for t in today_tournaments:
            if await self.results.is_result_finalized(t["id"]):
                finalized_tids.add(t["id"])

        return {
            "today_tournaments": today_tournaments,
            "today_racers": today_racers,
            "entry_map": entry_map,
            "finalized_tids": finalized_tids,
        }

    # ---- 一覧（検索・フィルタ・計算列ソート） ----
    async def list_racers(self, sort: str, order: str, q: str, kana: str, regular: str):
        is_computed_sort = sort in COMPUTED_SORT
        sort_col = "yomi" if is_computed_sort else RacerRepository.VALID_SORT.get(sort, "yomi")
        sort_ord = RacerRepository.VALID_ORDER.get(order, "ASC")

        racers = await self.racers.list_visible(sort_col, sort_ord, q)

        # 「常連」フィルタは先頭文字（kana）とは独立。regular指定時はkanaを無視する。
        if regular == "1":
            kana = ""

        if kana in KANA_ROWS:
            racers = [r for r in racers
                      if kana in (kana_row_of(r["name"]), kana_row_of(r["yomi"]))]
        else:
            kana = ""

        if regular == "1":
            racers = [r for r in racers if r["is_regular"]]
        else:
            regular = ""

        # 前回優勝日・前回入賞日を算出して各行へ付与（Rowは読み取り専用のためdict化）
        racer_ids = [r["id"] for r in racers]
        award_map = await self.results.last_award_dates(racer_ids)
        racers = [dict(r) for r in racers]
        for r in racers:
            a = award_map.get(r["id"], {})
            r["last_podium_date"] = a.get("podium")
            r["last_win_date"] = a.get("win")
            r["last_entry_date"] = a.get("entry")

        # 計算列でのソート。日付なしは昇順・降順いずれでも常に末尾。
        if is_computed_sort:
            key_field = COMPUTED_SORT[sort]
            reverse = (sort_ord == "DESC")
            present = [r for r in racers if r.get(key_field) not in (None, "")]
            absent = [r for r in racers if r.get(key_field) in (None, "")]
            present.sort(key=lambda row: row.get(key_field) or "", reverse=reverse)
            racers = present + absent

        # 料金計算用: 来店日の曜日/祝日タイプを判定
        day_types = {}
        for r in racers:
            vd = (r["last_visit_at"] or "")[:10]
            if vd and vd not in day_types:
                day_types[vd] = day_type_of(vd)

        return racers, kana, regular, day_types

    # ---- 追加・編集・削除 ----
    async def add_racer(self, name, yomi, is_child_val, is_regular_val):
        """重複時は None を返さずエラーメッセージ文字列を返す。成功時は None。"""
        if await self.racers.find_by_name(name):
            return f"「{name}」は既に登録されています。"
        await self.racers.insert(name, yomi, is_child_val, is_regular_val)
        await self.db.commit()
        return None

    async def edit_racer(self, racer_id, name, yomi, is_child_val, is_regular_val):
        if await self.racers.find_by_name(name, exclude_id=racer_id):
            return f"「{name}」は既に登録されています。"
        await self.racers.update_identity(racer_id, name, yomi, is_child_val, is_regular_val)
        await self.db.commit()
        return None

    async def delete_racer(self, racer_id):
        await self.racers.delete(racer_id)
        await self.db.commit()

    # ---- エントリー単発操作 ----
    async def entry_single(self, racer_id, tournament_id):
        if await self.entries.exists(tournament_id, racer_id):
            return {"ok": False, "error": "既にエントリー済みです"}
        order = await self.entries.next_entry_order(tournament_id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.entries.insert(tournament_id, racer_id, order, now)
        await self.db.commit()
        return {"ok": True, "timestamp": now}

    async def remove_entry_single(self, racer_id, tournament_id):
        await self.entries.delete_one(tournament_id, racer_id)
        await self.db.commit()
        return {"ok": True}

    # ---- 来店・本日一括エントリー ----
    async def cancel_visit(self, racer_id):
        """来店取消: last_visit_at をNULLに戻し、本日のレースエントリーをすべて削除する"""
        today = date.today().isoformat()
        await self.racers.set_last_visit(racer_id, None)
        today_tids = await self.entries.list_tournament_ids_on(today)
        if today_tids:
            await self.entries.delete_racer_entries_in(racer_id, today_tids)
        await self.db.commit()
        return {"ok": True}

    async def entry_today(self, racer_id):
        """本日開催予定のレースにエントリーする。
        - ジュニアレースは is_child=1 のみ / それ以外は全員
        """
        today = date.today().isoformat()
        racer = await self.racers.get_is_child(racer_id)
        if not racer:
            return None  # 呼び出し側で404
        is_child = bool(racer["is_child"])

        today_tournaments = await self.entries.list_today_tournaments(today)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.racers.set_last_visit(racer_id, now)

        entered, skipped_junior = [], []
        for t in today_tournaments:
            tid = t["id"]
            if is_junior_tournament(t["regulation"]) and not is_child:
                skipped_junior.append(t["name"])
                continue
            if await self.entries.exists(tid, racer_id):
                continue
            order = await self.entries.next_entry_order(tid)
            await self.entries.insert(tid, racer_id, order, now)
            entered.append(t["name"])

        await self.db.commit()
        return {
            "ok": True,
            "entered": len(entered),
            "entered_names": entered,
            "skipped_junior": skipped_junior,
            "timestamp": now,
            "visit_at": now,
        }

    # ---- 料金計算API ----
    async def visit_data(self):
        today = date.today().isoformat()
        today_tids = await self.entries.list_today_tournament_ids(today)

        entry_map = {}
        if today_tids:
            for row in await self.entries.list_entries_in(today_tids):
                entry_map.setdefault(str(row["racer_id"]), {})[str(row["tournament_id"])] = True

        day_types = {today: day_type_of(today)}

        # today_day_type（サーバー保存値を優先、なければ自動判定）
        saved = await self.entries.get_saved_today_day_type()
        if saved and ":" in saved:
            saved_date, saved_type = saved.split(":", 1)
            if saved_date == today:
                day_types[today] = saved_type

        return {
            "day_types": day_types,
            "entry_map": entry_map,
            "today_tournament_ids": [str(t) for t in today_tids],
            "today_day_type": day_types.get(today, "weekday"),
        }

    # ---- CSVエクスポート ----
    async def export_rows(self):
        exclude = await self.racers.has_column("ephemeral")
        rows = await self.racers.list_for_export(exclude_ephemeral=exclude)
        out = [["uid", "name", "yomigana", "is_junior"]]
        for r in rows:
            uid = r["uid"] or str(uuid.uuid4())  # 万一uid未設定でも空欄にしない
            out.append([uid, r["name"], r["yomi"] or "", 1 if r["is_child"] else 0])
        return out

    # ---- CSVインポート ----
    async def import_preview(self, raw: bytes):
        text = racer_import.decode_csv_bytes(raw)
        rows, errors = racer_import.parse_import_csv(text)
        if not rows and errors:
            return None, errors
        existing = await self.racers.list_for_matching()
        preview = racer_import.build_preview(existing, rows)
        counts = {"new": 0, "id_match": 0, "name_match": 0, "conflict": 0}
        for p in preview:
            counts[p["state"]] = counts.get(p["state"], 0) + 1
        return {"ok": True, "preview": preview, "counts": counts, "warnings": errors}, None

    async def import_commit(self, rows: list[dict]):
        added = updated = skipped = 0
        for row in rows:
            action = (row.get("action") or "skip").strip()
            name = racer_import.norm(row.get("name"))
            yomi = racer_import.norm(row.get("yomigana"))
            is_jr = 1 if int(row.get("is_junior") or 0) == 1 else 0
            uid = racer_import.norm(row.get("uid"))
            existing_id = row.get("existing_id")

            if action == "skip" or not name:
                skipped += 1
                continue
            if action == "overwrite" and existing_id:
                await self.racers.update_identity(existing_id, name, yomi, is_jr)
                updated += 1
            elif action == "import":
                new_uid = uid
                if new_uid:
                    if await self.racers.find_by_uid(new_uid):
                        new_uid = str(uuid.uuid4())
                else:
                    new_uid = str(uuid.uuid4())
                await self.racers.insert_imported(name, yomi, is_jr, new_uid)
                added += 1
            else:
                skipped += 1
        await self.db.commit()
        return {"ok": True, "added": added, "updated": updated, "skipped": skipped}

    # ---- 実績画面 ----
    async def achievements(self, racer_id: int, start: str, end: str):
        racer = await self.racers.get_visible(racer_id)
        if not racer:
            return None

        today = date.today()
        if not end:
            end = today.isoformat()
        if not start:
            try:
                start = today.replace(year=today.year - 1).isoformat()
            except ValueError:
                # 2/29 対策
                start = today.replace(year=today.year - 1, day=28).isoformat()

        cand = await self.entries.list_races_of_racer(racer_id, start, end)

        rows = []
        finalized_count = win_count = podium_count = 0
        last_win = None
        for t in cand:
            if not await self.results.is_result_finalized(t["id"]):
                continue
            finalized_count += 1
            podium = await self.results.race_podium_racer_ids(t["id"], t["qualifying_type"])
            my_rank = None
            for rk in (1, 2, 3):
                if podium.get(rk) == racer_id:
                    my_rank = rk
                    break
            if my_rank == 1:
                win_count += 1
                podium_count += 1
                label = "1位"
                if last_win is None:  # candは日付降順なので最初が最新
                    last_win = {"date": t["date"], "name": t["name"]}
            elif my_rank in (2, 3):
                podium_count += 1
                label = f"{my_rank}位"
            else:
                label = "━"
            rows.append({"date": t["date"], "name": t["name"], "result": label})

        win_rate = round(win_count / finalized_count * 100) if finalized_count else 0
        podium_rate = round(podium_count / finalized_count * 100) if finalized_count else 0

        return {
            "racer": racer, "start": start, "end": end, "rows": rows,
            "race_count": finalized_count,
            "win_rate": win_rate, "podium_rate": podium_rate, "last_win": last_win,
        }

    # ---- 過去成績（参加者向け・全レーサー集計） ----
    async def history_overview(self):
        """確定済み大会をもとに、全レーサーの 参加数／優勝数／入賞数 を集計して返す。
        大会ごとに表彰台を1回だけ求めて各レーサーへ配るため、レーサー数に依存せず軽い。
        """
        async with self.db.execute(
            "SELECT id, name, date, qualifying_type FROM tournaments ORDER BY date DESC, id DESC"
        ) as cur:
            tournaments = await cur.fetchall()

        finalized = []
        for t in tournaments:
            if await self.results.is_result_finalized(t["id"]):
                finalized.append(t)

        stats = {}
        def _slot(rid):
            s = stats.get(rid)
            if s is None:
                s = {"races": 0, "wins": 0, "podiums": 0}
                stats[rid] = s
            return s

        # 参加回数（確定大会の参加者。重複エントリは1回に丸める）
        if finalized:
            ftids = [t["id"] for t in finalized]
            seen = set()
            for row in await self.entries.list_entries_in(ftids):
                key = (row["racer_id"], row["tournament_id"])
                if key in seen:
                    continue
                seen.add(key)
                _slot(row["racer_id"])["races"] += 1

        # 優勝・入賞（各大会の表彰台1〜3位）
        for t in finalized:
            podium = await self.results.race_podium_racer_ids(t["id"], t["qualifying_type"])
            for rk in (1, 2, 3):
                rid = podium.get(rk)
                if rid is None:
                    continue
                s = _slot(rid)
                s["podiums"] += 1
                if rk == 1:
                    s["wins"] += 1

        ids = [rid for rid, s in stats.items() if s["races"] > 0 or s["podiums"] > 0]
        names = {}
        if ids:
            ph = ",".join("?" for _ in ids)
            async with self.db.execute(
                f"SELECT id, name, yomi, is_child FROM racers WHERE id IN ({ph})", ids
            ) as cur:
                for row in await cur.fetchall():
                    names[row["id"]] = {"name": row["name"], "yomi": row["yomi"] or "",
                                        "jr": bool(row["is_child"])}

        racers = []
        for rid in ids:
            info = names.get(rid)
            if not info:
                continue  # マスタから削除されたレーサーは除外
            s = stats[rid]
            racers.append({
                "id": rid, "name": info["name"], "yomi": info["yomi"],
                "jr": info.get("jr", False),
                "races": s["races"], "wins": s["wins"], "podiums": s["podiums"],
            })
        racers.sort(key=lambda r: (-r["wins"], -r["podiums"], -r["races"], r["yomi"], r["name"]))
        return {"racers": racers, "race_total": len(finalized)}