// firmware/gate/src/main.cpp
// ============================================================================
//  M4LAPS セクター機（SQ0〜SQ5）ファーム
//  役割：3レーンのビーム通過を検出 → GW時刻へ換算 → EVENTでGWへ送る（ACK必須）。
//        定期的にSYNCを投げてオフセットを保つ。JOIN/HEARTBEATで存在を知らせる。
//  common（protocol/espnow_link/timesync/beam）を呼ぶだけの薄い実装。
//  NODE_ID は platformio.ini の -DNODE_ID で焼き込む（0..5）。
// ============================================================================
#include <Arduino.h>
#include <WiFi.h>
#include "protocol.h"
#include "espnow_link.h"
#include "chfollow.h"
#include "timesync.h"
#include "beam.h"

#ifndef NODE_ID
#define NODE_ID 0
#endif
#ifndef ESPNOW_CHANNEL
#define ESPNOW_CHANNEL 1
#endif

static constexpr uint8_t FW_MAJOR = 0;
static constexpr uint8_t FW_MINOR = 1;

// ---- 送信中イベント（ACK待ち・最大4回再送・docs/12 EVENT）----------------
struct Pending {
  bool     used;
  uint32_t seq;         // このEVENTのヘッダseq（ACK照合用）
  uint8_t  tries;
  uint32_t last_ms;
  proto::EventBody body;
};
static constexpr int    PENDING_MAX = 8;
static constexpr uint8_t MAX_TRIES  = 4;
static constexpr uint32_t RESEND_MS = 60;     // ACK来なければ60 msで再送
static Pending s_pending[PENDING_MAX];

static bool s_assigned = false;   // JOIN_ACKを受けた（台帳にMAC登録済み）

// ---- 受信ハンドラ ----------------------------------------------------------
static void on_recv(const proto::PktHeader& h, const uint8_t* body,
                    int body_len, const uint8_t* /*mac*/) {
  switch (h.type) {
    case proto::PT_SYNC_RSP: {
      if (body_len >= (int)sizeof(proto::SyncBody)) {
        proto::SyncBody b; memcpy(&b, body, sizeof(b));
        tsync::on_sync_rsp(b);
      }
    } break;

    case proto::PT_JOIN_ACK: {
      if (body_len >= (int)sizeof(proto::JoinAckBody)) {
        proto::JoinAckBody b; memcpy(&b, body, sizeof(b));
        if (b.node_id == NODE_ID) s_assigned = true;   // IDはenv固定・上書きしない
        // ch追従（状態B）：GWが載せた運用ch（=WiFi実ch）へ念のため合わせる。通常は
        //   走査で既に同chに居るが、GWのch移動直後などのズレをここでも吸収する。
        if (b.channel >= 1 && b.channel <= 13) mesh::set_channel(b.channel);
      }
    } break;

    case proto::PT_EVENT_ACK: {
      uint32_t acked_seq = h.reserved1;                // GWが載せた受領seq
      for (int i = 0; i < PENDING_MAX; i++)
        if (s_pending[i].used && s_pending[i].seq == acked_seq)
          s_pending[i].used = false;                   // 受領確認 → 外す
    } break;

    default: break;
  }
}

// ---- EVENT をキューに積む ---------------------------------------------------
static void enqueue_event(const beam::Hit& hit) {
  for (int i = 0; i < PENDING_MAX; i++) {
    if (s_pending[i].used) continue;
    Pending& p = s_pending[i];
    p.used = true;
    p.tries = 0;
    p.last_ms = 0;   // すぐ送る
    p.body.lane    = hit.lane;
    // 張り付き(2/A1)は同期状態に依らない異常通知なので未同期でも 2 を保つ。
    //   それ以外は S4 に従い未同期なら 3 で上書き。
    p.body.quality = (hit.quality == 2) ? 2
                     : (tsync::is_synced() ? hit.quality : 3);
    p.body._pad    = 0;
    p.body.t_us    = tsync::to_gw_us(hit.t_a_us);            // GW時刻へ換算（S3）
    p.body.t_us_b  = hit.t_b_us ? tsync::to_gw_us(hit.t_b_us) : 0;
    return;
  }
  Serial.println("[WARN] pending full");
}

