// firmware/gw/src/main.cpp
// ============================================================================
//  M4LAPS ゲートウェイ（GW6／予備GW7）ファーム
//  役割（docs/14 DA1/DA2）：全ゲートの集約点。SYNC親時計。EVENT集約→サーバーPOST。
//    JOIN転送（/api/timing/join）。S/G（自分の3レーン）ビーム検出。
//  ＋スタートシーケンス（docs/14 DA11・docs/12.3 状態機械）：
//    赤ボタン(本体) or リモコンCMD_SIGNAL → 新レース作成(layout_id付き) →
//    赤点灯 → 最低3秒＋ランダム → 緑点灯(green_t_us記録・F1式) → シグナルへCOMMAND
//    灰ボタン or CMD_RESET → IDLEへ戻す（消灯）
//  ⚠ 「レース中か」の意味づけはアプリ（DA1）。GWが持つのは“演出の進行”のみ。
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

// ⚠ 当面はレイアウトIDをファームに固定で持つ（(A)最小案・docs/19.10.6 #1）。
//   将来アプリ→GWでヒートID/レイアウトを動的送信する（DA5・(B)フル案）。
#ifndef LAYOUT_ID
#define LAYOUT_ID 1
#endif
#ifndef TARGET_LAPS
#define TARGET_LAPS 3
#endif

// ---- 本体ボタン（docs/07.4）-----------------------------------------------
static constexpr int PIN_BTN_RED  = 26;   // 赤 SIGNAL（押下LOW・内部プルアップ）
static constexpr int PIN_BTN_GRAY = 27;   // 灰 RESET
static constexpr uint32_t DEBOUNCE_MS   = 20;
static constexpr uint32_t RESET_HOLD_MS = 600;   // RESETは600ms長押し（docs/12 S9）

// ---- スタート演出の状態機械（docs/12.3）-----------------------------------
enum GwState { ST_IDLE, ST_ARMED, ST_GREEN, ST_RACE };
static GwState s_state = ST_IDLE;
static uint32_t s_armed_ms = 0;         // ARMEDに入った時刻
static uint32_t s_red_dur_ms = 0;       // 今回の赤の長さ（3秒＋ランダム）
static uint64_t s_green_t_us = 0;       // 緑を出したGW時刻（F1式・green_t_us）

// ---- 受信EVENTレコード -----------------------------------------------------
struct RawEvent {
  uint8_t src; uint32_t src_boot; uint32_t seq;
  uint8_t lane; uint8_t quality; uint64_t t_us; uint64_t t_us_b;
};

static const char* SPOOL = "/spool.jsonl";
static uint32_t s_race_id = 0;

// ---- WiFi ------------------------------------------------------------------
static bool wifi_up() {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) delay(100);
  return WiFi.status() == WL_CONNECTED;
}

// ---- 新レース作成（layout_id・任意でgreen_t_usを付ける）--------------------
//  green あり → F1式 / green なし → 走行式（docs/14 DA4）。
static uint32_t create_race(bool with_green, uint64_t green_us) {
  if (!wifi_up()) return 0;
  char body[160];
  if (with_green) {
    snprintf(body, sizeof(body),
      "{\"target_laps\":%d,\"layout_id\":%d,\"green_t_us\":%llu}",
      TARGET_LAPS, LAYOUT_ID, (unsigned long long)green_us);
  } else {
    snprintf(body, sizeof(body),
      "{\"target_laps\":%d,\"layout_id\":%d}", TARGET_LAPS, LAYOUT_ID);
  }
  HTTPClient http;
  http.begin(String(SERVER_BASE) + "/api/timing/races");
  http.addHeader("Content-Type", "application/json");
  if (strlen(TIMING_TOKEN)) http.addHeader("X-Timing-Token", TIMING_TOKEN);
  uint32_t rid = 0;
  int code = http.POST((uint8_t*)body, strlen(body));
  if (code == 200) {
    String r = http.getString();
    int p = r.indexOf("\"race_id\"");
    if (p >= 0) { int c = r.indexOf(':', p); rid = r.substring(c+1).toInt(); }
  }
  http.end();
  return rid;
}

// ---- 既存レースに緑時刻を後付け（走行式→F1式・残課題7の解消）--------------
//  race_id を変えずに green_t_us だけPATCHする。POST .../{id}/green。
static bool set_race_green(uint32_t rid, uint64_t green_us) {
  if (!rid || !wifi_up()) return false;
  char body[80];
  snprintf(body, sizeof(body), "{\"green_t_us\":%llu}", (unsigned long long)green_us);
  HTTPClient http;
  http.begin(String(SERVER_BASE) + "/api/timing/races/" + rid + "/green");
  http.addHeader("Content-Type", "application/json");
  if (strlen(TIMING_TOKEN)) http.addHeader("X-Timing-Token", TIMING_TOKEN);
  int code = http.POST((uint8_t*)body, strlen(body));
  http.end();
  return code == 200;
}

// ---- シグナルへ COMMAND（赤/緑/リセット）----------------------------------
static void signal_cmd(uint8_t code) {
  proto::CommandBody c = {};
  c.code = code;
  mesh::send(proto::PT_COMMAND, 10 /*SG10*/, &c, sizeof(c));
}

// ---- スタートシーケンス制御 -----------------------------------------------
//  赤ボタン/CMD_SIGNAL共通の入口（docs/03「本体とリモコンで同じ処理」）。
static void on_signal_pressed() {
  if (s_state != ST_IDLE) return;             // 二度押し無視
  // レースをここで切る（layout_id付き・まだ緑は無いのでgreenなしで作成）
  s_race_id = create_race(/*with_green=*/false, 0);
  // 赤点灯 → 最低3秒＋ランダム（ランダムはGWが決める・DA11）
  s_red_dur_ms = 3000 + (esp_random() % 2000);   // 3.0〜5.0秒
  s_armed_ms   = millis();
  s_state      = ST_ARMED;
  signal_cmd(proto::CMD_RED);
  Serial.printf("[SEQ] ARMED race=%u red=%ums\n", s_race_id, s_red_dur_ms);
}

