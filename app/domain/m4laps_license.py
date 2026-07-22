"""M4LAPS ライセンス判定。

正解キーは平文でコードに置かず、SHA-256ハッシュのみを保持して照合する。
（キーそのものはコードからは読み取れない。）
ライセンス有効状態は app_settings テーブルに保存し、一度登録すれば
その店舗のDBで永続的に有効になる。
"""

import hashlib
import aiosqlite

# 正解キーのSHA-256ハッシュ（キー平文はコード・ドキュメントに記載しない）
_LICENSE_HASH = "54c488a950bf28c710ebe61f2210766f9a36d0407ee028b20c3aadd01b251b22"

_SETTING_KEY = "m4laps_licensed"


def verify_key(key: str) -> bool:
    """入力キーが正解かをハッシュ照合で判定する。"""
    if not key:
        return False
    digest = hashlib.sha256(key.strip().encode()).hexdigest()
    return digest == _LICENSE_HASH


async def is_licensed(db: aiosqlite.Connection) -> bool:
    """この店舗DBで M4LAPS が有効化済みか。"""
    try:
        async with db.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_SETTING_KEY,),
        ) as cur:
            row = await cur.fetchone()
        return bool(row) and row["value"] == "1"
    except Exception:
        return False


async def activate(db: aiosqlite.Connection) -> None:
    """有効化フラグを立てる（キー照合が通った後に呼ぶ）。"""
    await db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, '1')",
        (_SETTING_KEY,),
    )
    await db.commit()


async def deactivate(db: aiosqlite.Connection) -> None:
    """無効化（ライセンス解除）。"""
    await db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, '0')",
        (_SETTING_KEY,),
    )
    await db.commit()
