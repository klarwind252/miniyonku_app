// firmware/common/timesync.h
// ============================================================================
//  M4LAPS 共通 時刻同期（全機種で共有）
//  方針（docs/12 S3）：換算はノード側で行い、GWには“GW時刻”で送る。
//    - GWが親時計。ノードはSYNC往復でオフセットを推定し続ける。
//    - ノードの打刻は to_gw_us() でGW時刻へ換算してからEVENTに載せる。
//  推定法：最小RTT選抜（一番速い往復時の推定offsetを採用）。timesync_check準拠。
//  未同期のまま打刻したEVENTは quality=3 を立てる（捨てない・docs/12 S4）。
// ============================================================================
#pragma once
#include <stdint.h>
#include "esp_timer.h"
#include "protocol.h"
#include "espnow_link.h"

namespace tsync {

static inline uint64_t now_us() { return (uint64_t)esp_timer_get_time(); }

// ---- ノード側：オフセット推定状態 -----------------------------------------
static int64_t  s_offset_us   = 0;                       // GW時刻 − 自分時刻
static uint64_t s_best_rtt    = 0xFFFFFFFFFFFFFFFFULL;   // 最小RTT
static uint32_t s_samples     = 0;
static uint64_t s_pending_req = 0;                       // 直近SYNC_REQを送った自分時刻

static constexpr uint64_t RTT_RESET_US  = 60ULL * 1000000ULL; // 60秒ごとに最小RTTを緩める
static uint64_t s_last_reset = 0;

// 同期できているか（1回でもRTTを採れたか）。未同期なら EVENT quality=3。
static inline bool is_synced() { return s_samples > 0; }

// 自分時刻 → GW時刻へ換算（docs/12 S3）。EVENTのt_usはこれを通す。
static inline uint64_t to_gw_us(uint64_t self_us) {
  return (uint64_t)((int64_t)self_us + s_offset_us);
}
static inline uint64_t now_gw_us() { return to_gw_us(now_us()); }

// ---- ノード側：定期的にSYNC_REQを送る -------------------------------------
//  loop から一定間隔で呼ぶ。中継禁止(direct_only=true)で直通の速い往復を狙う。
static void tick_request(uint32_t interval_ms) {
  static uint32_t last = 0;
  uint32_t nowm = millis();
  if (nowm - last < interval_ms) return;
  last = nowm;

  // 一定周期で最小RTTを一度リセット（温度ドリフト・混雑変化に追従）
  uint64_t t = now_us();
  if (t - s_last_reset > RTT_RESET_US) {
    s_best_rtt = 0xFFFFFFFFFFFFFFFFULL;
    s_last_reset = t;
  }

  proto::SyncBody b = {};
  s_pending_req = now_us();
  b.t_req_us = s_pending_req;
  // GW(6)宛て・中継禁止（届かない時だけ上位で工夫。まずは直通）
  mesh::send(proto::PT_SYNC_REQ, 6, &b, sizeof(b), /*direct_only=*/true);
}

// ---- ノード側：SYNC_RSP受信でオフセット更新 -------------------------------
static void on_sync_rsp(const proto::SyncBody& b) {
  uint64_t t_recv = now_us();
  if (b.t_req_us != s_pending_req) return;      // 自分の要求への応答だけ採用
  uint64_t rtt = t_recv - b.t_req_us;
  // GW応答時刻の推定＝要求送信＋片道。offset = GW時刻 − (自分時刻)
  int64_t off = (int64_t)b.t_tx_us - (int64_t)(b.t_req_us + rtt / 2);
  s_samples++;
  if (rtt < s_best_rtt) { s_best_rtt = rtt; s_offset_us = off; }
}

// ---- GW側：SYNC_REQに即応答（自分の時刻を載せて返す）----------------------
//  GWのハンドラから呼ぶ。dst=要求元へ、中継禁止で返す。
static void gw_reply(const proto::PktHeader& h, const proto::SyncBody& in) {
  proto::SyncBody b = in;
  b.t_rx_us = now_us();     // 受けた瞬間
  b.t_tx_us = now_us();     // 返す瞬間（GW時刻）
  mesh::send(proto::PT_SYNC_RSP, h.src, &b, sizeof(b), /*direct_only=*/true);
}

// デバッグ表示用
static inline int64_t  offset_us() { return s_offset_us; }
static inline uint64_t best_rtt()  { return s_best_rtt; }
static inline uint32_t samples()   { return s_samples; }

} // namespace tsync
