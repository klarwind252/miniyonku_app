"""決勝進出予定人数の計算（純粋関数・旧 routers/tournaments.py calc_finalists を移設・無変更）。

bracket / viewer / qualifying / tournaments の4モジュールが共有する計算カーネル。
DB・FastAPI 非依存。
"""
from app.core.config import HEAT_TOURNAMENT_TYPES


def calc_finalists(qual_type: str, data: dict) -> int | None:
    """決勝進出予定人数を計算"""
    if qual_type in ("none", "none_roundrobin"):
        return None
    if qual_type in HEAT_TOURNAMENT_TYPES:
        hc = data.get("qual_heat_count", 1) or 1
        gc = data.get("qual_group_count", 1) or 1
        ga = data.get("qual_group_advance", 2) or 2
        has_final = bool(data.get("qual_heat_final", 0))
        if has_final:
            fa = data.get("qual_heat_advance", 1) or 1
            return hc * fa
        else:
            return hc * gc * ga
    if qual_type == "heat_roundrobin":
        hc = data.get("qual_heat_count", 1)
        gc = data.get("qual_group_count", 1)
        gp = data.get("qual_group_advance", 2)
        has_final = bool(data.get("qual_heat_final", 0))
        if has_final:
            fa = data.get("qual_heat_final_advance", 1)
            return hc * fa
        else:
            return hc * gc * gp
    if qual_type in ("point", "roundrobin", "order"):
        return data.get("qual_final_advance", 2)
    if qual_type == "order_winner":
        # 並び順（勝ち抜け）：最終段階の通過人数（advance_count）＝決勝進出人数
        stages = data.get("order_winner_stages") or []
        if stages:
            return stages[-1].get("advance_count")
        return None
    return None
