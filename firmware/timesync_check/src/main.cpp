// ============================================================================
//  timesync_check — 時刻同期 確認ファーム（検証専用・使い捨て）
//  目的: GWを親時計として、SQが自分の時計とのズレ(offset)を測って合わせる。
//        → 設計書 13.3 段階B（SYNC・オフセット収束・RTT分布）の最小版
//
//  役割は platformio.ini の -D ROLE_xx で切替：
//    ROLE_GW=1 … 親時計。SYNC要求に自分のマイクロ秒時刻を返す。
//    ROLE_SQ=1 … 子。定期的にSYNCを投げ、RTTとoffsetを計算・表示。番号 -D MY_NUM。
//
//  ⚠ 検証用：チャンネル固定(1)・暗号化なし・ブロードキャスト。
//     方式は最小RTT選抜（一番速い往復のときの推定offsetを採用）。
// ============================================================================
#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <string.h>
#include "esp_timer.h"

#ifndef CH
#define CH 1
#endif
static const uint8_t BCAST[6] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};

// SYNCパケット
//  kind 'Q' = SQ->GW 要求（t_sq_send を載せる）
//  kind 'R' = GW->SQ 応答（元の t_sq_send をそのまま返し、t_gw を追加）
struct Sync {
  char     kind;
  uint8_t  num;       // SQ番号（応答が自分宛てか判別）
  uint64_t t_sq_send; // SQが送った瞬間のSQ時刻
  uint64_t t_gw;      // GWが応答した瞬間のGW時刻
};

static inline uint64_t now_us() { return (uint64_t)esp_timer_get_time(); }

#if defined(ROLE_GW)
// ---- GW役：SYNC要求に即応答（自分の時刻を載せる） --------------------------
static void onRecv(const uint8_t* mac, const uint8_t* data, int len) {
  if (len < (int)sizeof(Sync)) return;
  Sync s; memcpy(&s, data, sizeof(s));
  if (s.kind != 'Q') return;
  s.kind = 'R';
  s.t_gw = now_us();
  esp_now_send(BCAST, (uint8_t*)&s, sizeof(s));
}

void setup() {
  Serial.begin(115200); delay(300);
  Serial.println("\n=== 時刻同期 確認 : GW役（親時計）===");
  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(CH, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) { Serial.println("ESP-NOW init 失敗"); return; }
  esp_now_peer_info_t peer = {}; memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = CH; peer.encrypt = false; esp_now_add_peer(&peer);
  esp_now_register_recv_cb(onRecv);
  Serial.printf("チャンネル=%d で応答待ち。SQが同期を開始します。\n", CH);
}
void loop() {
  static uint32_t t = 0;
  if (millis() - t > 3000) { t = millis(); Serial.println("[GW] 親時計 稼働中"); }
}

#elif defined(ROLE_SQ)
// ---- SQ役：SYNCを投げてRTTとoffsetを計算 -----------------------------------
#ifndef MY_NUM
#define MY_NUM 1
#endif
static uint64_t g_best_rtt = 0xFFFFFFFFFFFFFFFFULL;
static int64_t  g_best_off = 0;
static uint32_t g_samples  = 0;

static void onRecv(const uint8_t* mac, const uint8_t* data, int len) {
  if (len < (int)sizeof(Sync)) return;
  Sync s; memcpy(&s, data, sizeof(s));
  if (s.kind != 'R' || s.num != MY_NUM) return;
  uint64_t t_recv = now_us();
  uint64_t rtt    = t_recv - s.t_sq_send;
  int64_t  off    = (int64_t)s.t_gw - (int64_t)(s.t_sq_send + rtt / 2);
  g_samples++;
  if (rtt < g_best_rtt) { g_best_rtt = rtt; g_best_off = off; }
  Serial.printf("[SQ%d] RTT=%lu us  offset=%ld us   (最小RTT=%lu us / offset=%ld us / %lu回)\n",
                MY_NUM, (unsigned long)rtt, (long)off,
                (unsigned long)g_best_rtt, (long)g_best_off, (unsigned long)g_samples);
}

void setup() {
  Serial.begin(115200); delay(300);
  Serial.printf("\n=== 時刻同期 確認 : SQ%d役 ===\n", MY_NUM);
  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(CH, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) { Serial.println("ESP-NOW init 失敗"); return; }
  esp_now_peer_info_t peer = {}; memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = CH; peer.encrypt = false; esp_now_add_peer(&peer);
  esp_now_register_recv_cb(onRecv);
  Serial.printf("チャンネル=%d で同期開始。offsetが安定すれば成功。\n", CH);
}
void loop() {
  Sync s{ 'Q', (uint8_t)MY_NUM, now_us(), 0 };
  esp_now_send(BCAST, (uint8_t*)&s, sizeof(s));
  delay(500);
}

#else
void setup(){ Serial.begin(115200); }
void loop(){ Serial.println("役割(ROLE)未定義"); delay(2000); }
#endif
