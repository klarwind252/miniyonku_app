// firmware/signal/src/main.cpp
// ============================================================================
//  M4LAPS シグナル（SG10/SG11・XIAO ESP32C3）ファーム
//  役割（docs/14 DA12）：2つの顔。
//    (1) GW連動：GWの COMMAND(CMD_RED / CMD_GREEN) 通りに赤/緑を点灯（計測に使える）
//    (2) 単独 ：本体の赤ボタンで即・緑を点灯（赤なし・ランダムなし・連動なし）
//  ⚠ ランダム時間や3秒マージンはGWが持つ。シグナル機は指示通り光るだけ（DA11）。
//  ピン（docs/07.6）：D0(GPIO2)=赤灯 / D3=緑灯 / ボタン=別ピン（下の注記）
//  ⚠ LEDは3W・約590mA。配線ミスのまま通電で素子が飛ぶ。まずゲート電圧のみで確認（docs/13.2）。
// ============================================================================
#include <Arduino.h>
#include <WiFi.h>
#include "protocol.h"
#include "espnow_link.h"
#include "chfollow.h"

#ifndef NODE_ID
#define NODE_ID 10
#endif
#ifndef ESPNOW_CHANNEL
#define ESPNOW_CHANNEL 1
#endif

static constexpr uint8_t FW_MAJOR = 0;
static constexpr uint8_t FW_MINOR = 1;

static constexpr int PIN_RED_LED   = 2;   // D0(GPIO2) 赤灯（→MOSFETゲート）
static constexpr int PIN_GREEN_LED = 3;   // D3        緑灯（→別MOSFET）
static constexpr int PIN_BUTTON    = 4;   // ⚠ docs/07.6のD1(GPIO3)は緑灯と衝突表記。実機で確定。
static constexpr uint32_t DEBOUNCE_MS = 20;

static bool s_assigned = false;

static void set_red(bool on)   { digitalWrite(PIN_RED_LED,   on ? HIGH : LOW); }
static void set_green(bool on) { digitalWrite(PIN_GREEN_LED, on ? HIGH : LOW); }
static void all_off()          { set_red(false); set_green(false); }

struct Button {
  int pin; bool last; uint32_t t;
  bool pressed() {
    bool now = (digitalRead(pin) == LOW);
    uint32_t nowm = millis();
    if (now != last && (nowm - t) > DEBOUNCE_MS) {
      t = nowm; last = now;
      return now;
    }
    return false;
  }
};
static Button s_btn { PIN_BUTTON, false, 0 };

static void on_recv(const proto::PktHeader& h, const uint8_t* body,
                    int body_len, const uint8_t*) {
  switch (h.type) {
    case proto::PT_JOIN_ACK: {
      if (body_len >= (int)sizeof(proto::JoinAckBody)) {
        proto::JoinAckBody b; memcpy(&b, body, sizeof(b));
        if (b.node_id == NODE_ID) s_assigned = true;
        if (b.channel >= 1 && b.channel <= 13) mesh::set_channel(b.channel);  // 状態B：GWのchへ
      }
    } break;
    case proto::PT_COMMAND: {
      if (body_len >= (int)sizeof(proto::CommandBody)) {
        proto::CommandBody c; memcpy(&c, body, sizeof(c));
        switch (c.code) {                    // GWの指示通りに光る（DA11）
          case proto::CMD_RED:   set_red(true);  set_green(false); Serial.println("[CMD] RED");   break;
          case proto::CMD_GREEN: set_red(false); set_green(true);  Serial.println("[CMD] GREEN"); break;
          case proto::CMD_RESET: all_off();                        Serial.println("[CMD] RESET"); break;
          default: break;
        }
      }
    } break;
    default: break;
  }
}

static void send_join() {
  proto::JoinBody jb = {};
  WiFi.macAddress(jb.mac);
  jb.kind = proto::KIND_SG;
  jb.fw_major = FW_MAJOR; jb.fw_minor = FW_MINOR;
  jb.nvs_node_id = NODE_ID;
  mesh::send(proto::PT_JOIN, 6, &jb, sizeof(jb));
}

static void tick_presence() {
  static uint32_t last = 0;
  uint32_t nowm = millis();
  uint32_t interval = s_assigned ? 3000 : 1000;
  if (nowm - last < interval) return;
  last = nowm;
  if (!s_assigned) send_join();
  else             mesh::send(proto::PT_HEARTBEAT, 6, nullptr, 0);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n=== M4LAPS Signal SG%d ===\n", NODE_ID);
  pinMode(PIN_RED_LED,   OUTPUT);
  pinMode(PIN_GREEN_LED, OUTPUT);
  pinMode(PIN_BUTTON,    INPUT_PULLUP);
  all_off();
  uint8_t ch0 = chfollow::initial_channel(ESPNOW_CHANNEL);
  if (!mesh::begin(NODE_ID, ch0, on_recv)) {
    Serial.println("ESP-NOW init 失敗"); return;
  }
  Serial.printf("稼働開始（ch=%d・GWへ自動追従）。GW連動 or 単独ボタンで緑。\n", ch0);
}

void loop() {
  chfollow::tick(send_join);   // GWのchへ追従（在圏切れで1..13走査）
  tick_presence();
  if (s_btn.pressed()) {                      // (2) 単独：即・緑（DA12）
    set_red(false); set_green(true);
    Serial.println("[BTN] 単独：緑 即点灯");
  }
}
