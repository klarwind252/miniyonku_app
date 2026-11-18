"""過去レースに「走った時のコース構成」を付け直す（★20261118）。

背景:
  timing_races.layout_json（レース時点のコース構成）が今まで保存されておらず、過去レースは
  常に「現在のレイアウト」で解釈されていた。レース後にセクターゲートを足す／並べ替えると、
  過去レースの全周が「セクターゲート欠け」と判定され、セクターが全部消える。
  このツールは、各レースの通過イベントから当時の構成を推定し、layout_json に固定する。

推定方法:
  そのレースで実際に通過イベントが届いたセクターノードだけを使い、
  「並び順 × レーンチェンジ(LC)の位置」の全候補でレースを組み立てて、
  セクターが揃った周が最も多い構成を採用する（S/G のノードは現在のレイアウトのものを使う）。
  大会内の全レースで共通の1構成を選ぶ（レースごとにばらばらにはしない）。

使い方（サーバー上で・リポジトリ直下から。アプリは止めなくてよい）:
  # 1) 確認だけ（DBは変更しない）
  python3 tools/fix_race_layout.py <店舗DBのパス> <大会ID>
  # 2) 内容に問題なければ書き込み（layout_json が空のレースだけ。速度も再計算）
  python3 tools/fix_race_layout.py <店舗DBのパス> <大会ID> --apply
  # 構成を自分で指定する場合（例: S/G→SE1→LC→SE0）
  python3 tools/fix_race_layout.py <店舗DBのパス> <大会ID> --layout "SG:6,SQ:1,LC,SQ:0" --apply

  書き込み後、管理画面で予選管理を開き直す。配信HTMLは結果確定など管理操作を1回すると再生成される。
"""
import asyncio
import itertools
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _parse_layout(spec: str):
    out = []
    for tok in spec.split(","):
        tok = tok.strip().upper()
        if not tok:
            continue
        if tok == "LC":
            out.append({"kind": "LC", "node_id": None})
        else:
            k, n = tok.split(":")
            if k not in ("SG", "SQ"):
                raise ValueError(f"不明な要素: {tok}")
            out.append({"kind": k, "node_id": int(n)})
    return out


def _fmt(elems):
    return " → ".join(e["kind"] if e["kind"] == "LC" else f"{'S/G' if e['kind']=='SG' else 'SE'}{e['node_id']}"
                      for e in elems)


def _candidates(sg_node: int, sq_nodes: list[int], n_lc: int):
    """S/G を先頭に、SQ の並び順 × LC の位置（n_lc 個）の全候補。"""
    seen = set()
    for perm in itertools.permutations(sq_nodes):
        body = [("SQ", n) for n in perm]
        slots = len(body) + 1          # LC を差し込める位置（S/Gの直後〜末尾）
        for pos in itertools.combinations_with_replacement(range(slots), n_lc):
            seq = []
            for i in range(slots):
                seq += [("LC", None)] * pos.count(i)
                if i < len(body):
                    seq.append(body[i])
            key = tuple(seq)
            if key in seen:
                continue
            seen.add(key)
            yield [{"kind": "SG", "node_id": sg_node}] + [{"kind": k, "node_id": n} for k, n in seq]


