#!/usr/bin/env python3
"""2026-11-20 の不具合（SEが1台も動いていないレースが「1周=1区間」で組み立てられた）の後始末。

対象：timing_races.skip_absent=1 のレースだけ（＝2026-11-20 の更新後に作られたレース）。それ以外には触らない。
  1) 速度（timing_race_speeds）を修正後のロジックで計算し直す
  2) そのレースのベスト（timing_bests scope='race'）を作り直す
  3) そのレースがあった日の当日ベスト（scope='day'）を一から作り直す

使い方（修正版 timing_race_service.py を入れたあと・リポジトリ直下で）：
  python3 tools/repair_skip_absent_20261120.py data/<店舗>.db            # 確認だけ（書き込まない）
  python3 tools/repair_skip_absent_20261120.py data/<店舗>.db --apply    # 実行
"""
import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite  # noqa: E402

from app.application import timing_best_service as best_svc  # noqa: E402
from app.application import timing_race_speed_store as speed_store  # noqa: E402
from app.application.timing_race_service import build_race_result  # noqa: E402
from app.infrastructure.db.repositories.timing_repository import TimingRaceRepository  # noqa: E402


async def _speed_fns(db, race_ids):
    """timing_api._speed_fns_for_races と同じ（保存済みの速度を読む）。"""
    speed_map, lapavg_map, totavg_map = {}, {}, {}
    for rid in race_ids:
        st = await speed_store.load_speeds(db, rid)
        for lane, mp in st["lane"].items():
            if "total_avg" in mp:
                totavg_map[(rid, lane)] = mp["total_avg"]
        for (lane, lap), v in st["lap"].items():
            lapavg_map[(rid, lane, lap)] = v
        for (lane, lap, sno), v in st["sec"].items():
            speed_map[(rid, lane, lap, sno - 1)] = v
    return (lambda rid, lane, idx, lap=1: speed_map.get((rid, lane, lap, idx)),
            lambda rid, lane, lap: lapavg_map.get((rid, lane, lap)),
            lambda rid, lane, _t=None, _l=None: totavg_map.get((rid, lane)))


async def _day_bests(db, day):
    cur = await db.execute(
        "SELECT metric, rank, value, race_id FROM timing_bests WHERE scope='day' AND scope_key=? "
        "ORDER BY metric, rank", (day,))
    return [tuple(r) for r in await cur.fetchall()]


async def run(path: str, apply: bool) -> None:
    work = path
    if not apply:   # 確認だけのときはコピーに対して実行する（元DBには書かない）
        fd, work = tempfile.mkstemp(suffix=".db"); os.close(fd)
        shutil.copy(path, work)
    db = await aiosqlite.connect(work)
    db.row_factory = aiosqlite.Row
    try:
        cur = await db.execute("SELECT id, created_at FROM timing_races WHERE skip_absent=1 ORDER BY id")
        races = await cur.fetchall()
        if not races:
            print("対象レースなし（skip_absent=1 のレースがありません）")
            return
        ids = [r["id"] for r in races]
        days = sorted({(r["created_at"] or "")[:10] for r in races} - {""})
        print(f"対象レース {len(ids)} 件: {ids}  / 対象日: {days}")
        before = {d: await _day_bests(db, d) for d in days}

        repo = TimingRaceRepository(db)
        for rid in ids:
            await speed_store.compute_and_store_speeds(db, rid)
            await db.execute("DELETE FROM timing_bests WHERE scope='race' AND scope_key=?", (str(rid),))
            await db.commit()
            _, res = await build_race_result(db, rid)
            if res is not None:
                sf, lf, tf = await _speed_fns(db, [rid])
                cands = best_svc.collect_from_result(res, rid, sf, lf, tf)
                await best_svc.merge_bests(db, "race", str(rid), cands)
                print(f"  race {rid}: " + ", ".join(
                    f"L{sl} 合計={'-' if m.total_time_us is None else round(m.total_time_us / 1e6, 3)} "
                    f"周={[round(l.lap_time_us / 1e6, 3) for l in m.laps if l.lap_time_us]}"
                    for sl, m in sorted(res.machines.items())))
        for d in days:
            day_races = await repo.list_races_by_date(d, limit=500)
            sf, lf, tf = await _speed_fns(db, [r["id"] for r in day_races])
            await best_svc.recalc_day(db, d, lambda x: repo.list_races_by_date(x, limit=500),
                                      build_race_result, sf, lf, tf)
            after = await _day_bests(db, d)
            gone = [x for x in before[d] if x not in after]
            new = [x for x in after if x not in before[d]]
            print(f"  当日ベスト {d}: 消える {gone}")
            print(f"  当日ベスト {d}: 入る   {new}")
        print("実行しました。" if apply else "確認のみ（書き込んでいません）。実行するには --apply を付けてください。")
    finally:
        await db.close()
        if not apply:
            os.remove(work)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    asyncio.run(run(sys.argv[1], "--apply" in sys.argv))
