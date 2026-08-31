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
#include <Preferences.h>
#include "esp_timer.h"

namespace beam {

// ---- レーン数とピン（docs/03・07）-----------------------------------------
static constexpr int LANES = 3;
static constexpr int PIN_PWM = 25;
static const int PIN_A[LANES] = {32, 33, 34};   // L1, L2, L3
static const int PIN_B[LANES] = {19, 21, 22};   // L1, L2, L3

// ---- 使用レーンマスク（20260830c）------------------------------------------
//  未接続レーンの浮きピン誤発火対策。bit0=L1 / bit1=L2 / bit2=L3。
//  NVS "m4cfg"/"lanes"(u8) から読む（未設定は既定）。ビルド時既定は
//  -D BEAM_LANE_MASK_DEFAULT=0x01 等で上書き可（例：L1のみ運用のベンチ）。
//  マスク外レーンは ISR を張らず、poll/poll_stick でも読まない。
#ifndef BEAM_LANE_MASK_DEFAULT
#define BEAM_LANE_MASK_DEFAULT 0x07
#endif
static uint8_t s_mask = BEAM_LANE_MASK_DEFAULT;
static inline bool lane_on(int i) { return (s_mask >> i) & 1; }
static inline uint8_t mask() { return s_mask; }

// ---- しきい値 --------------------------------------------------------------
//  最短遮断は 2.0〜8.0 m/s で 19〜75ms（docs/01）。跳ねはµsオーダ。
//  A-B穴間隔は 30mm（docs/05・20260804変更確定）。最低想定 3m/s → A→B は最大10ms。
static constexpr uint32_t DEBOUNCE_US  = 1500;   // 跳ね除去（遮断幅19msより十分小）
static constexpr uint32_t PAIR_WAIT_US = 12000;  // A→B待ち猶予(30mm/3m/s=10ms+余裕)。超えたら片ビーム(q=1)
// 20260831h：1通過確定後の不応期。A/Bが割れて確定しても、対の残り物や車体長ぶんの
//   遮断残響（最長75ms程度）を新規通過として二重カウントしないよう締め出す。
//   地点間の最短到達(MIN_LAP_US=1s)より十分短い100msに設定（隣地点の正規通過は締め出さない）。
static constexpr uint32_t REFRACTORY_US = 100000; // 100ms（対確定後の同レーン締め出し）
// 24.5 A1：両ビーム欠（LED破損・センサー破損・光軸ズレ・CO停車・外乱光の感度飽和）。
//   遮断(LOW)が STICK_US 継続したら「張り付き」＝異常として quality=2 を1回発火。
//   3秒＝READY感知不能エラー／A1故障判定の共通しきい値（20260831b）。
//   遅いマシン（低速通過）でも遮断は長くて数百ms程度なので、3秒なら通過を誤検知しない。
static constexpr uint32_t STICK_US     = 3000000; // 3s（張り付き＝感知不能/A1判定）

// ---- 確定した1通過（1レーンぶん）------------------------------------------
struct Hit {
  uint8_t  lane;      // 1..3（人向け番号。配列index+1）
  uint64_t t_a_us;    // A遮断µs（自分時計）
  uint64_t t_b_us;    // B遮断µs（0=取れず）
  uint8_t  quality;   // 0=両取得 / 1=片ビーム欠 / 2=両ビーム欠(張り付き・A1)
  uint8_t  miss;      // q=1時どちらが欠けたか：1=A欠け(Bのみ) / 2=B欠け(Aのみ) / 0=該当なし（20260830c）
};

// ---- 張り付き(A1)監視用の状態（レーンごと）--------------------------------
//  遮断が始まった時刻を保持。STICK_US 継続で quality=2 を1回発火。復帰で解除。
static uint64_t s_block_since[LANES] = {0};   // 0=非遮断 / 非0=遮断開始µs
static bool     s_stick_fired[LANES] = {false}; // このブロック区間で発火済みか
// 20260831h：1通過(A/B対)確定後の不応期。A→Bが割れて確定した直後に、対の残り物や
//   3cm由来の遅延エッジを「新しい通過」として二重カウントしないための締め出し窓。
//   poll確定時に now_us()+REFRACTORY_US を入れ、これを過ぎるまで同レーンの新規発火を捨てる。
static uint64_t s_refractory_until[LANES] = {0};

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
  // 使用レーンマスク：NVS "m4cfg"/"lanes" があればそれを採用（20260830c）
  {
    Preferences p;
    if (p.begin("m4cfg", /*readOnly=*/true)) {
      if (p.isKey("lanes")) s_mask = p.getUChar("lanes", BEAM_LANE_MASK_DEFAULT) & 0x07;
      p.end();
    }
    if (s_mask == 0) s_mask = 0x07;   // 全禁止は無効（設定ミス保護）
  }
  // 74HC14の出力を受ける。プルアップは基板側4.7kΩが担当するのでINPUT。
  for (int i = 0; i < LANES; i++) {
    pinMode(PIN_A[i], INPUT);
    pinMode(PIN_B[i], INPUT);
  }
  // 遮断=LOW立下り → FALLING。マスク外レーンはISRを張らない（浮きピン誤発火対策）。
  if (lane_on(0)) { attachInterrupt(digitalPinToInterrupt(PIN_A[0]), isr_a<0>, FALLING);
                    attachInterrupt(digitalPinToInterrupt(PIN_B[0]), isr_b<0>, FALLING); }
  if (lane_on(1)) { attachInterrupt(digitalPinToInterrupt(PIN_A[1]), isr_a<1>, FALLING);
                    attachInterrupt(digitalPinToInterrupt(PIN_B[1]), isr_b<1>, FALLING); }
  if (lane_on(2)) { attachInterrupt(digitalPinToInterrupt(PIN_A[2]), isr_a<2>, FALLING);
                    attachInterrupt(digitalPinToInterrupt(PIN_B[2]), isr_b<2>, FALLING); }
  // 起動時 自己診断ログ（HIGH=受光中/LOW=未受光。LED未点灯直後はLOWでも正常）
  Serial.printf("[BEAM] mask=0x%X  ", s_mask);
  for (int i = 0; i < LANES; i++) {
    if (!lane_on(i)) { Serial.printf("L%d:--/-- ", i + 1); continue; }
    Serial.printf("L%d:%s/%s ", i + 1,
                  digitalRead(PIN_A[i]) == HIGH ? "A_OK" : "A_NG",
                  digitalRead(PIN_B[i]) == HIGH ? "B_OK" : "B_NG");
  }
  Serial.println();
}

