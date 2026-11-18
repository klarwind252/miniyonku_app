"""セクターが表示されない原因の診断（読み取り専用・DBは一切書き換えない）。

使い方（サーバー上で・リポジトリ直下から）:
    python3 tools/diag_sector.py <店舗DBのパス> <大会ID>
例:
    python3 tools/diag_sector.py data/stores/arcadebase/miniyonku.db 32

レースごとに次を出す:
  [L] そのレースが使うレイアウト（受信時に固定した layout_json 優先）: 順番・種別・ノード
  [E] 通過イベントの内訳: 発生ノード(src) × 物理レーン × quality の件数
      ※ レイアウトに無いノードのイベントは「×未登録」と表示＝セクター計算で無視される
  [R] 組み立て結果: スタートレーンごとの 周回数 / セクター付きの周の数 / 欠測フラグ
最後に、全レースを通した判定（どの原因が当てはまるか）を出す。
"""
import asyncio
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


async def main(db_path: str, tid: int) -> None:
    import aiosqlite
    from app.application.timing_racer_best_service import _collect_race_rows
    from app.application.timing_race_service import build_race_result

    db = await aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = aiosqlite.Row
    try:
        rows = await _collect_race_rows(db, tid, include_finals=False)
        print(f"大会 {tid}: 反映済み計測レース {len(rows)} 本\n")
        tot = Counter()
        for rr in rows:
            rid = rr["race_id"]
            async with db.execute("SELECT * FROM timing_races WHERE id=?", (rid,)) as cur:
                race = await cur.fetchone()
            keys = race.keys()
            lj = race["layout_json"] if "layout_json" in keys else None
            if lj:
                elems = json.loads(lj)
                src_l = "layout_json(受信時に固定)"
            else:
                async with db.execute(
                    "SELECT kind, node_id FROM timing_layout_elements WHERE layout_id=? ORDER BY position",
                    (race["layout_id"],)) as cur:
                    elems = [dict(r) for r in await cur.fetchall()]
                src_l = f"現在のレイアウト id={race['layout_id']}"
            sq_nodes = {e.get("node_id") for e in elems if e.get("kind") == "SQ"}
            all_nodes = {e.get("node_id") for e in elems if e.get("kind") in ("SG", "SQ")}
            print(f"=== race {rid}  {race['created_at']}  周回={race['target_laps']}  "
                  f"{'F1式' if race['green_t_us'] is not None else '走行式'}")
            print(f"  [L] {src_l}: " + " → ".join(
                f"{e.get('kind')}{'' if e.get('node_id') is None else '(node ' + str(e.get('node_id')) + ')'}"
                for e in elems))
            if not sq_nodes:
                tot["no_sq_in_layout"] += 1

            async with db.execute(
                "SELECT src, lane, COALESCE(quality,0) AS q, COUNT(*) AS n,"
                " SUM(CASE WHEN t_us_b IS NULL OR t_us_b=0 THEN 1 ELSE 0 END) AS nob"
                " FROM timing_events WHERE race_id=? GROUP BY src, lane, q ORDER BY src, lane, q",
                (rid,)) as cur:
                evs = await cur.fetchall()
            seen_src = set()
            for e in evs:
                seen_src.add(e["src"])
                tag = "" if e["src"] in all_nodes else "  ×未登録(レイアウトに無い→無視)"
                qn = {0: "正常", 1: "B欠", 2: "張付", 3: "未同期→剥奪"}.get(e["q"], str(e["q"]))
                print(f"  [E] node {e['src']:>2}  L{e['lane']}  q={e['q']}({qn})  {e['n']}件"
                      f"{tag}")
                if e["src"] not in all_nodes:
                    tot["ev_node_not_in_layout"] += e["n"]
                if e["src"] in sq_nodes:
                    tot["sq_events"] += e["n"]
                    if e["q"] == 3:
                        tot["sq_q3"] += e["n"]
            for n in sorted(sq_nodes - seen_src):
                print(f"  [E] node {n:>2}  ★イベント0件（このSQは1回も届いていない）")
                tot["sq_node_silent"] += 1

            try:
                _race, res = await build_race_result(db, rid)
            except Exception as ex:
                print(f"  [R] 組み立て失敗: {type(ex).__name__}: {ex}")
                continue
            if res is None:
                print("  [R] 結果なし")
                continue
            for m in res.ranking():
                nl = len(m.laps)
                ns = sum(1 for lp in m.laps if lp.sectors)
                tot["laps"] += nl
                tot["laps_with_sec"] += ns
                print(f"  [R] スタートL{m.start_lane}: 周回 {nl}  セクター付き {ns}"
                      f"  欠測フラグ={'あり' if m.missing else 'なし'}")
            print()

        print("=== 判定 ===")
        print(f"  周回合計 {tot['laps']} / うちセクター付き {tot['laps_with_sec']}")
        if tot["no_sq_in_layout"]:
            print(f"  ・{tot['no_sq_in_layout']} 本のレースでレイアウトにSQが無い"
                  "（受信時のレイアウトにSE0/SE1が入っていなかった）")
        if tot["ev_node_not_in_layout"]:
            print(f"  ・レイアウトに無いノードからのイベント {tot['ev_node_not_in_layout']} 件"
                  "（SEのノード番号とレイアウトのノード指定が食い違い）")
        if tot["sq_node_silent"]:
            print(f"  ・SQノードのイベントが1件も無いレースが延べ {tot['sq_node_silent']} 件"
                  "（そのSEの計測/送信が届いていない）")
        if tot["sq_q3"]:
            print(f"  ・SQイベント {tot['sq_events']} 件中 {tot['sq_q3']} 件が q=3(未同期)で剥奪")
        if tot["sq_events"] and not tot["laps_with_sec"] and not tot["sq_q3"]:
            print("  ・SQイベントは届いているのに、どの周にも割り当たっていない"
                  "（レーンチェンジ(LC)の位置／SEの時刻ずれで周の窓・期待レーンに入らない）")
    finally:
        await db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(sys.argv[1], int(sys.argv[2])))
