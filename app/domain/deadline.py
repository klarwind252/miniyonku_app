"""事前エントリー締切文字列（datetime-local形式）の解釈。純粋関数。"""
from datetime import datetime


def parse_deadline(s: str):
    """'YYYY-MM-DDTHH:MM'（datetime-local）を datetime へ。失敗時 None。"""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def deadline_passed(s: str) -> bool:
    """締切文字列が過去なら True（NULL/空・解釈不能は False＝締切なし扱い）。"""
    dt = parse_deadline(s)
    if dt is None:
        return False
    return datetime.now() > dt