// 素子の生状態（74HC14先：受信=HIGH / 遮断=LOW）
static inline bool raw_a_blocked(int lane_idx) { return digitalRead(PIN_A[lane_idx]) == LOW; }
static inline bool raw_b_blocked(int lane_idx) { return digitalRead(PIN_B[lane_idx]) == LOW; }

// ---- 張り付き(A1)判定：どこか1レーンで STICK_US 継続遮断なら quality=2 を返す ----
//  24.5 A1：両ビーム欠（LED破損・センサー破損・光軸ズレ・CO停車・外乱光の感度飽和）。
//  正常な通過は最長75msで解けるので、3秒継続=異常。1ブロック区間で1回だけ発火する。
//  発火時に miss でどちら側かを分類：1=Aのみ / 2=Bのみ / 3=両方（20260830c）。
static bool poll_stick(Hit& out) {
  uint64_t t = now_us();
  for (int i = 0; i < LANES; i++) {
    if (!lane_on(i)) continue;            // マスク外レーンは見ない（20260830c）
    bool a_blk = raw_a_blocked(i), b_blk = raw_b_blocked(i);
    bool blocked = a_blk || b_blk;
    if (!blocked) {                       // 復帰：状態リセット（次の張り付きに備える）
      s_block_since[i] = 0;
      s_stick_fired[i] = false;
      continue;
    }
    if (s_block_since[i] == 0) {           // 遮断開始
      s_block_since[i] = t;
      continue;
    }
    if (!s_stick_fired[i] && (t - s_block_since[i]) >= STICK_US) {
      s_stick_fired[i] = true;            // このブロック区間では二度発火しない
      out.lane    = (uint8_t)(i + 1);
      out.t_a_us  = t;                    // 張り付き検知時刻（参考値）
      out.t_b_us  = 0;
      out.quality = 2;                    // A1：両ビーム欠（張り付き）
      out.miss    = (a_blk && b_blk) ? 3 : (a_blk ? 1 : 2);   // どちら側の張り付きか
      // エッジ由来の未確定分は捨てる（張り付き中の片エッジは通過ではない）
      noInterrupts();
      s_a_edge[i] = 0; s_b_edge[i] = 0;
      interrupts();
      return true;
    }
  }
  return false;
}

