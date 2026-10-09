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
import secrets as _secrets
import sqlite3
import time

# QRトークンの時刻窓（署名付きURL（非常用）の時刻窓）
WINDOW_SEC = 12 * 60 * 60
# 発行クッキーの有効期間（スキャンから観覧できる時間）＝観覧の有効期限
TTL_SEC = 12 * 60 * 60

_SIG_LEN = 16  # 署名の16進表現の使用桁数（64bit相当・用途上十分）

# ===========================================================================
# 毎日ローテーションする参加者入口（デイリー・ワード）
# ---------------------------------------------------------------------------
# 従来の固定 /enter は「踏むたびに12時間を発行」するため、一度QRを踏んだ端末が
# URLをブックマーク／共有して無期限に再入場できた（＝常時観覧の穴）。
# これを塞ぐため、発行できる入口URLを 1日ごとに変わるランダムな単語にする。
#   - 入口URL例:  https://<host>/enter/<毎日変わる単語>   （既定・nginx無改修で動く）
#   - 素の /enter は【一切発行しない】（有効クッキーの通過のみ）
#   - 昨日以前の単語URLも発行不可（WORD_ACCEPT_PREV_DAY で猶予可）
#
# 単語は secret（店舗ごと）＋ epoch（強制失効の世代）＋ 日付インデックスから
# HMAC で決定的に生成する。サーバーは「今日の単語」を都度計算して照合するだけで、
# DBに保存する必要はない（強制失効＝世代+1 を行えば、その瞬間に単語も変わる）。
# ===========================================================================

# 1日の区切り（JST）。要望:「09:00-08:59」= 毎日 09:00 JST に切り替わる。
# 09:00 JST は 00:00 UTC と一致するため、内部計算は UTC エポックの日割りで済む。
DAY_START_HOUR_JST = 9
_TZ_OFFSET_SEC = 9 * 60 * 60  # JST = UTC+9

# 入口URLの形:
#   "enter" … /enter/<単語>   （既定。nginx 無改修で確実に動く）
#   "root"  … /<単語>         （ドメイン直下。nginx に1ブロック追加が必要）
ENTRY_PATH_STYLE = "enter"

# 昨日の単語URLでも発行を許すか（True＝約2日間の重なりを許容 / False＝当日のみ＝厳密日替わり）
WORD_ACCEPT_PREV_DAY = False

# 読みやすい「ランダムワード」を作るための語彙（衝突回避のため末尾に短い英数字も付与）。
_WL_ADJ = (
    "akai", "aoi", "hayai", "tsuyoi", "kiiro", "midori", "shiro", "kuro",
    "ginga", "hikari", "kaze", "honoo", "inazuma", "arashi", "yuki", "tsuki",
    "hoshi", "ryu", "tora", "washi", "taka", "kuma", "ookami", "hayate",
    "shippu", "raimei", "sora", "umi", "mori", "iwa", "tetsu", "kurogane",
    "shinku", "ougon", "hagane", "shippo", "mach", "turbo", "nitro", "sonic",
)
_WL_NOUN = (
    "dash", "racer", "booster", "motor", "circuit", "roller", "gear", "shaft",
    "wing", "frame", "chassis", "bumper", "stay", "guide", "tire", "axle",
    "spring", "damper", "brake", "charger", "pit", "lane", "course", "gate",
    "flag", "pole", "lap", "turn", "straight", "corner", "slope", "jump",
    "comet", "rocket", "falcon", "tiger", "dragon", "phoenix", "cobra", "wolf",
)


