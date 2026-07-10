"""ドメイン純関数の characterization テスト（現状の挙動を golden 値として固定）。

対象は DB・FastAPI 非依存の純関数のみ。ここでの golden 値は「現状こう動く」を
記録したもので、リファクタ時に挙動が変わればここが赤くなる＝安全網になる。

注意（環境依存を避ける方針）:
  - day_type_of の祝日判定は任意依存 jpholiday の有無で変わるため、祝日でない
    平日・土・日だけをアサートする（祝日日付は使わない）。
  - deadline_passed は現在時刻に対する相対判定のため、明確な過去/未来のみ使う。
"""
from app.domain.kana import kana_row_of
from app.domain.day_type import day_type_of
from app.domain.deadline import parse_deadline, deadline_passed
from app.domain.regulation import is_junior_tournament, is_junior_or_kids_tournament
from app.domain.finalists import calc_finalists


# ---- 五十音「行」判定 ----------------------------------------------------
def test_kana_row_of_golden():
    assert kana_row_of("あきら") == "あ"
    assert kana_row_of("カルロス") == "か"   # カタカナも同じ行へ
    assert kana_row_of("ざぼん") == "さ"      # 濁点は清音の行へ
    assert kana_row_of("ぱんだ") == "は"      # 半濁点は「は」行へ
    assert kana_row_of("ヴァイオリン") == "あ"  # ヴ→「あ」行
    assert kana_row_of("ん") == "わ"           # ん は「わ」行に含める


def test_kana_row_of_non_kana_is_none():
    assert kana_row_of("") is None
    assert kana_row_of(None) is None
    assert kana_row_of("Bob") is None
    assert kana_row_of("ゐ") is None   # どの行にも属さない文字


# ---- 曜日区分（祝日非依存の日付のみ）------------------------------------
def test_day_type_of_weekday_saturday_sunday():
    assert day_type_of("2026-07-06") == "weekday"   # 月曜
    assert day_type_of("2026-07-11") == "saturday"  # 土曜
    assert day_type_of("2026-07-12") == "sunday"    # 日曜


def test_day_type_of_invalid_falls_back_to_weekday():
    assert day_type_of("bad") == "weekday"
    assert day_type_of("") == "weekday"


# ---- 締切パース／判定 ----------------------------------------------------
def test_parse_deadline():
    assert parse_deadline("2020-01-01T00:00") is not None
    assert parse_deadline("") is None
    assert parse_deadline("not-a-date") is None


def test_deadline_passed():
    assert deadline_passed("2020-01-01T00:00") is True    # 明確な過去
    assert deadline_passed("2999-12-31T23:59") is False   # 明確な未来
    assert deadline_passed("") is False                   # 締切なし
    assert deadline_passed("not-a-date") is False         # 解釈不能は未締切扱い


# ---- レギュレーション判定（画面ごとに基準が異なる意図的差異を固定）------
def test_is_junior_tournament():
    assert is_junior_tournament("ジュニア") is True
    assert is_junior_tournament("Junior") is True
    assert is_junior_tournament("Jr初級") is True
    assert is_junior_tournament("子供クラス") is False   # 「子供」は junior には含めない
    assert is_junior_tournament("オープン") is False
    assert is_junior_tournament("") is False
    assert is_junior_tournament(None) is False


def test_is_junior_or_kids_tournament():
    assert is_junior_or_kids_tournament("ジュニア") is True
    assert is_junior_or_kids_tournament("子供クラス") is True  # こちらは「子供」も含む
    assert is_junior_or_kids_tournament("オープン") is False
    assert is_junior_or_kids_tournament(None) is False


# ---- 決勝進出予定人数（空データ時の現状挙動を固定）----------------------
def test_calc_finalists_defaults():
    assert calc_finalists("roundrobin", {}) == 2
    assert calc_finalists("heat_tournament", {}) == 2
    assert calc_finalists("heat", {}) is None
    assert calc_finalists("none_roundrobin", {}) is None
