// ============================================================================
//  hwcheck_gw — 基板 受入確認用 最小ファーム（4MB検証統一版・診断専用）
//  ★検証段階：全機材を4MB基板(32E系)で統一。PSRAMは対象外。
//  合格基準: 起動する・シリアルが出る・Flashが読める（4MB前後）。
//  ⚠ 本番GW(VE/8MB+PSRAM)へ移す際は、PSRAM確認を別途行うこと（決定ログ参照）。
// ============================================================================
#include <Arduino.h>
#include "esp_system.h"
#include "esp_chip_info.h"

static void line() {
  Serial.println("--------------------------------------------------");
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  line();
  Serial.println("  基板受入確認 (hwcheck_gw / 4MB検証版)");
  line();

  esp_chip_info_t chip;
  esp_chip_info(&chip);
  Serial.printf("Chip cores     : %d\n", chip.cores);
  Serial.printf("Chip revision  : %d\n", chip.revision);
  Serial.printf("CPU frequency  : %lu MHz\n",
                (unsigned long)getCpuFrequencyMhz());

  uint32_t flash = ESP.getFlashChipSize();
  Serial.printf("Flash size     : %u bytes (%.1f MB)\n",
                (unsigned)flash, flash / 1048576.0);
  Serial.printf("Free heap      : %u bytes\n",
                (unsigned)ESP.getFreeHeap());

  line();
  bool pass = (flash >= 2 * 1024 * 1024) && (chip.cores >= 1);
  if (pass) {
    Serial.println("  判定: 合格  基板は正常に起動・通信できています");
  } else {
    Serial.println("  判定: 不合格  基板情報が読めません（接続を確認）");
  }
  line();
  Serial.println("（2秒ごとに再表示します。終了は Ctrl+] ）");
}

void loop() {
  Serial.printf("[生存] Flash=%u bytes  空きヒープ=%u bytes\n",
                (unsigned)ESP.getFlashChipSize(),
                (unsigned)ESP.getFreeHeap());
  delay(2000);
}