static void on_reset_pressed() {
  s_state = ST_IDLE;
  s_green_t_us = 0;
  signal_cmd(proto::CMD_RESET);
  Serial.println("[SEQ] RESET -> IDLE");
}

// ARMED経過で緑へ。loopから呼ぶ。
static void tick_sequence() {
  if (s_state == ST_ARMED && (millis() - s_armed_ms) >= s_red_dur_ms) {
    s_green_t_us = tsync::now_gw_us();          // 緑を出した瞬間＝green_t_us
    s_state = ST_GREEN;
    signal_cmd(proto::CMD_GREEN);
    // F1式へ：race_id を変えずに緑時刻だけ後付けする（残課題7を解消）。
    bool ok = set_race_green(s_race_id, s_green_t_us);
    Serial.printf("[SEQ] GREEN t=%llu race=%u green_patch=%s\n",
                  (unsigned long long)s_green_t_us, s_race_id, ok ? "OK" : "NG");
    s_state = ST_RACE;
  }
}

// ---- JOIN転送 --------------------------------------------------------------
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

// ---- EVENTスプール ---------------------------------------------------------
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
                    int body_len, const uint8_t*) {
  switch (h.type) {
    case proto::PT_SYNC_REQ: {
      if (body_len >= (int)sizeof(proto::SyncBody)) {
        proto::SyncBody b; memcpy(&b, body, sizeof(b));
        tsync::gw_reply(h, b);
      }
    } break;

    case proto::PT_EVENT: {
      if (body_len >= (int)sizeof(proto::EventBody)) {
        proto::EventBody b; memcpy(&b, body, sizeof(b));
        mesh::send_ack(proto::PT_EVENT_ACK, h.src, h.seq);   // ingest前ACK（S5）
        RawEvent e{ h.src, h.boot_id, h.seq, b.lane, b.quality, b.t_us, b.t_us_b };
        spool_append(e);
        Serial.printf("[EV] src=SQ%u lane=%u q=%u t=%llu\n",
                      h.src, b.lane, b.quality, (unsigned long long)b.t_us);
      }
    } break;

    case proto::PT_JOIN: {
      if (body_len >= (int)sizeof(proto::JoinBody)) {
        proto::JoinBody jb; memcpy(&jb, body, sizeof(jb));
        forward_join(jb);
      }
    } break;

    case proto::PT_COMMAND: {
      // リモコンからのCOMMAND。本体ボタンと同じ処理を呼ぶ（docs/03）。
      if (body_len >= (int)sizeof(proto::CommandBody)) {
        proto::CommandBody c; memcpy(&c, body, sizeof(c));
        if (c.code == proto::CMD_SIGNAL) on_signal_pressed();
        else if (c.code == proto::CMD_RESET) on_reset_pressed();
        // ACK（届いた確認・docs/12 COMMAND_ACK）
        mesh::send_ack(proto::PT_COMMAND_ACK, h.src, h.seq);
      }
    } break;

    case proto::PT_HEARTBEAT: break;
    default: break;
  }
}

// ---- レース払い出し（EVENTを送る先が無ければ暫定で1つ作る）----------------
//  スタートシーケンス未使用でもS/G通過だけ記録したい場合の保険。
static bool ensure_race() {
  if (s_race_id) return true;
  s_race_id = create_race(/*with_green=*/false, 0);
  return s_race_id != 0;
}

// ---- スプールPOST（200まで消さない・S8）-----------------------------------
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
  if (code == 200) { LittleFS.remove(SPOOL); Serial.printf("[POST] %d件 送信\n", n); }
  else             { Serial.printf("[POST] 失敗 code=%d\n", code); }
  http.end();
}

// ---- 本体ボタン読み取り ----------------------------------------------------
static void tick_buttons() {
  // 赤：押下エッジ（20ms）
  static bool red_last = false; static uint32_t red_t = 0;
  bool red_now = (digitalRead(PIN_BTN_RED) == LOW);
  if (red_now != red_last && (millis() - red_t) > DEBOUNCE_MS) {
    red_t = millis(); red_last = red_now;
    if (red_now) on_signal_pressed();
  }
  // 灰：600ms長押しでRESET（docs/12 S9）
  static uint32_t gray_down = 0; static bool gray_fired = false;
  bool gray_now = (digitalRead(PIN_BTN_GRAY) == LOW);
  if (gray_now) {
    if (gray_down == 0) { gray_down = millis(); gray_fired = false; }
    else if (!gray_fired && (millis() - gray_down) >= RESET_HOLD_MS) {
      gray_fired = true; on_reset_pressed();
    }
  } else { gray_down = 0; }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n=== M4LAPS Gateway GW%d (VE) ===\n", NODE_ID);
  pinMode(PIN_BTN_RED,  INPUT_PULLUP);
  pinMode(PIN_BTN_GRAY, INPUT_PULLUP);
  if (!LittleFS.begin(true)) Serial.println("LittleFS mount 失敗");
  Serial.printf("PSRAM=%u（8388608ならOK）\n", (unsigned)ESP.getPsramSize());
  if (!mesh::begin(NODE_ID, ESPNOW_CHANNEL, on_recv)) {
    Serial.println("ESP-NOW init 失敗"); return;
  }
  beam::begin();
  wifi_up();
  Serial.println("稼働開始。赤=スタート / 灰長押し=RESET。");
}

void loop() {
  tick_buttons();
  tick_sequence();

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
