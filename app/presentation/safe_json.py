"""`<script>` 埋め込み用の安全なJSONシリアライズ（XSS対策 S4 / 20261105）。

json.dumps は `<` `>` `&` `U+2028/U+2029` をエスケープしないため、`{{ ... | safe }}` で
<script> に直挿しすると、データ中の `</script>` 等でスクリプトコンテキストを脱出できる。
本ヘルパはこれらを \\uXXXX へ退避する（Jinja の `| tojson` と同等の安全性）。

挙動不変の保証：危険文字を含まない通常データでは json.dumps と出力が同一。
危険文字のみ \\uXXXX に変わる（JSONとしての値は等価＝JS側の JSON.parse 結果も同一）。
"""
from __future__ import annotations

import json
from typing import Any

_ESCAPE = {
    ord("<"): "\\u003c",
    ord(">"): "\\u003e",
    ord("&"): "\\u0026",
    ord(" "): "\\u2028",
    ord(" "): "\\u2029",
}


def safe_json_dumps(obj: Any, *, ensure_ascii: bool = False) -> str:
    """<script> 内に直挿ししても安全なJSON文字列を返す。"""
    return json.dumps(obj, ensure_ascii=ensure_ascii).translate(_ESCAPE)
