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

#ifndef BEAM_DIAG
#define BEAM_DIAG 0      // ★20261120 1=診断版（A/B遮光時間を [DIAG] でシリアル出力）。通常は0
#endif
#if BEAM_DIAG
#include "soc/soc.h"
#include "soc/gpio_reg.h"
#endif

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
// 20260831k：速度域は 1〜10m/s（ユーザー確定）。最高10m/s→A→B 3ms／最低1m/s→A→B 30ms。
//   対待ちは最低速側で決まる。40ms=30ms+余裕10ms（どんなに遅くても速度計測を成立させる）。
//   通過中ラッチ(CLEAR_US=70ms・改修⑤)＞40ms・地点間最短(MIN_LAP_US=1s)≫40ms で整合済み。
static constexpr uint32_t PAIR_WAIT_US = 40000;  // A→B待ち猶予(30mm/1m/s=30ms+余裕)。超えたら片ビーム(q=1)
// 20260904 改修⑤（案B・ビーム開ラッチ）：旧・固定不応期(REFRACTORY_US=100ms)を廃止。
//   固定時間だと低速(1〜1.5m/s)で車体長(150mm)を覆いきれず、車体後部の隙間や空洞シャーシ
//   (実測：150mm中10mm前後の空洞)の後端エッジを「別の通過」＝ファントムとして誤確定していた
//   （TFTはMIN_LAP 1sで弾くがspool→サーバーには幽霊イベントが流入＝誤レーン/幽霊周回の根）。
//   対策：先頭ペア確定後そのレーンを「通過中」ラッチし、生ビームが CLEAR_US 連続で開くまで
//   再武装しない。空洞の隙間で一瞬開いても再遮断で計測をリセット＝1台を1通過に束ねる。
//   速度に自動追従（速い車は即再武装／遅い車は通過し切るまで保持）。
//   CLEAR_US=70ms 根拠：空洞10mm÷最低速1m/s=10ms開きの7倍マージン。70ms開くには70mm@1m/s
//   必要＝空洞では起こり得ない。次周のMIN_LAP_US(1s)≫70msで正規通過は一切潰さない。
static constexpr uint32_t CLEAR_US = 70000;  // 70ms（通過中ラッチの再武装しきい値・案B）
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
  bool     reverse;   // 逆走（B→A順）＝TFT計測対象外。20260831k。両ビーム取得時のみ判定可
};

// ---- 張り付き(A1)監視用の状態（レーンごと）--------------------------------
//  遮断が始まった時刻を保持。STICK_US 継続で quality=2 を1回発火。復帰で解除。
static uint64_t s_block_since[LANES] = {0};   // 0=非遮断 / 非0=遮断開始µs
static bool     s_stick_fired[LANES] = {false}; // このブロック区間で発火済みか
// 20260904 改修⑤（案B）：通過中ラッチ。先頭ペア確定でONにし、生ビームが CLEAR_US 連続で
//   開くまで再武装しない。ラッチ中に溜まったエッジ（同一車の残響・空洞後端）は捨てる。
static bool     s_pass_latched[LANES] = {false};  // true=通過中（再武装待ち）
static uint64_t s_open_since[LANES]   = {0};       // 生ビーム両方openになった時刻µs（0=まだ遮断中）

// ---- ISR用の生状態（レーンごと・volatile）---------------------------------
static volatile uint64_t s_a_edge[LANES] = {0};
static volatile uint64_t s_b_edge[LANES] = {0};
static volatile uint64_t s_a_last[LANES] = {0};
static volatile uint64_t s_b_last[LANES] = {0};

static inline uint64_t now_us() { return (uint64_t)esp_timer_get_time(); }

