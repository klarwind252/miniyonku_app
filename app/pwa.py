"""
ホーム画面アイコン（Webアプリ）対応 — クラウド版専用。

「PWAフル実装」ではなく、Web App Manifest と iOS 用メタタグだけを付与して
「ホーム画面に追加 → アプリ（スタンドアロン）モードで起動」を可能にする軽量版。
Service Worker・オフラインキャッシュ・プッシュ通知は持たない。

設定はベース店舗（スラッグなし）の設定画面から行い、app_settings に保存する
（スキーマ変更なし・キー追加のみ）。複数店舗化に備え、設定値は各店舗 DB の
app_settings に格納されるため店舗ごとに独立する。

【v5.6 変更】
  アイコン画像は「アプリ共通（app/static/pwa/）1枚」から「店舗ごと・画面ごと」へ。
  各店舗の公開ディレクトリ（public_dir）配下 pwa/ に、以下を書き出す:
    icon-src-512.png / icon-src-192.png         … 枠なし元画像（QR中央ロゴ・プレビュー用）
    icon-admin-512.png / -192 / apple-touch-icon-admin.png … ゴールドグラデ枠
    icon-view-512.png  / -192 / apple-touch-icon-view.png  … シルバーグラデ枠
    icon-html-512.png  / -192 / apple-touch-icon-html.png  … 枠なし（現状維持）
  配信は nginx 直接（{prefix}/pwa/icon-...png）。店舗1は prefix="" で /pwa/...。

3画面（admin / view / html）の対応:
  - admin（管理用） : /admin/manifest.webmanifest（動的・認証下）→ ゴールド枠アイコン
  - view（観覧用）   : /view/manifest.webmanifest（動的・認証下）→ シルバー枠アイコン
  - html（レーサー用）: {slug}/manifest.webmanifest（静的・nginx 直接配信）→ 枠なし

admin / view は「key 埋め込み起動」が有効なとき start_url に ?key=<token> を埋める。
iOS のスタンドアロン起動で Cookie が引き継がれない場合でも、起動時に再認証される。
"""
from __future__ import annotations

import os
import io
import sqlite3
import html as _html

# ---- 設定キーと既定値（app_settings の key = 下記） ----
DEFAULTS = {
    "pwa_enabled": "1",
    "pwa_name_admin": "管理用",
    "pwa_name_view": "観覧用",
    "pwa_name_html": "レーサー用",
    "pwa_theme_admin": "#2c3e50",
    "pwa_theme_view": "#0f1923",
    "pwa_theme_html": "#0f1923",
    "pwa_keylaunch_admin": "1",
    "pwa_keylaunch_view": "1",
    "pwa_icon_ver": "",   # 空 = アイコン未アップロード
    "app_icon_ver": "",   # 空 = アプリ用アイコン未アップロード
    "bg_enabled": "0",    # 1 = view/html 待機画面の背景を使う
    "bg_ver": "",         # 空 = 背景画像未アップロード
    "slideshow_enabled": "0",   # 1 = view/html 待機画面でスライドショーを流す
    "slideshow_ver": "",        # キャッシュバスター（更新のたびに変わる）
    "slideshow_count": "0",     # 登録済みスライド枚数（0〜10）
}
PWA_KEYS = list(DEFAULTS.keys())

# アイコン下のフル名称（name）。short_name は設定の pwa_name_* を使う。
_FULL_NAME = {
    "admin": "ミニ四駆レース 管理用",
    "view": "ミニ四駆レース 観覧用",
    "html": "ミニ四駆レース レーサー用",
}
_BG_COLOR = {"admin": "#ffffff", "view": "#0f1923", "html": "#0f1923"}

# ---- 枠（フレーム）色：画面別グラデーション（135deg / 左上→右下） ----
# CSS linear-gradient(135deg, ...) を Pillow で再現する。位置(0..1) と RGB。
GOLD_STOPS = [
    (0.00, (0xFF, 0xE6, 0x9C)),
    (0.40, (0xD4, 0xAF, 0x37)),
    (1.00, (0x8B, 0x65, 0x08)),
]
SILVER_STOPS = [
    (0.00, (0x99, 0x99, 0x99)),
    (0.50, (0xFF, 0xFF, 0xFF)),
    (1.00, (0xCC, 0xCC, 0xCC)),
]
# 画面 → 枠グラデ（html は枠なし＝None）
FRAME_STOPS = {
    "admin": GOLD_STOPS,
    "view": SILVER_STOPS,
    "html": None,
}

