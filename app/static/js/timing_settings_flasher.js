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
const URL_FW       = `${BASE}/firmware`;

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
let currentReg = {};   // 端末台帳(timing_devices)の登録MAC
let connected = false;
let fwReg = {};   // { env: {version,size,sha256,...} }

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
    currentReg = j.current || {};
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

    const cur = currentReg[normMac(mac)] || null;   // 端末台帳（現用機）一致を最優先
    const j = judgeAB(mac);
    if (cur && j) {
      setStatus("ab-info", `→ ${cur.label}（現用機・${j.set}セット）`, "ok-strong");
    } else if (cur) {
      setStatus("ab-info", `→ ${cur.label}（端末台帳に登録の現用機）`, "ok-strong");
    } else if (j) {
      setStatus("ab-info", `→ ${j.label} / ${j.set}セット（※端末台帳には未登録＝予備機の可能性）`, "warn");
    } else {
      setStatus("ab-info", "→ どの台帳にも無いMAC（テプラと要照合）", "warn");
    }
    connected = true;
    $("btn-connect").disabled = true;
    $("btn-disconnect").disabled = false;
    updateButtons();
    log(`[接続] ${chip} / ${mac}`);
  } catch (e) {
    log("[接続失敗] " + e);
    setStatus("chip-info", "接続失敗", "err");
    await safeDisconnect();
  }
}

async function safeDisconnect() {
  try { if (transport) await transport.disconnect(); } catch (_) {}
  transport = null; esploader = null; connected = false;
  $("btn-connect").disabled = false;
  $("btn-disconnect").disabled = true;
  updateButtons();
}

// 選択中の機材メタ（option の data-* から）
function currentDev() {
  const o = $("f-target").selectedOptions[0];
  if (!o) return { unit: "", env: "", chip: "", wifi: false };
  return { unit: o.value, env: o.dataset.env || "", chip: o.dataset.chip || "",
           wifi: o.dataset.wifi === "1" };
}

// ボタンの活殺（接続状態＋機材種別＋ファーム登録有無で決める）
function updateButtons() {
  const d = currentDev();
  $("btn-flash").disabled    = !connected;                       // 設定(ch)は全機材
  $("btn-flash-fw").disabled = !(connected && !!fwReg[d.env]);  // 本体は登録があれば全機材
}

