#pragma once
// ============================================================
//  M4LAPS GW6/GW7 TFT設定（ILI9341 MSP2807 V1.2基板 / J1オープン=VCC 5V）
//  2026-07-30 実機検証で確定（tfttestで赤緑青+HELLO表示成功）
//  ⚠ MISOは-1（無効）：GPIO19はビームB L1と競合するため使わない。
//     表示のみで読み出し不要なのでMISO未使用で問題なし。
//  使用GPIO競合チェック済 → CS=15/DC=2/RST=4 はGW内で未使用を確認。
//     （GW占有：PWM25 / ビームA 32,33,34 / ビームB 19,21,22 / btn 26,27）
// ============================================================
#define ILI9341_DRIVER
#define TFT_WIDTH  240
#define TFT_HEIGHT 320

#define TFT_MISO -1   // ★競合回避：GPIO19はビームB L1が使用。表示のみなので-1でOK
#define TFT_MOSI 23
#define TFT_SCLK 18
#define TFT_CS   15   // 旧5から変更（実機確定値）
#define TFT_DC    2   // 旧13から変更
#define TFT_RST   4   // 旧14から変更

#define LOAD_GLCD
#define LOAD_FONT2
#define LOAD_FONT4
#define LOAD_FONT6
#define LOAD_FONT7
#define LOAD_FONT8
#define LOAD_GFXFF
#define SMOOTH_FONT

#define SPI_FREQUENCY       27000000
#define SPI_READ_FREQUENCY   6000000