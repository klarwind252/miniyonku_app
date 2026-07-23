#!/usr/bin/env python3
"""M4LAPS ダミー計測データ投入スクリプト（実機なしでPIP/計測結果を試す用）

使い方（PCのコマンドプロンプト / ターミナルから）:

    python dummy_race.py --base http://66.245.220.187 --layout 1

  よく使うオプション:
    --base    サーバーのURL（既定 http://localhost:8000）
    --layout  使用するレイアウトID（必須。M4LAPSのコースレイアウト画面で確認）
    --lanes   レーン数（既定 3）
    --laps    周回数（既定 3）
    --races   作るレース本数（既定 1）
    --token   TIMING_TOKEN を設定している場合のみ指定
    --heat    予選/決勝のヒートIDに紐づける場合に指定（省略可）

必要なもの: Python 3 のみ（標準ライブラリだけで動作。pip不要）

仕組み:
  1) POST /api/timing/races        でレースを1本作る
  2) POST /api/timing/races/{id}/events で通過イベントを流し込む
  → PIP・計測結果画面にタイムと順位が出る

注意（重要）:
  通過イベントの重複判定キーは (device_id, src, src_boot_id, seq) で race_id を
  含まない。そのため本スクリプトはレースごとに src_boot_id を変え、seq も通しで
  増やしている。ここを固定にすると2本目以降が「重複」として無視される。
  （実機ファームでも同じ制約。seqはリセットしないか、boot_idを振り直すこと）
"""

import argparse
import json
import random
import ssl
import sys
import urllib.error
import urllib.request


# 自己署名証明書（IPアドレス直アクセス等）を許容するSSLコンテキスト
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _open(req, redirects_left: int = 5):
    """POSTでも 307/308 リダイレクトを手動で追従する。

    urllib は POST の 308 を自動追従しないため、Location を読んで貼り直す。
    http→https の常時SSL化サーバーでよく発生する。
    """
    try:
        return urllib.request.urlopen(req, timeout=20, context=_SSL_CTX)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and redirects_left > 0:
            loc = e.headers.get("Location")
            if loc:
                if loc.startswith("/"):
                    from urllib.parse import urlsplit
                    u = urlsplit(req.full_url)
                    loc = f"{u.scheme}://{u.netloc}{loc}"
                print(f"  → リダイレクト: {loc}")
                new = urllib.request.Request(loc, data=req.data, method=req.get_method())
                for k, v in req.header_items():
                    new.add_header(k, v)
                return _open(new, redirects_left - 1)
        raise


def post(url: str, payload: dict, token: str | None):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Timing-Token", token)
    try:
        with _open(req) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        print(f"  [HTTP {e.code}] {url}\n    {detail}", file=sys.stderr)
        raise
    except urllib.error.URLError as e:
        print(f"  [接続失敗] {url}\n    {e.reason}", file=sys.stderr)
        raise


def get(url: str):
    try:
        req = urllib.request.Request(url)
        with _open(req) as res:
            return json.loads(res.read().decode())
    except Exception:
        return None


