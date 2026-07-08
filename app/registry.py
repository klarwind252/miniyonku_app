"""
店舗レジストリ（control.db）。

複数店舗化（クラウド版のみ）の店舗マスタを、レース用DBとは別ファイル
data/control.db で管理する。レース用スキーマには一切手を入れない。

- 店舗1（マスター）: slug="" の既定店舗。移行時に1行登録される。
- 店舗2〜5        : マスター（店舗1）の設定画面から追加・編集・削除。

スラッグは予約語（admin/view/static/logo/health/api/favicon/enter）と重複不可、
英小文字・数字・ハイフンのみ。最大店舗数は MAX_STORES。

削除は「アーカイブ保持」: レジストリから無効化＆除外し、DBファイルと配信
ディレクトリは _archive/ 配下へ日付付きで退避する（物理削除しない）。
"""
from __future__ import annotations

import os
import re
import time
import secrets
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from app.store_context import Store

# ---- 配置・上限 ----------------------------------------------------------
_THIS_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.abspath(os.path.join(_THIS_DIR, "..", "data"))
CONTROL_DB_PATH = os.path.join(DATA_DIR, "control.db")
STORES_DIR = os.path.join(DATA_DIR, "stores")          # 店舗2〜のDB置き場
ARCHIVE_DIR = os.path.join(DATA_DIR, "_archive")       # 削除時の退避先
DEFAULT_DB_PATH = os.path.join(DATA_DIR, "miniyonku.db")  # 店舗1（既存DB）

MAX_STORES = 5
RESERVED_SLUGS = {
    "admin", "view", "static", "logo", "health", "api", "favicon", "enter",
    "store", "stores", "_archive",
}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")

