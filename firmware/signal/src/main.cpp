// firmware/signal/src/main.cpp
// ============================================================================
//  ⚠⚠ 一時テストファーム（T-6切り分け用・強制点灯）⚠⚠  2026-08-25
//  これは正規版ではない。確認が済んだら正規版
//  （前回ZIP「変更分_SGブザーをブー化_20260825.zip」内の signal/main.cpp）へ戻すこと。
//
//  XIAO ESP32C3 / IRLU3410（ゲートHIGH＝点灯）。ネットワーク等は一切省略。
//    赤灯 : D0 = GPIO2
//    緑灯 : D3 = GPIO5   ← 設計値（00§4-1・04・07で一致）
//    ボタン: D1 = GPIO3   ← 緑が誤ってここに配線されていないかの確認用
//
//  ── SG_TEST_MODE で挙動を切替（数字を変えて焼き直すだけ）──
//    0 = 赤(GPIO2)＋緑(GPIO5) を両方 点きっぱなし（＝強制点灯）※既定
//    1 = GPIO2→GPIO5→GPIO3 を1本ずつ順に点灯（どのピンで緑が点くか目視特定）
//
//  ⚠ 電源：両点灯は合計約1.1A＋発熱。5V/2Aアダプタ・放熱基板付き・短時間で確認する。
//  ⚠ MODE1中はボタンを押さない（GPIO3をOUTPUT HIGHで駆動するため）。
// ============================================================================
#include <Arduino.h>

#define SG_TEST_MODE 0     // ← 0=強制点灯 / 1=ピン総当たり

static const int PIN_RED    = 2;   // D0 GPIO2 赤灯ゲート
static const int PIN_GREEN  = 5;   // D3 GPIO5 緑灯ゲート（設計）
static const int PIN_BUTTON = 3;   // D1 GPIO3 ボタン位置（緑誤配線チェック用）

static void all_low() {
  digitalWrite(PIN_RED,    LOW);
  digitalWrite(PIN_GREEN,  LOW);
  digitalWrite(PIN_BUTTON, LOW);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(PIN_RED,    OUTPUT);
  pinMode(PIN_GREEN,  OUTPUT);
  pinMode(PIN_BUTTON, OUTPUT);
  all_low();
  Serial.println("\n=== SG 一時テストファーム（強制点灯）===");
#if SG_TEST_MODE == 0
  digitalWrite(PIN_RED,   HIGH);
  digitalWrite(PIN_GREEN, HIGH);
  Serial.println("MODE0: 赤(GPIO2)＋緑(GPIO5) 強制ON（点きっぱなし）");
  Serial.println("  → 赤は点くが緑が点かない＝緑側ハード（GPIO5→100Ω→ゲート/");
  Serial.println("     IRLU3410向き/緑LED極性/2.2Ω+1Ω/S=GND）を疑う。MODE1で総当たり可。");
#else
  Serial.println("MODE1: GPIO2→GPIO5→GPIO3 を2秒ずつ順に点灯。緑が光ったピンが実配線。");
#endif
}

void loop() {
#if SG_TEST_MODE == 0
  digitalWrite(PIN_RED,   HIGH);
  digitalWrite(PIN_GREEN, HIGH);
  delay(1000);
#else
  const struct { int pin; const char* label; } seq[] = {
    { PIN_RED,    "GPIO2 (D0/赤・設計)" },
    { PIN_GREEN,  "GPIO5 (D3/緑・設計)" },
    { PIN_BUTTON, "GPIO3 (D1/ボタン位置・緑の誤配線チェック)" },
  };
  for (auto& s : seq) {
    all_low();
    digitalWrite(s.pin, HIGH);
    Serial.printf("[SWEEP] %s = HIGH\n", s.label);
    delay(2000);
  }
#endif
}
