// firmware/common/display.h
// ============================================================================
//  M4LAPS GW TFT表示モジュール（docs/20・20章追記＝文言確定 準拠）
//  パネル：ILI9341 320x240 横向き（setRotation(1)）。PSRAMスプライトで裏描き→転送。
//  画面：待機(IDLE) / 計測中(ON TRACK) / 計測終了(FINISH) / エラー。
//  共通：最下部28pxステータスバー。×が1つでもあればバー全体を赤。
//
//  ⚠実機なしのため座標・フォント・折り返しは docs/20.6 のとおり実機で追い込む前提。
//    本モジュールは「型・状態切替・確定エラー文言」までを実装（see docs/20章追記）。
//  ⚠この時点では当日ベストAPI(20.2)・ラップテーブル(20.3/20.4)の中身描画は
//    プレースホルダ。数値の流し込みは残課題#8（レイアウト動的送信）とセットで拡張する。
// ============================================================================
#pragma once
#include <Arduino.h>
#include <TFT_eSPI.h>

namespace disp {

// ---- 色（docs/20.1 レーン色・状態色）--------------------------------------
static constexpr uint16_t C_BLACK  = 0x0000;
static constexpr uint16_t C_WHITE  = 0xFFFF;
static constexpr uint16_t C_READY  = 0x27F6;   // #2ED9B0 ブルーグリーン(近似)
static constexpr uint16_t C_ONTRK  = 0xE229;   // #E24B4A 赤(L4/ON TRACK)
static constexpr uint16_t C_FINISH = 0xEE80;   // #EFD000 黄(FINISH/警告文字)
static constexpr uint16_t C_L1     = 0x2C7C;   // #2E8FE6 青
static constexpr uint16_t C_L2     = 0x3DA9;   // #39B54A 緑
static constexpr uint16_t C_L3     = 0xEE80;   // #EFD000 黄
static constexpr uint16_t C_L4     = 0xE229;   // #E24B4A 赤
static constexpr uint16_t C_L5     = 0xFFFF;   // #FFFFFF 白
static constexpr uint16_t C_BARERR = 0xC000;   // エラー時バー背景（暗め赤）
static constexpr uint16_t C_GREY   = 0x8410;   // 灰（周番号など）

static inline uint16_t lane_color(int lane1) {
  switch (lane1) { case 1: return C_L1; case 2: return C_L2; case 3: return C_L3;
                   case 4: return C_L4; case 5: return C_L5; default: return C_WHITE; }
}

// ---- 画面サイズ ----
static constexpr int W = 320, H = 240;
static constexpr int BAR_H = 28;                 // ステータスバー高（docs/20.1）
static constexpr int BODY_H = H - BAR_H;

// ---- ステータス状態（バーの○×判定に使う）---------------------------------
struct Status {
  uint8_t gw_id   = 6;
  uint8_t ch      = 2;
  bool wifi_ok    = false;
  int  node_have  = 0;      // 実JOIN数
  int  node_need  = 0;      // レイアウト割当数（0なら「-」表示）
  bool beam_ok    = false;
  int  unsent     = 0;      // 未送信件数（0で○）
  bool gw_dup     = false;  // GW2台検知（true=異常）
};

// ---- エラー種別（docs/20.5 優先順位）---------------------------------------
enum ErrKind { ERR_GW_DUP=0, ERR_SEND_FAIL, ERR_NODE_LOST, ERR_BEAM_CUT };

struct ErrItem {
  ErrKind kind;
  int  arg_i = 0;        // ノードID or レーン番号 or 保持件数
  char label[16] = {0};  // "SQ3" / "L2" 等（該当時）
};

// ---- 共有状態（main側が随時更新する。バー描画がこれを読む）----------------
static Status g_status;

// ---- 描画本体 --------------------------------------------------------------
static TFT_eSPI    s_tft;
static TFT_eSprite s_spr = TFT_eSprite(&s_tft);   // PSRAMフルフレーム裏バッファ

static bool s_ok = false;

static void begin() {
  s_tft.init();
  s_tft.setRotation(1);           // 横向き 320x240
  s_tft.fillScreen(C_BLACK);
  // PSRAMに 320x240x16bit(=150KB) スプライト確保（docs/02・20.6）
  s_spr.setColorDepth(16);
  void* p = s_spr.createSprite(W, H);
  s_ok = (p != nullptr);
  if (!s_ok) { Serial.println("[TFT] sprite確保失敗（PSRAM未有効？）"); }
}
static inline bool ready() { return s_ok; }

// ===== 共通：ステータスバー（全画面の最下部・docs/20.1）=====================
//  ○判定：WiFi接続 / NODE充足(need>0 && have==need) / ビーム全数 / 未送信0。
//  GW番号・chは固定値＝○×なし（20.1）。×が1つでもあればバー全体赤。
static void draw_status_bar() {
  Status& st = g_status;   // 共有状態（main側が更新）
  bool node_ok = (st.node_need > 0) && (st.node_have == st.node_need);
  bool send_ok = (st.unsent == 0);
  bool any_ng  = (!st.wifi_ok) || (!node_ok) || (!st.beam_ok) || (!send_ok) || st.gw_dup;

  uint16_t bg = any_ng ? C_BARERR : C_BLACK;
  s_spr.fillRect(0, H - BAR_H, W, BAR_H, bg);
  s_spr.setTextColor(C_WHITE, bg);
  s_spr.setTextDatum(ML_DATUM);
  s_spr.setTextFont(2);

  int y = H - BAR_H/2;
  int x = 4;
  char buf[24];

  // GW番号（○×なし）
  snprintf(buf, sizeof(buf), "GW%u", st.gw_id);
  s_spr.drawString(buf, x, y); x += 40;
  // ch（○×なし）
  snprintf(buf, sizeof(buf), "ch%u", st.ch);
  s_spr.drawString(buf, x, y); x += 40;
  // WiFi（○×は実機で記号調整。ここではO/xで表現）
  s_spr.drawString(st.wifi_ok ? "WiFi O" : "WiFi x", x, y); x += 60;
  // NODE
  if (st.node_need > 0) snprintf(buf, sizeof(buf), "NODE %d/%d %s", st.node_have, st.node_need, node_ok?"O":"x");
  else                  snprintf(buf, sizeof(buf), "NODE -/-");
  s_spr.drawString(buf, x, y); x += 96;
  // ビーム
  s_spr.drawString(st.beam_ok ? "beam O" : "beam x", x, y); x += 60;
  // 送信 or GW重複（GW重複は最重要なので、その時だけ差し替え表示）
  if (st.gw_dup) {
    s_spr.drawString("GWdup x", x, y);
  } else {
    snprintf(buf, sizeof(buf), "send%d %s", st.unsent, send_ok?"O":"x");
    s_spr.drawString(buf, x, y);
  }
}

// ===== 待機(IDLE)画面（docs/20.2）==========================================
//  当日ベスト4行はプレースホルダ（WiFi切れ時は「-」）。数値流し込みは後続。
static void draw_idle() {
  s_spr.fillRect(0, 0, W, BODY_H, C_BLACK);
  s_spr.setTextDatum(TL_DATUM);
  s_spr.setTextColor(C_READY, C_BLACK);
  s_spr.setTextFont(4);
  s_spr.drawString("READY", 8, 8);

  s_spr.setTextColor(C_WHITE, C_BLACK);
  s_spr.setTextFont(2);
  s_spr.setTextDatum(TR_DATUM);
  s_spr.drawString("TODAY BEST", W - 8, 12);

  // ベスト4行（Total/Av./Lap/Max）。実データはAPI取得後に差し替え。
  const char* rows[4] = {"Total", "Av.", "Lap", "Max"};
  s_spr.setTextDatum(TL_DATUM);
  int y = 60;
  for (int i = 0; i < 4; i++) {
    s_spr.setTextFont(i == 0 ? 4 : 2);
    s_spr.setTextColor(C_WHITE, C_BLACK);
    s_spr.drawString(rows[i], 16, y);
    s_spr.setTextDatum(TR_DATUM);
    s_spr.drawString("-", W - 40, y);     // WiFi切れ/未取得は「-」
    s_spr.setTextDatum(TL_DATUM);
    y += (i == 0 ? 40 : 34);
  }
}

// ===== 計測中(ON TRACK)画面（docs/20.3）====================================
//  ●点滅・経過秒・3レーン枠。ラップテーブルは骨組みのみ（実データ後続）。
static void draw_ontrack(uint32_t elapsed_ms, bool blink_on, int laps) {
  s_spr.fillRect(0, 0, W, BODY_H, C_BLACK);
  // ● ON TRACK（●点滅）
  if (blink_on) s_spr.fillCircle(14, 16, 6, C_ONTRK);
  s_spr.setTextDatum(ML_DATUM);
  s_spr.setTextColor(C_ONTRK, C_BLACK);
  s_spr.setTextFont(4);
  s_spr.drawString("ON TRACK", 28, 16);
  // 経過秒（右上・大きく）
  s_spr.setTextDatum(TR_DATUM);
  s_spr.setTextColor(C_WHITE, C_BLACK);
  char t[16]; snprintf(t, sizeof(t), "%.1f", elapsed_ms / 1000.0);
  s_spr.drawString(t, W - 8, 8);

  // 3レーン枠（枠線＝レーン色）。中身のラップは実データ流し込みで拡張。
  int lane_w = (W - 16) / 3;
  for (int i = 0; i < 3; i++) {
    int x = 8 + i * lane_w;
    s_spr.drawRect(x, 40, lane_w - 4, BODY_H - 48, lane_color(i + 1));
    s_spr.setTextDatum(TL_DATUM);
    s_spr.setTextFont(2);
    char h[12]; snprintf(h, sizeof(h), "L%d", i + 1);
    s_spr.setTextColor(C_WHITE, C_BLACK);
    s_spr.drawString(h, x + 4, 44);
    s_spr.setTextDatum(TR_DATUM);
    char p[12]; snprintf(p, sizeof(p), "0/%d", laps);
    s_spr.setTextColor(lane_color(i + 1), C_BLACK);
    s_spr.drawString(p, x + lane_w - 8, 44);
  }
}

// ===== 計測終了(FINISH)画面（docs/20.4）====================================
static void draw_finish(int laps) {
  s_spr.fillRect(0, 0, W, BODY_H, C_BLACK);
  s_spr.setTextDatum(ML_DATUM);
  s_spr.fillCircle(12, 12, 5, C_FINISH);
  s_spr.setTextColor(C_FINISH, C_BLACK);
  s_spr.setTextFont(2);
  s_spr.drawString("FINISH", 24, 12);

  int lane_w = (W - 16) / 3;
  for (int i = 0; i < 3; i++) {
    int x = 8 + i * lane_w;
    s_spr.drawRect(x, 30, lane_w - 4, BODY_H - 38, lane_color(i + 1));
    s_spr.setTextDatum(TL_DATUM);
    s_spr.setTextFont(2);
    char h[12]; snprintf(h, sizeof(h), "L%d", i + 1);
    s_spr.setTextColor(C_WHITE, C_BLACK);
    s_spr.drawString(h, x + 4, 34);
    // 合計タイム（中央・大きく）はプレースホルダ
    s_spr.setTextDatum(MC_DATUM);
    s_spr.setTextFont(4);
    s_spr.drawString("--.-", x + (lane_w - 4) / 2, 70);
  }
}

// ===== エラー画面（docs/20.5・20章追記＝文言確定）==========================
//  ⚠文言の正本は docs/20章追記_エラー画面文言確定_20260728.md。
//    TFT_eSPI標準フォントは日本語グリフを持たないため、実機で日本語フォント
//    （efont/M+ 等）を導入してから、下記 MSG_* を確定日本語へ差し替える。
//    現段階はASCIIプレースホルダ＝実機なしでレイアウト・分岐・優先順位を検証する用。
//    差し替え時は「行数・改行位置」を docs の確定文言に一致させること。

// 単一エラーの本文行（ASCIIプレースホルダ。日本語はフォント導入後に差し替え）
static void err_lines_single(const ErrItem& e, const char* out[6], int& n) {
  n = 0;
  switch (e.kind) {
    case ERR_GW_DUP:      // docs 20.5.2(1) GW2台検知（最優先）
      out[n++] = "! GW x2 DETECTED";
      out[n++] = "Two GWs are running.";
      out[n++] = "Power OFF one of them.";
      break;
    case ERR_SEND_FAIL:   // docs 20.5.2(2) 送信失敗（データ保持を明記）
      out[n++] = "! SEND FAILED";
      out[n++] = "Server unreachable.";
      out[n++] = "Data is retained.";
      out[n++] = "Check WiFi / signal.";
      out[n++] = "Auto-resend on idle.";
      break;
    case ERR_NODE_LOST:   // docs 20.5.2(3) ノード離脱（label=SQ名）
      out[n++] = "! NODE NO RESPONSE";
      out[n++] = "No reply for 10s.";
      out[n++] = "1) power/battery";
      out[n++] = "2) restart";
      out[n++] = "3) reposition";
      break;
    case ERR_BEAM_CUT:    // docs 20.5.2(4) ビーム切れ（label=レーン）
      out[n++] = "! BEAM CUT";
      out[n++] = "Optical axis lost.";
      out[n++] = "1) LED aim/height";
      out[n++] = "2) clean slit";
      out[n++] = "3) swap LED";
      break;
  }
}

// 複数時の1行要約（docs 20.5.2(5) の固定順・ASCIIプレースホルダ）
static void err_summary_line(const ErrItem& e, char* out, size_t n) {
  switch (e.kind) {
    case ERR_GW_DUP:    snprintf(out, n, "- GW x2 detected (power off 1)"); break;
    case ERR_SEND_FAIL: snprintf(out, n, "- send failed (%d held)", e.arg_i); break;
    case ERR_NODE_LOST: snprintf(out, n, "- %s no response", e.label); break;
    case ERR_BEAM_CUT:  snprintf(out, n, "- %s beam cut", e.label); break;
  }
}

//  errs：優先順位順に積んだ配列。cnt=1なら単一詳細、>=2なら複数箇条書き。
static void draw_error(const ErrItem* errs, int cnt) {
  s_spr.fillRect(0, 0, W, BODY_H, C_BLACK);
  s_spr.setTextColor(C_FINISH, C_BLACK);   // 黄色文字（docs/20.5）
  s_spr.setTextDatum(TL_DATUM);

  if (cnt <= 1) {
    const char* lines[6]; int n = 0;
    if (cnt == 1) err_lines_single(errs[0], lines, n);
    int font = (n <= 5) ? 4 : 2;           // 入りきらなければ縮小（20.1）
    s_spr.setTextFont(font);
    int lh = (font == 4) ? 30 : 22;
    int y = 8;
    for (int i = 0; i < n; i++) { s_spr.drawString(lines[i], 8, y); y += lh; }
    // ノード離脱/ビーム切れは1行目右側にlabel（SQ名/レーン）を重ねて出す
    if (cnt == 1 && errs[0].label[0] &&
        (errs[0].kind == ERR_NODE_LOST || errs[0].kind == ERR_BEAM_CUT)) {
      s_spr.setTextDatum(TR_DATUM);
      s_spr.drawString(errs[0].label, W - 8, 8);
      s_spr.setTextDatum(TL_DATUM);
    }
  } else {
    s_spr.setTextFont(4);
    s_spr.drawString("! MULTIPLE ERRORS", 8, 8);
    int font = (cnt <= 4) ? 4 : 2;
    s_spr.setTextFont(font);
    int lh = (font == 4) ? 30 : 22;
    int y = 44;
    for (int i = 0; i < cnt; i++) {
      char line[48]; err_summary_line(errs[i], line, sizeof(line));
      s_spr.drawString(line, 8, y); y += lh;
    }
  }
}

// ===== 画面確定→転送 ========================================================
//  各draw_*の後に呼ぶ。ステータスバーを重ねてから一括push（ちらつき無し）。
static void commit() {
  if (!s_ok) return;
  draw_status_bar();
  s_spr.pushSprite(0, 0);
}

} // namespace disp