# 利用時間制限（店舗2〜5・デモ/お試し用）で使う
JST = ZoneInfo("Asia/Tokyo")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ---- 低レベル接続（同期。レジストリは件数が少なく頻度も低いため sqlite3 で十分）----
def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(CONTROL_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def gen_token() -> str:
    return secrets.token_urlsafe(32)


def _public_base_dir() -> str:
    """参加者向けHTMLの親ディレクトリ（PUBLIC_HTML_DIR）。各店舗はこの下に slug 名で作る。"""
    from app.config import PUBLIC_HTML_DIR
    return PUBLIC_HTML_DIR or "/var/www/miniyonku_public"


def _row_to_store(row: sqlite3.Row) -> Store:
    return Store(
        id=row["id"],
        slug=row["slug"] or "",
        name=row["name"],
        db_path=row["db_path"],
        public_dir=row["public_dir"],
        admin_token=row["admin_token"],
        view_token=row["view_token"],
        enabled=bool(row["enabled"]),
        restrict_hours=bool(row["restrict_hours"]),
        access_start=row["access_start"],
        access_end=row["access_end"],
    )


# ---- 初期化＆店舗1の移行登録 --------------------------------------------
def init_registry(default_admin_token: str = "", default_view_token: str = "") -> None:
    """control.db を作成し、店舗1（既定店舗・slug="")を1行用意する。

    既存環境からの移行: 既存の data/miniyonku.db を店舗1のDBとしてそのまま使う。
    default_admin_token / default_view_token は .env の ADMIN_TOKEN / VIEW_TOKEN を
    店舗1の初期トークンとして流用するために渡す（未設定なら自動生成）。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(STORES_DIR, exist_ok=True)
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stores (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                slug         TEXT UNIQUE,
                name         TEXT NOT NULL,
                db_path      TEXT NOT NULL,
                public_dir   TEXT NOT NULL,
                admin_token  TEXT NOT NULL,
                view_token   TEXT NOT NULL,
                enabled      INTEGER NOT NULL DEFAULT 1,
                restrict_hours INTEGER NOT NULL DEFAULT 0,
                access_start TEXT,
                access_end   TEXT,
                created_at   TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.commit()

        # 既存DB向けマイグレーション（列が無ければ追加。店舗2〜5の利用時間制限機能用）
        existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(stores)").fetchall()}
        if "restrict_hours" not in existing_cols:
            conn.execute("ALTER TABLE stores ADD COLUMN restrict_hours INTEGER NOT NULL DEFAULT 0")
        if "access_start" not in existing_cols:
            conn.execute("ALTER TABLE stores ADD COLUMN access_start TEXT")
        if "access_end" not in existing_cols:
            conn.execute("ALTER TABLE stores ADD COLUMN access_end TEXT")
        conn.commit()

        # 店舗1（既定店舗）が無ければ作る
        cur = conn.execute("SELECT COUNT(*) AS n FROM stores WHERE slug IS NULL OR slug=''")
        if cur.fetchone()["n"] == 0:
            conn.execute(
                "INSERT INTO stores (slug, name, db_path, public_dir, admin_token, view_token, enabled) "
                "VALUES (?,?,?,?,?,?,1)",
                (
                    "",                       # 既定店舗はスラッグなし
                    "店舗1",
                    DEFAULT_DB_PATH,          # 既存DBをそのまま店舗1に
                    _public_base_dir(),       # 店舗1はPUBLIC_HTML_DIR直下（従来どおりルート配信）
                    default_admin_token or gen_token(),
                    default_view_token or gen_token(),
                ),
            )
            conn.commit()
    finally:
        conn.close()


# ---- 参照 ----------------------------------------------------------------
def list_stores(include_disabled: bool = True) -> list[Store]:
    conn = _connect()
    try:
        q = "SELECT * FROM stores"
        if not include_disabled:
            q += " WHERE enabled=1"
        q += " ORDER BY id"
        return [_row_to_store(r) for r in conn.execute(q).fetchall()]
    finally:
        conn.close()


def get_default_store() -> Optional[Store]:
    conn = _connect()
    try:
        r = conn.execute("SELECT * FROM stores WHERE slug IS NULL OR slug='' LIMIT 1").fetchone()
        return _row_to_store(r) if r else None
    finally:
        conn.close()


def get_store_by_slug(slug: str) -> Optional[Store]:
    if not slug:
        return get_default_store()
    conn = _connect()
    try:
        r = conn.execute("SELECT * FROM stores WHERE slug=? LIMIT 1", (slug,)).fetchone()
        return _row_to_store(r) if r else None
    finally:
        conn.close()


def get_store_by_id(store_id: int) -> Optional[Store]:
    conn = _connect()
    try:
        r = conn.execute("SELECT * FROM stores WHERE id=? LIMIT 1", (store_id,)).fetchone()
        return _row_to_store(r) if r else None
    finally:
        conn.close()


# ---- バリデーション ------------------------------------------------------
def validate_slug(slug: str, exclude_id: Optional[int] = None) -> Optional[str]:
    """OKなら None、NGなら理由文字列を返す。"""
    slug = (slug or "").strip().lower()
    if not slug:
        return "スラッグを入力してください。"
    if slug in RESERVED_SLUGS:
        return f"'{slug}' は予約語のため使用できません。"
    if not _SLUG_RE.match(slug):
        return "スラッグは英小文字・数字・ハイフンのみ（2〜31文字・先頭は英数字）です。"
    conn = _connect()
    try:
        r = conn.execute("SELECT id FROM stores WHERE slug=?", (slug,)).fetchone()
        if r and r["id"] != exclude_id:
            return f"スラッグ '{slug}' は既に使われています。"
    finally:
        conn.close()
    return None


# ---- 追加・編集・有効無効・削除（アーカイブ）----------------------------
def add_store(name: str, slug: str) -> Store:
    name = (name or "").strip() or slug
    slug = (slug or "").strip().lower()

    err = validate_slug(slug)
    if err:
        raise ValueError(err)

    # 最大店舗数（有効・無効問わず登録行数で判定）
    if len(list_stores(include_disabled=True)) >= MAX_STORES:
        raise ValueError(f"店舗は最大 {MAX_STORES} 店舗までです。")

    db_path = os.path.join(STORES_DIR, slug, "miniyonku.db")
    public_dir = os.path.join(_public_base_dir(), slug)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO stores (slug, name, db_path, public_dir, admin_token, view_token, enabled) "
            "VALUES (?,?,?,?,?,?,1)",
            (slug, name, db_path, public_dir, gen_token(), gen_token()),
        )
        conn.commit()
        r = conn.execute("SELECT * FROM stores WHERE slug=?", (slug,)).fetchone()
        return _row_to_store(r)
    finally:
        conn.close()


def validate_time_str(t: str) -> Optional[str]:
    """OKなら None、NGなら理由文字列を返す（HH:MM 形式チェック）。"""
    if not t:
        return None
    if not _TIME_RE.match(t):
        return f"時刻の形式が不正です（HH:MM で入力してください）: {t}"
    return None


def update_store(store_id: int, name: Optional[str] = None,
                 slug: Optional[str] = None, enabled: Optional[bool] = None,
                 restrict_hours: Optional[bool] = None,
                 access_start: Optional[str] = None,
                 access_end: Optional[str] = None) -> Store:
    store = get_store_by_id(store_id)
    if not store:
        raise ValueError("店舗が見つかりません。")
    if not store.slug:
        # 店舗1（既定店舗）はスラッグ変更・無効化・時間制限を許可しない（名前のみ可）
        if slug not in (None, "") or enabled is False:
            raise ValueError("店舗1（既定店舗）のスラッグ変更・無効化はできません。")
        if restrict_hours or access_start or access_end:
            raise ValueError("店舗1（既定店舗）には利用時間制限を設定できません。")

    new_slug = store.slug
    if slug is not None and store.slug:  # 店舗2〜のみスラッグ変更可
        slug = slug.strip().lower()
        err = validate_slug(slug, exclude_id=store_id)
        if err:
            raise ValueError(err)
        new_slug = slug

    new_name = (name.strip() if name else None) or store.name
    new_enabled = store.enabled if enabled is None else bool(enabled)

    # 利用時間制限（店舗2〜5のみ）
    new_restrict = store.restrict_hours if restrict_hours is None else bool(restrict_hours)
    new_start = store.access_start if access_start is None else (access_start.strip() or None)
    new_end = store.access_end if access_end is None else (access_end.strip() or None)
    for _t in (new_start, new_end):
        if _t:
            err = validate_time_str(_t)
            if err:
                raise ValueError(err)
    if new_restrict and (not new_start or not new_end):
        raise ValueError("利用時間制限をONにする場合は開始・終了時刻を両方入力してください。")

    # スラッグ変更時は配信ディレクトリ名も追従（参加者URL/QRが変わる旨はUIで警告）
    new_public_dir = store.public_dir
    if store.slug and new_slug != store.slug:
        new_public_dir = os.path.join(_public_base_dir(), new_slug)

    conn = _connect()
    try:
        conn.execute(
            "UPDATE stores SET slug=?, name=?, public_dir=?, enabled=?, "
            "restrict_hours=?, access_start=?, access_end=? WHERE id=?",
            (new_slug or "", new_name, new_public_dir, 1 if new_enabled else 0,
             1 if new_restrict else 0, new_start, new_end, store_id),
        )
        conn.commit()
        return get_store_by_id(store_id)
    finally:
        conn.close()


def regenerate_tokens(store_id: int) -> Store:
    conn = _connect()
    try:
        conn.execute("UPDATE stores SET admin_token=?, view_token=? WHERE id=?",
                     (gen_token(), gen_token(), store_id))
        conn.commit()
        return get_store_by_id(store_id)
    finally:
        conn.close()


def delete_store(store_id: int) -> None:
    """アーカイブ保持で削除する。

    - 店舗1（既定店舗）は削除不可。
    - レジストリから行を削除し、DBファイル・配信ディレクトリは _archive/ へ
      日付付きで退避（mv）。物理削除はしない。
    """
    store = get_store_by_id(store_id)
    if not store:
        raise ValueError("店舗が見つかりません。")
    if not store.slug:
        raise ValueError("店舗1（既定店舗）は削除できません。")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    arch_root = os.path.join(ARCHIVE_DIR, f"{store.slug}_{stamp}")
    os.makedirs(arch_root, exist_ok=True)

    # DBファイル一式（-wal/-shm 含む）を退避
    try:
        src_db_dir = os.path.dirname(store.db_path)
        if os.path.isdir(src_db_dir):
            os.replace(src_db_dir, os.path.join(arch_root, "db"))
    except Exception as e:
        print(f"[registry] archive db move failed: {e}", flush=True)

    # 配信ディレクトリを退避
    try:
        if os.path.isdir(store.public_dir):
            os.replace(store.public_dir, os.path.join(arch_root, "public"))
    except Exception as e:
        print(f"[registry] archive public move failed: {e}", flush=True)

    conn = _connect()
    try:
        conn.execute("DELETE FROM stores WHERE id=?", (store_id,))
        conn.commit()
    finally:
        conn.close()
    print(f"[registry] store '{store.slug}' archived -> {arch_root}", flush=True)


# ---- 利用時間制限（店舗2〜5・デモ/お試し用） ------------------------------
def is_store_open(store: Store) -> bool:
    """時間制限が無効、または店舗1（既定店舗）なら常に True。

    制限有効な店舗は現在時刻（JST）が [access_start, access_end) の範囲内かを判定する。
    access_end < access_start の場合は日をまたぐ時間帯として扱う（例: 20:00〜02:00）。
    制限ONだが時刻未設定の場合はフェイルオープン（安全側で常時許可）とする。
    """
    if not store.slug:
        return True
    if not store.restrict_hours:
        return True
    if not store.access_start or not store.access_end:
        return True
    now = datetime.now(JST).strftime("%H:%M")
    start, end = store.access_start, store.access_end
    if start <= end:
        return start <= now < end
    return now >= start or now < end
