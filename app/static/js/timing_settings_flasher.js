// timing_settings_flasher.js
// ============================================================================
//  M4LAPS 機材設定フラッシャ（PC/Chrome限定・USB1台ずつ）
//  - サーバーが生成した nvs.bin(0x5000) を、nvs領域 0x9000 に焼くだけ。
//  - 焼く前に MAC を読み、A/Bセットを判定表示（誤書込み防止）。
//  - 焼いた後は再起動して、シリアルの [CFG] 行を best-effort で拾って検証。
//  esptool-js は app/static へ vendor 済み（CDN非依存）。
// ============================================================================
import { ESPLoader, Transport } from "/static/vendor/esptool-js/bundle.js";

const NVS_ADDR = 0x9000; // partitions_8mb.csv の nvs パーティション先頭

// このページ自身のURL（/admin/timing/settings、店舗スラッグ付きにも自動追従）。
const BASE = location.pathname.replace(/\/+$/, "");
const URL_NVS      = `${BASE}/nvs.bin`;
const URL_PROFILES = `${BASE}/profiles`;
const URL_LEDGER   = `${BASE}/ab-ledger`;

const $ = (id) => document.getElementById(id);
const logEl = () => $("log");
function log(msg) {
  const el = logEl();
  if (!el) { console.log(msg); return; }
  el.textContent += msg + "\n";
  el.scrollTop = el.scrollHeight;
}
function setStatus(id, text, cls) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = cls || "";
}

// --- 状態 -------------------------------------------------------------------
let port = null;
let transport = null;
let esploader = null;
let ledger = {};

const term = {
  clean() {},
  writeLine(s) { log(s); },
  write(s) { const el = logEl(); if (el) el.textContent += s; },
};

function normMac(m) { return (m || "").toLowerCase().replace(/-/g, ":").trim(); }

function judgeAB(mac) {
  const m = normMac(mac);
  for (const [label, ab] of Object.entries(ledger)) {
    if (ab.A && normMac(ab.A) === m) return { label, set: "A" };
    if (ab.B && normMac(ab.B) === m) return { label, set: "B" };
  }
  return null;
}

async function loadLedger() {
  try {
    const r = await fetch(URL_LEDGER);
    const j = await r.json();
    ledger = j.ledger || {};
  } catch (e) { log("A/B台帳の取得に失敗: " + e); }
}

// --- USB接続 & MAC読み出し --------------------------------------------------
async function connect() {
  if (!("serial" in navigator)) {
    alert("このブラウザは Web Serial 非対応です。Chrome か Edge を使ってください。");
    return;
  }
  try {
    port = await navigator.serial.requestPort();
    transport = new Transport(port, true);
    esploader = new ESPLoader({
      transport,
      baudrate: 115200,
      romBaudrate: 115200,
      terminal: term,
      debugLogging: false,
    });
    setStatus("chip-info", "接続中…", "muted");
    const chip = await esploader.main();      // 同期・チップ検出
    const mac = await esploader.chip.readMac(esploader);
    setStatus("chip-info", `チップ: ${chip}`, "ok");
    setStatus("mac-info", `MAC: ${mac}`, "ok");

    const j = judgeAB(mac);
    if (j) {
      setStatus("ab-info", `→ ${j.label} / ${j.set}セット`, "ok-strong");
    } else {
      setStatus("ab-info", "→ 台帳に無いMAC（テプラと要照合）", "warn");
    }
    $("btn-flash").disabled = false;
    $("btn-connect").disabled = true;
    $("btn-disconnect").disabled = false;
    log(`[接続] ${chip} / ${mac}`);
  } catch (e) {
    log("[接続失敗] " + e);
    setStatus("chip-info", "接続失敗", "err");
    await safeDisconnect();
  }
}

async function safeDisconnect() {
  try { if (transport) await transport.disconnect(); } catch (_) {}
  transport = null; esploader = null;
  $("btn-connect").disabled = false;
  $("btn-flash").disabled = true;
  $("btn-disconnect").disabled = true;
}

// --- 設定NVSを生成して焼く --------------------------------------------------
function formValues() {
  return {
    ssid:  $("f-ssid").value,
    pass:  $("f-pass").value,
    host:  $("f-host").value,
    ip:    $("f-ip").value,
    token: $("f-token").value,
  };
}

async function fetchNvsBin() {
  const r = await fetch(URL_NVS, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(formValues()),
  });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
    throw new Error("NVS生成に失敗: " + msg);
  }
  return new Uint8Array(await r.arrayBuffer());
}

function u8ToBinStr(u8) {
  let s = "";
  for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
  return s;
}

