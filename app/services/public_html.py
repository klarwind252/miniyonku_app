"""
参加者向け静的HTML生成・GCSアップロードサービス

- export_current_html(db) : 現在のhost_stateに対応するページを生成してGCSへアップロード
- _get_settings(db)       : app_settings からクラウド設定を取得
- _render_page(url, db)   : viewer の各ルーター関数を直接呼び出してHTMLを生成
- _upload_to_gcs(html, bucket) : GCSへアップロード
"""

from __future__ import annotations
import logging
import re

logger = logging.getLogger(__name__)


async def _get_settings(db) -> dict:
    """app_settings からクラウド設定を取得"""
    result = {}
    keys = ["public_html_enabled", "public_html_gcs_bucket", "public_html_gcp_project"]
    for key in keys:
        async with db.execute(
            "SELECT value FROM app_settings WHERE key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
        result[key] = row["value"] if row else ""
    return result


_logo_cache: dict = {}   # path -> (mtime, data_uri)


def _get_logo_base64() -> str | None:
    """ロゴ画像をBase64エンコードして返す。

    ロゴは起動中ほぼ不変なので、ファイルの更新時刻(mtime)をキーにキャッシュする。
    差し替え時は mtime が変わるため自動的に再読み込みされる。
    （①のデバウンスで書き出し頻度自体は下がったが、読み込みコストはゼロにできる）
    """
    import base64, os
    base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    for name in ("logo_header.jpg", "logo_header.png"):
        path = os.path.join(base, "app", "static", name)
        if not os.path.exists(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        hit = _logo_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        with open(path, "rb") as f:
            uri = f"data:{mime};base64,{base64.b64encode(f.read()).decode()}"
        _logo_cache[path] = (mtime, uri)
        return uri
    return None


def _patch_html_for_static(html: str, slug: str = "") -> str:
    """
    静的配信用: 全JSを除去し、bracket線描画JS＋有効期限ゲートJSを再注入する。

    有効期限（案①・クライアント側ソフト方式 / 24時間）:
      参加者は QR（/enter）経由でアクセスし、/enter が localStorage に発行時刻を記録する。
      本ページは発行時刻から24時間以内なら30秒ごとに「更新の有無」を確認し、変化があった
      ときだけ反映する（ETag条件付きGETで304が返れば何もしない）。超過すると自動更新を止めて
      「QRを読み直してください」のオーバーレイを表示する。単純な再読込では復帰せず、QR
      （/enter）を読み直すと新たな24時間が始まる。判定は全てブラウザ側で完結するため
      サーバー負荷は増えない（ページ配信は従来どおり nginx の静的配信）。
    """
    import re as _re

    # admin と同一のブラケット・レイアウトJS（window._bracketDrawConnectors を定義する
    # 自己実行関数ブロック）を、全script除去の前に抽出して保持する。
    # これにより参加者向け静的HTMLでも PC版と同一のツリー配置・コネクタ線描画が動作する。
    bracket_layout_js = ""
    _blm = _re.search(
        r'<script>\s*\(function\(\)\s*\{.*?window\._bracketDrawConnectors.*?\}\)\(\);\s*</script>',
        html, flags=_re.DOTALL
    )
    if _blm:
        bracket_layout_js = _blm.group(0)

    # 全<script>タグを除去
    patched = _re.sub(r'<script[^>]*>.*?</script>', '', html, flags=_re.DOTALL)

    # view 画面由来の PWA タグを除去する。参加者向け静的HTMLは view画面のHTMLを
    # 流用しているため、そのままだと <link rel="manifest" href="/{slug}/view/manifest.webmanifest">
    # が残る。iOS で「ホーム画面に追加」すると、この view 用 manifest の start_url(=/{slug}/view/)
    # がアイコンに登録され、アイコンから起動すると view 画面が開いてしまう。
    # ここで view 用を落とし、この後 render_pwa_head_html が注入するレーサー用 manifest
    # (/{slug}/manifest.webmanifest, start_url=/{slug}/enter) だけを残す。
    patched = _re.sub(r'<link[^>]*rel=["\']manifest["\'][^>]*>', '', patched, flags=_re.IGNORECASE)
    patched = _re.sub(r'<meta[^>]*name=["\']theme-color["\'][^>]*>', '', patched, flags=_re.IGNORECASE)
    patched = _re.sub(r'<meta[^>]*name=["\'](?:mobile-web-app-capable|apple-mobile-web-app-capable|apple-mobile-web-app-status-bar-style|apple-mobile-web-app-title)["\'][^>]*>', '', patched, flags=_re.IGNORECASE)
    patched = _re.sub(r'<link[^>]*rel=["\']apple-touch-icon["\'][^>]*>', '', patched, flags=_re.IGNORECASE)

    # ナビボタン非表示を追加（自動更新は下部の expiry スクリプトがJSで制御するため
    # 従来の <meta refresh> は使わない）
    inject = """<style>
/* ナビボタン非表示 */
.v-nav .v-nav-btn{display:none}
#sync-btn{display:none}

/* 最大幅・中央寄せ */
html{overflow-x:hidden}body{padding-top:48px}.v-container{max-width:480px;margin:0 auto!important}

/* info-grid: 左=項目名・右=値 の2列行レイアウト */
.info-grid{display:block!important;border:1px solid #2c3e50;border-radius:6px;overflow:hidden}
.info-cell{display:flex!important;align-items:baseline;padding:6px 12px;border-bottom:1px solid #2c3e50}
.info-cell:last-child{border-bottom:none}
.info-label{color:#7f8c8d;font-size:12px;flex:0 0 7em;white-space:nowrap;font-weight:normal}
.info-value{font-size:12px;font-weight:normal;color:#ecf0f1;flex:1}
.info-value.large{font-size:12px;font-weight:bold}

/* entry-grid: 1列 */
.entry-grid{grid-template-columns:1fr!important}
/* bracket: 横スクロールをスマホでも確実に・スムーズに */
.bracket-wrap{overflow-x:auto!important;-webkit-overflow-scrolling:touch}
/* entry-card: 横並び・幅いっぱい */
.entry-card{display:flex!important;flex-direction:row!important;align-items:center;gap:8px;width:100%;padding:8px 12px;box-sizing:border-box}
/* レーサー名: 14px bold・左揃え・省略なし */
.entry-name{font-size:14px!important;font-weight:bold!important;white-space:nowrap;overflow:visible!important;text-overflow:clip!important;flex:1;text-align:left}
/* よみがな: 10px normal */
.entry-yomi{font-size:10px!important;font-weight:normal!important;color:#7f8c8d;white-space:nowrap;flex-shrink:0;margin-top:0!important}
/* 順位バッジ（entry-card 内の先頭span）: 改行・縮小させない */
.entry-card > span:first-child{flex-shrink:0!important;white-space:nowrap!important}
</style>
"""
    patched = patched.replace('</head>', inject + '</head>', 1)

    # no-bracketを常に表示（JSが除去されているため）
    patched = patched.replace(
        '<div id="no-bracket" style="display:none;',
        '<div id="no-bracket" style="display:block;',
    )

    # /logo をBase64埋め込みに変換
    logo_b64 = _get_logo_base64()
    if logo_b64:
        patched = patched.replace('src="/logo"', f'src="{logo_b64}"')

    # 有効期限ゲート（24時間）＋自動更新（更新検知時のみ反映）スクリプト
    _slug_key = (slug or "default")
    _enter_url = (f"/{slug}/enter" if slug else "/enter")
    expiry_script = """<script>
(function(){
  var KEY = "m4_pub_issued___SLUGKEY__";
  var TTL = 24*60*60*1000;          // 24時間
  var CHECK_MS = 30000;             // 30秒ごとに「更新の有無」だけ確認する
  var ENTER = "__ENTERURL__";

  function issued(){ try { return parseInt(localStorage.getItem(KEY)||"0",10)||0; } catch(e){ return 0; } }
  function expired(){ var t=issued(); return (!t) || (Date.now()-t > TTL); }
  function showOverlay(){
    if(document.getElementById("m4-expired")) return;
    var ov=document.createElement("div");
    ov.id="m4-expired";
    ov.style.cssText="position:fixed;inset:0;z-index:99999;background:rgba(20,24,33,.96);color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px;font-family:sans-serif;";
    ov.innerHTML='<div style="font-size:22px;font-weight:bold;margin-bottom:14px">観覧の有効期限が切れました</div>'
      +'<div style="font-size:15px;line-height:1.7;margin-bottom:22px;opacity:.9">お手元のQRコードを<br>もう一度スキャンしてください。</div>'
      +'<div style="display:inline-block;background:#2c3e50;color:#cfd8e3;padding:12px 22px;border-radius:8px;font-size:15px;font-weight:bold;line-height:1.6">QRコードを再スキャンすると<br>最新の観覧画面を表示できます</div>';
    document.body.appendChild(ov);
  }

  if(expired()){ showOverlay(); return; }

  // ---- 操作中フラグ（操作中はリロードを保留してチカチカ・割り込みを防ぐ） ----
  var lastInteract = 0;
  function touch(){ lastInteract = Date.now(); }
  ['touchstart','touchmove','pointerdown','scroll','keydown','wheel'].forEach(function(ev){
    window.addEventListener(ev, touch, {passive:true});
  });
  function isBusy(){
    // 直近1.2秒以内に操作があれば「操作中」とみなす
    if(Date.now() - lastInteract < 1200) return true;
    // プルダウン（マイレーサー選択）が開いている/フォーカス中なら保留
    var sel = document.getElementById('m4-racer-select');
    if(sel && document.activeElement === sel) return true;
    // テキスト入力等にフォーカス中なら保留
    var ae = document.activeElement;
    if(ae && (ae.tagName === 'SELECT' || ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA')) return true;
    return false;
  }

  // ---- 更新検知（ETag優先・無ければ本文ハッシュ） ----
  var lastTag = null;     // 直近に確認したETag
  var lastHash = null;    // ETagが無い環境用の本文ハッシュ
  var pendingReload = false;

  function hashStr(s){
    // 軽量な文字列ハッシュ（FNV-1a 32bit）。暗号用途ではなく変化検出のみ。
    var h = 0x811c9dc5;
    for(var i=0;i<s.length;i++){ h ^= s.charCodeAt(i); h = (h + ((h<<1)+(h<<4)+(h<<7)+(h<<8)+(h<<24))) >>> 0; }
    return h >>> 0;
  }

  var pendingHtml = null;
  var lastStruct = null;  // トーナメント構造の世代印（生成・再生成・削除で変化）

  // 取得HTMLから「トーナメント表の骨格」だけを抜き出して世代印を作る。
  // 中身（勝者名など）ではなく“枠組み”の変化を捉えたいので、
  //  - トーナメント表要素（.br-round）の個数
  //  - 各ラウンド・各グループの「スロット数」の並び
  // をつないだ文字列とする。結果入力だけでは変わらず、
  // 生成・再生成・削除でのみ変わる（=フルリロードすべき瞬間）。
  function structSig(root){
    try {
      var rounds = root.querySelectorAll('.br-round');
      if(!rounds || rounds.length === 0){
        return 'no-bracket';   // トーナメント表がまだ無い状態も1世代として区別
      }
      var parts = [];
      rounds.forEach(function(r){
        var g = [];
        r.querySelectorAll('.br-group').forEach(function(grp){
          g.push(grp.querySelectorAll('.br-slot').length);
        });
        parts.push(g.join('-'));
      });
      return parts.join('|');
    } catch(e){
      return 'err';
    }
  }

  // 取得済みHTMLから .v-container の中身だけを差し替える（フルリロードしない）。
  // 予選（全形式）・決勝のいずれも .v-container 配下にレンダリングされるため一律に効く。
  // 差し替え後にブラケット線の再描画とマイレーサーの再適用を行う。
  // 想定外（構造が取れない/解析失敗）のときは安全側で location.reload() にフォールバック。
  function applyPartial(newHtml){
    try {
      var doc = new DOMParser().parseFromString(newHtml, 'text/html');
      var fresh = doc.querySelector('.v-container');
      var live  = document.querySelector('.v-container');
      if(!fresh || !live){ location.reload(); return; }
      // マイレーサー選択・スクロール位置を保持
      var selOld = document.getElementById('m4-racer-select');
      var selVal = selOld ? selOld.value : null;
      var sx = window.scrollX, sy = window.scrollY;
      // 画面別CSSの同期：待機画面と各レース画面（予選/決勝/総当たり）は
      // それぞれ異なる <style>（.schedule-table 等）を <head> に持つ。
      // 部分更新は .v-container（body側）だけを差し替えるため、待機画面→レース画面へ
      // 自動更新した際に、レース画面用CSSが <head> に無く表組みが崩れる。
      // そこで新HTMLの <head> 内 <style> 群を現在の <head> に反映してから本文を差し替える。
      try {
        var liveHead = document.head;
        var freshStyles = doc.querySelectorAll('head style');
        var liveSig = Array.prototype.map.call(
          liveHead.querySelectorAll('style'), function(s){ return s.textContent; }
        ).join('\u0001');
        var freshSig = Array.prototype.map.call(
          freshStyles, function(s){ return s.textContent; }
        ).join('\u0001');
        if(freshStyles.length && freshSig !== liveSig){
          // 既存の <style> を除去し、新しい画面の <style> 群へ入れ替える
          Array.prototype.forEach.call(
            liveHead.querySelectorAll('style'), function(s){ s.remove(); }
          );
          Array.prototype.forEach.call(freshStyles, function(s){
            liveHead.appendChild(document.importNode(s, true));
          });
        }
      } catch(e){ /* CSS同期失敗時も本文差し替えは継続（従来挙動） */ }
      // 更新箇所だけ差し替え
      live.innerHTML = fresh.innerHTML;
      // ブラケット線を再描画（決勝・ヒート予選の全 .bracket-outer が対象）
      if(typeof window._bracketDrawConnectors === 'function'){
        try { window._bracketDrawConnectors(); } catch(e){}
      }
      // 線描画後にマイレーサーを再適用＋選択・スクロール復元
      setTimeout(function(){
        if(typeof window._m4Reapply === 'function'){
          try { window._m4Reapply(); } catch(e){}
        }
        var sel2 = document.getElementById('m4-racer-select');
        if(sel2 && selVal != null){ try { sel2.value = selVal; } catch(e){} }
        try { window.scrollTo(sx, sy); } catch(e){}
      }, 120);
    } catch(e){
      location.reload();   // 想定外は全リロードで確実に最新化
    }
  }

  function applyWhenIdle(newHtml){
    pendingReload = true;
    pendingHtml = newHtml;
    (function wait(){
      if(expired()){ showOverlay(); return; }
      if(!isBusy()){
        var h = pendingHtml; pendingHtml = null; pendingReload = false;
        applyPartial(h);
        return;
      }
      setTimeout(wait, 600);   // 操作が落ち着くまで待ってから反映
    })();
  }

  function check(){
    if(window.__m4ModalOpen){ return; }   // レイアウト/タイスケ/備考の観覧中は自動更新を止める
    if(expired()){ showOverlay(); return; }
    if(pendingReload) return;  // 反映待ち中は多重チェックしない

    var headers = {};
    if(lastTag) headers['If-None-Match'] = lastTag;

    fetch(location.pathname + location.search, {
      method: 'GET',
      cache: 'no-store',
      headers: headers
    }).then(function(res){
      if(res.status === 304){
        // 変更なし（ETagが一致）。ボディも返らないため最も軽い。
        return null;
      }
      // 200が返ったらETagの値は信用せず、必ず本文ハッシュで実体の変化を確認する。
      // （ETagが内容更新を正しく反映しない配信構成があるため、中身で判定するのが確実）
      var tag = res.headers.get('ETag');
      return res.text().then(function(txt){
        var h = hashStr(txt);
        if(lastHash === null){
          // 初回：基準値として記録（ETagも保存して次回は304を狙う）
          lastHash = h;
          lastTag = tag;
          lastStruct = structSig(document);  // 現在表示中の構造世代を基準化
          return;
        }
        if(h !== lastHash){
          lastHash = h;
          lastTag = tag;

          // 取得HTMLの「トーナメント構造の世代印」を算出
          var freshDoc = new DOMParser().parseFromString(txt, 'text/html');
          var newStruct = structSig(freshDoc);
          if(lastStruct === null){ lastStruct = structSig(document); }

          // トーナメントの生成・再生成・削除（骨組みの世代が変化）を検出したら、
          // 部分更新では枠組みが食い違って崩れるため、フルリロードで確実に最新化する。
          // （no-bracket ⇔ bracket の遷移も構造変化として拾う）
          if(newStruct !== lastStruct){
            lastStruct = newStruct;
            if(expired()){ showOverlay(); return; }  // 追従停止中はリロードせず従来どおり
            location.reload();
            return;
          }

          // 構造は同じ（＝勝者などの中身だけ変化）→ 従来どおり軽い部分更新
          applyWhenIdle(txt);
        } else {
          // 中身は同じ。ETagだけ更新しておき次回の304判定に使う
          lastTag = tag;
        }
      });
    }).catch(function(){ /* 一時的な通信失敗は無視して次回再試行 */ });
  }

  // 初回に基準値（ETag/ハッシュ）を取得してから、定期チェックを開始する
  check();
  setInterval(check, CHECK_MS);
})();
</script>""".replace("__SLUGKEY__", _slug_key).replace("__ENTERURL__", _enter_url)

    # bracket レイアウト＋線描画JSを再注入（admin と同一ロジックを保持したものを使用）。
    # connector_js 内で window._bracketDrawConnectors と init() が定義され、ページ内の
    # 全 .bracket-outer（決勝・予選ヒート）のグループ絶対配置とコネクタ線描画を行う。
    if bracket_layout_js:
        redraw_script = bracket_layout_js + """<script>
window.addEventListener('load', function(){
  setTimeout(function(){
    if (typeof window._bracketDrawConnectors === 'function') { window._bracketDrawConnectors(); }
  }, 250);
});
</script>"""
    else:
        redraw_script = ""
    my_racer_script = """<script>
/* ===== マイレーサー フォーカス機能 ===== */
(function(){
  var SLUG_KEY = '__SLUGKEY__';
  var LS_KEY = 'm4_my_racer_' + SLUG_KEY;

  /* ---------- localStorage ---------- */
  function getMyRacer(){ try{ var v=localStorage.getItem(LS_KEY); return v?JSON.parse(v):null; }catch(e){ return null; } }
  function setMyRacer(obj){ try{ localStorage.setItem(LS_KEY, JSON.stringify(obj)); }catch(e){} }
  function clearMyRacer(){ try{ localStorage.removeItem(LS_KEY); }catch(e){} }

  /* ---------- エントリー名一覧を収集 ---------- */
  function collectNames(){
    var names = [];
    /* エントリー一覧（決勝トーナメント等） */
    document.querySelectorAll('.entry-card').forEach(function(card){
      var n = card.querySelector('.entry-name');
      if(n){ var t = n.textContent.trim(); if(t && names.indexOf(t)<0) names.push(t); }
    });
    /* .entry-card に属さない .entry-name（ヒート別決勝進出一覧など）も収集 */
    document.querySelectorAll('.entry-name').forEach(function(el){
      var t = el.textContent.trim();
      if(t && names.indexOf(t)<0) names.push(t);
    });
    /* 総当たり等のレーススケジュール表の名前（.rr-name） */
    document.querySelectorAll('.rr-name').forEach(function(el){
      var t = el.textContent.trim();
      if(t && names.indexOf(t)<0) names.push(t);
    });
    /* 決勝トーナメントのスロット名（.br-slot-name） */
    document.querySelectorAll('.br-slot-name').forEach(function(el){
      var t = el.textContent.trim();
      if(t && names.indexOf(t)<0) names.push(t);
    });
    return names;
  }

  /* ---------- バナー（プルダウン付き） ---------- */
  function renderBanner(name){
    /* 待機画面（「○○のミニ四駆レースへようこそ！／お待ちください」）では
       レーサー選択バーを表示しない。既に出ている場合は除去する。 */
    if(document.querySelector('.v-waiting')){
      var ex = document.getElementById('m4-my-banner');
      if(ex){ ex.remove(); }
      var pb = document.getElementById('m4-position-banner');
      if(pb){ pb.remove(); }
      document.body.style.paddingTop = '';
      return;
    }
    var b = document.getElementById('m4-my-banner');
    var _slot = document.getElementById('m4-picker-slot');
    if(!b){
      b = document.createElement('div');
      b.id = 'm4-my-banner';
      if(_slot){
        /* ヘッダー（v-nav）右側のスロットに配置（最上部固定はしない） */
        b.style.cssText = 'display:flex;align-items:center;gap:6px;flex:1;min-width:0;'
          + 'background:#1a6e3c;color:#fff;border-radius:6px;'
          + 'padding:3px 6px;font-size:12px;font-weight:bold;box-sizing:border-box;';
        _slot.appendChild(b);
      } else {
        b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9000;background:#1a6e3c;color:#fff;'
          + 'display:flex;align-items:center;gap:8px;'
          + 'padding:6px 10px;font-size:13px;font-weight:bold;box-shadow:0 2px 8px rgba(0,0,0,.4);'
          + 'max-width:480px;margin:0 auto;box-sizing:border-box;';
        document.body.appendChild(b);
        document.body.style.paddingTop = '44px';
      }
    }

    /* プルダウン生成 */
    var names = collectNames();
    var opts = '<option value="">👤 レーサーを選択</option>';
    names.forEach(function(n){
      opts += '<option value="' + n + '"' + (n === name ? ' selected' : '') + '>' + n + '</option>';
    });
    var selStyle = 'flex:1;min-width:0;background:#0f4d2a;color:#fff;border:1px solid rgba(255,255,255,.3);'
      + 'border-radius:4px;padding:4px 6px;font-size:13px;font-weight:bold;cursor:pointer;';
    var btnStyle = 'background:rgba(255,255,255,.2);border:none;color:#fff;'
      + 'border-radius:4px;padding:4px 10px;font-size:12px;cursor:pointer;white-space:nowrap;';
    b.innerHTML = '<select id="m4-racer-select" style="' + selStyle + '">' + opts + '</select>'
      + '<button onclick="m4ClearMyRacer()" style="' + btnStyle + '">✕</button>';

    /* selectのchangeイベント */
    var sel = document.getElementById('m4-racer-select');
    sel.addEventListener('change', function(){
      var val = sel.value;
      if(!val){ m4ClearMyRacer(); return; }
      var card = Array.from(document.querySelectorAll('.entry-card')).find(function(c){
        var n = c.querySelector('.entry-name');
        return n && n.textContent.trim() === val;
      });
      var yomi = '';
      if(card){ var y = card.querySelector('.entry-yomi'); yomi = y ? y.textContent.trim() : ''; }
      setMyRacer({ name: val, yomi: yomi });
      applyAll();
    });
  }

  /* ---------- ハイライト（エントリーカード） ---------- */
  function applyEntryHighlight(name){
    var cards = document.querySelectorAll('.entry-card');
    if(!name || cards.length === 0) return;
    cards.forEach(function(card){
      var n = card.querySelector('.entry-name');
      if(!n) return;
      var match = n.textContent.trim() === name;
      /* 他はグレーアウトせず元のまま。対象だけオレンジで強調する。 */
      if(match){
        /* 背景 rgb(230,126,34)・枠はより濃いオレンジ・文字は白。子要素が背景色を
           持つ場合に隠れないよう important を付け、名前要素にも背景・文字色を当てる。 */
        card.style.setProperty('background', 'rgb(230,126,34)', 'important');
        card.style.setProperty('border', '2px solid rgb(160,80,15)', 'important');
        card.style.setProperty('color', '#ffffff', 'important');
        card.style.boxShadow = '0 0 0 2px rgba(230,126,34,.6)';
        card.style.opacity = '1';
        n.style.setProperty('background', 'transparent', 'important');
        n.style.setProperty('color', '#ffffff', 'important');
        n.style.fontWeight = 'bold';
      } else {
        card.style.removeProperty('border');
        card.style.removeProperty('background');
        card.style.removeProperty('color');
        card.style.boxShadow = '';
        card.style.opacity = '';
        n.style.removeProperty('background');
        n.style.removeProperty('color');
        n.style.fontWeight = '';
      }
    });
  }

  /* ---------- 出走順バナー ---------- */
  function renderPositionBanner(name){
    var old = document.getElementById('m4-position-banner');
    if(old) old.remove();
    /* 出走順バナーを消した時点で、本文の余白をヘッダー（マイレーサー選択バー）の
       高さぶんに戻す。バナーを再表示する場合は末尾で改めて上書きする。 */
    var _slotR = document.getElementById('m4-picker-slot');
    var _navR = document.querySelector('.v-nav');
    var _myBar = document.getElementById('m4-my-banner');
    document.body.style.paddingTop = _slotR
      ? ((_navR ? Math.round(_navR.getBoundingClientRect().height) : 44) + 'px')
      : ((_myBar ? Math.round(_myBar.getBoundingClientRect().height) : 44) + 'px');
    /* 別レーサーへ切替時に前回の強調が残らないよう、まず全リセット
       （important で当てた背景・文字色等は removeProperty で確実に消す） */
    document.querySelectorAll('.br-slot-name').forEach(function(el){
      el.style.removeProperty('background'); el.style.removeProperty('color');
      el.style.fontWeight = '';
    });
    document.querySelectorAll('.br-slot').forEach(function(s){
      s.style.removeProperty('background'); s.style.removeProperty('box-shadow');
    });
    document.querySelectorAll('.racer-cell').forEach(function(c){
      c.style.removeProperty('background'); c.style.removeProperty('color');
      c.style.removeProperty('box-shadow'); c.style.fontWeight = '';
    });
    if(!name) return;

    /* 決勝 br-slot-name */
    var msgs = [];
    var bannerBg = '#154360';   /* バナー背景。あと0組=赤・あと1組=橙・それ以外=濃紺 */

    /* ── ヒートトーナメント予選：ヒート（とグループ）ごとに位置を出す ──
       本戦と違い、準決勝・決勝も「数える」（各ヒートは独立した小トーナメントのため）。 */
    var _htConts = document.querySelectorAll('[id^="viewer-ht-bracket-"]');
    var isHeatQual = _htConts.length > 0;
    if(isHeatQual){
      _htConts.forEach(function(cont){
        var idp = cont.id.split('-');           /* viewer-ht-bracket-{hno}-{sec} */
        var hno = idp[3];
        var rs = cont.querySelectorAll('.br-round');
        if(!rs.length) return;
        var mR = -1, mG = -1;
        rs.forEach(function(rnd, ri){
          rnd.querySelectorAll('.br-group').forEach(function(grp, gi){
            grp.querySelectorAll('.br-slot-name').forEach(function(el){
              if(el.textContent.trim() === name){ mR = ri; mG = gi; }
            });
          });
        });
        if(mR < 0) return;
        var mGrp = rs[mR].querySelectorAll('.br-group')[mG];
        if(mGrp && mGrp.classList.contains('has-winner')){
          /* 自分の最終地点グループが決着済み＝このヒートでの出番は終了
             （勝って次が無い／負けた のいずれも）。勝敗を問わず出走順は出さない。 */
          return;
        }
        var lb = rs[mR].querySelector('.br-round-label');
        var roundName = lb ? lb.textContent.trim() : ('R'+(mR+1));
        var before = 0;
        rs.forEach(function(rnd, ri){
          rnd.querySelectorAll('.br-group').forEach(function(grp, gi){
            if(ri > mR) return;
            if(ri === mR && gi >= mG) return;
            if(grp.classList.contains('has-winner')) return;
            before++;
          });
        });
        var head = 'ヒート' + hno + ' ' + roundName + ' 第' + (mG+1) + 'グループ';
        if(before === 0){ msgs.push(head + ' あなたの番です'); bannerBg = 'rgb(200,40,40)'; }
        else if(before === 1){ msgs.push(head + ' あなたは次です'); bannerBg = 'rgb(210,140,30)'; }
        else { msgs.push(head + '（あと' + before + '組）'); bannerBg = '#154360'; }
        rs.forEach(function(rnd){
          rnd.querySelectorAll('.br-slot-name').forEach(function(el){
            if(el.textContent.trim() === name){
              el.style.setProperty('background', 'rgb(230,126,34)', 'important');
              el.style.setProperty('color', '#ffffff', 'important');
              el.style.fontWeight = 'bold';
              var slot = el.closest('.br-slot') || el.parentElement;
              if(slot){
                slot.style.setProperty('background', 'rgb(230,126,34)', 'important');
                slot.style.setProperty('box-shadow', 'inset 0 0 0 2px rgb(160,80,15)', 'important');
              }
            }
          });
        });
      });
    }

    /* 決勝トーナメント（本戦）。ヒート予選画面では数えない。 */
    var rounds = document.querySelectorAll('.br-round');
    if(!isHeatQual && rounds.length > 0){
      var myRound = -1, myGroup = -1;
      rounds.forEach(function(rnd, ri){
        rnd.querySelectorAll('.br-group').forEach(function(grp, gi){
          grp.querySelectorAll('.br-slot-name').forEach(function(el){
            if(el.textContent.trim() === name){ myRound = ri; myGroup = gi; }
          });
        });
      });
      if(myRound >= 0){
        /* 敗退判定：自分の現在地グループ（名前が入っている最後尾グループ）が勝者確定済みで、
           その勝者が自分でない場合は、自分は負けて勝ち残っていない。
           この場合は出走順バナーを出さない（冒頭で全リセット済みなのでハイライトも消える）。 */
        var myGrpEl = rounds[myRound].querySelectorAll('.br-group')[myGroup];
        if(myGrpEl && myGrpEl.classList.contains('has-winner')){
          var w = (myGrpEl.dataset && myGrpEl.dataset.winnerName) ? myGrpEl.dataset.winnerName.trim() : '';
          if(w && w !== name){
            return;   // 敗退 → バナーごと非表示
          }
        }

        var label = rounds[myRound].querySelector('.br-round-label');
        var roundName = label ? label.textContent.trim() : ('R'+(myRound+1));

        /* 「あと〇組」= 自分が次に出走するまでに消化される残り組数。
           - 数えないラウンド：準決勝・決勝・3位決定戦・敗者復活戦・裏トーナメント。
             それ以外（ラウンド1〜N・準々決勝）はすべて数える。
           - 自分の現在地グループより前にあり、勝者未確定（.has-winner なし）の
             グループを数える。確定済みは自動的に差し引かれリアルタイムに減る。 */
        function isExcludedRound(rnd){
          var lb = rnd.querySelector('.br-round-label');
          var t = lb ? lb.textContent.trim() : '';
          /* 準決勝・決勝・3位決定戦も出走順バーを表示する
             （スーパーシード等、準決勝から登場するレーサーにも出すため）。
             敗者復活戦・裏トーナメントのみ除外する
             （別ブラケットで「残り組数」の概念が本戦と異なるため）。 */
          if(t.indexOf('敗者復活') >= 0) return true;   // 敗者復活戦
          if(t.indexOf('裏') >= 0) return true;          // 裏トーナメント（裏R…）
          return false;
        }

        /* 自分の現在地ラウンドが除外ラウンド（準決勝・決勝・3位・敗者復活・裏）の場合は、
           出走順バナーを出さない（残り組数の概念を適用しない）。 */
        if(isExcludedRound(rounds[myRound])){
          return;
        }

        var before = 0;
        rounds.forEach(function(rnd, ri){
          if(isExcludedRound(rnd)) return;            // 除外ラウンドは数えない
          var grps = rnd.querySelectorAll('.br-group');
          grps.forEach(function(grp, gi){
            // 現在地グループ自身・それより後ろは数えない
            if(ri > myRound) return;
            if(ri === myRound && gi >= myGroup) return;
            // 勝者確定済み（消化済み）は数えない
            if(grp.classList.contains('has-winner')) return;
            before++;
          });
        });

        if(before === 0){
          /* 自分の出走順が来た */
          msgs.push(roundName + ' 第' + (myGroup+1) + 'グループ あなたの番です');
          bannerBg = 'rgb(200,40,40)';      /* 最も目立つ赤 */
        } else if(before === 1){
          msgs.push(roundName + ' 第' + (myGroup+1) + 'グループ あなたは次です');
          bannerBg = 'rgb(210,140,30)';     /* 少し目立つ橙 */
        } else {
          msgs.push(roundName + ' 第' + (myGroup+1) + 'グループ（あと' + before + '組）');
          bannerBg = '#154360';             /* 通常（濃紺） */
        }
        /* 他は薄くしない。対象レーサーのスロット名だけオレンジで強調する。 */
        rounds.forEach(function(rnd){
          rnd.querySelectorAll('.br-slot-name').forEach(function(el){
            if(el.textContent.trim() === name){
              /* 名前要素・スロットの両方に背景rgb(230,126,34)を important で当てる
                 （子要素が背景色を持つと親の background が見えないため）。
                 枠はより濃いオレンジ、文字は白。 */
              el.style.setProperty('background', 'rgb(230,126,34)', 'important');
              el.style.setProperty('color', '#ffffff', 'important');
              el.style.fontWeight = 'bold';
              var slot = el.closest('.br-slot') || el.parentElement;
              if(slot){
                slot.style.setProperty('background', 'rgb(230,126,34)', 'important');
                slot.style.setProperty('box-shadow', 'inset 0 0 0 2px rgb(160,80,15)', 'important');
              }
            }
          });
        });
      }
    }

    /* 予選 racer-cell / heat lane */
    var racerCells = document.querySelectorAll('.racer-cell');
    if(racerCells.length > 0){
      /* 自分が出走する行のうち、まだ結果が出ていない（done でない）最初の行＝
         「自分の次の出走」。その行より前にある未了レース数を「あと〇走」とする。
         結果が出た行（done）は差し引かれ、更新検知リロードのたびに減っていく。 */
      var myTr = null, myTbody = null;
      racerCells.forEach(function(cell){
        if(myTr) return;
        if(cell.textContent.trim() === name){
          var tr = cell.closest('tr');
          if(tr && !tr.classList.contains('done')){
            myTr = tr; myTbody = tr.closest('tbody');
          }
        }
      });
      if(myTr && myTbody){
        var rows = Array.from(myTbody.querySelectorAll('tr'));
        var myIdx = rows.indexOf(myTr);
        /* 自分の次の出走行より前にある「未了」レース数を数える */
        var before = 0;
        for(var ri = 0; ri < myIdx; ri++){
          if(!rows[ri].classList.contains('done')) before++;
        }
        if(before === 0){
          msgs.push('予選 第' + (myIdx + 1) + '走 あなたの番です');
          bannerBg = 'rgb(200,40,40)';      /* 最も目立つ赤 */
        } else if(before === 1){
          msgs.push('予選 第' + (myIdx + 1) + '走 次です');
          bannerBg = 'rgb(210,140,30)';     /* 少し目立つ橙 */
        } else {
          msgs.push('予選 第' + (myIdx + 1) + '走（あと' + before + '走）');
          bannerBg = '#154360';             /* 通常（濃紺） */
        }
        /* 対象レーサーのセルだけオレンジで強調する。 */
        myTr.querySelectorAll('.racer-cell').forEach(function(cell){
          if(cell.textContent.trim() === name){
            cell.style.setProperty('background', 'rgb(230,126,34)', 'important');
            cell.style.setProperty('color', '#ffffff', 'important');
            cell.style.fontWeight = 'bold';
            cell.style.setProperty('box-shadow', 'inset 0 0 0 2px rgb(160,80,15)', 'important');
          }
        });
      }
    }

    /* ── 決勝入賞チェック（本戦の表彰台のみ。ヒート予選ページでは判定しない）──
       選択レーサーが本戦の表彰台（1〜3位）にいれば、順番待ち表示を祝福メッセージに差し替える。 */
    if(document.querySelectorAll('[id^="viewer-ht-bracket-"]').length === 0){
      var _podRank = 0;
      [['.br-champion','.br-champion-name',1],
       ['.br-runner-up','.br-runner-up-name',2],
       ['.br-third-pod','.br-third-pod-name',3]].forEach(function(p){
        document.querySelectorAll(p[0]).forEach(function(card){
          var nm = card.querySelector(p[1]);
          if(nm && nm.textContent.trim() === name){ _podRank = p[2]; }
        });
      });
      if(_podRank === 1){
        msgs = ['🎉🏆✨　優勝おめでとうございます　✨🏆🎉'];
        bannerBg = 'linear-gradient(135deg,#b8860b 0%,#f6c744 50%,#b8860b 100%)';
      } else if(_podRank >= 2){
        msgs = ['🎊🏅✨　入賞おめでとうございます　✨🏅🎊'];
        bannerBg = 'linear-gradient(135deg,#1f6fb2 0%,#2ecc71 50%,#1f6fb2 100%)';
      }
    }

    if(msgs.length === 0) return;
    var b = document.createElement('div');
    b.id = 'm4-position-banner';
    /* スクロールしても上部に残るよう固定表示。位置はタイトルヘッダー
       （マイレーサー選択バー m4-my-banner）の直下に重ねる。 */
    var _slotP = document.getElementById('m4-picker-slot');
    var _navEl = document.querySelector('.v-nav');
    var myBar = document.getElementById('m4-my-banner');
    var topPx = _slotP ? (_navEl ? Math.round(_navEl.getBoundingClientRect().height) : 44)
                       : (myBar ? Math.round(myBar.getBoundingClientRect().height) : 44);
    b.style.cssText = 'position:fixed;left:0;width:100vw;top:'+topPx+'px;z-index:8999;'
      + 'background:'+bannerBg+';color:#fff;padding:8px 14px;font-size:13px;'
      + 'text-align:center;font-weight:bold;'
      + 'transform:translateZ(0);-webkit-transform:translateZ(0);'
      + 'box-sizing:border-box;box-shadow:0 2px 6px rgba(0,0,0,.35);';
    b.textContent = msgs.join(' / ');
    document.body.appendChild(b);
    /* 固定した2本のバー（ヘッダー＋出走順）の合計高さぶん本文に余白を確保し、
       本文先頭がバーに隠れないようにする。 */
    var ownH = Math.round(b.getBoundingClientRect().height) || 36;
    /* v-nav(固定) と 出走順バナー の合計高さぶん本文を下げる。 */
    document.body.style.paddingTop = (topPx + ownH) + 'px';
  }

  /* ---------- 全体適用 ---------- */
  function applyAll(){
    var r = getMyRacer();
    var name = r ? r.name : null;
    renderBanner(name); /* 常時表示（名前未選択でもプルダウンを出す） */
    applyEntryHighlight(name);
    renderPositionBanner(name);
  }

  /* ---------- entry-card タップ ---------- */
  function bindEntryCards(){
    document.querySelectorAll('.entry-card').forEach(function(card){
      card.style.cursor = 'pointer';
      card.addEventListener('click', function(){
        var n = card.querySelector('.entry-name');
        if(!n) return;
        var name = n.textContent.trim();
        var cur = getMyRacer();
        if(cur && cur.name === name){
          /* 同じカードを再タップ → 解除 */
          clearMyRacer();
        } else {
          var y = card.querySelector('.entry-yomi');
          setMyRacer({ name: name, yomi: y ? y.textContent.trim() : '' });
        }
        applyAll();
      });
    });
  }

  /* ---------- グローバル公開（バナーの✕ボタン用） ---------- */
  window.m4ClearMyRacer = function(){
    clearMyRacer();
    /* ハイライト解除（important で当てた分は removeProperty で確実に戻す） */
    document.querySelectorAll('.entry-card').forEach(function(card){
      card.style.removeProperty('border');
      card.style.removeProperty('background');
      card.style.removeProperty('color');
      card.style.opacity = '';
      card.style.boxShadow = '';
      var n = card.querySelector('.entry-name');
      if(n){ n.style.removeProperty('background'); n.style.removeProperty('color'); n.style.fontWeight=''; }
    });
    document.querySelectorAll('.br-group').forEach(function(g){ g.style.opacity=''; });
    document.querySelectorAll('tr').forEach(function(r){ r.style.opacity=''; });
    document.querySelectorAll('.br-slot-name').forEach(function(el){
      el.style.removeProperty('background');
      el.style.removeProperty('color');
      el.style.fontWeight = '';
    });
    document.querySelectorAll('.br-slot').forEach(function(s){
      s.style.removeProperty('background');
      s.style.removeProperty('box-shadow');
    });
    document.querySelectorAll('.racer-cell').forEach(function(c){
      c.style.removeProperty('background');
      c.style.removeProperty('color');
      c.style.removeProperty('box-shadow');
      c.style.fontWeight = '';
    });
    var pb = document.getElementById('m4-position-banner');
    if(pb) pb.remove();
    document.body.style.paddingTop = '';
    renderBanner(''); /* プルダウンは維持、選択をリセット */
  };

  /* ---------- selectをfocusしたとき名前を再収集してoptionを更新 ---------- */
  function refreshSelect(){
    var sel = document.getElementById('m4-racer-select');
    if(!sel) return;
    var cur = sel.value;
    var names = collectNames();
    var opts = '<option value="">👤 レーサーを選択</option>';
    names.forEach(function(n){
      opts += '<option value="' + n + '"' + (n === cur ? ' selected' : '') + '>' + n + '</option>';
    });
    sel.innerHTML = opts;
  }

  /* ---------- 初期化（部分更新後の再適用にも使う） ---------- */
  function _m4Init(){
    bindEntryCards();
    applyAll();
    /* selectのfocusで名前を再収集 */
    var sel = document.getElementById('m4-racer-select');
    if(sel) sel.addEventListener('focus', refreshSelect);
  }
  /* .v-container 差し替え（部分更新）後に、更新スクリプトから呼ばれる公開フック */
  window._m4Reapply = function(){ try { _m4Init(); } catch(e){} };
  window.addEventListener('load', function(){
    /* bracket注入JSが300ms後に動くのでさらに後で初期化 */
    setTimeout(_m4Init, 600);
  });
})();
</script>"""
    my_racer_script = my_racer_script.replace('__SLUGKEY__', _slug_key)

    # ▼ 追加：強制リロードボタン（参加者向けHTML用・body直下に生成＝再描画で消えない）
    reload_btn_script = """<script>
(function(){
  if(document.getElementById('m4-reload-btn')) return;
  var b=document.createElement('button');
  b.id='m4-reload-btn'; b.type='button'; b.title='再読み込み';
  b.setAttribute('aria-label','再読み込み'); b.textContent='\u21bb';
  b.style.cssText='position:fixed;top:58px;right:12px;z-index:99999;width:44px;height:44px;'
    +'border-radius:50%;border:none;cursor:pointer;padding:0;background:#f39c12;color:#fff;'
    +'opacity:.62;font-size:24px;line-height:44px;text-align:center;'
    +'box-shadow:0 2px 6px rgba(0,0,0,.3);-webkit-tap-highlight-color:transparent;';
  b.onclick=function(){try{var u=new URL(location.href);u.searchParams.set('_',Date.now());location.replace(u.toString());}catch(e){location.reload();}};
  document.body.appendChild(b);
})();
</script>"""

    # ▼ 追加：画面スリープ抑止（Wake Lock）。観覧画面を開いている間は画面を消えさせない。
    #   非対応端末・取得失敗は黙って無視（従来挙動を壊さない）。
    #   他アプリから戻る等で解除されたら visibilitychange で自動再取得する。
    # 画面スリープ抑止（Wake Lock）。観覧画面を開いている間は画面を消えさせない。
    #   iOS Safari対策：ページ表示時の自動取得は NotAllowedError で拒否されるため、
    #   最初のユーザー操作（タップ/スクロール/キー）を起点に取得する。
    #   ・解放後はタップで再取得 ・一度取得済みなら他アプリから戻った際に自動再取得
    #   ・非対応端末／取得失敗は黙って無視（従来挙動を壊さない）
    wakelock_script = """<script>
(function(){
  if(!('wakeLock' in navigator)) return;   /* 非対応端末は何もしない */
  var wakeLock=null;
  var acquiredOnce=false;

  function requestWakeLock(){
    if(document.visibilityState!=='visible' || wakeLock!==null) return;
    /* ユーザー操作ハンドラ内から同期的に呼ぶこと（iOSの起動条件） */
    navigator.wakeLock.request('screen').then(function(wl){
      wakeLock=wl; acquiredOnce=true;
      wakeLock.addEventListener('release', function(){ wakeLock=null; });
    }).catch(function(){ wakeLock=null; });   /* 取得失敗は黙って無視 */
  }

  /* 最初の操作（および解放後の再操作）で取得する */
  ['click','touchend','keydown'].forEach(function(ev){
    document.addEventListener(ev, function(){ requestWakeLock(); }, {passive:true});
  });

  /* 一度取得できていれば、他アプリから戻ったときに自動で取り直す */
  document.addEventListener('visibilitychange', function(){
    if(document.visibilityState==='visible' && wakeLock===null && acquiredOnce){ requestWakeLock(); }
  });
})();
</script>"""

    info_bar_script = """<script>
(function(){
  var el = document.getElementById('m4-info-data');
  var DATA = {};
  if(el){ try{ DATA = JSON.parse(el.textContent || '{}'); }catch(e){ DATA = {}; } }
  function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  window.m4ToggleZoom = function(img){
    if(img.getAttribute('data-z') === '1'){
      img.style.maxWidth='100%'; img.style.width=''; img.style.cursor='zoom-in'; img.setAttribute('data-z','0');
    } else {
      img.style.maxWidth='none'; img.style.width='auto'; img.style.cursor='zoom-out'; img.setAttribute('data-z','1');
    }
  };
  function slugPrefix(){
    var segs = location.pathname.split('/').filter(function(s){ return s; });
    var reserved = {admin:1, view:1, static:1, health:1, logo:1, api:1, enter:1, entry:1};
    if(segs.length && !reserved[segs[0]]){ return '/' + segs[0]; }
    return '';
  }
  var PREFIX = slugPrefix();
  function assetUrl(u){ return (u.charAt(0) === '/') ? (PREFIX + u) : u; }
  window.m4OpenInfo = function(kind){
    var d = DATA[kind]; if(!d) return;
    var t = document.getElementById('m4-info-title'); if(t){ t.textContent = d.title; }
    var html = '';
    if(d.text){ html += '<div style="white-space:pre-wrap;color:#ecf0f1;font-size:15px;line-height:1.7;margin-bottom:16px">'+esc(d.text)+'</div>'; }
    var items = d.items || [];
    for(var i=0;i<items.length;i++){
      html += '<div style="margin:0 0 16px">';
      html += '<div style="overflow:auto;-webkit-overflow-scrolling:touch;border-radius:8px;background:#000"><img src="'+assetUrl(items[i].u)+'" onclick="m4ToggleZoom(this)" style="max-width:100%;border-radius:8px;display:block;cursor:zoom-in;touch-action:pinch-zoom" data-z="0"></div>';
      html += '<div style="font-size:11px;color:#7f8c8d;margin-top:4px">タップで拡大／スワイプで移動</div>';
      if(items[i].n){ html += '<div style="color:#bdc3c7;font-size:12px;margin-top:2px">'+esc(items[i].n)+'</div>'; }
      html += '</div>';
    }
    if(!html){ html = '<div style="color:#95a5a6">（内容がありません）</div>'; }
    var b = document.getElementById('m4-info-body'); if(b){ b.innerHTML = html; }
    var ov = document.getElementById('m4-info-overlay'); if(ov){ ov.style.display = 'block'; }
    window.__m4ModalOpen = true;
  };
  window.m4CloseInfo = function(){
    var ov = document.getElementById('m4-info-overlay'); if(ov){ ov.style.display = 'none'; }
    window.__m4ModalOpen = false;
  };
  var bar = document.getElementById('m4-info-bar');
  if(bar){ document.body.style.paddingBottom = (Math.round(bar.getBoundingClientRect().height) + 8) + 'px'; }
})();
</script>"""

    patched = patched.replace('</body>', expiry_script + my_racer_script + redraw_script + reload_btn_script + wakelock_script + info_bar_script + '</body>', 1)

    return patched


async def _render_page(view_url: str, store=None) -> str | None:
    """
    viewer の各ルーター関数を直接呼び出してHTMLを生成する。
    戻り値: HTML文字列 or None（対応するページがない場合）
    """
    from app.main import app

    try:
        import os
        import httpx
        from httpx import ASGITransport
        from app.config import (
            IS_CLOUD as _IS_CLOUD, ADMIN_TOKEN as _ADMIN_TOKEN,
            ADMIN_COOKIE as _ADMIN_COOKIE, admin_cookie_name as _acn,
        )
        _headers = {}
        if _IS_CLOUD and store is not None:
            # 店舗別Cookie＋内部レンダリングヘッダ（resolver が店舗を確定）
            _cookies = {_acn(store.id): store.admin_token} if store.admin_token else {}
            _headers["x-internal-store-id"] = str(store.id)
            _secret = os.environ.get("INTERNAL_RENDER_SECRET", "")
            if _secret:
                _headers["x-internal-render-secret"] = _secret
        else:
            _cookies = {_ADMIN_COOKIE: _ADMIN_TOKEN} if (_IS_CLOUD and _ADMIN_TOKEN) else {}
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost", follow_redirects=True, cookies=_cookies, headers=_headers) as client:
            # 参加者向けhtml生成であることをテンプレートへ伝えるため _public=1 を付与
            _sep = "&" if ("?" in view_url) else "?"
            _get_url = view_url + _sep + "_public=1"
            resp = await client.get(_get_url)
            print(f"[public_html] render status: {_get_url} -> {resp.status_code} (final url: {resp.url})", flush=True)
            if resp.status_code == 200:
                return resp.text
            return None
    except Exception as e:
        print(f"[public_html] render error: {e}", flush=True)
        return None


def _find_key_file() -> str | None:
    """
    サービスアカウントキーファイルを探す。
    優先順位:
      1. 環境変数 GOOGLE_APPLICATION_CREDENTIALS
      2. アプリ直下の miniyonku-gcs-key.json
    """
    import os
    # 環境変数が設定されていればそちらを優先（Cloud Run等）
    env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if env and os.path.exists(env):
        return env
    # アプリ直下を探す（ローカルPC運用）
    base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    key_path = os.path.join(base, "miniyonku-gcs-key.json")
    if os.path.exists(key_path):
        return key_path
    return None


async def _inject_bracket_html(html: str, view_url: str, store=None) -> str:
    """
    静的HTML内の動的fetchで埋まるコンテナに、実際のbracket/htmlを埋め込む。
    対象:
      - #viewer-ht-bracket-{hno}  (qualifying/heat-tournament)
      - #bracket-html-container    (bracket)
    """
    import os
    import re
    from app.main import app
    import httpx
    from httpx import ASGITransport
    from app.config import (
        IS_CLOUD as _IS_CLOUD2, ADMIN_TOKEN as _ADMIN_TOKEN2,
        ADMIN_COOKIE as _ADMIN_COOKIE2, admin_cookie_name as _acn2,
    )
    _headers2 = {}
    if _IS_CLOUD2 and store is not None:
        _cookies2 = {_acn2(store.id): store.admin_token} if store.admin_token else {}
        _headers2["x-internal-store-id"] = str(store.id)
        _secret2 = os.environ.get("INTERNAL_RENDER_SECRET", "")
        if _secret2:
            _headers2["x-internal-render-secret"] = _secret2
    else:
        _cookies2 = {_ADMIN_COOKIE2: _ADMIN_TOKEN2} if (_IS_CLOUD2 and _ADMIN_TOKEN2) else {}

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost", follow_redirects=True, cookies=_cookies2, headers=_headers2) as client:

        # ① qualifying heat-tournament ブラケット（section単位で注入）
        # 差込先 id は viewer-ht-bracket-{hno}-{section_no}（section_no=0 はヒート決勝）。
        # view 側 JS（loadHtBracketSection）と同一のエンドポイント・パラメータで取得する。
        m = re.search(r"/view/tournament/(\d+)/qualifying", view_url)
        if m:
            tid = m.group(1)
            _PH = '<div style="color:#7f8c8d;font-size:12px;padding:8px">読み込み中...</div>'
            # HTMLから (hno, section_no) を収集
            ht_pairs = re.findall(r'id="viewer-ht-bracket-(\d+)-(\d+)"', html)
            for hno, sec in ht_pairs:
                _url = (
                    f"/admin/tournaments/{tid}/qualifying/heat-tournament/{hno}"
                    f"/bracket/html?compact=2&section_no={sec}"
                )
                if int(sec) == 0:
                    _url += "&heat_final=1"
                resp = await client.get(_url)
                if resp.status_code != 200:
                    continue
                bracket_html = resp.text
                # 該当コンテナ（id）以降の最初の「読み込み中」プレースホルダだけを差し替える
                marker = f'id="viewer-ht-bracket-{hno}-{sec}"'
                pos = html.find(marker)
                if pos == -1:
                    continue
                ph_pos = html.find(_PH, pos)
                if ph_pos == -1:
                    continue
                html = html[:ph_pos] + bracket_html + html[ph_pos + len(_PH):]

            # heat-roundrobin ブラケット
            hr_pattern = re.compile(r'id="viewer-hr-bracket-(\d+)"')
            hr_nos = hr_pattern.findall(html)
            for hno in hr_nos:
                resp = await client.get(
                    f"/admin/tournaments/{tid}/qualifying/heat-roundrobin/{hno}/bracket/html"
                )
                if resp.status_code == 200:
                    bracket_html = resp.text
                    old_container = re.search(
                        f'id="viewer-hr-bracket-{hno}"' + r'[^>]*>.*?</div>',
                        html, flags=re.DOTALL
                    )
                    if old_container:
                        tag_open = old_container.group(0).split('>')[0] + '>'
                        html = html[:old_container.start()] + tag_open + bracket_html + '</div>' + html[old_container.end():]

        # ② bracket (決勝トーナメント)
        m = re.search(r"/view/tournament/(\d+)/bracket", view_url)
        if m:
            tid = m.group(1)
            if 'id="bracket-html-container"' in html:
                resp = await client.get(f"/admin/tournaments/{tid}/bracket/html")
                if resp.status_code == 200:
                    bracket_html = resp.text
                    if bracket_html.strip() and '開始されていません' not in bracket_html:
                        html = html.replace(
                            '<div id="bracket-html-container"></div>',
                            f'<div id="bracket-html-container">{bracket_html}</div>'
                        )
                        # bracket注入成功時はno-bracketを非表示に戻す
                        html = html.replace(
                            '<div id="no-bracket" style="display:block;',
                            '<div id="no-bracket" style="display:none;',
                        )

    return html


async def _upload_to_gcs(html: str, bucket: str) -> bool:
    """GCS の index.html に上書きアップロード"""
    try:
        from google.cloud import storage  # type: ignore
        from google.oauth2 import service_account  # type: ignore

        key_path = _find_key_file()
        if key_path:
            credentials = service_account.Credentials.from_service_account_file(
                key_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            client = storage.Client(credentials=credentials)
        else:
            # Cloud Run上はデフォルト認証（環境変数不要）
            client = storage.Client()

        b = client.bucket(bucket)
        blob = b.blob("index.html")
        blob.cache_control = "no-store, max-age=0"
        blob.upload_from_string(
            html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )
        print(f"[public_html] uploaded to gs://{bucket}/index.html", flush=True)
        return True
    except Exception as e:
        print(f"[public_html] GCS upload error: {e}", flush=True)
        return False


# host_state を参照するためにviewerモジュールから取得（複数店舗化: 店舗別）
def _current_store():
    try:
        from app.store_context import current_store
        return current_store.get()
    except Exception:
        return None


def _get_host_state_url(store=None) -> str:
    try:
        from app.routers.viewer import _host_states
        sid = store.id if store is not None else 0
        st = _host_states.get(sid) or _host_states.get(0) or {}
        return st.get("url", "/view/")
    except Exception:
        return "/view/"


async def export_current_html(db=None) -> bool:
    """
    現在のhost_state URLに対応するページを生成して配信する。

    - クラウド版（IS_CLOUD）：PUBLIC_HTML_DIR/index.html へ書き出す（nginx が直接配信）。
      設定の public_html_enabled / bucket には依存しない（常に書き出す）。
    - オンプレ版：従来どおり、設定が有効な場合のみ GCS へアップロードする。

    設定が無効な場合（オンプレ）は何もせず True を返す。
    db引数は後方互換のため残しているが使用しない（内部で新規接続を開く）。
    """
    import aiosqlite
    from app.models.database import DB_PATH
    from app.config import IS_CLOUD, PUBLIC_HTML_DIR

    # 複数店舗化：現在の店舗（ContextVar）を解決。クラウドで未解決なら既定店舗。
    store = _current_store()
    if IS_CLOUD and store is None:
        try:
            from app import registry
            store = registry.get_default_store()
        except Exception:
            store = None

    db_path = store.db_path if store else DB_PATH
    out_dir = store.public_dir if store else PUBLIC_HTML_DIR
    slug = store.slug if store else ""

    async with aiosqlite.connect(db_path) as own_db:
        own_db.row_factory = aiosqlite.Row

        settings = await _get_settings(own_db)

        # オンプレ：設定が無効なら何もしない。クラウド：常に書き出す。
        if not IS_CLOUD and settings.get("public_html_enabled") != "1":
            return True

        if not IS_CLOUD:
            bucket = settings.get("public_html_gcs_bucket", "").strip()
            if not bucket:
                print("[public_html] bucket name not configured", flush=True)
                return False

        view_url = await _resolve_active_url(own_db, store)
        print(f"[public_html] target url: {view_url} (store={slug or 'default'})", flush=True)

        html = await _render_page(view_url, store)
        if not html:
            print(f"[public_html] render FAILED for: {view_url}", flush=True)
            return False

        print(f"[public_html] render OK ({len(html)} bytes), injecting bracket html...", flush=True)
        html = await _inject_bracket_html(html, view_url, store)
        html = _patch_html_for_static(html, slug)
        # ホーム画面アイコン（Webアプリ）: <head> へメタ注入 ＋ 静的 manifest 書き出し（クラウド版のみ）
        try:
            from app import pwa as _pwa
            if IS_CLOUD:
                _pwa_settings = _pwa.get_pwa_settings(db_path=db_path)
                if _pwa_settings.get("pwa_enabled") == "1":
                    _head = _pwa.render_pwa_head_html(_pwa_settings, slug)
                    if _head and "</head>" in html:
                        html = html.replace("</head>", _head + "</head>", 1)
                    _pwa.write_static_html_manifest(out_dir, _pwa_settings, slug)
        except Exception as _e:
            print(f"[public_html] pwa inject skipped: {_e}", flush=True)
        print(f"[public_html] patched ({len(html)} bytes), publishing...", flush=True)

        if IS_CLOUD:
            result = _write_local_html(html, out_dir)
        else:
            bucket = settings.get("public_html_gcs_bucket", "").strip()
            result = await _upload_to_gcs(html, bucket)
        if result:
            print(f"[public_html] publish OK", flush=True)
        return result


def _write_local_html(html: str, out_dir: str) -> bool:
    """クラウド版：参加者向けHTMLを out_dir/index.html へ書き出す（nginx 直接配信用）。"""
    import os
    if not out_dir:
        print("[public_html] PUBLIC_HTML_DIR が未設定です。", flush=True)
        return False
    try:
        os.makedirs(out_dir, exist_ok=True)
        # 一時ファイルへ書いてから rename（配信中の半端な読み取りを防ぐ）
        tmp = os.path.join(out_dir, ".index.html.tmp")
        dst = os.path.join(out_dir, "index.html")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(tmp, dst)
        print(f"[public_html] wrote {dst}", flush=True)
        return True
    except Exception as e:
        print(f"[public_html] local write error: {e}", flush=True)
        return False


async def _resolve_active_url(db, store=None) -> str:
    """
    参加者向けHTMLの生成対象URLを host_state に完全に一致させる。

    host_state は admin の画面追従で更新される（viewer.host_sync）。
    - host_state がレース画面（/view/tournament/...）      → そのレース画面を生成
    - host_state が /view/（レース4画面以外を開いた/待機） → お待ちくださいを生成
    view 画面は host_state を直接見て描画するため、HTML もこれに揃えることで
    「view は待機中なのに HTML だけレース画面が残る」不一致を防ぐ。
    （以前は host_state が /view/ でも DB の進行中レースを拾ってレース画面を
      返していたため、admin が待機画面に切り替えても HTML が連動しなかった。）
    """
    hs_url = _get_host_state_url(store)
    if hs_url in ("/view/", "/view"):
        return "/view/"
    return hs_url