// firmware/gw/src/main.cpp
// ============================================================================
//  M4LAPS ゲートウェイ（GW6／予備GW7）ファーム
//  役割（docs/14 DA1/DA2）：全ゲートの集約点。SYNC親時計。EVENT集約→サーバーPOST。
//    JOIN転送（/api/timing/join）。S/G（自分の3レーン）ビーム検出。
//  ＋スタートシーケンス（docs/14 DA11・docs/12.3 状態機械）：
//    赤ボタン(本体) or リモコンCMD_SIGNAL →
//    赤点灯 → 2〜5秒ランダム → 緑点灯(green_t_us記録・F1式) → シグナルへCOMMAND
//    灰ボタン or CMD_RESET → IDLEへ戻す（消灯）
//  ⚠ 「レース中か」の意味づけはアプリ（DA1）。GWが持つのは“演出の進行”のみ。
//  common（protocol/espnow_link/timesync/beam）を呼ぶ。TFTは最小デバッグ表示。
//
//  ============================================================================
//  【20260821 設計改修：POST集約・内部race_id・参加レーン自動確定】
//  ・計測中はサーバー通信を一切しない（D11の純化）。走行中は「受信→内部ridで刻む→
//    LittleFSへ溜める→TFT表示」だけ。赤→緑の演出も裏でPOSTしない（緑時刻はRAM保持）。
//  ・サーバーへのPOST（レース作成＋イベント送信）は次の2契機だけで行う：
//      ① 全員完走（有効化された全レーンが規定周回を完了）＝自動送信
//      ② 灰ボタン（RESET）＝手動送信（CO等で①が立たない時の受け皿・D3）
//  ・race_id は GW内部の連番(s_internal_rid)で刻み、flush時にサーバーrace_idへ翻訳
//    （s_ridmap）。F1式/走行式で作られ方が分岐しない（ensure_raceの特殊分岐を廃止）。
//  ・参加レーン確定：各レーンは「GWのS/Gを1回目通過」で有効化＝完走待ち対象。未通過の
//    空きレーンは計測・完走カウント・送信すべて対象外。DA8によりLC総数は3の倍数＝各機は
//    毎周同じ物理レーンでS/Gを切るため、物理レーン別クロス数＝そのレーンの周回数になる。
//    活性化(1回目)は起点なので、完走条件は「クロス数 ≥ target_laps + 1」。
//  ・計測中の赤ボタンは無視（G2）。やり直しは「灰→赤」。
//  ・HTTPS（レース作成/送信）は必ずloop文脈で実行（ESP-NOWコールバックでI/O禁止・#20）。
// ============================================================================
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <LittleFS.h>
#include <ArduinoJson.h>
#include "protocol.h"
#include "espnow_link.h"
#include "timesync.h"
#include "beam.h"
#include "display.h"
#include "secrets.h"
#include <Preferences.h>   // NVS：アプリからUSBで焼く設定の読み込み用（ESP32コア標準）

#ifndef NODE_ID
#define NODE_ID 6
#endif
#ifndef ESPNOW_CHANNEL
#define ESPNOW_CHANNEL 1
#endif

// ⚠ #8-a（docs/21.4）：レイアウトID/周回数を実行時変数化。
//   #define は「起動時の既定値」として残し、以後は s_layout_id / s_target_laps を使う。
//   fetch_layout() 成功時に取得値で s_target_laps を更新（サーバー主導・案P土台）。
#ifndef DEFAULT_LAYOUT_ID
#define DEFAULT_LAYOUT_ID 1
#endif
#ifndef DEFAULT_TARGET_LAPS
#define DEFAULT_TARGET_LAPS 3
#endif
static uint32_t s_layout_id   = DEFAULT_LAYOUT_ID;    // 現在のレイアウトID（起動既定→将来アプリ追従）
static int      s_target_laps = DEFAULT_TARGET_LAPS;  // 現在の周回数（fetch_layoutで更新）
static int      s_node_need   = 0;   // レイアウト要求ノード数（fetch_layoutのnode_count・0=未取得で「-」表示）

// ---- サーバ接続（K5：DNS回避＝IP直＋Hostヘッダ／#9保険＝DNS二段構え）--------
//  ⚠ESP32の hostByName() が失敗するため、暫定でIP直＋Hostヘッダを使ってきた。
//    #9保険（docs/21・残#9）：起動時にDNS解決を試し、通ればホスト名運用へ。
//    ダメなら従来のIP直＋Hostへフォールバック。会場でDNSが直っても再書込み不要。
//    ⚠IPが変わったらここ1箇所を直す（残課題#9・会場WiFiで要再確認）。
// 既定値（NVSに設定が無ければこれ＝従来と同じ挙動）。
static const char* DEF_SERVER_IP   = "133.117.77.69";
static const char* DEF_SERVER_HOST = "v133-117-77-69.sefs.static.cnode.jp";

// 実行時の設定（NVS "m4cfg" が上書き。無ければ既定 / secrets.h）。load_config()で確定。
static String g_ssid  = WIFI_SSID;
static String g_pass  = WIFI_PASS;
static String g_host  = DEF_SERVER_HOST;
static String g_ip    = DEF_SERVER_IP;
static String g_token = TIMING_TOKEN;
// g_channel：起動時の初期ch（ブートストラップ／オフライン集合用）。WiFi接続後は
//   gw_adopt_wifi_channel() が“スマホAPの実ch”で上書きし、以後はそれが運用ch（状態B）。
static uint8_t g_channel = ESPNOW_CHANNEL;

// DNS解決可否。true＝ホスト名でつなぐ / false＝IP直＋Hostヘッダ（暫定）。
static bool s_dns_ok = false;

// つなぎ先ベースURL。DNS可ならホスト名、不可ならIP直。
static inline String server_base() {
  return s_dns_ok ? (String("https://") + g_host)
                  : (String("https://") + g_ip);
}
// Hostヘッダを付けるべきか（IP直のときだけ必要。ホスト名運用時は不要）。
static inline bool need_host_header() { return !s_dns_ok; }

// http.begin後に共通ヘッダを付ける（Host＋任意トークン）。IP直時のみHost付与。
static void add_common_headers(HTTPClient& http) {
  if (need_host_header()) http.addHeader("Host", g_host);
  if (g_token.length())   http.addHeader("X-Timing-Token", g_token);
}

// ---- fetch_layout の再取得間隔（残課題#14：成功=60s / 失敗=5s のバックオフ）--
static constexpr uint32_t LAYOUT_OK_MS   = 60000;   // 取得成功後は60秒あける
static constexpr uint32_t LAYOUT_NG_MS   = 30000;   // ★失敗中は30秒で再試行（旧5秒＝TLSブロック頻発でESP-NOWを邪魔していた・T-9）
static bool s_layout_ok = false;                    // 直近取得の成否

// ---- 本体ボタン（docs/07.4）-----------------------------------------------
static constexpr int PIN_BTN_RED  = 26;   // 赤 SIGNAL（押下LOW・内部プルアップ）
static constexpr int PIN_BTN_GRAY = 27;   // 灰 RESET
static constexpr uint32_t DEBOUNCE_MS   = 20;
static constexpr uint32_t RESET_HOLD_MS = 600;   // RESETは600ms長押し（docs/12 S9）
// 24.3(1-e)：送信失敗中に灰ボタンを OVERRIDE_HOLD_MS 長押しすると「封じ強制解除」。
//   長時間WiFi不通時、運営が承知の上で次レースへ進むための緊急解除（灰ボタン起点で
//   既存操作と衝突しない。データはスプールに残るのでD1で後日後送り可能）。
static constexpr uint32_t OVERRIDE_HOLD_MS = 3000;  // 灰3秒超で封じ強制解除

// ---- ブザー（29章／GPIO12・自励式109800・実機確定 20260811）--------------
//  待機中(ST_IDLE)にGW自身のS/Gを車が通過した瞬間だけ 60ms「ピッ」。自励式ゆえHIGH/LOWのみ。
//  ⚠非ブロッキング：loopをdelayで止めない（millis管理で消灯）。計測中・エラー面では鳴らさない。
static constexpr int      PIN_BUZZER = 12;   // ストラップだが実機で書込み/起動OK確認済(20260811)
static constexpr uint32_t BUZZER_MS  = 60;   // 鳴動長（80→40→20→60で実機比較し確定）
static constexpr uint32_t BUZZER_ERR_MS = 1000;  // エラー発生時の「ピー」（1秒・1回だけ）
static uint32_t s_buzz_off_ms = 0;           // 0=消灯中／非0=この millis で消灯予定
static bool     s_sg_beeped   = false;       // ST_IDLE中に既にS/G通過ピッを鳴らしたか（TAの毎回鳴り防止・灰リセットで復活）
static inline void buzzer_start(uint32_t ms) {   // 非ブロッキング開始（ms鳴らす）
  digitalWrite(PIN_BUZZER, HIGH);
  s_buzz_off_ms = millis() + ms;
  if (s_buzz_off_ms == 0) s_buzz_off_ms = 1; // millis境界で0になる稀ケースを回避
}
static inline void buzzer_beep() {           // S/G通過の瞬間に呼ぶ（60ms「ピッ」）
  buzzer_start(BUZZER_MS);
}
static inline void buzzer_tick() {           // loop毎に呼ぶ（時間が来たら消灯）
  if (s_buzz_off_ms && (int32_t)(millis() - s_buzz_off_ms) >= 0) {
    digitalWrite(PIN_BUZZER, LOW);
    s_buzz_off_ms = 0;
  }
}
// エラー表示の立ち上がり（無→有）でだけ 1秒「ピー」を1回鳴らす。
//  err_now=エラー面を出しているか。出し続けている間は再鳴動しない（1回だけ）。
//  エラーが一旦消えて再発したら、また1回鳴る。tick_display（約4fps）から呼ぶ。
static inline void buzzer_error_edge(bool err_now) {
  static bool prev = false;
  if (err_now && !prev) buzzer_start(BUZZER_ERR_MS);
  prev = err_now;
}