// ============================================================================
// ★20261120 診断版（BEAM_DIAG=1 のときだけ有効・通常ビルドは従来と同一のコード）
//   目的：1回の通過で A と B の「遮光時間」が一致しているかを見る（速い値が本物か、Bの早切れかの判定）。
//   ・遮光の始まり(FALLING)の打刻・通過確定・送信は従来と同じロジックのまま（下の isr_a/isr_b 参照）。
//   ・追加で、戻り(RISING)の時刻と、通過中に何回切れたか（穴またぎの回数）を記録し、
//     通過が終わった（再武装した）瞬間に [DIAG] 1行をシリアルへ出す。
//   ・ISRは CHANGE で張り、ISR内でピンのレベルを直接レジスタから読んで「切れた/戻った」を判別する。
// ============================================================================
#if BEAM_DIAG
// ピン番号はコンパイル時定数で渡す（ISRがフラッシュ上の定数表を読まないように）
static constexpr int diag_pin_a(int l) { return l == 0 ? 32 : (l == 1 ? 33 : 34); }   // = PIN_A
static constexpr int diag_pin_b(int l) { return l == 0 ? 19 : (l == 1 ? 21 : 22); }   // = PIN_B
static inline bool IRAM_ATTR diag_pin_low(int pin) {
  uint32_t v = (pin < 32) ? REG_READ(GPIO_IN_REG) : REG_READ(GPIO_IN1_REG);
  return ((v >> (pin & 31)) & 1u) == 0;   // 74HC14先：遮断=LOW
}
struct DiagBeam {
  uint64_t fall0;   // この通過で最初に切れた時刻
  uint64_t rise0;   // 最初の遮光が戻った時刻（＝1回目の遮光の長さ）
  uint64_t rise_last; // 最後に戻った時刻（＝通過全体の長さ）
  uint16_t falls;   // 切れた回数（穴またぎ・ノイズなら2以上）
};
static volatile DiagBeam s_dA[LANES] = {};
static volatile DiagBeam s_dB[LANES] = {};
static inline void IRAM_ATTR diag_edge(volatile DiagBeam& d, bool low, uint64_t t) {
  if (low) { if (d.fall0 == 0) d.fall0 = t; if (d.falls < 0xFFFF) d.falls++; }
  else if (d.fall0 != 0) { if (d.rise0 == 0) d.rise0 = t; d.rise_last = t; }
}
static void diag_reset(int i) {
  noInterrupts();
  s_dA[i].fall0 = 0; s_dA[i].rise0 = 0; s_dA[i].rise_last = 0; s_dA[i].falls = 0;
  s_dB[i].fall0 = 0; s_dB[i].rise0 = 0; s_dB[i].rise_last = 0; s_dB[i].falls = 0;
  interrupts();
}
// 通過が終わった（再武装した）ときに1行出す。単位 µs。速度は 30mm 前提の参考値。
//   dAB = B最初の切れ − A最初の切れ（サーバーの速度計算に使う時間差と同じ）
//   wA/wB = 1回目の遮光の長さ、WA/WB = 最初に切れてから最後に戻るまで、nA/nB = 切れた回数
static void diag_print(int i) {
  noInterrupts();
  DiagBeam a = {s_dA[i].fall0, s_dA[i].rise0, s_dA[i].rise_last, s_dA[i].falls};
  DiagBeam b = {s_dB[i].fall0, s_dB[i].rise0, s_dB[i].rise_last, s_dB[i].falls};
  interrupts();
  long dab = (a.fall0 && b.fall0) ? (long)((int64_t)b.fall0 - (int64_t)a.fall0) : 0;
  long wA = (a.fall0 && a.rise0) ? (long)(a.rise0 - a.fall0) : 0;
  long wB = (b.fall0 && b.rise0) ? (long)(b.rise0 - b.fall0) : 0;
  long WA = (a.fall0 && a.rise_last) ? (long)(a.rise_last - a.fall0) : 0;
  long WB = (b.fall0 && b.rise_last) ? (long)(b.rise_last - b.fall0) : 0;
  float kmh = (dab > 0) ? (30.0f / 1000.0f) / (dab / 1e6f) * 3.6f : 0.0f;
  float ratio = (wA > 0 && wB > 0) ? (float)wB / (float)wA : 0.0f;
  Serial.printf("[DIAG] lane=%d dAB=%ld kmh=%.1f wA=%ld wB=%ld wB/wA=%.2f WA=%ld WB=%ld nA=%u nB=%u\n",
                i + 1, dab, kmh, wA, wB, ratio, WA, WB, a.falls, b.falls);
}
#endif

