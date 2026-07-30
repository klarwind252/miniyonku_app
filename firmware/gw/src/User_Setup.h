// firmware/gw/src/User_Setup.h
// ============================================================================
//  M4LAPS GW用 TFT_eSPI 設定（docs/02 のVE用ピン割当に一致）
//  パネル：MSP2807 = ILI9341 320x240・SPI・横向き運用。
//  ⚠ タッチ・SDは使わない（MISO=GPIO19 はビームB L1と共用のため）。
//  ⚠ この設定を使うには platformio.ini で
//       build_flags = -DUSER_SETUP_LOADED=1 -include src/User_Setup.h
//     のように読み込ませる（TFT_eSPI標準のUser_Setup_Select.hを回避）。
// ============================================================================
#pragma once

// ---- ドライバ ----
#define ILI9341_DRIVER

// ---- パネル解像度（TFT_eSPI内部は縦持ち基準。横向きはsetRotationで回す）----
#define TFT_WIDTH  240
#define TFT_HEIGHT 320

// ---- ピン割当（docs/02 確定・VE用）----
//  SCK=18 / MOSI=23 / CS=5 / DC=13 / RST=14
//  MISO は使わない（-1）。ビームB(GPIO19)と衝突するため読み出ししない。
#define TFT_MISO -1
#define TFT_MOSI 23
#define TFT_SCLK 18
#define TFT_CS    5
#define TFT_DC   13
#define TFT_RST  14
// バックライト(LED/BL)は3.3V直結（docs/02）。ソフト制御しないので未定義。

// ---- フォント（GLCD/2/4/6/7/8＋GFXFF有効）----
#define LOAD_GLCD
#define LOAD_FONT2
#define LOAD_FONT4
#define LOAD_FONT6
#define LOAD_FONT7
#define LOAD_FONT8
#define LOAD_GFXFF
#define SMOOTH_FONT

// ---- SPI速度 ----
//  ILI9341は40MHz常用可。読み出しは使わないのでREADは低め。
#define SPI_FREQUENCY       10000000
#define SPI_READ_FREQUENCY  20000000