// ---- スタート演出の状態機械（docs/12.3）-----------------------------------
enum GwState { ST_SPLASH, ST_IDLE, ST_ARMED, ST_GREEN, ST_RACE, ST_FINISH };
static GwState s_state = ST_IDLE;
static uint32_t s_splash_ms = 0;                 // スプラッシュ開始時刻（ST_SPLASH中）
static constexpr uint32_t SPLASH_MS = 3200;      // スプラッシュ総時間（締めの大拡大ぶん延長）
// 赤押下でSETを「即座に」出すための一発フラグ。on_signal_pressed（ESP-NOW受信
// コールバック文脈からも呼ばれる）でSPIを叩くとクラッシュしうるため、ここでは
// フラグだけ立て、実描画は loop（安全な文脈）で render_set_now() が行う。
static volatile bool s_set_now_req = false;

// ---- COMMAND冪等化（SIGNAL二重発火の絶対防止・20260819）----------------
//  RCは1押下ごとに一意 nonce(=CommandBody.arg) を採番し、その押下の全再送に同値を載せる。
//  GWは直近の (src,nonce) を覚え、同一の再来はACKのみでアクションを実行しない。
//  → 状態（ST_IDLE復帰）に依存せず「1押下＝最大1アクション」を保証。
//  srcごとに持つので RC8/RC9 を同時に使っても nonce が混ざらない。
//  arg==0 は旧RC互換（nonce無し）＝従来の状態ガードに委ねる。
struct CmdKey { uint8_t src; uint32_t nonce; };
static CmdKey  s_cmd_seen[8] = {};
static uint8_t s_cmd_seen_pos = 0;
static bool cmd_nonce_seen(uint8_t src, uint32_t nonce) {
  if (nonce == 0) return false;                 // 0は「nonce無し」（旧RC互換）
  for (uint8_t i = 0; i < 8; i++)
    if (s_cmd_seen[i].src == src && s_cmd_seen[i].nonce == nonce) return true;
  s_cmd_seen[s_cmd_seen_pos].src   = src;
  s_cmd_seen[s_cmd_seen_pos].nonce = nonce;
  s_cmd_seen_pos = (uint8_t)((s_cmd_seen_pos + 1) & 7);
  return false;
}
static uint32_t s_armed_ms = 0;         // ARMEDに入った時刻
// ★CMD_RED再送（灰→赤/初回赤の取りこぼし対策・2026-08-26）
static constexpr uint32_t RED_RESEND_WINDOW_MS = 400;  // ARMED後この時間だけ赤を再送
static constexpr uint32_t RED_RESEND_EVERY_MS  = 60;   // 再送間隔（60ms毎＝約6回×3連送）
static uint32_t s_red_resend_ms = 0;    // 最後にCMD_REDを再送した時刻
static uint32_t s_red_dur_ms = 0;       // 今回の赤の長さ（3秒＋ランダム）
static uint64_t s_green_t_us = 0;       // 緑を出したGW時刻（F1式・green_t_us）。走行式は0のまま

// ---- レース識別・参加レーン（20260821改修）--------------------------------
//  s_internal_rid：GW内部の連番。スプール各行に刻む“取り違え防止用”のID。サーバーの
//    race_id とは別物で、flush時に s_ridmap で翻訳する。0=まだレース未開始。
//  s_race_active：現在レースが進行中か（begin_internal_race で true、flush/RESETで false）。
//  s_lane_cross/s_lane_active：GWのS/G(自機3レーン)の物理レーン別クロス数と活性化。
//    活性化(1回目通過)＝参加確定。完走は cross ≥ target_laps+1（起点1回ぶん足す）。
static uint32_t s_internal_rid = 0;
static bool     s_race_active  = false;
static uint32_t s_lane_cross[4] = {0,0,0,0};   // index 1..3（0は未使用）
static bool     s_lane_active[4] = {false,false,false,false};
// ラップ・合計計算用の打刻（自機S/G・index 1..3）。20260830追加（TFTラップ表示）。
static uint64_t s_lane_first_us[4] = {0,0,0,0};  // 起点通過（走行式の合計の起点）
static uint64_t s_lane_last_us[4]  = {0,0,0,0};  // 直近通過（ラップ＝前回との差分）
// s_need_flush：送信要求フラグ。全員完走 or 灰ボタンで立て、loopのtick_flushがHTTPSを実行。
//   （on_reset_pressed はESP-NOWコールバックからも呼ばれるためHTTPSを直接呼ばない・#20）
static bool     s_need_flush   = false;

// ---- 受信EVENTレコード -----------------------------------------------------
struct RawEvent {
  uint8_t src; uint32_t src_boot; uint32_t seq;
  uint8_t lane; uint8_t quality; uint64_t t_us; uint64_t t_us_b;
};

static const char* SPOOL = "/spool.jsonl";
// 24.3(1-e)：送信失敗中は「次の赤ボタン(新レース開始)を封じる」安全フラグ。
//   flush_spool 成功でクリア、失敗でセット。灰3秒長押し(緊急解除)でも手動クリア可。
static bool s_send_blocked = false;

// ---- 内部rid → サーバーrace_id の対応表（flush時に確定・20260821）----------
//  同じ内部ridの2回目以降のflush（100件超で分割送信）では既に作ったサーバーrace_idを
//  再利用する。races が短命で順次flushされるため 8枠で足りる。
struct RidMap { uint32_t internal; uint32_t server; };
static RidMap  s_ridmap[8] = {};
static uint8_t s_ridmap_pos = 0;
static uint32_t ridmap_lookup(uint32_t internal) {
  if (internal == 0) return 0;
  for (uint8_t i = 0; i < 8; i++)
    if (s_ridmap[i].internal == internal) return s_ridmap[i].server;
  return 0;
}
static void ridmap_store(uint32_t internal, uint32_t server) {
  for (uint8_t i = 0; i < 8; i++)
    if (s_ridmap[i].internal == internal) { s_ridmap[i].server = server; return; }
  s_ridmap[s_ridmap_pos].internal = internal;
  s_ridmap[s_ridmap_pos].server   = server;
  s_ridmap_pos = (uint8_t)((s_ridmap_pos + 1) & 7);
}

// ---- ③群 GW配線（A1/A2・Sync・C1 Lost・24.4⑤画面フロー・27章）--------------
//  A1(q=2 両ビーム欠)/A2(q=1 片ビーム欠)は per-raceで最悪値を保持し、灰ボタン後3秒だけ表示。
//  Sync(q=3 未同期打刻)は自動復帰型なので直近受信時刻だけ持つ。
//  C1 Lostは give-up通知(PT_LOST_NOTICE)で立て、灰リセットまで出しっぱなし。
static bool     s_race_q2 = false;  static uint8_t s_race_q2_src = 0;  // A1：両ビーム欠(q=2)
static bool     s_race_q1 = false;  static uint8_t s_race_q1_src = 0;  // A2：片ビーム欠(q=1)
static bool     s_beam_bad = false;  // Beam×：片/両ビーム欠を検知したら保持（灰リセットで解除）
static uint32_t s_last_q3_ms     = 0;   // B1：未同期打刻(q=3)を最後に受けた時刻→Sync×(自動復帰)
static uint32_t s_post_err_until = 0;   // 24.4⑤：灰ボタン後3秒だけA1/A2を出す窓(millis基準)
static uint8_t  s_lost_src       = 0;   // C1：give-up通知の発生SQ(Lostラベル用・24.14)

// 受信/自機の通過qualityを per-raceフラグへ反映（A1/A2/Sync集約の入口）。
static inline void note_quality(uint8_t q, uint8_t src) {
  if (q == 2)      { if (!s_race_q2) { s_race_q2 = true; s_race_q2_src = src; } }
  else if (q == 1) { if (!s_race_q1) { s_race_q1 = true; s_race_q1_src = src; } }
  if (q == 1 || q == 2) s_beam_bad = true;   // 片/両ビーム欠でBeam×（灰リセットまで保持）
  else if (q == 3) { s_last_q3_ms = millis(); }
}

// ---- 排他運用（GW/RC/SG は各ペア1台のみ・在席テーブル）----------------------
//  方針（本改修）：GW6/7・RC8/9・SG10/11 は「どちらか1台だけ電源ON」で運用する。
//    同種が2台“生きている”ことを検知したら排他ロックし、新規スタートを受け付けない
//    （既存の「GW2台オン検知」思想＝docs/14 §14.11 を RC/SG にも統合）。
//    片方の電源を切れば PRESENCE_TIMEOUT_MS 経過後に警告が自動で消え、待機へ戻る。
//  GWはハブとして全機の受信でsrcを見て在席を記録する。GW同士は自分の在席ビーコン
//    （tick_gw_presence）で互いに検知する。RC/SGは画面を持たないためGWのTFTへ代理表示。
static constexpr uint32_t PRESENCE_TIMEOUT_MS = 10000;  // 10秒無受信で離脱扱い（docs/11.4）
static constexpr uint32_t GW_BEACON_MS        = 2000;   // GW自身の在席ビーコン周期
static uint32_t s_last_seen[proto::NODE_ID_MAX + 1] = {0};
static bool     s_ever_seen[proto::NODE_ID_MAX + 1] = {false};
static uint8_t  s_active_sg_id = 10;   // 現在通電中のシグナルID（10 or 11）＝COMMAND送信先

// 受信のたびに送信元node_idの在席時刻を更新する（0xFE/0xFFは無視）。
static inline void mark_seen(uint8_t src) {
  if (src > proto::NODE_ID_MAX) return;
  s_last_seen[src] = millis();
  s_ever_seen[src] = true;
}
static inline bool node_alive(uint8_t id) {
  return s_ever_seen[id] && (millis() - s_last_seen[id] <= PRESENCE_TIMEOUT_MS);
}

