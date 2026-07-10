"""互換シム：実装は app/presentation/routers/qualifying.py へ移設済み。

viewer / bracket / main が旧パスで router および計算関数を import するため、
それらをまとめて再エクスポートする。全モジュール移行後に削除。
"""
from app.presentation.routers.qualifying import (  # noqa: F401
    router,
    # 順位計算
    _calc_standings,
    _calc_standings_rr,
    _calc_standings_none_rr,
    _calc_standings_group,
    _calc_standings_group_round,
    # 星取表
    _calc_hoshitori_group,
    _calc_hoshitori_group_round,
    # ヒートトーナメント関連
    _ht_get_advanced,
    _ht_get_heatfinal_advancers,
    _ht_get_group_advancers,
    _ht_heat_final_section_no,
    _ht_update_advanced,
    # 並び順（ポイント制）関連
    _order_current_round,
    _order_queue_pending,
    # 並び順（勝ち抜け）関連
    _ow_current_stage,
    _ow_stage_count,
    _ow_stage_row,
    _ow_passed_count,
    _ow_queue_pending,
)
