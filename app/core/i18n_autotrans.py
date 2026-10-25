"""テンプレート自動翻訳レイヤー（Jinjaプリプロセス拡張）。

テンプレート読み込み時に、HTMLの「テキストノード」と placeholder/title 属性の
日本語を自動で `{{ tr("原文") }}` に包む。これにより各テンプレートを手で書き換え
なくても、カタログ（i18n.CATALOG）に対訳がある文字列は全ページで翻訳される。
カタログに無い文字列は tr() が原文を返すため、見た目は従来どおり（安全な素通し）。

安全設計:
  - 触るのは「タグの外側のテキスト」と placeholder/title 属性のみ。
  - `{{ }}` `{% %}` `{# #}`・`<script>` `<style>`・HTMLコメント・onclick等の
    イベント属性は一切触らない（JSや既存の {{ tr(...) }} を壊さない）。
  - 既に `{{ }}` を含む属性値はスキップ（二重変換防止）。
  - 変換中に何か起きたら原文をそのまま返す（レンダリングを止めない）。
"""
from __future__ import annotations

import json
import re

from jinja2.ext import Extension

# 日本語（ひらがな・カタカナ・漢字）を1文字でも含むか
_JP = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")

# 保護領域（この中は絶対に触らない）＋タグ。優先順位＝記述順。
_MASTER = re.compile(
    r"(?P<jcomment>\{#.*?#\})"
    r"|(?P<jblock>\{%.*?%\})"
    r"|(?P<jvar>\{\{.*?\}\})"
    r"|(?P<script><script\b[^>]*>.*?</script\s*>)"
    r"|(?P<style><style\b[^>]*>.*?</style\s*>)"
    r"|(?P<hcomment><!--.*?-->)"
    # タグ：属性値（" " / ' '）の中に > があっても正しく1タグとして食う
    # （例: onclick="a.forEach(c=>c.checked=false)"）
    r"|(?P<tag><[a-zA-Z/!][^>\"']*(?:(?:\"[^\"]*\"|'[^']*')[^>\"']*)*>)",
    re.DOTALL | re.IGNORECASE,
)

# placeholder / title 属性（値に {{ }} を含むものはスキップ）
_ATTR = re.compile(r'\b(placeholder|title)="([^"{}]*?)"', re.IGNORECASE)

_WS = re.compile(r"\s+")

# コード（CSS/JS）とみなす強いシグナル。子テンプレートが親の {% block %} に
# <style>/<script> タグ無しで流し込む CSS/JS を誤って翻訳対象にしないためのガード。
# これらを含むテキストノードは絶対に tr() で包まない（空白圧縮でJSが壊れる事故を防ぐ）。
_CODEY = ("{", "}", ";", "/*", "*/", "//", "function", "var ", "let ", "const ", "=>")


def _has_jp(s: str) -> bool:
    return bool(_JP.search(s))


_ENTITY = re.compile(r"&[#a-zA-Z0-9]+;")


def _looks_like_code(s: str) -> bool:
    # HTMLエンティティ（&#10; 等）の ; を誤検知しないよう除去してから判定
    s = _ENTITY.sub("", s)
    return any(tok in s for tok in _CODEY)


def _wrap_text(seg: str) -> str:
    """タグ外テキストノード。日本語を含めば中身を tr() で包む（前後の空白は保持）。
    CSS/JSらしきテキスト（波括弧・セミコロン・コメント等を含む）は一切触らない。"""
    if not _has_jp(seg):
        return seg
    m = re.match(r"^(\s*)(.*?)(\s*)$", seg, re.DOTALL)
    lead, core, trail = m.group(1), m.group(2), m.group(3)
    if not _has_jp(core) or _looks_like_code(core):
        return seg
    key = _WS.sub(" ", core)  # 内部の改行・連続空白は1スペースに（HTMLと同じ扱い）
    return lead + "{{ tr(" + json.dumps(key, ensure_ascii=False) + ") }}" + trail


def _wrap_tag(tag: str) -> str:
    """開始タグ内の placeholder/title だけ、日本語値を tr() 化する。"""
    def repl(m: re.Match) -> str:
        attr, val = m.group(1), m.group(2)
        if not _has_jp(val):
            return m.group(0)
        lit = "'" + val.replace("\\", "\\\\").replace("'", "\\'") + "'"
        return attr + '="{{ tr(' + lit + ') }}"'
    return _ATTR.sub(repl, tag)


def autotranslate_source(source: str, name: str | None = None) -> str:
    """テンプレートソースを自動翻訳用に書き換える。"""
    out = []
    pos = 0
    for m in _MASTER.finditer(source):
        if m.start() > pos:
            out.append(_wrap_text(source[pos:m.start()]))
        kind = m.lastgroup
        tok = m.group(0)
        out.append(_wrap_tag(tok) if kind == "tag" else tok)
        pos = m.end()
    if pos < len(source):
        out.append(_wrap_text(source[pos:]))
    return "".join(out)


def extract_keys(source: str) -> list[str]:
    """このソースから自動生成される翻訳キー（原文）の一覧。カタログ作成用。"""
    transformed = autotranslate_source(source)
    keys = []
    for m in re.finditer(r"\{\{ tr\((\".*?\"|'(?:[^'\\]|\\.)*')\) \}\}", transformed):
        lit = m.group(1)
        try:
            if lit[0] == '"':
                keys.append(json.loads(lit))
            else:
                keys.append(lit[1:-1].replace("\\'", "'").replace("\\\\", "\\"))
        except Exception:
            pass
    return keys


class AutoTranslateExtension(Extension):
    """Jinja2 のプリプロセス拡張として登録する。"""

    def preprocess(self, source: str, name, filename=None) -> str:
        try:
            if name and not str(name).endswith(".html"):
                return source
            return autotranslate_source(source, name)
        except Exception:
            # 何かあっても絶対にレンダリングを止めない
            return source