# 枠の太さ（画像辺に対する比率）・角丸（辺に対する比率）
FRAME_RATIO = 0.08
RADIUS_RATIO = 0.18


# ---------------------------------------------------------------------------
# 内部ヘルパ（店舗解決）
# ---------------------------------------------------------------------------
def _store_of(request):
    return getattr(getattr(request, "state", None), "store", None)


def _db_path_for(request) -> str:
    store = _store_of(request)
    if store is not None and getattr(store, "db_path", None):
        return store.db_path
    from app.models.database import DB_PATH
    return DB_PATH


def _public_dir_for(request) -> str:
    """現在店舗の公開ディレクトリ（nginx 直接配信のルート）。"""
    store = _store_of(request)
    if store is not None and getattr(store, "public_dir", None):
        return store.public_dir
    from app.config import PUBLIC_HTML_DIR
    return PUBLIC_HTML_DIR or ""


def icon_dir_for_public(public_dir: str) -> str:
    """公開ディレクトリ配下のアイコン保存先（{public_dir}/pwa）。"""
    return os.path.join(public_dir, "pwa") if public_dir else ""


def slug_prefix(request) -> str:
    """ブラウザURL上のスラッグ前置（ベース店舗は ""）。"""
    store = _store_of(request)
    if store is not None and getattr(store, "slug", ""):
        return "/" + store.slug
    return ""


def get_pwa_settings(request=None, db_path: str | None = None) -> dict:
    """app_settings から pwa_* を読む（同期 sqlite3・軽量）。未設定は既定値。"""
    if db_path is None and request is not None:
        db_path = _db_path_for(request)
    vals = dict(DEFAULTS)
    if not db_path or not os.path.exists(db_path):
        return vals
    try:
        con = sqlite3.connect(db_path)
        try:
            placeholders = ",".join("?" * len(PWA_KEYS))
            cur = con.execute(
                f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})",
                PWA_KEYS,
            )
            for k, v in cur.fetchall():
                if v is not None:
                    vals[k] = v
        finally:
            con.close()
    except Exception:
        pass
    return vals


# ---------------------------------------------------------------------------
# アイコン画像生成（グラデ枠合成）
# ---------------------------------------------------------------------------
def _lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def _color_at(stops, t: float):
    """位置 t(0..1) のグラデ色を線形補間で求める。"""
    if t <= stops[0][0]:
        return stops[0][1]
    if t >= stops[-1][0]:
        return stops[-1][1]
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if p0 <= t <= p1:
            local = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
            return (_lerp(c0[0], c1[0], local),
                    _lerp(c0[1], c1[1], local),
                    _lerp(c0[2], c1[2], local))
    return stops[-1][1]


def _make_gradient_square(size: int, stops):
    """135deg（左上→右下）グラデーションの正方形 RGBA 画像を生成する。"""
    from PIL import Image
    grad = Image.new("RGBA", (size, size))
    px = grad.load()
    # 135deg = 左上(0)→右下(1)。対角線上の射影 (x+y)/(2*(size-1)) を係数に使う。
    denom = 2 * (size - 1) if size > 1 else 1
    # 行ごとに同じ x+y 値が並ぶため、(x+y) でキャッシュ
    cache = {}
    for y in range(size):
        for x in range(size):
            s = x + y
            col = cache.get(s)
            if col is None:
                col = _color_at(stops, s / denom)
                cache[s] = col
            px[x, y] = (col[0], col[1], col[2], 255)
    return grad


