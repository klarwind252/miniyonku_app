// firmware/remote/src/main.cpp
// ============================================================================
//  M4LAPS リモコン（RC8/RC9・XIAO ESP32C3）ファーム
//  役割（docs/14 DA11）：赤=SIGNAL（GWへ「スタートシーケンス開始」）/ 灰=RESET。
//    押下 → ESP-NOWで COMMAND を3連射（取りこぼし対策・docs/12 S10）→ GWがACK。
//  GWは受信時、本体ボタンと同じ処理を呼ぶ（別実装にしない・docs/03）。
//  チャタリング除去はソフト20ms。常時起動（スリープしない）。
// ============================================================================
#include <Arduino.h>
#include <WiFi.h>
#include "protocol.h"
#include "espnow_link.h"
#include "chfollow.h"

#ifndef NODE_ID
#define NODE_ID 8
#endif
#ifndef ESPNOW_CHANNEL
#define ESPNOW_CHANNEL 1
#endif

static constexpr uint8_t FW_MAJOR = 0;
static constexpr uint8_t FW_MINOR = 1;

static constexpr int PIN_RED  = 3;   // D1(GPIO3) 赤タクト SIGNAL
static constexpr int PIN_GRAY = 4;   // D2(GPIO4) 灰タクト RESET
static constexpr uint32_t DEBOUNCE_MS = 20;

static bool s_assigned = false;

struct Button {
  int pin; bool last; uint32_t t;
  bool pressed() {
    bool now = (digitalRead(pin) == LOW);   // 押下=LOW（内部プルアップ）
    uint32_t nowm = millis();
    if (now != last && (nowm - t) > DEBOUNCE_MS) {
      t = nowm; last = now;
      return now;
    }
    return false;
  }
};
static Button s_red  { PIN_RED,  false, 0 };
static Button s_gray { PIN_GRAY, false, 0 };

static void send_command(uint8_t code) {
  proto::CommandBody c = {};
  c.code = code;
  for (int i = 0; i < 3; i++) {              // 3連射（docs/12 S10）
    mesh::send(proto::PT_COMMAND, 6 /*GW*/, &c, sizeof(c));
    delay(3);
  }
}

static void on_recv(const proto::PktHeader& h, const uint8_t* body,
                    int body_len, const uint8_t*) {
  if (h.type == proto::PT_JOIN_ACK && body_len >= (int)sizeof(proto::JoinAckBody)) {
    proto::JoinAckBody b; memcpy(&b, body, sizeof(b));
    if (b.node_id == NODE_ID) s_assigned = true;
    if (b.channel >= 1 && b.channel <= 13) mesh::set_channel(b.channel);  // 状態B：GWのchへ
  }
}

static void send_join() {
  proto::JoinBody jb = {};
  WiFi.macAddress(jb.mac);
  jb.kind = proto::KIND_RC;
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
  Serial.printf("\n=== M4LAPS Remote RC%d ===\n", NODE_ID);
  pinMode(PIN_RED,  INPUT_PULLUP);
  pinMode(PIN_GRAY, INPUT_PULLUP);
  uint8_t ch0 = chfollow::initial_channel(ESPNOW_CHANNEL);
  if (!mesh::begin(NODE_ID, ch0, on_recv)) {
    Serial.println("ESP-NOW init 失敗"); return;
  }
  Serial.printf("稼働開始（ch=%d・GWへ自動追従）。赤=SIGNAL / 灰=RESET。\n", ch0);
}

void loop() {
  chfollow::tick(send_join);   // GWのchへ追従（在圏切れで1..13走査）
  tick_presence();
  if (s_red.pressed())  { send_command(proto::CMD_SIGNAL); Serial.println("[BTN] SIGNAL"); }
  if (s_gray.pressed()) { send_command(proto::CMD_RESET);  Serial.println("[BTN] RESET");  }
}
