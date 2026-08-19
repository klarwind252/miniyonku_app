// firmware/remote/src/main.cpp
// ============================================================================
//  M4LAPS リモコン（RC8/RC9・XIAO ESP32C3）ファーム —— ディープスリープ版
//  役割（docs/14 DA11）：赤=SIGNAL / 灰=RESET。押下でGWへ COMMAND を投げる。
//
//  【20260819 ①スリープ化】ボタン(GPIO3/4)のLOWで起床。待機~5µA。起床＝リブート。
//  【20260819 ②二重発火防止】1押下＝一意nonce(CommandBody.arg)。正chで在圏確認→送信。
//  【20260819 ③改善】
//     ・スタックボタンガード：解放されない起床が連続したら、GPIO起床を無効化して
//       タイマー起床のクールダウンへ逃がし、固着/ショートによる枯渇ループを断つ。
//     ・ACK駆動の同一nonce再送：送信後COMMAND_ACKを待ち、来なければ同一nonceで1回だけ
//       再送（GWがnonceで重複を殺すため“二度発火せず”配送率だけ上げられる）。
//
//  ⚠ 従来版（常時起動）に戻すときは USE_DEEP_SLEEP を 0 にしてビルド。
//  ⚠ esp_deep_sleep_enable_gpio_wakeup / esp_sleep_get_gpio_wakeup_status は
//     arduino-esp32 3.x(IDF5)・C3 で有効。古いコアでは名称が違う場合あり。
// ============================================================================
#include <Arduino.h>
#include <WiFi.h>
#include "esp_sleep.h"
#include "driver/gpio.h"
#include "protocol.h"
#include "espnow_link.h"
#include "chfollow.h"

#ifndef NODE_ID
#define NODE_ID 8
#endif
#ifndef ESPNOW_CHANNEL
#define ESPNOW_CHANNEL 1
#endif

// 1=ディープスリープ運用（内蔵LiPo） / 0=従来の常時起動（USB給電）
#ifndef USE_DEEP_SLEEP
#define USE_DEEP_SLEEP 1
#endif

static constexpr uint8_t FW_MAJOR = 0;
static constexpr uint8_t FW_MINOR = 4;      // 3→4：スタックガード＋ACK再送

static constexpr int PIN_RED  = 3;   // D1(GPIO3) 赤タクト SIGNAL
static constexpr int PIN_GRAY = 4;   // D2(GPIO4) 灰タクト RESET
static constexpr uint32_t DEBOUNCE_MS = 20;

// --- 起床セッションのタイミング ---
static constexpr uint32_t RELEASE_TIMEOUT_MS = 1500;  // ボタン解放待ちの上限
static constexpr uint32_t ACK_WAIT_MS        = 70;    // COMMAND_ACK待ち（ESP-NOWは数ms）
// --- スタックボタンガード ---
static constexpr uint8_t  STUCK_STREAK_MAX   = 3;     // 未解放起床がこの回数連続でクールダウンへ
static constexpr uint32_t STUCK_COOLDOWN_S   = 300;   // クールダウン（GPIO起床を無効化して眠る）

static bool s_assigned = false;
static volatile bool s_cmd_acked = false;   // COMMAND_ACK受信フラグ（recvコールバックが立てる）

// ---- COMMAND 3連射（同一 nonce を全弾に載せる。ACKは上位で待つ）------------
static void send_command(uint8_t code, uint32_t nonce = 0) {
  proto::CommandBody c = {};
  c.code = code;
  c.arg  = nonce;                            // ★押下ごとに一意（GW側で重複排除の鍵）
  for (int i = 0; i < 3; i++) {              // 3連射（取りこぼし対策・docs/12 S10）
    mesh::send(proto::PT_COMMAND, 6 /*GW*/, &c, sizeof(c));
    delay(3);
  }
}

