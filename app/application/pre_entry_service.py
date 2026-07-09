"""公開事前エントリーフォーム（/entry）のユースケース。

このユースケースは「現在店舗の db_path へ自前接続する」旧 main.py の方式を
そのまま維持する（get_db 依存にしない）。SQL・判定順序は旧コードと同一。
"""
import uuid
import aiosqlite

from app.domain.deadline import deadline_passed


class PreEntryService:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def list_open_form_races(self):
        """フォーム方式・締切前のレース一覧。"""
        rows = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT id, name, date, time_slot, regulation, pre_entry_deadline, status
                     FROM tournaments
                    WHERE pre_entry=1 AND pre_entry_method='form'
                    ORDER BY date DESC, id DESC"""
            ) as cur:
                all_form = await cur.fetchall()
        for r in all_form:
            if deadline_passed(r["pre_entry_deadline"]):
                continue
            rows.append(dict(r))
        return rows

    async def prepare_form(self, tid: int):
        """レース取得＋（受付中なら）使い捨てトークン発行。
        返り値: (race_row|None, token, closed)。race が None/対象外なら呼び出し側で一覧へ。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT id, name, date, time_slot, regulation,
                          pre_entry, pre_entry_method, pre_entry_deadline
                     FROM tournaments WHERE id=?""", (tid,)
            ) as cur:
                t = await cur.fetchone()

            if (not t) or (not t["pre_entry"]) or (t["pre_entry_method"] != "form"):
                return None, "", False

            closed = deadline_passed(t["pre_entry_deadline"])
            token = ""
            if not closed:
                token = uuid.uuid4().hex
                await db.execute(
                    "INSERT INTO entry_form_tokens (token, tournament_id) VALUES (?,?)",
                    (token, tid),
                )
                await db.commit()
        return t, token, closed

    async def submit(self, tid: int, form):
        """フォーム送信を pre_entries へ登録する。
        返り値: ("ok", race_row, added) または ("redirect_list", None, 0)
                または ("error:<code>", None, 0)。判定順序は旧コードと同一。"""
        token = (form.get("token", "") or "").strip()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # レース存在・方式・締切チェック
            async with db.execute(
                """SELECT id, name, pre_entry, pre_entry_method, pre_entry_deadline
                     FROM tournaments WHERE id=?""", (tid,)
            ) as cur:
                t = await cur.fetchone()
            if (not t) or (not t["pre_entry"]) or (t["pre_entry_method"] != "form"):
                return "redirect_list", None, 0
            if deadline_passed(t["pre_entry_deadline"]):
                return "error:closed", None, 0

            # 二重サブミット防止：トークンが有効（存在・未使用・当該レース）か検証
            async with db.execute(
                "SELECT token, tournament_id, used FROM entry_form_tokens WHERE token=?",
                (token,),
            ) as cur:
                tok = await cur.fetchone()
            if (not tok) or (tok["tournament_id"] != tid) or (tok["used"] == 1):
                return "error:token", None, 0

            # ---- 入力パース（代表者＝1人目／2人目以降は同行フィールド）----
            rep_pref = (form.get("prefecture", "") or "").strip()
            rep_ctype = (form.get("contact_type", "") or "").strip()
            rep_contact = (form.get("contact", "") or "").strip()

            names = [s.strip() for s in form.getlist("name")]
            yomis = [s.strip() for s in form.getlist("yomi")]
            children = form.getlist("is_child")  # "0"/"1" の配列

            n = len(names)
            if not rep_pref or rep_ctype not in ("mail", "phone", "x") or not rep_contact:
                return "error:required", None, 0
            if n == 0 or len(yomis) != n or len(children) != n:
                return "error:required", None, 0
            for i in range(n):
                if not names[i] or not yomis[i]:
                    return "error:required", None, 0
                if children[i] not in ("0", "1"):
                    return "error:required", None, 0

            # 連投スロットル：同一連絡先で直近60秒以内の登録があればはじく
            async with db.execute(
                """SELECT COUNT(*) AS c FROM pre_entries
                    WHERE tournament_id=? AND contact_type=? AND contact=?
                      AND created_at >= datetime('now','localtime','-60 seconds')""",
                (tid, rep_ctype, rep_contact),
            ) as cur:
                recent = (await cur.fetchone())["c"]
            if recent > 0:
                return "error:toofast", None, 0

            # 同一レース内の既存名（重複登録防止）
            async with db.execute(
                "SELECT name FROM pre_entries WHERE tournament_id=?", (tid,)
            ) as cur:
                existing = {r["name"] for r in await cur.fetchall()}

            # 連番の現在最大値
            async with db.execute(
                "SELECT COALESCE(MAX(seq_no),0) AS mx FROM pre_entries WHERE tournament_id=?",
                (tid,),
            ) as cur:
                seq = (await cur.fetchone())["mx"]

            # ---- 登録（1人目を代表者として連絡先を持たせる）----
            added = 0
            seen_in_input = set()
            for i in range(n):
                nm = names[i]
                if nm in existing or nm in seen_in_input:
                    continue  # 既存・同一送信内重複はスキップ
                seen_in_input.add(nm)
                seq += 1
                is_rep = 1 if i == 0 else 0
                await db.execute(
                    """INSERT INTO pre_entries
                         (tournament_id, seq_no, name, yomi, is_child,
                          prefecture, contact_type, contact, is_representative)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (tid, seq, nm, yomis[i], int(children[i]),
                     rep_pref, rep_ctype, rep_contact, is_rep),
                )
                existing.add(nm)
                added += 1

            # トークンを使用済みに（再送信を無効化）
            await db.execute(
                "UPDATE entry_form_tokens SET used=1 WHERE token=?", (token,)
            )
            await db.commit()

        return "ok", t, added