def _day_index(now: float | None = None) -> int:
    """1日の区切り（既定: 09:00 JST = 00:00 UTC）に基づく日付インデックス。"""
    t = now if now is not None else time.time()
    # JST ローカル秒へ変換し、区切り時刻ぶん手前にずらして日割り。
    jst = t + _TZ_OFFSET_SEC
    shifted = jst - DAY_START_HOUR_JST * 3600
    return int(shifted // 86400)


def _day_expiry(ts: float) -> float:
    """ts が属する論理日の終わり（＝次の 09:00 JST）の UTC エポック秒。

    発行クッキーは「発行した日のうち」だけ有効とし、この時刻を過ぎたら失効させる。
    これにより 09:00 の切替で発行済み端末も全て接続不可になる（URLを変える意味を担保）。
    """
    di = _day_index(ts)
    next_shifted = (di + 1) * 86400                 # 翌日の論理日開始（shifted空間）
    return next_shifted + DAY_START_HOUR_JST * 3600 - _TZ_OFFSET_SEC


def remaining_valid_sec(issued_ts: float, now: float | None = None) -> int:
    """発行時刻 issued_ts のクッキーの残り有効秒（TTL と当日の終わりの早い方）。"""
    t = now if now is not None else time.time()
    hard = min(issued_ts + TTL_SEC, _day_expiry(issued_ts))
    return int(hard - t)


_SIGN_KEY = "pub_gate_secret"


def secret_for(store) -> str:
    """署名鍵の元になるシークレット文字列。

    優先順: 店舗 admin_token → 環境変数 ADMIN_TOKEN → 店舗DBに自動生成した
    永続シークレット（app_settings 'pub_gate_secret'）。
    従来は admin_token/環境変数が無いとゲート自体が無効（フェイルオープン）に
    なっていたが、それでは設定漏れ＝無制限公開になるため、DBに自動生成した
    シークレットを最終フォールバックとして必ずゲートを有効化する。
    """
    tok = getattr(store, "admin_token", "") if store is not None else ""
    if tok:
        return str(tok)
    env = os.environ.get("ADMIN_TOKEN", "") or ""
    if env:
        return env
    return _get_or_create_setting(db_path_for(store), _SIGN_KEY, 32)


def _get_or_create_setting(db_path: str, key: str, nbytes: int) -> str:
    """app_settings の key を読み、無ければランダム値を生成して保存して返す。"""
    if not db_path:
        return ""
    try:
        con = sqlite3.connect(db_path, timeout=5)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
            row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            if row and row[0]:
                return str(row[0])
            val = _secrets.token_urlsafe(nbytes)
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, val),
            )
            con.commit()
            return val
        finally:
            con.close()
    except Exception:
        return ""


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
    # 未来時刻のクッキー（時計異常・改竄）は不正扱い
    if ts > t + 300:
        return ("invalid", 0)
    # 有効期限 = TTL と「発行した日の終わり（次の09:00）」の早い方。
    # これにより 09:00 の切替で発行済み端末も全て失効する（＝毎日URLを変える意味を担保）。
    remain = remaining_valid_sec(ts, t)
    if remain <= 0:
        return ("expired", 0)
    return ("valid", remain)


# ---------------- PWA 引き継ぎトークン（handoff） ----------------
# iOS の PWA は Safari と独立した Cookie ストアを持つため、ブラウザで得た観覧許可を
# そのまま PWA に渡す手段が必要。観覧ページは有効なうちに /api/pub-handoff で
# 「発行時刻 ts を署名した引き継ぎトークン」を受け取り、manifest の start_url に
# 埋め込む。PWA 初回起動時に /enter?h=<token> でクッキーを発行するが、
# その発行時刻は元の ts をそのまま使う＝ブラウザ側と同じ期限で切れる。
# つまり PWA は「期限を引き継ぐ」だけで、決して新しい12時間を得られない。

def issue_handoff(secret: str, epoch: int, issued_ts: int) -> str:
    return f"{int(issued_ts)}.{_sign(secret, f'ho:{epoch}:{int(issued_ts)}')}"


def verify_handoff(secret: str, epoch: int, token: str | None,
                   now: float | None = None) -> int:
    """引き継ぎトークンを検証し、元の発行時刻 ts を返す（不正・期限切れは 0）。"""
    if not token or not secret:
        return 0
    try:
        ts_s, sig = token.split(".", 1)
        ts = int(ts_s)
    except Exception:
        return 0
    if not hmac.compare_digest(_sign(secret, f"ho:{epoch}:{ts}"), sig):
        return 0
    t = now if now is not None else time.time()
    # TTL・未来時刻に加え、発行日の終わり（次の09:00）を過ぎた引き継ぎも無効。
    if ts + TTL_SEC <= t or ts > t + 300 or _day_expiry(ts) <= t:
        return 0
    return ts


def cookie_issued_ts(value: str | None) -> int:
    """クッキー値から発行時刻を取り出す（検証はしない。表示・引き継ぎ用）。"""
    try:
        return int((value or "").split(".", 1)[0])
    except Exception:
        return 0


# ---------------- 失効画面のカメラ読み取り用トークン（scan） ----------------
# 「ページ内から /enter へ遷移する」経路は、旧版クライアントの自動再発行ループを
# 塞ぐために発行を拒否する（Sec-Fetch-Site: same-origin を見る）。
# 失効画面のカメラ読み取りだけは正規の再スキャンなので、サーバーが失効画面に
# 埋め込んだ短命トークン（10分窓・現在と1つ前）を付けて /enter?scan=<token> で通す。
SCAN_WINDOW_SEC = 10 * 60


