"""通過速度フィルタの係数を、実データから自動で校正する（★20261118）。

背景:
  通過速度は「2本のビームの時間差」から出すため、受光の揺れ（サブms打刻）で異常に速い値が出る。
  一方、実車が異常に速くなることはまずない（遅い方向の外れ＝コースアウト・減速はよくある）。
  外れはノード（機器）・日ごとの調子で出る（実データ: SE0/SE1 はほぼ0%、SE2 は日により20〜40%、
  GW6 は 11/08 だけ17.6%）。そこで timing_race_speed_store のフィルタは次の3段で判定する:

    1. 安全上限 100km/h（物理的にありえない値）
    2. ノード健全性: そのレースでのノード中央値 > 基準ref × node_med_k → そのノードは不採用
    3. 個別の通過:   v > ノード中央値 × pass_k  または  v > 基準ref × rel_k → 不採用
       基準ref = max(このレースの最速ラップ平均, 同コース過去のラップ平均P95, 通過中央値÷1.2)

  この3つの係数（pass_k / rel_k / node_med_k）を、このモジュールが実データの分布から決める。

校正のしかた（毎回同じ手順・決定論的）:
  直近 DAYS 日の実レース（サンプル送信は除く）について、フィルタを掛ける前の生の通過速度を集め:
    a. node_ratio = ノード中央値 / ref            … ノードが「まともか」の指標
    b. pass_ratio = v / ノード中央値（健全ノード） … 同じ機器での車ごとのばらつき
    c. ref_ratio  = v / ref（健全ノード）          … 最速車基準でのばらつき
  ノード健全性は「ノードごとの基準線」で決める（揺れは値を上にしか動かさないので下側で測る）:
    node_base[ノード] = P25(そのノードの node_ratio)   … 設置位置（直線/コーナー）ごとの素の速さ
    健全 = node_ratio ≤ node_base × NODE_REL_K（固定 1.3）
    （実データ: SE0≈0.92 / SE1≈1.08 / GW6≈1.1。揺れの日の GW6 は中心1.59＝基準の1.45倍で弾ける）
    基準線の無いノード（新設など）は node_med_k（既定1.8）を絶対線として使う。
  健全ノードのデータで:pass_k / rel_k は健全データの上位分位 × 余裕（MARGIN）:
    pass_k     = clamp(P99.5(pass_ratio) × 1.15)
    rel_k      = clamp(P99.5(ref_ratio)  × 1.15)
  値域は CLAMP で制限。
  サンプルが MIN_SAMPLES 未満なら更新しない。1回の更新で動かせる幅は ±MAX_STEP（急変防止）。

過去記録との関係:
  速度を計算したレースには「使った係数」を timing_races.speed_params に記録し、
  再計算（Av.再計算）でも記録済みの係数を使う。係数が日次で変わっても過去レースの速度は変わらない。

保存先: app_settings
  m4laps_speed_calib          … 現在の係数と最終校正レポート（JSON）
  m4laps_speed_calib_history  … 直近 HISTORY_KEEP 回分の履歴（JSON配列）
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _JST = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover
    _JST = timezone(timedelta(hours=9))

DEFAULTS = {"pass_k": 1.6, "rel_k": 2.0, "node_med_k": 1.8, "node_rel_k": 1.3, "node_base": {}}
CLAMP = {"pass_k": (1.3, 2.5), "rel_k": (1.6, 3.0), "node_med_k": (1.4, 2.5),
         "node_rel_k": (1.1, 2.0), "node_base": (0.5, 2.0)}
MARGIN = {"pass_k": 1.15, "rel_k": 1.15}
NODE_REL_K = 1.3
NODE_BASE_PCT = 0.25
NODE_BASE_MIN_N = 10    # 基準線を作る最小レース数（ノードごと）
PCT = 0.995
NODE_HEALTHY = 1.5      # node_ratio のサンプルが少なく頑健統計が出せないときの健全線
DAYS = 30
MIN_SAMPLES = 500
MAX_STEP = 0.10
HISTORY_KEEP = 60
KEY = "m4laps_speed_calib"
HIST_KEY = "m4laps_speed_calib_history"


# ---------------------------------------------------------------------------
# 係数の読み書き
# ---------------------------------------------------------------------------
async def _get_setting(db, key: str):
    async with db.execute("SELECT value FROM app_settings WHERE key=?", (key,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def _set_setting(db, key: str, value: str) -> None:
    await db.execute("DELETE FROM app_settings WHERE key=?", (key,))
    await db.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (key, value))


def _sanitize(p: dict | None) -> dict:
    out = dict(DEFAULTS)
    out["node_base"] = {}
    try:
        lo, hi = CLAMP["node_base"]
        for n, b in ((p or {}).get("node_base") or {}).items():
            out["node_base"][str(n)] = round(min(hi, max(lo, float(b))), 3)
    except (TypeError, ValueError, AttributeError):
        pass
    for k in DEFAULTS:
        if k == "node_base":
            continue
        try:
            if p and p.get(k) is not None:
                lo, hi = CLAMP[k]
                out[k] = round(min(hi, max(lo, float(p[k]))), 3)
        except (TypeError, ValueError):
            pass
    return out


async def current_params(db) -> dict:
    """現在の係数（未校正なら既定値）。"""
    try:
        raw = await _get_setting(db, KEY)
        if raw:
            return _sanitize(json.loads(raw).get("params"))
    except Exception:
        pass
    return dict(DEFAULTS)


async def params_for_race(db, race_id: int) -> dict:
    """そのレースで使う係数。記録済みならそれ（過去記録を変えない）、無ければ現在の係数。"""
    try:
        async with db.execute("SELECT speed_params FROM timing_races WHERE id=?", (race_id,)) as cur:
            row = await cur.fetchone()
        if row and row[0]:
            return _sanitize(json.loads(row[0]))
    except Exception:
        pass
    return await current_params(db)


async def remember_params(db, race_id: int, params: dict) -> None:
    """そのレースの計算に使った係数を記録する（列が無い旧DBでは何もしない）。"""
    try:
        await db.execute("UPDATE timing_races SET speed_params=? WHERE id=?",
                         (json.dumps(_sanitize(params)), race_id))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 校正
# ---------------------------------------------------------------------------
def _q(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1))))]


def analyze(materials: list[dict]) -> dict:
    """各レースの生データ（speed_store の sink）から分布と推奨係数を出す（純粋関数）。"""
    from app.application.timing_race_speed_store import NODE_MIN_N
    # 1回目：ノードごとの基準線 node_base = P25(node_ratio)
    _nr: dict = {}
    for mat in materials:
        ref = mat.get("ref")
        if not ref:
            continue
        by_node: dict = {}
        for node, _lap, _idx, v in mat.get("samples", []):
            by_node.setdefault(node, []).append(v)
        for node, vs in by_node.items():
            if len(vs) >= NODE_MIN_N:
                _nr.setdefault(node, []).append(statistics.median(vs) / ref)
    node_base = {}
    for node, a in _nr.items():
        if len(a) >= NODE_BASE_MIN_N:
            a.sort()
            node_base[node] = round(_q(a, NODE_BASE_PCT), 3)

    def _healthy_line(node):
        b = node_base.get(node)
        return b * NODE_REL_K if b else NODE_HEALTHY

    node_ratio_all, node_ratio_ok, pass_ratio, ref_ratio = [], [], [], []
    per_node: dict = {}
    n_samples = 0
    for mat in materials:
        ref = mat.get("ref")
        by_node: dict = {}
        for node, _lap, _idx, v in mat.get("samples", []):
            by_node.setdefault(node, []).append(v)
        for node, vs in by_node.items():
            n_samples += len(vs)
            st = per_node.setdefault(node, {"n": 0, "bad_races": 0, "races": 0, "over40": 0})
            st["n"] += len(vs)
            st["over40"] += sum(1 for v in vs if v * 3.6 > 40)
            if len(vs) < NODE_MIN_N or not ref:
                continue
            md = statistics.median(vs)
            nr = md / ref
            st["races"] += 1
            node_ratio_all.append(nr)
            if nr > _healthy_line(node):
                st["bad_races"] += 1
                continue
            node_ratio_ok.append(nr)
            for v in vs:
                pass_ratio.append(v / md)
                ref_ratio.append(v / ref)
    for lst in (node_ratio_all, node_ratio_ok, pass_ratio, ref_ratio):
        lst.sort()

    def dist(a):
        return {k: (round(_q(a, p), 3) if a else None)
                for k, p in (("P50", .5), ("P90", .9), ("P99", .99), ("P99.5", .995), ("max", 1.0))}

    rec = {}
    if pass_ratio:
        rec["pass_k"] = _q(pass_ratio, PCT) * MARGIN["pass_k"]
    if ref_ratio:
        rec["rel_k"] = _q(ref_ratio, PCT) * MARGIN["rel_k"]
    rec["node_base"] = node_base
    return {
        "races": len(materials),
        "samples": n_samples,
        "healthy_samples": len(pass_ratio),
        "dist": {"node_ratio": dist(node_ratio_all), "pass_ratio": dist(pass_ratio),
                 "ref_ratio": dist(ref_ratio)},
        "per_node": {str(k): {**v, "over40_pct": round(v["over40"] / v["n"] * 100, 1) if v["n"] else 0}
                     for k, v in sorted(per_node.items(), key=lambda x: (x[0] is None, x[0]))},
        "recommended_raw": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in rec.items()},
    }


async def collect(db, days: int = DAYS) -> list[dict]:
    """直近 days 日の実レースの生データを集める（DBは書き換えない）。"""
    from app.application.timing_race_speed_store import compute_and_store_speeds
    since = (datetime.now(_JST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    async with db.execute(
        "SELECT id FROM timing_races WHERE created_at >= ? AND sample_note IS NULL ORDER BY id",
        (since,),
    ) as cur:
        ids = [r[0] for r in await cur.fetchall()]
    mats = []
    for rid in ids:
        sink: dict = {}
        try:
            await compute_and_store_speeds(db, rid, params=dict(DEFAULTS), sink=sink)
        except Exception:
            continue
        if sink.get("samples"):
            mats.append(sink)
    return mats


async def run_calibration(db, *, days: int = DAYS, apply: bool = True) -> dict:
    """校正を1回実行する。apply=True なら係数を保存（新しく計算するレースから効く）。"""
    old = await current_params(db)
    mats = await collect(db, days)
    rep = analyze(mats)
    new = dict(old)
    reason = ""
    if rep["healthy_samples"] < MIN_SAMPLES:
        reason = f"サンプル不足（健全 {rep['healthy_samples']} < {MIN_SAMPLES}）→ 係数は据え置き"
    else:
        for k, raw in rep["recommended_raw"].items():
            if k == "node_base":
                # ノード基準線：日々の揺れを抑えるため、旧値との差を ±MAX_STEP に制限
                nb = dict(old.get("node_base") or {})
                for n, b in raw.items():
                    n = str(n)
                    o = nb.get(n)
                    nb[n] = round(b if o is None else min(o * (1 + MAX_STEP), max(o * (1 - MAX_STEP), b)), 3)
                new["node_base"] = nb
                continue
            lo, hi = CLAMP[k]
            target = min(hi, max(lo, raw))
            step = old[k] * MAX_STEP
            new[k] = round(min(old[k] + step, max(old[k] - step, target)), 3)
        reason = "更新" if new != old else "推奨値と同じ（変更なし）"
    rep.update({"at": datetime.now(_JST).strftime("%Y-%m-%d %H:%M:%S"), "days": days,
                "old": old, "new": new, "applied": bool(apply), "note": reason})
    if apply:
        await _set_setting(db, KEY, json.dumps({"params": new, "report": rep}, ensure_ascii=False))
        try:
            hist = json.loads(await _get_setting(db, HIST_KEY) or "[]")
        except Exception:
            hist = []
        hist.append({"at": rep["at"], "old": old, "new": new, "samples": rep["samples"],
                     "healthy": rep["healthy_samples"], "note": reason})
        await _set_setting(db, HIST_KEY, json.dumps(hist[-HISTORY_KEEP:], ensure_ascii=False))
        await db.commit()
    return rep


def format_report(rep: dict) -> str:
    """人が読む用のレポート文字列。"""
    L = [f"校正 {rep.get('at')}  直近{rep.get('days')}日  レース {rep['races']}  通過 {rep['samples']}"
         f"（健全ノード分 {rep['healthy_samples']}）"]
    L.append("ノード別: " + " / ".join(
        f"node{k}: {v['n']}件 40km/h超 {v['over40_pct']}% 不健全レース {v['bad_races']}/{v['races']}"
        for k, v in rep["per_node"].items()))
    for name, d in rep["dist"].items():
        L.append(f"{name:10s} " + "  ".join(f"{k}={v}" for k, v in d.items()))
    L.append(f"推奨(生) {rep['recommended_raw']}")
    L.append(f"係数 {rep.get('old')} → {rep.get('new')}  [{rep.get('note')}]"
             f"{'' if rep.get('applied') else '（試算のみ・未保存）'}")
    return "\n".join(L)
