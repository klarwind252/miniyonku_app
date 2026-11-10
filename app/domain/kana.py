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
#  ※名前はGW側 char[24]（UTF-8・カタカナ3バイト/字）に収めるため、UTF-8で23バイトに切る。
#   画面側は右はみ出しをクリップするので、長い名前はこの範囲で十分。
def to_gw_name(s: str | None, max_bytes: int = 23) -> str:
    import unicodedata
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
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
    if nm and all(ord(c) < 0x80 for c in nm):   # 全部ASCII＝英数字ハンドル
        return nm
    y = to_gw_name(yomi)
    return y or nm