// firmware/gw/src/main.cpp
// ============================================================================
//  M4LAPS ゲートウェイ（GW6／予備GW7）ファーム
//  役割（docs/14 DA1/DA2）：全ゲートの集約点。SYNC親時計。EVENT集約→サーバーPOST。
//    JOIN転送（/api/timing/join）。S/G（自分の3レーン）ビーム検出。
//  ＋スタートシーケンス（docs/14 DA11・docs/12.3 状態機械）：
//    赤ボタン(本体) or リモコンCMD_SIGNAL → 新レース作成(layout_id付き) →
//    赤点灯 → 最低3秒＋ランダム → 緑点灯(green_t_us記録・F1式) → シグナルへCOMMAND
//    灰ボタン or CMD_RESET → IDLEへ戻す（消灯）
//  ⚠ 「レース中か」の意味づけはアプリ（DA1）。GWが持つのは“演出の進行”のみ。
//  common（protocol/espnow_link/timesync/beam）を呼ぶ。TFTは最小デバッグ表示。
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
static constexpr uint32_t LAYOUT_NG_MS   =  5000;   // 失敗中は5秒で再試行
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
enum GwState { ST_IDLE, ST_ARMED, ST_GREEN, ST_RACE };
static GwState s_state = ST_IDLE;
static uint32_t s_armed_ms = 0;         // ARMEDに入った時刻
static uint32_t s_red_dur_ms = 0;       // 今回の赤の長さ（3秒＋ランダム）
static uint64_t s_green_t_us = 0;       // 緑を出したGW時刻（F1式・green_t_us）
// #20: サーバー登録はloop文脈で非同期実行（ESP-NOWコールバックからHTTPSしない）
static bool s_need_race        = false;  // レース作成が未了
static bool s_need_green_patch = false;  // green後付けが未了

// ---- 受信EVENTレコード -----------------------------------------------------
struct RawEvent {
  uint8_t src; uint32_t src_boot; uint32_t seq;
  uint8_t lane; uint8_t quality; uint64_t t_us; uint64_t t_us_b;
};

static const char* SPOOL = "/spool.jsonl";
static uint32_t s_race_id = 0;
// 24.3(1-e)：送信失敗中は「次の赤ボタン(新レース開始)を封じる」安全フラグ。
//   flush_spool 成功でクリア、失敗でセット。灰3秒長押し(緊急解除)でも手動クリア可。
static bool s_send_blocked = false;

// ---- ③群 GW配線（A1/A2・Sync・C1 Lost・24.4⑤画面フロー・27章）--------------
//  A1(q=2 両ビーム欠)/A2(q=1 片ビーム欠)は per-raceで最悪値を保持し、灰ボタン後3秒だけ表示。
//  Sync(q=3 未同期打刻)は自動復帰型なので直近受信時刻だけ持つ。
//  C1 Lostは give-up通知(PT_LOST_NOTICE)で立て、灰リセットまで出しっぱなし。
static bool     s_race_q2 = false;  static uint8_t s_race_q2_src = 0;  // A1：両ビーム欠(q=2)
static bool     s_race_q1 = false;  static uint8_t s_race_q1_src = 0;  // A2：片ビーム欠(q=1)
static uint32_t s_last_q3_ms     = 0;   // B1：未同期打刻(q=3)を最後に受けた時刻→Sync×(自動復帰)
static uint32_t s_post_err_until = 0;   // 24.4⑤：灰ボタン後3秒だけA1/A2を出す窓(millis基準)
static uint8_t  s_lost_src       = 0;   // C1：give-up通知の発生SQ(Lostラベル用・24.14)