def _rounded_mask(size: int, radius: int):
    """角丸正方形のアルファマスク（L）を返す。"""
    from PIL import Image, ImageDraw
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _compose_framed(src_square, size: int, stops):
    """枠なし正方形 src から、size×size の「グラデ枠付き角丸アイコン」を生成。

    stops が None の場合は枠なし（src をリサイズして角丸にするだけ）。
    """
    from PIL import Image
    src = src_square.resize((size, size), Image.LANCZOS).convert("RGBA")
    radius = max(1, int(round(size * RADIUS_RATIO)))

    if not stops:
        # 枠なし：角丸だけ適用
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(src, (0, 0))
        out.putalpha(_rounded_mask(size, radius))
        return out

    frame_w = max(1, int(round(size * FRAME_RATIO)))
    inner = size - frame_w * 2

    # 背景＝グラデ枠（角丸正方形）
    grad = _make_gradient_square(size, stops)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(grad, (0, 0))
    out.putalpha(_rounded_mask(size, radius))

    # 内側に元画像（枠の内側もわずかに角丸）
    if inner > 0:
        inner_img = src.resize((inner, inner), Image.LANCZOS)
        inner_radius = max(1, int(round(inner * RADIUS_RATIO)))
        inner_mask = _rounded_mask(inner, inner_radius)
        out.paste(inner_img, (frame_w, frame_w), inner_mask)
    return out


def generate_icons(src_bytes: bytes, public_dir: str) -> None:
    """アップロード画像から src＋admin/view/html の枠付き一式を public_dir/pwa へ書き出す。

    - src（枠なし）: icon-src-512.png / icon-src-192.png
    - 各画面: icon-{screen}-512.png / -192.png / apple-touch-icon-{screen}.png
    """
    from PIL import Image, ImageOps

    # 画素爆弾（decompression bomb）対策：極端に大きな画像はメモリ枯渇を招くため弾く。
    # 64MP ≒ 8000x8000 程度を上限とする。超過時 Image.open が DecompressionBombError。
    Image.MAX_IMAGE_PIXELS = 64_000_000

    out_dir = icon_dir_for_public(public_dir)
    if not out_dir:
        raise ValueError("public_dir が未設定です。")
    os.makedirs(out_dir, exist_ok=True)

    src = Image.open(io.BytesIO(src_bytes)).convert("RGBA")
    # 正方形にセンタークロップ（cover）
    side = min(src.size)
    src = ImageOps.fit(src, (side, side), method=Image.LANCZOS, centering=(0.5, 0.5))

    # 枠なし元画像（QR中央ロゴ・プレビュー用）。角丸を付けず素のまま保存。
    for sz in (512, 192):
        src.resize((sz, sz), Image.LANCZOS).save(
            os.path.join(out_dir, f"icon-src-{sz}.png"), "PNG"
        )

    # 画面別アイコン
    for screen in ("admin", "view", "html"):
        stops = FRAME_STOPS.get(screen)
        for sz in (512, 192):
            img = _compose_framed(src, sz, stops)
            img.save(os.path.join(out_dir, f"icon-{screen}-{sz}.png"), "PNG")
        # apple-touch-icon は透過非対応端末向けに白背景へ合成
        apple = _compose_framed(src, 180, stops)
        bg = Image.new("RGBA", (180, 180), (255, 255, 255, 255))
        bg.alpha_composite(apple)
        bg.convert("RGB").save(
            os.path.join(out_dir, f"apple-touch-icon-{screen}.png"), "PNG"
        )


# ---------------------------------------------------------------------------
# アイコン参照URL
# ---------------------------------------------------------------------------
def generate_app_icon(src_bytes: bytes, public_dir: str, max_edge: int = 38) -> None:
    """アプリ用アイコン（枠なし・単一画像）を長辺 max_edge px へ縮小して
    {public_dir}/pwa/icon-app-{max_edge}.png に保存する。
    アスペクト比は保持し、元画像が小さい場合は拡大しない（縮小のみ）。
    共通アイコンのレーサー用と同様、枠は付けない。"""
    from PIL import Image

    # 画素爆弾（decompression bomb）対策
    Image.MAX_IMAGE_PIXELS = 64_000_000

    out_dir = icon_dir_for_public(public_dir)
    if not out_dir:
        raise ValueError("public_dir が未設定です。")
    os.makedirs(out_dir, exist_ok=True)

    src = Image.open(io.BytesIO(src_bytes)).convert("RGBA")
    # 長辺を max_edge に縮小（アスペクト比保持・拡大はしない）
    src.thumbnail((max_edge, max_edge), Image.LANCZOS)
    src.save(os.path.join(out_dir, f"icon-app-{max_edge}.png"), "PNG")


