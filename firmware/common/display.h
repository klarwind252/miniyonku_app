// firmware/common/display.h
// ============================================================================
//  M4LAPS GW TFT表示モジュール（docs/20・20章追記＝文言確定 準拠）
//  パネル：ILI9341 320x240 横向き（setRotation(3)）。PSRAMスプライトで裏描き→転送。
//  画面：待機(IDLE) / 計測中(ON TRACK) / 計測終了(FINISH) / エラー。
//  共通：最下部28pxステータスバー。×が1つでもあればバー全体を赤。
//
//  ⚠実機なしのため座標・フォント・折り返しは docs/20.6 のとおり実機で追い込む前提。
//    本モジュールは「型・状態切替・確定エラー文言」までを実装（see docs/20章追記）。
//  ⚠当日ベスト(20.2)：Total/Lap は GW自己完結の実値を描画（20260901・案A／電源ON毎に揮発）。
//    Av./Max は速度＝アプリ側担当のため「-」据置き（案Bで埋める余地）。
//    ラップテーブル(20.3/20.4)は g_run の実データを描画済み（DC37）。
// ============================================================================
#pragma once
#include <Arduino.h>
#include <math.h>
#include <TFT_eSPI.h>
#include "jpfont.h"        // 20260901h：日本語エラー文言用サブセットフォント（1bit・自動生成）
#ifdef LOGO_B
  #include "logo_onedafull.h"
#else
  #include "logo_arcadebase.h"
#endif

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
static constexpr int BAR_H = 22;                 // ステータスバー高（font1化で28→22に縮小・ボディ+6px・2026-08-24）
static constexpr int BODY_H = H - BAR_H;
static constexpr int NUM_LANES = 3;              // ★このファームのレーン数（3レーン機=3／将来5レーン機=5）。ロスターREADYの行数に使用（20261109）

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
  char link[48]  = "-";     // 接続中の機材ID一覧（main側が在席から生成）
  // 20260901：本日のベスト（案A＝GW自己完結／電源ON毎に揮発。0=未記録→draw_idleは「-」）。
  //  時間系のみGWが自前で持つ。速度(Av./Max)はアプリ側担当のため保持しない。
  uint32_t best_total_ms   = 0; uint8_t best_total_lane = 0;  // 完走合計の最小＋達成レーン(1..3)
  uint32_t best_lap_ms     = 0; uint8_t best_lap_lane   = 0;  // 単周ラップの最小＋達成レーン(1..3)
  // ★20261109：READYのLap行をレーン別ベストに変更（L1/L2/L3）。0=未記録→「-」
  uint32_t best_lap_lane_ms[3] = {0,0,0};
  // ★20261109：アクティブレース＝ロスターREADY用（main側がfetch_raceで更新）
  bool race_active = false;                 // true=アクティブ有り→ロスター表示／false=通常READY(Total/Lap)
  char lane_name[5][24] = {{0}};            // L1..L5 レーサー名（サーバー正規化済み：カタカナ/半角英数）。空文字="" →「ー」
};

// ---- レーン別 走行データ（ON TRACK/FINISHのラップ表示用・main側が更新）------
//  docs/20.3-20.4：レーン枠内にラップ縦テーブル＋周回x/y＋完走合計を出すための器。
//  main.cpp が自機S/G通過のたびに書き込み、begin_internal_race で全消去する。
static constexpr int MAX_LAPS = 9;               // docs/14 DA15：周回は最大9
struct LaneRun {
  uint8_t  done     = 0;      // 確定したラップ数（0..MAX_LAPS）
  bool     fin      = false;  // 規定周回を走り切った＝このレーンは完走
  uint32_t lap_ms[MAX_LAPS] = {0};  // 各周のラップ(ms)
  uint32_t total_ms = 0;      // 完走時の合計(ms)。F1式=緑起点／走行式=初回通過起点
};
static LaneRun g_run[3];      // index 0..2 = L1..L3
static inline void run_reset() { for (int i = 0; i < 3; i++) g_run[i] = LaneRun(); }

// ---- エラー種別（docs/20.5 優先順位）---------------------------------------
//  ⚠ RC/SG重複（ERR_RC_DUP/ERR_SG_DUP）は本改修で追加した新種別。
//     docs/20章追記（文言確定）にはまだ無いので、確定日本語文言は後日そこへ追記すること。
//  ⚠ A1/A2/C1（ERR_SENSOR_BOTH/ERR_SPEED_ONLY/ERR_SECTOR_COMM）は 24.5/24.6/24.14 で確定。
//     優先順位（20.5.2＋24.14）：GW2台 > 送信失敗 > ノード離脱 > ビーム切れ/A1 > A2 > SG重複 > RC重複 > C1
enum ErrKind {
  // 20260904 改修③：ERR_SEND_FAIL / ERR_NODE_LOST / ERR_BEAM_CUT は文言だけあって
  //   main.cpp から一度もインスタンス化されない死に配線だったため削除（送信失敗は
  //   全画面に出さない方針で確定。ノード離脱はバーのSQ n/m、光軸切れはBeam:NG＋
  //   READY感知不能/A1/A2/全ビーム断でカバー）。優先順位はif文の並び順で実装され
  //   enum数値には依存しないため、後続の値が繰り上がっても他エラーに影響なし。
  ERR_GW_DUP=0,
  ERR_SENSOR_BOTH,   // A1：両ビーム欠(Wセンサー不良/CO/外乱光)・24.6
  ERR_SPEED_ONLY,    // A2：片ビーム欠(速度未計測)・24.6
  ERR_RC_DUP, ERR_SG_DUP,
  ERR_SECTOR_COMM,   // C1：セクター通信不良(Lost)・24.14・最後尾
  ERR_ALL_BEAM,      // 全ビーム断＝TX共通系(LED電源/38kHz)疑い（20260830c追加）
  ERR_READY_NOBEAM   // READY中の感知不能（1レーンでも受光できていない・20260831）
};

