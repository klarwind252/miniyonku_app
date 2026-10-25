"""多言語対応（i18n）の中核。

方針（決定 2026-10-25）:
  - 翻訳はサーバー側の手動カタログ（原文＝日本語をキーにする）。自動翻訳は使わない
    （予選・敗者復活・受付番号などレース用語の精度を優先）。
  - 表示言語は店舗単位で app_settings.language に保存（key/value）。未設定なら
    管理画面で言語選択モーダルを必ず表示し、一度設定したら維持する。
  - テンプレートは Jinja グローバル `t('原文')` を呼ぶだけ。現在言語は
    current_db_path()（店舗 ContextVar）から解決する。

まず台湾語＝**繁體中文（台湾華語・zh-TW）** を実装。以降 en / zh-CN / 広東 / ko /
fil / id / th / vi を同じカタログ方式で順次追加する。
"""
from __future__ import annotations

import os
import sqlite3
import time

DEFAULT_LANG = "ja"

# 対応言語（海外進出ロードマップ）。enabled=True のみ実運用で選択可。
#   code     : app_settings.language に保存する値（BCP-47 準拠）
#   native   : その言語話者向けの表示名
#   ja_label : 管理者（日本語）向けの呼び名
LANGUAGES = [
    {"code": "ja",    "native": "日本語",            "ja_label": "日本語",         "enabled": True},
    {"code": "zh-TW", "native": "繁體中文（台灣）",   "ja_label": "台湾語（繁體中文）", "enabled": True},
    {"code": "en",    "native": "English",           "ja_label": "英語",           "enabled": False},
    {"code": "zh-CN", "native": "简体中文（北京）",   "ja_label": "中国語（北京）",   "enabled": False},
    {"code": "yue",   "native": "廣東話（粵語）",     "ja_label": "中国語（広東）",   "enabled": False},
    {"code": "ko",    "native": "한국어",            "ja_label": "韓国語",         "enabled": False},
    {"code": "fil",   "native": "Filipino",          "ja_label": "フィリピン語",    "enabled": False},
    {"code": "id",    "native": "Bahasa Indonesia",  "ja_label": "インドネシア語",  "enabled": False},
    {"code": "th",    "native": "ภาษาไทย",           "ja_label": "タイ語",         "enabled": False},
    {"code": "vi",    "native": "Tiếng Việt",        "ja_label": "ベトナム語",      "enabled": False},
]

SUPPORTED = {l["code"] for l in LANGUAGES}
ENABLED = {l["code"] for l in LANGUAGES if l["enabled"]}


# ============================================================================
#  翻訳カタログ（原文＝日本語 → 各言語）
#  ※ここにある原文のみ翻訳され、無いものは原文（日本語）のまま返す（安全な素通し）。
# ============================================================================
CATALOG: dict[str, dict[str, str]] = {
    "zh-TW": {
        # --- 共通ナビ（base.html） ---
        "ダッシュボード": "儀表板",
        "レース一覧": "賽事列表",
        "レーサー一覧": "選手列表",
        "テロップ": "跑馬燈",
        "自動": "自動",
        "設定": "設定",
        "料金区分": "收費類別",
        "平日": "平日",
        "土曜": "週六",
        "日曜": "週日",
        "祝日": "國定假日",
        "特別": "特別",
        # --- テロップ ポップオーバー ---
        "例）決勝は14時開始です": "例）決賽於14:00開始",
        "表示": "顯示",
        "消去": "清除",
        "「表示」で view・参加者html の帯に反映（最大30秒）":
            "點「顯示」後會反映到觀賽頁與參賽者頁的跑馬燈（最多30秒）",
        # --- レーサー用アクセスQR モーダル ---
        "レーサー用アクセスQR": "選手專用入場QR",
        "レーサーはこのQRから入場・観覧ページにアクセスできます。":
            "選手可透過此QR進入入場・觀賽頁面。",
        # --- 設定ページ 共通 ---
        "アプリの各種設定・テンプレートを管理します": "管理應用程式的各項設定與範本",
        "設定項目": "設定項目",
        "初期値": "初始值",
        "店舗名": "店名",
        "利用料金": "使用費用",
        "賞状テンプレート": "獎狀範本",
        "QR/バーコード印刷テンプレート": "QR/條碼列印範本",
        "ポストテンプレート": "貼文範本",
        "参加者向け配信": "參賽者頁發布",
        "ホーム画面アイコン": "主畫面圖示",
        "複数店舗展開": "多店舖展開",
        "稼働ヘルス": "運作健康狀態",
        "アクセス統計": "存取統計",
        "監査ログ": "稽核日誌",
        "データ初期化": "資料初始化",
        "再起動": "重新啟動",
        # --- 言語設定セクション & モーダル ---
        "言語設定": "語言設定",
        "表示言語": "顯示語言",
        "この端末ではなく店舗ごとの設定です。管理画面・参加者ページの表示言語に反映します。":
            "此為各店舖的設定（非單一裝置）。會套用到管理畫面與參賽者頁面的顯示語言。",
        "保存": "儲存",
        "表示言語を選択してください": "請選擇顯示語言",
        "最初に表示言語を選んでください。あとから設定でいつでも変更できます。":
            "請先選擇顯示語言。日後可隨時於設定中變更。",
        "準備中": "準備中",
        "言語を保存しました": "已儲存語言設定",
    },
}


# ============================================================================
#  言語の解決（店舗DBから）。描画ごとに大量に呼ばれるため短TTLでキャッシュ。
# ============================================================================
_cache: dict[str, tuple[str, float]] = {}   # db_path -> (raw_value, expires_at)
_TTL = 5.0


def _db_path() -> str | None:
    try:
        from app.infrastructure.db.connection import current_db_path
        return current_db_path()
    except Exception:
        return None


def get_language_raw(db_path: str | None = None) -> str:
    """app_settings.language の生値を返す（未設定なら ''）。"""
    if db_path is None:
        db_path = _db_path()
    if not db_path:
        return ""
    now = time.time()
    hit = _cache.get(db_path)
    if hit and hit[1] > now:
        return hit[0]
    raw = ""
    try:
        if os.path.exists(db_path):
            con = sqlite3.connect(db_path)
            try:
                row = con.execute(
                    "SELECT value FROM app_settings WHERE key='language'"
                ).fetchone()
            finally:
                con.close()
            if row and row[0]:
                raw = str(row[0]).strip()
    except Exception:
        raw = ""
    _cache[db_path] = (raw, now + _TTL)
    return raw


def invalidate_language_cache(db_path: str | None = None) -> None:
    """言語保存時にキャッシュを飛ばす（即時反映）。"""
    if db_path is None:
        db_path = _db_path()
    if db_path:
        _cache.pop(db_path, None)
    else:
        _cache.clear()


def is_language_set(db_path: str | None = None) -> bool:
    """表示言語が設定済みか（＝モーダル不要か）。"""
    return get_language_raw(db_path) in SUPPORTED


def get_language(db_path: str | None = None) -> str:
    """実効言語（未設定・未知値は DEFAULT_LANG＝日本語扱い）。"""
    raw = get_language_raw(db_path)
    return raw if raw in SUPPORTED else DEFAULT_LANG


def t(text: str, lang: str | None = None) -> str:
    """原文（日本語）を現在言語へ。訳が無ければ原文のまま返す（安全）。"""
    if lang is None:
        lang = get_language()
    if not lang or lang == DEFAULT_LANG:
        return text
    return CATALOG.get(lang, {}).get(text, text)
