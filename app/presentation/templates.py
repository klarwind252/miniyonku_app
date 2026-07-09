"""Jinja2Templates の共通ファクトリ。

旧コードでは各ルーターが個別に Jinja2Templates を生成し inject_globals を
呼んでいた（8箇所で重複）。ここで1つだけ生成して共有する。
"""
import os
from fastapi.templating import Jinja2Templates
from app.core.config import inject_globals

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "../templates")

templates = Jinja2Templates(directory=TEMPLATES_DIR)
inject_globals(templates)