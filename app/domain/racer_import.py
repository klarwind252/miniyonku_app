"""レーサーマスタCSVの解析・突合（純粋関数・DB非依存）。

CSV列構成: uid, name, yomigana, is_junior （ヘッダー行あり）
  - yomigana は内部カラム yomi に対応
  - is_junior は内部カラム is_child に対応（0/1）
identity のみを対象とし、来店・料金・エントリー・隠しレーサーは対象外。
"""
import csv
import io


def norm(s) -> str:
    """突合用の正規化（前後空白除去）。"""
    return (s or "").strip()


def decode_csv_bytes(raw: bytes) -> str:
    """インポートCSVを寛容にデコードする。
    BOM付き/なしUTF-8・ShiftJIS(cp932) のいずれも受理する。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    for enc in ("utf-8", "cp932", "shift_jis"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def parse_import_csv(text: str):
    """CSVテキストを行リストに変換する。ヘッダーの揺れも吸収する。
    返り値: [{"uid","name","yomigana","is_junior","line"}, ...], errors:list[str]
    """
    errors = []
    text = text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(text))
    try:
        all_rows = list(reader)
    except Exception as e:
        return [], [f"CSVの解析に失敗しました: {e}"]
    if not all_rows:
        return [], ["CSVが空です。"]

    header = [h.strip().lower() for h in all_rows[0]]

    def idx(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    i_uid = idx("uid")
    i_name = idx("name", "名前", "氏名")
    i_yomi = idx("yomigana", "yomi", "よみがな", "よみ")
    i_jr = idx("is_junior", "is_child", "ジュニア", "junior")

    if i_name is None:
        return [], ["CSVに name 列が見つかりません。ヘッダー行（uid,name,yomigana,is_junior）を確認してください。"]

    rows = []
    for ln, raw in enumerate(all_rows[1:], start=2):
        if not any((c or "").strip() for c in raw):
            continue  # 空行スキップ

        def get(i):
            return raw[i].strip() if (i is not None and i < len(raw)) else ""

        name = get(i_name)
        if not name:
            errors.append(f"{ln}行目: name が空のためスキップしました。")
            continue
        jr_raw = get(i_jr)
        is_junior = 1 if jr_raw in ("1", "true", "True", "はい", "ジュニア") else 0
        rows.append({
            "uid": get(i_uid),
            "name": name,
            "yomigana": get(i_yomi),
            "is_junior": is_junior,
            "line": ln,
        })
    return rows, errors


def build_preview(existing_racers, rows):
    """各行を既存レーサーと突合し、状態（new/id_match/name_match/conflict）を判定する。
    突合優先順位: uid一致 → よみがな+名前一致 → 新規

    existing_racers: [{id, uid, name, yomi, is_child}, ...]（DB取得は呼び出し側の責務）
    """
    by_uid = {}
    by_namekey = {}
    for e in existing_racers:
        if e["uid"]:
            by_uid[e["uid"]] = e
        key = (norm(e["yomi"]), norm(e["name"]))
        by_namekey.setdefault(key, e)

    preview = []
    for row in rows:
        uid = norm(row["uid"])
        name = norm(row["name"])
        yomi = norm(row["yomigana"])
        is_jr = row["is_junior"]
        state = "new"
        existing_match = None
        default_action = "import"  # new の既定は取り込む

        if uid and uid in by_uid:
            em = by_uid[uid]
            existing_match = em
            if norm(em["name"]) != name or norm(em["yomi"]) != yomi or int(em["is_child"] or 0) != is_jr:
                state = "conflict"
                default_action = "skip"   # 競合の既定はスキップ（要確認）
            else:
                state = "id_match"
                default_action = "keep"   # 既存維持
        else:
            key = (yomi, name)
            if key in by_namekey:
                state = "name_match"
                existing_match = by_namekey[key]
                default_action = "skip"   # 名前のみ一致は安全側：既定で取り込まない
            else:
                state = "new"
                default_action = "import"

        preview.append({
            "line": row["line"],
            "uid": uid,
            "name": name,
            "yomigana": yomi,
            "is_junior": is_jr,
            "state": state,
            "default_action": default_action,
            "existing_id": existing_match["id"] if existing_match else None,
            "existing_name": existing_match["name"] if existing_match else None,
            "existing_yomi": (existing_match["yomi"] or "") if existing_match else None,
            "existing_is_junior": (1 if (existing_match and existing_match["is_child"]) else 0) if existing_match else None,
        })
    return preview