// firmware/common/display.h
// ============================================================================
//  M4LAPS GW TFT表示モジュール（docs/20・20章追記＝文言確定 準拠）
//  パネル：ILI9341 320x240 横向き（setRotation(3)）。PSRAMスプライトで裏描き→転送。
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
  bool rc_dup     = false;  // RC2台検知（リモコン重複・true=異常）
  bool sg_dup     = false;  // SG2台検知（シグナル重複・true=異常）
  bool sync_ok    = true;   // 時刻同期完了（B1・24.11。false=未同期でSync x）
  bool lost       = false;  // あきらめ(give up)発生（C1・24.14。true=Lost x・灰リセットまで保持）
};

// ---- エラー種別（docs/20.5 優先順位）---------------------------------------
//  ⚠ RC/SG重複（ERR_RC_DUP/ERR_SG_DUP）は本改修で追加した新種別。
//     docs/20章追記（文言確定）にはまだ無いので、確定日本語文言は後日そこへ追記すること。
//  ⚠ A1/A2/C1（ERR_SENSOR_BOTH/ERR_SPEED_ONLY/ERR_SECTOR_COMM）は 24.5/24.6/24.14 で確定。
//     優先順位（20.5.2＋24.14）：GW2台 > 送信失敗 > ノード離脱 > ビーム切れ/A1 > A2 > SG重複 > RC重複 > C1
enum ErrKind {
  ERR_GW_DUP=0, ERR_SEND_FAIL, ERR_NODE_LOST, ERR_BEAM_CUT,
  ERR_SENSOR_BOTH,   // A1：両ビーム欠(Wセンサー不良/CO/外乱光)・24.6
  ERR_SPEED_ONLY,    // A2：片ビーム欠(速度未計測)・24.6
  ERR_RC_DUP, ERR_SG_DUP,
  ERR_SECTOR_COMM    // C1：セクター通信不良(Lost)・24.14・最後尾
};

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

// ---- 経過秒フィールド（案B：この矩形だけ高頻度で部分転送）------------------
//  ON TRACKの経過秒を、フル画面(約4fps)とは別に約30fpsで小領域だけ書き換える。
//  この小スプライトの位置/フォント/右揃えは draw_ontrack の経過秒描画と同一に
//  合わせてある（screen(312,8)基準）。フルフレームが下地、これを上に重ねる。
//  ⚠経過秒は「見せかけの表示」で、公式タイムはS/GのISR打刻(µs)で確定するため、
//    ここの描画頻度は計測精度に一切影響しない。
static constexpr int TIME_X = 186, TIME_Y = 2, TIME_W = 130, TIME_H = 38;
static TFT_eSprite s_time_spr = TFT_eSprite(&s_tft);
static bool s_time_ok = false;

static void begin() {
  s_tft.init();
  s_tft.setRotation(3);           // 横向き 320x240
  s_tft.fillScreen(C_BLACK);
  // PSRAMに 320x240x16bit(=150KB) スプライト確保（docs/02・20.6）
  s_spr.setColorDepth(16);
  void* p = s_spr.createSprite(W, H);
  s_ok = (p != nullptr);
  if (!s_ok) { Serial.println("[TFT] sprite確保失敗（PSRAM未有効？）"); }
  // 経過秒フィールド用の小スプライト（約10KB）。確保失敗時はフルフレームの
  // 4fps描画にフォールバックするだけなので致命ではない。
  s_time_spr.setColorDepth(16);
  void* pt = s_time_spr.createSprite(TIME_W, TIME_H);
  s_time_ok = (pt != nullptr);
  if (!s_time_ok) { Serial.println("[TFT] timeスプライト確保失敗（4fpsで継続）"); }
}
static inline bool ready() { return s_ok; }

