// firmware/common/protocol.h
// ============================================================================
//  M4LAPS 共通プロトコル定義（全機種で共有する“通信の契約”）
//  - docs/12.1 確定：PROTO_VERSION=1 / ヘッダ20バイト固定 / boot_id同梱
//  - docs/14 DA9 ：ノードID＝機体番号（固定・永続）
//  - docs/19.5   ：JOIN契約（kind 0-3 / nvs_node_id 0xFE）をサーバAPIに一致
//  ⚠ このヘッダのサイズ(20)を変えると全ノードを同時に焼き直す必要がある。
//     拡張は reserved を削って行い、20バイトを保つこと（docs/12.1）。
// ============================================================================
#pragma once
#include <stdint.h>

namespace proto {

// ---- バージョン ------------------------------------------------------------
static constexpr uint8_t PROTO_VERSION = 1;

// ---- ノードID（＝機体番号・固定永続 / docs/14 DA9）-------------------------
//  0..5  = SQ0..SQ5（セクター）   6/7 = GW6/GW7   8/9 = RC8/RC9   10/11 = SG10/SG11
static constexpr uint8_t NODE_ID_MAX     = 11;    // 台帳12台（0..11）
static constexpr uint8_t NODE_UNASSIGNED = 0xFE;  // 未割当（JOINで自称）→ API nvs_node_id
static constexpr uint8_t NODE_BROADCAST  = 0xFF;  // 全員向け（ESP-NOWのFF:FF..と対応）

// ---- 種別（kind）: サーバ /api/timing/join と“完全一致”させる（変更不可）----
enum NodeKind : uint8_t {
  KIND_GW = 0,   // ゲートウェイ（S/G一体）
  KIND_SQ = 1,   // セクター
  KIND_RC = 2,   // リモコン
  KIND_SG = 3,   // シグナル
};

// あるノードIDがどの種別かを返す（0..11 の固定割当）。範囲外は 0xFF。
static inline uint8_t kind_of(uint8_t node_id) {
  if (node_id <= 5)  return KIND_SQ;
  if (node_id <= 7)  return KIND_GW;
  if (node_id <= 9)  return KIND_RC;
  if (node_id <= 11) return KIND_SG;
  return 0xFF;
}

// ---- パケット種別 ----------------------------------------------------------
enum PktType : uint8_t {
  PT_JOIN        = 1,   // ノード→GW：MAC＋kind＋自称IDを名乗る
  PT_JOIN_ACK    = 2,   // GW→ノード：確定node_idを返す（未割当なら送らない）
  PT_HEARTBEAT   = 3,   // ノード→GW：死活
  PT_SYNC_REQ    = 4,   // 双方向：時刻同期（⚠中継禁止・FLAG_DIRECT_ONLY）
  PT_SYNC_RSP    = 5,   // 同上
  PT_EVENT       = 6,   // ノード→GW：通過打刻（ACK必須・最大4回再送）
  PT_EVENT_ACK   = 7,   // GW→ノード：受領（ingest前に返す・docs/12 S5）
  PT_COMMAND     = 8,   // RC→GW / GW→SG：赤緑等
  PT_COMMAND_ACK = 9,
  PT_HEALTH      = 10,  // ノード→GW：詳細健全性
  PT_CONFIG      = 11,  // GW→ノード：設定配布（チャンネル等）
};

// ---- フラグ ----------------------------------------------------------------
static constexpr uint8_t FLAG_RELAYED     = 1 << 0;  // 中継された（docs/12 D13）
static constexpr uint8_t FLAG_DIRECT_ONLY = 1 << 1;  // 中継禁止（SYNC専用・D14）

// ---- ヘッダ（20バイト固定・全パケット先頭）--------------------------------
//  冪等キー（サーバ）= (device_id, src, src_boot_id, seq)（docs/12.1 D12）に対応：
//     src=送信元node_id / src_boot_id=boot_id / seq=seq
#pragma pack(push, 1)
struct PktHeader {
  uint8_t  version;    // = PROTO_VERSION
  uint8_t  type;       // PktType
  uint8_t  src;        // 送信元 node_id（未割当は NODE_UNASSIGNED）
  uint8_t  dst;        // 宛先 node_id（全員は NODE_BROADCAST）
  uint8_t  ttl;        // 中継ホップ上限（0で破棄）
  uint8_t  flags;      // FLAG_*
  uint8_t  relay_by;   // 中継したノード（無ければ NODE_BROADCAST）
  uint8_t  reserved0;  // 予約（0固定・整列用）
  uint32_t boot_id;    // 起動ごと esp_random()（再起動で変わる）
  uint32_t seq;        // 送信元内で単調増加
  uint32_t reserved1;  // 予約4バイト（0固定・将来の拡張はここを削る）
};
#pragma pack(pop)
static_assert(sizeof(PktHeader) == 20, "PktHeader must stay 20 bytes (docs/12.1)");

// ---- ペイロード ------------------------------------------------------------
// JOIN：ノードが自分のMAC/種別/自称IDを名乗る。GWはこれをそのまま
//        サーバ POST /api/timing/join（mac,kind,fw_major,fw_minor,nvs_node_id）へ転記する。
#pragma pack(push, 1)
struct JoinBody {
  uint8_t  mac[6];       // ノード自身のSTA MAC（中継されても失われないよう同梱）
  uint8_t  kind;         // NodeKind（0..3）
  uint8_t  fw_major;
  uint8_t  fw_minor;
  uint8_t  nvs_node_id;  // 自称ID（未割当は NODE_UNASSIGNED=0xFE）
};

// JOIN_ACK：GWが確定node_idを返す。あわせて運用チャンネルも配れる（docs/19.6）。
struct JoinAckBody {
  uint8_t  mac[6];       // 宛先ノードのMAC（誰宛か明示）
  uint8_t  node_id;      // 確定ID（0..11）
  uint8_t  channel;      // 運用WiFiチャンネル（GW基準・0なら無指定）
};

// EVENT：通過打刻。時刻はノード側でGW時刻へ換算済み（docs/12 S3）。
//   quality: 0=正常 / 1=片ビーム欠 / 3=未同期のまま打刻（docs/13.4）
struct EventBody {
  uint8_t  lane;         // 物理レーン 1..3
  uint8_t  quality;      // 上記
  uint16_t _pad;
  uint64_t t_us;         // A素子の打刻（GW時刻・µs）
  uint64_t t_us_b;       // B素子の打刻（µs・無ければ0）
};

// SYNC：往復で片道遅延とオフセットを推定（中継禁止）。
struct SyncBody {
  uint64_t t_req_us;     // 要求送信時刻（要求元時計）
  uint64_t t_rx_us;      // 受信時刻（応答側時計）※RSPで埋める
  uint64_t t_tx_us;      // 応答送信時刻（応答側時計）※RSPで埋める
};

// COMMAND：赤/緑/リセット等（docs/14 DA11）。
enum CmdCode : uint8_t { CMD_RESET=1, CMD_SIGNAL=2, CMD_GREEN=3, CMD_RED=4 };
struct CommandBody {
  uint8_t  code;         // CmdCode
  uint8_t  _pad[3];
  uint32_t arg;          // 予備（ランダム時間msなど）
};
#pragma pack(pop)

// ---- 送受信ヘルパの目安（実装は espnow_link 側）----------------------------
static constexpr uint8_t DEFAULT_TTL = 4;   // 総ノード10台での中継上限の目安（docs/14 DA10）

} // namespace proto
