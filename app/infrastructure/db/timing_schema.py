"""タイミング計測用スキーマ（端末台帳・コースレイアウト）。

既存の schema.py を汚さないよう、テーブル定義と12台の自動投入を
このモジュールに分離する。init_db の末尾から ensure_timing_schema() を
1行呼ぶだけで有効化できる（冪等）。

14章:
  DA6  台帳（timing_devices）と地図（timing_layouts/elements）の分離
  DA9  ノードID＝機体番号・固定12台（SQ0-5 / GW6,7 / RC8,9 / SG10,11）
"""

import aiosqlite


# 固定12台（DA9）。ノードID順。
FIXED_DEVICES = [
    (0, "SQ", "SQ0"),
    (1, "SQ", "SQ1"),
    (2, "SQ", "SQ2"),
    (3, "SQ", "SQ3"),
    (4, "SQ", "SQ4"),
    (5, "SQ", "SQ5"),
    (6, "GW", "GW6"),
    (7, "GW", "GW7"),
    (8, "RC", "RC8"),
    (9, "RC", "RC9"),
    (10, "SG", "SG10"),
    (11, "SG", "SG11"),
]


async def ensure_timing_schema(db: aiosqlite.Connection) -> None:
    """タイミング用テーブルを作成し、固定12台を投入する（冪等）。"""
    await db.executescript("""
        -- 端末台帳（実機の名簿・DA9）。node_id は機体番号と一致・固定。
        CREATE TABLE IF NOT EXISTS timing_devices (
            node_id     INTEGER PRIMARY KEY,   -- 0..11
            kind        TEXT NOT NULL,         -- 'SQ'/'GW'/'RC'/'SG'
            label       TEXT NOT NULL,         -- 表示名（例 "SQ0"）
            mac         TEXT,                  -- 実機MAC（任意・後で登録）
            note        TEXT,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );

        -- コースレイアウト（地図の見出し・DA6）
        CREATE TABLE IF NOT EXISTS timing_layouts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            target_laps INTEGER NOT NULL DEFAULT 3,  -- 3の倍数・最大9
            lap_length_m REAL,                       -- 1周の距離(m)。ラップ平均速度の算出に使う
            created_at  TEXT DEFAULT (datetime('now','localtime')),
            updated_at  TEXT DEFAULT (datetime('now','localtime'))
        );

        -- レイアウト要素（通過順の並び・DA6）。地図が台帳を node_id で参照。
        CREATE TABLE IF NOT EXISTS timing_layout_elements (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            layout_id   INTEGER NOT NULL,
            position    INTEGER NOT NULL,      -- 0,1,2... 通過順
            kind        TEXT NOT NULL,         -- 'SG'/'SQ'/'LC'
            node_id     INTEGER,               -- SG/SQ のとき台帳参照（LCはNULL）
            beam_gap_mm REAL,                  -- 2本のビームの間隔(mm)。通過速度の算出に使う
            FOREIGN KEY (layout_id) REFERENCES timing_layouts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_timing_layout_elems
            ON timing_layout_elements(layout_id, position);

        -- レース（独立・DA5）。GWのヒートIDで管理。既存 heats とは当面切り離す。
        -- いずれトーナメントの heat_id と橋渡しする（timing_races.heat_id を後で使う）。
        CREATE TABLE IF NOT EXISTS timing_races (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            heat_tag     INTEGER,               -- GWが貼るヒートID（対戦の識別）
            layout_id    INTEGER,               -- どのコースで走ったか
            target_laps  INTEGER NOT NULL DEFAULT 3,
            green_t_us   INTEGER,               -- 緑時刻（NULLなら走行式・DA4）
            heat_id      INTEGER,               -- 将来：既存トーナメントheatへの橋渡し用
            created_at   TEXT DEFAULT (datetime('now','localtime'))
        );

        -- 通過イベント（GWが記録した材料・DA3）。冪等キーは D12。
        CREATE TABLE IF NOT EXISTS timing_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id      INTEGER NOT NULL,
            device_id    TEXT NOT NULL,         -- GW識別（冪等キーの一部・D12）
            src          INTEGER NOT NULL,      -- 発生ノードID（ゲート）
            src_boot_id  INTEGER NOT NULL,      -- 発生ノードのboot_id（D12）
            seq          INTEGER NOT NULL,      -- 発生ノードごとの通番（D12）
            lane         INTEGER NOT NULL,      -- 物理レーン 1..3
            t_us         INTEGER NOT NULL,      -- ビームA打刻（GW時刻）
            t_us_b       INTEGER,               -- ビームB打刻（速度用・任意）
            quality      INTEGER DEFAULT 0,
            -- 冪等キー（D12）: 同一イベントの再送を弾く
            UNIQUE (device_id, src, src_boot_id, seq),
            FOREIGN KEY (race_id) REFERENCES timing_races(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_timing_events_race
            ON timing_events(race_id);

        -- ベスト記録の保持（毎回の再計算をやめ、受信時に更新する）
        --   scope     : 'day'（その日）/ 'race'（そのレース内）
        --   scope_key : 'YYYY-MM-DD' / レースID(文字列)
        --   metric    : total / max_ms / lap / lap_avg / sector / sector_ms
        --   rank      : 1..3（上位3傑を保持。画面で色分けするため）
        --   value     : 秒 または m/s（タイム系は最小・速度系は最大が「ベスト」）
        -- 期間指定などの集計は、このテーブルを走査せず timing_events から
        -- 別途まとめて計算する（リアルタイム性を求めないため）。
        CREATE TABLE IF NOT EXISTS timing_bests (
            scope       TEXT NOT NULL,
            scope_key   TEXT NOT NULL,
            metric      TEXT NOT NULL,
            rank        INTEGER NOT NULL DEFAULT 1,   -- 1=最良 2=2番目 3=3番目
            value       REAL NOT NULL,
            race_id     INTEGER,            -- どのレースで出た記録か
            start_lane  INTEGER,            -- どのマシンか（スタートレーン）
            lap         INTEGER,            -- 何周目か（lap/sector系のみ）
            sector_no   INTEGER,            -- 何番セクターか（sector系のみ）
            updated_at  TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (scope, scope_key, metric, rank)
        );

        CREATE INDEX IF NOT EXISTS idx_timing_bests_scope
            ON timing_bests(scope, scope_key);
    """)

    # --- 速度算出用カラムのマイグレーション ---
    # lap_length_m（1周の距離）と beam_gap_mm（ビーム間隔）は後から追加したため、
    # 既存DBには存在しない。無ければ ALTER TABLE で足す（データは保持される）。
    async with db.execute("PRAGMA table_info(timing_layouts)") as cur:
        _cols = {r[1] for r in await cur.fetchall()}
    if "lap_length_m" not in _cols:
        await db.execute("ALTER TABLE timing_layouts ADD COLUMN lap_length_m REAL")
        await db.commit()

    async with db.execute("PRAGMA table_info(timing_layout_elements)") as cur:
        _cols = {r[1] for r in await cur.fetchall()}
    if "beam_gap_mm" not in _cols:
        await db.execute("ALTER TABLE timing_layout_elements ADD COLUMN beam_gap_mm REAL")
        await db.commit()

    # --- 反映先の記録（重複反映の警告に使う）---
    # どの計測レースを決勝のどのグループへ反映したかを保持する。
    # 「同じ結果を別のグループにも反映しようとしている」を検出するために必要。
    # （予選ヒートへの反映は既存の heat_id を使う）
    async with db.execute("PRAGMA table_info(timing_races)") as cur:
        _cols = {r[1] for r in await cur.fetchall()}
    if "applied_group_id" not in _cols:
        await db.execute("ALTER TABLE timing_races ADD COLUMN applied_group_id INTEGER")
        await db.commit()

    # --- timing_bests のマイグレーション ---
    # 旧版は上位1件のみ（rank カラムなし）だった。rank が無ければ作り直す。
    # このテーブルは受信時に再構築されるため、作り直しても実害はない。
    async with db.execute("PRAGMA table_info(timing_bests)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    if "rank" not in cols:
        await db.execute("DROP TABLE IF EXISTS timing_bests")
        await db.execute("""
            CREATE TABLE timing_bests (
                scope       TEXT NOT NULL,
                scope_key   TEXT NOT NULL,
                metric      TEXT NOT NULL,
                rank        INTEGER NOT NULL DEFAULT 1,
                value       REAL NOT NULL,
                race_id     INTEGER,
                start_lane  INTEGER,
                lap         INTEGER,
                sector_no   INTEGER,
                updated_at  TEXT DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (scope, scope_key, metric, rank)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_timing_bests_scope "
            "ON timing_bests(scope, scope_key)"
        )
        await db.commit()

    # 固定12台を投入（既にあれば無視＝冪等）
    for node_id, kind, label in FIXED_DEVICES:
        await db.execute(
            "INSERT OR IGNORE INTO timing_devices (node_id, kind, label) "
            "VALUES (?, ?, ?)",
            (node_id, kind, label),
        )

    await db.commit()