// ---- 送信中イベントの面倒を見る（送信・再送・あきらめ）--------------------
static void service_pending() {
  uint32_t nowm = millis();
  for (int i = 0; i < PENDING_MAX; i++) {
    Pending& p = s_pending[i];
    if (!p.used) continue;
    if (p.last_ms != 0 && (nowm - p.last_ms) < RESEND_MS) continue;

    if (p.tries >= MAX_TRIES) {
      p.used = false;
      Serial.printf("[WARN] event give up lane=%u\n", p.body.lane);
      // C1(24.14/案A・27章)：あきらめた瞬間、GWへLost通知を1本送る（best-effort）。
      //   この通知自体は再送管理しない。届けばGWが Lost×/セクター通信不良を表示。
      //   落ちても既存のNode×や後続EVENTのseq飛びで異常自体は別途露見しうる。
      proto::LostNoticeBody ln{};
      ln.lane        = p.body.lane;
      ln.dropped_seq = p.seq;
      mesh::send(proto::PT_LOST_NOTICE, 6 /*GW*/, &ln, sizeof(ln));
      continue;
    }
    mesh::send(proto::PT_EVENT, 6 /*GW*/, &p.body, sizeof(p.body));
    p.seq     = mesh::last_seq();   // 直近に送ったseq
    p.tries  += 1;
    p.last_ms = nowm;
  }
}

// 現chでJOINを1本撒く（ch追従の走査からも呼ぶ）
static void send_join() {
  proto::JoinBody jb = {};
  WiFi.macAddress(jb.mac);
  jb.kind = proto::KIND_SQ;
  jb.fw_major = FW_MAJOR;
  jb.fw_minor = FW_MINOR;
  jb.nvs_node_id = NODE_ID;        // 解釈X：自分のIDを知っている
  mesh::send(proto::PT_JOIN, 6, &jb, sizeof(jb));
}

// JOIN/HEARTBEAT を定期送信（MAC台帳登録・死活）
static void tick_presence() {
  static uint32_t last = 0;
  uint32_t nowm = millis();
  uint32_t interval = s_assigned ? 3000 : 1000;   // 未登録は短め
  if (nowm - last < interval) return;
  last = nowm;

  if (!s_assigned) send_join();
  else             mesh::send(proto::PT_HEARTBEAT, 6, nullptr, 0);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n=== M4LAPS Sector SQ%d ===\n", NODE_ID);
  memset(s_pending, 0, sizeof(s_pending));

  uint8_t ch0 = chfollow::initial_channel(ESPNOW_CHANNEL);   // 前回学習ch→無ければ既定
  if (!mesh::begin(NODE_ID, ch0, on_recv)) {
    Serial.println("ESP-NOW init 失敗"); return;
  }
  beam::begin();
  Serial.printf("ch=%d で稼働（GWのchへ自動追従）。SYNCとビーム待受け開始。\n", ch0);
}

void loop() {
  chfollow::tick(send_join);   // GWのchへ追従（在圏切れで1..13走査）
  tsync::tick_request(500);    // 0.5秒ごとにSYNC
  tick_presence();

  beam::Hit hit;
  while (beam::poll(hit)) {    // 溜まった通過を全部拾う
    enqueue_event(hit);
    uint8_t q = (hit.quality == 2) ? 2
                : (tsync::is_synced() ? hit.quality : 3);
    Serial.printf("[EV] lane=%u q=%u tA=%llu tB=%llu%s\n",
                  hit.lane, q,
                  (unsigned long long)hit.t_a_us,
                  (unsigned long long)hit.t_b_us,
                  (q == 2) ? " STICK(A1)" : "");
  }
  service_pending();
}
