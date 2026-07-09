"""互換シム：実装は app/presentation/routers/tournaments.py へ移設済み。

bracket / viewer / qualifying などの未移行モジュールが旧パスで
router・calc_finalists・_is_result_finalized・各種ラベルを import するため、
それらを再エクスポートする。全モジュール移行後に削除する。
"""
from app.presentation.routers.tournaments import (  # noqa: F401
    router,
    calc_finalists,
    _is_result_finalized,
    get_regulation_labels,
    parse_order_winner_stages,
    save_order_winner_stages,
    load_order_winner_stages,
    STATUS_LABELS,
    TIME_SLOT_LABELS,
    REGULATION_LABELS,
    QUALIFYING_LABELS,
)
