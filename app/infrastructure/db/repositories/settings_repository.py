"""app_settings・店舗名・ポストテンプレートの取得。"""
import json

import aiosqlite

from app.core.config import IS_CLOUD
from app.domain.labels import REGULATION_LABELS


class SettingsRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def get_regulation_labels(self) -> dict:
        """DBのapp_settingsからレギュレーション一覧を取得。未設定はREGULATION_LABELSにフォールバック"""
        try:
            async with self.db.execute(
                "SELECT value FROM app_settings WHERE key='regulations'"
            ) as cur:
                row = await cur.fetchone()
            if row:
                items = json.loads(row["value"])
                # ラベル文字列をそのままキーにした辞書を返す
                return {v: v for v in items}
        except Exception:
            pass
        return REGULATION_LABELS

    async def get_store1_name(self) -> str:
        """店舗1（既定店舗）の表示名を返す。
        クラウド版：店舗レジストリ（control.db）の既定店舗名。
        オンプレ版：メインDBの app_settings キー 'store_name'。
        未設定・失敗時は空文字。
        """
        if IS_CLOUD:
            try:
                from app import registry
                st = registry.get_default_store()
                return (st.name or "") if st else ""
            except Exception:
                return ""
        try:
            async with self.db.execute(
                "SELECT value FROM app_settings WHERE key='store_name'"
            ) as cur:
                row = await cur.fetchone()
            return (row["value"] if row else "") or ""
        except Exception:
            return ""

    async def get_post_templates(self) -> list:
        """ポストテンプレート一覧を取得"""
        async with self.db.execute(
            "SELECT id, name, body FROM post_templates ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]