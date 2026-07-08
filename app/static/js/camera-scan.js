/* =========================================================================
 * camera-scan.js  ―  本体カメラによる QR / CODE128 スキャン共通モジュール
 *
 * 外部リーダー（HIDスキャナ）が無い場合に、スマホ/PCの内蔵カメラで
 * エントリーカードのコードを読み取るための共通UI。
 *
 * 依存：/static/js/zxing.min.js（ZXing UMD。window.ZXing を公開）
 *       ※ CDN不要・完全オフライン動作（オンプレLAN内でも動く）
 *
 * 使い方：
 *   M4Scan.open({
 *     title: 'カメラでスキャン',          // 任意：上部見出し
 *     continuous: true,                    // true=連続読み取り / false=1枚読んだら閉じる
 *     onDecode: function(codeText){ ... },  // 読み取り成功時（10桁の文字列が渡る）
 *     onError:  function(message){ ... },   // 任意：起動失敗時
 *     onClose:  function(){ ... }           // 任意：閉じたとき
 *   });
 *   M4Scan.isSupported();  // → true/false（HTTPS・getUserMedia・ZXingの有無）
 *
 * 注意：getUserMedia は HTTPS（または localhost）でのみ動作する。
 *       http で同一LANの別端末から開いた画面では起動できない（仕様）。
 * ========================================================================= */
(function () {
  'use strict';
  if (window.M4Scan) return;

  var overlay = null, reader = null, videoEl = null, statusEl = null;
  var opts = null, lastCode = '', lastTime = 0, audioCtx = null;

  function supported() {
    return !!(navigator.mediaDevices &&
              navigator.mediaDevices.getUserMedia &&
              window.ZXing &&
              window.isSecureContext);
  }

  function beep() {
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      var o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = 'sine'; o.frequency.value = 1100;
      o.connect(g); g.connect(audioCtx.destination);
      var t = audioCtx.currentTime;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.3, t + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
      o.start(t); o.stop(t + 0.13);
    } catch (e) { /* 音は鳴らせなくても続行 */ }
  }

  function setStatus(text, color) {
    if (statusEl) { statusEl.textContent = text; statusEl.style.color = color || '#fff'; }
  }

  function buildUI() {
    overlay = document.createElement('div');
    overlay.id = 'm4scan-overlay';
    overlay.style.cssText =
      'position:fixed;inset:0;z-index:2147483600;background:#000;display:flex;flex-direction:column;';

    var bar = document.createElement('div');
    bar.style.cssText =
      'display:flex;align-items:center;justify-content:space-between;'
      + 'padding:10px 14px;background:#111;color:#fff;font:bold 15px/1.4 sans-serif;';
    var title = document.createElement('span');
    title.textContent = (opts && opts.title) || 'カメラでスキャン';
    var close = document.createElement('button');
    close.textContent = '✕ 閉じる';
    close.style.cssText =
      'background:#c0392b;color:#fff;border:none;border-radius:6px;'
      + 'padding:8px 14px;font:bold 14px sans-serif;cursor:pointer;';
    close.onclick = function () { M4Scan.close(); };
    bar.appendChild(title); bar.appendChild(close);

    var vwrap = document.createElement('div');
    vwrap.style.cssText =
      'flex:1;position:relative;overflow:hidden;display:flex;'
      + 'align-items:center;justify-content:center;background:#000;';
    videoEl = document.createElement('video');
    videoEl.setAttribute('playsinline', '');   // iOS：インライン再生（全画面化させない）
    videoEl.setAttribute('muted', '');
    videoEl.muted = true;
    videoEl.style.cssText = 'width:100%;height:100%;object-fit:cover;';

    var frame = document.createElement('div');   // 中央の照準枠
    frame.style.cssText =
      'position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);'
      + 'width:72%;max-width:340px;height:42%;max-height:240px;'
      + 'border:3px solid rgba(255,255,255,.9);border-radius:12px;'
      + 'box-shadow:0 0 0 9999px rgba(0,0,0,.35);pointer-events:none;';
    vwrap.appendChild(videoEl); vwrap.appendChild(frame);

    statusEl = document.createElement('div');
    statusEl.style.cssText =
      'padding:12px 14px;background:#111;color:#fff;'
      + 'font:14px/1.5 sans-serif;text-align:center;min-height:1.4em;';
    statusEl.textContent = 'カメラを起動しています…';

    overlay.appendChild(bar);
    overlay.appendChild(vwrap);
    overlay.appendChild(statusEl);
    document.body.appendChild(overlay);
  }

  function onResult(text) {
    var now = Date.now();
    // 重複読み取り防止：同じコードは1.5秒以内は無視（枠内に映り続けても連射しない）
    if (text === lastCode && (now - lastTime) < 1500) return;
    lastCode = text; lastTime = now;
    beep();
    setStatus('読み取り: ' + text, '#2ecc71');
    var keepOpen = !!opts.continuous;
    try { if (opts.onDecode) opts.onDecode(text); } catch (e) { /* 呼び出し側エラーは握りつぶさない用に後で確認可 */ }
    if (!keepOpen) { M4Scan.close(); }
  }

  var M4Scan = {
    isSupported: supported,

    open: function (options) {
      opts = options || {};
      if (opts.continuous === undefined) opts.continuous = true;

      if (!supported()) {
        var why = !window.isSecureContext
          ? 'このページはHTTPSではないためカメラを使用できません（クラウド版、またはホストPC本体で操作してください）。'
          : (!window.ZXing
              ? 'スキャナ部品（zxing.min.js）が読み込まれていません。'
              : 'この端末／ブラウザはカメラに対応していません。');
        if (opts.onError) opts.onError(why); else alert(why);
        return;
      }

      lastCode = ''; lastTime = 0;
      buildUI();

      var hints = new Map();
      hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS,
                [ZXing.BarcodeFormat.QR_CODE, ZXing.BarcodeFormat.CODE_128]);
      reader = new ZXing.BrowserMultiFormatReader(hints, 200);

      reader.decodeFromConstraints(
        { video: { facingMode: { ideal: 'environment' } } },  // 背面カメラ優先
        videoEl,
        function (result, err) {
          if (result) {
            onResult(result.getText ? result.getText() : (result.text || String(result)));
          }
          // 読めないフレームごとに err（NotFoundException）が来るが、これは正常なので無視
        }
      ).then(function () {
        setStatus('コードを枠内に映してください');
      }).catch(function (e) {
        var name = (e && e.name) || e;
        var msg = (name === 'NotAllowedError')
            ? 'カメラの使用が許可されませんでした。ブラウザ／OSの権限を確認してください。'
          : (name === 'NotFoundError' || name === 'OverconstrainedError')
            ? 'カメラが見つかりませんでした。'
          : 'カメラを起動できませんでした（' + name + '）。';
        setStatus(msg, '#e74c3c');
        if (opts.onError) opts.onError(msg);
      });
    },

    close: function () {
      try { if (reader) reader.reset(); } catch (e) { /* 解放失敗は無視 */ }
      reader = null;
      if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
      overlay = null; videoEl = null; statusEl = null;
      var cb = opts && opts.onClose;
      if (cb) { try { cb(); } catch (e) {} }
    }
  };

  window.M4Scan = M4Scan;
})();
