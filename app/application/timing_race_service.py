"""レース結果の組み立て（application層）。

保存済みの通過イベントとレイアウトを読み、domain の race_builder で
ラップ・セクター・合計・順位を組み立てる。DB読み出しと domain の橋渡し。
"""

from app.infrastructure.db.repositories.timing_repository import (
    TimingRaceRepository,
    TimingLayoutRepository,
)
from app.domain.rotation import LayoutElement
from app.domain.race_builder import PassEvent, build_race


async def build_race_result(db, race_id: int):
    """race_id の結果を組み立てて返す。

    戻り値: (race_row, RaceResult) または (race_row, None)（レイアウト未設定など）
    """
    rrepo = TimingRaceRepository(db)
    lrepo = TimingLayoutRepository(db)

    race = await rrepo.get_race(race_id)
    if race is None:
        return None, None

    # レイアウト要素 → LayoutElement 列
    layout_id = race["layout_id"]
    if layout_id is None:
        return race, None
    elems = await lrepo.get_elements(layout_id)
    layout = [
        LayoutElement(kind=e["kind"], node_id=e["node_id"])
        for e in elems
    ]
    if not any(e.kind == "SG" for e in layout):
        return race, None  # S/Gが無ければ組み立て不能

    # 通過イベント → PassEvent 列
    ev_rows = await rrepo.get_events(race_id)
    events = [
        PassEvent(
            node_id=r["src"],
            lane=r["lane"],
            t_us=r["t_us"],
            t_us_b=r["t_us_b"],
            quality=r["quality"],
            seq=r["seq"],
        )
        for r in ev_rows
    ]

    result = build_race(
        layout,
        events,
        target_laps=race["target_laps"],
        green_t_us=race["green_t_us"],
        heat_id=race["heat_tag"],
    )
    return race, result