// 排他状態を評価。g_status.gw_dup/rc_dup/sg_dup を更新し、いずれか競合なら true。
//  あわせて“生きているSG”を s_active_sg_id に反映し、予備SG単独運用でも光るようにする。
static bool eval_exclusivity() {
  bool gw_peer = false; int rc = 0, sg = 0; int last_sg = -1;
  for (uint8_t id = 0; id <= proto::NODE_ID_MAX; id++) {
    if (!node_alive(id)) continue;
    switch (proto::kind_of(id)) {
      case proto::KIND_GW: if (id != NODE_ID) gw_peer = true; break;  // 自分以外のGW＝重複
      case proto::KIND_RC: rc++; break;
      case proto::KIND_SG: sg++; last_sg = id; break;
      default: break;   // SQ等は排他対象外
    }
  }
  if (last_sg >= 0) s_active_sg_id = (uint8_t)last_sg;   // 生存SGへCMDを向ける
  disp::g_status.gw_dup = gw_peer;
  disp::g_status.rc_dup = (rc >= 2);
  disp::g_status.sg_dup = (sg >= 2);
  return gw_peer || (rc >= 2) || (sg >= 2);
}
static inline bool exclusivity_conflict() {
  return disp::g_status.gw_dup || disp::g_status.rc_dup || disp::g_status.sg_dup;
}

// ---- WiFi ------------------------------------------------------------------
//  #9保険：接続確立後に一度だけDNS解決を試し、s_dns_ok を決める。
//  DNS明示（8.8.8.8/1.1.1.1）は、ルーターがESP32へDNSを配らない会場対策。
static void probe_dns() {
  // 既にDNS運用中なら再判定しない（毎回のhostByNameは無駄＆遅延源）。
  if (s_dns_ok) return;
  IPAddress ip;
  if (WiFi.hostByName(g_host.c_str(), ip) == 1 && ip != IPAddress(0,0,0,0)) {
    s_dns_ok = true;
    Serial.printf("[DNS] OK %s -> %s（ホスト名運用へ）\n",
                  g_host.c_str(), ip.toString().c_str());
  } else {
    s_dns_ok = false;
    Serial.println("[DNS] NG（IP直＋Hostヘッダにフォールバック）");
  }
}

// ---- WiFi実chの採用（状態B・docs/21 段階1）--------------------------------
//  GWのSTAは接続したAP（＝スマホのホットスポット）のchに自動で乗る。そのchを読み、
//  ESP-NOW側（peer/表示/JOIN_ACKで配る値）を実chに一致させる。固定chは持たない。
//  ・接続後にAP側のchが変わった場合もここで拾い直して追従する（ノードは各自で再走査）。
//  ・未接続時は呼ばれない＝g_channel はブートストラップchのまま（オフライン集合用）。
static void gw_adopt_wifi_channel() {
  uint8_t primary = 0;
  wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
  if (esp_wifi_get_channel(&primary, &second) != ESP_OK) return;
  if (primary < 1 || primary > 13) return;
  if (primary == g_channel) return;                 // 変化なし
  Serial.printf("[CH] adopt WiFi ch=%u (was %u)\n", primary, g_channel);
  g_channel = primary;
  mesh::set_channel(primary);                       // ESP-NOW peer を実chへ合わせる
  disp::g_status.ch = g_channel;                    // TFT表示も更新
}

// ---- WiFi（非ブロッキング・2026-08-24e）------------------------------------
//  旧実装は WiFi.begin 後に最大8秒のビジーウェイトがあり、未接続環境では
//  tick_fetch_layout(失敗時5秒間隔)と組み合わさって loop がほぼ常時停止。
//  → TFTがREADYのまま固まる／赤・灰の押下が丸ごと消える の根本原因。
//  本実装：接続済みなら true。未接続なら begin() を仕掛けて即 false
//  （完了待ちしない）。再beginは15秒間隔。probe_dns等は接続完了の
//  立ち上がりで1回だけ実行する。
static uint32_t s_wifi_try_ms = 0;      // 直近 begin() を仕掛けた時刻
static bool     s_wifi_began  = false;  // begin() 済みか
static bool     s_wifi_was_up = false;  // 接続完了エッジ検出用
static constexpr uint32_t WIFI_RETRY_MS = 15000;

static bool wifi_up() {
  if (WiFi.status() == WL_CONNECTED) {
    if (!s_wifi_was_up) {                       // 接続完了の立ち上がりで1回だけ
      s_wifi_was_up = true;
      WiFi.setSleep(false);                     // ★AP接続で省電力が戻るのを再無効化（ESP-NOW脱落防止・T-8）
      probe_dns(); gw_adopt_wifi_channel();     // DNS判定＋実ch採用（従来どおり）
    }
    return true;
  }
  s_wifi_was_up = false;
  uint32_t now = millis();
  if (!s_wifi_began || (now - s_wifi_try_ms) >= WIFI_RETRY_MS) {
    s_wifi_began  = true;
    s_wifi_try_ms = now;
    // DNSを明示（ルーターがDNS未配布でも解決を試せるように）。SSID接続前に設定。
    WiFi.config(IPAddress(0,0,0,0), IPAddress(0,0,0,0), IPAddress(0,0,0,0),
                IPAddress(8,8,8,8), IPAddress(1,1,1,1));
    WiFi.begin(g_ssid.c_str(), g_pass.c_str()); // 非ブロッキング：完了待ちしない
    Serial.println("[WIFI] begin (non-blocking)");
  }
  return false;                                 // 未接続。呼び出し元は今回は見送る
}

// ---- 新レース作成（layout_id・任意でgreen_t_usを付ける）--------------------
//  green あり → F1式 / green なし → 走行式（docs/14 DA4）。
static uint32_t create_race(bool with_green, uint64_t green_us) {
  if (!wifi_up()) return 0;
  JsonDocument doc;
  doc["target_laps"] = s_target_laps;
  doc["layout_id"]   = s_layout_id;
  if (with_green) doc["green_t_us"] = green_us;
  String body; serializeJson(doc, body);

  WiFiClientSecure _c; _c.setInsecure();            // K4：ローカル生成＋CN検証なし
  HTTPClient http;
  http.begin(_c, server_base() + "/api/timing/races");   // #9：DNS可ならホスト名/不可ならIP直
  http.setConnectTimeout(800); http.setTimeout(1500);   // ★不達でも最長0.8秒で諦める（初回赤の取りこぼし対策・T-9）
  add_common_headers(http);                         // Host(IP直時のみ)＋トークン
  http.addHeader("Content-Type", "application/json");
  uint32_t rid = 0;
  int code = http.POST(body);
  if (code == 200) {
    JsonDocument res;
    if (deserializeJson(res, http.getString()) == DeserializationError::Ok)
      rid = res["race_id"] | 0;
  }
  http.end();
  return rid;
}

// ---- シグナルへ COMMAND（赤/緑/リセット）----------------------------------
static void signal_cmd(uint8_t code) {
  proto::CommandBody c = {};
  c.code = code;
  // 生きているシグナル（SG10 or 予備SG11）へ送る。既定はSG10。
  // ★2026-08-25：ブロードキャストはACKが無く単発だと1回のパケットロスで取りこぼす
  //   （起動直後の初回赤がSGに届かない事象＝T-9）。CMD_RED/GREEN/RESETは受信側で冪等なので
  //   3連送して確実性を上げる（seqは毎回+1され重複排除に掛からず全て配送される）。
  //   ⚠ 本関数はESP-NOWコールバック(CMD_RESET)からも呼ばれるため delay() は入れない。
  for (int i = 0; i < 3; i++) {
    mesh::send(proto::PT_COMMAND, s_active_sg_id, &c, sizeof(c));
  }
}

// ---- 内部レース開始（内部rid採番＋参加レーン/緑/per-raceフラグの初期化）------
//  on_signal_pressed が on_reset_pressed を先に呼ぶため前方宣言（定義は後方）。
static void on_reset_pressed();
//  F1式：on_signal_pressed から。走行式：最初のS/G通過時に loop から呼ぶ。
//  ⚠ HTTPSは一切しない（サーバー作成はflush時）。ここは純粋にRAM上の初期化のみ。
static void begin_internal_race() {
  s_internal_rid++;
  if (s_internal_rid == 0) s_internal_rid = 1;   // wrap回避（0は「未開始」の意味に予約）
  s_race_active = true;
  s_green_t_us  = 0;                              // 走行式は0のまま／F1は緑点灯時に上書き
  for (int i = 1; i <= 3; i++) { s_lane_cross[i] = 0; s_lane_active[i] = false;
                                 s_lane_first_us[i] = 0; s_lane_last_us[i] = 0; }
  disp::run_reset();                              // TFTのラップ表示もクリア（20260830）
  s_race_q2 = false; s_race_q1 = false;          // 24.4：A1/A2 per-raceフラグをclear
}

// tick_display を介さず SET 画面を即時に1回描くための前方宣言（赤押下の即応用）。
static void render_set_now();
// 緑点灯と同時に計測中(ON TRACK)画面を即時描画するための前方宣言（緑の即応用）。
static void render_ontrack_now();
// ---- スタートシーケンス制御 -----------------------------------------------
//  赤ボタン/CMD_SIGNAL共通の入口（docs/03「本体とリモコンで同じ処理」）。
static void on_signal_pressed() {
  if (s_state == ST_SPLASH) { s_state = ST_IDLE; Serial.println("[SPLASH] skip -> READY"); return; }
  // docs要望2026-08-24b：赤を受け付けるのは READY(待機=ST_IDLE) のときだけ。
  //   計測中・ARMED・GREEN・RACE、および走行式レース活性中は赤を無視する。
  //   やり直しは従来どおり「灰(RESET)→赤」の順。
  if (s_state != ST_IDLE) {   // READY(=IDLE画面)のときだけ受付。s_race_active は画面と無関係なので見ない
    Serial.println("[SEQ] ignored: not READY (赤はREADYのときだけ。やり直しは灰→赤)");
    return;
  }
  if (exclusivity_conflict()) {                 // GW/RC/SG重複中は新規スタートを受け付けない
    Serial.println("[SEQ] blocked: GW/RC/SG duplicate active (power off one)");
    return;
  }
  // 演出のみ即実行。HTTPS（レース作成）はここでは一切しない（#20・本関数はESP-NOW
  //  受信コールバック文脈からも呼ばれるため、ブロッキングI/O禁止）。作成はflush時。
  begin_internal_race();                          // 内部rid採番＋参加レーン初期化
  s_red_dur_ms = 2000 + (esp_random() % 3000);   // 2.0〜5.0秒（DC26・旧3〜5秒）。RCも本関数を通るため同じ
  s_armed_ms   = millis();
  s_red_resend_ms = millis();                     // 再送タイマ基点（以後tick_sequenceで再送）
  s_state      = ST_ARMED;
  signal_cmd(proto::CMD_RED);
  s_set_now_req = true;                           // ★即SET要求（実描画はloopで・コールバック文脈のSPIクラッシュ回避）
  Serial.printf("[SEQ] ARMED rid=%u red=%ums\n", s_internal_rid, s_red_dur_ms);
}