// --- 設定NVSを生成して焼く --------------------------------------------------
function formValues() {
  return {
    ch:    $("f-ch").value,
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
  // 大きなfactory.bin(1〜2MB)でも固まらないよう32KBずつ変換。
  const CHUNK = 32768;
  const parts = [];
  for (let i = 0; i < u8.length; i += CHUNK) {
    parts.push(String.fromCharCode.apply(null, u8.subarray(i, i + CHUNK)));
  }
  return parts.join("");
}

async function flash() {
  if (!esploader) { alert("先に接続してください"); return; }
  const target = $("f-target").value || "";
  const ch = $("f-ch").value.trim();
  const ssid = $("f-ssid").value.trim();
  if (!ssid && !ch) { alert("chかSSIDのどちらかを入力してください"); return; }
  if (!confirm(
      `この1台に設定を書き込みます。\n` +
      `対象=${target}` + (ch ? ` / ch=${ch}` : "") + (ssid ? ` / SSID=${ssid}` : "") + `\n\n` +
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
  $("f-ch").value    = (p && p.ch != null) ? p.ch : "";
  $("f-ssid").value  = p ? p.ssid : "";
  $("f-pass").value  = p ? p.wifi_pass : "";
  $("f-host").value  = p ? p.host : "";
  $("f-ip").value    = p ? p.ip : "";
  $("f-token").value = p ? p.token : "";
}

function onTargetChange() {
  const d = currentDev();
  // WiFi/サーバー欄はGWのみ表示。ch欄と保存/削除・①は全機材で常時。
  $("wifi-block").style.display = d.wifi ? "" : "none";
  $("target-note").textContent = d.wifi
    ? "GW：ch＋WiFi/サーバーを設定できます（1機材につき1件保存）。"
    : "ESP-NOW専用機：chのみ設定できます（1機材につき1件保存）。";
  fillFormForTarget(d.unit);
  refreshFwAvail();
  updateButtons();
}

function refreshFwAvail() {
  const d = currentDev();
  const m = fwReg[d.env];
  const el = $("fw-avail");
  if (!el) return;
  if (m) el.innerHTML = `登録済み: <strong>${m.version}</strong>（${m.size} bytes・env ${d.env}）`;
  else   el.innerHTML = `env <strong>${d.env}</strong> のファーム未登録（下の「ファーム登録」でアップロード）`;
}

async function saveProfile() {
  const target = $("f-target").value;
  const body = {
    target,
    ch:        $("f-ch").value.trim(),
    ssid:      $("f-ssid").value.trim(),
    wifi_pass: $("f-pass").value,
    host:      $("f-host").value.trim(),
    ip:        $("f-ip").value.trim(),
    token:     $("f-token").value,
  };
  const r = await fetch(URL_PROFILES, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
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

// --- ファーム本体（案A：factoryイメージを 0x0 へ全消去書込み）---------------
async function flashFirmware() {
  if (!esploader) { alert("先に接続してください"); return; }
  const d = currentDev();
  if (!fwReg[d.env]) { alert("この機材のファームは未登録です"); return; }
  if (!confirm(
      `⚠ ${d.unit}（env ${d.env}）にファーム本体を書き込みます。\n` +
      `これは全消去の factory 書込みで、NVS設定も消えます。\n` +
      `接続は1台だけ・バッテリーは外した状態か確認してください。\n\n続行しますか？`)) return;
  $("btn-flash-fw").disabled = true;
  try {
    log(`[本体] ${d.env} の factory.bin を取得中…`);
    const r = await fetch(`${URL_FW}/${encodeURIComponent(d.env)}/blob`);
    if (!r.ok) throw new Error(`取得失敗 HTTP ${r.status}`);
    const bin = new Uint8Array(await r.arrayBuffer());
    log(`[本体] ${bin.length} bytes を 0x0 に書込み（全消去）…`);
    await esploader.writeFlash({
      fileArray: [{ data: u8ToBinStr(bin), address: 0x0 }],
      flashSize: "keep", flashMode: "keep", flashFreq: "keep",
      eraseAll: true, compress: true,
      reportProgress: (i, w, t) =>
        setStatus("flash-progress", `本体書込 ${Math.round((w / t) * 100)}%`, "muted"),
    });
    log("[本体] 完了。再起動します…");
    try { await esploader.hardReset(); } catch (_) { try { await esploader.after("hard_reset"); } catch (_) {} }
    setStatus("flash-progress", "本体書込 完了 ✓", "ok-strong");
    alert(d.wifi
      ? "本体書込み完了。NVSが消えたので、①で設定を焼き直してください。"
      : "本体書込み完了。");
  } catch (e) {
    log("[本体書込失敗] " + e);
    setStatus("flash-progress", "本体書込 失敗", "err");
    alert("ファーム書込みに失敗しました。ログを確認してください。");
  } finally {
    await safeDisconnect();
  }
}

// --- ファームレジストリ（開発用アップロード・一覧） ------------------------
async function loadFirmwareRegistry() {
  try {
    const r = await fetch(URL_FW);
    const j = await r.json();
    fwReg = j.firmware || {};
  } catch (e) { log("ファーム一覧の取得に失敗: " + e); fwReg = {}; }
  renderFwList();
  refreshFwAvail();
  updateButtons();
}

function renderFwList() {
  const el = $("fw-list"); if (!el) return;
  const envs = Object.keys(fwReg).sort();
  if (!envs.length) { el.innerHTML = '<div class="muted">登録なし</div>'; return; }
  el.innerHTML = envs.map((env) => {
    const m = fwReg[env];
    return `<div class="row"><span><strong>${env}</strong> — ${m.version}（${m.size}B）</span>` +
      `<button type="button" class="btn btn-secondary btn-sm" data-del-env="${env}">削除</button></div>`;
  }).join("");
  el.querySelectorAll("[data-del-env]").forEach((b) =>
    b.addEventListener("click", () => deleteFirmware(b.dataset.delEnv)));
}

async function deleteFirmware(env) {
  if (!confirm(`env ${env} の登録ファームを削除しますか？`)) return;
  const r = await fetch(`${URL_FW}/${encodeURIComponent(env)}/delete`, { method: "POST" });
  if (r.ok) await loadFirmwareRegistry();
}

async function uploadFirmware() {
  const env = $("fw-env").value;
  const file = $("fw-file").files[0];
  if (!file) { alert("factory.bin を選んでください"); return; }
  const version = $("fw-version").value.trim();
  const url = `${URL_FW}/${encodeURIComponent(env)}` +
              (version ? `?version=${encodeURIComponent(version)}` : "");
  $("btn-fw-upload").disabled = true;
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,                     // 生バイナリ（multipart非依存）
    });
    const j = await r.json().catch(() => ({}));
    if (r.ok && j.ok) { $("fw-file").value = ""; await loadFirmwareRegistry(); alert(`${env} を登録しました`); }
    else alert("登録に失敗: " + (j.detail || r.status));
  } finally { $("btn-fw-upload").disabled = false; }
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
  $("btn-flash-fw").addEventListener("click", flashFirmware);
  $("btn-fw-upload").addEventListener("click", uploadFirmware);
  $("f-target").addEventListener("change", onTargetChange);
  $("btn-save-profile").addEventListener("click", saveProfile);
  $("btn-delete-profile").addEventListener("click", deleteProfile);
  loadLedger();
  loadProfiles();
  loadFirmwareRegistry();
  onTargetChange();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
