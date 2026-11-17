"""五十音「行」の判定（純粋関数・依存なし）。"""

# 五十音「行」フィルタ用（先頭1文字一致）。濁点/半濁点/小書きも各行に含める。
KANA_ROWS = {
    "あ": "あいうえおぁぃぅぇぉゔ",
    "か": "かきくけこがぎぐげごゕゖ",
    "さ": "さしすせそざじずぜぞ",
    "た": "たちつてとだぢづでどっ",
    "な": "なにぬねの",
    "は": "はひふへほばびぶべぼぱぴぷぺぽ",
    "ま": "まみむめも",
    "や": "やゆよゃゅょ",
    "ら": "らりるれろ",
    "わ": "わをんゎ",
}
KANA_ROW_ORDER = ["あ", "か", "さ", "た", "な", "は", "ま", "や", "ら", "わ"]
_CHAR2ROW = {ch: row for row, chars in KANA_ROWS.items() for ch in chars}


def kana_row_of(s: str | None):
    """文字列の先頭1文字が属する五十音「行」を返す（該当なしは None）。
    カタカナはひらがなに正規化して判定する。"""
    if not s:
        return None
    ch = s[0]
    o = ord(ch)
    if 0x30A1 <= o <= 0x30F6:      # カタカナ → ひらがな
        ch = chr(o - 0x60)
    return _CHAR2ROW.get(ch)


# ── GW(TFT)送信用のレーサー名正規化（20261109）─────────────────────────────
#  GWの内蔵フォント(jpfont.h)は「カタカナ＋半角ASCII」のみを持つ。そこに合わせて
#  サーバー側で正規化してから送る（ユーザー確定仕様）：
#   ・全角英数/記号 → 半角、半角カナ → 全角カナ（unicodedata NFKC）
#   ・ひらがな → カタカナ
#   ・カタカナ(長音ー・中黒・含む)と半角ASCIIのみ残し、漢字等の未収録文字は破棄
#   例： "ああ11AAaa" → "アア11AAaa" ／ "グレ太郎" → "グレ" + (太郎は漢字なので破棄)
#   ・括弧とその中身は丸ごと破棄（★20261117）："RS（宮崎）" → "RS"
#  ※名前はGW側 char[24]（UTF-8・カタカナ3バイト/字）に収めるため、UTF-8で23バイトに切る。
#   画面側は右はみ出しをクリップするので、長い名前はこの範囲で十分。
def to_gw_name(s: str | None, max_bytes: int = 23) -> str:
    import unicodedata
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))   # 全角（）もここで半角()になる
    # ★20261117 括弧とその中身（地域名など。例「らっしー（鹿児島）」の（鹿児島））は GW に送らない。
    #   閉じ括弧が無い（文字数で切れた）場合も末尾まで落とす。前後の空白も詰める。
    import re as _re
    s = _re.sub(r"\([^)]*\)?", "", s).strip()
    out = []
    for ch in s:
        o = ord(ch)
        if 0x3041 <= o <= 0x3096:        # ひらがな → カタカナ
            out.append(chr(o + 0x60))
        elif 0x30A0 <= o <= 0x30FF:      # カタカナ（ー・中黒・小書き・濁点付き含む）
            out.append(ch)
        elif 0x20 <= o <= 0x7E:          # 半角ASCII（英数字・＠等の記号）
            out.append(ch)
        # それ以外（漢字・絵文字など未収録）は破棄
    r = "".join(out)
    b = r.encode("utf-8")
    if len(b) > max_bytes:               # UTF-8バイト境界で安全に切る
        r = b[:max_bytes].decode("utf-8", "ignore")
    return r


def gw_display_name(name: str | None, yomi: str | None) -> str:
    """GWに送る最終表示名を決める（ユーザー確定仕様）：
      ・名前が英数字ハンドル(TKD/774等) → そのまま英数字で表示
      ・日本語名 → yomi(ひらがな読み)をカタカナ化して表示（読みが無ければ名前の正規化結果）
    どちらもカタカナ＋半角ASCIIに正規化済み（jpfontで出せる文字のみ）。"""
    nm = to_gw_name(name)
    # ★20261117 英数字ハンドル判定は「元の名前（括弧と中身を除く）が全部ASCII」で行う。
    #   以前は漢字を捨てた後の結果で判定していたため「VZ太郎」→「VZ」になっていた。
    import re as _re
    import unicodedata as _ud
    _raw = _re.sub(r"\([^)]*\)?", "", _ud.normalize("NFKC", str(name or ""))).strip()
    if nm and _raw and all(ord(c) < 0x80 for c in _raw):   # 全部ASCII＝英数字ハンドル
        return nm
    y = to_gw_name(yomi)
    return y or nm


# ── 五十音（あいうえお）ソートキー。エントリー一覧・名簿の並び替え用（20261115）──
#  読み(yomi)優先、無ければ名前を使う。カタカナ→ひらがな・全半角はNFKCで正規化して
#  比較するため、ひらがな／カタカナ読みが混ざっても同じ「あ〜わ」位置にそろう。
#  かな始まり→先頭グループ(0)、英数字・記号始まり(69/ATSUSHI等)→末尾グループ(1)。
def gojuon_sort_key(name: str | None, yomi: str | None):
    import unicodedata
    raw = (yomi or "").strip() or (name or "").strip()
    s = unicodedata.normalize("NFKC", raw)
    out = []
    for ch in s:
        o = ord(ch)
        if 0x30A1 <= o <= 0x30F6:        # カタカナ → ひらがな
            ch = chr(o - 0x60)
        out.append(ch)
    s = "".join(out).lower()
    first = s[0] if s else ""
    is_kana = bool(first) and (0x3041 <= ord(first) <= 0x3096 or first in "ーゝゞ")
    return (0 if is_kana else 1, s)