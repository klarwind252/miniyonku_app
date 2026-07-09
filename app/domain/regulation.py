"""レギュレーション文字列に関するドメイン知識。

⚠ ジュニア判定は画面によって基準が異なる（意図的な差異のため統一しない）：
  - racers（本日一括エントリー）: 「ジュニア/junior/Jr」
  - tournaments（レース属性判定）: 上記に加えて「子供」も含む
"""


def is_junior_tournament(regulation: str | None) -> bool:
    """「ジュニア」「junior」「Jr」を含むか（racers 系の判定）。"""
    if not regulation:
        return False
    reg = regulation.lower()
    return "ジュニア" in regulation or "junior" in reg or "jr" in reg


def is_junior_or_kids_tournament(regulation: str | None) -> bool:
    """「ジュニア」「子供」「junior」「Jr」を含むか（tournaments 系の判定）。"""
    if not regulation:
        return False
    reg = regulation.lower()
    return "ジュニア" in regulation or "子供" in regulation or "junior" in reg or "jr" in reg