// 受信/自機の通過qualityを per-raceフラグへ反映（A1/A2/Sync集約の入口）。
static inline void note_quality(uint8_t q, uint8_t src) {
  if (q == 2)      { if (!s_race_q2) { s_race_q2 = true; s_race_q2_src = src; } }
  else if (q == 1) { if (!s_race_q1) { s_race_q1 = true; s_race_q1_src = src; } }
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

static bool wifi_up() {
  if (WiFi.status() == WL_CONNECTED) { gw_adopt_wifi_channel(); return true; }
  // DNSを明示（ルーターがDNS未配布でも解決を試せるように）。SSID接続前に設定。
  //  IP等は0で自動（DHCP）、DNSだけ固定にする。
  WiFi.config(IPAddress(0,0,0,0), IPAddress(0,0,0,0), IPAddress(0,0,0,0),
              IPAddress(8,8,8,8), IPAddress(1,1,1,1));
  WiFi.begin(g_ssid.c_str(), g_pass.c_str());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) delay(100);
  bool up = (WiFi.status() == WL_CONNECTED);
  if (up) { probe_dns(); gw_adopt_wifi_channel(); }  // 接続直後にDNS判定＋実ch採用
  return up;
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

// ---- 既存レースに緑時刻を後付け（走行式→F1式・残課題7の解消）--------------
//  race_id を変えずに green_t_us だけPATCHする。POST .../{id}/green。
static bool set_race_green(uint32_t rid, uint64_t green_us) {
  if (!rid || !wifi_up()) return false;
  JsonDocument doc;
  doc["green_t_us"] = green_us;
  String body; serializeJson(doc, body);
  WiFiClientSecure _c; _c.setInsecure();
  HTTPClient http;
  http.begin(_c, server_base() + "/api/timing/races/" + rid + "/green");
  add_common_headers(http);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  http.end();
  return code == 200;
}

// ---- シグナルへ COMMAND（赤/緑/リセット）----------------------------------
static void signal_cmd(uint8_t code) {
  proto::CommandBody c = {};
  c.code = code;
  // 生きているシグナル（SG10 or 予備SG11）へ送る。既定はSG10。
  mesh::send(proto::PT_COMMAND, s_active_sg_id, &c, sizeof(c));
}

// ---- スタートシーケンス制御 -----------------------------------------------
//  赤ボタン/CMD_SIGNAL共通の入口（docs/03「本体とリモコンで同じ処理」）。
static void on_signal_pressed() {
  if (s_state != ST_IDLE) return;             // 二度押し無視
  if (s_send_blocked) {                        // 1-e：送信失敗中は新レースを封じる
    Serial.println("[SEQ] blocked: 前レース未送信。灰ボタンで再送、または灰3秒長押しで強制解除");
    return;
  }
  if (exclusivity_conflict()) {               // GW/RC/SG重複中は新規スタートを受け付けない
    Serial.println("[SEQ] blocked: GW/RC/SG duplicate active (power off one)");
    return;
  }
  // #19+#20: 演出のみ即実行。HTTPS（レース作成）はここでは一切しない。
  //  本関数はESP-NOW受信コールバック文脈からも呼ばれるため、ブロッキングI/O禁止。
  //  レース作成は tick_race_registration()（loop文脈）が拾う。
  s_red_dur_ms = 3000 + (esp_random() % 2000);   // 3.0〜5.0秒
  s_armed_ms   = millis();
  s_state      = ST_ARMED;
  s_race_q2 = false; s_race_q1 = false;   // 24.4：新レース開始でA1/A2 per-raceフラグをclear
  signal_cmd(proto::CMD_RED);
  Serial.printf("[SEQ] ARMED red=%ums\n", s_red_dur_ms);
  s_race_id          = 0;
  s_need_race        = true;
  s_need_green_patch = false;
}

// flush_spool / spool_has_data は本関数より後方で定義されるため前方宣言。
static bool spool_has_data();
static void flush_spool();

static void on_reset_pressed() {
  // 24.3：灰ボタン押下時、スプールに溜まっていれば送信（空なら何もしない）。
  //   送信 → 表示リセット → IDLE の順。送信失敗しても RESET 自体は進める
  //   （灰ボタンは再送手段として残す設計・24.3）。WiFi不通時は flush_spool 内で
  //   送信せず return し、データはスプールに残る（消えない）。
  bool was_idle = (s_state == ST_IDLE);   // F-1：この灰が「走行終了」か「待機での確認」かを区別
  if (spool_has_data()) flush_spool();

  // 24.4⑤：待機画面に入った直後、3秒間だけA1/A2エラーを表示する窓を開く。
  s_post_err_until = millis() + 3000;
  // C1(24.14/F-1)：Lostは待機画面で一度見せてから灰で解除する。
  //   走行を止めた灰(was_idle=false)では消さない＝走行中に届いたLostが
  //   未表示のまま消えるのを防ぐ。待機で見えている状態の灰(was_idle=true)が「確認＝解除」。
  if (was_idle) { disp::g_status.lost = false; s_lost_src = 0; }

  s_state = ST_IDLE;
  s_green_t_us = 0;
  s_need_race        = false;   // #20: 保留中のサーバー登録も破棄
  s_need_green_patch = false;
  signal_cmd(proto::CMD_RESET);
  Serial.println("[SEQ] RESET -> IDLE");
}

// ARMED経過で緑へ。loopから呼ぶ。
static void tick_sequence() {
  if (s_state == ST_ARMED && (millis() - s_armed_ms) >= s_red_dur_ms) {
    s_green_t_us = tsync::now_gw_us();          // 緑を出した瞬間＝green_t_us
    s_state = ST_GREEN;
    signal_cmd(proto::CMD_GREEN);
    Serial.printf("[SEQ] GREEN t=%llu\n", (unsigned long long)s_green_t_us);
    // #20: green後付けはレース確定後に tick_race_registration() が実行する。
    s_need_green_patch = true;
    s_state = ST_RACE;
  }
}

// ---- #20: サーバー登録の非同期実行（loop文脈・失敗は3秒おきに再試行）------
static void tick_race_registration() {
  static uint32_t last_try = 0;
  uint32_t nowm = millis();
  if (nowm - last_try < 3000) return;           // 試行は3秒に1回まで
  if (s_need_race && s_race_id == 0) {
    last_try = nowm;
    s_race_id = create_race(/*with_green=*/false, 0);
    Serial.printf("[SEQ] race=%u%s\n", s_race_id, s_race_id ? "" : " (retry in 3s)");
    if (s_race_id) s_need_race = false;
    return;                                     // 1周1リクエストに抑える
  }
  if (s_need_green_patch && s_race_id) {
    last_try = nowm;
    bool ok = set_race_green(s_race_id, s_green_t_us);
    Serial.printf("[SEQ] green_patch=%s race=%u%s\n",
                  ok ? "OK" : "NG", s_race_id, ok ? "" : " (retry in 3s)");
    if (ok) s_need_green_patch = false;
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
static void spool_append(const RawEvent& e) {
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
  // 24.3(1-c)：race_id は「セクターから受信した瞬間」の値を刻む。flush時点の
  //   s_race_id では、受信〜送信間に再起動等でIDが変わると誤帰属する。
  //   rid はスプール内部の振り分け専用（送信ペイロードには載せない。サーバーの
  //   冪等キーは device_id/src/src_boot_id/seq で race_id を含まないため）。
  //   rid=0 は「受信時まだレース未確定」を意味し、flush時にその時点の確定IDへ寄せる。
  doc["rid"]         = s_race_id;
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
        if (c.code == proto::CMD_SIGNAL) on_signal_pressed();
        else if (c.code == proto::CMD_RESET) on_reset_pressed();
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

// ---- レース払い出し（EVENTを送る先が無ければ暫定で1つ作る）----------------
//  スタートシーケンス未使用でもS/G通過だけ記録したい場合の保険。
static bool ensure_race() {
  if (s_race_id) return true;
  s_race_id = create_race(/*with_green=*/false, 0);
  return s_race_id != 0;
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

// ---- スプールPOST（200まで消さない・S8）-----------------------------------
// ---- スプールPOST（200まで消さない・S8／rid=受信時race_idでグループ送信・1-c）---
//  24.3(1-c)：各行が受信時の race_id(rid) を持つ。1回のflushでは「先頭有効行の
//  グループ(=同じ送信先race_id)」だけを最大100件集めて送る。これにより受信〜送信間に
//  s_race_id が変わっても、行は自分の rid のレースへ正しく送られる（誤帰属しない）。
//  rid=0 の行（受信時レース未確定）は、その時点の確定 race_id(ensure_race) へ寄せる。
static void flush_spool() {
  if (!LittleFS.exists(SPOOL)) return;
  File f = LittleFS.open(SPOOL, FILE_READ);
  if (!f) return;

  // まず先頭の有効行から「今回送るグループの rid」を決める。
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

  // 送信先 race_id を確定。group_rid>0 ならそれを使う。0 なら ensure_race で確定。
  uint32_t dest_race_id = group_rid;
  if (dest_race_id == 0) {
    if (!ensure_race()) return;   // WiFi不通等：送らずデータは残す
    dest_race_id = s_race_id;
  }
  if (dest_race_id == 0) return;

  // dest グループ(rid==group_rid)の行だけを最大100件、events配列へ積む。
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
    ev.remove("rid");                                       // rid は内部用。送信payloadから除く
    arr.add(ev);
    n++;
    if (n >= 100) { capped = true; break; }                // 1回のPOSTは100件まで
  }
  f.close();
  if (n == 0) return;
  String payload; serializeJson(out, payload);

  String url = server_base() + "/api/timing/races/" + dest_race_id + "/events";
  WiFiClientSecure _c; _c.setInsecure();
  HTTPClient http;
  http.begin(_c, url);
  add_common_headers(http);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(payload);
  http.end();

  if (code != 200) {
    Serial.printf("[POST] 失敗 code=%d\n", code);
    s_send_blocked = true;              // 1-e：送信失敗→次レース封じ
    return;
  }

  // 送信成功：送った group_rid の行を（最大100件ぶん）スプールから除去。
  rewrite_spool_drop_group(group_rid, n);
  s_send_blocked = false;              // 1-e：送信成功→封じ解除
  Serial.printf("[POST] rid=%u %d件 送信%s\n", group_rid, n, capped ? "（続きあり・同rid）" : "");
}

// ---- レイアウト取得（GW向け軽量版 /for_gw・docs/19.16）--------------------
//  サーバーから target_laps / lap_length_m / nodes 等を取得してログ表示する。
//  戻り値：取得成功=true。呼び出し間隔は tick_fetch_layout がバックオフ管理（#14）。
static bool fetch_layout() {
  if (!wifi_up()) return false;
  WiFiClientSecure _c; _c.setInsecure();
  HTTPClient http;
  http.begin(_c, server_base() + "/api/timing/layouts/" + String((int)s_layout_id) + "/for_gw");
  add_common_headers(http);
  int code = http.GET();
  bool ok = false;
  if (code == 200) {
    JsonDocument res;
    if (deserializeJson(res, http.getString()) == DeserializationError::Ok) {
      int laps  = res["target_laps"] | 0;
      int nodes = res["node_count"]  | 0;
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
static void tick_fetch_layout() {
  static uint32_t last = 0;
  static bool first = true;
  uint32_t interval = s_layout_ok ? LAYOUT_OK_MS : LAYOUT_NG_MS;
  if (!first && (millis() - last) < interval) return;
  first = false;
  last  = millis();
  s_layout_ok = fetch_layout();
}

// ---- 本体ボタン読み取り ----------------------------------------------------
static void tick_buttons() {
  // 赤：押下エッジ（20ms）
  static bool red_last = false; static uint32_t red_t = 0;
  bool red_now = (digitalRead(PIN_BTN_RED) == LOW);
  if (red_now != red_last && (millis() - red_t) > DEBOUNCE_MS) {
    red_t = millis(); red_last = red_now;
    if (red_now) on_signal_pressed();
  }
  // 灰：600ms長押しでRESET（docs/12 S9）／さらに3秒超で送信封じの緊急解除（1-e）
  static uint32_t gray_down = 0; static bool gray_fired = false; static bool ovr_fired = false;
  bool gray_now = (digitalRead(PIN_BTN_GRAY) == LOW);
  if (gray_now) {
    if (gray_down == 0) { gray_down = millis(); gray_fired = false; ovr_fired = false; }
    else {
      uint32_t held = millis() - gray_down;
      if (!gray_fired && held >= RESET_HOLD_MS) {
        gray_fired = true; on_reset_pressed();     // 600ms：通常RESET（再送も試みる）
      }
      if (!ovr_fired && held >= OVERRIDE_HOLD_MS) {
        ovr_fired = true;                          // 3秒：封じの緊急解除
        if (s_send_blocked) {
          s_send_blocked = false;
          Serial.println("[SEQ] 送信封じを強制解除（データはスプールに保持・後日後送り可）");
        }
      }
    }
  } else { gray_down = 0; }
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
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);   // 起動直後は必ず消灯
  if (!LittleFS.begin(true, "/littlefs", 10, "littlefs")) Serial.println("LittleFS mount 失敗");  // K7: csvのName列を明示
  Serial.printf("PSRAM=%u (VE normal: approx 4MB mapped of 8MB)\n", (unsigned)ESP.getPsramSize());
  if (!mesh::begin(NODE_ID, g_channel, on_recv)) {
    Serial.println("ESP-NOW init 失敗"); return;
  }
  beam::begin();
  disp::begin();                       // TFT初期化（PSRAMスプライト確保）
  disp::g_status.gw_id = NODE_ID;
  disp::g_status.ch    = g_channel;
  wifi_up();
  Serial.println("稼働開始。赤=スタート / 灰長押し=RESET。");
}

// 経過秒フィールド（案B）の設定。
//  s_show_ontrack：いまフルフレーム層にON TRACK画面が出ているか（tick_displayが4fpsで更新）。
//   これがtrueのときだけ経過秒フィールドを上に重ねる（エラー面の上に重ねない保険）。
static bool s_show_ontrack = false;
static constexpr uint32_t TIME_FIELD_MS = 33;   // 約30fps（1000/30≒33）

// 経過秒フィールドだけを高頻度で部分更新する（案B）。loop()から毎周回呼ぶ。
//  tick_displayの250msゲートに縛られず、ON TRACK表示中のみ経過秒を機敏に動かす。
//  ⚠打刻はISR/tsync由来なので、この描画頻度は計測精度に一切影響しない。
static void tick_time_field() {
  if (!s_show_ontrack) return;                        // ON TRACK表示中のみ
  static uint32_t last_t = 0;
  if (millis() - last_t < TIME_FIELD_MS) return;      // 約30fpsに間引き
  last_t = millis();
  uint32_t elapsed = s_green_t_us ? (uint32_t)((tsync::now_gw_us() - s_green_t_us) / 1000ULL) : 0;
  disp::draw_time_field(elapsed, /*hundredths=*/true);   // 1/100秒（ストップウォッチ的）
}

// ---- TFT描画：状態機械に同期して画面を切り替える（docs/20・12.3）----------
//  ステータスバーの○×はここで g_status に反映してから各画面を描く。
static void tick_display() {
  static uint32_t last = 0;
  static bool blink = false;
  if (millis() - last < 250) return;     // 約4fps（●点滅もこの周期）
  last = millis();
  blink = !blink;

  // ステータス更新（NODE充足は残課題#8で実数へ。今はJOIN実数の反映口のみ用意）
  disp::g_status.wifi_ok = (WiFi.status() == WL_CONNECTED);
  disp::g_status.ch      = g_channel;
  // beam_ok / node_have / node_need / unsent は各機能側から順次代入予定。

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
      // ARMED画面はTFTでは持たない（docs/20.6・シグナル機が担当）。待機表示を維持。
      disp::draw_idle();
      s_show_ontrack = false;
      break;
    case ST_GREEN:
    case ST_RACE: {
      uint32_t elapsed = s_green_t_us ? (uint32_t)((tsync::now_gw_us() - s_green_t_us) / 1000ULL) : 0;
      disp::draw_ontrack(elapsed, blink, s_target_laps);
      s_show_ontrack = true;      // 以降、経過秒フィールドを約30fpsで重ねる
      break;
    }
  }
  buzzer_error_edge(false);              // エラー無し＝立ち上がり検出をリセット（再発時にまた1回鳴る）
  disp::commit();                        // ステータスバーを重ねて一括転送
}

void loop() {
  tick_buttons();
  buzzer_tick();              // ブザー非ブロッキング消灯（29章）
  tick_gw_presence();         // GW自身の在席ビーコン（GW2台検知の相互化）
  tick_sequence();
  tick_race_registration();   // #20: レース作成/green後付けをloop文脈で非同期実行

  beam::Hit hit;
  int drain = 0;
  while (drain++ < 8 && beam::poll(hit)) {   // 1周8件まで（浮きGPIO洪水でloopを占有させない）
    static uint32_t self_seq = 0;
    RawEvent e{ (uint8_t)NODE_ID, mesh::boot_id(), ++self_seq,
                hit.lane, hit.quality, hit.t_a_us, hit.t_b_us };
    spool_append(e);
    note_quality(hit.quality, (uint8_t)NODE_ID);   // 自機S/GのA1/A2も拾う（27章）
    Serial.printf("[SG] lane=%u q=%u\n", hit.lane, hit.quality);
    if (s_state == ST_IDLE) buzzer_beep();   // 待機中のS/G通過のみ 60ms「ピッ」（29章）
  }

  // 24.3：3秒ごとの機械的POSTは廃止。送信は灰ボタン(on_reset_pressed)起点のみ。
  //   （旧: static uint32_t last=0; if (millis()-last>3000){ last=millis(); flush_spool(); }）

  tick_fetch_layout();   // #14：成功60s/失敗5sのバックオフで /for_gw を取得
  tick_display();        // TFT描画：全画面フル更新（状態機械に同期・約4fps）
  tick_time_field();     // 案B：ON TRACK経過秒だけ約30fpsで部分更新（機敏に動かす）
}
