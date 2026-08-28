// firmware/signal/src/main.cpp
// ============================================================================
//  M4LAPS シグナル（SG10/SG11・XIAO ESP32C3）ファーム
//  役割（docs/14 DA12）：2つの顔。
//    (1) GW連動：GWの COMMAND(CMD_RED / CMD_GREEN) 通りに赤/緑を点灯（計測に使える）
//    (2) 単独 ：本体の赤ボタンで即・緑を点灯（赤なし・ランダムなし・連動なし）
//  ⚠ ランダム時間や3秒マージンはGWが持つ。シグナル機は指示通り光るだけ（DA11）。
//  ピン（docs/12 §12.5 確定・XIAO C3: D0=GPIO2/D1=GPIO3/D2=GPIO4/D3=GPIO5）：
//    赤灯=D0(GPIO2) / 緑灯=D3(GPIO5) / 手元ボタン=D1(GPIO3) / ブザー=D2(GPIO4)
//  緑点灯は2秒で自動消灯（GREEN_HOLD_MS・DC30）。GW連動/単独の両経路で共通。
//  GO時は緑を先に点け、0.05秒後に赤を消す＝赤緑オーバーラップ（GREEN_LEAD_MS・DC32）。暗転の隙間なし。
//  ブザー（他励式SPT15）は緑と同時に1秒「ブー」（DC27）。1200Hz共振キャリアを約83Hzで
//    ブツ切り(AM)して低い唸り＝カーレースのホーン風。音量は共振点のまま最大。GW連動/単独両方。
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

// ── ピン割当（docs/12 §12.5 確定）──────────────────────────────────────
//  ★T-6修正（2026-08-25）：緑灯は D3=GPIO5。旧コードは 3(=GPIO3=D1) を指しており
//    [CMD] GREEN を受けても緑MOSFET(GPIO5側)がHIGHにならず点灯しなかった＝T-6の主原因。
static constexpr int PIN_RED_LED   = 2;   // D0(GPIO2) 赤灯（→MOSFETゲート）
static constexpr int PIN_GREEN_LED = 5;   // D3(GPIO5) 緑灯（→別MOSFET）★旧3(GPIO3)は誤り
static constexpr int PIN_BUTTON    = 3;   // D1(GPIO3) 手元ボタン ★旧4(GPIO4)はブザー用D2と衝突
static constexpr int PIN_BUZZER    = 4;   // D2(GPIO4) ブザー（自励式UGCM1205XP→1kΩ→SS8050→ブザー）
static constexpr uint32_t DEBOUNCE_MS = 20;

// ── 緑オート消灯（DC25・非ブロッキング＝GWブザー buzzer_tick 方式）───────
static constexpr uint32_t GREEN_HOLD_MS = 2000;  // 緑点灯の保持時間（最終確定：2秒・DC30）
static uint32_t s_green_off_ms = 0;              // 0=予約なし。緑ONで millis()+GREEN_HOLD_MS

// ── 赤→緑オーバーラップ（DC32・非ブロッキング）────────────────────────
//  F1の消灯式回避策として「暗転の隙間」を作らない。GO時に緑を先に点け、GREEN_LEAD_MS 後に
//  赤を消す＝赤と緑が GREEN_LEAD_MS だけ同時点灯（重なり）してから赤が落ちる。
//  ⚠ GW連動時のみ意味を持つ（直前にCMD_REDで赤が点灯しているため）。単独ボタン運用は
//    元から赤なし（DA12）なので、この赤消灯予約は「既に消えている赤」への無害な空振り。
//  ⚠ 計測非干渉：GW連動の計測起点はGW側 green_t_us（DA4/DA13）。SGの赤緑は表示演出のみ。
static constexpr uint32_t GREEN_LEAD_MS = 50;    // 緑が先行して点く時間＝重なり（0.05秒。0.1秒にするなら100）
static uint32_t s_red_off_ms = 0;                // 0=予約なし。緑ONで millis()+GREEN_LEAD_MS

// ── SGブザー（DC30/DC31・自励式UGCM1205XP・緑と同時に2秒・非ブロッキング）──
//  ★2026-08-26：他励式SPT15は5V駆動では音量不足だったため、手持ちの自励式UGCM1205XP
//    （定格5V・5Vで85dB・共振2.3kHz・発振回路内蔵）を採用（DC31）。自励式ゆえ電圧をかける
//    だけで鳴る＝tone/LEDC不要。GW側ブザー109800と同じ digitalWrite の HIGH/LOW のみ。
//  ※将来「低いブー」にしたい場合は SPT15＋12V昇圧(107608) に差替（保留中）。その時は
//    ledcWriteTone 版へ戻す。音程固定(2.3kHz「ピー」)は自励式の制約。
//  配線：GPIO4 →1kΩ→ SS8050(B)／E→GND／C→ブザー(−)／ブザー(＋)→5V（1N4148並列・帯=5V側）。
static constexpr uint32_t BUZZER_MS = 500;    // 鳴動長（★仮：0.5秒で試験中。緑は2秒維持。最終確定でDC更新）
static uint32_t s_buzz_off_ms = 0;            // 0=非鳴動。0以外＝この millis で停止