struct ErrItem {
  ErrKind kind;
  int  arg_i = 0;        // ノードID or レーン番号 or 保持件数
  char label[40] = {0};  // 20260901j：本文差し込み用の日本語ラベル（例「GW6 レーン1 センサーA」）
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

// ★20261109 前方宣言：ロスターREADYのレーサー名描画（日本語1bitフォント）。定義は jp_find 以降。
static int jp_draw_string(int x, int y, const char* str, uint16_t color);
static int jp_draw_string_scaled(int x, int y, const char* str, uint16_t color, int sc, int x_max);
// ★20261109：ON TRACKが1〜3周モード（右上タイムをsize2で特大描画）か。trueの間は
//   draw_time_field()の小サイズ部分更新を止め、ベース(4fps)の特大タイムを活かす（競合回避）。
static bool s_ontrack_big = false;

static void begin() {
  s_tft.init();
  s_tft.setRotation(1);           // 横向き 320x240
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

// ===== 起動スプラッシュ（ARCADE BASE・回転縮小リビール・非ブロッキング）========
//  右下がり45°＋ドアップの状態から、0°・100%へ回転しながら縮小して着地。黒地・白ロゴ。
//  1フレーム = draw_splash_frame(p)（p:0→1）。main側がloopで少しずつ呼ぶ（裏で通信進行）。
//  逆写像アフィン（増分ステップ）で s_spr に直接描く。ステータスバー無し。
#ifdef LOGO_B
// ===== B系スプラッシュ（One Da Full・扇振り→武者震い＋星→拡大・黒地/白）=========
static inline uint8_t lb_alpha(int x,int y){ if(x<0||x>=LOGOB_W||y<0||y>=LOGOB_H)return 0; return pgm_read_byte(&LOGOB_ALPHA[y*LOGOB_W+x]); }
static inline uint8_t lb_glow (int x,int y){ if(x<0||x>=LOGOB_W||y<0||y>=LOGOB_H)return 0; return pgm_read_byte(&LOGOB_GLOW [y*LOGOB_W+x]); }
static void draw_star(int cx,int cy,int size,int bright){   // 白の8方向きらめき（十字＋斜め）
  if(bright<=8)return; if(bright>255)bright=255;
  for(int t=-size;t<=size;t++){
    int at=t<0?-t:t; int b=bright*(size+1-at)/(size+1); if(b<8)continue;
    uint16_t cc=s_spr.color565((uint8_t)b,(uint8_t)b,(uint8_t)b);
    int px[4]={cx+t,cx,cx+t,cx+t}, py[4]={cy,cy+t,cy+t,cy-t};
    for(int k=0;k<4;k++){int x=px[k],y=py[k]; if(x>=0&&x<W&&y>=0&&y<H)s_spr.drawPixel(x,y,cc);}
  }
}
static void draw_splash_frame(float p){
  if(!s_ok)return; if(p<0)p=0; if(p>1)p=1;
  const float CXf=W/2.0f, CYf=H/2.0f;
  const float PIVX=CXf, PIVY=CYf+150.0f, RAD=PIVY-CYf;   // 支点=中央の下・ロゴは上側で振れる（下が中心の扇・th=0で中央収束）
  float ox,oy,ang,scale;
  float th_cur=0.0f; bool in_swing=(p<0.55f);
  if(in_swing){                                  // 扇状に大きく振れて減衰（画面外から）
    float q=p/0.55f; float damp=expf(-2.0f*q);
    th_cur=(88.0f*3.14159265f/180.0f)*sinf(q*3.14159265f*5.0f)*damp;   // ±88°（画面外から）
    ox=PIVX+sinf(th_cur)*RAD; oy=PIVY-cosf(th_cur)*RAD; ang=th_cur*0.5f; scale=1.0f;
  }else if(p<0.78f){                             // 武者震い
    float q=(p-0.55f)/0.23f;
    ox=W/2.0f+sinf(q*3.14159265f*22.0f)*4.0f;
    oy=H/2.0f+cosf(q*3.14159265f*19.0f)*3.0f;
    ang=(sinf(q*3.14159265f*22.0f)*1.5f)*3.14159265f/180.0f; scale=1.0f;
  }else{                                          // 拡大して終了
    float q=(p-0.78f)/0.22f; ox=W/2.0f; oy=H/2.0f; ang=0.0f; scale=1.0f+(7.0f-1.0f)*q*q;
  }
  float ca=cosf(ang), sa=sinf(ang);
  float lcx=LOGOB_W/2.0f, lcy=LOGOB_H/2.0f;
  float sxu=ca/scale, sxv=-sa/scale;
  for(int y=0;y<H;y++){
    float rx=0-ox, ry=(float)y-oy;
    float u=( ca*rx+sa*ry)/scale+lcx;
    float v=(-sa*rx+ca*ry)/scale+lcy;
    for(int x=0;x<W;x++){
      int su=(int)(u+0.5f), sv=(int)(v+0.5f);
      int a=lb_alpha(su,sv); int g=lb_glow(su,sv);
      int R=0,G=0,B=0;
      int gw=g>>1; if(gw>R){R=gw;G=gw;B=gw;}     // 白グロー
      if(a>=110){R=255;G=255;B=255;}             // 白ロゴ
      s_spr.drawPixel(x,y,s_spr.color565((uint8_t)R,(uint8_t)G,(uint8_t)B));
      u+=sxu; v+=sxv;
    }
  }
  // 星・着地バースト：揺れ終わり(0.55)直後に大きく12個を四方へ弾く
  if(p>=0.55f && p<0.72f){
    float q=(p-0.55f)/0.17f; int bright=(int)(255*(1.0f-q));
    for(int i=0;i<12;i++){
      float aa=(float)i*(6.2831853f/12.0f); float r=40.0f+170.0f*q;
      draw_star((int)(W/2+cosf(aa)*r),(int)(H/2+sinf(aa)*r),26,bright);
    }
  }
  // 星A：武者震いで四方へ飛散（大きめ）
  if(p>=0.55f && p<0.88f){
    float q=(p-0.55f)/0.33f; int bright=(int)(255*(1.0f-q));
    static const float SA_[9]={0.3f,1.0f,1.9f,2.7f,3.5f,4.2f,5.0f,5.7f,0.9f};
    static const float SD_[9]={70,120,95,140,80,130,100,150,110};
    for(int i=0;i<9;i++){
      float r=SD_[i]*q; int sx=(int)(W/2+cosf(SA_[i])*r); int sy=(int)(H/2+sinf(SA_[i])*r);
      draw_star(sx,sy,22,bright);
    }
  }
  // 星B：左右の振り切り（折り返し＝角度が極大の瞬間）でロゴ周囲へバースト。
  //  非ブロッキングのため前2フレームの|th|を保持し、山（増加→減少）を検出。
  static float pth1 = 0.0f, pth2 = 0.0f;      // |th| 履歴
  static float burst_x = 0, burst_y = 0;      // 直近の端バースト発生位置
  static int   burst_age = 99;                // バースト経過フレーム
  if (in_swing) {
    float ath = th_cur < 0 ? -th_cur : th_cur;
    // 折り返し検出：一つ前がピーク（pth1>=ath かつ pth1>=pth2）かつ十分振れている
    if (pth1 >= ath && pth1 >= pth2 && pth1 > (20.0f * 3.14159265f / 180.0f)) {
      burst_x = ox; burst_y = oy; burst_age = 0;   // ここで発火
    }
    pth2 = pth1; pth1 = ath;
  }
  if (burst_age < 5) {                          // 5フレームかけて広がり減衰
    float q = burst_age / 5.0f; int br = (int)(255 * (1.0f - q));
    for (int k = 0; k < 8; k++) {
      float a0 = (float)k * (6.2831853f / 8.0f);
      float r = 20.0f + 90.0f * q;
      draw_star((int)(burst_x + cosf(a0) * r), (int)(burst_y + sinf(a0) * r), 20, br);
    }
    burst_age++;
  }
  s_spr.pushSprite(0,0);
}
#else
static inline uint8_t logo_alpha(int lx, int ly) {
  if (lx < 0 || lx >= LOGO_W || ly < 0 || ly >= LOGO_H) return 0;
  return pgm_read_byte(&LOGO_ALPHA[ly * LOGO_W + lx]);
}
static inline uint8_t logo_glow(int lx, int ly) {
  if (lx < 0 || lx >= LOGO_W || ly < 0 || ly >= LOGO_H) return 0;
  return pgm_read_byte(&LOGO_GLOW[ly * LOGO_W + lx]);
}
// F1リビール準拠スプラッシュ（2026-08-24r）
//  カーボン地 → 横スピードライン → ロゴ(赤塗り＋白熱縁＋ガウスグロー)が45°ドアップから着地
//  → 発光ホールド(呼吸) → 40xズームで突き抜け(ライン再走) → 暗転。
//  グローは事前ガウスぼかし(LOGO_GLOW)参照のみ＝実行時ぼかし計算なし（軽量）。
static void draw_splash_frame(float p) {
  if (!s_ok) return;
  if (p < 0) p = 0; if (p > 1) p = 1;

  // カーボン柄タイル（20x20・初回のみ生成）
  static bool tile_ok = false;
  static uint8_t tR[20][20], tG[20][20], tB[20][20];
  if (!tile_ok) { tile_ok = true;
    for (int y = 0; y < 20; y++) for (int x = 0; x < 20; x++) {
      int cx = x % 10, cy = y % 10;
      int par = ((x / 10) + (y / 10)) & 1;
      int diag = par ? (9 - cx) + cy : cx + cy;
      float sheen = 0.5f + 0.5f * cosf(diag / 10.0f * 3.14159265f);
      float base = 16.0f + sheen * 22.0f;
      uint8_t gray = (uint8_t)base;   // 白黒カーボン（色素なし）
      tR[y][x] = gray; tG[y][x] = gray; tB[y][x] = gray;
    }
  }

  // --- 進行スケジュール（プレビューと同一）---
  bool  logo_vis = (p >= 0.06f);
  float ang = 0.0f, scale = 1.0f;
  if (logo_vis && p < 0.26f) {
    float q = (p - 0.06f) / 0.20f; float e = 1.0f - (1.0f - q) * (1.0f - q);
    ang = (45.0f * 3.14159265f / 180.0f) * (1.0f - e);
    scale = 1.0f + (5.0f - 1.0f) * (1.0f - e);
  } else if (p < 0.66f) {
    scale = 1.0f + 0.015f * sinf((p - 0.26f) / 0.40f * 3.14159265f * 3.0f);  // 呼吸
  } else {
    float q = (p - 0.66f) / 0.34f;
    scale = 1.0f + (90.0f - 1.0f) * q * q * q;                              // 90xで超拡大・突き抜け
  }
  float ca = cosf(ang), sa = sinf(ang);
  float cx = W / 2.0f, cy = H / 2.0f, lcx = LOGO_W / 2.0f, lcy = LOGO_H / 2.0f;
  float sxu = ca / scale, sxv = -sa / scale;

  // スピードライン（固定y・イントロ/アウトロ）
  static const int SY[9] = {13, 177, 84, 220, 49, 140, 201, 26, 110};
  float in_t   = (p < 0.22f) ? (p / 0.22f) : -1.0f;
  float in_amp = (p < 0.22f) ? (1.0f - p / 0.22f) * 1.3f : 0.0f;
  float out_t  = (p >= 0.80f) ? ((p - 0.80f) / 0.20f) : -1.0f;
  float out_amp= (p >= 0.80f) ? fminf(1.0f, (p - 0.80f) / 0.06f) : 0.0f;
  float fade   = (p >= 0.95f) ? (1.0f - (p - 0.95f) / 0.05f) : 1.0f;

  for (int y = 0; y < H; y++) {
    // この行に掛かるライン（帯幅±5px）を最大4本収集
    struct SL { float amp; float xo; };
    SL Lin[4], Lout[4]; int nin = 0, nout = 0;
    for (int i = 0; i < 9; i++) {
      int dy = y - SY[i]; if (dy < 0) dy = -dy;
      if (dy >= 5) continue;
      float band = 1.0f - dy / 5.0f;
      if (in_t >= 0 && nin < 4)  { Lin[nin].amp  = band * in_amp;
        Lin[nin].xo  = fmodf(in_t  * 1000.0f + i * 90.0f, (float)(W + 260)) - 130.0f; nin++; }
      if (out_t >= 0 && nout < 4){ Lout[nout].amp = band * out_amp;
        Lout[nout].xo = fmodf(out_t * 1000.0f + i * 90.0f + 300.0f, (float)(W + 260)) - 130.0f; nout++; }
    }
    const uint8_t* rR = tR[y % 20]; const uint8_t* rG = tG[y % 20]; const uint8_t* rB = tB[y % 20];
    float rx0 = 0 - cx, ry = (float)y - cy;
    float u = ( ca * rx0 + sa * ry) / scale + lcx;
    float v = (-sa * rx0 + ca * ry) / scale + lcy;
    for (int x = 0; x < W; x++) {
      int R = rR[x % 20], G = rG[x % 20], B = rB[x % 20];   // カーボン地
      // イントロのライン（ロゴより下層）
      for (int i = 0; i < nin; i++) {
        float d = x - Lin[i].xo; if (d < 0) d = -d;
        if (d < 130.0f) { float h = 1.0f - d / 130.0f; float sgl = Lin[i].amp * h * h;
          R += (int)(sgl * 110); G += (int)(sgl * 12); B += (int)(sgl * 28); }
      }
      if (logo_vis) {
        int su = (int)(u + 0.5f), sv = (int)(v + 0.5f);
        int a  = logo_alpha(su, sv);
        int g8 = logo_glow(su, sv);
        // 縁のにじみ＝ワインレッド（ガウスぼかしグローをワイン色で加算）
        int gr = (g8 * 110) >> 8, gg = (g8 * 12) >> 8, gb = (g8 * 28) >> 8;   // ダークワインにじみ
        if (gr > R) R = gr; if (gg > G) G = gg; if (gb > B) B = gb;
        if (a >= 128) { R = 255; G = 255; B = 255; }   // ロゴ本体＝白固定
      }
      // アウトロのライン（ロゴの上層）
      for (int i = 0; i < nout; i++) {
        float d = x - Lout[i].xo; if (d < 0) d = -d;
        if (d < 130.0f) { float h = 1.0f - d / 130.0f; float sgl = Lout[i].amp * h * h;
          R += (int)(sgl * 110); G += (int)(sgl * 12); B += (int)(sgl * 28); }
      }
      if (fade < 1.0f) { R = (int)(R * fade); G = (int)(G * fade); B = (int)(B * fade); }
      if (R > 255) R = 255; if (G > 255) G = 255; if (B > 255) B = 255;
      s_spr.drawPixel(x, y, s_spr.color565((uint8_t)R, (uint8_t)G, (uint8_t)B));
      u += sxu; v += sxv;
    }
  }
  s_spr.pushSprite(0, 0);
}
#endif  // LOGO_B

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
  int x = 4;
  char buf[24];

  // 全項目フォント1・ASCIIのみ（実機で確実に表示）。OK=正常 / NG=異常。
  //  SQ・Send・Lost… の並び（24.11）。SQ/Sendは数字が情報なのでOK/NGは付けない。
  s_spr.setTextFont(1);
  s_spr.setTextSize(1);

  // GW番号（OK/NGなし）
  snprintf(buf, sizeof(buf), "GW%u", st.gw_id);
  s_spr.drawString(buf, x, y); x += 26;
  // ch（OK/NGなし）
  snprintf(buf, sizeof(buf), "ch%u", st.ch);
  s_spr.drawString(buf, x, y); x += 26;
  // WiFi
  s_spr.drawString(st.wifi_ok ? "WiFi:OK" : "WiFi:NG", x, y); x += 44;
  // SQ（実JOIN数/必要数。未取得は -/-）
  if (st.node_need > 0) snprintf(buf, sizeof(buf), "SQ%d/%d", st.node_have, st.node_need);
  else                  snprintf(buf, sizeof(buf), "SQ-/-");
  s_spr.drawString(buf, x, y); x += 40;
  // Beam
  s_spr.drawString(st.beam_ok ? "Beam:OK" : "Beam:NG", x, y); x += 48;
  // Send or 重複検知（重複は最重要なのでSendスロットを差し替え・優先 GW>SG>RC）
  if (any_dup) {
    const char* d = st.gw_dup ? "GWx2!" : (st.sg_dup ? "SGx2!" : "RCx2!");
    s_spr.drawString(d, x, y);
  } else {
    snprintf(buf, sizeof(buf), "Send:%d", st.unsent);
    s_spr.drawString(buf, x, y);
  }
  x += 40;
  // Sync（時刻同期・B1/24.11）
  s_spr.drawString(st.sync_ok ? "Sync:OK" : "Sync:NG", x, y); x += 44;
  // Lost（あきらめ発生・C1/24.14。灰リセットまで保持）
  s_spr.drawString(st.lost ? "Lost:NG" : "Lost:OK", x, y);
}

// ===== 待機(IDLE)画面（docs/20.2）==========================================
//  当日ベスト4行：Total/Lap は実値（20260901・案A）。Av./Max と未記録は「-」。
// 接続中の機材ID一覧を上段中央に固定表示（READY/ON TRACK共通・フォント1・白・2026-08-24）。
//  READYなら「READY」と「TODAY BEST」の間、ON TRACKなら「ON TRACK」とタイムの間。
// ★20261109：接続機材ID一覧（SQ…）。通常READYの右上の細い帯に右寄せ表示。
//   ON TRACK/FINISHでは呼ばない（計測中はSQ非表示＝ユーザー確定）。ロスターREADYでも非表示。
//   READYは経過秒の部分更新スプライトを使わないため、右上(TR)に出しても競合しない。
static void draw_link_line() {
  s_spr.setTextFont(1);
  s_spr.setTextSize(1);
  s_spr.setTextColor(C_GREY, C_BLACK);   // 接続一覧はグレー
  s_spr.setTextDatum(TR_DATUM);          // 右上寄せ
  s_spr.drawString(g_status.link, W - 6, 4);
}

// ★20261109：READYヘッダ（「READY」特大・右端近くまで・左上起点）。通常/ロスター共通の上段。
static void draw_ready_header() {
  s_spr.setTextDatum(TL_DATUM);
  s_spr.setTextColor(C_READY, C_BLACK);
  s_spr.setTextFont(4); s_spr.setTextSize(3);     // font4(≈26px)×3≒78px。右端近くまで大きく
  s_spr.drawString("READY", 6, 14);
  s_spr.setTextSize(1);
}

// 通常READY：本日ベスト（Total 1行＋Lapレーン別）。アクティブレース「無し」のとき。
//  Total/Lap は GW自己完結の実値（電源ON毎に揮発）。未記録は「-」。座標は実機で追い込み前提。
static void draw_ready_best() {
  draw_link_line();   // 接続機材ID一覧（右上の細い帯）
  // ── Total 行（値をfont4 size2で大きく・1行「Total xxx s L2」）──
  const int ty = 108;
  s_spr.setTextDatum(TL_DATUM);
  s_spr.setTextColor(C_WHITE, C_BLACK);
  s_spr.setTextFont(4);
  s_spr.drawString("Total", 12, ty + 13);
  char val[12];
  if (g_status.best_total_ms == 0)           snprintf(val, sizeof(val), "-");
  else if (g_status.best_total_ms >= 100000) snprintf(val, sizeof(val), "%.1f", g_status.best_total_ms / 1000.0);
  else                                       snprintf(val, sizeof(val), "%.2f", g_status.best_total_ms / 1000.0);
  s_spr.setTextDatum(TR_DATUM);
  s_spr.setTextFont(4); s_spr.setTextSize(2);
  s_spr.drawString(val, W - 78, ty);
  s_spr.setTextSize(1);
  s_spr.setTextFont(4);
  s_spr.drawString("s", W - 44, ty + 26);
  if (g_status.best_total_ms > 0 && g_status.best_total_lane >= 1 && g_status.best_total_lane <= 3) {
    char ln[4]; snprintf(ln, sizeof(ln), "L%u", g_status.best_total_lane);
    s_spr.setTextColor(lane_color(g_status.best_total_lane), C_BLACK);
    s_spr.drawString(ln, W - 8, ty + 14);
  }
  // ── Lap 行：レーン別ベスト「Lap L1 xxx L2 xxx L3 xxx」──
  const int ly = 176;
  s_spr.setTextDatum(TL_DATUM);
  s_spr.setTextColor(C_WHITE, C_BLACK);
  s_spr.setTextFont(4);
  s_spr.drawString("Lap", 12, ly);
  s_spr.setTextFont(2);
  const int gx[3] = { 72, 158, 244 };
  for (int i = 0; i < 3; i++) {
    char tag[4]; snprintf(tag, sizeof(tag), "L%d", i + 1);
    s_spr.setTextDatum(TL_DATUM);
    s_spr.setTextColor(lane_color(i + 1), C_BLACK);
    s_spr.drawString(tag, gx[i], ly + 6);
    char lv[12];
    if (g_status.best_lap_lane_ms[i] == 0) snprintf(lv, sizeof(lv), "-");
    else                                   snprintf(lv, sizeof(lv), "%.2f", g_status.best_lap_lane_ms[i] / 1000.0);
    s_spr.setTextColor(C_WHITE, C_BLACK);
    s_spr.drawString(lv, gx[i] + 20, ly + 6);
  }
}

// ロスターREADY：レーン番号＋レーサー名（番号レーン色・名前白・大）。アクティブレース「有り」のとき。
//  行数＝NUM_LANES。空きレーン＝「ー」。長い名前は右端(W-6)でクリップ（縮小・スクロールなし）。
static void draw_roster() {
  const int top = 94, bot = BODY_H;                 // READY下端≈92〜バー(218)の間
  const int n   = NUM_LANES;
  const int sc  = (n <= 3) ? 2 : 1;                 // 3レーン=2倍(≒42px)／5レーン=等倍(21px)
  const int cell_h = jpf::CELL_H * sc;
  const int pitch  = (bot - top) / n;
  for (int i = 0; i < n; i++) {
    int y = top + i * pitch + (pitch - cell_h) / 2;
    uint16_t col = lane_color(i + 1);
    char num[4]; snprintf(num, sizeof(num), "%d", i + 1);
    jp_draw_string_scaled(14, y, num, col, sc, W - 6);                 // 番号＝レーン色
    const char* nm = g_status.lane_name[i];
    if (nm[0] == '\0') jp_draw_string_scaled(58, y, "ー", C_GREY,  sc, W - 6);  // 空きレーン＝ー
    else               jp_draw_string_scaled(58, y, nm, C_WHITE, sc, W - 6);    // 名前＝白・右クリップ
  }
}

static void draw_idle() {
  s_spr.fillRect(0, 0, W, BODY_H, C_BLACK);
  draw_ready_header();
  if (g_status.race_active) draw_roster();      // アクティブ有り＝ロスター（レーサー名）
  else                      draw_ready_best();  // 無し＝通常READY（本日ベスト）
}

// ===== 受付(SET)画面（赤押下→ARMED・docs/20.6）=============================
//  赤ボタンを受け付けた瞬間〜緑点灯までの「間」を埋める表示。
//  READY(待機)から即座に大きな「SET」へ切り替え、押下が通ったことを視認させる。
//  ⚠docs要望2026-08-24b：ステータスバーを除く本体全面に「SET」の一語だけを出す。
//    以前の「GET READY」＋点滅ドットは廃止（引数 blink は互換のため残すが未使用）。
static void draw_set(bool /*blink 未使用*/) {
  // 本体領域（バーを除く全面）を黒で塗り、中央に SET だけを大書きする。
  s_spr.fillRect(0, 0, W, BODY_H, C_BLACK);
  s_spr.setTextDatum(MC_DATUM);
  s_spr.setTextColor(C_FINISH, C_BLACK);      // 黄＝SET（赤押下受付の合図・20260831要望）
  // font4(約26px)を size4 で拡大し、本体中央に大書き（英字入りフォントはfont4系のみ）。
  s_spr.setTextFont(4);
  s_spr.setTextSize(4);
  s_spr.drawString("SET", W / 2, BODY_H / 2);
  s_spr.setTextSize(1);                        // 以降のために倍率を戻す
}

// ===== 計測中(ON TRACK)画面（docs/20.3）====================================
//  ●点滅・経過秒・3レーン枠。ラップテーブルは骨組みのみ（実データ後続）。
// ★20261109：完走タイム（走行中は累計＝暫定／完走でtotal_ms確定）
static uint32_t lane_running_total(const LaneRun& r) {
  if (r.fin) return r.total_ms;
  uint32_t s = 0;
  for (int k = 0; k < r.done && k < MAX_LAPS; k++) s += r.lap_ms[k];
  return s;
}

// ラップ縦テーブル（ns個の均等スロット・最大cap行・font2）。レーン間で行が揃う。
static void draw_lap_table(int x, int lane_w, const LaneRun& r, int top, int bot, int ns, int cap) {
  s_spr.setTextFont(2);
  int rows = r.done; if (rows > cap) rows = cap; if (rows > MAX_LAPS) rows = MAX_LAPS;
  const int row_h = 16;
  int g = ((bot - top) - ns * row_h) / (ns + 1); if (g < 0) g = 0;
  for (int k = 0; k < rows; k++) {
    int yy = top + g * (k + 1) + row_h * k;
    s_spr.setTextDatum(TL_DATUM); s_spr.setTextColor(C_GREY, C_BLACK);
    char num[4]; snprintf(num, sizeof(num), "%d", k + 1); s_spr.drawString(num, x + 6, yy);
    s_spr.setTextDatum(TR_DATUM); s_spr.setTextColor(C_WHITE, C_BLACK);
    char lt[12]; snprintf(lt, sizeof(lt), "%.2f", r.lap_ms[k] / 1000.0); s_spr.drawString(lt, x + lane_w - 8, yy);
  }
}

// レーン枠1つ（ヘッダL#＋周回/FIN/DNF、完走タイムheadline、区切り線、ラップ表）。
//  ON TRACK と FINISH で共通。finish_mode=true＝未完走はDNF/「--.-」グレー、false＝x/y。
//  ★レーサー名は枠内に出さない（名前はロスターREADYのみ・ユーザー確定20261109）。
static void draw_lane_box(int x, int lane_w, int i, const LaneRun& r, int fy, int fbot,
                          int laps, int ns, int cap, bool finish_mode) {
  uint16_t col = lane_color(i + 1);
  s_spr.drawRect(x, fy, lane_w - 4, fbot - fy, col);
  // ヘッダ（L# ＋ 周回/FIN/DNF）
  s_spr.setTextFont(2);
  s_spr.setTextDatum(TL_DATUM); s_spr.setTextColor(C_WHITE, C_BLACK);
  char h[8]; snprintf(h, sizeof(h), "L%d", i + 1); s_spr.drawString(h, x + 4, fy + 3);
  s_spr.setTextDatum(TR_DATUM);
  char p[12];
  if (finish_mode) snprintf(p, sizeof(p), r.fin ? "FIN" : "DNF");
  else if (r.fin)  snprintf(p, sizeof(p), "FIN");
  else             snprintf(p, sizeof(p), "%u/%d", (unsigned)r.done, laps);
  s_spr.setTextColor((finish_mode && !r.fin) ? C_GREY : col, C_BLACK);
  s_spr.drawString(p, x + lane_w - 8, fy + 3);
  // 完走タイム headline（中央・font4）
  bool have   = finish_mode ? r.fin : (r.done > 0 || r.fin);
  uint32_t rt = finish_mode ? r.total_ms : lane_running_total(r);
  char tot[12];
  if (!have)             snprintf(tot, sizeof(tot), finish_mode ? "--.-" : "-");
  else if (rt >= 100000) snprintf(tot, sizeof(tot), "%.1f", rt / 1000.0);
  else                   snprintf(tot, sizeof(tot), "%.2f", rt / 1000.0);
  s_spr.setTextDatum(MC_DATUM); s_spr.setTextFont(4);
  s_spr.setTextColor(have ? C_WHITE : C_GREY, C_BLACK);
  s_spr.drawString(tot, x + (lane_w - 4) / 2, fy + (ns <= 3 ? 36 : 30));
  // 区切り線＋ラップ表
  int dvy = fy + (ns <= 3 ? 54 : 46);
  s_spr.drawFastHLine(x + 6, dvy, lane_w - 16, C_GREY);
  draw_lap_table(x, lane_w, r, dvy + 6, fbot - 5, ns, cap);
}

// ===== 計測中(ON TRACK)画面（20261109刷新：周回数で1〜3/4周以上を分岐）============
//  ●点滅＋ON TRACK（左上）／右上に経過秒（1〜3周=特大／4周以上=通常）。計測中SQ非表示。
//  各枠：L番号＋周回＋完走タイム(累計)＋ラップ。1〜3周=枠拡大・3スロット均等、
//  4周以上=最大6行で打切り。laps==0は4周以上側に倒す。
static void draw_ontrack(uint32_t elapsed_ms, bool blink_on, int laps) {
  s_spr.fillRect(0, 0, W, BODY_H, C_BLACK);
  if (blink_on) s_spr.fillCircle(14, 16, 6, C_ONTRK);
  s_spr.setTextDatum(ML_DATUM); s_spr.setTextColor(C_ONTRK, C_BLACK); s_spr.setTextFont(4);
  s_spr.drawString("ON TRACK", 28, 16);

  const bool few = (laps >= 1 && laps <= 3);     // 1〜3周＝大画面／それ以外(0含む)＝4周以上
  s_ontrack_big = few;                           // 1〜3周は部分更新を止める（下記draw_time_field）
  // 右上タイム（1〜3周=size2特大／4周以上=size1）。4周以上は draw_time_field が30fpsで重ねる。
  s_spr.setTextDatum(TR_DATUM); s_spr.setTextColor(C_WHITE, C_BLACK); s_spr.setTextFont(4);
  if (few) s_spr.setTextSize(2);
  char t[16]; snprintf(t, sizeof(t), "%.1f", elapsed_ms / 1000.0);
  s_spr.drawString(t, W - 8, few ? 6 : 8);
  s_spr.setTextSize(1);

  const int lane_w = (W - 16) / 3;
  const int fy = few ? 56 : 38, fbot = 212;
  const int ns = few ? 3 : 6, cap = few ? 3 : 6;
  for (int i = 0; i < 3; i++)
    draw_lane_box(8 + i * lane_w, lane_w, i, g_run[i], fy, fbot, laps, ns, cap, /*finish_mode=*/false);
}

// ===== 計測終了(FINISH)画面（docs/20.4）====================================
//  各枠：L番号 → 合計タイム（中央・大きく）→ 各周ラップ縦テーブル。
//  完走レーンは合計を表示、未完走（DNF等）は「--.-」のまま。
// ===== 計測終了(FINISH)画面（20261109：ON TRACKとレイアウト統一）==============
//  ヘッダ「FINISH」（黄・●なし・左端・font4）／右上＝最後に通過したマシンのタイム
//  （完走レーンの合計の最大）。周回数で1〜3/4周以上を分岐（ON TRACKと同じ枠）。
//  各枠：L番号＋FIN/DNF＋合計（完走=白／未完走=グレー「--.-」）＋ラップ。名前は出さない。
static void draw_finish(int laps) {
  s_spr.fillRect(0, 0, W, BODY_H, C_BLACK);
  s_spr.setTextDatum(ML_DATUM); s_spr.setTextColor(C_FINISH, C_BLACK); s_spr.setTextFont(4);
  s_spr.drawString("FINISH", 8, 16);

  const bool few = (laps >= 1 && laps <= 3);
  // 右上タイム＝最後に通過したマシン（完走レーンの合計の最大）
  uint32_t last_ms = 0;
  for (int i = 0; i < 3; i++)
    if (g_run[i].fin && g_run[i].total_ms > last_ms) last_ms = g_run[i].total_ms;
  s_spr.setTextDatum(TR_DATUM); s_spr.setTextColor(C_WHITE, C_BLACK); s_spr.setTextFont(4);
  if (few) s_spr.setTextSize(2);
  char t[16];
  if (last_ms == 0)           snprintf(t, sizeof(t), "--.-");
  else if (last_ms >= 100000) snprintf(t, sizeof(t), "%.1f", last_ms / 1000.0);
  else                        snprintf(t, sizeof(t), "%.2f", last_ms / 1000.0);
  s_spr.drawString(t, W - 8, few ? 6 : 8);
  s_spr.setTextSize(1);

  const int lane_w = (W - 16) / 3;
  const int fy = few ? 56 : 38, fbot = 212;
  const int ns = few ? 3 : 6, cap = few ? 3 : 6;
  for (int i = 0; i < 3; i++)
    draw_lane_box(8 + i * lane_w, lane_w, i, g_run[i], fy, fbot, laps, ns, cap, /*finish_mode=*/true);
}

// ===== エラー画面（docs/20.5・20章追記＝文言確定）==========================
//  ⚠文言の正本は docs/20章追記_エラー画面文言確定_20260728.md。
//    TFT_eSPI標準フォントは日本語グリフを持たないため、実機で日本語フォント
//    （efont/M+ 等）を導入してから、下記 MSG_* を確定日本語へ差し替える。
//    現段階はASCIIプレースホルダ＝実機なしでレイアウト・分岐・優先順位を検証する用。
//    差し替え時は「行数・改行位置」を docs の確定文言に一致させること。

// 単一エラーの本文行（ASCIIプレースホルダ。日本語はフォント導入後に差し替え）
// ===== 日本語1bitフォント描画（jpfontサブセット・20260901h）=====================
//  UTF-8文字列をデコードして s_spr に前景色で描く（背景は黒地に透過）。数字/英字も同梱。
//  ⚠ エラー画面の静止テキスト専用。数字・右上カウンタ・ステータスバーは従来の内蔵フォント。
static int jp_find(uint32_t cp) {                 // GLYPHSはcp昇順→二分探索
  int lo = 0, hi = (int)jpf::COUNT - 1;
  while (lo <= hi) {
    int mid = (lo + hi) >> 1; uint16_t c = jpf::GLYPHS[mid].cp;
    if (c == cp) return mid;
    if (c < cp) lo = mid + 1; else hi = mid - 1;
  }
  return -1;
}
static constexpr int JP_LINE_H = jpf::CELL_H + 3;   // 行送り
// x,yは行の左上。戻り値=描画後のペンX。
static int jp_draw_string(int x, int y, const char* str, uint16_t color) {
  int penx = x;
  const uint8_t* pp = (const uint8_t*)str;
  while (*pp) {
    uint32_t cp; uint8_t b = *pp;
    if (b < 0x80) { cp = b; pp += 1; }
    else if ((b >> 5) == 0x6) { cp = ((uint32_t)(b & 0x1F) << 6) | (pp[1] & 0x3F); pp += 2; }
    else if ((b >> 4) == 0xE) { cp = ((uint32_t)(b & 0x0F) << 12) | ((uint32_t)(pp[1] & 0x3F) << 6) | (pp[2] & 0x3F); pp += 3; }
    else { pp += 1; continue; }
    int gi = jp_find(cp);
    if (gi < 0 && cp == 0x26A0) gi = jp_find('!');   // ⚠は未収録→「!」（生成側で代替済みだが保険）
    if (gi < 0) { penx += jpf::CELL_H / 2; continue; }
    const jpf::Glyph& g = jpf::GLYPHS[gi];
    int bpr = (g.w + 7) >> 3;
    const uint8_t* bm = &jpf::BITS[g.off];
    for (int r = 0; r < jpf::CELL_H; r++)
      for (int c = 0; c < g.w; c++)
        if (bm[r * bpr + (c >> 3)] & (0x80 >> (c & 7)))
          s_spr.drawPixel(penx + c, y + r, color);
    penx += g.adv;
  }
  return penx;
}

// ★20261109：拡大版（sc倍のブロックで描く）。ロスターREADYの大きなレーサー名用。
//  x_max を超える字は描かない（右クリップ＝はみ出し無視・ユーザー確定）。sc=1なら等倍。
static int jp_draw_string_scaled(int x, int y, const char* str, uint16_t color, int sc, int x_max) {
  if (sc < 1) sc = 1;
  int penx = x;
  const uint8_t* pp = (const uint8_t*)str;
  while (*pp) {
    uint32_t cp; uint8_t b = *pp;
    if (b < 0x80) { cp = b; pp += 1; }
    else if ((b >> 5) == 0x6) { cp = ((uint32_t)(b & 0x1F) << 6) | (pp[1] & 0x3F); pp += 2; }
    else if ((b >> 4) == 0xE) { cp = ((uint32_t)(b & 0x0F) << 12) | ((uint32_t)(pp[1] & 0x3F) << 6) | (pp[2] & 0x3F); pp += 3; }
    else { pp += 1; continue; }
    int gi = jp_find(cp);
    if (gi < 0) { penx += (jpf::CELL_H / 2) * sc; continue; }
    const jpf::Glyph& g = jpf::GLYPHS[gi];
    if (penx + g.adv * sc > x_max) break;   // 右クリップ：入りきらない字で停止
    int bpr = (g.w + 7) >> 3;
    const uint8_t* bm = &jpf::BITS[g.off];
    for (int r = 0; r < jpf::CELL_H; r++)
      for (int c = 0; c < g.w; c++)
        if (bm[r * bpr + (c >> 3)] & (0x80 >> (c & 7)))
          s_spr.fillRect(penx + c * sc, y + r * sc, sc, sc, color);
    penx += g.adv * sc;
  }
  return penx;
}
// 20260901j：単一エラー本文。ゲート/レーン/センサーA-Bは e.label（例「GW6 レーン1 センサーA」）
//  を本文へ差し込む（%s）。ラベル無しのkindは固定文。書き込み先はcaller提供のバッファ。
static void err_lines_single(const ErrItem& e, char out[6][64], int& n) {
  n = 0;
  switch (e.kind) {
    case ERR_GW_DUP:
      snprintf(out[n++], 64, "⚠ GW 2台 検知");
      snprintf(out[n++], 64, "GWが2台同時に");
      snprintf(out[n++], 64, "動いています。");
      snprintf(out[n++], 64, "どちらか1台の");
      snprintf(out[n++], 64, "電源を切ってください。");
      break;
    // 20260904 改修③：ERR_SEND_FAIL / ERR_NODE_LOST / ERR_BEAM_CUT のcaseを削除
    case ERR_SENSOR_BOTH:
      // 20260901k：A2(片側)と同一体裁に統一。違いはラベルの センサーA/B/AB だけ。
      snprintf(out[n++], 64, "⚠ %s 異常", e.label);
      snprintf(out[n++], 64, "①床センサーの清掃");
      snprintf(out[n++], 64, "②天井LED破損確認");
      break;
    case ERR_SPEED_ONLY:
      snprintf(out[n++], 64, "⚠ %s 異常", e.label);
      snprintf(out[n++], 64, "①床センサーの清掃");
      snprintf(out[n++], 64, "②天井LED破損確認");
      break;
    case ERR_RC_DUP:
      snprintf(out[n++], 64, "⚠ リモコン 2台 検知");
      snprintf(out[n++], 64, "リモコンが2台同時に");
      snprintf(out[n++], 64, "動いています。");
      snprintf(out[n++], 64, "どちらか1台の");
      snprintf(out[n++], 64, "電源を切ってください。");
      break;
    case ERR_SG_DUP:
      snprintf(out[n++], 64, "⚠ シグナル 2台 検知");
      snprintf(out[n++], 64, "シグナルが2台同時に");
      snprintf(out[n++], 64, "動いています。");
      snprintf(out[n++], 64, "どちらか1台の");
      snprintf(out[n++], 64, "電源を切ってください。");
      break;
    case ERR_SECTOR_COMM:
      snprintf(out[n++], 64, "⚠ %s 通信不良", e.label);
      snprintf(out[n++], 64, "SEと通信できません");
      snprintf(out[n++], 64, "全端末再起動");
      break;
    case ERR_ALL_BEAM:
      snprintf(out[n++], 64, "⚠ %s 全ビーム断", e.label);
      snprintf(out[n++], 64, "断線・基盤損傷の疑い");
      snprintf(out[n++], 64, "外乱光を対策");
      break;
    case ERR_READY_NOBEAM:
      snprintf(out[n++], 64, "⚠ %s 受光なし", e.label);
      snprintf(out[n++], 64, "①床センサーの清掃");
      snprintf(out[n++], 64, "②天井LED破損確認");
      break;
  }
}

// 複数時の1行要約（docs 20.5.2(5) の固定順・ASCIIプレースホルダ）
static void err_summary_line(const ErrItem& e, char* out, size_t n) {
  switch (e.kind) {
    case ERR_GW_DUP: snprintf(out, n, "・GW2台検知（1台切る）"); break;
    // 20260904 改修③：ERR_SEND_FAIL / ERR_NODE_LOST / ERR_BEAM_CUT の要約行を削除
    case ERR_SENSOR_BOTH: snprintf(out, n, "・%s 異常", e.label); break;
    case ERR_SPEED_ONLY: snprintf(out, n, "・%s 異常", e.label); break;
    case ERR_RC_DUP: snprintf(out, n, "・リモコン2台検知（1台切る）"); break;
    case ERR_SG_DUP: snprintf(out, n, "・シグナル2台検知（1台切る）"); break;
    case ERR_SECTOR_COMM: snprintf(out, n, "・%s 通信不良", e.label); break;
    case ERR_ALL_BEAM: snprintf(out, n, "・全ビーム断（送信側確認）"); break;
    case ERR_READY_NOBEAM: snprintf(out, n, "・%s 受光なし", e.label); break;
  }
}

//  errs：優先順位順に積んだ配列。cnt=1なら単一詳細、>=2なら複数箇条書き。
static void draw_error(const ErrItem* errs, int cnt) {
  s_spr.fillRect(0, 0, W, BODY_H, C_BLACK);
  if (cnt <= 1) {
    char lines[6][64]; int n = 0;
    if (cnt == 1) err_lines_single(errs[0], lines, n);
    int y = 8;
    for (int i = 0; i < n; i++) { jp_draw_string(8, y, lines[i], C_FINISH); y += JP_LINE_H; }
    // 20260901j：機体/レーン/センサーは本文へ差し込んだので、右上ラベルは廃止。
  } else {
    jp_draw_string(8, 8, "⚠ 複数のエラー", C_FINISH);
    int y = 8 + JP_LINE_H;
    for (int i = 0; i < cnt; i++) {
      char line[80]; err_summary_line(errs[i], line, sizeof(line));
      jp_draw_string(8, y, line, C_FINISH); y += JP_LINE_H;
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
  if (s_ontrack_big) return;   // ★1〜3周モードは特大タイム(size2)をベースで描く＝小サイズ上書きしない
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
