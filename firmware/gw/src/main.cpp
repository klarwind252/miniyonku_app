// firmware/gw/src/main.cpp
// ============================================================================
//  M4LAPS ゲートウェイ（GW6／予備GW7）ファーム
//  役割（docs/14 DA1/DA2）：状態を持たない記録係。全ゲートの集約点。
//    - SYNC_REQに応答（親時計）
//    - S/G（自分の3レーン）のビームも検出（GWはS/G一体）
//    - ノードからのEVENTを受けてEVENT_ACKを返し、LittleFSに追記
//    - JOINをサーバー /api/timing/join へ転送し、必要ならJOIN_ACKを返す
//    - 溜めたEVENTをWiFiでサーバーへPOST（200が返るまで消さない・S8）
//  common（protocol/espnow_link/timesync/beam）を呼ぶ。TFTは最小デバッグ表示。
// ============================================================================
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <LittleFS.h>
#include "protocol.h"
#include "espnow_link.h"
#include "timesync.h"
#include "beam.h"
#include "secrets.h"

#ifndef NODE_ID
#define NODE_ID 6
#endif
#ifndef ESPNOW_CHANNEL
#define ESPNOW_CHANNEL 1
#endif

// ---- 受信EVENTの一時レコード ----------------------------------------------
//  冪等キー (device_id, src, src_boot_id, seq) はサーバが弾く（D12）。
struct RawEvent {
  uint8_t  src;         // 送信元node_id（=device_id）
  uint32_t src_boot;    // 送信元boot_id
  uint32_t seq;
  uint8_t  lane;
  uint8_t  quality;
  uint64_t t_us;
  uint64_t t_us_b;
};

static const char* SPOOL = "/spool.jsonl";   // 未送信EVENT（1行1件）
static uint32_t s_race_id = 0;               // 現在のrace_id（0=未開始）

// ---- WiFi接続 --------------------------------------------------------------
static bool wifi_up() {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) delay(100);
  return WiFi.status() == WL_CONNECTED;
}

// ---- JOINをサーバーへ転送（未割当ノード報告・docs/19.5）--------------------
static void forward_join(const proto::JoinBody& jb) {
  if (!wifi_up()) return;
  char mac[18];
  snprintf(mac, sizeof(mac), "%02X:%02X:%02X:%02X:%02X:%02X",
           jb.mac[0],jb.mac[1],jb.mac[2],jb.mac[3],jb.mac[4],jb.mac[5]);
  char body[192];
  snprintf(body, sizeof(body),
    "{\"mac\":\"%s\",\"kind\":%u,\"fw_major\":%u,\"fw_minor\":%u,\"nvs_node_id\":%u}",
    mac, jb.kind, jb.fw_major, jb.fw_minor, jb.nvs_node_id);

  HTTPClient http;
  http.begin(String(SERVER_BASE) + "/api/timing/join");
  http.addHeader("Content-Type", "application/json");
  if (strlen(TIMING_TOKEN)) http.addHeader("X-Timing-Token", TIMING_TOKEN);
  int code = http.POST((uint8_t*)body, strlen(body));
  if (code == 200) {
    String resp = http.getString();
    int p = resp.indexOf("\"node_id\"");
    if (resp.indexOf("assigned") >= 0 && p >= 0) {
      int c = resp.indexOf(':', p);
      int nid = resp.substring(c+1).toInt();
      proto::JoinAckBody ack = {};
      memcpy(ack.mac, jb.mac, 6);
      ack.node_id = (uint8_t)nid;
      ack.channel = ESPNOW_CHANNEL;
      mesh::send(proto::PT_JOIN_ACK, (uint8_t)nid, &ack, sizeof(ack));
    }
  }
  http.end();
}

// ---- EVENTを1行JSONでスプールに追記 ---------------------------------------
static void spool_append(const RawEvent& e) {
  File f = LittleFS.open(SPOOL, FILE_APPEND);
  if (!f) return;
  char line[224];
  if (e.t_us_b) {
    snprintf(line, sizeof(line),
      "{\"device_id\":%u,\"src\":%u,\"src_boot_id\":%u,\"seq\":%u,"
      "\"lane\":%u,\"t_us\":%llu,\"t_us_b\":%llu,\"quality\":%u}\n",
      e.src, e.src, e.src_boot, e.seq, e.lane,
      (unsigned long long)e.t_us, (unsigned long long)e.t_us_b, e.quality);
  } else {
    snprintf(line, sizeof(line),
      "{\"device_id\":%u,\"src\":%u,\"src_boot_id\":%u,\"seq\":%u,"
      "\"lane\":%u,\"t_us\":%llu,\"quality\":%u}\n",
      e.src, e.src, e.src_boot, e.seq, e.lane,
      (unsigned long long)e.t_us, e.quality);
  }
  f.print(line);
  f.close();
}

