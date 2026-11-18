"""通過速度フィルタの校正レポート（★20261118）。

サーバーが毎日 04:20 JST に自動で行う校正（app/services/speed_calib_scheduler.py）と
まったく同じ処理を、手動で1回実行する。エクスポートしたDBのコピーでも使える。

使い方（リポジトリ直下から）:
  python3 tools/speed_calib.py <DBのパス>                 # 試算のみ（DBは変更しない）
  python3 tools/speed_calib.py <DBのパス> --days 60       # 対象期間を変える（既定30日）
  python3 tools/speed_calib.py <DBのパス> --all           # 全期間を対象
  python3 tools/speed_calib.py <DBのパス> --apply         # 係数を保存（サーバー上で使う場合）
  python3 tools/speed_calib.py <DBのパス> --json          # レポートをJSONで出す
"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


async def main(db_path: str, days: int, apply: bool, as_json: bool) -> None:
    import aiosqlite
    from app.application import timing_speed_calib as calib
    uri = db_path if apply else f"file:{db_path}?mode=ro"
    db = await aiosqlite.connect(uri, uri=not apply)
    db.row_factory = aiosqlite.Row
    try:
        rep = await calib.run_calibration(db, days=days, apply=apply)
    finally:
        await db.close()
    print(json.dumps(rep, ensure_ascii=False, indent=1) if as_json else calib.format_report(rep))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 1:
        print(__doc__)
        sys.exit(1)
    days = 30
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
        args = [a for a in args if a != str(days)]
    if "--all" in sys.argv:
        days = 36500
    asyncio.run(main(args[0], days, "--apply" in sys.argv, "--json" in sys.argv))
