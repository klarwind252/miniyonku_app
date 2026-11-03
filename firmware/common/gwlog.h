// firmware/common/gwlog.h
// ============================================================================
//  M4LAPS 全ログ記録（GW・SE共通・20261102）
//  ねらい：Serial に出している全ログを「残す」。
//    ・各行の先頭に [経過秒.ms 状態] を付ける（例 "[   123.456 R] [SEQ] GREEN ..."）
//    ・GW（LOG_TO_FS=1）：全行をRAMリング（PSRAM 256KB／無ければ内蔵16KB）に溜め、
//      netタスクが「計測中でない時」だけ LittleFS の /gwlog.txt へ追記（計測への影響なし）。
//      /gwlog.txt が LOG_FILE_MAX を超えたら /gwlog.1.txt へ回転（最大2世代≈512KB）。
//      PCからシリアルで  log      → 全ログを出力（古い順）
//                        logtail  → 末尾8KBだけ
//                        logclear → ファイル削除
//    ・SE（LOG_TO_FS=0）：時刻プレフィックスのみ（ファイル無し。PC側の log2file で残す）
//  使い方：main.cpp で <Arduino.h>（GWは <LittleFS.h> も）の後・共通ヘッダの前に include する。
//    ヘッダ末尾で `#define Serial gwlog::out` するため、以降の Serial.printf/println は
//    すべて本クラスを通る（共通ヘッダ内の Serial も同じ翻訳単位なので捕捉される）。
//  ⚠ 既存の Serial 呼び出しは一切書き換え不要。プロトコル・ピン・挙動は不変。
// ============================================================================
#pragma once
#include <Arduino.h>
#include <stdarg.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_system.h"
#ifndef LOG_TO_FS
#define LOG_TO_FS 0
#endif
#if LOG_TO_FS
#include <LittleFS.h>
#endif

namespace gwlog {

static HardwareSerial& real = Serial;          // ★ #define の前に実体を捕まえておく

using StateFn = char (*)();                    // 現在状態を1文字で返す（GW: I/R/F 等。未設定は '-'）
static StateFn s_state_fn = nullptr;
static inline void set_state_fn(StateFn f) { s_state_fn = f; }

static SemaphoreHandle_t s_mtx = nullptr;      // 行の混線防止（複数タスクから書く）
static bool s_line_start = true;

#if LOG_TO_FS
static constexpr size_t   RING_PSRAM   = 256 * 1024;
static constexpr size_t   RING_SMALL   = 16 * 1024;
static constexpr size_t   LOG_FILE_MAX = 256 * 1024;   // /gwlog.txt がこれを超えたら回転
static constexpr const char* LOG_FILE  = "/gwlog.txt";
static constexpr const char* LOG_FILE1 = "/gwlog.1.txt";
static char*   s_ring = nullptr;
static size_t  s_cap  = 0;
static size_t  s_head = 0;        // 次に書く位置
static size_t  s_pend = 0;        // 未保存バイト数（最大 s_cap）
static uint32_t s_dropped = 0;    // 保存が追いつかず捨てた回数
static portMUX_TYPE s_ring_mux = portMUX_INITIALIZER_UNLOCKED;

static inline void ring_put(const uint8_t* b, size_t n) {
  if (!s_ring || n == 0) return;
  portENTER_CRITICAL(&s_ring_mux);
  for (size_t i = 0; i < n; i++) {
    s_ring[s_head] = (char)b[i];
    s_head = (s_head + 1) % s_cap;
    if (s_pend < s_cap) s_pend++; else s_dropped++;
  }
  portEXIT_CRITICAL(&s_ring_mux);
}
#endif

class Log : public Print {
 public:
  void begin(unsigned long baud) {
    real.begin(baud);
    if (!s_mtx) s_mtx = xSemaphoreCreateMutex();
#if LOG_TO_FS
    if (!s_ring) {
      s_ring = (char*)ps_malloc(RING_PSRAM);
      if (s_ring) s_cap = RING_PSRAM;
      else { s_ring = (char*)malloc(RING_SMALL); s_cap = s_ring ? RING_SMALL : 0; }
    }
#endif
  }
  void flush() { real.flush(); }
  int  available() { return real.available(); }
  int  read()      { return real.read(); }
  size_t write(uint8_t c) override { return write(&c, 1); }
  size_t write(const uint8_t* buf, size_t n) override {
    bool locked = s_mtx && xSemaphoreTake(s_mtx, pdMS_TO_TICKS(50)) == pdTRUE;
    size_t i = 0;
    while (i < n) {
      if (s_line_start) { emit_prefix(); s_line_start = false; }
      size_t j = i;
      while (j < n && buf[j] != '\n') j++;
      if (j < n) j++;                      // 改行まで含める
      out_raw(buf + i, j - i);
      if (buf[j - 1] == '\n') s_line_start = true;
      i = j;
    }
    if (locked) xSemaphoreGive(s_mtx);
    return n;
  }