// ---- 受信 -------------------------------------------------------------------
static void on_recv(const proto::PktHeader& h, const uint8_t* body,
                    int body_len, const uint8_t*) {
  if (h.type == proto::PT_COMMAND_ACK) {     // GWがCOMMANDを受領した確認
    s_cmd_acked = true;
    return;
  }
  if (h.type == proto::PT_JOIN_ACK && body_len >= (int)sizeof(proto::JoinAckBody)) {
    proto::JoinAckBody b; memcpy(&b, body, sizeof(b));
    if (b.node_id == NODE_ID) s_assigned = true;
    if (b.channel >= 1 && b.channel <= 13) mesh::set_channel(b.channel);  // GWのchへ
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

static inline bool in_contact() {
  uint32_t last = mesh::last_gw_rx_ms();
  return last != 0 && (millis() - last) < chfollow::SWEEP_AFTER_MS;
}

#if USE_DEEP_SLEEP
// 起床をまたいで保持（RTCメモリ）：未解放起床の連続回数（スタック検出）
RTC_DATA_ATTR static uint32_t s_stuck_streak = 0;

// COMMAND_ACK を最大 ms 待つ（アクションはしない・眠る前の確認だけ）
static bool wait_ack(uint32_t ms) {
  uint32_t t = millis();
  while (millis() - t < ms) { if (s_cmd_acked) return true; delay(2); }
  return s_cmd_acked;
}

// 正chで在圏を確定する。現ch(=前回学習ch)→ダメなら1..13を能動スキャン。
static bool acquire_contact(uint8_t start_ch) {
  const uint32_t PER_CH_MS = 120;
  const uint8_t  MAX_HOPS  = 14;
  uint8_t ch = start_ch;
  for (uint8_t hop = 0; hop < MAX_HOPS; hop++) {
    mesh::set_channel(ch);
    send_join();                    // GWは JOIN受信で即HEARTBEATを撒き返す
    uint32_t t = millis();
    while (millis() - t < PER_CH_MS) {
      if (in_contact()) return true;
      delay(3);
    }
    ch = (uint8_t)((ch % 13) + 1);
  }
  return in_contact();
}

// 起床要因のボタンを返す（quickタップで既に離されていても wake status で判定）
static uint8_t woke_by_command() {
  if (esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_GPIO) return 0;  // 冷起動/タイマー
  uint64_t st = esp_sleep_get_gpio_wakeup_status();
  bool red  = st & (1ULL << PIN_RED);
  bool gray = st & (1ULL << PIN_GRAY);
  if (!red && !gray) {
    red  = (digitalRead(PIN_RED)  == LOW);
    gray = (digitalRead(PIN_GRAY) == LOW);
  }
  if (red)  return proto::CMD_SIGNAL;  // 両押しは赤（SIGNAL）優先
  if (gray) return proto::CMD_RESET;
  return 0;
}

static void arm_and_sleep() {
  // 解放待ち（HIGH復帰）。LOWのまま眠るとwake-on-LOWで即再起床する。
  uint32_t t0 = millis();
  while (millis() - t0 < RELEASE_TIMEOUT_MS) {
    if (digitalRead(PIN_RED) == HIGH && digitalRead(PIN_GRAY) == HIGH) break;
    delay(10);
  }
  delay(30);
  bool still_low = (digitalRead(PIN_RED) == LOW || digitalRead(PIN_GRAY) == LOW);

  WiFi.mode(WIFI_OFF);
  // 入力＋内部プルアップ保持
  gpio_set_direction((gpio_num_t)PIN_RED,  GPIO_MODE_INPUT);
  gpio_set_direction((gpio_num_t)PIN_GRAY, GPIO_MODE_INPUT);
  gpio_pullup_en((gpio_num_t)PIN_RED);   gpio_pulldown_dis((gpio_num_t)PIN_RED);
  gpio_pullup_en((gpio_num_t)PIN_GRAY);  gpio_pulldown_dis((gpio_num_t)PIN_GRAY);

  if (still_low) {
    // ★スタックガード：解放されない起床が連続 → 固着/ショート疑い。
    //   GPIO起床のまま眠ると即再起床の枯渇ループになるため、規定回数でタイマー起床へ逃がす。
    s_stuck_streak++;
    if (s_stuck_streak >= STUCK_STREAK_MAX) {
      Serial.printf("[SLEEP] stuck? streak=%lu → timer cooldown %lus（GPIO起床は無効化）\n",
                    (unsigned long)s_stuck_streak, (unsigned long)STUCK_COOLDOWN_S);
      Serial.flush();
      esp_sleep_enable_timer_wakeup((uint64_t)STUCK_COOLDOWN_S * 1000000ULL);
      esp_deep_sleep_start();          // GPIO起床は敢えて設定しない＝固着でも起きない
    }
  } else {
    s_stuck_streak = 0;                // 正常解放でリセット
  }

  esp_deep_sleep_enable_gpio_wakeup((1ULL << PIN_RED) | (1ULL << PIN_GRAY),
                                    ESP_GPIO_WAKEUP_GPIO_LOW);
  Serial.println("[SLEEP] deep sleep（ボタンで起床）");
  Serial.flush();
  esp_deep_sleep_start();
}

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);   // ホスト未読でもブロックしない（XIAO CDCハング対策）
  pinMode(PIN_RED,  INPUT_PULLUP);
  pinMode(PIN_GRAY, INPUT_PULLUP);

  uint8_t cmd = woke_by_command();   // 冷起動/タイマーなら0
  Serial.printf("\n=== M4LAPS Remote RC%d (deep-sleep fw) cmd=%u streak=%lu ===\n",
                NODE_ID, cmd, (unsigned long)s_stuck_streak);

  uint8_t ch0 = chfollow::initial_channel(ESPNOW_CHANNEL);
  if (!mesh::begin(NODE_ID, ch0, on_recv)) {
    Serial.println("ESP-NOW init 失敗→スリープ");
    arm_and_sleep();
  }
  delay(15);

  // 押下ごとに一意 nonce（0は使わない＝旧互換の“nonce無し”と区別）
  uint32_t nonce = 0;
  if (cmd) { nonce = esp_random(); if (nonce == 0) nonce = 1; }

  // ★送る前に正chで在圏を確定（別ch再送を作らない）
  bool contact = acquire_contact(ch0);
  Serial.printf("[LINK] contact=%d ch=%u\n", contact ? 1 : 0, mesh::channel());

  if (cmd) {
    // 1発目
    s_cmd_acked = false;
    send_command(cmd, nonce);
    bool ok = wait_ack(ACK_WAIT_MS);
    // ★ACKが来なければ“同一nonce”で1回だけ再送（GWがnonceで重複を殺す＝二度発火なし）
    if (!ok) {
      send_command(cmd, nonce);
      ok = wait_ack(ACK_WAIT_MS);
    }
    Serial.printf("[BTN] %s nonce=%08x ack=%d\n",
                  cmd == proto::CMD_SIGNAL ? "SIGNAL" : "RESET", nonce, ok ? 1 : 0);
  }

  arm_and_sleep();
}

