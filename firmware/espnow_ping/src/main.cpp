// ============================================================================
//  espnow_ping — ESP-NOW 疎通確認ファーム（検証専用・使い捨て）
//  目的: 複数の基板が無線(ESP-NOW)でお互いを認識できるかを実機で確認する。
//        → 設計書 13.3 段階A（JOIN・HEARTBEAT・死活検出）の最小版
//
//  役割は platformio.ini の -D ROLE_xx で切り替える：
//    ROLE_GW=1 … 親。SQからの「いるよ」を受けて一覧表示。
//    ROLE_SQ=1 … 子。自分の番号つきで定期的に「いるよ」を送る。
//                番号は -D MY_NUM=1 / 2 で指定。
//
//  ⚠ 検証用のためチャンネル固定(1)・暗号化なし・ブロードキャスト送信。
//     受信コールバックは旧来型 (const uint8_t* mac, ...) を使用（当環境に合わせる）。
// ============================================================================
#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <string.h>

#ifndef CH
#define CH 1                 // 無線チャンネル（全機材で揃える。検証は1固定）
#endif

static const uint8_t BCAST[6] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};

// 送るデータ：種別('S'=SQ) と 番号
struct Msg { char kind; uint8_t num; };

#if defined(ROLE_GW)
// ---- GW役：受信して「見つけたSQ」を管理 ------------------------------------
struct Seen { bool active; uint8_t num; uint32_t last_ms; };
static Seen g_seen[8];

// ★ 旧来型シグネチャ：第1引数は送信元MAC(const uint8_t*)
static void onRecv(const uint8_t* mac, const uint8_t* data, int len) {
  (void)mac;
  if (len < (int)sizeof(Msg)) return;
  Msg m; memcpy(&m, data, sizeof(m));
  if (m.kind != 'S') return;
  if (m.num < 1 || m.num > 7) return;
  g_seen[m.num].active  = true;
  g_seen[m.num].num     = m.num;
  g_seen[m.num].last_ms = millis();
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== ESP-NOW 疎通確認 : GW役 ===");
  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(CH, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) { Serial.println("ESP-NOW init 失敗"); return; }
  esp_now_register_recv_cb(onRecv);
  Serial.printf("チャンネル=%d で待受け中。SQの電源を入れてください。\n", CH);
}

void loop() {
  static uint32_t t = 0;
  if (millis() - t > 2000) {
    t = millis();
    int n = 0; char buf[64] = {0};
    for (int i = 1; i <= 7; i++) {
      if (g_seen[i].active && (millis() - g_seen[i].last_ms) < 5000) {
        n++;
        char one[8]; snprintf(one, sizeof(one), "SQ%d ", i);
        strncat(buf, one, sizeof(buf) - strlen(buf) - 1);
      }
    }
    if (n > 0) Serial.printf("[GW] SQ %d台と通信中 : %s\n", n, buf);
    else       Serial.println("[GW] まだSQを検出していません…（電源とチャンネルを確認）");
  }
}

#elif defined(ROLE_SQ)
// ---- SQ役：自分の番号を定期送信 ---------------------------------------------
#ifndef MY_NUM
#define MY_NUM 1
#endif

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n=== ESP-NOW 疎通確認 : SQ%d役 ===\n", MY_NUM);
  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(CH, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) { Serial.println("ESP-NOW init 失敗"); return; }
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = CH;
  peer.encrypt = false;
  esp_now_add_peer(&peer);
  Serial.printf("チャンネル=%d で送信開始。\n", CH);
}

void loop() {
  Msg m{ 'S', (uint8_t)MY_NUM };
  esp_now_send(BCAST, (uint8_t*)&m, sizeof(m));
  Serial.printf("[SQ%d] いるよ を送信\n", MY_NUM);
  delay(1000);
}

#else
void setup() { Serial.begin(115200); }
void loop()  { Serial.println("役割(ROLE)未定義。platformio.ini を確認"); delay(2000); }
#endif
