"""エントリーカード用バーコード値のユーティリティ。

バーコード値の構成（合計10桁の数字）:
    レースID（4桁ゼロ埋め） + 連番（4桁ゼロ埋め） + 予備（1桁・固定0） + チェックディジット（1桁）

例: レースID=34, 連番=7
    → "0034" + "0007" + "0" + check
    → "003400070C"

チェックディジットはモジュラス10（重み 3,1,3,1... を右から適用するJAN/EAN方式）で、
先頭9桁から計算する。スキャン時は10桁全体を検算し、前4桁=レースID・次4桁=連番に分解する。

QR / CODE128 のどちらで印刷しても、エンコードする値はこの10桁文字列で共通とする。
"""

# 桁数定義
RACE_ID_DIGITS = 4   # レースID
SEQ_DIGITS = 4       # 連番
RESERVED_DIGITS = 1  # 予備（固定0）
TOTAL_DIGITS = 10    # 合計（チェックディジット含む）

RACE_ID_MAX = 9999
SEQ_MAX = 9999


def calc_check_digit(body: str) -> int:
    """モジュラス10（JAN/EAN方式）のチェックディジットを計算する。

    body の各桁を右端から重み 3,1,3,1... で掛けて合計し、
    合計を10で割った余りの「10の補数」（0なら0）を返す。

    Args:
        body: チェックディジットを除いた数字列（本仕様では9桁）

    Returns:
        0〜9 のチェックディジット
    """
    if not body.isdigit():
        raise ValueError(f"body must be digits only: {body!r}")
    total = 0
    # 右端から重み 3,1,3,1...
    for i, ch in enumerate(reversed(body)):
        weight = 3 if i % 2 == 0 else 1
        total += int(ch) * weight
    return (10 - (total % 10)) % 10


def build_code(race_id: int, seq_no: int) -> str:
    """レースIDと連番から10桁のバーコード値を生成する。

    Args:
        race_id: レースID（1〜9999）
        seq_no: 連番（1〜9999）

    Returns:
        10桁の数字文字列（例: "003400070C" の C は実際の数字）
    """
    if not (0 <= race_id <= RACE_ID_MAX):
        raise ValueError(f"race_id out of range (0-{RACE_ID_MAX}): {race_id}")
    if not (0 <= seq_no <= SEQ_MAX):
        raise ValueError(f"seq_no out of range (0-{SEQ_MAX}): {seq_no}")
    body = f"{race_id:0{RACE_ID_DIGITS}d}{seq_no:0{SEQ_DIGITS}d}{'0' * RESERVED_DIGITS}"
    check = calc_check_digit(body)
    return f"{body}{check}"


def parse_code(code: str) -> dict:
    """10桁のバーコード値を検証し、レースID・連番に分解する。

    Args:
        code: スキャンで読み取った文字列

    Returns:
        {
            "valid": bool,        # 桁数・数字・チェックディジットがすべて正当か
            "race_id": int|None,  # 前4桁
            "seq_no": int|None,   # 次4桁
            "reason": str|None,   # valid=False のときの理由
        }
    """
    s = (code or "").strip()
    if len(s) != TOTAL_DIGITS:
        return {"valid": False, "race_id": None, "seq_no": None,
                "reason": f"桁数が不正です（{len(s)}桁／期待{TOTAL_DIGITS}桁）"}
    if not s.isdigit():
        return {"valid": False, "race_id": None, "seq_no": None,
                "reason": "数字以外が含まれています"}
    body, check = s[:-1], int(s[-1])
    if calc_check_digit(body) != check:
        return {"valid": False, "race_id": None, "seq_no": None,
                "reason": "チェックディジットが一致しません（読み取りエラーの可能性）"}
    # 予備桁（レースID4桁＋連番4桁の次の1桁）は固定0。0以外は不正とみなす
    reserved = s[RACE_ID_DIGITS + SEQ_DIGITS:RACE_ID_DIGITS + SEQ_DIGITS + RESERVED_DIGITS]
    if reserved != "0" * RESERVED_DIGITS:
        return {"valid": False, "race_id": None, "seq_no": None,
                "reason": "予備桁が不正です（読み取りエラーの可能性）"}
    race_id = int(s[:RACE_ID_DIGITS])
    seq_no = int(s[RACE_ID_DIGITS:RACE_ID_DIGITS + SEQ_DIGITS])
    return {"valid": True, "race_id": race_id, "seq_no": seq_no, "reason": None}
