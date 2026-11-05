// drivers/buzzer.h ── 圧電ブザー駆動の分離（リファクタの“型”・20261105）
//
// 【このファイルの位置づけ】
//   firmware/gw/src/main.cpp は約2,050行で、WiFi/HTTP・ESP-NOW受信・ブザー・
//   信号灯・TFT描画・レース状態機械が1ファイルに同居している（責務過多）。
//   動作そのものは既に高度に最適化済み（部分再描画・間引き・非ブロッキング）だが、
//   “構造”としては関心事ごとにヘッダへ切り出せる。最も凝集度が高く副作用の局所的な
//   「ブザー」を最初の抽出単位として示す。
//
// 【設計の意図】
//   - ハード依存（ledc/ピン/周波数定数）とブザー状態（s_buzz_off_ms）を本ヘッダに閉じる。
//   - API は従来の関数名・引数・既定値をそのまま維持（buzzer_start/beep/tick/error_edge）。
//     → main.cpp 側は該当ブロックを削除し #include "drivers/buzzer.h" に置き換えるだけ。
//       呼び出し箇所は一切変更不要＝動作不変（ビルドして実機確認で確定）。
//   - 音の周波数/長さ定数は意味がドキュメント化された実機確定値のため1文字も変えない。
//
// 【適用手順（ユーザー側・PlatformIOでビルド＆焼き確認）】
//   1) main.cpp の PIN_BUZZER〜buzzer_error_edge() の定義ブロックを削除。
//   2) main.cpp 冒頭のインクルード群に #include "drivers/buzzer.h" を追加。
//   3) pio run -e gw6a でビルド → 実機で通過ピッ/スタート/エラー音を聞いて一致確認。
#pragma once
#include <Arduino.h>

namespace buzzer {

// ⚠ 12Vが行くのはブザー＋のみ。GPIO12はSS8050ドライバ経由でSPT15(−)を叩く（配線は従来どおり）。
static constexpr int      PIN_BUZZER       = 12;    // ストラップだが実機で書込み/起動OK確認済(20260811)
static constexpr uint8_t  BUZZER_LEDC_CH   = 2;     // ★ledcチャンネル。ch2=timer1（38kHz IRのch0=timer0とタイマー非共有）。ch1はtimer0共有でNG（20261012修正）
static constexpr uint8_t  BUZZER_LEDC_BITS = 10;    // 分解能（ledcWriteToneがduty50%を自動設定）
static constexpr uint32_t BUZZER_F_BEEP    = 2000;  // ピッ（通過・切断）。高めの明瞭音。SPT15最大音量は約1200Hz（85dBA@9Vpp・データシート）
static constexpr uint32_t BUZZER_F_START   = 1000;  // ブー（スタート）。※圧電は200Hz等の低音はほぼ無音→可聴の低めへ。最大音量が要るなら1200へ
static constexpr uint32_t BUZZER_F_ERR     = 1200;  // ピー（エラー）＝SPT15最大音量点1200Hz
static constexpr uint32_t BUZZER_MS        = 60;    // 通過ピッ鳴動長（80→40→20→60で実機比較し確定）
static constexpr uint32_t BUZZER_START_MS  = 500;   // スタート「ブー」長（緑と同時・DC27/DC30）
static constexpr uint32_t BUZZER_ERR_MS    = 500;   // エラー/WiFi切断/ノード離脱＝2000Hz・500msに統一（20261030）
static constexpr uint32_t GOAL_FREQ        = 800;   // ★ゴール(完走)音の周波数（20261030）
static constexpr uint32_t GOAL_MS          = 500;   // ★ゴール(完走)音の長さ

static uint32_t s_buzz_off_ms = 0;           // 0=消音中／非0=この millis で消音予定
static bool     s_sg_beeped   = false;       // ST_IDLE中に既にS/G通過ピッを鳴らしたか（TAの毎回鳴り防止・灰リセットで復活）

// ledc(PWM)で周波数を出す/止める（コア2.x=ch指定 / 3.x=pin指定・beam.hと同方式）。freq=0で停止。
static inline void tone(uint32_t freq) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWriteTone(PIN_BUZZER, freq);
#else
  ledcWriteTone(BUZZER_LEDC_CH, freq);
#endif
}

static inline void start(uint32_t ms, uint32_t freq = BUZZER_F_BEEP) {   // 非ブロッキング開始
  Serial.printf("[BUZ] %luHz %lums%s\n", (unsigned long)freq, (unsigned long)ms, s_buzz_off_ms ? " (上書き)" : "");   // ★20261102
  tone(freq);
  s_buzz_off_ms = millis() + ms;
  if (s_buzz_off_ms == 0) s_buzz_off_ms = 1; // millis境界で0になる稀ケースを回避
}

static inline void beep() {                  // S/G通過の瞬間に呼ぶ（60ms「ピッ」）
  start(BUZZER_MS, BUZZER_F_BEEP);
}

static inline void tick() {                  // loop毎に呼ぶ（時間が来たら消音）
  if (s_buzz_off_ms && (int32_t)(millis() - s_buzz_off_ms) >= 0) {
    tone(0);
    s_buzz_off_ms = 0;
    Serial.println("[BUZ] off");   // ★20261102
  }
}

// エラー表示の立ち上がり（無→有）でだけ 2秒「ピー」を1回鳴らす。
static inline void error_edge(bool err_now) {
  static bool prev = false;
  if (err_now && !prev) start(BUZZER_ERR_MS, BUZZER_F_BEEP);  // 2000Hz・500ms（20261030）
  prev = err_now;
}

}  // namespace buzzer

// ── 旧API互換シム ─────────────────────────────────────────────
// main.cpp 内の既存呼び出し（buzzer_start/buzzer_beep/...）をそのまま使えるようにする。
// これにより呼び出し箇所を1行も触らずに移行でき、動作が変わらないことを保証する。
static inline void buzzer_tone(uint32_t freq)                         { buzzer::tone(freq); }
static inline void buzzer_start(uint32_t ms, uint32_t freq = buzzer::BUZZER_F_BEEP) { buzzer::start(ms, freq); }
static inline void buzzer_beep()                                      { buzzer::beep(); }
static inline void buzzer_tick()                                      { buzzer::tick(); }
static inline void buzzer_error_edge(bool err_now)                    { buzzer::error_edge(err_now); }
