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
#include <math.h>
#include <TFT_eSPI.h>
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
//  当日ベスト4行はプレースホルダ（WiFi切れ時は「-」）。数値流し込みは後続。
// 接続中の機材ID一覧を上段中央に固定表示（READY/ON TRACK共通・フォント1・白・2026-08-24）。
//  READYなら「READY」と「TODAY BEST」の間、ON TRACKなら「ON TRACK」とタイムの間。
static void draw_link_line() {
  s_spr.setTextFont(1);
  s_spr.setTextSize(1);
  s_spr.setTextColor(C_GREY, C_BLACK);   // 接続一覧はグレー（2026-08-24）
  s_spr.setTextDatum(MR_DATUM);          // 中央揃え(MC,186)→右揃えに変更（2026-08-28）
  // ⚠右端はx=182固定。経過秒の部分更新スプライト領域(x=186〜316,y=2〜40)に
  //  1pxでも入ると30fpsのdraw_time_field()に消され→4fpsで復活のチカチカが出る。
  s_spr.drawString(g_status.link, 182, 14);
}

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
  draw_link_line();   // 上段中央に接続機材ID一覧
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
  s_spr.setTextColor(C_ONTRK, C_BLACK);       // 赤系＝これから始まる合図
  // font4(約26px)を size4 で拡大し、本体中央に大書き（英字入りフォントはfont4系のみ）。
  s_spr.setTextFont(4);
  s_spr.setTextSize(4);
  s_spr.drawString("SET", W / 2, BODY_H / 2);
  s_spr.setTextSize(1);                        // 以降のために倍率を戻す
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
  char t[16]; snprintf(t, sizeof(t), "%.1f", elapsed_ms / 1000.0);
  s_spr.drawString(t, W - 8, 8);
  draw_link_line();   // 上段中央に接続機材ID一覧（ON TRACKとタイムの間）

  // 3レーン枠（枠線＝レーン色）。周回カウンタとラップは g_run の実データを描く。
  int lane_w = (W - 16) / 3;
  int row_h  = (laps <= 3) ? 24 : (laps <= 6) ? 20 : 16;   // docs/20.3：周回数で段階縮小
  for (int i = 0; i < 3; i++) {
    int x = 8 + i * lane_w;
    const LaneRun& r = g_run[i];
    s_spr.drawRect(x, 40, lane_w - 4, BODY_H - 48, lane_color(i + 1));
    s_spr.setTextDatum(TL_DATUM);
    s_spr.setTextFont(2);
    char h[12]; snprintf(h, sizeof(h), "L%d", i + 1);
    s_spr.setTextColor(C_WHITE, C_BLACK);
    s_spr.drawString(h, x + 4, 44);
    // 周回カウンタ x/y（完走したら FIN 表示・レーン色）
    s_spr.setTextDatum(TR_DATUM);
    char p[12];
    if (r.fin) snprintf(p, sizeof(p), "FIN");
    else       snprintf(p, sizeof(p), "%u/%d", (unsigned)r.done, laps);
    s_spr.setTextColor(lane_color(i + 1), C_BLACK);
    s_spr.drawString(p, x + lane_w - 8, 44);
    // ラップ縦テーブル（周番号=灰・小 / タイム=白・右揃え）
    int y = 44 + row_h;
    for (int k = 0; k < r.done && k < MAX_LAPS; k++) {
      if (y + row_h > 40 + (BODY_H - 48)) break;           // 枠からはみ出さない
      char num[4]; snprintf(num, sizeof(num), "%d", k + 1);
      s_spr.setTextDatum(TL_DATUM);
      s_spr.setTextColor(C_GREY, C_BLACK);
      s_spr.drawString(num, x + 6, y);
      char lt[12]; snprintf(lt, sizeof(lt), "%.2f", r.lap_ms[k] / 1000.0);
      s_spr.setTextDatum(TR_DATUM);
      s_spr.setTextColor(C_WHITE, C_BLACK);
      s_spr.drawString(lt, x + lane_w - 8, y);
      y += row_h;
    }
  }
}

// ===== 計測終了(FINISH)画面（docs/20.4）====================================
//  各枠：L番号 → 合計タイム（中央・大きく）→ 各周ラップ縦テーブル。
//  完走レーンは合計を表示、未完走（DNF等）は「--.-」のまま。
static void draw_finish(int laps) {
  s_spr.fillRect(0, 0, W, BODY_H, C_BLACK);
  s_spr.setTextDatum(ML_DATUM);
  s_spr.fillCircle(12, 12, 5, C_FINISH);
  s_spr.setTextColor(C_FINISH, C_BLACK);
  s_spr.setTextFont(2);
  s_spr.drawString("FINISH", 24, 12);

  int lane_w = (W - 16) / 3;
  int row_h  = (laps <= 3) ? 20 : (laps <= 6) ? 18 : 16;   // docs/20.4：段階縮小
  for (int i = 0; i < 3; i++) {
    int x = 8 + i * lane_w;
    const LaneRun& r = g_run[i];
    s_spr.drawRect(x, 30, lane_w - 4, BODY_H - 38, lane_color(i + 1));
    s_spr.setTextDatum(TL_DATUM);
    s_spr.setTextFont(2);
    char h[12]; snprintf(h, sizeof(h), "L%d", i + 1);
    s_spr.setTextColor(C_WHITE, C_BLACK);
    s_spr.drawString(h, x + 4, 34);
    // 合計タイム（中央・大きく）。完走のみ実値、未完走は "--.-"
    s_spr.setTextDatum(MC_DATUM);
    s_spr.setTextFont(4);
    char tot[12];
    if (r.fin) {
      if (r.total_ms >= 100000) snprintf(tot, sizeof(tot), "%.1f", r.total_ms / 1000.0);
      else                      snprintf(tot, sizeof(tot), "%.2f", r.total_ms / 1000.0);
    } else {
      snprintf(tot, sizeof(tot), "--.-");
    }
    s_spr.setTextColor(r.fin ? C_WHITE : C_GREY, C_BLACK);
    s_spr.drawString(tot, x + (lane_w - 4) / 2, 62);
    // 各周ラップ縦テーブル
    s_spr.setTextFont(2);
    int y = 82;
    for (int k = 0; k < r.done && k < MAX_LAPS; k++) {
      if (y + row_h > 30 + (BODY_H - 38)) break;
      char num[4]; snprintf(num, sizeof(num), "%d", k + 1);
      s_spr.setTextDatum(TL_DATUM);
      s_spr.setTextColor(C_GREY, C_BLACK);
      s_spr.drawString(num, x + 6, y);
      char lt[12]; snprintf(lt, sizeof(lt), "%.2f", r.lap_ms[k] / 1000.0);
      s_spr.setTextDatum(TR_DATUM);
      s_spr.setTextColor(C_WHITE, C_BLACK);
      s_spr.drawString(lt, x + lane_w - 8, y);
      y += row_h;
    }
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
