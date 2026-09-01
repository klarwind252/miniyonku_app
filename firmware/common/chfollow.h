// firmware/common/chfollow.h
// ============================================================================
//  M4LAPS ノード側チャンネル追従（SQ/RC/SG 共通・docs/21 案X ch-1＋走査）
//  ねらい：GWがWiFi(スマホ)の実chへ乗る → ノードはGWのchへ自動追従する（状態B）。
//          どこにも固定chを置かず、SSIDだけ固定でchはスマホ任せにできる。
//
//  仕組み（ノードは非接続STAなので自由にchを動かせる。GWは動かせない＝ノードが探す）：
//    ・起動chは load_channel（前回学習値・無ければコンパイル既定=ブートストラップ）。
//    ・GW由来パケット（HEARTBEAT等）を受けている間は在圏＝現chが正しい。何もしない。
//    ・SWEEP_AFTER_MS 在圏が切れたら「走査」：1→13chを順に移りJOINを撒く。
//      GWは JOIN受信で即HEARTBEATを撒き返す（gw側実装）ので、正しいchに乗った瞬間に
//      在圏が回復し、走査停止＝そのchにロック。学習値をNVSへ保存（次回の初手短縮）。
//    ・運用中にスマホのchが変わってGWが移動しても、在圏が切れれば再走査で追従する。
//
//  GWは本ヘッダを使わない（GWは自分のWiFi実chを採用する別ロジック・gw/main.cpp）。
// ============================================================================
#pragma once
#include <Arduino.h>
#include "espnow_link.h"
#include "nvs_config.h"

namespace chfollow {

using JoinSender = void (*)();   // 現chでJOINを1本撒くコールバック（機種ごとのkindを含む）

// 走査パラメータ（GWは2sごと在席ビーコン＋JOIN即応なので短い滞在で拾える）。
static constexpr uint32_t SWEEP_AFTER_MS = 12000;  // 在圏切れ判定（GWビーコン2s×6本の余裕。GW側の数秒停止で走査に入らない・2026-08-24）
static constexpr uint32_t SWEEP_DWELL_MS = 500;    // 1chあたりの滞在（JOIN撒いて待つ）

// 20260831k：GW計測中ラッチ（「計測中はch走査禁止」・ユーザー確定）。
//  GWビーコン付帯の racing フラグを note_gw_racing() で記憶。在圏が切れても、
//  最後に聞いた状態が計測中なら RACING_HOLD_MS の間は走査を保留する（無駄なch離脱を抑止）。
//  保持上限を設ける理由：レース中にGWが再起動しchが変わると、SEはもうGWの声を聞けない。
//  我慢し続けると永遠に再接続できないため、60秒無音＝異常とみなして走査を再開する（安全弁）。
static bool     s_gw_racing    = false;
static uint32_t s_gw_racing_ms = 0;
static constexpr uint32_t RACING_HOLD_MS = 60000;
inline void note_gw_racing(bool racing) { s_gw_racing = racing; s_gw_racing_ms = millis(); }

// 起動時の初手ch：前回学習値（NVS "m4cfg"/"ch"）→無ければ def（=ブートストラップ）。
inline uint8_t initial_channel(uint8_t def) { return cfg::load_channel(def); }

// loop から毎周回呼ぶ。send_join は「今のchでJOINを撒く」機種側の関数。
inline void tick(JoinSender send_join) {
  static bool     sweeping  = false;
  static uint32_t last_hop  = 0;
  static bool     persisted = false;

  const uint32_t nowm = millis();
  const uint32_t last = mesh::last_gw_rx_ms();
  const bool in_contact = (last != 0) && (nowm - last < SWEEP_AFTER_MS);

  if (in_contact) {
    sweeping = false;
    if (!persisted) {                    // 在圏が確立した最初の一度だけ学習値を保存
      cfg::save_channel(mesh::channel());
      persisted = true;
    }
    return;
  }

  // 在圏が切れた → 走査。ただし20260831k：GWが計測中(最後に聞いた状態)なら保留。
  //   READYに戻ったビーコン(racing=0)を受ければ即・通常動作へ。60秒無音で安全弁解除。
  if (s_gw_racing && (nowm - s_gw_racing_ms) < RACING_HOLD_MS) {
    sweeping = false;                    // 保留中は走査状態も持ち越さない（次回は最初から）
    return;
  }
  // 在圏が切れた → 走査。ただし最初の1滞在は「現chのままJOIN再送」（2026-08-24）。
  //   GWが一時的に黙っていただけなら現chで即復帰でき、無駄なch離脱をしない。
  persisted = false;
  static bool first_dwell = false;
  if (!sweeping) {
    sweeping = true; first_dwell = true;
    last_hop = nowm;
    if (send_join) send_join();          // 現chで即JOIN（chは動かさない）
    return;
  }
  if (nowm - last_hop < SWEEP_DWELL_MS) return;
  last_hop = nowm;
  if (first_dwell) { first_dwell = false; }
  uint8_t next = (uint8_t)((mesh::channel() % 13) + 1);   // 1..13 を巡回
  mesh::set_channel(next);
  if (send_join) send_join();            // GWの即HEARTBEATを誘発し、正chで在圏回復
}

}  // namespace chfollow
