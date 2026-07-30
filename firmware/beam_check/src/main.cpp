#include <Arduino.h>

// フェーズ3 1LED2受光の確認（ESP32-DevKitC-32E）
// GPIO25: 38kHz → SS8050 → IR LED（1個）
// GPIO32: TSSP 1個目 OUT
// GPIO33: TSSP 2個目 OUT
// TSSP: 受信中=LOW / 遮断=HIGH

#define PIN_PWM   25
#define PIN_A     32
#define PIN_B     33

void setup() {
  Serial.begin(115200);
  delay(300);

#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(PIN_PWM, 38000, 8);
  ledcWrite(PIN_PWM, 128);
#else
  ledcSetup(0, 38000, 8);
  ledcAttachPin(PIN_PWM, 0);
  ledcWrite(0, 128);
#endif

  pinMode(PIN_A, INPUT_PULLUP);
  pinMode(PIN_B, INPUT_PULLUP);

  Serial.println("\n=== Phase3 ===");
}

void loop() {
  static uint32_t lastPrint = 0;

  if (millis() - lastPrint >= 1000) {
    lastPrint = millis();
    int a = digitalRead(PIN_A);
    int b = digitalRead(PIN_B);
    Serial.printf("1個目: %s   2個目: %s\n",
                  a == LOW ? "受信 ●" : "遮断 ○",
                  b == LOW ? "受信 ●" : "遮断 ○");
  }
}