def generate_background(src_bytes: bytes, public_dir: str, max_edge: int = 1200) -> None:
    """待機画面（view / html）の背景画像を長辺 max_edge px 以下へ縮小して
    {public_dir}/pwa/bg.png に保存する（単一画像・アスペクト比保持・縮小のみ）。"""
    from PIL import Image

    # 画素爆弾（decompression bomb）対策
    Image.MAX_IMAGE_PIXELS = 64_000_000

    out_dir = icon_dir_for_public(public_dir)
    if not out_dir:
        raise ValueError("public_dir が未設定です。")
    os.makedirs(out_dir, exist_ok=True)

    src = Image.open(io.BytesIO(src_bytes)).convert("RGB")
    # 長辺を max_edge に縮小（アスペクト比保持・拡大はしない）
    src.thumbnail((max_edge, max_edge), Image.LANCZOS)
    # PNG（nginx の /pwa/*.png 配信に合わせる）
    src.save(os.path.join(out_dir, "bg.png"), "PNG")


def icon_url(prefix: str, name: str, ver: str) -> str:
    """nginx 直接配信のアイコンURL。prefix は店舗接頭辞（店舗1は ""）。"""
    q = f"?v={ver}" if ver else ""
    return f"{prefix}/pwa/{name}{q}"


def src_icon_url(request, size: int = 512) -> str:
    """QR中央ロゴ・プレビューで使う『枠なし元アイコン』のURL。未設定なら空。"""
    settings = get_pwa_settings(request)
    ver = settings.get("pwa_icon_ver", "")
    if not ver:
        return ""
    return icon_url(slug_prefix(request), f"icon-src-{size}.png", ver)


def bg_url(request) -> str:
    """view / html の待機画面で使う背景画像URL。
    bg_enabled=1 かつ bg_ver がある時だけ URL を返す（それ以外は空）。"""
    settings = get_pwa_settings(request)
    if settings.get("bg_enabled") != "1":
        return ""
    ver = settings.get("bg_ver", "")
    if not ver:
        return ""
    return icon_url(slug_prefix(request), "bg.png", ver)


SLIDESHOW_MAX = 10


def generate_slideshow(images: list[bytes], public_dir: str,
                       max_edge: int = 1200, max_count: int = SLIDESHOW_MAX) -> int:
    """待機画面スライドショー用の画像（最大 max_count 枚）を長辺 max_edge px 以下へ
    縮小して {public_dir}/pwa/slide-01.png .. slide-NN.png に保存する。
    既存の余分なスライド（新しい枚数を超える分）は削除する。保存した枚数を返す。"""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 64_000_000

    out_dir = icon_dir_for_public(public_dir)
    if not out_dir:
        raise ValueError("public_dir が未設定です。")
    os.makedirs(out_dir, exist_ok=True)

    saved = 0
    for data in images:
        if saved >= max_count:
            break
        if not data:
            continue
        try:
            im = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            continue
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)  # 長辺max_edge・拡大なし
        saved += 1
        im.save(os.path.join(out_dir, f"slide-{saved:02d}.png"), "PNG")

    # 新しい枚数を超える古いスライドを削除
    for i in range(saved + 1, max_count + 1):
        p = os.path.join(out_dir, f"slide-{i:02d}.png")
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    return saved


