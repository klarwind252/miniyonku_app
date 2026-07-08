"""
デモ（お試し）予約の中核ロジック。

運用イメージ:
  1. 公開フォーム（/reserve）で日時をカレンダー選択し、メールアドレスだけで申込む。
  2. 申込を受けると、デモ用店舗（demo1〜demo4）のうち、その時間帯に空いている
     1店舗を自動で確保し、その店舗のトークンを再生成する。
  3. 確保した店舗の admin / view URL とテンプレート文を、申込メールへ自動送信する。
  4. 予約時間内だけ、その店舗はアクセス可能（reservation-based gating）。
  5. 予約終了時刻を過ぎると自動的にクローズし、店舗は次の予約に再利用される。

設計方針:
  - 予約表 demo_reservations は control.db（店舗レジストリと同じDB）に持つ。
    レース用スキーマ（miniyonku.db）には一切手を入れない。
  - デモ店舗の開閉は「予約表」で判定する（日付＋時刻）。registry.is_store_open() が
    デモ店舗についてはここへ委譲する。予約が無いデモ店舗は常にクローズ（=再利用可能）。
  - 追加の pip 依存なし（sqlite3 / 標準ライブラリのみ）。

環境変数（すべて任意・既定値あり）:
  DEMO_ENABLED         : "true"/"false"（既定 true）。デモ予約機能の有効/無効。
  DEMO_STORE_SLUGS     : カンマ区切り（既定 "demo1,demo2,demo3,demo4"）。デモ店舗スラッグ。
  DEMO_STORE_NAME_FMT  : 店舗名テンプレ（既定 "お試し{n}"）。{n} に 1..N が入る。
  DEMO_SLOT_MINUTES    : 1枠の長さ・分（既定 60）。
  DEMO_DAY_START       : 受付開始 HH:MM（既定 "10:00"）。
  DEMO_DAY_END         : 受付終了 HH:MM（既定 "20:00"）。この時刻に終わる枠まで。
  DEMO_DAYS_AHEAD      : 何日先まで予約可か（既定 14）。当日を含む。
  DEMO_LEAD_MINUTES    : 現在時刻から最低これだけ先の枠のみ予約可（既定 10）。
  DEMO_SEND_ADMIN_URL  : "true"/"false"（既定 true）。管理URL(admin)を案内するか。
  DEMO_SEND_VIEW_URL   : "true"/"false"（既定 true）。観覧URL(view)を案内するか。
  DEMO_SEED_DB         : 予約時にコピーして初期化するサンプルDBの絶対パス（任意）。
                         指定時は各セッション開始前にデモ店舗DBをこの内容へリセットする。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app import registry
from app.store_context import Store

JST = ZoneInfo("Asia/Tokyo")
_FMT = "%Y-%m-%d %H:%M"          # 予約日時の保存・表示フォーマット（JSTローカル・naive）


# ----------------------------------------------------------------------------
# 設定アクセサ
# ----------------------------------------------------------------------------
def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    return _bool(os.environ.get("DEMO_ENABLED"), True)


def demo_slugs() -> list[str]:
    raw = os.environ.get("DEMO_STORE_SLUGS", "demo1,demo2,demo3,demo4")
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def _name_fmt() -> str:
    return os.environ.get("DEMO_STORE_NAME_FMT", "お試し{n}")


def slot_minutes() -> int:
    try:
        return max(5, int(os.environ.get("DEMO_SLOT_MINUTES", "60")))
    except ValueError:
        return 60


def _day_start() -> str:
    return os.environ.get("DEMO_DAY_START", "10:00")


def _day_end() -> str:
    return os.environ.get("DEMO_DAY_END", "20:00")


def _days_ahead() -> int:
    try:
        return max(0, int(os.environ.get("DEMO_DAYS_AHEAD", "14")))
    except ValueError:
        return 14


def _lead_minutes() -> int:
    try:
        return max(0, int(os.environ.get("DEMO_LEAD_MINUTES", "10")))
    except ValueError:
        return 10


def send_admin_url() -> bool:
    return _bool(os.environ.get("DEMO_SEND_ADMIN_URL"), True)


def send_view_url() -> bool:
    return _bool(os.environ.get("DEMO_SEND_VIEW_URL"), True)


def _seed_db() -> str:
    return os.environ.get("DEMO_SEED_DB", "").strip()


# ----------------------------------------------------------------------------
# 時刻ユーティリティ
# ----------------------------------------------------------------------------
def now_jst() -> datetime:
    """JSTの現在時刻（naive）。DB保存値と同じ土俵で比較するため tz を落とす。"""
    return datetime.now(JST).replace(tzinfo=None)


def _hm_to_minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    for fmt in (_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def fmt_dt(dt: datetime) -> str:
    return dt.strftime(_FMT)


# ----------------------------------------------------------------------------
# control.db 接続 & 初期化
# ----------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    os.makedirs(registry.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(registry.CONTROL_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_demo() -> None:
    """予約テーブルを作成する（無ければ）。"""
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS demo_reservations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT NOT NULL,
                store_id   INTEGER NOT NULL,
                slug       TEXT NOT NULL,
                start_dt   TEXT NOT NULL,   -- 'YYYY-MM-DD HH:MM'（JST）
                end_dt     TEXT NOT NULL,   -- 'YYYY-MM-DD HH:MM'（JST）
                status     TEXT NOT NULL DEFAULT 'active',  -- active | cancelled
                mailed     INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_demo_res_store "
            "ON demo_reservations(store_id, status)"
        )
        conn.commit()
    finally:
        conn.close()


def ensure_demo_stores() -> list[Store]:
    """デモ店舗（demo1〜）がレジストリに無ければ作成し、一覧を返す。

    既存の registry.add_store() を使う（MAX_STORES 上限の範囲内で作成）。
    既に存在すれば作らない。無効化されていれば有効化する。
    """
    slugs = demo_slugs()
    existing = {s.slug: s for s in registry.list_stores(include_disabled=True)}
    fmt = _name_fmt()
    for i, slug in enumerate(slugs, start=1):
        st = existing.get(slug)
        if st is None:
            try:
                registry.add_store(name=fmt.format(n=i), slug=slug)
            except Exception as e:
                # MAX_STORES 到達などは警告のみ（既存店舗運用を壊さない）
                print(f"[demo] ensure store '{slug}' skipped: {e}", flush=True)
        elif not st.enabled:
            try:
                registry.update_store(st.id, enabled=True)
            except Exception as e:
                print(f"[demo] re-enable '{slug}' failed: {e}", flush=True)
    return [s for s in registry.list_stores(include_disabled=True) if s.slug in set(slugs)]


def demo_store_ids() -> list[int]:
    slugs = set(demo_slugs())
    return [s.id for s in registry.list_stores(include_disabled=True)
            if s.slug in slugs and s.enabled]


def is_demo_store(store: Store) -> bool:
    return bool(store.slug) and store.slug in set(demo_slugs())


# ----------------------------------------------------------------------------
# 予約に基づく開閉判定（registry.is_store_open から委譲される）
# ----------------------------------------------------------------------------
def active_reservation(store_id: int, at: Optional[datetime] = None) -> Optional[sqlite3.Row]:
    """指定時刻を含む active な予約（start<=at<end）を返す。無ければ None。"""
    at = at or now_jst()
    key = fmt_dt(at)
    conn = _connect()
    try:
        r = conn.execute(
            "SELECT * FROM demo_reservations "
            "WHERE store_id=? AND status='active' AND start_dt<=? AND end_dt>? "
            "ORDER BY start_dt LIMIT 1",
            (store_id, key, key),
        ).fetchone()
        return r
    finally:
        conn.close()


def is_demo_store_open(store: Store, at: Optional[datetime] = None) -> bool:
    """デモ店舗は「現在有効な予約がある時だけ」開く。無ければ常にクローズ。"""
    return active_reservation(store.id, at) is not None


def next_reservation(store_id: int, at: Optional[datetime] = None) -> Optional[sqlite3.Row]:
    """指定時刻以降の直近の active 予約（案内表示用）。"""
    at = at or now_jst()
    key = fmt_dt(at)
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM demo_reservations "
            "WHERE store_id=? AND status='active' AND end_dt>? "
            "ORDER BY start_dt LIMIT 1",
            (store_id, key),
        ).fetchone()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# 空き計算（カレンダー表示・予約時の確保）
# ----------------------------------------------------------------------------
def _free_store_ids(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[int]:
    """[start,end) と重なる active 予約を持たないデモ店舗IDの一覧。"""
    ids = demo_store_ids()
    if not ids:
        return []
    s_key, e_key = fmt_dt(start), fmt_dt(end)
    # 重なり条件: existing.start < req.end AND existing.end > req.start
    busy = {
        row["store_id"]
        for row in conn.execute(
            "SELECT DISTINCT store_id FROM demo_reservations "
            "WHERE status='active' AND start_dt<? AND end_dt>?",
            (e_key, s_key),
        ).fetchall()
    }
    return [i for i in ids if i not in busy]


def slot_capacity(start: datetime) -> int:
    """その枠に確保可能なデモ店舗数（0なら満席）。"""
    end = start + timedelta(minutes=slot_minutes())
    conn = _connect()
    try:
        return len(_free_store_ids(conn, start, end))
    finally:
        conn.close()


def list_days() -> list[str]:
    """予約可能な日付（YYYY-MM-DD）の一覧。"""
    today = now_jst().date()
    return [(today + timedelta(days=d)).strftime("%Y-%m-%d")
            for d in range(_days_ahead() + 1)]


def slots_for_day(day: str) -> list[dict]:
    """指定日の枠一覧（開始時刻・空き数）。空きが無い枠も available=false で返す。"""
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return []
    step = slot_minutes()
    start_m = _hm_to_minutes(_day_start())
    end_m = _hm_to_minutes(_day_end())
    now = now_jst()
    lead = timedelta(minutes=_lead_minutes())
    total = len(demo_store_ids())

    out: list[dict] = []
    conn = _connect()
    try:
        m = start_m
        while m + step <= end_m:
            sh, sm = divmod(m, 60)
            start = datetime(d.year, d.month, d.day, sh, sm)
            end = start + timedelta(minutes=step)
            # 過去枠・リード時間内は締切
            bookable = start >= now + lead
            free = len(_free_store_ids(conn, start, end)) if bookable else 0
            out.append({
                "start": fmt_dt(start),
                "label": f"{sh:02d}:{sm:02d}",
                "end_label": end.strftime("%H:%M"),
                "capacity": total,
                "free": free,
                "available": bookable and free > 0,
            })
            m += step
    finally:
        conn.close()
    return out


# ----------------------------------------------------------------------------
# 予約の確定 / 取り消し
# ----------------------------------------------------------------------------
class SlotFull(Exception):
    pass


class SlotInvalid(Exception):
    pass


def reserve(email: str, start_str: str) -> tuple[sqlite3.Row, Store, str, str]:
    """1枠を確保し、店舗トークンを再生成する。

    返り値: (reservation_row, store, admin_token, view_token)
    例外: SlotInvalid（枠が不正/期限切れ） / SlotFull（満席）
    """
    start = parse_dt(start_str)
    if start is None:
        raise SlotInvalid("日時の形式が正しくありません。")

    # 枠境界（DAY_START からの grid）に一致しているか軽く検証
    step = slot_minutes()
    if (start.hour * 60 + start.minute - _hm_to_minutes(_day_start())) % step != 0:
        raise SlotInvalid("選択できない時間帯です。")
    if start < now_jst() + timedelta(minutes=_lead_minutes()):
        raise SlotInvalid("その時間帯は受付を終了しました。別の枠を選んでください。")

    end = start + timedelta(minutes=step)
    s_key, e_key = fmt_dt(start), fmt_dt(end)

    conn = _connect()
    try:
        # 二重確保防止のため即時ロック
        conn.execute("BEGIN IMMEDIATE")
        free = _free_store_ids(conn, start, end)
        if not free:
            conn.execute("ROLLBACK")
            raise SlotFull("その時間帯は満席です。別の枠を選んでください。")
        store_id = free[0]
        store = registry.get_store_by_id(store_id)
        slug = store.slug if store else ""
        conn.execute(
            "INSERT INTO demo_reservations (email, store_id, slug, start_dt, end_dt, status) "
            "VALUES (?,?,?,?,?, 'active')",
            (email, store_id, slug, s_key, e_key),
        )
        res_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.close()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # セッション開始前にDBをサンプルへリセット（任意）
    seed = _seed_db()
    if seed and os.path.isfile(seed) and store:
        try:
            _reset_store_db(store, seed)
        except Exception as e:
            print(f"[demo] seed reset failed for '{store.slug}': {e}", flush=True)

    # トークン再生成（案内直前に実行 → 前回利用者の鍵を無効化）
    store = registry.regenerate_tokens(store_id)

    row = get_reservation(res_id)
    return row, store, store.admin_token, store.view_token


def _reset_store_db(store: Store, seed_path: str) -> None:
    """デモ店舗のDBをサンプルDBの内容で上書きする（WAL/SHMも掃除）。"""
    dst = store.db_path
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    for suffix in ("-wal", "-shm"):
        p = dst + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    shutil.copyfile(seed_path, dst)


def get_reservation(res_id: int) -> Optional[sqlite3.Row]:
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM demo_reservations WHERE id=?", (res_id,)
        ).fetchone()
    finally:
        conn.close()


def mark_mailed(res_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE demo_reservations SET mailed=1 WHERE id=?", (res_id,))
        conn.commit()
    finally:
        conn.close()


def cancel_reservation(res_id: int) -> None:
    """予約を取り消す（メール送信失敗時のロールバック等）。店舗は即再利用可能になる。"""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE demo_reservations SET status='cancelled' WHERE id=?", (res_id,)
        )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# 案内URL・メール本文
# ----------------------------------------------------------------------------
def _base_url() -> str:
    """案内メール用の公開ベースURLを決める。

    - 末尾スラッシュは除去。
    - DEMO_FORCE_HTTPS=true（既定）なら http:// を https:// へ寄せる。
      逆引きホスト名/IP を http で設定してしまっても、案内URLは https になる。
    - PUBLIC_BASE_URL 未設定時は DEMO_PUBLIC_BASE_URL を代替に使う（任意）。
    """
    from app.config import PUBLIC_BASE_URL
    base = (PUBLIC_BASE_URL or os.environ.get("DEMO_PUBLIC_BASE_URL", "")).strip()
    base = base.rstrip("/")
    if base and _bool(os.environ.get("DEMO_FORCE_HTTPS"), True):
        if base.startswith("http://"):
            base = "https://" + base[len("http://"):]
        elif not base.startswith("https://"):
            base = "https://" + base
    return base


def build_urls(store: Store, admin_token: str, view_token: str) -> dict:
    base = _base_url()
    prefix = store.prefix  # 例 "/demo1"
    return {
        "admin": f"{base}{prefix}/admin/?key={admin_token}",
        "view": f"{base}{prefix}/view/?key={view_token}",
        "html": f"{base}{prefix}/",
    }


def build_mail(email: str, store: Store, start: datetime, end: datetime,
               urls: dict) -> tuple[str, str]:
    """(subject, body) を返す。DEMO_MAIL_SUBJECT / _BODY で上書き可。"""
    subject = os.environ.get(
        "DEMO_MAIL_SUBJECT",
        "【ミニ四駆レース管理システム】お試しプレイ用URLのご案内",
    )
    lines = [
        "この度はお試しプレイにお申し込みいただき、ありがとうございます。",
        "",
        f"■ ご利用日時： {fmt_dt(start)} 〜 {end.strftime('%H:%M')}",
        "  （この時間内のみアクセスできます。時間を過ぎると自動的に終了します）",
        "",
    ]
    if send_admin_url():
        lines += [
            "▼ 管理画面（実際に操作してお試しいただけます）",
            f"  {urls['admin']}",
            "",
        ]
    if send_view_url():
        lines += [
            "▼ 観覧画面（結果表示・進行の確認用）",
            f"  {urls['view']}",
            "",
        ]
    lines += [
        "※上記URLは今回のお試し専用です。第三者への共有はお控えください。",
        "※ご利用時間が終了すると、URLは自動的に無効になります。",
        "",
        "――――――――――――――――",
        "ミニ四駆レース管理システム（お試し自動案内）",
    ]
    # 全文テンプレを .env で差し替えたい場合（{admin_url} 等のプレースホルダ対応）
    override = os.environ.get("DEMO_MAIL_BODY")
    if override:
        body = (override
                .replace("{start}", fmt_dt(start))
                .replace("{end}", end.strftime("%H:%M"))
                .replace("{admin_url}", urls.get("admin", ""))
                .replace("{view_url}", urls.get("view", ""))
                .replace("{html_url}", urls.get("html", ""))
                .replace("{email}", email))
    else:
        body = "\n".join(lines)
    return subject, body
