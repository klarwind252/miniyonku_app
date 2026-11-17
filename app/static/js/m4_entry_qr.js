/* ★20261117 当日の参加者QR（日替わり2語URL）を「くっきり＋店舗ロゴ入り」で描く共通処理。
   使う画面：観覧の待機画面（viewer/base.html）・TA view（viewer/ta_board.html）。
   - qrcode.min.js でQRの白黒マス情報だけを作り、1マス＝整数ピクセル（画面の拡大率 devicePixelRatio 込み）
     で自前のcanvasに描く。CSSで拡大縮小しないので滲まない。
   - 誤り訂正は M（15%）。ロゴが覆うのはQR全体の約5%なので読み取りに支障はなく、Hより1マスが大きく描ける
     （マス数 53→37前後）＝同じ表示サイズでもくっきり大きい。中央に店舗ロゴを重ねる（管理画面右上QRと同じ方式）。
     ロゴは「QRのURLが指す店舗」のアイコン（/<slug>/pwa/icon-src-512.png）。
   - 60秒ごとに /view/entry-qr を取り直し、09:00 の切替でURLが変わったら描き直す。
   使い方：M4EntryQr.start({box: 要素, wrap: 表示/非表示を切り替える要素, size: function(){ return CSS px }}) */
(function(){
  "use strict";
  if (window.M4EntryQr) return;

  function slugPrefixFromEntryUrl(url){
    // 入場URL（.../<slug>/<観覧語>/enter/<鍵語> または .../<観覧語>/enter/<鍵語>）から店舗prefixを取る
    try{
      var u = new URL(url, location.href);
      var before = u.pathname.replace(/^\/+/, "").split("/enter/")[0];
      var segs = before.split("/");
      return (segs.length >= 2 && segs[0]) ? ("/" + segs[0]) : "";
    }catch(e){ return ""; }
  }

  function roundRect(ctx, x, y, w, h, r){
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r); ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h); ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y); ctx.closePath();
  }

  // QRのマス情報（true=黒）を作る。qrcode.js の内部モデルから直接読む。
  function modules(url){
    var tmp = document.createElement("div");
    var q = new QRCode(tmp, {text: url, width: 64, height: 64,
                             colorDark: "#000", colorLight: "#fff",
                             correctLevel: QRCode.CorrectLevel.M});
    var m = q._oQRCode;
    var n = m.getModuleCount();
    var grid = [];
    for (var r = 0; r < n; r++){ var row = []; for (var c = 0; c < n; c++){ row.push(m.isDark(r, c)); } grid.push(row); }
    return grid;
  }

  // cssSize（表示上の一辺px）以下で、1マスが整数ピクセルになる最大サイズで描く
  function draw(box, url, cssSize, logoImg){
    var grid = modules(url), n = grid.length;
    var dpr = Math.max(1, window.devicePixelRatio || 1);
    var scale = Math.max(1, Math.floor((cssSize * dpr) / n));   // 1マスの実ピクセル（整数）
    var px = n * scale;
    var cv = document.createElement("canvas");
    cv.width = px; cv.height = px;
    cv.style.width = (px / dpr) + "px"; cv.style.height = (px / dpr) + "px";
    cv.style.display = "block";
    cv.style.imageRendering = "pixelated";
    var ctx = cv.getContext("2d");
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, px, px);
    ctx.fillStyle = "#000";
    for (var r = 0; r < n; r++){ for (var c = 0; c < n; c++){ if (grid[r][c]) ctx.fillRect(c * scale, r * scale, scale, scale); } }
    if (logoImg){
      // ロゴは一辺の20%（面積約4%）。白い角丸の下地を敷いてから載せる。
      var L = Math.round(px * 0.20), bp = Math.round(scale * 1.5), cx = px / 2, cy = px / 2;
      ctx.fillStyle = "#fff"; roundRect(ctx, cx - L / 2 - bp, cy - L / 2 - bp, L + bp * 2, L + bp * 2, Math.round(scale * 2)); ctx.fill();
      ctx.imageSmoothingEnabled = true;   // ロゴ画像は滑らかに縮小
      try{ ctx.drawImage(logoImg, Math.round(cx - L / 2), Math.round(cy - L / 2), L, L); }catch(e){}
    }
    box.innerHTML = ""; box.appendChild(cv);
  }

  function start(opt){
    var box = opt.box, wrap = opt.wrap || opt.box;
    if (!box || typeof QRCode === "undefined") return;
    var pfx = location.pathname.split("/view")[0];
    var last = "", logo = null, logoFor = "";
    function render(){
      if (!last) return;
      var size = Math.max(60, Math.floor(opt.size ? opt.size() : 180));
      try{ draw(box, last, size, logo); wrap.style.display = ""; }
      catch(e){ wrap.style.display = "none"; }
    }
    function setUrl(url){
      if (!url || url === last) return;
      last = url;
      var lp = slugPrefixFromEntryUrl(url);
      if (lp + "|" !== logoFor){
        logoFor = lp + "|";
        var im = new Image();
        im.onload = function(){ logo = im; render(); };
        im.onerror = function(){ logo = null; render(); };
        im.src = lp + "/pwa/icon-src-512.png?t=" + Date.now();
      }
      render();   // ロゴ読み込み前でもQRは先に出す（読み込めたら描き直し）
    }
    function tick(){
      fetch(pfx + "/view/entry-qr", {cache: "no-store"})
        .then(function(r){ return r.json(); })
        .then(function(j){ if (j && j.url) setUrl(j.url); })
        .catch(function(){});
    }
    var rt = null;
    window.addEventListener("resize", function(){ clearTimeout(rt); rt = setTimeout(render, 150); });
    tick(); setInterval(tick, 60000);
  }

  window.M4EntryQr = {start: start};
})();
