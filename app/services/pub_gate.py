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
import time

# QRトークンの時刻窓（この間隔でQRの中身が自動更新される）
WINDOW_SEC = 24 * 60 * 60
# 発行クッキーの有効期間（スキャンから観覧できる時間）
TTL_SEC = 24 * 60 * 60

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


# ---------------- QRトークン（k） ----------------

def qr_token(secret: str, now: float | None = None) -> str:
    """現在の時刻窓に対応するQRトークン。"""
    win = int((now if now is not None else time.time()) // WINDOW_SEC)
    return _sign(secret, f"qr:{win}")


def verify_qr_token(secret: str, k: str, now: float | None = None) -> bool:
    """現在の窓と1つ前の窓の k のみ受理（＝最長48時間で失効）。"""
    if not secret or not k:
        return False
    t = now if now is not None else time.time()
    win = int(t // WINDOW_SEC)
    for w in (win, win - 1):
        if hmac.compare_digest(_sign(secret, f"qr:{w}"), k):
            return True
    return False


# ---------------- 発行クッキー ----------------

def cookie_name(slug: str) -> str:
    return f"m4_pub_gate_{slug or 'default'}"


def issue_cookie_value(secret: str, now: float | None = None) -> str:
    ts = int(now if now is not None else time.time())
    return f"{ts}.{_sign(secret, f'ck:{ts}')}"


def check_cookie_value(secret: str, value: str | None,
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
    if not hmac.compare_digest(_sign(secret, f"ck:{ts}"), sig):
        return ("invalid", 0)
    t = now if now is not None else time.time()
    remain = int(ts + TTL_SEC - t)
    if remain <= 0:
        return ("expired", 0)
    # 未来時刻のクッキー（時計異常・改竄）も不正扱い
    if ts > t + 300:
        return ("invalid", 0)
    return ("valid", remain)
