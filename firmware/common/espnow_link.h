// firmware/common/espnow_link.h
// ============================================================================
//  M4LAPS 共通 ESP-NOW リンク層（全機種で共有）
//  役割：初期化 / ブロードキャスト送信（20Bヘッダ付与）/ 受信 /
//        中継（TTL減算・ブロードキャスト再送）/ 重複排除リング(src,boot,seq)
//  - 検証ファーム(espnow_ping/timesync_check)の実績ある初期化手順を踏襲
//    （WIFI_STA + esp_wifi_set_channel + 旧来型recv cb + BCAST peer）
//  - docs/12 S1(ブロードキャスト+dst選別) / S2(重複排除) / D14(SYNC中継禁止)
// ============================================================================
#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <string.h>
#include "esp_timer.h"
#include "protocol.h"

namespace mesh {

using RecvHandler = void (*)(const proto::PktHeader& h,
                            const uint8_t* body, int body_len,
                            const uint8_t* src_mac);

static const uint8_t BCAST[6] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};

// ---- 内部状態 --------------------------------------------------------------
static uint8_t     s_my_id   = proto::NODE_UNASSIGNED;
static uint32_t    s_boot_id = 0;
static uint32_t    s_seq     = 0;
static RecvHandler s_handler = nullptr;
static uint8_t     s_cur_ch  = 0;        // 現在の運用ch（begin/ set_channel で更新）
static uint32_t    s_last_gw_rx_ms = 0;  // GW由来パケットを最後に受けた時刻（ch追従の在圏判定）

// 重複排除リング（直近64件）: src / boot_id / seq の3点で同定（docs/12 S2）
struct SeenKey { uint8_t src; uint32_t boot; uint32_t seq; };
static SeenKey s_seen[64];
static uint8_t s_seen_pos = 0;

static inline uint64_t now_us() { return (uint64_t)esp_timer_get_time(); }

static bool seen_before(uint8_t src, uint32_t boot, uint32_t seq) {
  for (int i = 0; i < 64; i++)
    if (s_seen[i].src == src && s_seen[i].boot == boot && s_seen[i].seq == seq)
      return true;
  s_seen[s_seen_pos] = { src, boot, seq };
  s_seen_pos = (s_seen_pos + 1) & 63;
  return false;
}

// ---- 送信 ------------------------------------------------------------------
//  dst=NODE_BROADCAST で全員へ。direct_only=true でSYNC等の中継禁止を立てる。
static void send(uint8_t type, uint8_t dst,
                const void* body, uint8_t body_len,
                bool direct_only = false) {
  uint8_t buf[250];
  proto::PktHeader h = {};
  h.version  = proto::PROTO_VERSION;
  h.type     = type;
  h.src      = s_my_id;
  h.dst      = dst;
  h.ttl      = proto::DEFAULT_TTL;
  h.flags    = direct_only ? proto::FLAG_DIRECT_ONLY : 0;
  h.relay_by = proto::NODE_BROADCAST;   // 自分発（未中継）
  h.boot_id  = s_boot_id;
  h.seq      = ++s_seq;
  memcpy(buf, &h, sizeof(h));
  if (body_len) memcpy(buf + sizeof(h), body, body_len);
  // 自分が出したものも重複排除リングに載せておく（中継の跳ね返り対策）
  seen_before(h.src, h.boot_id, h.seq);
  esp_now_send(BCAST, buf, sizeof(h) + body_len);
}

// ---- 中継（受信したブロードキャストをTTL減算して撒き直す）------------------
static void relay(proto::PktHeader h, const uint8_t* body, int body_len) {
  if (h.flags & proto::FLAG_DIRECT_ONLY) return;   // SYNCは中継しない（D14）
  if (h.ttl == 0) return;
  h.ttl     -= 1;
  h.flags   |= proto::FLAG_RELAYED;
  h.relay_by = s_my_id;
  uint8_t buf[250];
  memcpy(buf, &h, sizeof(h));
  if (body_len > 0) memcpy(buf + sizeof(h), body, body_len);
  esp_now_send(BCAST, buf, sizeof(h) + body_len);
}

