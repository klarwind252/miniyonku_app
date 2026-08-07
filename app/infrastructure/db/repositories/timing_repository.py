"""timing_devices / timing_layouts へのアクセス。

既存リポジトリ（racer_repository 等）と同じく、aiosqlite.Connection を受け取り
SQL を薄くラップする。ドメイン判断（バリデーション等）は持たない。
"""

import aiosqlite


class TimingDeviceRepository:
    """端末台帳（固定12台）へのアクセス。"""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def list_all(self):
        async with self.db.execute(
            "SELECT node_id, kind, label, mac, note, sensor_width_mm FROM timing_devices "
            "ORDER BY node_id"
        ) as cur:
            return await cur.fetchall()

    async def list_by_kind(self, kind: str):
        async with self.db.execute(
            "SELECT node_id, kind, label, mac, note FROM timing_devices "
            "WHERE kind = ? ORDER BY node_id",
            (kind,),
        ) as cur:
            return await cur.fetchall()

    async def get(self, node_id: int):
        async with self.db.execute(
            "SELECT node_id, kind, label, mac, note, sensor_width_mm FROM timing_devices "
            "WHERE node_id = ?",
            (node_id,),
        ) as cur:
            return await cur.fetchone()

    async def update_meta(self, node_id: int, label: str, mac: str, note: str,
                          sensor_width_mm=None):
        """表示名・MAC・メモ・センサー幅の更新（node_id と kind は固定）。

        sensor_width_mm: ダブルセンサーの間隔(mm)。None なら未設定に戻す。
        """
        await self.db.execute(
            "UPDATE timing_devices SET label = ?, mac = ?, note = ?, sensor_width_mm = ? "
            "WHERE node_id = ?",
            (label, mac, note, sensor_width_mm, node_id),
        )
        await self.db.commit()


class TimingLayoutRepository:
    """コースレイアウト（地図）へのアクセス。"""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def list_layouts(self):
        async with self.db.execute(
            "SELECT id, name, target_laps, created_at, updated_at "
            "FROM timing_layouts ORDER BY updated_at DESC, id DESC"
        ) as cur:
            return await cur.fetchall()

    async def get_layout(self, layout_id: int):
        async with self.db.execute(
            "SELECT id, name, target_laps, lap_length_m, created_at, updated_at "
            "FROM timing_layouts WHERE id = ?",
            (layout_id,),
        ) as cur:
            return await cur.fetchone()

    async def get_elements(self, layout_id: int):
        """通過順に並んだ要素列を返す。"""
        async with self.db.execute(
            "SELECT id, position, kind, node_id, beam_gap_mm "
            "FROM timing_layout_elements WHERE layout_id = ? ORDER BY position",
            (layout_id,),
        ) as cur:
            return await cur.fetchall()

    async def create_layout(self, name: str, target_laps: int) -> int:
        cur = await self.db.execute(
            "INSERT INTO timing_layouts (name, target_laps) VALUES (?, ?)",
            (name, target_laps),
        )
        await self.db.commit()
        return cur.lastrowid

    async def save_elements(self, layout_id: int, elements: list[dict]):
        """要素列を丸ごと置き換える（position 0..N-1 で入れ直す）。

        elements: [{"kind": "SG"|"SQ"|"LC", "node_id": int|None,
                    "beam_gap_mm": float|None}, ...]（通過順）
        beam_gap_mm は2本のビームの間隔(mm)。通過速度の算出に使う（省略可）。
        """
        await self.db.execute(
            "DELETE FROM timing_layout_elements WHERE layout_id = ?",
            (layout_id,),
        )
        for pos, el in enumerate(elements):
            await self.db.execute(
                "INSERT INTO timing_layout_elements "
                "(layout_id, position, kind, node_id, beam_gap_mm) "
                "VALUES (?, ?, ?, ?, ?)",
                (layout_id, pos, el["kind"], el.get("node_id"),
                 el.get("beam_gap_mm")),
            )
        await self.db.execute(
            "UPDATE timing_layouts SET updated_at = datetime('now','localtime') "
            "WHERE id = ?",
            (layout_id,),
        )
        await self.db.commit()

    # 「指定なし＝変更しない」と「明示的にNone＝クリア」を区別するための番兵
    _UNSET = object()

    async def update_meta(self, layout_id: int, name: str, target_laps: int,
                          lap_length_m=_UNSET):
        """レイアウトの基本情報を更新する。

        lap_length_m: 1周の距離(m)。ラップ平均速度の算出に使う。
          - 省略  … 変更しない
          - float … その値を設定
          - None  … 未設定に戻す（クリア）
        ⚠ None を「変更しない」と扱うと設定を解除できなくなるため、
           省略時の番兵(_UNSET)と明確に区別している。
        """
        if lap_length_m is self._UNSET:
            await self.db.execute(
                "UPDATE timing_layouts SET name = ?, target_laps = ?, "
                "updated_at = datetime('now','localtime') WHERE id = ?",
                (name, target_laps, layout_id),
            )
        else:
            await self.db.execute(
                "UPDATE timing_layouts SET name = ?, target_laps = ?, lap_length_m = ?, "
                "updated_at = datetime('now','localtime') WHERE id = ?",
                (name, target_laps, lap_length_m, layout_id),
            )
        await self.db.commit()

    async def delete_layout(self, layout_id: int):
        await self.db.execute(
            "DELETE FROM timing_layout_elements WHERE layout_id = ?",
            (layout_id,),
        )
        await self.db.execute(
            "DELETE FROM timing_layouts WHERE id = ?",
            (layout_id,),
        )
        await self.db.commit()