// flush_spool / spool_has_data は本関数より後方で定義されるため前方宣言。
static bool spool_has_data();
static void flush_spool();

static void on_reset_pressed() {
  // 24.3：灰ボタン押下時、スプールに溜まっていれば送信（空なら何もしない）。
  //   ⚠ 本関数はESP-NOWコールバック(CMD_RESET)からも呼ばれるため、ここでHTTPSは
  //     しない。送信要求(s_need_flush)だけ立て、実送信は loop の tick_flush が行う(#20)。
  //   WiFi不通時は tick_flush 内で送らず、データはスプールに残る（消えない）。
  bool was_idle = (s_state == ST_IDLE);   // F-1：この灰が「走行終了」か「待機での確認」かを区別
  if (spool_has_data()) s_need_flush = true;

  // 24.4⑤：待機画面に入った直後、3秒間だけA1/A2エラーを表示する窓を開く。
  s_post_err_until = millis() + 3000;
  // C1(24.14/F-1)：Lostは待機画面で一度見せてから灰で解除する。
  //   走行を止めた灰(was_idle=false)では消さない＝走行中に届いたLostが
  //   未表示のまま消えるのを防ぐ。待機で見えている状態の灰(was_idle=true)が「確認＝解除」。
  if (was_idle) { disp::g_status.lost = false; s_lost_src = 0; }

  s_state      = ST_IDLE;
  s_race_active = false;        // 現レースを閉じる（次のS/G通過は新しい内部レースになる）
  s_beam_bad   = false;         // Beam×も待機復帰でクリア
  s_sg_beeped  = false;         // リセットで「次の1回」を再び鳴らせるようにする（新しい待機セッション）
  s_green_t_us = 0;
  signal_cmd(proto::CMD_RESET);
  Serial.println("[SEQ] RESET -> IDLE (flush queued if data)");
}

// ARMED経過で緑へ。loopから呼ぶ。緑時刻はRAM保持のみ（POSTしない。flush時に同梱）。
static void tick_sequence() {
  // ★2026-08-26：ARMED直後の一定時間、CMD_REDを再送し続ける。
  //   灰→赤直後などでサーバーTLS試行がloopを一瞬ブロックし、単発（3連送）のCMD_REDを
  //   SGが取りこぼす事象（初回赤/灰→赤で点灯しない）への対策。SGはCMD_RED冪等なので
  //   再送は無害。緑遷移（s_red_dur_ms＝2〜5秒）より十分手前で送り終わる。
  if (s_state == ST_ARMED) {
    uint32_t since = millis() - s_armed_ms;
    if (since < RED_RESEND_WINDOW_MS &&
        (millis() - s_red_resend_ms) >= RED_RESEND_EVERY_MS) {
      s_red_resend_ms = millis();
      signal_cmd(proto::CMD_RED);
    }
  }
  if (s_state == ST_ARMED && (millis() - s_armed_ms) >= s_red_dur_ms) {
    s_green_t_us = tsync::now_gw_us();          // 緑を出した瞬間＝green_t_us（F1式起点）
    s_state = ST_GREEN;
    signal_cmd(proto::CMD_GREEN);
    Serial.printf("[SEQ] GREEN t=%llu (rid=%u)\n",
                  (unsigned long long)s_green_t_us, s_internal_rid);
    s_state = ST_RACE;
    render_ontrack_now();   // ★緑点灯と同時に計測中画面へ即切替＋カウント開始（250ms待ちを迂回）
  }
}

// ---- JOIN転送のレート制限（#18：同一MACは30秒に1回だけサーバーへ転送）------
//  未アサインのノードは1秒ごとにJOINを送ってくる（tick_presence）。その都度
//  HTTPSを張るとTLS失敗の嵐＋ヒープ圧迫になるため、転送だけを間引く。
static bool join_rate_ok(const uint8_t* mac) {
  static uint8_t  macs[8][6];
  static uint32_t last_ms[8];
  static int      n = 0;
  uint32_t nowm = millis();
  for (int i = 0; i < n; i++) {
    if (memcmp(macs[i], mac, 6) == 0) {
      if (nowm - last_ms[i] < 30000) return false;   // 30秒以内は捨てる
      last_ms[i] = nowm;
      return true;
    }
  }
  if (n < 8) { memcpy(macs[n], mac, 6); last_ms[n] = nowm; n++; }
  return true;                                        // 初見MACは通す
}

// ---- JOIN転送 --------------------------------------------------------------
static void forward_join(const proto::JoinBody& jb) {
  if (!join_rate_ok(jb.mac)) return;                  // #18：HTTPS連打を抑止
  if (!wifi_up()) return;
  char mac[18];
  snprintf(mac, sizeof(mac), "%02X:%02X:%02X:%02X:%02X:%02X",
           jb.mac[0],jb.mac[1],jb.mac[2],jb.mac[3],jb.mac[4],jb.mac[5]);
  JsonDocument doc;
  doc["mac"]         = mac;
  doc["kind"]        = jb.kind;
  doc["fw_major"]    = jb.fw_major;
  doc["fw_minor"]    = jb.fw_minor;
  doc["nvs_node_id"] = jb.nvs_node_id;
  String body; serializeJson(doc, body);

  WiFiClientSecure _c; _c.setInsecure();
  HTTPClient http;
  http.begin(_c, server_base() + "/api/timing/join");
  http.setConnectTimeout(800); http.setTimeout(1500);   // ★不達でも最長0.8秒で諦める（初回赤の取りこぼし対策・T-9）
  add_common_headers(http);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  if (code == 200) {
    JsonDocument res;
    if (deserializeJson(res, http.getString()) == DeserializationError::Ok) {
      const char* status = res["status"] | "";
      if (strcmp(status, "assigned") == 0 && res["node_id"].is<int>()) {
        int nid = res["node_id"].as<int>();
        proto::JoinAckBody ack = {};
        memcpy(ack.mac, jb.mac, 6);
        ack.node_id = (uint8_t)nid;
        ack.channel = g_channel;
        mesh::send(proto::PT_JOIN_ACK, (uint8_t)nid, &ack, sizeof(ack));
      }
    }
  }
  http.end();
}

// ---- EVENTスプール ---------------------------------------------------------
// スプール上限（超過分は古い順に破棄）。
static constexpr int SPOOL_MAX = 200;
static int spool_count();   // 後方定義の前方宣言
// 先頭（最古）から (count-keep) 行を削除して keep 行に収める。
static void spool_trim_to(int keep) {
  int total = spool_count();
  if (total <= keep) return;
  int drop = total - keep;
  File in = LittleFS.open(SPOOL, FILE_READ);
  if (!in) return;
  const char* TMP2 = "/spool_trim.jsonl";
  File out = LittleFS.open(TMP2, FILE_WRITE);
  if (!out) { in.close(); return; }
  int idx = 0;
  while (in.available()) {
    String line = in.readStringUntil('\n');
    if (line.length() == 0) continue;
    if (idx >= drop) { out.print(line); out.print('\n'); }   // 古いdrop行はスキップ
    idx++;
  }
  in.close(); out.close();
  LittleFS.remove(SPOOL);
  LittleFS.rename(TMP2, SPOOL);
}

static void spool_append(const RawEvent& e) {
  // q=2連続の間引き：同一レーンでq=2が0.5秒以内に連続したら2件目以降は捨てる
  //   （張り付き/未接続の洪水を発生源で止める。正当なエラーは先頭1件だけ残る）。
  if (e.quality == 2) {
    static uint32_t last_q2_ms[4] = {0,0,0,0};    // lane 1..3（0は未使用）
    uint8_t ln = (e.lane >= 1 && e.lane <= 3) ? e.lane : 0;
    uint32_t now = millis();
    if (last_q2_ms[ln] != 0 && (now - last_q2_ms[ln]) < 500) { last_q2_ms[ln] = now; return; }
    last_q2_ms[ln] = now;
  }
  // 上限：追記前に (SPOOL_MAX-1) 行へ詰めておく（append後にちょうど上限）。
  spool_trim_to(SPOOL_MAX - 1);

  File f = LittleFS.open(SPOOL, FILE_APPEND);
  if (!f) return;
  JsonDocument doc;
  doc["device_id"]   = e.src;      // device_id と src は同じ（送信元node_id）
  doc["src"]         = e.src;
  doc["src_boot_id"] = e.src_boot;
  doc["seq"]         = e.seq;
  doc["lane"]        = e.lane;
  doc["t_us"]        = e.t_us;
  if (e.t_us_b) doc["t_us_b"] = e.t_us_b;
  doc["quality"]     = e.quality;
  // 24.3(1-c)＋20260821：race_id は「受信した瞬間」の“内部rid”を刻む。サーバーの
  //   race_id は flush 時に採番して s_ridmap で翻訳するので、受信〜送信間の再起動等で
  //   取り違えが起きない。rid はスプール内部の振り分け専用（送信payloadには載せない。
  //   サーバーの冪等キーは device_id/src/src_boot_id/seq で race_id を含まないため）。
  //   grn：その時点の緑時刻（F1式のgreen_t_us・走行式は0）。flush時にグループ内の最大値を
  //   採ってレース作成に同梱する（緑前=フライング通過はgrn=0でも他行の値で拾える）。
  doc["rid"]         = s_internal_rid;
  doc["grn"]         = (uint64_t)s_green_t_us;
  serializeJson(doc, f);           // 1行1JSON
  f.print('\n');
  f.close();
}

