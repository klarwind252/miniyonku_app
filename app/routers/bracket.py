"""互換シム：実装は app/presentation/routers/bracket.py へ移設済み。

main / qualifying が旧パスで router および内部関数を import するため
それらを再エクスポートする。移行完了後に削除。
"""
from app.presentation.routers.bracket import (  # noqa: F401
    router,
    _get_all_standings,
    _render_html_bracket,
    combinations_2_3,
)