class TimingRaceRepository:
    """独立レース（timing_races）と通過イベント（timing_events）へのアクセス。"""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def create_race(
        self,
        heat_tag: int | None,
        layout_id: int | None,
        target_laps: int,
        green_t_us: int | None,
        client_key: str | None = None,
        sample_note: str | None = None,
    ) -> int:
        # 冪等キー（任意）: GWが (device_id:boot_id:heat_tag) 等を送ってきた場合、
        # 200応答の消失→再送で同一ヒートのレースが二重生成される事故を防ぐ。
        # 旧ファーム（キーなし）は従来どおり毎回新規作成（後方互換）。
        if client_key:
            async with self.db.execute(
                "SELECT id FROM timing_races WHERE client_key = ?", (client_key,)
            ) as cur:
                row = await cur.fetchone()
            if row:
                return row["id"] if hasattr(row, "keys") else row[0]
        cur = await self.db.execute(
            "INSERT INTO timing_races (heat_tag, layout_id, target_laps, green_t_us, client_key, sample_note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (heat_tag, layout_id, target_laps, green_t_us, client_key, sample_note),
        )
        await self.db.commit()
        return cur.lastrowid

    async def insert_events_batch(self, race_id: int, events: list) -> tuple[int, int]:
        """イベント一括挿入（単一トランザクション）。戻り値 (inserted, duplicate)。

        従来の insert_event はイベント1件ごとに commit しており、GWの1バッチ
        （最大数十件）で数十回の fsync が走っていた。バッチ全体を1Txにまとめ、
        入力検証（lane範囲・必須キー）もここで行う。
        """
        inserted = 0
        duplicate = 0
        try:
            for ev in events:
                lane = int(ev["lane"])
                if not (1 <= lane <= 8):
                    raise ValueError(f"lane out of range: {lane}")
                cur = await self.db.execute(
                    "INSERT OR IGNORE INTO timing_events "
                    "(race_id, device_id, src, src_boot_id, seq, lane, t_us, t_us_b, quality) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (race_id, str(ev["device_id"]), int(ev["src"]),
                     int(ev["src_boot_id"]), int(ev["seq"]), lane,
                     int(ev["t_us"]), ev.get("t_us_b"), int(ev.get("quality", 0))),
                )
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    duplicate += 1
            await self.db.commit()
        except Exception:
            try:
                await self.db.rollback()
            except Exception:
                pass
            raise
        return inserted, duplicate

    async def set_green_t_us(self, race_id: int, green_t_us: int) -> int:
        """既存レースに緑時刻を後付けする（F1式へ切り替える）。

        GWは赤ボタン時点ではまだ緑時刻を持たないため、走行式として create_race し、
        緑を出した瞬間にこのメソッドで green_t_us を書き込む。これにより
        「緑の瞬間にレースを作り直す」割り切り（docs/19 残課題7）を解消する。
        戻り値: 更新した行数（0=該当レースなし）。
        """
        cur = await self.db.execute(
            "UPDATE timing_races SET green_t_us = ? WHERE id = ?",
            (green_t_us, race_id),
        )
        await self.db.commit()
        return cur.rowcount

    async def update_status(self, race_id: int, status: str) -> int:
        """レースの status（confirmed / needs_review）を更新する（I群 §24.71）。

        受信時判定の結果を書き込む。戻り値: 更新した行数。
        """
        cur = await self.db.execute(
            "UPDATE timing_races SET status = ? WHERE id = ?",
            (status, race_id),
        )
        await self.db.commit()
        return cur.rowcount

    async def get_race(self, race_id: int):
        # applied_ht_group_id はマイグレーション（timing_schema.py）で後から追加された列。
        # PIP の「反映済（ヒート○ …）」表示に必要なので取得するが、未適用の旧DBでも
        # レース取得自体は失敗させない（列なしSELECTへフォールバック）。
        _base = ("id, heat_tag, layout_id, target_laps, green_t_us, heat_id, "
                 "applied_group_id, created_at, status")
        try:
            async with self.db.execute(
                f"SELECT {_base}, applied_ht_group_id FROM timing_races WHERE id = ?",
                (race_id,),
            ) as cur:
                return await cur.fetchone()
        except Exception:
            async with self.db.execute(
                f"SELECT {_base} FROM timing_races WHERE id = ?",
                (race_id,),
            ) as cur:
                return await cur.fetchone()

    async def list_races(self, limit: int = 50):
        async with self.db.execute(
            "SELECT id, heat_tag, layout_id, target_laps, green_t_us, applied_group_id, created_at, sample_note, status "
            "FROM timing_races ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            return await cur.fetchall()

    async def list_race_dates(self, limit: int = 60):
        """計測実績のある日付を新しい順に返す（絞り込みプルダウン用）。

        created_at は "YYYY-MM-DD HH:MM:SS" 形式なので日付部分で集計する。
        戻り値: [{"date": "2026-07-23", "n": 12}, ...]
        """
        async with self.db.execute(
            "SELECT substr(created_at,1,10) AS d, COUNT(*) AS n "
            "FROM timing_races GROUP BY d ORDER BY d DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [{"date": r["d"], "n": int(r["n"])} for r in rows]

    async def list_races_by_date(self, date: str, limit: int = 500):
        """指定日（YYYY-MM-DD）のレースを新しい順に返す。"""
        async with self.db.execute(
            "SELECT id, heat_tag, layout_id, target_laps, green_t_us, applied_group_id, created_at, sample_note, status "
            "FROM timing_races WHERE substr(created_at,1,10) = ? "
            "ORDER BY id DESC LIMIT ?",
            (date, limit),
        ) as cur:
            return await cur.fetchall()

    async def list_races_between(self, date_from: str, date_to: str, limit: int = 5000):
        """期間（両端を含む）のレースを古い順に返す。ベスト集計用。

        created_at は "YYYY-MM-DD HH:MM:SS" 形式なので日付部分で比較する。
        """
        async with self.db.execute(
            "SELECT id, heat_tag, layout_id, target_laps, green_t_us, applied_group_id, created_at, sample_note, status "
            "FROM timing_races "
            "WHERE substr(created_at,1,10) >= ? AND substr(created_at,1,10) <= ? "
            "ORDER BY id ASC LIMIT ?",
            (date_from, date_to, limit),
        ) as cur:
            return await cur.fetchall()

    async def list_races_between_ts(self, ts_from: str, ts_to: str, limit: int = 2000):
        """作成日時の範囲（両端を含む）でレースを古い順に返す。

        ts_from / ts_to は created_at と同じ 'YYYY-MM-DD HH:MM:SS' 形式。
        「当日」を 09:00〜翌08:59 の24時間で扱うため、日付だけでなく時刻まで見る。
        固定長・ゼロ埋めの文字列なので、辞書順の比較で日時の比較になる。
        """
        async with self.db.execute(
            "SELECT id, heat_tag, layout_id, target_laps, green_t_us, applied_group_id, created_at, sample_note, status "
            "FROM timing_races WHERE created_at >= ? AND created_at <= ? "
            "ORDER BY id ASC LIMIT ?",
            (ts_from, ts_to, limit),
        ) as cur:
            return await cur.fetchall()

    async def delete_race(self, race_id: int) -> int:
        """レースを1件削除する（通過イベントも一緒に消える）。

        timing_events は ON DELETE CASCADE だが、既存の delete_layout と同じ流儀で
        明示的に消してから親を消す（PRAGMAの状態に依存しないようにするため）。
        戻り値: 削除した通過イベント件数（0でもレース自体は削除する）
        """
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM timing_events WHERE race_id = ?", (race_id,)
        ) as cur:
            row = await cur.fetchone()
        n_events = int(row["n"]) if row else 0

        await self.db.execute(
            "DELETE FROM timing_events WHERE race_id = ?", (race_id,)
        )
        await self.db.execute(
            "DELETE FROM timing_races WHERE id = ?", (race_id,)
        )
        await self.db.commit()
        return n_events

    async def insert_event(self, race_id: int, ev: dict) -> bool:
        """通過イベントを1件挿入。冪等キー（D12）で重複は無視。

        戻り値: True=新規挿入 / False=重複（既にある）
        ev: {device_id, src, src_boot_id, seq, lane, t_us, t_us_b?, quality?}
        """
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO timing_events "
            "(race_id, device_id, src, src_boot_id, seq, lane, t_us, t_us_b, quality) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                race_id,
                str(ev["device_id"]),
                int(ev["src"]),
                int(ev["src_boot_id"]),
                int(ev["seq"]),
                int(ev["lane"]),
                int(ev["t_us"]),
                ev.get("t_us_b"),
                int(ev.get("quality", 0)),
            ),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def get_events(self, race_id: int, include_excluded: bool = False):
        """通過イベントを時刻順で返す。

        include_excluded=False（既定）: 除外(excluded=1)を除いた「有効な検出」だけ。
          → 同定・集計（build_race_result）はこちらを使う＝除外が即座に効く。
        include_excluded=True: 除外も含めた全検出（生データ表示・訂正モーダル用）。
        列に id と excluded を含める（訂正でその通過を特定・表示するため）。
        """
        if include_excluded:
            sql = ("SELECT id, src, lane, t_us, t_us_b, quality, seq, excluded "
                   "FROM timing_events WHERE race_id = ? ORDER BY t_us")
        else:
            sql = ("SELECT id, src, lane, t_us, t_us_b, quality, seq, excluded "
                   "FROM timing_events WHERE race_id = ? AND excluded = 0 ORDER BY t_us")
        async with self.db.execute(sql, (race_id,)) as cur:
            return await cur.fetchall()

    async def set_event_excluded(self, race_id: int, event_id: int,
                                 excluded: bool) -> bool:
        """通過イベント1件の除外フラグを立てる/外す（訂正モーダルから叩く）。

        race_id 一致を必須条件にして、他レースのイベントを誤って触らないようにする。
        戻り値: 更新できたら True（該当なしは False）。
        """
        cur = await self.db.execute(
            "UPDATE timing_events SET excluded = ? WHERE id = ? AND race_id = ?",
            (1 if excluded else 0, int(event_id), int(race_id)),
        )
        await self.db.commit()
        return cur.rowcount > 0