 private:
  void out_raw(const uint8_t* b, size_t n) {
    real.write(b, n);
#if LOG_TO_FS
    ring_put(b, n);
#endif
  }
  void emit_prefix() {
    char p[24];
    uint32_t ms = millis();
    char st = s_state_fn ? s_state_fn() : '-';
    int n = snprintf(p, sizeof(p), "[%7lu.%03lu %c] ", (unsigned long)(ms / 1000), (unsigned long)(ms % 1000), st);
    if (n > 0) out_raw((const uint8_t*)p, (size_t)n);
  }
};

static Log out;

#if LOG_TO_FS
// ---- netタスクから呼ぶ：未保存ぶんを /gwlog.txt へ追記（allowed=false なら何もしない）----
//   1KB溜まるか5秒経ったら書く。1回の書込みは最大8KB（長く握らない）。
static inline void persist(bool allowed) {
  if (!allowed || !s_ring) return;
  static uint32_t last = 0;
  uint32_t now = millis();
  size_t pend;
  portENTER_CRITICAL(&s_ring_mux); pend = s_pend; portEXIT_CRITICAL(&s_ring_mux);
  if (pend == 0) return;
  if (pend < 1024 && (now - last) < 5000) return;
  last = now;
  // 回転
  File f = LittleFS.open(LOG_FILE, FILE_APPEND);
  if (f && f.size() > LOG_FILE_MAX) {
    f.close();
    LittleFS.remove(LOG_FILE1);
    LittleFS.rename(LOG_FILE, LOG_FILE1);
    f = LittleFS.open(LOG_FILE, FILE_APPEND);
  }
  if (!f) return;
  static uint8_t chunk[1024];
  size_t budget = 8192;
  while (budget > 0) {
    size_t n = 0;
    portENTER_CRITICAL(&s_ring_mux);
    size_t avail = s_pend;
    if (avail > sizeof(chunk)) avail = sizeof(chunk);
    size_t start = (s_head + s_cap - s_pend) % s_cap;
    for (; n < avail; n++) chunk[n] = (uint8_t)s_ring[(start + n) % s_cap];
    s_pend -= n;
    portEXIT_CRITICAL(&s_ring_mux);
    if (n == 0) break;
    f.write(chunk, n);
    budget = (budget > n) ? budget - n : 0;
  }
  f.close();
}

// ---- loopから呼ぶ：シリアル入力コマンド（log / logtail / logclear）----
static inline void dump_file(const char* name, size_t tail_bytes = 0) {
  File f = LittleFS.open(name, FILE_READ);
  if (!f) return;
  if (tail_bytes && f.size() > tail_bytes) f.seek(f.size() - tail_bytes);
  static uint8_t buf[512];
  while (f.available()) { int n = f.read(buf, sizeof(buf)); if (n <= 0) break; real.write(buf, n); }
  f.close();
}
// リングの未保存ぶんをそのまま出力（消費しない・persistと競合しないよう読むだけ）
static inline void dump_ring() {
  if (!s_ring) return;
  static uint8_t buf[512];
  size_t off = 0;
  for (;;) {
    size_t n = 0;
    portENTER_CRITICAL(&s_ring_mux);
    size_t pend = s_pend;
    if (off < pend) {
      size_t start = (s_head + s_cap - pend + off) % s_cap;
      size_t avail = pend - off; if (avail > sizeof(buf)) avail = sizeof(buf);
      for (; n < avail; n++) buf[n] = (uint8_t)s_ring[(start + n) % s_cap];
    }
    portEXIT_CRITICAL(&s_ring_mux);
    if (n == 0) break;
    real.write(buf, n); off += n;
  }
}
static inline void service_serial() {
  static char line[16]; static uint8_t len = 0;
  while (real.available()) {
    int c = real.read();
    if (c == '\r' || c == '\n') {
      line[len] = 0;
      if (len) {
        if (!strcmp(line, "log")) {          // ファイル（古→新）＋未保存のリング
          real.println("===== LOG BEGIN (old -> new) =====");
          dump_file(LOG_FILE1); dump_file(LOG_FILE); dump_ring();
          real.println("===== LOG END =====");
        } else if (!strcmp(line, "logtail")) {
          real.println("===== LOG TAIL 8KB =====");
          dump_file(LOG_FILE, 8192); dump_ring();
          real.println("===== LOG END =====");
        } else if (!strcmp(line, "logclear")) {
          LittleFS.remove(LOG_FILE); LittleFS.remove(LOG_FILE1);
          real.println("[LOG] cleared");
        } else if (!strcmp(line, "logstat")) {
          File f = LittleFS.open(LOG_FILE, FILE_READ); size_t a = f ? f.size() : 0; if (f) f.close();
          File g = LittleFS.open(LOG_FILE1, FILE_READ); size_t b = g ? g.size() : 0; if (g) g.close();
          real.printf("[LOG] ring=%u/%u pend=%u dropped=%lu file=%u old=%u\n",
                      (unsigned)s_pend, (unsigned)s_cap, (unsigned)s_pend, (unsigned long)s_dropped, (unsigned)a, (unsigned)b);
        }
      }
      len = 0;
    } else if (len < sizeof(line) - 1 && c >= 0x20) {
      line[len++] = (char)c;
    }
  }
}
#endif

// 起動理由を1行残す（setup冒頭で呼ぶ）
static inline void log_boot(const char* who) {
  const char* r = "?";
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: r = "POWERON"; break;  case ESP_RST_SW: r = "SW"; break;
    case ESP_RST_PANIC: r = "PANIC"; break;      case ESP_RST_INT_WDT: r = "INT_WDT"; break;
    case ESP_RST_TASK_WDT: r = "TASK_WDT"; break; case ESP_RST_WDT: r = "WDT"; break;
    case ESP_RST_BROWNOUT: r = "BROWNOUT"; break; case ESP_RST_DEEPSLEEP: r = "DEEPSLEEP"; break;
    default: break;
  }
  out.printf("==== BOOT %s reset=%s ====\n", who, r);
}

} // namespace gwlog

#define Serial gwlog::out