// ---- 受信ハンドラ ----------------------------------------------------------
static void on_recv(const proto::PktHeader& h, const uint8_t* body,
                    int body_len, const uint8_t*) {
  mark_seen(h.src);                    // 在席記録（排他検知の土台・全種別で更新）
  switch (h.type) {
    case proto::PT_SYNC_REQ: {
      if (body_len >= (int)sizeof(proto::SyncBody)) {
        proto::SyncBody b; memcpy(&b, body, sizeof(b));
        tsync::gw_reply(h, b);
      }
    } break;

    case proto::PT_EVENT: {
      if (body_len >= (int)sizeof(proto::EventBody)) {
        proto::EventBody b; memcpy(&b, body, sizeof(b));
        mesh::send_ack(proto::PT_EVENT_ACK, h.src, h.seq);   // ingest前ACK（S5）
        RawEvent e{ h.src, h.boot_id, h.seq, b.lane, b.quality, b.t_us, b.t_us_b };
        spool_append(e);
        note_quality(b.quality, h.src);   // A1/A2/Sync集約（27章）
        Serial.printf("[EV] src=SQ%u lane=%u q=%u t=%llu\n",
                      h.src, b.lane, b.quality, (unsigned long long)b.t_us);
      }
    } break;

    case proto::PT_JOIN: {
      if (body_len >= (int)sizeof(proto::JoinBody)) {
        proto::JoinBody jb; memcpy(&jb, body, sizeof(jb));
        // ch追従（状態B）：JOINを受けたら即・在席ビーコンを撒き返す。走査中のノードが
        //   正chに乗った瞬間に在圏を検知でき、ロックが速い（HTTPS転送は下で従来どおり）。
        //   これはESP-NOWのみ・非スロットル（サーバー連打抑止=join_rate_okとは別経路）。
        mesh::send(proto::PT_HEARTBEAT, proto::NODE_BROADCAST, nullptr, 0);
        forward_join(jb);
      }
    } break;

    case proto::PT_COMMAND: {
      // リモコンからのCOMMAND。本体ボタンと同じ処理を呼ぶ（docs/03）。
      if (body_len >= (int)sizeof(proto::CommandBody)) {
        proto::CommandBody c; memcpy(&c, body, sizeof(c));
        // ★冪等化：同一押下(src,nonce=c.arg)の再送はアクションを実行しない。
        //   別ch再送・ACK駆動再送・中継の遅延重複でも SIGNAL が二度発火しない（絶対NG対策）。
        if (!cmd_nonce_seen(h.src, c.arg)) {
          if (c.code == proto::CMD_SIGNAL) on_signal_pressed();
          else if (c.code == proto::CMD_RESET) on_reset_pressed();
        }
        // ACK（届いた確認・docs/12 COMMAND_ACK）
        mesh::send_ack(proto::PT_COMMAND_ACK, h.src, h.seq);
      }
    } break;

    case proto::PT_LOST_NOTICE: {
      // C1(24.14/案A・27章)：SQがEVENTをあきらめた通知。Lost×＋「セクター通信不良」を
      //   表示する（灰リセットまで出しっぱなし）。発生SQは h.src。
      disp::g_status.lost = true;
      s_lost_src = h.src;
      Serial.printf("[LOSTNOTE] src=SQ%u\n", h.src);
    } break;

    case proto::PT_HEARTBEAT: break;
    default: break;
  }
}

// ---- スプールに送るべき行があるか（空/存在しないなら false）----------------
//  24.3：灰ボタンは「溜まっていれば送信・空なら何もしない」。その判定に使う。
static bool spool_has_data() {
  if (!LittleFS.exists(SPOOL)) return false;
  File f = LittleFS.open(SPOOL, FILE_READ);
  if (!f) return false;
  bool has = false;
  while (f.available()) {
    String line = f.readStringUntil('\n'); line.trim();
    if (line.length() > 0) { has = true; break; }   // 有効行が1行でもあれば true
  }
  f.close();
  return has;
}

// 未送信件数＝スプールの有効行数（TFTバーの Send:N 表示用・2026-08-24）。
static int spool_count() {
  if (!LittleFS.exists(SPOOL)) return 0;
  File f = LittleFS.open(SPOOL, FILE_READ);
  if (!f) return 0;
  int n = 0;
  while (f.available()) {
    String line = f.readStringUntil('\n'); line.trim();
    if (line.length() > 0) n++;
  }
  f.close();
  return n;
}

// ---- 指定 rid の行を先頭から最大 limit 件だけ除去し、残りを書き戻す（1-c）-----
//  flush で送れた group_rid の行（最大100件）をスプールから消す。別 rid の行と、
//  100件上限で送りきれなかった同 rid の残りは持ち越す。照合は冪等キーではなく
//  「rid一致かつ先頭から limit 件」で足りる（送信は先頭から詰めているため順序一致）。
static void rewrite_spool_drop_group(uint32_t rid, int limit) {
  File in = LittleFS.open(SPOOL, FILE_READ);
  if (!in) return;
  const char* TMP = "/spool.tmp";
  File out = LittleFS.open(TMP, FILE_WRITE);
  if (!out) { in.close(); return; }
  int removed = 0;
  while (in.available()) {
    String line = in.readStringUntil('\n');
    String t = line; t.trim();
    if (t.length() == 0) continue;                 // 空行は捨てる
    JsonDocument ev;
    if (deserializeJson(ev, t) != DeserializationError::Ok) continue; // 壊れ行は捨てる
    uint32_t r = ev["rid"] | 0;
    if (r == rid && removed < limit) {             // 送信済みの同 rid 先頭 limit 件を捨てる
      removed++;
      continue;
    }
    out.print(t); out.print('\n');                 // 残りを持ち越し
  }
  in.close();
  out.close();
  LittleFS.remove(SPOOL);
  LittleFS.rename(TMP, SPOOL);
}

// ---- スプールPOST（200まで消さない・S8／内部ridグループ→サーバーrace_id翻訳）---
//  20260821：各行は受信時の“内部rid”を持つ。1回のflushでは「先頭有効行のグループ
//  (=同じ内部rid)」だけを最大100件送る。送信先サーバーrace_idは：
//    ・既に作成済み(s_ridmap)ならそれを再利用（100件超の分割2回目以降）。
//    ・未作成なら“今ここで” create_race（グループ内 grn の最大値＝緑があればF1式）で採番し、
//      s_ridmap に記録。これが「作成POSTも終了時に集約」の実体。
//  ⚠ 必ずloop文脈(tick_flush)から呼ぶこと（HTTPSブロッキング・#20）。
static void flush_spool() {
  if (!wifi_up()) return;  // WiFi接続進行中は送らない（封じない・s_need_flush維持で1.5秒後に再試行・2026-08-24e）
  if (!LittleFS.exists(SPOOL)) return;
  File f = LittleFS.open(SPOOL, FILE_READ);
  if (!f) return;

  // まず先頭の有効行から「今回送るグループの内部rid」を決める。
  uint32_t group_rid = 0;
  bool group_found = false;
  while (f.available()) {
    String line = f.readStringUntil('\n'); line.trim();
    if (line.length() == 0) continue;
    JsonDocument ev;
    if (deserializeJson(ev, line) != DeserializationError::Ok) continue;
    group_rid = ev["rid"] | 0;
    group_found = true;
    break;
  }
  f.close();
  if (!group_found) return;   // 有効行なし

  // 内部rid → サーバーrace_id を確定。未作成ならグループの緑(最大)を拾って create_race。
  uint32_t server_id = ridmap_lookup(group_rid);
  if (server_id == 0) {
    uint64_t group_green = 0;                       // グループ内 grn の最大値（0=走行式）
    f = LittleFS.open(SPOOL, FILE_READ);
    if (!f) return;
    while (f.available()) {
      String line = f.readStringUntil('\n'); line.trim();
      if (line.length() == 0) continue;
      JsonDocument ev;
      if (deserializeJson(ev, line) != DeserializationError::Ok) continue;
      if ((uint32_t)(ev["rid"] | 0) != group_rid) continue;
      uint64_t g = ev["grn"] | (uint64_t)0;
      if (g > group_green) group_green = g;
    }
    f.close();
    server_id = create_race(/*with_green=*/group_green != 0, group_green);  // ここで初めてサーバー作成
    if (server_id == 0) {                            // WiFi/TLS失敗：送らずデータは残す
      Serial.println("[POST] create_race 失敗（データ保持・再試行）");
      s_send_blocked = true;
      return;
    }
    ridmap_store(group_rid, server_id);
    Serial.printf("[POST] rid(int)=%u -> race=%u%s\n",
                  group_rid, server_id, group_green ? " (F1)" : " (run)");
  }

  // group（内部rid==group_rid）の行だけを最大100件、events配列へ積む。
  f = LittleFS.open(SPOOL, FILE_READ);
  if (!f) return;
  JsonDocument out;
  JsonArray arr = out["events"].to<JsonArray>();
  int n = 0;
  bool capped = false;
  while (f.available()) {
    String line = f.readStringUntil('\n'); line.trim();
    if (line.length() == 0) continue;
    JsonDocument ev;
    if (deserializeJson(ev, line) != DeserializationError::Ok) continue; // 壊れた行は捨てる
    if ((uint32_t)(ev["rid"] | 0) != group_rid) continue;   // 別グループは今回送らない
    ev.remove("rid");                                       // rid/grn は内部用。送信payloadから除く
    ev.remove("grn");
    arr.add(ev);
    n++;
    if (n >= 100) { capped = true; break; }                // 1回のPOSTは100件まで
  }
  f.close();
  if (n == 0) return;
  String payload; serializeJson(out, payload);

  String url = server_base() + "/api/timing/races/" + server_id + "/events";
  WiFiClientSecure _c; _c.setInsecure();
  HTTPClient http;
  http.begin(_c, url);
  http.setConnectTimeout(800); http.setTimeout(1500);   // ★不達でも最長0.8秒で諦める（初回赤の取りこぼし対策・T-9）
  add_common_headers(http);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(payload);
  http.end();

  if (code != 200) {
    Serial.printf("[POST] events 失敗 code=%d\n", code);
    s_send_blocked = true;              // 1-e：送信失敗→次レース封じ
    return;
  }

  // 送信成功：送った group_rid の行を（最大100件ぶん）スプールから除去。
  rewrite_spool_drop_group(group_rid, n);
  s_send_blocked = false;              // 1-e：送信成功→封じ解除
  Serial.printf("[POST] race=%u %d件 送信%s\n", server_id, n, capped ? "（続きあり・同rid）" : "");
}