// レーンごとISR：74HC14経由で遮断=LOW。FALLINGで発火。
// テンプレートで3レーン分を生成（引数なしISRにレーン番号を焼き込む）。
template <int L> static void IRAM_ATTR isr_a() {
  uint64_t t = now_us();
#if BEAM_DIAG
  bool low = diag_pin_low(diag_pin_a(L));
  diag_edge(s_dA[L], low, t);
  if (!low) return;                 // 戻り(RISING)は記録だけ。以下は従来の FALLING 処理そのまま
#endif
  if (t - s_a_last[L] < DEBOUNCE_US) return;
  s_a_last[L] = t;
  if (s_a_edge[L] == 0) s_a_edge[L] = t;
}
template <int L> static void IRAM_ATTR isr_b() {
  uint64_t t = now_us();
#if BEAM_DIAG
  bool low = diag_pin_low(diag_pin_b(L));
  diag_edge(s_dB[L], low, t);
  if (!low) return;                 // 戻り(RISING)は記録だけ。以下は従来の FALLING 処理そのまま
#endif
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
  //   ★20261120 診断版(BEAM_DIAG=1)だけ CHANGE（戻りの時刻も取る）。通常ビルドは FALLING のまま。
#if BEAM_DIAG
#define BEAM_ISR_MODE CHANGE
  Serial.println("[BEAM] DIAG build: A/B block widths are logged as [DIAG]");
#else
#define BEAM_ISR_MODE FALLING
#endif
  if (lane_on(0)) { attachInterrupt(digitalPinToInterrupt(PIN_A[0]), isr_a<0>, BEAM_ISR_MODE);
                    attachInterrupt(digitalPinToInterrupt(PIN_B[0]), isr_b<0>, BEAM_ISR_MODE); }
  if (lane_on(1)) { attachInterrupt(digitalPinToInterrupt(PIN_A[1]), isr_a<1>, BEAM_ISR_MODE);
                    attachInterrupt(digitalPinToInterrupt(PIN_B[1]), isr_b<1>, BEAM_ISR_MODE); }
  if (lane_on(2)) { attachInterrupt(digitalPinToInterrupt(PIN_A[2]), isr_a<2>, BEAM_ISR_MODE);
                    attachInterrupt(digitalPinToInterrupt(PIN_B[2]), isr_b<2>, BEAM_ISR_MODE); }
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
      out.reverse = false;                // 張り付きは逆走ではない（20260831k）
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

  uint64_t t = now_us();
  for (int i = 0; i < LANES; i++) {
    if (!lane_on(i)) continue;            // マスク外レーンは見ない（20260830c）

    // 20260904 改修⑤（案B）：通過中ラッチ。先頭ペア確定後、生ビームが CLEAR_US 連続で
    //   開くまで再武装しない。空洞シャーシの隙間で一瞬開いても、その後の再遮断で
    //   open_since をリセット＝1台を1通過に束ねる（速度に自動追従）。
    if (s_pass_latched[i]) {
      bool a_blk = raw_a_blocked(i), b_blk = raw_b_blocked(i);
      if (a_blk || b_blk) {
        s_open_since[i] = 0;                        // まだ車体（or 空洞後端）がいる→開き計測やり直し
      } else if (s_open_since[i] == 0) {
        s_open_since[i] = t;                        // 両方開いた瞬間から連続開を計測開始
      } else if (t - s_open_since[i] >= CLEAR_US) {
        s_pass_latched[i] = false; s_open_since[i] = 0;   // 連続開が続いた→再武装
#if BEAM_DIAG
        diag_print(i); diag_reset(i);                     // ★診断：通過が終わった時点で1行出す
#endif
      }
      // ラッチ中に溜まったエッジは通過ではない（同一車の残響・空洞）→読み捨てる
      noInterrupts(); s_a_edge[i] = 0; s_b_edge[i] = 0; interrupts();
      continue;
    }

    noInterrupts();
    uint64_t a = s_a_edge[i], b = s_b_edge[i];
    interrupts();

    if (a == 0 && b == 0) continue;

    // 対待ち：先に来たエッジ（A優先。無ければB）を起点に PAIR_WAIT_US 窓で相方を待つ。
    //   窓内に両方揃えば have_both、窓を過ぎたら片ビーム(q=1)で確定（Aが無ければBで代用）。
    uint64_t first = a ? a : b;
    bool have_both = (a != 0 && b != 0);
    if (!have_both && (t - first) < PAIR_WAIT_US) continue; // まだ対を待つ

    out.lane    = (uint8_t)(i + 1);
    out.t_a_us  = a;
    out.t_b_us  = b;
    out.quality = have_both ? 0 : 1;
    out.miss    = have_both ? 0 : (a ? 2 : 1);   // Aのみ有=B欠け(2) / Bのみ有=A欠け(1)
    // 逆走判定（20260831k）：両ビーム取得時のみ。物理的にB素子が先に遮断＝B→A順なら逆走。
    //   正走は A先→B後（t_a<t_b）。逆走は t_b<t_a。片ビーム(q=1)は順序不明＝判定しない(false)。
    //   TFT計測はGW側で reverse を見て除外。生データ(spool)はそのままPUSH（アプリで再判定）。
    out.reverse = (have_both && b < a);

    // この通過を確定。両edgeをクリアし、通過中ラッチON（再武装はビーム連続開で・案B）。
    noInterrupts();
    s_a_edge[i] = 0; s_b_edge[i] = 0;
    interrupts();
    s_pass_latched[i] = true; s_open_since[i] = 0;
    return true;
  }
  return false;
}

// ---- 20260904 改修④①：ビーム状態の全クリア（RESETで残留を一掃）------------
//  RESET(灰)直後に、残留エッジ・進行中の張り付きタイマ・通過中ラッチが新しい内部レースを
//  誤って起動させないよう、全レーンの検出状態をリセットする。on_reset_pressed から呼ぶ。
static void flush() {
  noInterrupts();
  for (int i = 0; i < LANES; i++) { s_a_edge[i] = 0; s_b_edge[i] = 0; }
  interrupts();
  for (int i = 0; i < LANES; i++) {
    s_block_since[i]  = 0;
    s_stick_fired[i]  = false;
    s_pass_latched[i] = false;
    s_open_since[i]   = 0;
#if BEAM_DIAG
    diag_reset(i);
#endif
  }
}

} // namespace beam