def scan_token(secret: str, epoch: int, now: float | None = None) -> str:
    win = int((now if now is not None else time.time()) // SCAN_WINDOW_SEC)
    return _sign(secret, f"scan:{epoch}:{win}")


def verify_scan_token(secret: str, epoch: int, token: str | None,
                      now: float | None = None) -> bool:
    if not token or not secret:
        return False
    t = now if now is not None else time.time()
    win = int(t // SCAN_WINDOW_SEC)
    for w in (win, win - 1):
        if hmac.compare_digest(_sign(secret, f"scan:{epoch}:{w}"), token):
            return True
    return False


# ---------------- デイリー・ワード（毎日ローテーションする入口の秘密語） ----------------

def daily_word(secret: str, epoch: int = 0, dindex: int | None = None,
               now: float | None = None) -> str:
    """その日の入口URLに使う秘密語を決定的に生成する。

    形式: "<adj>-<noun>-<英数4桁>"（例: "hayate-booster-k7m2"）。
    secret / epoch / 日付のいずれかが変われば別語になる。強制失効（epoch+1）で
    その瞬間に当日の語も変わる。secret が無ければ空文字（＝ゲート無効）。
    """
    if not secret:
        return ""
    di = dindex if dindex is not None else _day_index(now)
    raw = hmac.new(_key(secret), f"word:{epoch}:{di}".encode("utf-8"),
                   hashlib.sha256).digest()
    adj = _WL_ADJ[raw[0] % len(_WL_ADJ)]
    noun = _WL_NOUN[raw[1] % len(_WL_NOUN)]
    # 残りのバイトから base32 風の短い英数字（紛らわしい文字を除外）を4桁。
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"  # l,o,0,1 を除外
    tail = "".join(alphabet[b % len(alphabet)] for b in raw[2:6])
    return f"{adj}-{noun}-{tail}"


def verify_daily_word(secret: str, epoch: int, word: str | None,
                      now: float | None = None) -> bool:
    """入口URLの秘密語を照合する（当日のみ。設定により前日も許容）。"""
    if not secret or not word:
        return False
    di = _day_index(now)
    candidates = [di]
    if WORD_ACCEPT_PREV_DAY:
        candidates.append(di - 1)
    for d in candidates:
        if hmac.compare_digest(daily_word(secret, epoch, d), word):
            return True
    return False


def _daily_word_salted(secret: str, epoch: int, salt: str, dindex: int | None = None,
                       now: float | None = None) -> str:
    """daily_word に salt を足した派生語。観覧語(view)と鍵語(key)を別々に生成するため。"""
    if not secret:
        return ""
    di = dindex if dindex is not None else _day_index(now)
    raw = hmac.new(_key(secret), f"word:{salt}:{epoch}:{di}".encode("utf-8"),
                   hashlib.sha256).digest()
    adj = _WL_ADJ[raw[0] % len(_WL_ADJ)]
    noun = _WL_NOUN[raw[1] % len(_WL_NOUN)]
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    tail = "".join(alphabet[b % len(alphabet)] for b in raw[2:6])
    return f"{adj}-{noun}-{tail}"


def view_word(secret: str, epoch: int, now: float | None = None) -> str:
    """観覧URLに出る語（/<観覧語>）。毎日・店舗ごとに変わる。"""
    return _daily_word_salted(secret, epoch, "view", now=now)


def key_word(secret: str, epoch: int, now: float | None = None) -> str:
    """入場URLの鍵語（/<観覧語>/enter/<鍵語>）。毎日・店舗ごとに変わる。"""
    return _daily_word_salted(secret, epoch, "key", now=now)


def verify_view_word(secret: str, epoch: int, w: str | None, now: float | None = None) -> bool:
    if not secret or not w:
        return False
    di = _day_index(now)
    cands = [di] + ([di - 1] if WORD_ACCEPT_PREV_DAY else [])
    for d in cands:
        if hmac.compare_digest(_daily_word_salted(secret, epoch, "view", d), w):
            return True
    return False


def verify_key_word(secret: str, epoch: int, w: str | None, now: float | None = None) -> bool:
    if not secret or not w:
        return False
    di = _day_index(now)
    cands = [di] + ([di - 1] if WORD_ACCEPT_PREV_DAY else [])
    for d in cands:
        if hmac.compare_digest(_daily_word_salted(secret, epoch, "key", d), w):
            return True
    return False


def entry_sub_path(secret: str, epoch: int, now: float | None = None) -> str:
    """2語方式の入場サブパス = /<観覧語>/enter/<鍵語>。

    読み取ると観覧URL /<観覧語> へ移る。secret が無い場合のみ "/enter"。
    """
    vw = view_word(secret, epoch, now=now)
    kw = key_word(secret, epoch, now=now)
    if not vw or not kw:
        return "/enter"
    return f"/{vw}/enter/{kw}"


def entry_url(base: str, pfx: str, store) -> str:
    """参加者入口の絶対URL（QR・共有用）。base 未設定なら空文字。

    base: 公開ベースURL（例 https://xxx）/ pfx: 店舗prefix（既定店舗は ""）。
    """
    if not base:
        return ""
    secret = secret_for(store)
    epoch = get_epoch(db_path_for(store))
    return f"{base}{pfx}{entry_sub_path(secret, epoch)}"