// ---- 全員完走の判定（20260821・DA15）--------------------------------------
//  有効化(1回目S/G通過)された全レーンが規定周回を完了したら、自動で送信要求を立てる。
//  完了条件：物理レーン別クロス数 ≥ target_laps + 1（1回目＝起点ぶんを加算）。
//  DA8によりLC総数は3の倍数＝各機は毎周同じ物理レーンでS/Gを切るため、この単純カウントで
//  機ごとの周回数に一致する。1台でもCO(=活性化済みだが完了しない)なら成立せず→灰ボタンへ。
static void maybe_autocomplete() {
  if (!s_race_active) return;
  bool any = false, all = true;
  for (int i = 1; i <= 3; i++) {
    if (!s_lane_active[i]) continue;
    any = true;
    if (s_lane_cross[i] < (uint32_t)s_target_laps + 1) all = false;
  }
  if (!(any && all)) return;

  Serial.printf("[SEQ] 全員完走 rid=%u -> FINISH表示＋自動送信\n", s_internal_rid);
  s_race_active   = false;        // レースを閉じる
  s_need_flush    = true;         // 送信はloopのtick_flushが実行
  s_state         = ST_FINISH;    // 計測終了(FINISH)画面へ（灰リセットで待機に戻る・20260830）
  s_sg_beeped     = false;
  s_green_t_us    = 0;
  s_post_err_until = millis() + 3000;   // 24.4⑤：A1/A2を3秒だけ表示
  signal_cmd(proto::CMD_RESET);         // シグナル消灯（念のため）
}

// ---- 送信要求の実行（loop文脈・HTTPSはここだけ・#20）------------------------
//  s_need_flush が立っている間、throttleしつつ flush_spool を回す。flush_spool は
//  1回で1グループ(最大100件)送るので、スプールが空になるまで複数tickで送り切る。
//  失敗（s_send_blocked）時は要求を残したまま再試行（灰3秒長押しで封じ解除も可）。
static void tick_flush() {
  if (!s_need_flush) return;
  static uint32_t last = 0;
  if (millis() - last < 1500) return;      // 1.5秒に1回まで（TLS連打防止）
  last = millis();
  if (!spool_has_data()) { s_need_flush = false; return; }
  flush_spool();                            // 1グループ送信（失敗ならs_send_blocked）
  if (!spool_has_data()) s_need_flush = false;   // 送り切ったら要求解除
}

// ---- レイアウト取得（GW向け軽量版 /for_gw・docs/19.16）--------------------
//  サーバーから target_laps / lap_length_m / nodes 等を取得してログ表示する。
//  戻り値：取得成功=true。呼び出し間隔は tick_fetch_layout がバックオフ管理（#14）。
static bool fetch_layout() {
  if (!wifi_up()) return false;
  WiFiClientSecure _c; _c.setInsecure();
  HTTPClient http;
  http.begin(_c, server_base() + "/api/timing/layouts/" + String((int)s_layout_id) + "/for_gw");
  http.setConnectTimeout(800); http.setTimeout(1500);   // ★不達でも最長0.8秒で諦める（初回赤の取りこぼし対策・T-9）
  add_common_headers(http);
  int code = http.GET();
  bool ok = false;
  if (code == 200) {
    JsonDocument res;
    if (deserializeJson(res, http.getString()) == DeserializationError::Ok) {
      int laps  = res["target_laps"] | 0;
      int nodes = res["node_count"]  | 0;
      s_node_need = (nodes >= 0 && nodes <= 6) ? nodes : 0;   // 使用SQ数（0..6）。異常値は0=未取得扱い
      int lc    = res["lc_count"]    | 0;
      float len = res["lap_length_m"] | 0.0f;
      // #8-a：取得できた周回数を実行時変数へ反映（0や異常値は無視して既定を維持）
      if (laps >= 1 && laps <= 99) s_target_laps = laps;
      Serial.printf("[LAYOUT] id=%u laps=%d nodes=%d lc=%d len=%.1fm\n",
                    s_layout_id, laps, nodes, lc, len);
      ok = true;
    }
  }
  if (!ok) Serial.printf("[LAYOUT] fetch NG code=%d\n", code);
  http.end();
  return ok;
}

// ---- fetch_layout の呼び出し間隔管理（#14：成功60s / 失敗5s）---------------
// レイアウト取得は「起動後に1回だけ」（要望2026-08-24）。定期取得は廃止。
//   起動直後はWiFi未接続のことが多いので、接続できるまでは数秒間隔で試し、
//   1回成功したら以降は取りに行かない（TLS試行でESP-NOWを巻き込まない）。
static void tick_fetch_layout() {
  if (s_state != ST_IDLE) return;      // SET〜計測中は通信しない
  static bool done = false;            // 1回成功したら終了
  if (done) return;
  static uint32_t last = 0;
  if (last != 0 && (millis() - last) < 5000) return;   // 未取得のうちは5秒間隔で再試行
  last = millis();
  if (fetch_layout()) { s_layout_ok = true; done = true; Serial.println("[LAYOUT] 起動時取得 完了（以降は取得しない）"); }
}

// ---- 本体ボタン読み取り ----------------------------------------------------
// ---- ボタンISRラッチ（2026-08-24e）------------------------------------------
//  旧実装はloop内ポーリングのエッジ検出のみ。loopがWiFi/HTTPS等で停止している間に
//  押して離した押下は「エッジごと消失」していた（＝赤が効かない主因のひとつ）。
//  FALLINGエッジをISRで取りこぼしなくラッチし、実行はloop（tick_buttons）で行う。
//  ISRではフラグを立てるだけ（描画・無線・シリアルはしない）。
static volatile bool     s_red_evt = false, s_gray_evt = false;
static volatile uint32_t s_red_isr_ms = 0,  s_gray_isr_ms = 0;
static void IRAM_ATTR isr_btn_red() {
  uint32_t now = millis();
  if (now - s_red_isr_ms >= DEBOUNCE_MS) { s_red_isr_ms = now; s_red_evt = true; }
}
static void IRAM_ATTR isr_btn_gray() {
  uint32_t now = millis();
  if (now - s_gray_isr_ms >= DEBOUNCE_MS) { s_gray_isr_ms = now; s_gray_evt = true; }
}

static void tick_buttons() {
  // 赤：ISRラッチ消化（loop停止中の押下も失われない）。受付判定はon_signal_pressed側。
  if (s_red_evt) { s_red_evt = false; on_signal_pressed(); }

  // 灰：ISRラッチで単押し即RESET（長押し機能は廃止・2026-08-24）。
  if (s_gray_evt) { s_gray_evt = false; on_reset_pressed(); }
}

// ---- GW自身の在席ビーコン（相手GWが「GW2台」を検知できるように）------------
//  GWはRC/SGのように定期ハートビートを出していなかったため、相手GWから見えない。
//  ここで自分の在席をブロードキャストし、GW6⇔GW7 が相互に重複検知できるようにする。
static void tick_gw_presence() {
  static uint32_t last = 0;
  if (millis() - last < GW_BEACON_MS) return;
  last = millis();
  mesh::send(proto::PT_HEARTBEAT, proto::NODE_BROADCAST, nullptr, 0);
}

// ---- 接続機材ロスター（設営確認用・2026-08-24）--------------------------------
//  3秒ごとに、いま在席(node_alive)している機材IDをシリアルへ一覧出力する。
//  新配線・プロトコル変更なし（既存の在席トラッキングを読むだけ）。無線・描画に影響なし。
//  例）[LINK] SQ:0,1,2 RC:8 SG:10 GW7:-
static void tick_roster() {
  static uint32_t last = 0;
  if (millis() - last < 3000) return;
  last = millis();
  disp::g_status.unsent = spool_count();          // 未送信件数をバー(Send:N)へ反映
  char sq[40]; int sn = 0; sq[0] = 0;
  for (uint8_t id = 0; id <= 5; id++)
    if (node_alive(id)) sn += snprintf(sq + sn, sizeof(sq) - sn, sn ? ",%u" : "%u", id);
  if (sn == 0) { sq[0] = '-'; sq[1] = 0; }
  char rc[16]; int rn = 0; rc[0] = 0;
  for (uint8_t id = 8; id <= 9; id++)
    if (node_alive(id)) rn += snprintf(rc + rn, sizeof(rc) - rn, rn ? ",%u" : "%u", id);
  if (rn == 0) { rc[0] = '-'; rc[1] = 0; }
  char sg[16]; int gn = 0; sg[0] = 0;
  for (uint8_t id = 10; id <= 11; id++)
    if (node_alive(id)) gn += snprintf(sg + gn, sizeof(sg) - gn, gn ? ",%u" : "%u", id);
  if (gn == 0) { sg[0] = '-'; sg[1] = 0; }
  Serial.printf("[LINK] SQ:%s RC:%s SG:%s GW7:%s (activeSG=%u)\n",
                sq, rc, sg, node_alive(7) ? "on" : "-", s_active_sg_id);
}

