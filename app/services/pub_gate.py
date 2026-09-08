"""参加者向けHTML（レーサー用QR）のサーバー側24時間ゲート。

背景（不具合）:
    従来の有効期限は「/enter が localStorage に発行時刻を書き、判定は全て
    ブラウザ側」のソフト方式（案①）だった。このため
      - /enter のURLが固定（QRの中身が不変）なので、ブラウザ履歴・ブックマーク・
        共有URLから /enter を開き直すだけで、QRを再スキャンせずに無期限に延長できた
      - PWA（ホーム画面アイコン）の期限切れオーバーレイの「更新」ボタンが
        /enter?src=rescan を踏み、これも無条件で延長扱いになっていた
    → 「QRスキャンから24時間で失効」がサーバー側では一切担保されていなかった。

本モジュール（案②・サーバー署名方式）:
    1) QRトークン k
       QRのURLを /enter?k=<HMAC(secret, 時刻窓)> にする。
       時刻窓は WINDOW_SEC（既定24時間）単位で自動的に切り替わり、
       サーバーは「現在の窓」と「1つ前の窓」の k だけを受理する。
       → 過去にコピーしたURL（履歴・共有）は最長48時間で必ず無効になる。
       管理画面のQRは表示のたびに現在の k で生成されるため、運用は従来どおり
       「管理画面のQRを見せる」だけでよい（※印刷したQRは定期的に刷り直しが必要）。

    2) 発行クッキー
       有効な k で /enter を通過した端末に、署名付きクッキー
       <issued_ts>.<HMAC(secret, issued_ts)> を発行する（有効 TTL_SEC=24時間）。
       観覧ページは /api/pub-status を30秒ごとに確認し、サーバーが
       expired/invalid と判定したら自動更新を停止してオーバーレイを表示する。
       判定はサーバー時刻・サーバー署名で行うため、端末の時計変更や
       localStorage の書き換えでは延長できない。

    secret は店舗ごとの admin_token（クラウド）／環境変数 ADMIN_TOKEN（単一構成）
    から導出する。どちらも無い環境では secret が空になり、ゲートは無効
    （従来のクライアント側ソフト方式のみ）として後方互換で動作する。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import time

# QRトークンの時刻窓（署名付きURL（非常用）の時刻窓）
WINDOW_SEC = 12 * 60 * 60
# 発行クッキーの有効期間（スキャンから観覧できる時間）＝観覧の有効期限
TTL_SEC = 12 * 60 * 60

_SIG_LEN = 16  # 署名の16進表現の使用桁数（64bit相当・用途上十分）


def secret_for(store) -> str:
    """署名鍵の元になるシークレット文字列。空文字ならゲート無効。"""
    tok = getattr(store, "admin_token", "") if store is not None else ""
    if tok:
        return str(tok)
    return os.environ.get("ADMIN_TOKEN", "") or ""


def _key(secret: str) -> bytes:
    # admin_token をそのまま使わず、用途文字列を混ぜて導出する
    return hashlib.sha256(("m4-pub-gate:" + secret).encode("utf-8")).digest()


def _sign(secret: str, msg: str) -> str:
    return hmac.new(_key(secret), msg.encode("utf-8"), hashlib.sha256).hexdigest()[:_SIG_LEN]


# ---------------- 世代（epoch）＝強制失効スイッチ ----------------
# 署名メッセージに世代番号を混ぜる。管理画面から世代を+1すると、発行済みの
# 全クッキーと表示中QRの k が「その瞬間に」全端末で無効になる（強制排除）。
# 世代は各店舗DBの app_settings('pub_gate_epoch') に保存する。

_EPOCH_KEY = "pub_gate_epoch"


def db_path_for(store) -> str:
    """店舗のDBパス（単一構成は既定DB）。取れなければ空文字＝世代0扱い。"""
    p = getattr(store, "db_path", "") if store is not None else ""
    if p:
        return str(p)
    try:
        from app.models.database import DB_PATH
        return str(DB_PATH)
    except Exception:
        return ""


def get_epoch(db_path: str) -> int:
    """現在の世代番号（未設定・読取不可は 0）。同期・軽量読み取り。"""
    if not db_path or not os.path.exists(db_path):
        return 0
    try:
        con = sqlite3.connect(db_path, timeout=3)
        try:
            row = con.execute(
                "SELECT value FROM app_settings WHERE key=?", (_EPOCH_KEY,)
            ).fetchone()
            return int(row[0]) if row and row[0] else 0
        finally:
            con.close()
    except Exception:
        return 0


def bump_epoch(db_path: str) -> int:
    """世代を+1して新しい世代番号を返す（＝全端末の即時強制失効）。"""
    cur = get_epoch(db_path)
    new = cur + 1
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.execute(
            "INSERT INTO app_settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_EPOCH_KEY, str(new)),
        )
        con.commit()
    finally:
        con.close()
    return new


# ---------------- QRトークン（k） ----------------

def qr_token(secret: str, epoch: int = 0, now: float | None = None) -> str:
    """現在の時刻窓（＋世代）に対応するQRトークン。"""
    win = int((now if now is not None else time.time()) // WINDOW_SEC)
    return _sign(secret, f"qr:{epoch}:{win}")


def verify_qr_token(secret: str, epoch: int, k: str,
                    now: float | None = None) -> bool:
    """現在の窓と1つ前の窓の k のみ受理（＝最長48時間で失効）。
    世代が違う k（強制失効前に表示されたQR）は即座に不一致になる。"""
    if not secret or not k:
        return False
    t = now if now is not None else time.time()
    win = int(t // WINDOW_SEC)
    for w in (win, win - 1):
        if hmac.compare_digest(_sign(secret, f"qr:{epoch}:{w}"), k):
            return True
    return False


# ---------------- 発行クッキー ----------------

def cookie_name(slug: str) -> str:
    return f"m4_pub_gate_{slug or 'default'}"


def issue_cookie_value(secret: str, epoch: int = 0,
                       now: float | None = None) -> str:
    ts = int(now if now is not None else time.time())
    return f"{ts}.{_sign(secret, f'ck:{epoch}:{ts}')}"


def check_cookie_value(secret: str, epoch: int, value: str | None,
                       now: float | None = None) -> tuple[str, int]:
    """クッキー値を検証する。

    Returns:
        (state, remain_sec)
        state: "valid"   … 署名正・TTL内
               "expired" … 署名正・TTL超過（＝正規発行済みだが期限切れ）
               "none"    … クッキー無し
               "invalid" … 署名不正・書式不正
    """
    if not value:
        return ("none", 0)
    if not secret:
        return ("invalid", 0)
    try:
        ts_s, sig = value.split(".", 1)
        ts = int(ts_s)
    except Exception:
        return ("invalid", 0)
    if not hmac.compare_digest(_sign(secret, f"ck:{epoch}:{ts}"), sig):
        # 世代不一致（強制失効実行後）もここに落ちる＝invalid
        return ("invalid", 0)
    t = now if now is not None else time.time()
    remain = int(ts + TTL_SEC - t)
    if remain <= 0:
        return ("expired", 0)
    # 未来時刻のクッキー（時計異常・改竄）も不正扱い
    if ts > t + 300:
        return ("invalid", 0)
    return ("valid", remain)
