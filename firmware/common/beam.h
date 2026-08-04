// firmware/common/beam.h
// ============================================================================
//  M4LAPS 共通 ビーム検出（セクター機・GW(S/G)が共有）
//  実配線：docs/03（SQ回路図）・docs/07（配線表）に一致
//    GPIO25 = 38kHz PWM → SS8050 → IR LED（3レーン共通）
//    ビームA = GPIO 32 / 33 / 34  （L1 / L2 / L3・74HC14経由）
//    ビームB = GPIO 19 / 21 / 22  （L1 / L2 / L3・74HC14経由）
//  論理：74HC14の先は「受信中=HIGH / 遮断=LOW」→ 遮断はFALLINGで拾う（docs/10.4確定）
//  ⚠ GPIO34は入力専用（プルアップ不可）。プルアップは基板側4.7kΩが担当 → INPUT。
//  ⚠ GPIO35/36/39は使用禁止（エラッタ・docs/03）。
//  本番追加：遮断エッジをISRでµs打刻 / デバウンス / A・B対で quality 判定
//    打刻は esp_timer_get_time()（µs）。GW時刻換算は tsync::to_gw_us() で後段。
//    quality：両素子取得=0 / 片方のみ=1（docs/13.4）。未同期の3はEVENT組立側。
// ============================================================================
#pragma once
#include <Arduino.h>
#include "esp_timer.h"

namespace beam {

// ---- レーン数とピン（docs/03・07）-----------------------------------------
static constexpr int LANES = 3;
static constexpr int PIN_PWM = 25;
static const int PIN_A[LANES] = {32, 33, 34};   // L1, L2, L3
static const int PIN_B[LANES] = {19, 21, 22};   // L1, L2, L3

// ---- しきい値 --------------------------------------------------------------
//  最短遮断は 2.0〜8.0 m/s で 19〜75ms（docs/01）。跳ねはµsオーダ。
//  A-B穴間隔は 30mm（docs/05・20260804変更確定）。最低想定 3m/s → A→B は最大10ms。
static constexpr uint32_t DEBOUNCE_US  = 1500;   // 跳ね除去（遮断幅19msより十分小）
static constexpr uint32_t PAIR_WAIT_US = 12000;  // A→B待ち猶予(30mm/3m/s=10ms+余裕)。超えたら片ビーム(q=1)

// ---- 確定した1通過（1レーンぶん）------------------------------------------
struct Hit {
  uint8_t  lane;      // 1..3（人向け番号。配列index+1）
  uint64_t t_a_us;    // A遮断µs（自分時計）
  uint64_t t_b_us;    // B遮断µs（0=取れず）
  uint8_t  quality;   // 0=両取得 / 1=片ビーム欠
};

// ---- ISR用の生状態（レーンごと・volatile）---------------------------------
static volatile uint64_t s_a_edge[LANES] = {0};
static volatile uint64_t s_b_edge[LANES] = {0};
static volatile uint64_t s_a_last[LANES] = {0};
static volatile uint64_t s_b_last[LANES] = {0};

static inline uint64_t now_us() { return (uint64_t)esp_timer_get_time(); }

// レーンごとISR：74HC14経由で遮断=LOW。FALLINGで発火。
// テンプレートで3レーン分を生成（引数なしISRにレーン番号を焼き込む）。
template <int L> static void IRAM_ATTR isr_a() {
  uint64_t t = now_us();
  if (t - s_a_last[L] < DEBOUNCE_US) return;
  s_a_last[L] = t;
  if (s_a_edge[L] == 0) s_a_edge[L] = t;
}
template <int L> static void IRAM_ATTR isr_b() {
  uint64_t t = now_us();
  if (t - s_b_last[L] < DEBOUNCE_US) return;
  s_b_last[L] = t;
  if (s_b_edge[L] == 0) s_b_edge[L] = t;
}

// ---- 初期化 ----------------------------------------------------------------
static void begin() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(PIN_PWM, 38000, 8);
  ledcWrite(PIN_PWM, 128);
#else
  ledcSetup(0, 38000, 8);
  ledcAttachPin(PIN_PWM, 0);
  ledcWrite(0, 128);
#endif
  // 74HC14の出力を受ける。プルアップは基板側4.7kΩが担当するのでINPUT。
  for (int i = 0; i < LANES; i++) {
    pinMode(PIN_A[i], INPUT);
    pinMode(PIN_B[i], INPUT);
  }
  // 遮断=LOW立下り → FALLING。テンプレートで各レーンのISRを結線。
  attachInterrupt(digitalPinToInterrupt(PIN_A[0]), isr_a<0>, FALLING);
  attachInterrupt(digitalPinToInterrupt(PIN_A[1]), isr_a<1>, FALLING);
  attachInterrupt(digitalPinToInterrupt(PIN_A[2]), isr_a<2>, FALLING);
  attachInterrupt(digitalPinToInterrupt(PIN_B[0]), isr_b<0>, FALLING);
  attachInterrupt(digitalPinToInterrupt(PIN_B[1]), isr_b<1>, FALLING);
  attachInterrupt(digitalPinToInterrupt(PIN_B[2]), isr_b<2>, FALLING);
}

// ---- ポーリング回収（loopから毎回呼ぶ）------------------------------------
//  どこか1レーンで通過が確定したら true と Hit を返す。複数同時は次回以降で回収。
static bool poll(Hit& out) {
  for (int i = 0; i < LANES; i++) {
    noInterrupts();
    uint64_t a = s_a_edge[i], b = s_b_edge[i];
    interrupts();

    if (a == 0 && b == 0) continue;

    uint64_t first = a ? a : b;
    bool have_both = (a != 0 && b != 0);
    if (!have_both && (now_us() - first) < PAIR_WAIT_US) continue; // まだ対を待つ

    out.lane    = (uint8_t)(i + 1);
    out.t_a_us  = a;
    out.t_b_us  = b;
    out.quality = have_both ? 0 : 1;

    noInterrupts();
    s_a_edge[i] = 0; s_b_edge[i] = 0;
    interrupts();
    return true;
  }
  return false;
}

// デバッグ：素子の生状態（74HC14先：受信=HIGH / 遮断=LOW）
static inline bool raw_a_blocked(int lane_idx) { return digitalRead(PIN_A[lane_idx]) == LOW; }
static inline bool raw_b_blocked(int lane_idx) { return digitalRead(PIN_B[lane_idx]) == LOW; }

} // namespace beam