// ---- 設定の読み込み（NVS "m4cfg" → 無ければ secrets.h / 既定）---------------
//  アプリが nvs_partition_gen で作った nvs.bin を USB(Web Serial) で 0x9000 に焼くと、
//  ここが拾って上書きする。キーが無ければ従来どおり secrets.h の値で動く（後方互換）。
static void load_config() {
  Preferences p;
  bool from_nvs = false;
  if (p.begin("m4cfg", /*readOnly=*/true)) {
    if (p.isKey("ssid"))  { g_ssid  = p.getString("ssid",  g_ssid);  from_nvs = true; }
    if (p.isKey("pass"))  { g_pass  = p.getString("pass",  g_pass);  from_nvs = true; }
    if (p.isKey("host"))  { g_host  = p.getString("host",  g_host);  from_nvs = true; }
    if (p.isKey("ip"))    { g_ip    = p.getString("ip",    g_ip);    from_nvs = true; }
    if (p.isKey("token")) { g_token = p.getString("token", g_token); from_nvs = true; }
    if (p.isKey("ch"))    { uint8_t c = p.getUChar("ch", g_channel);
                            if (c >= 1 && c <= 13) { g_channel = c; from_nvs = true; } }
    p.end();
  }
  // 空を焼いた時の保険（ssid/host/ipは空なら既定へ。pass/tokenは空を許容）。
  if (g_ssid.isEmpty()) g_ssid = WIFI_SSID;
  if (g_host.isEmpty()) g_host = DEF_SERVER_HOST;
  if (g_ip.isEmpty())   g_ip   = DEF_SERVER_IP;
  Serial.printf("[CFG] src=%s ch=%d ssid=\"%s\" host=%s ip=%s token=%dB\n",
                from_nvs ? "NVS" : "default", (int)g_channel,
                g_ssid.c_str(), g_host.c_str(), g_ip.c_str(), (int)g_token.length());
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n=== M4LAPS Gateway GW%d (VE) ===\n", NODE_ID);
  load_config();   // NVS "m4cfg" を読む（無ければ secrets.h 既定）。以後は g_* を使用
  pinMode(PIN_BTN_RED,  INPUT_PULLUP);
  pinMode(PIN_BTN_GRAY, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_BTN_RED),  isr_btn_red,  FALLING);  // 押下取りこぼし防止（2026-08-24e）
  attachInterrupt(digitalPinToInterrupt(PIN_BTN_GRAY), isr_btn_gray, FALLING);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);   // 起動直後は必ず消灯
  if (!LittleFS.begin(true, "/littlefs", 10, "littlefs")) Serial.println("LittleFS mount 失敗");  // K7: csvのName列を明示
  // 案X：起動時にスプールを全消し（毎回まっさら・2026-08-24）。
  //   「空ファイルを作成」して常に存在させる → 読み込み時の vfs "does not exist" ログ抑止。
  { File sf = LittleFS.open(SPOOL, FILE_WRITE); if (sf) sf.close(); }
  Serial.println("[SPOOL] 起動時クリア（空作成）");
  Serial.printf("PSRAM=%u (VE normal: approx 4MB mapped of 8MB)\n", (unsigned)ESP.getPsramSize());
  if (!mesh::begin(NODE_ID, g_channel, on_recv)) {
    Serial.println("ESP-NOW init 失敗"); return;
  }
  beam::begin();
  disp::begin();                       // TFT初期化（PSRAMスプライト確保）
  s_state = ST_SPLASH; s_splash_ms = millis(); Serial.println("[SPLASH] start");   // 開始（非ブロッキング・loopで描画）
  disp::g_status.gw_id = NODE_ID;
  disp::g_status.ch    = g_channel;
  wifi_up();
  Serial.println("稼働開始。赤=スタート(READY時のみ) / 灰=RESET(即時)。");
}

// 経過秒フィールド（案B）の設定。
//  s_show_ontrack：いまフルフレーム層にON TRACK画面が出ているか（tick_displayが4fpsで更新）。
//   これがtrueのときだけ経過秒フィールドを上に重ねる（エラー面の上に重ねない保険）。
static bool s_show_ontrack = false;
static constexpr uint32_t TIME_FIELD_MS = 16;   // 約60fps（より滑らか・2026-08-24）

// 経過秒フィールドだけを高頻度で部分更新する（案B）。loop()から毎周回呼ぶ。
//  tick_displayの250msゲートに縛られず、ON TRACK表示中のみ経過秒を機敏に動かす。
//  ⚠打刻はISR/tsync由来なので、この描画頻度は計測精度に一切影響しない。
static void tick_time_field() {
  if (!s_show_ontrack) return;                        // ON TRACK表示中のみ
  static uint32_t last_t = 0;
  if (millis() - last_t < TIME_FIELD_MS) return;      // 約30fpsに間引き
  last_t = millis();
  uint32_t elapsed = s_green_t_us ? (uint32_t)((tsync::now_gw_us() - s_green_t_us) / 1000ULL) : 0;
  disp::draw_time_field(elapsed, /*hundredths=*/false);  // コンマ1秒（右上・滑らか描画）
}

// ---- SET画面の即時描画（赤押下の即応・tick_displayの250msゲートを迂回）------
//  on_signal_pressed から呼ぶ。ARMEDに入った瞬間、状態機械の次周(最大250ms後)を
//  待たずに SET 全面を1回転送する。以後の維持描画は tick_display(ST_ARMED)が担う。
static void render_set_now() {
  if (!disp::ready()) return;
  disp::draw_set(false);        // blink未使用
  s_show_ontrack = false;       // 経過秒フィールドを重ねない
  disp::commit();               // ステータスバーを重ねて即転送
}

// ---- 計測中画面の即時描画（緑点灯の即応・tick_displayの250msゲートを迂回）----
//  tick_sequence が緑(CMD_GREEN)を出した瞬間に呼ぶ。状態機械の次周(最大250ms後)を
//  待たずに ON TRACK 全面を1回転送し、カウント表示をその場で始める。以後の維持描画は
//  tick_display(ST_RACE)＋経過秒フィールド(約30fps)が担う。
//  ⚠公式タイムは green_t_us(µs)基準。この描画タイミングは計測精度に影響しない。
static void render_ontrack_now() {
  if (!disp::ready()) return;
  uint32_t elapsed = s_green_t_us ? (uint32_t)((tsync::now_gw_us() - s_green_t_us) / 1000ULL) : 0;
  disp::draw_ontrack(elapsed, /*blink=*/true, s_target_laps);
  s_show_ontrack = true;        // 以降、経過秒フィールドを約30fpsで重ねる
  disp::commit();               // ステータスバーを重ねて即転送
}

