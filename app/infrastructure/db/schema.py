import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "../../../data/miniyonku.db")
DB_PATH = os.path.abspath(DB_PATH)


def current_db_path() -> str:
    """現在のリクエストが属する店舗のDBパスを返す。

    複数店舗化（クラウド版）では StoreResolverMiddleware が ContextVar に店舗を
    セットしているため、その店舗のDBを開く。未設定（オンプレ／単一店舗）では
    従来どおり既定の DB_PATH を返す。
    """
    try:
        from app.store_context import current_store
        store = current_store.get()
        if store and store.db_path:
            return store.db_path
    except Exception:
        pass
    return DB_PATH


async def get_db():
    db = await aiosqlite.connect(current_db_path())
    db.row_factory = aiosqlite.Row
    # 接続ごとの標準PRAGMA。
    #   busy_timeout : バックグラウンドHTML書き出しと管理操作の書き込みが競合したとき、
    #                  即 "database is locked"（500）で落ちる代わりに最大5秒待って再試行する。
    #   synchronous  : WAL 前提の推奨値。速度を上げつつ実用上十分な安全性を確保する。
    #   foreign_keys : 参照整合性を有効化（誤った孤児行の混入を防ぐ）。
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        await db.close()


async def init_db(db_path: str = None):
    if db_path is None:
        db_path = DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")

        # ---- 基本テーブル作成 ----
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS racers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            yomi        TEXT,
            is_child    INTEGER DEFAULT 0,
            is_regular  INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS tournaments (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            name                    TEXT NOT NULL,
            date                    TEXT NOT NULL,
            time_slot               TEXT DEFAULT 'day',
            time_slot_free          TEXT,
            regulation              TEXT,
            qualifying_type         TEXT DEFAULT 'heat_tournament',
            final_type              TEXT DEFAULT 'tournament',
            lane_count              INTEGER DEFAULT 2,
            status                  TEXT DEFAULT 'prepare',
            note                    TEXT,
            qual_heat_count         INTEGER DEFAULT 1,
            qual_heat_advance       INTEGER DEFAULT 2,
            qual_group_count        INTEGER DEFAULT 1,
            qual_group_advance      INTEGER DEFAULT 2,
            qual_heat_final         INTEGER DEFAULT 0,
            qual_heat_final_advance INTEGER DEFAULT 1,
            qual_final_advance      INTEGER DEFAULT 2,
            created_at              TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS entries (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id  INTEGER NOT NULL,
            racer_id       INTEGER NOT NULL,
            car_class      TEXT,
            status         TEXT DEFAULT 'active',
            entry_order    INTEGER,
            entry_at       TEXT,
            advanced       INTEGER DEFAULT NULL,
            seeded         INTEGER DEFAULT 0,
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id),
            FOREIGN KEY (racer_id) REFERENCES racers(id)
        );
        CREATE TABLE IF NOT EXISTS heats (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id  INTEGER NOT NULL,
            heat_no        INTEGER NOT NULL,
            group_no       INTEGER DEFAULT 0,
            round_no       INTEGER DEFAULT 0,
            status         TEXT DEFAULT 'pending',
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id)
        );
        CREATE TABLE IF NOT EXISTS heat_lanes (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            heat_id   INTEGER NOT NULL,
            lane_no   INTEGER NOT NULL,
            entry_id  INTEGER NOT NULL,
            FOREIGN KEY (heat_id) REFERENCES heats(id),
            FOREIGN KEY (entry_id) REFERENCES entries(id)
        );
        CREATE TABLE IF NOT EXISTS brackets (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id  INTEGER NOT NULL,
            round          INTEGER NOT NULL,
            match_no       INTEGER NOT NULL,
            entry1_id      INTEGER,
            entry2_id      INTEGER,
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id)
        );
        CREATE TABLE IF NOT EXISTS match_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            bracket_id      INTEGER NOT NULL UNIQUE,
            winner_entry_id INTEGER,
            recorded_at     TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (bracket_id) REFERENCES brackets(id)
        );
        """)
        await db.commit()

        # ---- カラム追加マイグレーション ----
        async with db.execute("PRAGMA table_info(racers)") as cur:
            cols = [r["name"] for r in await cur.fetchall()]
        if "is_child" not in cols:
            await db.execute("ALTER TABLE racers ADD COLUMN is_child INTEGER DEFAULT 0")
            print("[DB] migration: racers.is_child added")

        async with db.execute("PRAGMA table_info(entries)") as cur:
            cols = [r["name"] for r in await cur.fetchall()]
        if "entry_at" not in cols:
            await db.execute("ALTER TABLE entries ADD COLUMN entry_at TEXT")
            print("[DB] migration: entries.entry_at added")

        # ---- heat_results スキーマ確認・作り直し ----
        async with db.execute("PRAGMA table_info(heat_results)") as cur:
            hr_cols = [r["name"] for r in await cur.fetchall()]

        if not hr_cols:
            # テーブルなし → 新規作成
            await db.execute("""
                CREATE TABLE heat_results (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    heat_lane_id  INTEGER NOT NULL UNIQUE,
                    lap_count     INTEGER DEFAULT 0,
                    best_time     REAL,
                    rank          INTEGER,
                    points        INTEGER DEFAULT 0,
                    win           INTEGER DEFAULT NULL,
                    FOREIGN KEY (heat_lane_id) REFERENCES heat_lanes(id)
                )
            """)
            print("[DB] migration: heat_results created")
        elif "heat_lane_id" not in hr_cols:
            # 旧スキーマ(heat_id+entry_id) → バックアップして再作成
            await db.execute("ALTER TABLE heat_results RENAME TO heat_results_old")
            await db.execute("""
                CREATE TABLE heat_results (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    heat_lane_id  INTEGER NOT NULL UNIQUE,
                    lap_count     INTEGER DEFAULT 0,
                    best_time     REAL,
                    rank          INTEGER,
                    points        INTEGER DEFAULT 0,
                    win           INTEGER DEFAULT NULL,
                    FOREIGN KEY (heat_lane_id) REFERENCES heat_lanes(id)
                )
            """)
            await db.execute("DROP TABLE heat_results_old")
            print("[DB] migration: heat_results schema updated")

        # entries.seeded カラム
        async with db.execute("PRAGMA table_info(entries)") as cur:
            e_cols2 = [r["name"] for r in await cur.fetchall()]
        if "seeded" not in e_cols2:
            await db.execute("ALTER TABLE entries ADD COLUMN seeded INTEGER DEFAULT 0")
            print("[DB] migration: entries.seeded added")

        # entries.advanced カラム
        async with db.execute("PRAGMA table_info(entries)") as cur:
            e_cols = [r["name"] for r in await cur.fetchall()]
        if "advanced" not in e_cols:
            await db.execute("ALTER TABLE entries ADD COLUMN advanced INTEGER DEFAULT NULL")
            print("[DB] migration: entries.advanced added")

        # tournaments 追加カラム
        async with db.execute("PRAGMA table_info(tournaments)") as cur:
            t_cols = [r["name"] for r in await cur.fetchall()]
        migrations = {
            "time_slot": "TEXT DEFAULT 'day'",
            "time_slot_free": "TEXT",
            "qual_heat_count": "INTEGER DEFAULT 1",
            "qual_heat_advance": "INTEGER DEFAULT 2",
            "qual_group_count": "INTEGER DEFAULT 1",
            "qual_group_advance": "INTEGER DEFAULT 2",
            "qual_heat_final": "INTEGER DEFAULT 0",
            "qual_heat_final_advance": "INTEGER DEFAULT 1",
            "qual_final_advance": "INTEGER DEFAULT 2",
            "point_1st": "INTEGER DEFAULT 3",
            "point_2nd": "INTEGER DEFAULT 2",
            "point_3rd": "INTEGER DEFAULT 1",
            "point_co": "INTEGER DEFAULT 0",
            "qual_round_count": "INTEGER DEFAULT 1",
            "heat_locks": "TEXT",
        }
        for col, typedef in migrations.items():
            if col not in t_cols:
                await db.execute(f"ALTER TABLE tournaments ADD COLUMN {col} {typedef}")
                print(f"[DB] migration: tournaments.{col} added")

        # heat_final テーブル（ヒート優勝トーナメント）
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='heat_finals'") as cur:
            if not await cur.fetchone():
                await db.executescript("""
                CREATE TABLE heat_finals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id INTEGER NOT NULL,
                    round_no INTEGER NOT NULL,
                    group_no INTEGER NOT NULL,
                    slot_no INTEGER NOT NULL,
                    entry_id INTEGER,
                    winner_entry_id INTEGER,
                    rank INTEGER DEFAULT NULL
                );
                """)
                print("[DB] migration: heat_finals created")

        # heat_tournament_rounds テーブル（ヒートごとのミニトーナメント）
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ht_rounds'") as cur:
            if not await cur.fetchone():
                await db.executescript("""
                CREATE TABLE ht_rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id INTEGER NOT NULL,
                    heat_no INTEGER NOT NULL,
                    round_no INTEGER NOT NULL,
                    round_type TEXT DEFAULT 'normal'
                );
                CREATE TABLE ht_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    round_id INTEGER NOT NULL,
                    group_no INTEGER NOT NULL
                );
                CREATE TABLE ht_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    slot_no INTEGER NOT NULL,
                    entry_id INTEGER
                );
                CREATE TABLE ht_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    winner_slot_id INTEGER
                );
                CREATE TABLE ht_slot_ranks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    slot_id INTEGER NOT NULL,
                    rank INTEGER
                );
                """)
                print("[DB] migration: ht_* tables created")

        # ht_rounds に section_no カラムを追加（グループ分け対応）
        async with db.execute("PRAGMA table_info(ht_rounds)") as cur:
            cols = [r[1] for r in await cur.fetchall()]
        if "section_no" not in cols:
            await db.execute("ALTER TABLE ht_rounds ADD COLUMN section_no INTEGER DEFAULT 1")
            await db.commit()
            print("[DB] migration: ht_rounds.section_no added")

        # ht_slots に seed_rank カラムを追加（ヒート決勝の段階シード用）
        # NULL=通常スロット / 1,2,3..=各セクションの何位群が流入する枠か
        async with db.execute("PRAGMA table_info(ht_slots)") as cur:
            cols = [r[1] for r in await cur.fetchall()]
        if "seed_rank" not in cols:
            await db.execute("ALTER TABLE ht_slots ADD COLUMN seed_rank INTEGER")
            await db.commit()
            print("[DB] migration: ht_slots.seed_rank added")

        # bracket_rounds テーブル
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bracket_rounds'") as cur:
            if not await cur.fetchone():
                await db.executescript('''
                CREATE TABLE bracket_rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tournament_id INTEGER NOT NULL,
                    round_no INTEGER NOT NULL,
                    round_type TEXT DEFAULT "normal"
                );
                CREATE TABLE bracket_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    round_id INTEGER NOT NULL,
                    group_no INTEGER NOT NULL
                );
                CREATE TABLE bracket_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    slot_no INTEGER NOT NULL,
                    entry_id INTEGER,
                    is_bye INTEGER DEFAULT 0
                );
                CREATE TABLE bracket_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL UNIQUE,
                    winner_slot_id INTEGER,
                    recorded_at TEXT
                );
                CREATE TABLE bracket_slot_ranks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    slot_id INTEGER NOT NULL,
                    rank INTEGER NOT NULL
                );
                ''')
                print("[DB] migration: bracket tables created")

        # heats.group_no カラム追加
        async with db.execute("PRAGMA table_info(heats)") as cur:
            h_cols = [r["name"] for r in await cur.fetchall()]
        if "group_no" not in h_cols:
            await db.execute("ALTER TABLE heats ADD COLUMN group_no INTEGER DEFAULT 0")
            print("[DB] migration: heats.group_no added")

        # heats.round_no カラム追加
        async with db.execute("PRAGMA table_info(heats)") as cur:
            h_cols2 = [r["name"] for r in await cur.fetchall()]
        if "round_no" not in h_cols2:
            await db.execute("ALTER TABLE heats ADD COLUMN round_no INTEGER DEFAULT 0")
            print("[DB] migration: heats.round_no added")

        # heat_finals.rank カラム追加
        async with db.execute("PRAGMA table_info(heat_finals)") as cur:
            hf_cols = [r["name"] for r in await cur.fetchall()]
        if "rank" not in hf_cols:
            await db.execute("ALTER TABLE heat_finals ADD COLUMN rank INTEGER DEFAULT NULL")
            print("[DB] migration: heat_finals.rank added")
        # heat_finals.final_type カラム追加（'heat': ヒート優勝, 'playoff': 決勝進出決定戦）
        if "final_type" not in hf_cols:
            await db.execute("ALTER TABLE heat_finals ADD COLUMN final_type TEXT DEFAULT 'heat'")
            print("[DB] migration: heat_finals.final_type added")

        # heat_results.win カラム追加
        async with db.execute("PRAGMA table_info(heat_results)") as cur:
            hr2_cols = [r["name"] for r in await cur.fetchall()]
        if hr2_cols and "win" not in hr2_cols:
            await db.execute("ALTER TABLE heat_results ADD COLUMN win INTEGER DEFAULT NULL")
        try:
            await db.execute("ALTER TABLE heat_results ADD COLUMN is_co INTEGER DEFAULT 0")
            print("[DB] migration: heat_results.is_co added")
        except Exception:
            pass
            print("[DB] migration: heat_results.win added")

        # tournaments.bracket_mode カラム追加
        async with db.execute("PRAGMA table_info(tournaments)") as cur:
            t_cols = [r["name"] for r in await cur.fetchall()]
        if "bracket_mode" not in t_cols:
            await db.execute("ALTER TABLE tournaments ADD COLUMN bracket_mode TEXT DEFAULT 'third_place'")
            print("[DB] migration: tournaments.bracket_mode added")
        if "qual_heat_exclude" not in t_cols:
            await db.execute("ALTER TABLE tournaments ADD COLUMN qual_heat_exclude INTEGER DEFAULT 0")
            print("[DB] migration: tournaments.qual_heat_exclude added")
        # 裏トーナメント（敗者復活/ルーザーズブラケット）設定
        #   losers_bracket: 0=無効 / 1=有効
        #   revival_target_round: 裏優勝者の復活先ラウンド番号（後半固定。NULL=決勝扱い）
        if "losers_bracket" not in t_cols:
            await db.execute("ALTER TABLE tournaments ADD COLUMN losers_bracket INTEGER DEFAULT 0")
            print("[DB] migration: tournaments.losers_bracket added")
        if "revival_target_round" not in t_cols:
            await db.execute("ALTER TABLE tournaments ADD COLUMN revival_target_round INTEGER DEFAULT NULL")
            print("[DB] migration: tournaments.revival_target_round added")
        # くじ引き配置：placement_method='auto'/'lottery'、lottery_pending=1で割り当て中
        if "placement_method" not in t_cols:
            await db.execute("ALTER TABLE tournaments ADD COLUMN placement_method TEXT DEFAULT 'auto'")
            print("[DB] migration: tournaments.placement_method added")
        if "lottery_pending" not in t_cols:
            await db.execute("ALTER TABLE tournaments ADD COLUMN lottery_pending INTEGER DEFAULT 0")
            print("[DB] migration: tournaments.lottery_pending added")
        # heats.deciding_position カラム追加（即決勝総当たりの決定戦）
        async with db.execute("PRAGMA table_info(heats)") as cur:
            h_cols = {r["name"] async for r in cur}
        if "deciding_position" not in h_cols:
            await db.execute("ALTER TABLE heats ADD COLUMN deciding_position INTEGER DEFAULT NULL")
            print("[DB] migration: heats.deciding_position added")
        # entries.none_rr_rank カラム追加（即決勝総当たりの最終順位）
        async with db.execute("PRAGMA table_info(entries)") as cur:
            e_cols2 = {r["name"] async for r in cur}
        if "none_rr_rank" not in e_cols2:
            await db.execute("ALTER TABLE entries ADD COLUMN none_rr_rank INTEGER DEFAULT NULL")
            print("[DB] migration: entries.none_rr_rank added")

        # app_settings テーブル（アプリ全体設定）
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings'") as cur:
            if not await cur.fetchone():
                await db.execute("""
                    CREATE TABLE app_settings (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                """)
                await db.execute(
                    "INSERT INTO app_settings (key, value) VALUES ('regulations', ?)",
                    ('["オープンクラス", "ジュニアクラス", "ストッククラス", "ストッククラス（Jr.の部）", "B-MAX", "GT-Advance", "巣組", "ノーマルモーター限定", "チューンモーター限定", "ライトダッシュ限定", "ハイパーダッシュ限定", "片軸限定", "片軸限定（Jr.の部）"]',)
                )
                print("[DB] migration: app_settings created")

        # certificate_templates テーブル（賞状テンプレート管理）
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='certificate_templates'") as cur:
            if not await cur.fetchone():
                await db.execute("""
                    CREATE TABLE certificate_templates (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        name        TEXT NOT NULL,
                        paper_size  TEXT DEFAULT 'A4',
                        orientation TEXT DEFAULT 'portrait',
                        layout_json TEXT DEFAULT '{}',
                        created_at  TEXT DEFAULT (datetime('now','localtime')),
                        updated_at  TEXT DEFAULT (datetime('now','localtime'))
                    )
                """)
                await db.execute("""
                    INSERT INTO certificate_templates (name, paper_size, orientation)
                    VALUES ('デフォルト', 'A4', 'portrait')
                """)
                print("[DB] migration: certificate_templates created")

        # post_templates テーブル（ポストテンプレート管理）
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='post_templates'") as cur:
            if not await cur.fetchone():
                await db.execute("""
                    CREATE TABLE post_templates (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        name        TEXT NOT NULL,
                        body        TEXT NOT NULL DEFAULT '',
                        created_at  TEXT DEFAULT (datetime('now','localtime')),
                        updated_at  TEXT DEFAULT (datetime('now','localtime'))
                    )
                """)
                await db.execute(
                    "INSERT INTO post_templates (name, body) VALUES (?, ?)",
                    ('デフォルト', "本日もたくさんのご参加ありがとうございました。\n\n{regulation}\n🥇 {rank1} さん\n🥈 {rank2} さん\n🥉 {rank3} さん\n\n#ミニ四駆\n#福岡")
                )
                print("[DB] migration: post_templates created")

        # card_templates テーブル（QR/バーコード印刷テンプレート管理。賞状テンプレートと同型）
        # 既定テンプレート（名刺サイズ・QR）。エディタのdefaultObjsと同一の配置を持つ。
        import json as _cardjson
        DEFAULT_CARD_LAYOUT = _cardjson.dumps({
            "bg_image_url": "",
            "objects": [
                {"id": "race_name", "label": "大会名", "field": "{race_name}",
                 "preview": "○○カップ オープンクラス",
                 "x": 12, "y": 8, "w": 320, "h": 22,
                 "fontSize": 13, "fontFamily": "sans-serif", "fontWeight": "bold", "fontStyle": "normal",
                 "color": "#1a1a1a", "align": "center", "verticalAlign": "middle", "letterSpacing": 0, "lineHeight": 1.2},
                {"id": "code", "type": "code", "label": "コード（QR/バーコード）",
                 "x": 122, "y": 34, "w": 100, "h": 100},
                {"id": "seq", "label": "連番", "field": "{seq}",
                 "preview": "0001234567",
                 "x": 12, "y": 140, "w": 320, "h": 18,
                 "fontSize": 12, "fontFamily": "monospace", "fontWeight": "normal", "fontStyle": "normal",
                 "color": "#333333", "align": "center", "verticalAlign": "middle", "letterSpacing": 1, "lineHeight": 1.2},
                {"id": "racer_name", "label": "レーサー名", "field": "{racer_name}",
                 "preview": "山田 太郎",
                 "x": 12, "y": 162, "w": 320, "h": 36,
                 "fontSize": 20, "fontFamily": "sans-serif", "fontWeight": "bold", "fontStyle": "normal",
                 "color": "#1a1a1a", "align": "center", "verticalAlign": "middle", "letterSpacing": 1, "lineHeight": 1.2},
            ],
        }, ensure_ascii=False)

        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='card_templates'") as cur:
            if not await cur.fetchone():
                await db.execute("""
                    CREATE TABLE card_templates (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        name        TEXT NOT NULL,
                        card_size   TEXT DEFAULT 'meishi',
                        code_type   TEXT DEFAULT 'qr',
                        layout_json TEXT DEFAULT '{}',
                        created_at  TEXT DEFAULT (datetime('now','localtime')),
                        updated_at  TEXT DEFAULT (datetime('now','localtime'))
                    )
                """)
                # 中身入りの既定テンプレート（名刺・QR）を作成
                await db.execute(
                    "INSERT INTO card_templates (name, card_size, code_type, layout_json) VALUES (?, ?, ?, ?)",
                    ('デフォルト（名刺QR）', 'meishi', 'qr', DEFAULT_CARD_LAYOUT)
                )
                print("[DB] migration: card_templates created (with default)")

        # 旧バージョンで自動作成された「空のデフォルト」テンプレートを掃除（白カード対策・冪等）。
        # objects未設定（layout_jsonが空/{}）かつ名前が'デフォルト'の行のみ削除。利用者作成分は残す。
        try:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='card_templates'"
            ) as cur:
                has_table = await cur.fetchone()
            if has_table:
                cur2 = await db.execute(
                    "DELETE FROM card_templates "
                    "WHERE name='デフォルト' AND (layout_json IS NULL OR TRIM(layout_json) IN ('','{}'))"
                )
                if cur2.rowcount:
                    print(f"[DB] migration: removed {cur2.rowcount} empty default card_template(s)")
                # テンプレートが1件も無ければ、中身入りの既定テンプレートを補充（初期値になる）
                async with db.execute("SELECT COUNT(*) AS n FROM card_templates") as c3:
                    row = await c3.fetchone()
                if row and row["n"] == 0:
                    await db.execute(
                        "INSERT INTO card_templates (name, card_size, code_type, layout_json) VALUES (?, ?, ?, ?)",
                        ('デフォルト（名刺QR）', 'meishi', 'qr', DEFAULT_CARD_LAYOUT)
                    )
                    print("[DB] migration: inserted default card_template")
        except Exception:
            pass

        # racers.last_visit_at（来店日時）
        async with db.execute("PRAGMA table_info(racers)") as cur:
            r_cols = {r["name"] async for r in cur}
        if "last_visit_at" not in r_cols:
            await db.execute("ALTER TABLE racers ADD COLUMN last_visit_at TEXT DEFAULT NULL")
            print("[DB] migration: racers.last_visit_at added")

        # 料金設定・today_day_type
        async with db.execute("SELECT value FROM app_settings WHERE key='pricing_enabled'") as cur:
            if not await cur.fetchone():
                import json as _json
                await db.execute("INSERT INTO app_settings (key, value) VALUES ('pricing_enabled', '0')")
                await db.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('pricing_rounding', 'floor')")
                empty_pricing = _json.dumps({
                    "adult": {"weekday": {"hour":"","free":"","race":""},
                              "saturday": {"hour":"","free":"","race":""},
                              "sunday": {"hour":"","free":"","race":""},
                              "holiday": {"hour":"","free":"","race":""},
                              "special": {"hour":"","free":"","race":""}},
                    "child": {"weekday": {"hour":"","free":"","race":""},
                              "saturday": {"hour":"","free":"","race":""},
                              "sunday": {"hour":"","free":"","race":""},
                              "holiday": {"hour":"","free":"","race":""},
                              "special": {"hour":"","free":"","race":""}}
                }, ensure_ascii=False)
                await db.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('pricing_table', ?)", (empty_pricing,))
                print("[DB] migration: pricing settings initialized")

        # today_day_type（当日の料金区分）
        async with db.execute("SELECT value FROM app_settings WHERE key='today_day_type'") as cur:
            if not await cur.fetchone():
                await db.execute("INSERT INTO app_settings (key, value) VALUES ('today_day_type', '')")
                print("[DB] migration: today_day_type initialized")

        # ht_finalist_seeds テーブル（ヒートトーナメント複数回出場時の枠単位シード管理）
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ht_finalist_seeds'") as cur:
            if not await cur.fetchone():
                await db.execute("""
                    CREATE TABLE ht_finalist_seeds (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        tournament_id   INTEGER NOT NULL,
                        entry_id        INTEGER NOT NULL,
                        heat_no         INTEGER NOT NULL,
                        seeded          INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(tournament_id, entry_id, heat_no)
                    )
                """)
                print("[DB] migration: ht_finalist_seeds created")

        # bracket_slots.seed_reserved（シード/スーパーシード用予約スロットフラグ）
        async with db.execute("PRAGMA table_info(bracket_slots)") as cur:
            bs_cols = {r["name"] async for r in cur}
        if "seed_reserved" not in bs_cols:
            await db.execute("ALTER TABLE bracket_slots ADD COLUMN seed_reserved INTEGER DEFAULT 0")
            print("[DB] migration: bracket_slots.seed_reserved added")
        # くじ引き配置：lottery_no（区分=登場ラウンド内で1から独立採番）
        if "lottery_no" not in bs_cols:
            await db.execute("ALTER TABLE bracket_slots ADD COLUMN lottery_no INTEGER DEFAULT NULL")
            print("[DB] migration: bracket_slots.lottery_no added")

        # bracket_groups.advance_to_slot_id（固定リンク方式・v5.7+）
        # 生成時に「このグループの勝者が進む先のスロットID」を焼き付ける。
        # NULLのままの既存トーナメントは従来の動的進行ロジックで動作する（後方互換）。
        async with db.execute("PRAGMA table_info(bracket_groups)") as cur:
            bg_cols = {r["name"] async for r in cur}
        if "advance_to_slot_id" not in bg_cols:
            await db.execute("ALTER TABLE bracket_groups ADD COLUMN advance_to_slot_id INTEGER DEFAULT NULL")
            print("[DB] migration: bracket_groups.advance_to_slot_id added")

        # ht_groups.advance_to_slot_id（予選ヒートトーナメント用 固定リンク・v5.7+）
        # 次ラウンド生成時に焼き付け。NULLの既存ヒートは従来の位置対応で動作（後方互換）。
        async with db.execute("PRAGMA table_info(ht_groups)") as cur:
            hg_cols = {r["name"] async for r in cur}
        if "advance_to_slot_id" not in hg_cols:
            await db.execute("ALTER TABLE ht_groups ADD COLUMN advance_to_slot_id INTEGER DEFAULT NULL")
            print("[DB] migration: ht_groups.advance_to_slot_id added")

        # racers.uid（店舗をまたいで同一人物を識別するグローバル一意ID / 第12.2章）
        async with db.execute("PRAGMA table_info(racers)") as cur:
            r_cols2 = {r["name"] async for r in cur}
        if "uid" not in r_cols2:
            import uuid as _uuid
            await db.execute("ALTER TABLE racers ADD COLUMN uid TEXT")
            # 既存全レーサーへUUID v4をバックフィル
            async with db.execute("SELECT id FROM racers WHERE uid IS NULL OR uid = ''") as cur:
                ids_to_fill = [row["id"] async for row in cur]
            for rid in ids_to_fill:
                await db.execute("UPDATE racers SET uid = ? WHERE id = ?", (str(_uuid.uuid4()), rid))
            # 一意インデックス（UNIQUE制約相当）
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_racers_uid ON racers(uid)")
            print(f"[DB] migration: racers.uid added (backfilled {len(ids_to_fill)} rows)")

        # racers.is_regular（常連＝いつもの常連メンバー / 運営者・常連フラグ）
        async with db.execute("PRAGMA table_info(racers)") as cur:
            r_cols3 = {r["name"] async for r in cur}
        if "is_regular" not in r_cols3:
            await db.execute("ALTER TABLE racers ADD COLUMN is_regular INTEGER DEFAULT 0")
            print("[DB] migration: racers.is_regular added")

        # racers.ephemeral / owner_tournament_id（マスタ非使用エントリー＝隠しレーサー / 仕様書12.1）
        async with db.execute("PRAGMA table_info(racers)") as cur:
            r_cols4 = {r["name"] async for r in cur}
        if "ephemeral" not in r_cols4:
            await db.execute("ALTER TABLE racers ADD COLUMN ephemeral INTEGER DEFAULT 0")
            print("[DB] migration: racers.ephemeral added")
        if "owner_tournament_id" not in r_cols4:
            await db.execute("ALTER TABLE racers ADD COLUMN owner_tournament_id INTEGER DEFAULT NULL")
            print("[DB] migration: racers.owner_tournament_id added")

        # tournaments.use_racer_master（レーサーマスタを使用するか / 仕様書12.1・初期値1）
        async with db.execute("PRAGMA table_info(tournaments)") as cur:
            t_cols_um = {r["name"] async for r in cur}
        if "use_racer_master" not in t_cols_um:
            await db.execute("ALTER TABLE tournaments ADD COLUMN use_racer_master INTEGER DEFAULT 1")
            print("[DB] migration: tournaments.use_racer_master added")

        # tournaments.pre_entry / pre_entry_method（事前エントリー設定 / 作成時のみ確定）
        #   pre_entry        : 0=OFF（既定） / 1=ON
        #   pre_entry_method : NULL（既定） / 'manual'（手動） / 'form'（エントリーフォーム）
        async with db.execute("PRAGMA table_info(tournaments)") as cur:
            t_cols_pe = {r["name"] async for r in cur}
        if "pre_entry" not in t_cols_pe:
            await db.execute("ALTER TABLE tournaments ADD COLUMN pre_entry INTEGER DEFAULT 0")
            print("[DB] migration: tournaments.pre_entry added")
        if "pre_entry_method" not in t_cols_pe:
            await db.execute("ALTER TABLE tournaments ADD COLUMN pre_entry_method TEXT DEFAULT NULL")
            print("[DB] migration: tournaments.pre_entry_method added")
        # pre_entry_deadline（エントリーフォーム方式の受付締切日時 / 'YYYY-MM-DDTHH:MM'）
        #   NULL＝締切なし。これを過ぎると公開フォーム（/entry）は受付終了表示にする。
        if "pre_entry_deadline" not in t_cols_pe:
            await db.execute("ALTER TABLE tournaments ADD COLUMN pre_entry_deadline TEXT DEFAULT NULL")
            print("[DB] migration: tournaments.pre_entry_deadline added")

        # tournaments.time_schedule（タイムスケジュール本文 / フリーワード）
        async with db.execute("PRAGMA table_info(tournaments)") as cur:
            t_cols_ts = {r["name"] async for r in cur}
        if "time_schedule" not in t_cols_ts:
            await db.execute("ALTER TABLE tournaments ADD COLUMN time_schedule TEXT DEFAULT ''")
            print("[DB] migration: tournaments.time_schedule added")

        # race_assets（レース情報の画像：コースレイアウト/タイムスケジュール/備考）
        #   kind : 'course'（コースレイアウト）/ 'schedule'（タイムスケジュール）/ 'remarks'（備考）
        #   seq  : 0..3（各種別 最大4枚）
        #   name : 画像の名前（任意）
        #   data_uri : 'data:image/xxx;base64,...'
        await db.execute("""
            CREATE TABLE IF NOT EXISTS race_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                seq INTEGER NOT NULL,
                name TEXT DEFAULT '',
                data_uri TEXT NOT NULL,
                UNIQUE(tournament_id, kind, seq)
            )
        """)

        # pre_entries（事前エントリー＝本エントリーの前段階テーブル / 事前エントリーON時に使用）
        #   seq_no            : レース内の連番（エントリーカードのバーコード値。登録順に自動採番）
        #   name / yomi       : レーサー名・よみがな（ステップ1では name のみ使用）
        #   is_child          : 0=大人 / 1=子供
        #   prefecture        : 都道府県
        #   contact_type/contact : 連絡先種別（mail/phone/x）・連絡先（代表者のみ）
        #   is_representative : 代表者フラグ（連絡先による自己訂正の照合用）
        #   checked_in        : 受付済みフラグ（当日スキャンで本エントリーへ昇格＝ステップ2で使用）
        await db.execute("""
        CREATE TABLE IF NOT EXISTS pre_entries (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id     INTEGER NOT NULL,
            seq_no            INTEGER,
            name              TEXT NOT NULL,
            yomi              TEXT,
            is_child          INTEGER DEFAULT 0,
            prefecture        TEXT,
            contact_type      TEXT,
            contact           TEXT,
            is_representative INTEGER DEFAULT 0,
            checked_in        INTEGER DEFAULT 0,
            is_walkin         INTEGER DEFAULT 0,
            created_at        TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id)
        )""")

        # entries.pre_seq_no（事前エントリー由来の連番＝エントリーカードのバーコード番号）
        #   受付スキャンで本エントリーへ昇格する際に pre_entries.seq_no を引き継ぐ。
        #   手動・他レースコピー等で連番を持たない本エントリーは NULL。
        async with db.execute("PRAGMA table_info(entries)") as cur:
            e_cols = {r["name"] async for r in cur}
        if "pre_seq_no" not in e_cols:
            await db.execute("ALTER TABLE entries ADD COLUMN pre_seq_no INTEGER DEFAULT NULL")
            print("[DB] migration: entries.pre_seq_no added")

        # pre_entries.is_walkin（飛び込み参加フラグ）
        async with db.execute("PRAGMA table_info(pre_entries)") as cur:
            pe_cols = {r["name"] async for r in cur}
        if "is_walkin" not in pe_cols:
            await db.execute("ALTER TABLE pre_entries ADD COLUMN is_walkin INTEGER DEFAULT 0")
            print("[DB] migration: pre_entries.is_walkin added")

        # ── order（並び順（ポイント制））関連 ──────────────────────────
        # tournaments.order_round_mode  : 'free'（フリー走行制）/ 'round'（ラウンド制）
        # tournaments.order_round_count : ラウンド制の規定ラウンド数（「もう1回」追加で増える）
        async with db.execute("PRAGMA table_info(tournaments)") as cur:
            t_cols_o = {r["name"] async for r in cur}
        if "order_round_mode" not in t_cols_o:
            await db.execute("ALTER TABLE tournaments ADD COLUMN order_round_mode TEXT DEFAULT 'free'")
            print("[DB] migration: tournaments.order_round_mode added")
        if "order_round_count" not in t_cols_o:
            await db.execute("ALTER TABLE tournaments ADD COLUMN order_round_count INTEGER DEFAULT 3")
            print("[DB] migration: tournaments.order_round_count added")
        if "order_current_round" not in t_cols_o:
            await db.execute("ALTER TABLE tournaments ADD COLUMN order_current_round INTEGER DEFAULT 1")
            print("[DB] migration: tournaments.order_current_round added")
        if "order_free_max_runs" not in t_cols_o:
            await db.execute("ALTER TABLE tournaments ADD COLUMN order_free_max_runs INTEGER DEFAULT NULL")
            print("[DB] migration: tournaments.order_free_max_runs added")

        # order_queue（並び順予選の待機列）
        #   scan_seq  : スキャンされた先着順（レース内・ラウンド内で単調増加）
        #   round_no  : 所属ラウンド（フリー制は常に1）
        #   consumed  : 0=待機中 / 1=組へ消化済み
        #   heat_id   : 消化先の heats.id（未消化は NULL）

        # entry_form_tokens（公開エントリーフォーム /entry の二重サブミット防止トークン）
        #   token       : フォーム表示時に発行する使い捨てトークン（UUID）
        #   tournament_id: 対象レース
        #   used        : 0=未使用 / 1=使用済み（送信成功で1に更新）
        #   created_at  : 発行時刻（古いトークンの掃除用）
        await db.execute("""
        CREATE TABLE IF NOT EXISTS entry_form_tokens (
            token          TEXT PRIMARY KEY,
            tournament_id  INTEGER NOT NULL,
            used           INTEGER DEFAULT 0,
            created_at     TEXT DEFAULT (datetime('now','localtime'))
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS order_queue (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id  INTEGER NOT NULL,
            entry_id       INTEGER NOT NULL,
            scan_seq       INTEGER NOT NULL,
            round_no       INTEGER DEFAULT 1,
            consumed       INTEGER DEFAULT 0,
            heat_id        INTEGER DEFAULT NULL,
            created_at     TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id),
            FOREIGN KEY (entry_id) REFERENCES entries(id)
        )""")

        # tournaments.order_status : order予選の締め状態（NULL/''=進行中, 'closed'=予選終了）
        if "order_status" not in t_cols_o:
            await db.execute("ALTER TABLE tournaments ADD COLUMN order_status TEXT DEFAULT NULL")
            print("[DB] migration: tournaments.order_status added")

        # ── order_winner（並び順（勝ち抜け））関連 ──────────────────────────
        # パターンA（先着・勝ち抜け型）の多段予選。
        #   各段階（1次/2次/3次…）ごとに「勝ち抜け勝利数・最大走行回数・通過人数」を設定。
        #   1組から1着は最大1人（0人＝全員COもあり）。規定勝利数に達したら通過。
        #   最大走行回数を使い切ると敗退。通過者が通過人数に達した瞬間に段階を強制終了。
        #   枠割れ時は「もう1周追加」で未通過者の最大走行回数を+1して再走させる。
        #   段階が変わったら勝利数・走行回数はリセット（stage_no ごとに別レコード）。
        #
        # tournaments.order_winner_stage_count   : 予選段階数（1次のみ=1 / 1・2次=2 …）
        # tournaments.order_winner_current_stage : 進行中の段階番号（1始まり）
        if "order_winner_stage_count" not in t_cols_o:
            await db.execute("ALTER TABLE tournaments ADD COLUMN order_winner_stage_count INTEGER DEFAULT 1")
            print("[DB] migration: tournaments.order_winner_stage_count added")
        if "order_winner_current_stage" not in t_cols_o:
            await db.execute("ALTER TABLE tournaments ADD COLUMN order_winner_current_stage INTEGER DEFAULT 1")
            print("[DB] migration: tournaments.order_winner_current_stage added")

        # order_queue.stage_no : 勝ち抜けの待機列がどの段階のものかを区別する
        #   （並び順（ポイント制）は round_no を使うが、勝ち抜けは段階概念が別なので stage_no を併設）
        async with db.execute("PRAGMA table_info(order_queue)") as cur:
            oq_cols = {r["name"] async for r in cur}
        if "stage_no" not in oq_cols:
            await db.execute("ALTER TABLE order_queue ADD COLUMN stage_no INTEGER DEFAULT NULL")
            print("[DB] migration: order_queue.stage_no added")

        # order_winner_stages（段階ごとの設定・状態）
        #   stage_no      : 段階番号（1,2,3…）
        #   win_target    : 勝ち抜けに必要な1着回数（既定1）
        #   max_runs      : 最大走行回数（「もう1周追加」で +1 され得る）
        #   advance_count : 通過人数（先着N名。最終段階は＝決勝進出人数）
        #   status        : 'pending'（未開始）/ 'running'（進行中）/ 'closed'（通過確定・締め）
        await db.execute("""
        CREATE TABLE IF NOT EXISTS order_winner_stages (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id  INTEGER NOT NULL,
            stage_no       INTEGER NOT NULL,
            win_target     INTEGER DEFAULT 1,
            max_runs       INTEGER DEFAULT 3,
            advance_count  INTEGER DEFAULT 3,
            status         TEXT DEFAULT 'pending',
            created_at     TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE (tournament_id, stage_no),
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id)
        )""")

        # order_winner_racers（段階×レーサーの累計・状態＝正）
        #   同一 tournament × stage × entry で1行。段階が変わると別 stage_no の行になる
        #   （＝勝利数・走行回数は段階ごとにリセットされる）。
        #   wins   : その段階での1着回数（1着入力で +1）
        #   runs   : その段階での走行回数（組確定で +1）
        #   status : 'racing'（挑戦中）/ 'passed'（通過）/ 'eliminated'（敗退＝上限消化）
        await db.execute("""
        CREATE TABLE IF NOT EXISTS order_winner_racers (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id  INTEGER NOT NULL,
            stage_no       INTEGER NOT NULL,
            entry_id       INTEGER NOT NULL,
            wins           INTEGER DEFAULT 0,
            runs           INTEGER DEFAULT 0,
            status         TEXT DEFAULT 'racing',
            passed_seq     INTEGER DEFAULT NULL,
            created_at     TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE (tournament_id, stage_no, entry_id),
            FOREIGN KEY (tournament_id) REFERENCES tournaments(id),
            FOREIGN KEY (entry_id) REFERENCES entries(id)
        )""")

        # ---- インデックス（冪等）------------------------------------------
        # 主キー以外は全表スキャンになるため、当日の高頻度アクセス経路（結果入力・
        # 観覧HTML生成）で参照される外部キー列にインデックスを張る。
        # すべて IF NOT EXISTS のため、既存DBに対しても起動時に自動で追加される。
        # 上の全テーブル定義・マイグレーションが終わった後に一括作成する。
        await db.executescript("""
        CREATE INDEX IF NOT EXISTS idx_entries_tid           ON entries(tournament_id);
        CREATE INDEX IF NOT EXISTS idx_entries_racer         ON entries(racer_id);
        CREATE INDEX IF NOT EXISTS idx_heats_tid_round       ON heats(tournament_id, round_no);
        CREATE INDEX IF NOT EXISTS idx_heat_lanes_heat       ON heat_lanes(heat_id);
        CREATE INDEX IF NOT EXISTS idx_heat_lanes_entry      ON heat_lanes(entry_id);
        CREATE INDEX IF NOT EXISTS idx_heat_results_lane     ON heat_results(heat_lane_id);
        CREATE INDEX IF NOT EXISTS idx_heat_finals_tid       ON heat_finals(tournament_id);
        CREATE INDEX IF NOT EXISTS idx_ht_rounds_tid         ON ht_rounds(tournament_id, heat_no);
        CREATE INDEX IF NOT EXISTS idx_ht_groups_round       ON ht_groups(round_id);
        CREATE INDEX IF NOT EXISTS idx_ht_slots_group        ON ht_slots(group_id);
        CREATE INDEX IF NOT EXISTS idx_ht_results_group      ON ht_results(group_id);
        CREATE INDEX IF NOT EXISTS idx_ht_slot_ranks_group   ON ht_slot_ranks(group_id);
        CREATE INDEX IF NOT EXISTS idx_bracket_rounds_tid    ON bracket_rounds(tournament_id, round_no);
        CREATE INDEX IF NOT EXISTS idx_bracket_groups_round  ON bracket_groups(round_id);
        CREATE INDEX IF NOT EXISTS idx_bracket_slots_group   ON bracket_slots(group_id);
        CREATE INDEX IF NOT EXISTS idx_bracket_results_group ON bracket_results(group_id);
        CREATE INDEX IF NOT EXISTS idx_bracket_slot_ranks_g  ON bracket_slot_ranks(group_id);
        CREATE INDEX IF NOT EXISTS idx_pre_entries_tid       ON pre_entries(tournament_id);
        CREATE INDEX IF NOT EXISTS idx_order_queue_tid       ON order_queue(tournament_id, round_no, consumed);
        CREATE INDEX IF NOT EXISTS idx_order_queue_stage     ON order_queue(tournament_id, stage_no);
        CREATE INDEX IF NOT EXISTS idx_ow_stages_tid         ON order_winner_stages(tournament_id, stage_no);
        CREATE INDEX IF NOT EXISTS idx_ow_racers_tid         ON order_winner_racers(tournament_id, stage_no);
        CREATE INDEX IF NOT EXISTS idx_race_assets_tid       ON race_assets(tournament_id);
        """)

        await db.commit()

    print(f"[DB] ready: {db_path}")
# NOTE: certificate_templates migration appended below in init_db flow