// ===== 共通：ステータスバー（全画面の最下部・docs/20.1）=====================
//  ○判定：WiFi接続 / NODE充足(need>0 && have==need) / ビーム全数 / 未送信0。
//  GW番号・chは固定値＝○×なし（20.1）。×が1つでもあればバー全体赤。
static void draw_status_bar() {
  Status& st = g_status;   // 共有状態（main側が更新）
  bool node_ok = (st.node_need > 0) && (st.node_have == st.node_need);
  bool send_ok = (st.unsent == 0);
  bool any_dup = st.gw_dup || st.rc_dup || st.sg_dup;   // GW/RC/SGいずれかの重複
  bool any_ng  = (!st.wifi_ok) || (!node_ok) || (!st.beam_ok) || (!send_ok)
                 || any_dup || (!st.sync_ok) || st.lost;   // Sync×・Lost×も赤バー（24.11）

  uint16_t bg = any_ng ? C_BARERR : C_BLACK;
  s_spr.fillRect(0, H - BAR_H, W, BAR_H, bg);
  s_spr.setTextColor(C_WHITE, bg);
  s_spr.setTextDatum(ML_DATUM);
  s_spr.setTextFont(2);

  int y = H - BAR_H/2;
  int x = 2;
  char buf[24];

  // 8項目を320px幅に収めるため間隔を詰める（フォント2・ML基準）。
  //  GW ch WiFi Node Beam Send Sync Lost（24.11の並び）。
  // GW番号（○×なし）
  snprintf(buf, sizeof(buf), "GW%u", st.gw_id);
  s_spr.drawString(buf, x, y); x += 30;
  // ch（○×なし）
  snprintf(buf, sizeof(buf), "ch%u", st.ch);
  s_spr.drawString(buf, x, y); x += 28;
  // WiFi
  s_spr.drawString(st.wifi_ok ? "WiFiO" : "WiFix", x, y); x += 44;
  // Node
  if (st.node_need > 0) snprintf(buf, sizeof(buf), "Nd%d/%d%s", st.node_have, st.node_need, node_ok?"O":"x");
  else                  snprintf(buf, sizeof(buf), "Nd-/-");
  s_spr.drawString(buf, x, y); x += 52;
  // Beam
  s_spr.drawString(st.beam_ok ? "BmO" : "Bmx", x, y); x += 32;
  // Send or 重複検知（重複は最重要なので、その時だけ差し替え表示）
  //  優先順位：GW > SG > RC（GWが最重要・docs/20.5）。
  if (any_dup) {
    const char* d = st.gw_dup ? "GWx2x" : (st.sg_dup ? "SGx2x" : "RCx2x");
    s_spr.drawString(d, x, y);
  } else {
    snprintf(buf, sizeof(buf), "Sd%d%s", st.unsent, send_ok?"O":"x");
    s_spr.drawString(buf, x, y);
  }
  x += 44;
  // Sync（時刻同期・B1/24.11）
  s_spr.drawString(st.sync_ok ? "SyO" : "Syx", x, y); x += 32;
  // Lost（あきらめ発生・C1/24.14。灰リセットまで保持）
  s_spr.drawString(st.lost ? "Lox" : "LoO", x, y);
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

// ===== 受付(SET)画面（赤押下→ARMED・docs/20.6）=============================
//  赤ボタンを受け付けた瞬間〜緑点灯までの「間」を埋める表示。
//  READY(待機)から即座に大きな「SET」へ切り替え、押下が通ったことを視認させる。
//  blink_on はtick_display側の250ms周期。SETの下の●で「準備中」を点滅表示する。
static void draw_set(bool blink_on) {
  s_spr.fillRect(0, 0, W, BODY_H, C_BLACK);
  // 中央に大きく SET（ON TRACK と同じ赤系＝これから始まる合図）
  //  font7/8は7セグ数字専用で英字が無いため、英字入りfont4を拡大して大書きする。
  s_spr.setTextDatum(MC_DATUM);
  s_spr.setTextColor(C_ONTRK, C_BLACK);
  s_spr.setTextFont(4);
  s_spr.setTextSize(3);                        // font4(約26px)×3で大型化
  s_spr.drawString("SET", W / 2, BODY_H / 2 - 10);
  s_spr.setTextSize(1);                        // 以降のために倍率を戻す
  // 下に「GET READY」＋点滅●（準備中の合図・フォント非依存で円を直接描く）
  s_spr.setTextFont(4);
  s_spr.setTextColor(C_FINISH, C_BLACK);      // 黄（間もなく＝注意色）
  s_spr.setTextDatum(MC_DATUM);
  const int ty = BODY_H - 34;
  s_spr.drawString("GET READY", W / 2, ty);
  if (blink_on) {
    int tw = s_spr.textWidth("GET READY");
    s_spr.fillCircle(W / 2 - tw / 2 - 16, ty, 6, C_FINISH);   // 見出し左に点滅ドット
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
  // 経過秒（右上・大きく）。1/100秒表示（案B：フィールド側と解像度を一致）。
  //  この描画はフルフレーム(約4fps)の下地。実際に機敏に動くのは下の
  //  draw_time_field()による約30fpsの部分更新（同じ位置・同じ書式に重なる）。
  s_spr.setTextDatum(TR_DATUM);
  s_spr.setTextColor(C_WHITE, C_BLACK);
  char t[16]; snprintf(t, sizeof(t), "%.2f", elapsed_ms / 1000.0);
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
    case ERR_SENSOR_BOTH: // A1：両ビーム欠(Wセンサー不良/CO/外乱光)・24.6（label=SQ/レーン）
      out[n++] = "! DUAL SENSOR FAULT";
      out[n++] = "Both beams blocked.";
      out[n++] = "1) ceiling LED";
      out[n++] = "2) floor W-sensor";
      out[n++] = "3) wiring / stray light";
      break;
    case ERR_SPEED_ONLY:  // A2：片ビーム欠(速度未計測)・24.6（label=SQ/レーン）
      out[n++] = "! SPEED NOT MEASURED";
      out[n++] = "One beam missing.";
      out[n++] = "1) clean floor";
      out[n++] = "2) floor sensor";
      out[n++] = "3) ceiling LED";
      break;
    case ERR_RC_DUP:      // リモコン2台検知（本改修で追加・GW2台と同思想）
      out[n++] = "! REMOTE x2";
      out[n++] = "Two remotes are on.";
      out[n++] = "Power OFF one of them.";
      break;
    case ERR_SG_DUP:      // シグナル2台検知（本改修で追加・GW2台と同思想）
      out[n++] = "! SIGNAL x2";
      out[n++] = "Two signals are on.";
      out[n++] = "Power OFF one of them.";
      break;
    case ERR_SECTOR_COMM: // C1：セクター通信不良(Lost)・24.14（label=SQ名）
      out[n++] = "! SECTOR COMM LOST";
      out[n++] = "Data did not arrive.";
      out[n++] = "1) restart S/G & node";
      out[n++] = "2) replace if persists";
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
    case ERR_SENSOR_BOTH: snprintf(out, n, "- %s dual sensor fault", e.label); break;
    case ERR_SPEED_ONLY:  snprintf(out, n, "- %s speed not measured", e.label); break;
    case ERR_RC_DUP:    snprintf(out, n, "- remote x2 (power off 1)"); break;
    case ERR_SG_DUP:    snprintf(out, n, "- signal x2 (power off 1)"); break;
    case ERR_SECTOR_COMM: snprintf(out, n, "- %s comm lost (1 dropped)", e.label); break;
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
// ===== 経過秒フィールドの部分更新（案B）====================================
//  ON TRACK中に高頻度で呼ぶ。小スプライトへ経過秒だけ描いて小領域転送する。
//  位置/フォント/右揃えは draw_ontrack の経過秒と同一なので、フルフレームの
//  上にぴったり重なる。転送量は約10KB(≒3ms/回)でSPI負荷は軽い。
//  hundredths=true で 1/100秒（ストップウォッチ的）、false で 1/10秒。
static void draw_time_field(uint32_t elapsed_ms, bool hundredths) {
  if (!s_time_ok) return;
  s_time_spr.fillSprite(C_BLACK);
  s_time_spr.setTextDatum(TR_DATUM);
  s_time_spr.setTextColor(C_WHITE, C_BLACK);
  s_time_spr.setTextFont(4);
  char t[16];
  if (hundredths) snprintf(t, sizeof(t), "%.2f", elapsed_ms / 1000.0);
  else            snprintf(t, sizeof(t), "%.1f", elapsed_ms / 1000.0);
  // ローカル(126,6)=screen(312,8)=draw_ontrackの(W-8,8)に一致。
  s_time_spr.drawString(t, TIME_W - 4, 6);
  s_time_spr.pushSprite(TIME_X, TIME_Y);
}

//  各draw_*の後に呼ぶ。ステータスバーを重ねてから一括push（ちらつき無し）。
static void commit() {
  if (!s_ok) return;
  draw_status_bar();
  s_spr.pushSprite(0, 0);
}

} // namespace disp
