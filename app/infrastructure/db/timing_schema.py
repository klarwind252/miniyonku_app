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
            FOREIGN KEY (layout_id) REFERENCES timing_layouts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_timing_layout_elems
            ON timing_layout_elements(layout_id, position);
    """)

    # 固定12台を投入（既にあれば無視＝冪等）
    for node_id, kind, label in FIXED_DEVICES:
        await db.execute(
            "INSERT OR IGNORE INTO timing_devices (node_id, kind, label) "
            "VALUES (?, ?, ?)",
            (node_id, kind, label),
        )

    await db.commit()
