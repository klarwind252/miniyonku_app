"""
デプロイモード設定。

環境変数（.env を systemd EnvironmentFile 経由で読み込む想定）:
  DEPLOY_MODE      : "onprem"（既定）| "cloud"
  PUBLIC_BASE_URL  : クラウド時の公開ベースURL（例: https://xxxx.vs.sakura.ne.jp）
  ADMIN_TOKEN      : クラウド時の admin 用固定トークン（単一店舗時のみ。複数店舗化では
                     店舗1の初期トークンとしてレジストリへ移行される）
  VIEW_TOKEN       : クラウド時の view 用固定トークン（同上）
  PUBLIC_HTML_DIR  : クラウド時に参加者向け静的HTMLを書き出す「親」ディレクトリ
                     （単一店舗時はここ直下。複数店舗化では配下に店舗ごとのサブディレクトリ）

オンプレ運用では環境変数未設定 → "onprem" にフォールバックし、従来挙動を維持する。
"""

import os

DEPLOY_MODE = os.environ.get("DEPLOY_MODE", "onprem").lower()   # "onprem" | "cloud"
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
IS_CLOUD = DEPLOY_MODE == "cloud"

# 予選形式「ヒート（トーナメント）」系の内部タイプ一覧。
# 「ヒート（トーナメント）[がらっぱ堂]」（heat_tournament_garappa）は
# 表示名だけが異なり、挙動はヒート（トーナメント）と完全に同一。
HEAT_TOURNAMENT_TYPES = ("heat_tournament", "heat_tournament_garappa")

# 「ヒート（トーナメント）[がらっぱ堂]」を選べるようにする条件となる店舗1の名称。
# 店舗1（既定店舗）の名称がこの値と一致するときだけ、作成・編集画面に
# 当該予選形式の選択肢を表示する。
GARAPPA_STORE_NAME = "がらっぱ堂"

# 固定トークン（クラウド時のみ使用。複数店舗化では店舗1初期値としてレジストリへ移行）
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
VIEW_TOKEN = os.environ.get("VIEW_TOKEN", "")

# クラウド時に参加者向け静的HTMLを書き出す親ディレクトリ（nginx 直接配信）
PUBLIC_HTML_DIR = os.environ.get("PUBLIC_HTML_DIR", "").rstrip("/")

# 認証クッキー名（複数店舗化では店舗IDを付与して店舗別に分離する。下記ヘルパ参照）
ADMIN_COOKIE = "m4_admin_token"
VIEW_COOKIE = "m4_view_token"


def admin_cookie_name(store_id: int | None = None) -> str:
    """店舗別 admin Cookie 名。store_id が None なら従来名（単一店舗互換）。"""
    return ADMIN_COOKIE if store_id is None else f"{ADMIN_COOKIE}_{store_id}"


def view_cookie_name(store_id: int | None = None) -> str:
    """店舗別 view Cookie 名。store_id が None なら従来名（単一店舗互換）。"""
    return VIEW_COOKIE if store_id is None else f"{VIEW_COOKIE}_{store_id}"


def inject_globals(templates):
    """各ルーターの Jinja2Templates に共通変数を注入する。

    テンプレート側（base.html / settings.html 等）で IS_CLOUD / DEPLOY_MODE を
    参照して UI を出し分けるために使用する。
    """
    templates.env.globals.update(DEPLOY_MODE=DEPLOY_MODE, IS_CLOUD=IS_CLOUD)
    # ホーム画面アイコン（Webアプリ）用の <head> 生成関数をテンプレートから呼べるように。
    # 循環 import を避けるためここで遅延 import する。
    try:
        from app.pwa import render_pwa_head
        templates.env.globals.update(render_pwa_head=render_pwa_head)
    except Exception:
        # pwa モジュール未配置でも従来挙動を壊さない
        templates.env.globals.setdefault("render_pwa_head", lambda *a, **k: "")
    # 左上ロゴ用：アプリ用アイコン（nginx直配信・?v付き）URLを返す関数
    try:
        from app.pwa import app_icon_src
        templates.env.globals.update(app_icon_src=app_icon_src)
    except Exception:
        templates.env.globals.setdefault("app_icon_src", lambda *a, **k: "/logo")