def clear_slideshow(public_dir: str, max_count: int = SLIDESHOW_MAX) -> None:
    """スライドショー画像をすべて削除する。"""
    out_dir = icon_dir_for_public(public_dir)
    if not out_dir:
        return
    for i in range(1, max_count + 1):
        p = os.path.join(out_dir, f"slide-{i:02d}.png")
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def slideshow_urls(request, respect_enabled: bool = True) -> list[str]:
    """待機画面スライドショーの画像URL一覧。
    respect_enabled=True（standby用）のときは slideshow_enabled=1 のときだけ返す。
    False（設定プレビュー用）のときは有効/無効に関係なく登録枚数分を返す。"""
    settings = get_pwa_settings(request)
    if respect_enabled and settings.get("slideshow_enabled") != "1":
        return []
    try:
        count = int(settings.get("slideshow_count") or "0")
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return []
    count = min(count, SLIDESHOW_MAX)
    ver = settings.get("slideshow_ver", "")
    prefix = slug_prefix(request)
    return [icon_url(prefix, f"slide-{i:02d}.png", ver) for i in range(1, count + 1)]


def app_icon_src(request) -> str:
    """ヘッダー左上ロゴのURL。アプリ用アイコン（icon-app-38.png）があれば
    nginx 直配信URL（?v=バージョン付きでアップロード即反映）を返す。
    無ければ共通アイコン（icon-src-192.png）、それも無ければ従来ロゴ /logo。
    ※ /store-icon（FastAPIルート）に依存せず nginx 配信を直接参照するため確実に反映される。"""
    settings = get_pwa_settings(request)
    prefix = slug_prefix(request)
    av = settings.get("app_icon_ver", "")
    if av:
        return icon_url(prefix, "icon-app-38.png", av)
    iv = settings.get("pwa_icon_ver", "")
    if iv:
        return icon_url(prefix, "icon-src-192.png", iv)
    return "/logo"


# ---------------------------------------------------------------------------
# manifest 生成
# ---------------------------------------------------------------------------
def build_manifest_dict(screen: str, settings: dict, slug: str = "",
                        key: str | None = None) -> dict:
    """画面別の manifest 辞書を生成する。

    screen : "admin" | "view" | "html"
    slug   : 店舗スラッグ（ベース店舗は ""）
    key    : start_url に埋め込むトークン（admin/view の key 埋め込み起動時のみ）
    """
    short = settings.get(f"pwa_name_{screen}", DEFAULTS.get(f"pwa_name_{screen}", ""))
    theme = settings.get(f"pwa_theme_{screen}", DEFAULTS.get(f"pwa_theme_{screen}", "#2c3e50"))
    ver = settings.get("pwa_icon_ver", "")
    pfx = ("/" + slug) if slug else ""

    if screen == "admin":
        scope = start = f"{pfx}/admin/"
    elif screen == "view":
        scope = start = f"{pfx}/view/"
    else:  # html
        # scope は参加者トップ配下。start_url は /enter にする。
        # PWA(ホーム画面アイコン)の「初回」起動時のみ /enter が localStorage へ
        # 発行時刻を記録してから参加者トップへ遷移するため、独立storageコンテキストの
        # PWA でも「発行時刻なし＝期限切れ」にならず、追加直後は開ける。
        # ?src=pwa を付けて本物のQR経由の /enter と区別し、2回目以降の起動では
        # 発行時刻を上書きしない（＝アイコンを開くだけで24時間制限を無期限延長できて
        # しまう不具合を防ぐ。期限切れ後は物理QRの再スキャンが必須のまま）。
        scope = f"{pfx}/"
        start = f"{pfx}/enter?src=pwa"

    if key:
        start = f"{start}?key={key}"

    icons = []
    if ver:
        # アイコンは店舗別・画面別（{prefix}/pwa/icon-{screen}-...）。nginx 直接配信。
        icons = [
            {"src": icon_url(pfx, f"icon-{screen}-192.png", ver), "sizes": "192x192",
             "type": "image/png", "purpose": "any maskable"},
            {"src": icon_url(pfx, f"icon-{screen}-512.png", ver), "sizes": "512x512",
             "type": "image/png", "purpose": "any maskable"},
        ]

    return {
        "id": scope,
        "name": _FULL_NAME.get(screen, short),
        "short_name": short,
        "scope": scope,
        "start_url": start,
        "display": "standalone",
        "orientation": "portrait" if screen == "html" else "any",
        "background_color": _BG_COLOR.get(screen, "#ffffff"),
        "theme_color": theme,
        "lang": "ja",
        "dir": "ltr",
        "icons": icons,
    }


