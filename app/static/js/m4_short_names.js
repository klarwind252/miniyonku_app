/* ★20261117 予選・決勝画面のレーサー名を「（地域）ごと省略」して表示する（admin・観覧・参加者HTML共通）。
   ルール：（ より前の名前が同じ人が他にいなければ（地域）を省略、2人以上いれば（）付きのまま。
     例「あああああ（福岡）」「あああああ（ふくおか）」→ そのまま ／「いいいいい（福岡）」→「いいいいい」
   - 対応表（正式名→省略名）はサーバーが <div id="m4-name-short"> にJSONで埋め込む（そのレースの全エントリーで判定）。
   - 画面の文字だけを置き換え、元の要素には data-racer-name="正式名" を残す（データ・照合は正式名のまま）。
   - エントリー一覧（.entry-card / .entry-chip）・レーサー選択パネル・入力欄は置き換えない。
   - 後からJSで描き直される部分（順位表・トーナメント表など）も MutationObserver で追従する。 */
(function(){
  "use strict";
  if (window.__m4ShortNames) return;
  window.__m4ShortNames = true;

  /* 正式名を返す（data-racer-name 優先。子孫に1つだけあればそれ）。他スクリプトからも使う。 */
  window.m4nm = window.m4nm || function(el){
    if (!el) return "";
    var a = el.getAttribute && el.getAttribute("data-racer-name");
    if (a) return a;
    if (el.querySelectorAll){
      var d = el.querySelectorAll("[data-racer-name]");
      if (d.length === 1) return d[0].getAttribute("data-racer-name") || "";
    }
    return el.textContent || "";
  };

  var MAP = null;
  function loadMap(){
    var el = document.getElementById("m4-name-short");
    if (!el) return null;
    try{ var m = JSON.parse(el.textContent || "{}"); return (m && typeof m === "object") ? m : null; }catch(e){ return null; }
  }

  var SKIP = ".entry-card,.entry-chip,[data-keep-fullname],#m4-racer-panel,#m4-name-short,script,style,textarea,select,option,input";
  function skip(node){
    var p = node.parentElement;
    return !p || (p.closest && p.closest(SKIP));
  }
  function fixText(node){
    var t = node.nodeValue;
    if (!t) return;
    var k = t.trim();
    if (!k || !Object.prototype.hasOwnProperty.call(MAP, k)) return;
    if (skip(node)) return;
    var p = node.parentElement;
    if (p && !p.getAttribute("data-racer-name")) p.setAttribute("data-racer-name", k);
    if (p && !p.getAttribute("title")) p.setAttribute("title", k);
    node.nodeValue = t.replace(k, MAP[k]);
  }
  function walk(root){
    if (!root) return;
    if (root.nodeType === 3){ fixText(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
    var n, list = [];
    while ((n = w.nextNode())) list.push(n);
    list.forEach(fixText);
  }

  function start(){
    MAP = loadMap();
    if (!MAP || !Object.keys(MAP).length) return;
    walk(document.body);
    var pending = [], sched = false;
    var mo = new MutationObserver(function(muts){
      muts.forEach(function(m){
        if (m.type === "characterData") pending.push(m.target);
        else m.addedNodes.forEach(function(x){ pending.push(x); });
      });
      if (!sched){
        sched = true;
        (window.requestAnimationFrame || setTimeout)(function(){
          sched = false;
          var p = pending; pending = [];
          p.forEach(walk);
        });
      }
    });
    mo.observe(document.body, {childList: true, subtree: true, characterData: true});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