// ---- 張り付きのライブ参照（ステータスバー・TX側疑い判定用・20260830c）------
//  レーンiが「いま」STICK_US 以上連続遮断か。poll_stick が s_block_since を更新
//  している前提（loopで beam::poll を回していれば満たされる）。
static inline bool lane_stuck(int i) {
  return s_block_since[i] != 0 && (now_us() - s_block_since[i]) >= STICK_US;
}
// マスク内のどこかが張り付き中か（Beamバーのライブ判定用）
static bool any_stuck() {
  for (int i = 0; i < LANES; i++) if (lane_on(i) && lane_stuck(i)) return true;
  return false;
}
// マスク内の全レーンが張り付き中か（=TX共通系〈LED電源/38kHz〉の故障疑い）
static bool all_stuck() {
  bool any = false;
  for (int i = 0; i < LANES; i++) {
    if (!lane_on(i)) continue;
    any = true;
    if (!lane_stuck(i)) return false;
  }
  return any;
}
// 最初に見つかった張り付きレーンをライブで返す（READY常時監視用・20260830f）。
//  戻り値：true=張り付きあり。lane=1..3、miss=1(Aのみ)/2(Bのみ)/3(両方)。
static bool first_stuck(uint8_t& lane, uint8_t& miss) {
  for (int i = 0; i < LANES; i++) {
    if (!lane_on(i) || !lane_stuck(i)) continue;
    bool a_blk = raw_a_blocked(i), b_blk = raw_b_blocked(i);
    lane = (uint8_t)(i + 1);
    miss = (a_blk && b_blk) ? 3 : (a_blk ? 1 : 2);
    return true;
  }
  return false;
}

// ---- ポーリング回収（loopから毎回呼ぶ）------------------------------------
//  どこか1レーンで通過が確定したら true と Hit を返す。複数同時は次回以降で回収。
static bool poll(Hit& out) {
  // A1張り付きを先に判定（異常は通過確定より優先して知らせる）
  if (poll_stick(out)) return true;

  for (int i = 0; i < LANES; i++) {
    if (!lane_on(i)) continue;            // マスク外レーンは見ない（20260830c）
    noInterrupts();
    uint64_t a = s_a_edge[i], b = s_b_edge[i];
    interrupts();

    if (a == 0 && b == 0) continue;

    // 20260831h：不応期中（直前の対確定直後）は、対の残り物や車体残響を新規通過に
    //   しない。エッジを読み捨ててクリアし、二重カウント／誤レーン記録を止める。
    if (now_us() < s_refractory_until[i]) {
      noInterrupts(); s_a_edge[i] = 0; s_b_edge[i] = 0; interrupts();
      continue;
    }

    // 対待ち：先に来たエッジ（A優先。無ければB）を起点に PAIR_WAIT_US 窓で相方を待つ。
    //   窓内に両方揃えば have_both、窓を過ぎたら片ビーム(q=1)で確定（Aが無ければBで代用）。
    uint64_t first = a ? a : b;
    bool have_both = (a != 0 && b != 0);
    if (!have_both && (now_us() - first) < PAIR_WAIT_US) continue; // まだ対を待つ

    out.lane    = (uint8_t)(i + 1);
    out.t_a_us  = a;
    out.t_b_us  = b;
    out.quality = have_both ? 0 : 1;
    out.miss    = have_both ? 0 : (a ? 2 : 1);   // Aのみ有=B欠け(2) / Bのみ有=A欠け(1)

    // この通過を確定。両edgeをクリアし、不応期を張って以後の残響を締め出す（20260831h）。
    noInterrupts();
    s_a_edge[i] = 0; s_b_edge[i] = 0;
    interrupts();
    s_refractory_until[i] = now_us() + REFRACTORY_US;
    return true;
  }
  return false;
}

} // namespace beam