static bool s_assigned = false;

static void set_red(bool on)   { digitalWrite(PIN_RED_LED,   on ? HIGH : LOW); }
static void set_green(bool on) { digitalWrite(PIN_GREEN_LED, on ? HIGH : LOW); }

// ブザー：鳴動開始（2秒間、HIGHで鳴らし続ける＝自励式）
static void buzzer_on() {
  digitalWrite(PIN_BUZZER, HIGH);       // 自励式：HIGHで鳴る
  s_buzz_off_ms = millis() + BUZZER_MS; // 2秒後に停止予約
}
// ブザー：非ブロッキング停止（毎loopで呼ぶ）。2秒経過でLOWにするだけ。
static void buzzer_tick() {
  if (s_buzz_off_ms && (int32_t)(millis() - s_buzz_off_ms) >= 0) {
    digitalWrite(PIN_BUZZER, LOW);
    s_buzz_off_ms = 0;
  }
}

static void all_off() {
  set_red(false); set_green(false);
  s_green_off_ms = 0;                              // 保留中の緑消灯予約も破棄
  s_red_off_ms   = 0;                              // 保留中の赤消灯予約も破棄（DC32）
  digitalWrite(PIN_BUZZER, LOW); s_buzz_off_ms = 0;// ブザーも即停止（RESET）
}

// 緑を点ける唯一の入口（GW連動 CMD_GREEN／単独ボタン の両方からこれを呼ぶ）
static void green_on() {
  set_green(true);                               // ★まず緑を点ける（赤はまだ消さない＝重なりを作る・DC32）
  s_red_off_ms   = millis() + GREEN_LEAD_MS;     // GREEN_LEAD_MS(0.05秒)後に赤を消す（GW連動時のみ実効）
  s_green_off_ms = millis() + GREEN_HOLD_MS;     // 2秒後の自動消灯を予約（DC30）
  buzzer_on();                                   // 緑と同時にブザー（★仮0.5秒・BUZZER_MS）自励式UGCM1205
}
// 非ブロッキング消灯：毎loopで呼ぶ。delay()を使わないのでESP-NOW受信/在圏を止めない
static void green_tick() {
  if (s_green_off_ms && (int32_t)(millis() - s_green_off_ms) >= 0) {
    set_green(false);
    s_green_off_ms = 0;
  }
}
// 赤の遅延消灯（DC32・非ブロッキング）：緑ONから GREEN_LEAD_MS 経過で赤を落とす。毎loopで呼ぶ。
static void red_tick() {
  if (s_red_off_ms && (int32_t)(millis() - s_red_off_ms) >= 0) {
    set_red(false);
    s_red_off_ms = 0;
  }
}

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
          case proto::CMD_RED:
            set_red(true); set_green(false);
            s_green_off_ms = 0; s_red_off_ms = 0;                  // 赤中は緑予約/赤消灯予約を消す（DC32）
            Serial.println("[CMD] RED");   break;
          case proto::CMD_GREEN:
            green_on();                                            // 緑ON＋1秒後自動消灯（DC25）
            Serial.println("[CMD] GREEN"); break;
          case proto::CMD_RESET:
            all_off();
            Serial.println("[CMD] RESET"); break;
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
  pinMode(PIN_BUZZER,    OUTPUT); digitalWrite(PIN_BUZZER, LOW);  // ブザー（自励式UGCM1205）初期LOW
  all_off();
  uint8_t ch0 = chfollow::initial_channel(ESPNOW_CHANNEL);
  if (!mesh::begin(NODE_ID, ch0, on_recv)) {
    Serial.println("ESP-NOW init 失敗"); return;
  }
  Serial.printf("稼働開始（ch=%d・GWへ自動追従）。GW連動 or 単独ボタンで緑（1秒）。\n", ch0);
}

void loop() {
  chfollow::tick(send_join);   // GWのchへ追従（在圏切れで1..13走査）
  tick_presence();
  green_tick();                // 緑の非ブロッキング自動消灯（DC25）
  red_tick();                  // 赤の遅延消灯（緑先行・重なり0.05秒／DC32）
  buzzer_tick();               // ブザーの非ブロッキング停止（DC27）
  if (s_btn.pressed()) {                      // (2) 単独：即・緑（DA12）
    green_on();                               // 緑ON＋1秒後自動消灯（DC25）
    Serial.println("[BTN] 単独：緑 即点灯（1秒）");
  }
}