// ---- 受信ハンドラ ----------------------------------------------------------
static void on_recv(const proto::PktHeader& h, const uint8_t* body,
                    int body_len, const uint8_t* /*mac*/) {
  switch (h.type) {
    case proto::PT_SYNC_REQ: {
      if (body_len >= (int)sizeof(proto::SyncBody)) {
        proto::SyncBody b; memcpy(&b, body, sizeof(b));
        tsync::gw_reply(h, b);                 // 親時計として即応答
      }
    } break;

    case proto::PT_EVENT: {
      if (body_len >= (int)sizeof(proto::EventBody)) {
        proto::EventBody b; memcpy(&b, body, sizeof(b));
        mesh::send_ack(proto::PT_EVENT_ACK, h.src, h.seq);   // ingest前にACK（S5）
        RawEvent e{ h.src, h.boot_id, h.seq, b.lane, b.quality, b.t_us, b.t_us_b };
        spool_append(e);
        Serial.printf("[EV] src=SQ%u lane=%u q=%u t=%llu\n",
                      h.src, b.lane, b.quality, (unsigned long long)b.t_us);
      }
    } break;

    case proto::PT_JOIN: {
      if (body_len >= (int)sizeof(proto::JoinBody)) {
        proto::JoinBody jb; memcpy(&jb, body, sizeof(jb));
        forward_join(jb);                      // サーバーへ転送→必要ならJOIN_ACK
      }
    } break;

    case proto::PT_HEARTBEAT: break;           // 死活のみ（TFT表示は最小）
    default: break;
  }
}

// ---- race払い出し（未開始なら） -------------------------------------------
static bool ensure_race() {
  if (s_race_id) return true;
  if (!wifi_up()) return false;
  HTTPClient http;
  http.begin(String(SERVER_BASE) + "/api/timing/races");
  http.addHeader("Content-Type", "application/json");
  if (strlen(TIMING_TOKEN)) http.addHeader("X-Timing-Token", TIMING_TOKEN);
  int code = http.POST(String("{\"target_laps\":3}"));   // 仮。ヒートIDは後段（DA5）
  if (code == 200) {
    String r = http.getString();
    int p = r.indexOf("\"race_id\"");
    if (p >= 0) { int c = r.indexOf(':', p); s_race_id = r.substring(c+1).toInt(); }
  }
  http.end();
  return s_race_id != 0;
}

// ---- スプールをサーバーへPOST（200まで消さない・S8）-----------------------
static void flush_spool() {
  if (!LittleFS.exists(SPOOL)) return;
  if (!ensure_race()) return;

  File f = LittleFS.open(SPOOL, FILE_READ);
  if (!f) return;
  String events = "["; bool first = true; int n = 0;
  while (f.available()) {
    String line = f.readStringUntil('\n'); line.trim();
    if (line.length() == 0) continue;
    if (!first) events += ",";
    events += line; first = false; n++;
  }
  f.close();
  events += "]";
  if (n == 0) return;

  String url = String(SERVER_BASE) + "/api/timing/races/" + s_race_id + "/events";
  HTTPClient http;
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  if (strlen(TIMING_TOKEN)) http.addHeader("X-Timing-Token", TIMING_TOKEN);
  int code = http.POST(String("{\"events\":") + events + "}");
  if (code == 200) {
    LittleFS.remove(SPOOL);                   // 200で初めて空に（冪等・S8）
    Serial.printf("[POST] %d件 送信\n", n);
  } else {
    Serial.printf("[POST] 失敗 code=%d（次回再送）\n", code);
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n=== M4LAPS Gateway GW%d (VE) ===\n", NODE_ID);

  if (!LittleFS.begin(true)) Serial.println("LittleFS mount 失敗");
  Serial.printf("PSRAM=%u（8388608ならOK）\n", (unsigned)ESP.getPsramSize());  // VE受入

  if (!mesh::begin(NODE_ID, ESPNOW_CHANNEL, on_recv)) {
    Serial.println("ESP-NOW init 失敗"); return;
  }
  beam::begin();       // GWもS/Gの3レーンを検出（S/G一体）
  wifi_up();
  Serial.println("稼働開始。SYNC応答・EVENT集約・POSTを行う。");
}

void loop() {
  // GW自身のS/G通過も拾って記録（src=NODE_ID）
  beam::Hit hit;
  while (beam::poll(hit)) {
    static uint32_t self_seq = 0;
    RawEvent e{ (uint8_t)NODE_ID, mesh::boot_id(), ++self_seq,
                hit.lane, hit.quality, hit.t_a_us, hit.t_b_us };
    spool_append(e);
    Serial.printf("[SG] lane=%u q=%u\n", hit.lane, hit.quality);
  }

  static uint32_t last = 0;
  if (millis() - last > 3000) { last = millis(); flush_spool(); }
}