void loop() { /* 使わない（setupで完結して眠る） */ }

#else  // ===== 従来の常時起動版（USB給電・スリープしない）=====================
struct Button {
  int pin; bool last; uint32_t t;
  bool pressed() {
    bool now = (digitalRead(pin) == LOW);
    uint32_t nowm = millis();
    if (now != last && (nowm - t) > DEBOUNCE_MS) { t = nowm; last = now; return now; }
    return false;
  }
};
static Button s_red  { PIN_RED,  false, 0 };
static Button s_gray { PIN_GRAY, false, 0 };

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
  if (!mesh::begin(NODE_ID, ch0, on_recv)) { Serial.println("ESP-NOW init 失敗"); return; }
  Serial.printf("稼働開始（ch=%d・GWへ自動追従）。赤=SIGNAL / 灰=RESET。\n", ch0);
}

void loop() {
  chfollow::tick(send_join);
  tick_presence();
  if (s_red.pressed())  { uint32_t n = esp_random(); if(!n)n=1;
                          send_command(proto::CMD_SIGNAL, n); Serial.println("[BTN] SIGNAL"); }
  if (s_gray.pressed()) { uint32_t n = esp_random(); if(!n)n=1;
                          send_command(proto::CMD_RESET,  n); Serial.println("[BTN] RESET");  }
}
#endif
