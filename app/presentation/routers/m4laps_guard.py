"""M4LAPS ルーターのアクセスガード（FastAPI依存）。

クラウド版かつライセンス登録済みでなければ 404 を返す。
- オンプレ版：常に 404（機能を根本的に隠す）。
- クラウド版・未登録：404。
- クラウド版・登録済み：通す。

404 にするのは「存在しないかのように」隠すため（403 だと存在が露見する）。
"""

from fastapi import Depends, HTTPException
import aiosqlite

from app.core.config import IS_CLOUD
from app.infrastructure.db.connection import get_db
from app.domain import m4laps_license


async def require_m4laps(db: aiosqlite.Connection = Depends(get_db)):
    """M4LAPSが利用可能でなければ404。ルーターに Depends で挿す。"""
    if not IS_CLOUD:
        raise HTTPException(status_code=404)
    if not await m4laps_license.is_licensed(db):
        raise HTTPException(status_code=404)
    return True