// ---- 受信コールバック（旧来型シグネチャ・当環境に合わせる）----------------
static void on_recv_raw(const uint8_t* mac, const uint8_t* data, int len) {
  if (len < (int)sizeof(proto::PktHeader)) return;
  proto::PktHeader h;
  memcpy(&h, data, sizeof(h));
  if (h.version != proto::PROTO_VERSION) return;

  // ch追従の在圏判定：GW由来（GW6/GW7）のパケットを受けたら“今のchでGWに届く”証拠。
  //   HEARTBEAT含め種別を問わず記録する（重複排除の前に見る：跳ね返り前でも在圏は真）。
  if (proto::kind_of(h.src) == proto::KIND_GW) s_last_gw_rx_ms = millis();

  // 重複（往復・多重中継）は捨てる
  if (seen_before(h.src, h.boot_id, h.seq)) return;

  const uint8_t* body = data + sizeof(h);
  int body_len = len - (int)sizeof(h);

  // 自分宛て or 全員宛てならハンドラへ渡す
  bool for_me = (h.dst == s_my_id) || (h.dst == proto::NODE_BROADCAST);
  if (for_me && s_handler) s_handler(h, body, body_len, mac);

  // 自分宛てでない全体パケットは中継する（メッシュ・docs/12 S1）
  if (h.dst != s_my_id) relay(h, body, body_len);
}

// ---- 初期化 ----------------------------------------------------------------
//  my_id：自分のノードID（envのNODE_ID）。channel：運用チャンネル。
static bool begin(uint8_t my_id, uint8_t channel, RecvHandler handler) {
  s_my_id   = my_id;
  s_handler = handler;
  s_boot_id = esp_random();              // 起動ごと（docs/12.1）
  s_seq     = 0;
  s_cur_ch  = channel;

  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_ps(WIFI_PS_NONE);          // ★ESP-NOW安定化：WiFi省電力を無効化。
                                          //   AP接続時のモデムスリープでESP-NOW受信が
                                          //   間欠脱落する（＝フラッピング）のを防ぐ（T-8）。
  if (esp_now_init() != ESP_OK) return false;

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = channel;
  peer.encrypt = false;
  esp_now_add_peer(&peer);

  esp_now_register_recv_cb(on_recv_raw);
  return true;
}

// 運用chを切り替える（ノードの追従／GWのWiFi実ch採用で使う）。
//  ⚠ ブロードキャストpeerはchを持つため、set直後にpeerを貼り直さないと esp_now_send が
//    ESP_ERR_ESPNOW_CHAN で失敗する。del→add で作り直す。
//  ・ノード（未接続STA）：esp_wifi_set_channel が実際にchを動かす。
//  ・GW（AP接続STA）    ：STAはAPのchに固定されるため set_channel は実質“peerのch合わせ”。
//                         必ずWiFi実ch（esp_wifi_get_channel）と同じ値を渡すこと。
static void set_channel(uint8_t ch) {
  if (ch < 1 || ch > 13) return;
  if (ch == s_cur_ch) return;
  esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
  esp_now_del_peer(BCAST);
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = ch;
  peer.encrypt = false;
  esp_now_add_peer(&peer);
  s_cur_ch = ch;
}
static inline uint8_t  channel()        { return s_cur_ch; }
static inline uint32_t last_gw_rx_ms()  { return s_last_gw_rx_ms; }

// 割当確定後にIDを差し替える（未割当→確定node_idへ）
static inline void set_node_id(uint8_t id) { s_my_id = id; }
static inline uint8_t  my_id()   { return s_my_id; }
static inline uint32_t boot_id() { return s_boot_id; }
static inline uint32_t last_seq(){ return s_seq; }   // 直近に送ったseq（EVENT再送の照合用）

// EVENT_ACK専用送信：body無しで、受領したseqをヘッダ reserved1 に載せて返す。
// gate側は EVENT_ACK 受信時に h.reserved1 と送信中EVENTのseqを照合して外す。
static void send_ack(uint8_t type, uint8_t dst, uint32_t acked_seq) {
  uint8_t buf[sizeof(proto::PktHeader)];
  proto::PktHeader h = {};
  h.version   = proto::PROTO_VERSION;
  h.type      = type;
  h.src       = s_my_id;
  h.dst       = dst;
  h.ttl       = proto::DEFAULT_TTL;
  h.flags     = 0;
  h.relay_by  = proto::NODE_BROADCAST;
  h.boot_id   = s_boot_id;
  h.seq       = ++s_seq;
  h.reserved1 = acked_seq;      // ★受領したseqを載せる（gate側が照合）
  memcpy(buf, &h, sizeof(h));
  seen_before(h.src, h.boot_id, h.seq);
  esp_now_send(BCAST, buf, sizeof(h));
}

} // namespace mesh
