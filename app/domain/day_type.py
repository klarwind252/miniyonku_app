"""日付 → 料金区分（weekday/saturday/sunday/holiday）の判定。

jpholiday は任意依存のため、旧コードと同じく ImportError 時は
祝日判定なしで動作する。
"""
from datetime import date as _date

try:
    import jpholiday as _jph
    _JPH_AVAILABLE = True
except ImportError:
    _JPH_AVAILABLE = False


def day_type_of(iso_date: str) -> str:
    """ISO日付文字列の料金区分を返す。解析不能時は 'weekday'（旧挙動維持）。"""
    try:
        d = _date.fromisoformat(iso_date)
        if _JPH_AVAILABLE and _jph.is_holiday(d):
            return "holiday"
        elif d.weekday() == 5:
            return "saturday"
        elif d.weekday() == 6:
            return "sunday"
        return "weekday"
    except Exception:
        return "weekday"