# ---------------------------------------------------------------------------
# <head> 注入用 HTML（admin / view のライブ画面はテンプレートからこれを呼ぶ）
# ---------------------------------------------------------------------------
def render_pwa_head(screen: str, request=None) -> str:
    """admin / view のテンプレート <head> に挿入する PWA メタ群を返す。

    Jinja グローバルとして登録され `{{ render_pwa_head('admin', request)|safe }}`
    のように呼ぶ。クラウド以外・無効時は空文字を返す。
    （html＝レーサー用は静的生成側で別途注入するため、ここでは admin / view のみ）
    """
    from app.config import IS_CLOUD
    if not IS_CLOUD or screen not in ("admin", "view"):
        return ""
    settings = get_pwa_settings(request)
    if settings.get("pwa_enabled") != "1":
        return ""

    pfx = slug_prefix(request)
    manifest_href = f"{pfx}/{screen}/manifest.webmanifest"
    short = _html.escape(settings.get(f"pwa_name_{screen}", ""))
    theme = _html.escape(settings.get(f"pwa_theme_{screen}", "#2c3e50"))
    ver = settings.get("pwa_icon_ver", "")

    lines = [
        # 同一オリジンでも manifest 取得時に Cookie を送らせるため use-credentials を付ける
        # （認証下の動的 manifest を 200 で取得し、key 埋め込み start_url を成立させる）
        f'<link rel="manifest" href="{manifest_href}" crossorigin="use-credentials">',
        f'<meta name="theme-color" content="{theme}">',
        '<meta name="mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">',
        f'<meta name="apple-mobile-web-app-title" content="{short}">',
    ]
    if ver:
        lines.append(
            f'<link rel="apple-touch-icon" href="{icon_url(pfx, f"apple-touch-icon-{screen}.png", ver)}">'
        )
    return "\n".join(lines)


def render_pwa_head_html(settings: dict, slug: str = "") -> str:
    """html（レーサー用・静的）の <head> に注入するメタ群を返す。

    静的生成（public_html._patch_html_for_static）から呼ぶ。manifest は静的書き出し
    （nginx 直接配信）なので crossorigin は付けない（認証なし・Cookie 不要）。
    html のアイコンは枠なし（現状維持）。
    """
    if settings.get("pwa_enabled") != "1":
        return ""
    pfx = ("/" + slug) if slug else ""
    manifest_href = f"{pfx}/manifest.webmanifest"
    short = _html.escape(settings.get("pwa_name_html", "レーサー用"))
    theme = _html.escape(settings.get("pwa_theme_html", "#0f1923"))
    ver = settings.get("pwa_icon_ver", "")

    lines = [
        f'<link rel="manifest" href="{manifest_href}">',
        f'<meta name="theme-color" content="{theme}">',
        '<meta name="mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">',
        f'<meta name="apple-mobile-web-app-title" content="{short}">',
    ]
    if ver:
        lines.append(
            f'<link rel="apple-touch-icon" href="{icon_url(pfx, "apple-touch-icon-html.png", ver)}">'
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# html（レーサー用）静的 manifest の書き出し
# ---------------------------------------------------------------------------
def write_static_html_manifest(out_dir: str, settings: dict, slug: str = "") -> bool:
    """out_dir/manifest.webmanifest を原子的に書き出す（nginx 直接配信用）。

    out_dir は店舗の公開ディレクトリ（ベース店舗は PUBLIC_HTML_DIR）。
    """
    import json
    if not out_dir:
        return False
    if settings.get("pwa_enabled") != "1":
        return False
    try:
        os.makedirs(out_dir, exist_ok=True)
        data = build_manifest_dict("html", settings, slug=slug, key=None)
        tmp = os.path.join(out_dir, ".manifest.webmanifest.tmp")
        dst = os.path.join(out_dir, "manifest.webmanifest")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dst)
        return True
    except Exception as e:
        print(f"[pwa] static manifest write error: {e}", flush=True)
        return False
