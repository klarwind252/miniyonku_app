"""Jinja2Templates の共通ファクトリ。

旧コードでは各ルーターが個別に Jinja2Templates を生成し inject_globals を
呼んでいた（8箇所で重複）。ここで1つだけ生成して共有する。
"""
import os
from fastapi.templating import Jinja2Templates
from app.core.config import inject_globals

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "../templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

# 多言語対応：テンプレート読み込み時に日本語テキストを自動で {{ tr("原文") }} に包む。
# カタログ（i18n.CATALOG）に対訳がある文字列だけ翻訳され、無い文字列は原文のまま
# （安全な素通し）。各テンプレートを手で書き換えなくても全ページが翻訳対象になる。
try:
    from app.core.i18n_autotrans import AutoTranslateExtension
    templates.env.add_extension(AutoTranslateExtension)
except Exception:
    # 拡張が読めなくても従来挙動（日本語）を壊さない
    pass

inject_globals(templates)