def build_events(*, lanes: int, laps: int, gates: list[int], boot_id: int,
                 seq_start: int) -> list[dict]:
    """1レース分の通過イベントを組み立てる。

    gates: 通過順のノードID列（先頭がS/G）。例 [6, 0, 1] なら S/G(GW6)→SQ0→SQ1。
         ※ LC（レーンチェンジ）はセンサーが無いので指定不要。

    合計タイムが 30〜50 秒に収まるよう、目標タイムから1区間あたりの時間を逆算する。
    （周回数やゲート数が変わっても合計が大きくブレないようにするため）
    レーンごとに少しずつ違うペースを与え、毎周わずかに揺らして現実に近づける。
    """
    events: list[dict] = []
    seq = seq_start
    t0 = 1_000_000  # 基準時刻(µs)

    # 1周あたりの区間数（先頭以外のゲート＋S/Gへ戻る分）
    seg_per_lap = len(gates)
    total_segments = max(1, laps * seg_per_lap)

    for lane in range(1, lanes + 1):
        # このレーンの目標合計タイム（30.0〜50.0秒）
        target_total_us = random.randint(30_000_000, 50_000_000)
        # 1区間あたりの基礎ペース＝目標合計 ÷ 総区間数
        base = target_total_us // total_segments
        t = t0
        # スタート通過（S/G）
        seq += 1
        events.append({
            "device_id": f"sim-{gates[0]}", "src": gates[0], "src_boot_id": boot_id,
            "seq": seq, "lane": lane, "t_us": t, "quality": 0,
        })
        for _lap in range(laps):
            # 各周：先頭以外のゲート → 最後にS/Gへ戻る
            for node in gates[1:] + [gates[0]]:
                # 毎区間 ±3% の揺らぎ（合計は目標付近に収まる）
                dt = int(base * random.uniform(0.97, 1.03))
                t += dt
                seq += 1
                events.append({
                    "device_id": f"sim-{node}", "src": node, "src_boot_id": boot_id,
                    "seq": seq, "lane": lane, "t_us": t, "quality": 0,
                })
    return events


def main() -> int:
    ap = argparse.ArgumentParser(description="M4LAPS ダミー計測データ投入")
    ap.add_argument("--base", default="http://localhost:8000",
                    help="サーバーURL（常時SSLなら https://... を指定）")
    ap.add_argument("--layout", type=int, required=True, help="レイアウトID（必須）")
    ap.add_argument("--lanes", type=int, default=3, help="レーン数（既定3）")
    ap.add_argument("--laps", type=int, default=3, help="周回数（既定3）")
    ap.add_argument("--races", type=int, default=1, help="作るレース本数（既定1）")
    ap.add_argument("--token", default=None, help="TIMING_TOKEN（設定時のみ）")
    ap.add_argument("--heat", type=int, default=None, help="紐づけるヒートID（省略可）")
    ap.add_argument("--gates", default=None,
                    help="通過順ノードID（例 '6,0,1'）。省略時は 6,0,1＝S/G(GW6)→SQ0→SQ1")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    gates = ([int(x) for x in args.gates.split(",")] if args.gates else [6, 0, 1])

    print(f"サーバー : {base}")
    print(f"レイアウト: {args.layout} / ゲート通過順: {gates}")
    print(f"条件     : {args.lanes}レーン × {args.laps}周 × {args.races}レース\n")

    boot_id = random.randint(10_000, 99_999)
    seq = 0
    made = []

    for n in range(args.races):
        payload = {
            "layout_id": args.layout,
            "target_laps": args.laps,
            "green_t_us": 0,
        }
        if args.heat is not None:
            payload["heat_tag"] = args.heat

        try:
            r = post(f"{base}/api/timing/races", payload, args.token)
        except Exception:
            print("レース作成に失敗しました。--base / --token を確認してください。")
            return 1
        rid = r.get("race_id")
        print(f"[{n+1}/{args.races}] レース作成 race_id={rid}")

        boot_id += 1  # ★レースごとに変える（重複判定回避）
        events = build_events(lanes=args.lanes, laps=args.laps, gates=gates,
                              boot_id=boot_id, seq_start=seq)
        seq += len(events)

        try:
            res = post(f"{base}/api/timing/races/{rid}/events",
                       {"events": events}, args.token)
        except Exception:
            print("  イベント投入に失敗しました。")
            return 1
        print(f"        イベント投入: 新規{res.get('inserted')}件 / "
              f"重複{res.get('duplicate')}件")
        made.append(rid)

    print("\n完了。以下で確認できます:")
    print(f"  ・PIP（画面右下）      … 管理画面を開く")
    print(f"  ・計測結果一覧         … {base}/admin/timing/results")
    for rid in made:
        print(f"  ・レース#{rid} の詳細   … {base}/admin/timing/results/{rid}")
    print("\n※ タイムが出ない場合は、レイアウトにS/G（kind=SG）が含まれているか、")
    print("   --gates のノードIDがレイアウトの構成と一致しているか確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