// ---- TFT描画：状態機械に同期して画面を切り替える（docs/20・12.3）----------
//  ステータスバーの○×はここで g_status に反映してから各画面を描く。
static void tick_display() {
  // スプラッシュ（非ブロッキング）：250msゲートより前で自前タイミング描画。
  if (s_state == ST_SPLASH) {
    static uint32_t sf_last = 0;
    uint32_t e = millis() - s_splash_ms;
    if (e >= SPLASH_MS) { s_state = ST_IDLE; Serial.println("[SPLASH] done -> READY"); return; }     // 終了→READY
    if (millis() - sf_last >= 33) {                        // 約30fps
      sf_last = millis();
      disp::draw_splash_frame((float)e / (float)SPLASH_MS);
    }
    return;                                                // スプラッシュ中は他描画しない
  }
  static uint32_t last = 0;
  static bool blink = false;
  if (millis() - last < 250) return;     // 約4fps（●点滅もこの周期）
  last = millis();
  blink = !blink;

  // ステータス更新（NODE充足は残課題#8で実数へ。今はJOIN実数の反映口のみ用意）
  disp::g_status.wifi_ok = (WiFi.status() == WL_CONNECTED);
  disp::g_status.ch      = g_channel;
  // Node充足：実JOIN中のSQ(0..5)数を数え、必要数はレイアウト由来(s_node_need)。
  //   need>0 のときだけ have/need を突合（need=0＝レイアウト未取得は「Nd-/-」表示）。
  {
    int have = 0;
    for (uint8_t id = 0; id <= 5; id++) if (node_alive(id)) have++;
    disp::g_status.node_have = have;
    disp::g_status.node_need = s_node_need;
  }
  // 切断ビープ（READY時のみ・切断の瞬間だけ1回。WiFi切断／ノード離脱・2026-08-24）。
  //  鳴りっぱなしにしない＝立ち上がり検出。復帰では鳴らさない。計測中は鳴らさない。
  {
    static bool init_done = false, prev_wifi = false; static int prev_have = 0;
    bool wifi_now = disp::g_status.wifi_ok;
    int  have_now = disp::g_status.node_have;
    if (s_state == ST_IDLE) {
      if (!init_done) { prev_wifi = wifi_now; prev_have = have_now; init_done = true; }
      else {
        if (prev_wifi && !wifi_now) { buzzer_start(150); Serial.println("[BEEP] WiFi切断"); }
        if (have_now < prev_have)   { buzzer_start(150); Serial.println("[BEEP] ノード離脱"); }
        prev_wifi = wifi_now; prev_have = have_now;
      }
    }
  }
  // READY用：接続中の機材ID一覧（番号のみ・昇順・自分は除く）。無ければ "-"。
  {
    char* d = disp::g_status.link; int n = 0; int cap = (int)sizeof(disp::g_status.link);
    for (uint8_t id = 0; id <= proto::NODE_ID_MAX; id++) {
      if (id == (uint8_t)NODE_ID) continue;           // 自分(GW)は除く
      if (!node_alive(id)) continue;
      n += snprintf(d + n, cap - n, n ? ",%u" : "%u", id);
      if (n >= cap - 4) break;
    }
    if (n == 0) { d[0] = '-'; d[1] = 0; }
  }
  disp::g_status.beam_ok = !s_beam_bad;   // 片/両ビーム欠を一度でも受けたら×（灰リセットで復帰）
  // unsent は各機能側から順次代入予定。

  // Sync集約（B1/24.11・27章）：自機未同期 or 直近q=3受信 で Sync×（自動復帰型）。
  {
    bool recent_q3 = (s_last_q3_ms != 0) && (millis() - s_last_q3_ms < 10000);
    disp::g_status.sync_ok = tsync::is_synced() && !recent_q3;
  }

  // 排他検知（GW/RC/SG）。gw_dup/rc_dup/sg_dup を更新し、生存SGも確定する。
  bool conflict = eval_exclusivity();
  if (conflict) {
    // 排他エラーは通常画面より優先して全面表示（優先順位：GW > SG > RC）。
    disp::ErrItem errs[3]; int cnt = 0;   // 既定でarg_i=0/label空。kindのみ下で設定。
    if (disp::g_status.gw_dup) errs[cnt++].kind = disp::ERR_GW_DUP;
    if (disp::g_status.sg_dup) errs[cnt++].kind = disp::ERR_SG_DUP;
    if (disp::g_status.rc_dup) errs[cnt++].kind = disp::ERR_RC_DUP;
    disp::draw_error(errs, cnt);
    disp::commit();               // ステータスバーを重ねて転送
    s_show_ontrack = false;       // エラー面の上に経過秒を重ねない
    buzzer_error_edge(true);      // エラー発生の立ち上がりで1秒1回
    return;
  }

  // 待機画面のエラー面（27章/24.4⑤・24.14）：
  //   C1 Lost は灰リセットまで出しっぱなし。A1/A2 は灰ボタン後3秒だけ。いずれも待機(ST_IDLE)で出す。
  //   排他系(GW/RC/SG重複)は上で return 済みなので、ここは A1/A2/Lost に絞れる（errs枠溢れ回避・27.3）。
  if (s_state == ST_IDLE) {
    bool show_post = ((int32_t)(s_post_err_until - millis()) > 0);   // wrap安全な残時間判定
    bool a1 = show_post && s_race_q2;
    bool a2 = show_post && s_race_q1;
    if (disp::g_status.lost || a1 || a2) {
      disp::ErrItem errs[3]; int cnt = 0;
      // F-2：描画は配列順。enumの「最後尾」意図どおり A1→A2→Lost の順に積む。
      if (a1) {
        errs[cnt].kind = disp::ERR_SENSOR_BOTH;                       // A1 両ビーム欠
        snprintf(errs[cnt].label, sizeof(errs[cnt].label), "SQ%u", s_race_q2_src); cnt++;
      }
      if (a2 && cnt < 3) {
        errs[cnt].kind = disp::ERR_SPEED_ONLY;                        // A2 片ビーム欠
        snprintf(errs[cnt].label, sizeof(errs[cnt].label), "SQ%u", s_race_q1_src); cnt++;
      }
      if (disp::g_status.lost && cnt < 3) {
        errs[cnt].kind = disp::ERR_SECTOR_COMM;                       // C1（最後尾・出しっぱなし）
        snprintf(errs[cnt].label, sizeof(errs[cnt].label), "SQ%u", s_lost_src); cnt++;
      }
      disp::draw_error(errs, cnt);
      disp::commit();
      s_show_ontrack = false;     // エラー面の上に経過秒を重ねない
      buzzer_error_edge(true);    // エラー発生の立ち上がりで1秒1回
      return;
    }
  }

  switch (s_state) {
    case ST_IDLE:
      disp::draw_idle();
      s_show_ontrack = false;
      break;
    case ST_ARMED:
      // 赤押下を受け付けた「間」を埋める SET 画面（docs/20.6）。
      //   READY(待機)から即切り替わることで、押下が通ったことを視認できる。
      //   緑点灯(ST_GREEN)へ移るまでこの表示。blinkで「GET READY」ドットを点滅。
      disp::draw_set(blink);
      s_show_ontrack = false;
      break;
    case ST_GREEN:
    case ST_RACE: {
      uint32_t elapsed = s_green_t_us ? (uint32_t)((tsync::now_gw_us() - s_green_t_us) / 1000ULL) : 0;
      disp::draw_ontrack(elapsed, blink, s_target_laps);
      s_show_ontrack = true;      // 以降、経過秒フィールドを約30fpsで重ねる
      break;
    }
    case ST_FINISH:
      disp::draw_finish(s_target_laps);   // 合計＋各周ラップ（docs/20.4・20260830実装）
      s_show_ontrack = false;             // 経過秒カウントは消す（docs/20.4）
      break;
  }
  buzzer_error_edge(false);              // エラー無し＝立ち上がり検出をリセット（再発時にまた1回鳴る）
  disp::commit();                        // ステータスバーを重ねて一括転送
}

void loop() {
  tick_buttons();
  buzzer_tick();              // ブザー非ブロッキング消灯（29章）
  tick_gw_presence();         // GW自身の在席ビーコン（GW2台検知の相互化）
  tick_roster();              // 接続機材の一覧をシリアルへ（設営確認用・3秒毎）
  tick_sequence();
  if (s_set_now_req) { s_set_now_req = false; render_set_now(); }  // 赤押下の即SETをloop文脈で安全に描画

  beam::Hit hit;
  int drain = 0;
  if (s_state == ST_SPLASH) {                 // スプラッシュ中はビームを捨てるだけ（書込み無し・loop解放）
    while (drain++ < 8 && beam::poll(hit)) { /* discard */ }
  } else
  while (drain++ < 8 && beam::poll(hit)) {   // 1周8件まで（浮きGPIO洪水でloopを占有させない）
    static uint32_t self_seq = 0;
    // 走行式(赤ボタン未使用)：最初のS/G通過で内部レースを開始する（F1式は既にactive）。
    //   これで s_internal_rid が採番され、以降の自機/SQイベントが同じ内部ridで刻まれる。
    // FINISH表示中は新レースを起こさない（余走の通過は完走レースのridのまま記録され、
    //   FINISH画面のラップ表示も保持される。次レースは灰リセット後・20260830）。
    if (!s_race_active && s_state != ST_FINISH) begin_internal_race();
    bool was_idle_hit = (s_state == ST_IDLE);   // 通過ピッ判定用（遷移前の状態を保持）
    // 走行式(TA)：待機のまま最初の「正常通過(q=0/1)」が来たら ON TRACK＋計測開始。
    //   q=2（張り付き＝A1：故障・断線・CO）は通過ではないのでスタートさせない（20260830）。
    if (s_state == ST_IDLE && hit.quality != 2) {
      s_green_t_us = hit.t_a_us ? hit.t_a_us : tsync::now_gw_us();
      s_state      = ST_RACE;          // TFTを ON TRACK に切替
      render_ontrack_now();            // 緑同様、即時に計測中画面を描く
      Serial.printf("[TA] START rid=%u t=%llu\n",
                    s_internal_rid, (unsigned long long)s_green_t_us);
    }
    // 参加レーン確定＋周回・ラップ記録（自機S/Gのみ・DA15/DA8）。
    //   q=2は通過ではないので数えない（張り付きで周回が水増しされるのを防ぐ・20260830）。
    if (hit.lane >= 1 && hit.lane <= 3 && hit.quality != 2 && s_state != ST_FINISH) {
      int L = hit.lane;
      uint64_t t = hit.t_a_us ? hit.t_a_us : hit.t_b_us;   // q=1でAが欠けたらBで代用
      s_lane_active[L] = true;
      s_lane_cross[L]++;
      disp::LaneRun& r = disp::g_run[L - 1];
      if (s_lane_cross[L] == 1) {
        s_lane_first_us[L] = t;                            // 起点通過（走行式の合計起点）
      } else if (r.done < disp::MAX_LAPS) {                // 2回目以降＝1周確定
        r.lap_ms[r.done++] = (uint32_t)((t - s_lane_last_us[L]) / 1000ULL);
      }
      s_lane_last_us[L] = t;
      // 完走判定：起点1回＋規定周回（既存 maybe_autocomplete と同条件）
      if (!r.fin && s_lane_cross[L] >= (uint32_t)s_target_laps + 1) {
        r.fin = true;                                      // このレーンは完走（TFTにFIN表示）
        uint64_t start = s_green_t_us ? s_green_t_us : s_lane_first_us[L];
        r.total_ms = (uint32_t)((t - start) / 1000ULL);    // F1式=緑起点／走行式=初回通過起点
        Serial.printf("[SEQ] L%d 完走 total=%.2fs\n", L, r.total_ms / 1000.0);
      }
    }
    RawEvent e{ (uint8_t)NODE_ID, mesh::boot_id(), ++self_seq,
                hit.lane, hit.quality, hit.t_a_us, hit.t_b_us };
    spool_append(e);
    note_quality(hit.quality, (uint8_t)NODE_ID);   // 自機S/GのA1/A2も拾う（27章）
    Serial.printf("[SG] lane=%u q=%u rid=%u cross=%u\n",
                  hit.lane, hit.quality, s_internal_rid,
                  (hit.lane>=1&&hit.lane<=3)?s_lane_cross[hit.lane]:0);
    if (was_idle_hit && !s_sg_beeped) {  // 待機からの「最初の」S/G通過のみ 60ms「ピッ」（29章・TA開始時も鳴る）
      buzzer_beep();
      s_sg_beeped = true;                       // 以降は灰リセットまで鳴らさない（TA周回の毎回鳴り防止）
    }
  }

  // 20260821：POSTは「全員完走(自動)」と「灰ボタン(手動)」の2契機のみ。
  //   計測中はサーバー通信ゼロ（受信→内部ridで刻む→LittleFSへ溜める→TFTのみ）。
  maybe_autocomplete();  // 有効レーンが全員規定周回完了なら s_need_flush を立てる（DA15）
  tick_flush();          // 送信要求があればloop文脈でHTTPS実行（#20・全送信の唯一の出口）

  tick_fetch_layout();   // #14：成功60s/失敗5sのバックオフで /for_gw を取得
  tick_display();        // TFT描画：全画面フル更新（状態機械に同期・約4fps）
  tick_time_field();     // 案B：ON TRACK経過秒だけ約30fpsで部分更新（機敏に動かす）
}