async def main(db_path: str, tid: int, apply: bool, layout_spec: str | None) -> None:
    import aiosqlite
    from app.application.timing_racer_best_service import _collect_race_rows
    from app.domain.race_builder import build_race, LayoutElement, PassEvent

    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    try:
        cols = [r[1] for r in await (await db.execute("PRAGMA table_info(timing_races)")).fetchall()]
        if "layout_json" not in cols:
            if not apply:
                print("※ layout_json 列が未作成（アプリ更新後の起動で自動作成）。確認のみ続行します。\n")
            else:
                await db.execute("ALTER TABLE timing_races ADD COLUMN layout_json TEXT")
                await db.commit()
                cols.append("layout_json")

        rows = await _collect_race_rows(db, tid, include_finals=True)
        race_ids = sorted({r["race_id"] for r in rows})
        print(f"大会 {tid}: 反映済み計測レース {len(race_ids)} 本")

        # レースごとの材料（レース行・イベント・現在レイアウト）
        mats = []
        for rid in race_ids:
            race = await (await db.execute("SELECT * FROM timing_races WHERE id=?", (rid,))).fetchone()
            if race is None:
                continue
            if "layout_json" in cols and race["layout_json"]:
                print(f"  race {rid}: 構成は保存済み → 対象外")
                continue
            cur_el = [dict(r) for r in await (await db.execute(
                "SELECT kind, node_id, beam_gap_mm FROM timing_layout_elements WHERE layout_id=? ORDER BY position",
                (race["layout_id"],))).fetchall()]
            evs = await (await db.execute(
                "SELECT id, src, lane, t_us, t_us_b, quality, seq FROM timing_events WHERE race_id=?",
                (rid,))).fetchall()
            events = [PassEvent(node_id=e["src"], lane=e["lane"], t_us=e["t_us"], t_us_b=e["t_us_b"],
                                quality=e["quality"], seq=e["seq"], id=e["id"]) for e in evs]
            mats.append((rid, race, cur_el, events))
        if not mats:
            print("対象レースなし（全レースで構成が保存済み）。")
            return

        cur_el0 = mats[0][2]
        sg_nodes = [e["node_id"] for e in cur_el0 if e["kind"] == "SG"]
        if not sg_nodes:
            print("現在のレイアウトに S/G がありません。--layout で指定してください。")
            return
        sg_node = sg_nodes[0]
        # センサー幅は機器台帳（timing_devices）の値で固定する（速度計算と同じ出所）
        gap_by_node = {}
        try:
            for d in await (await db.execute("SELECT node_id, sensor_width_mm FROM timing_devices")).fetchall():
                if d["sensor_width_mm"]:
                    gap_by_node[d["node_id"]] = d["sensor_width_mm"]
        except Exception:
            pass
        for e in cur_el0:
            if e["node_id"] is not None and e.get("beam_gap_mm") and e["node_id"] not in gap_by_node:
                gap_by_node[e["node_id"]] = e["beam_gap_mm"]
        n_lc_cur = sum(1 for e in cur_el0 if e["kind"] == "LC")
        print(f"現在のレイアウト: {_fmt(cur_el0)}")

        def score(elems):
            lay = [LayoutElement(kind=e["kind"], node_id=e["node_id"]) for e in elems]
            laps = sec = 0
            for _rid, race, _c, events in mats:
                try:
                    res = build_race(lay, events, target_laps=race["target_laps"],
                                     green_t_us=race["green_t_us"], heat_id=race["heat_tag"])
                except Exception:
                    continue
                for m in res.ranking():
                    laps += len(m.laps)
                    sec += sum(1 for lp in m.laps if lp.sectors)
            return sec, laps

        base_sec, base_laps = score(cur_el0)
        print(f"  現在のレイアウトで解釈: 周回 {base_laps} / セクター付き {base_sec}\n")

        if layout_spec:
            best = _parse_layout(layout_spec)
            bs, bl = score(best)
            print(f"指定の構成: {_fmt(best)}  → 周回 {bl} / セクター付き {bs}")
        else:
            # 実際にイベントが届いたノード（S/G以外）＝当時のセクターゲート
            sq_seen = sorted({ev.node_id for *_x, events in mats for ev in events
                              if ev.node_id != sg_node and ev.quality != 3})
            print(f"イベントが届いたセクターノード: {['SE' + str(n) for n in sq_seen] or 'なし'}")
            if not sq_seen:
                print("セクターノードのイベントが1件もありません＝セクターは当時から計測されていません。")
                return
            results = []
            for n_lc in sorted({n_lc_cur, 0, 1}):
                for cand in _candidates(sg_node, sq_seen, n_lc):
                    s, l = score(cand)
                    results.append((s, -abs(n_lc - n_lc_cur), cand, l))
            results.sort(key=lambda x: (x[0], x[1]), reverse=True)
            print("候補（セクターが揃った周の多い順・上位5）:")
            for s, _p, cand, l in results[:5]:
                print(f"  {s:>4} / {l} 周   {_fmt(cand)}")
            bs, _p, best, bl = results[0]
            if bs == 0:
                print("\nどの構成でもセクターが揃いません（SEの時刻ずれ・欠測の可能性）。書き込みは行いません。")
                return
            ties = [r for r in results if (r[0], r[1]) == (bs, _p)]
            if len(ties) > 1:
                print("\n⚠ 同点の候補が複数あります。--layout で当時の構成を指定してください。")
                return
            print(f"\n採用: {_fmt(best)}  （周回 {bl} / セクター付き {bs}）")

        if not apply:
            print("\n確認のみ（DBは未変更）。書き込むには --apply を付けて再実行してください。")
            return

        lj = json.dumps([{"kind": e["kind"], "node_id": e["node_id"],
                          "beam_gap_mm": gap_by_node.get(e["node_id"])} for e in best], ensure_ascii=False)
        from app.application import timing_race_speed_store as speed_store
        from app.application.timing_race_service import invalidate_race_result_cache
        for rid, *_rest in mats:
            await db.execute("UPDATE timing_races SET layout_json=? WHERE id=? AND (layout_json IS NULL OR layout_json='')",
                             (lj, rid))
        await db.commit()
        invalidate_race_result_cache()
        n_ok = 0
        for rid, *_rest in mats:
            try:
                await speed_store.compute_and_store_speeds(db, rid)   # セクター通過速度を当時の構成で保存し直す
                n_ok += 1
            except Exception as ex:
                print(f"  race {rid}: 速度の再計算に失敗 {type(ex).__name__}: {ex}")
        await db.commit()
        print(f"\n書き込み完了: {len(mats)} レースに構成を固定・速度再計算 {n_ok} 件。")
        print("管理画面の予選管理を開き直してください（配信HTMLは管理操作1回で再生成）。")
    finally:
        await db.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    spec = None
    if "--layout" in sys.argv:
        spec = sys.argv[sys.argv.index("--layout") + 1]
        args = [a for a in args if a != spec]
    if len(args) != 2:
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(args[0], int(args[1]), apply, spec))
