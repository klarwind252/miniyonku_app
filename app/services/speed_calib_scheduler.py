"""クラウド版：通過速度フィルタの係数を毎日自動で校正する（★20261118）。

毎日 04:20 JST に全店舗のDBで timing_speed_calib.run_calibration() を1回実行し、
結果を data/speed_calib.log に追記する（標準出力にも出す）。
新しい係数は「これから速度を計算するレース」から効く。過去レースは記録済みの係数のまま。
backup_scheduler（03:30）の後に走らせ、バックアップと重ならないようにしている。
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _JST = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover
    _JST = timezone(timedelta(hours=9))

_HOUR, _MINUTE = 4, 20
_task = None


def _seconds_until_next_run() -> float:
    now = datetime.now(_JST)
    nxt = now.replace(hour=_HOUR, minute=_MINUTE, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return max(1.0, (nxt - now).total_seconds())


def _log(line: str) -> None:
    print(f"[SPEEDCAL] {line}", flush=True)
    try:
        from app import registry
        with open(os.path.join(registry.DATA_DIR, "speed_calib.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


async def run_all_stores_once() -> None:
    import aiosqlite
    from app import registry
    from app.application import timing_speed_calib as calib
    for st in registry.list_stores():
        try:
            db = await aiosqlite.connect(st.db_path)
            db.row_factory = aiosqlite.Row
            try:
                rep = await calib.run_calibration(db, apply=True)
            finally:
                await db.close()
            _log(f"store={st.slug or 'default'}\n" + calib.format_report(rep))
        except Exception as e:
            _log(f"store={st.slug or 'default'} エラー: {type(e).__name__}: {e}")


async def calib_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_seconds_until_next_run())
            await run_all_stores_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"ループ内エラー（継続します）: {e}")
            await asyncio.sleep(60)


def launch():
    global _task
    if _task is not None:
        return _task
    _task = asyncio.create_task(calib_loop())
    print(f"[SPEEDCAL] 速度フィルタ自動校正 有効（毎日 {_HOUR:02d}:{_MINUTE:02d} JST / ログ data/speed_calib.log）",
          flush=True)
    return _task
