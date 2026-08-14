// firmware/common/nvs_config.h
// ============================================================================
//  全機材共通のNVS設定読み込み。環境依存値（ESP-NOWチャンネル）を NVS "m4cfg" から
//  読む。無ければコンパイル時の既定（ESPNOW_CHANNEL）にフォールバック。
//  ⚠ NODE_ID はここに含めない（機体の識別はenv焼き付けが正・台帳整合のため）。
//  アプリが生成した nvs.bin(0x9000) を USB(Web Serial) で焼くと、ここが拾う。
// ============================================================================
#pragma once
#include <Arduino.h>
#include <Preferences.h>

namespace cfg {

// NVS "m4cfg" / キー "ch"(u8) を読む。範囲外や未設定なら def。
// from_nvs に「NVS由来だったか」を返す（起動ログの src 表示用）。
inline uint8_t load_channel(uint8_t def, bool* from_nvs = nullptr) {
  Preferences p;
  uint8_t ch = def;
  bool got = false;
  if (p.begin("m4cfg", /*readOnly=*/true)) {
    if (p.isKey("ch")) { ch = p.getUChar("ch", def); got = true; }
    p.end();
  }
  if (ch < 1 || ch > 13) { ch = def; got = false; }   // 妥当域外は既定へ
  if (from_nvs) *from_nvs = got;
  return ch;
}

// NVS "m4cfg" / キー "ch"(u8) を書く（ノードがGWのchを学習したとき保存＝次回起動の初手を速く）。
//  ⚠ 31章のアプリ手動書込みと同じキー。学習値で上書きするが各機体のNVSは独立なので影響は自機のみ。
//  ⚠ 現在値と同じなら書かない（フラッシュ摩耗回避）。妥当域外は無視。
inline void save_channel(uint8_t ch) {
  if (ch < 1 || ch > 13) return;
  Preferences p;
  if (!p.begin("m4cfg", /*readOnly=*/false)) return;
  if (!p.isKey("ch") || p.getUChar("ch", 0xFF) != ch) p.putUChar("ch", ch);
  p.end();
}

}  // namespace cfg