async function flash() {
  if (!esploader) { alert("先に接続してください"); return; }
  if (!$("f-ssid").value.trim()) { alert("SSIDは必須です"); return; }
  const target = $("f-target").value || "GW";
  if (!confirm(
      `この1台に設定を書き込みます。\n` +
      `対象=${target} / SSID=${$("f-ssid").value}\n\n` +
      `⚠ 接続は1台だけ・バッテリーは外した状態で。よろしいですか？`)) return;

  $("btn-flash").disabled = true;
  try {
    log("[生成] サーバーで nvs.bin を作成中…");
    const bin = await fetchNvsBin();
    log(`[生成] nvs.bin ${bin.length} bytes`);

    log(`[書込] nvs 領域 0x${NVS_ADDR.toString(16)} へ…`);
    await esploader.writeFlash({
      fileArray: [{ data: u8ToBinStr(bin), address: NVS_ADDR }],
      flashSize: "keep",
      flashMode: "keep",
      flashFreq: "keep",
      eraseAll: false,
      compress: true,
      reportProgress: (idx, written, total) => {
        setStatus("flash-progress", `書込 ${Math.round((written / total) * 100)}%`, "muted");
      },
    });
    log("[書込] 完了。再起動します…");
    try { await esploader.hardReset(); } catch (_) { try { await esploader.after("hard_reset"); } catch (_) {} }

    // --- 検証（best-effort）：再起動後の [CFG] 行を拾う ---
    await verifyBoot();

    setStatus("flash-progress", "書込 完了 ✓", "ok-strong");
    alert("書き込み完了。ログの [CFG] src=NVS を確認してください。");
  } catch (e) {
    log("[書込失敗] " + e);
    setStatus("flash-progress", "書込 失敗", "err");
    alert("書き込みに失敗しました。ログを確認してください。");
  } finally {
    // 焼いた1台は切断（次の1台に備える）。
    await safeDisconnect();
  }
}

async function verifyBoot() {
  log("[検証] 再起動後のシリアルを読みます（最大5秒）…");
  try {
    await transport.disconnect();
  } catch (_) {}
  transport = null; esploader = null;
  // 同じportを115200で開き直して数秒読む。
  try {
    await port.open({ baudRate: 115200 });
  } catch (e) {
    log("[検証] シリアル再オープン不可（手元のモニタで [CFG] を確認してください）: " + e);
    return;
  }
  const decoder = new TextDecoder();
  const reader = port.readable.getReader();
  let buf = "";
  const deadline = Date.now() + 5000;
  try {
    while (Date.now() < deadline) {
      const { value, done } = await Promise.race([
        reader.read(),
        new Promise((res) => setTimeout(() => res({ value: undefined, done: false }), 400)),
      ]);
      if (done) break;
      if (value) {
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).replace(/\r$/, "");
          buf = buf.slice(nl + 1);
          if (line) log("  » " + line);
          if (line.includes("[CFG]")) {
            if (line.includes("src=NVS")) setStatus("verify-info", "検証: NVS反映を確認 ✓", "ok-strong");
            else setStatus("verify-info", "検証: まだ既定値のようです（要確認）", "warn");
          }
        }
      }
    }
  } catch (e) {
    log("[検証] 読み取り中断: " + e);
  } finally {
    try { reader.releaseLock(); } catch (_) {}
    try { await port.close(); } catch (_) {}
  }
}

// --- プロファイル（機材ごとに1件） -----------------------------------------
let profileCache = {};   // { target: {ssid,wifi_pass,host,ip,token,...} }

async function loadProfiles() {
  try {
    const r = await fetch(URL_PROFILES);
    const j = await r.json();
    profileCache = {};
    for (const p of (j.profiles || [])) profileCache[p.target] = p;
  } catch (e) { log("設定の取得に失敗: " + e); }
  fillFormForTarget($("f-target").value);
}

function fillFormForTarget(target) {
  const p = profileCache[target] || null;
  $("f-ssid").value  = p ? p.ssid : "";
  $("f-pass").value  = p ? p.wifi_pass : "";
  $("f-host").value  = p ? p.host : "";
  $("f-ip").value    = p ? p.ip : "";
  $("f-token").value = p ? p.token : "";
}

function onTargetChange() { fillFormForTarget($("f-target").value); }

async function saveProfile() {
  const target = $("f-target").value;
  const fd = new FormData();
  fd.append("target", target);
  fd.append("ssid",   $("f-ssid").value.trim());
  fd.append("wifi_pass", $("f-pass").value);
  fd.append("host",   $("f-host").value.trim());
  fd.append("ip",     $("f-ip").value.trim());
  fd.append("token",  $("f-token").value);
  const r = await fetch(URL_PROFILES, { method: "POST", body: fd });
  const j = await r.json().catch(() => ({}));
  if (r.ok && j.ok) { await loadProfiles(); alert(`${target} の設定を保存しました`); }
  else alert("保存に失敗: " + (j.detail || r.status));
}

async function deleteProfile() {
  const target = $("f-target").value;
  if (!profileCache[target]) { alert("この機材の保存済み設定はありません"); return; }
  if (!confirm(`${target} の保存済み設定を削除しますか？`)) return;
  const r = await fetch(`${URL_PROFILES}/${encodeURIComponent(target)}/delete`, { method: "POST" });
  if (r.ok) { await loadProfiles(); alert("削除しました"); }
  else alert("削除に失敗しました");
}

// --- 初期化 -----------------------------------------------------------------
function init() {
  if (!("serial" in navigator)) {
    setStatus("serial-support",
      "⚠ このブラウザは Web Serial 非対応です。Chrome / Edge（PC）で開いてください。", "err");
    $("btn-connect").disabled = true;
  }
  $("btn-connect").addEventListener("click", connect);
  $("btn-disconnect").addEventListener("click", safeDisconnect);
  $("btn-flash").addEventListener("click", flash);
  $("f-target").addEventListener("change", onTargetChange);
  $("btn-save-profile").addEventListener("click", saveProfile);
  $("btn-delete-profile").addEventListener("click", deleteProfile);
  loadLedger();
  loadProfiles();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
