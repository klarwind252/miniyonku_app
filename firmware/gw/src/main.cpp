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
#define LOG_TO_FS 1
#include "gwlog.h"      // ★20261102 全ログ記録（時刻プレフィックス＋PSRAMリング＋/gwlog.txt）。以降の Serial は gwlog::out
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
// 20260831i：signalなし（走行式）の合計タイム起点モード。
//   (い) START_MODE_SHARED=1（既定）：最初に通過した1台の時刻を全車共通の起点にする。
//        → 後からスタートした車のタイムも「最初の1台の初通過」から測る（ユーザー確定・20260831）。
//   (あ) START_MODE_SHARED=0：各車が自分の初通過(s_lane_first_us[m])を起点にする（DA4走行式の原形）。
//   ★将来 (あ) に戻す可能性あり（ユーザー指示・忘れないこと）。戻すときはここを 0 にするだけ。
//   ※signalあり（F1式・緑起点）はこのスイッチと無関係に常に緑時刻が全車共通起点。
// ★20261029 ユーザー確定：走行式は「各車が自分の初通過」を基準にする＝(あ)。
//   READYで最初にゲートをくぐった車で計測開始、後から走り出した2台目・3台目は
//   それぞれ自分のスタート打刻を起点に合計を出す。サーバー(race_builder 走行式)も同じ。
//   レース(F1式・シグナル)は従来どおり緑の瞬間が全車共通の起点。
#ifndef START_MODE_SHARED
#define START_MODE_SHARED 0
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
// 店舗スラグ（マルチ店舗。空=既定店舗／"arcadebase"等でその店舗へ投げる）。20261029。
//   投げ先URLは  https://<host>[/<slug>]/api/timing/...  の形。
//   既定はビルドフラグ -DSTORE_SLUG=\"arcadebase\"（platformio.ini env:gw6a）。NVS "slug" が優先。
#ifndef STORE_SLUG
#define STORE_SLUG ""
#endif

// 実行時の設定（NVS "m4cfg" が上書き。無ければ既定 / secrets.h）。load_config()で確定。
static String g_ssid  = WIFI_SSID;
static String g_pass  = WIFI_PASS;
static String g_host  = DEF_SERVER_HOST;
static String g_ip    = DEF_SERVER_IP;
static String g_token = TIMING_TOKEN;
static String g_slug  = STORE_SLUG;   // 店舗スラグ（空=既定店舗）。NVS "slug" で上書き可
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
// 店舗スラグのパス前置（"/arcadebase"等。空なら前置なし＝既定店舗）。先頭/を保証・末尾/除去。
static inline String api_prefix() {
  if (g_slug.isEmpty()) return String("");
  String s = g_slug;
  if (!s.startsWith("/")) s = "/" + s;
  while (s.length() && s.endsWith("/")) s.remove(s.length() - 1);
  return s;
}
// Hostヘッダを付けるべきか（IP直のときだけ必要。ホスト名運用時は不要）。
static inline bool need_host_header() { return !s_dns_ok; }

// http.begin後に共通ヘッダを付ける（Host＋任意トークン）。IP直時のみHost付与。
static void add_common_headers(HTTPClient& http) {
  if (need_host_header()) http.addHeader("Host", g_host);
  if (g_token.length())   http.addHeader("X-Timing-Token", g_token);
}
// infra/http_json.h — HTTPS(JSON) 呼び出しの共通化（接続使い回し）
// ============================================================================
//  旧 main.cpp では「WiFiClientSecure＋HTTPClient＋タイムアウト＋共通ヘッダ＋POST/GET＋JSON解析」が
//  create_race / forward_join_now / fetch_layout / flush_spool の4箇所にコピーされていた。ここに1本化。
//
//  ★20261031 性能（灰→PUSHが遅い対策）：
//    ・旧：毎回 WiFiClientSecure を新規生成＝毎回TLSフルハンドシェイク（ESP32で1〜2秒）。
//      灰1回＝create_race＋events で握手2回＝2〜4秒、rid が複数なら×2ずつ。
//    ・新：クライアントとHTTPClientを**1組だけ保持**し `setReuse(true)`。同一ホストへの連続要求は
//      keep-alive で握手なし（数百ms）。サーバー側が切っていれば HTTPClient が自動で張り直す。
//    ・応答タイムアウト：旧1.5秒はHTTPSがloop内にあった時代の「loopを止めない」ための値。
//      netタスク化で不要になったので用途別に延長（events=8s/作成=5s/他=3s）。
//      → サーバーの後処理で1.5秒を超えて“失敗扱い→同じ100件を再POST”していた無駄打ちが消える。
//  ⚠ netタスク専用（同時に複数スレッドから呼ばない）。
// ============================================================================

namespace httpj {

static constexpr uint16_t CONNECT_MS = 800;    // 接続は従来どおり0.8秒で諦める（不達の早期判定）
static constexpr uint16_t T_SHORT_MS = 3000;   // JOIN転送・レイアウト取得
static constexpr uint16_t T_CREATE_MS= 5000;   // レース作成
static constexpr uint16_t T_EVENTS_MS= 8000;   // イベント一括POST（サーバー後処理の揺れを吸収）

static WiFiClientSecure* s_cli  = nullptr;     // 1組だけ保持（netタスク専用）
static HTTPClient*       s_http = nullptr;
static String            s_last_base;          // ベースURLが変わったら（DNS可否の切替等）張り直す

static void _ensure() {
  if (!s_cli)  { s_cli = new WiFiClientSecure(); s_cli->setInsecure(); }   // K4：CN検証なし（従来どおり）
  if (!s_http) { s_http = new HTTPClient(); s_http->setReuse(true); }
}
static void _begin(const String& path) {
  _ensure();
  String base = server_base() + api_prefix();
  if (base != s_last_base) { s_http->end(); s_last_base = base; }       // 接続先が変わった＝旧接続を捨てる
  s_http->begin(*s_cli, base + path);
  s_http->setConnectTimeout(CONNECT_MS);
  add_common_headers(*s_http);                                          // Host(IP直時のみ)＋トークン
}
static void _finish(int code) {
  // 接続は保持（reuse）。ただし通信エラー（code<0）なら次回クリーンに張り直す。
  if (code < 0) { s_http->end(); s_cli->stop(); }
}

// body 非空=POST(JSON) / 空=GET。HTTPステータスを返す（<0 は接続失敗）。
// res!=nullptr かつ 200 なら本文をJSON解析して格納（parsed に成否）。
static int request(const String& path, const String& body, JsonDocument* res,
                   bool* parsed = nullptr, uint16_t timeout_ms = T_SHORT_MS) {
  uint32_t t0 = millis();
  _begin(path);
  s_http->setTimeout(timeout_ms);
  int code;
  if (body.length()) { s_http->addHeader("Content-Type", "application/json"); code = s_http->POST(body); }
  else               { code = s_http->GET(); }
  bool ok = false;
  if (code == 200 && res) ok = (deserializeJson(*res, s_http->getString()) == DeserializationError::Ok);
  else if (code > 0)      { String _drain = s_http->getString(); (void)_drain; }   // 本文を読み切る（keep-alive維持）
  if (parsed) *parsed = ok;
  _finish(code);
  Serial.printf("[HTTP] %s %s -> %d (%lums%s)\n", body.length() ? "POST" : "GET", path.c_str(), code,
                (unsigned long)(millis() - t0), (res && code == 200) ? (ok ? " json ok" : " json NG") : "");   // ★20261102 全要求を記録
  return code;
}

// 生バッファ版（自前生成JSON・PSRAMバッファ可）。戻り値はHTTPステータス。
static int request_raw(const String& path, const char* body, size_t len, uint16_t timeout_ms = T_EVENTS_MS) {
  _begin(path);
  s_http->setTimeout(timeout_ms);
  s_http->addHeader("Content-Type", "application/json");
  uint32_t t0 = millis();
  int code = s_http->POST((uint8_t*)body, len);
  if (code > 0) { String _drain = s_http->getString(); (void)_drain; }   // 本文を読み切る（keep-alive維持）
  _finish(code);
  Serial.printf("[HTTP] POST %s %uB -> %d (%lums)\n", path.c_str(), (unsigned)len, code, (unsigned long)(millis() - t0));
  return code;
}

} // namespace httpj

// ---- fetch_layout の再取得間隔（残課題#14：成功=60s / 失敗=5s のバックオフ）--
static constexpr uint32_t LAYOUT_OK_MS   = 60000;   // 取得成功後は60秒あける
static constexpr uint32_t LAYOUT_NG_MS   = 30000;   // ★失敗中は30秒で再試行（旧5秒＝TLSブロック頻発でESP-NOWを邪魔していた・T-9）
static bool s_layout_ok = false;                    // 直近取得の成否
static volatile bool s_layout_req = true;           // ★P3：取得要求（起動時true・灰リセットで再要求）。netタスクが消化

// ---- 本体ボタン（docs/07.4）-----------------------------------------------
static constexpr int PIN_BTN_RED  = 26;   // 赤 SIGNAL（押下LOW・内部プルアップ）
static constexpr int PIN_BTN_GRAY = 27;   // 灰 RESET
static constexpr uint32_t DEBOUNCE_MS   = 20;
// 20260904 改修①：長押しは一切使わない。灰＝単押し即RESETのみ（tick_buttonsで実装済み）。
//   旧 RESET_HOLD_MS(600ms長押し)・OVERRIDE_HOLD_MS(灰3秒で封じ強制解除) は未使用の死に定義
//   だったため削除。関連する「長押し」前提のコメントも本改修で撤去した。

// ---- ブザー（GPIO12・他励式SPT15＋外部基盤12V直・ledc駆動）★20261107 12V外部モジュール対応------
//  旧：自励式109800/UGCM1205を5VでdigitalWrite HIGH/LOW（音程固定・5Vブザー）。
//  新：他励式SPT15を外部基盤(docs/40)で鳴らす。音程は与えないと鳴らない＝ledc(PWM)で周波数駆動。
//    ・通過/切断の「ピッ」＝共振付近の高め（最大音量）。
//    ・スタートの「ブー」＝低め（可聴の低音を鳴らす目的・150〜250Hz）。緑点灯と同時に0.5秒。
//    ・エラーの「ピー」＝高め2秒・1回。いずれも非ブロッキング（millis管理でloopを止めない）。
//  ⚠ 配線（20261107実装確定・docs/40 §3）：IO12（紫リード）→外部基盤 1kΩ→SS8050ベース、
//    SS8050コレクタ→SPT15(−)、SPT15(+)＝12V直（470µF＋）。放電抵抗1kΩ(R_BZ)は外部基盤側。
//    GPIOから見た駆動はledcトーンのまま不変（昇圧回路・イネーブルGPIOは無し。12Vは外部DC）。
//  ⚠ Y2本体の既存5Vブザー(Q2/BZ1)は未使用（IO12を外部基盤へ引き出し。本体側は要切離し確認）。
static constexpr int      PIN_BUZZER       = 12;    // ストラップだが実機で書込み/起動OK確認済(20260811)
static constexpr uint8_t  BUZZER_LEDC_CH   = 2;     // ★ledcチャンネル。ch2=timer1（38kHz IRのch0=timer0とタイマー非共有）。ch1はtimer0共有でNG（20261012修正）
static constexpr uint8_t  BUZZER_LEDC_BITS = 10;    // 分解能（ledcWriteToneがduty50%を自動設定）
static constexpr uint32_t BUZZER_F_BEEP    = 2000;  // ピッ（通過・切断）。高めの明瞭音。SPT15最大音量は約1200Hz（85dBA@9Vpp・データシート）
static constexpr uint32_t BUZZER_F_START   = 1000;  // ブー（スタート）。※圧電は200Hz等の低音はほぼ無音→可聴の低めへ。最大音量が要るなら1200へ
static constexpr uint32_t BUZZER_F_ERR     = 1200;  // ピー（エラー）＝SPT15最大音量点1200Hz
static constexpr uint32_t BUZZER_MS        = 60;    // 通過ピッ鳴動長（80→40→20→60で実機比較し確定）
static constexpr uint32_t BUZZER_START_MS  = 500;   // スタート「ブー」長（緑と同時・DC27/DC30）
static constexpr uint32_t BUZZER_ERR_MS    = 500;   // エラー/WiFi切断/ノード離脱＝2000Hz・500msに統一（20261030）
static constexpr uint32_t GOAL_FREQ        = 800;   // ★ゴール(完走)音の周波数（20261030）
static constexpr uint32_t GOAL_MS          = 500;   // ★ゴール(完走)音の長さ
static uint32_t s_buzz_off_ms = 0;           // 0=消音中／非0=この millis で消音予定
static bool     s_sg_beeped   = false;       // ST_IDLE中に既にS/G通過ピッを鳴らしたか（TAの毎回鳴り防止・灰リセットで復活）

// ── 起動ジングル（パチンコ風・非ブロッキング／20261109）──────────────────────────
//  圧電SPT15は単音のみ＝旋律は時分割で1音ずつ鳴らす。loopのbuzzer_jingle_tick()が進行役。
//  明るい上昇ラン＋高音アクセントの約0.85秒（≤2秒厳守）。周波数は可聴域(1.3〜2.6kHz)に限定
//  （圧電は低音ほぼ無音・データシート最大音量点付近を使う）。a/b両ロゴ共通で鳴らす。
struct BuzNote { uint16_t freq; uint16_t ms; };     // freq=0 は無音（音符間の切れ目＝粒立ち）
static const BuzNote BOOT_JINGLE[] = {
  {1319, 60}, {0, 20}, {1568, 60}, {0, 20}, {1976, 60}, {0, 20},   // ミ↑ソ↑シ：弾む立ち上がり
  {1319, 55}, {1568, 55}, {1976, 55}, {2349, 55}, {0, 25},         // ピロリロ↑：速い上昇ラン
  {1976, 60}, {2349, 60}, {2637, 240},                            // 締めの高音アクセント「キラッ」
};  // 合計 ≒ 845ms
static constexpr size_t BOOT_JINGLE_N = sizeof(BOOT_JINGLE) / sizeof(BOOT_JINGLE[0]);
static int      s_jingle_idx     = -1;       // -1=停止中／0..N-1=再生中の音符インデックス
static uint32_t s_jingle_next_ms = 0;        // この millis で次の音符へ進む

static inline void buzzer_tone(uint32_t freq);   // 前方宣言（jingle_tickが使用）
static inline void buzzer_jingle_start() {       // 起動時に1回だけ呼ぶ（以後loopで進行）
  s_jingle_idx = 0;
  s_jingle_next_ms = millis();               // 先頭音へ即時
}
// ledc(PWM)で周波数を出す/止める（コア2.x=ch指定 / 3.x=pin指定・beam.hと同方式）。freq=0で停止。
static inline void buzzer_tone(uint32_t freq) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWriteTone(PIN_BUZZER, freq);
#else
  ledcWriteTone(BUZZER_LEDC_CH, freq);
#endif
}
static inline void buzzer_start(uint32_t ms, uint32_t freq = BUZZER_F_BEEP) {   // 非ブロッキング開始
  s_jingle_idx = -1;   // ★実イベント（スタート/エラー/通過）は起動ジングルより優先＝ジングル中断
  Serial.printf("[BUZ] %luHz %lums%s\n", (unsigned long)freq, (unsigned long)ms, s_buzz_off_ms ? " (上書き)" : "");   // ★20261102
  buzzer_tone(freq);
  s_buzz_off_ms = millis() + ms;
  if (s_buzz_off_ms == 0) s_buzz_off_ms = 1; // millis境界で0になる稀ケースを回避
}
static inline void buzzer_beep() {           // S/G通過の瞬間に呼ぶ（60ms「ピッ」）
  buzzer_start(BUZZER_MS, BUZZER_F_BEEP);
}
static inline void buzzer_tick() {           // loop毎に呼ぶ（時間が来たら消音）
  if (s_buzz_off_ms && (int32_t)(millis() - s_buzz_off_ms) >= 0) {
    buzzer_tone(0);
    s_buzz_off_ms = 0;
    Serial.println("[BUZ] off");   // ★20261102
  }
}

// 起動ジングルの進行（loop毎・非ブロッキング）。buzzer_tickと独立。freq=0は無音。
//  実イベントのbuzzer_start()が来たら s_jingle_idx=-1 にされ、この関数は即停止する。
static inline void buzzer_jingle_tick() {
  if (s_jingle_idx < 0) return;                            // 停止中
  if ((int32_t)(millis() - s_jingle_next_ms) < 0) return;  // まだ現在の音符の途中
  if (s_jingle_idx >= (int)BOOT_JINGLE_N) {                // 末尾まで鳴らし終えた
    buzzer_tone(0);
    s_jingle_idx = -1;
    return;
  }
  const BuzNote& n = BOOT_JINGLE[s_jingle_idx];
  buzzer_tone(n.freq);                                     // freq=0 は無音
  s_jingle_next_ms = millis() + n.ms;
  s_jingle_idx++;
}

// エラー表示の立ち上がり（無→有）でだけ 2秒「ピー」を1回鳴らす。
//  err_now=エラー面を出しているか。出し続けている間は再鳴動しない（1回だけ）。
//  エラーが一旦消えて再発したら、また1回鳴る。tick_display（約4fps）から呼ぶ。
static inline void buzzer_error_edge(bool err_now) {
  static bool prev = false;
  if (err_now && !prev) buzzer_start(BUZZER_ERR_MS, BUZZER_F_BEEP);  // 2000Hz・500ms（20261030）
  prev = err_now;
}

// ---- 信号灯（GW一体・GPIO13赤/GPIO14緑→外部基盤IRLU3410→12VテープLED）★20261107 12V外部モジュール対応------
//  旧：別筐体SG機へ ESP-NOW(CMD_RED/GREEN/RESET) で指示（signal_cmd・DC70で撤去）。
//  旧2：GW GPIO13/14→IRLU3410→抵抗内蔵5V -VV砲弾LED（オンボード）。
//  新：GPIO13/14（黄/青リード）で外部基盤のIRLU3410(ローサイド)を駆動し、
//    12V RGBテープLED（アノードコモン・104293/104292）の R/G カソードを叩く（docs/40・20261107通電確認§2.4）。
//    赤ボタン→赤点灯→2〜5秒→緑点灯＋ブザー0.5秒→0.05秒後に赤消灯→2秒後に緑消灯。
//    ★テープはR/G 2色のみ使用。青(B)はIO16未配線＝DNP将来枠（現ファームは赤緑のみで完結）。
//  ⚠ 低サイドHIGH=ONはオンボード5V版と同一＝ファーム極性は不変。テープ共通＋=12V直（FET右足がGND役）。
//  ⚠ G–GND間4.7kΩプルダウン（起動時の薄点灯防止・DC74/DC104）は外部基盤側。13/14は非ストラップ。
static constexpr int      PIN_SIG_RED   = 13;   // 黄リード→外部基盤100Ω→IRLU3410(赤)ゲート→テープ赤カソード
static constexpr int      PIN_SIG_GREEN = 14;   // 青リード→外部基盤100Ω→IRLU3410(緑)ゲート→テープ緑カソード
static constexpr uint32_t GREEN_HOLD_MS = 2000; // 緑点灯の保持（最終確定2秒・DC30）
static constexpr uint32_t GREEN_LEAD_MS = 50;   // 緑先行→0.05秒後に赤を消す重なり（暗転の隙間なし・DC32）
static uint32_t s_green_off_ms = 0;   // 0=予約なし／非0=この millis で緑を自動消灯
static uint32_t s_red_off_ms   = 0;   // 0=予約なし／非0=この millis で赤を消灯（緑先行の重なり後）
// ★通過検知フラッシュ（緑シグナルLEDを一瞬＋高音ピッ）。スタート演出とは独立・暫定。
static constexpr uint32_t DETECT_FLASH_MS = 50;   // 緑を光らせる長さ（一瞬・見えればOK）
static constexpr uint32_t DETECT_BEEP_MS  = 80;   // 通過「ピッ」の長さ（40→80・少し長く・20261030）
static uint32_t s_detect_green_off_ms = 0;        // 検知フラッシュの消灯予約（0=なし）
// ★20261102：点灯/消灯の「変化」だけログに残す（フライング点滅も含む）
static bool s_sig_red_on = false, s_sig_green_on = false;
static inline void sig_red(bool on)   { digitalWrite(PIN_SIG_RED,   on ? HIGH : LOW);
                                        if (on != s_sig_red_on)   { s_sig_red_on = on;   Serial.printf("[SIG] red=%d\n",   on ? 1 : 0); } }
static inline void sig_green(bool on) { digitalWrite(PIN_SIG_GREEN, on ? HIGH : LOW);
                                        if (on != s_sig_green_on) { s_sig_green_on = on; Serial.printf("[SIG] green=%d\n", on ? 1 : 0); } }
static inline void sig_all_off() {                     // 全消灯＋予約解除（RESET/完走時）
  sig_red(false); sig_green(false); s_green_off_ms = 0; s_red_off_ms = 0;
}
static inline void sig_on_red() {                      // 赤点灯（赤ボタン/CMD_SIGNAL受付時）
  sig_red(true); sig_green(false); s_green_off_ms = 0; s_red_off_ms = 0;
}
static inline void sig_on_green() {                    // 緑点灯の唯一の入口（tick_sequenceの緑遷移で）
  sig_green(true);
  s_red_off_ms   = millis() + GREEN_LEAD_MS;           // 0.05秒後に赤を消す（赤緑オーバーラップ・DC32）
  s_green_off_ms = millis() + GREEN_HOLD_MS;           // 2秒後に緑を自動消灯（DC30）
}
// ★20261030 動作変更：READYは常時赤点灯。赤を点灯しっぱなしにする（緑消灯・自動消灯予約も解除）。
static inline void sig_ready_red() {
  sig_red(true); sig_green(false);
  s_green_off_ms = 0; s_red_off_ms = 0; s_detect_green_off_ms = 0;
}
static inline void sig_tick() {                        // loop毎に呼ぶ（時間が来たら消灯・非ブロッキング）
  uint32_t now = millis();
  if (s_red_off_ms   && (int32_t)(now - s_red_off_ms)   >= 0) { sig_red(false);   s_red_off_ms = 0; }
  if (s_green_off_ms && (int32_t)(now - s_green_off_ms) >= 0) { sig_green(false); s_green_off_ms = 0; }
  if (s_detect_green_off_ms && (int32_t)(now - s_detect_green_off_ms) >= 0) {
    if (!s_green_off_ms) sig_green(false);   // スタート緑が点いていなければ消す（信号演出を優先）
    s_detect_green_off_ms = 0;
  }
}
// ★通過検知フィードバック：緑シグナルLEDを一瞬＋高音ピッ。
//   スタート演出で緑を保持中(s_green_off_ms!=0)なら緑には触れずピッだけ（信号を壊さない）。
static inline void detect_flash() {
  if (!s_green_off_ms) {
    sig_green(true);
    s_detect_green_off_ms = millis() + DETECT_FLASH_MS;
  }
  // ★20261031：通過ピッは低優先。鳴動中（スタート/ゴール/フライング/エラーの最中）は出さない。
  //   旧：無条件に buzzer_start → 緑点灯直後に車がS/Gを横切ると500msのスタート音が80msの
  //   ピッで上書きされ「スタート音が鳴ったり鳴らなかったり」の原因になっていた（実機確認）。
  if (!s_buzz_off_ms) buzzer_start(DETECT_BEEP_MS, BUZZER_F_BEEP);   // 一瞬「ピッ」＝2kHz（高音）
}

// ---- スタート演出の状態機械（docs/12.3）-----------------------------------
enum GwState { ST_SPLASH, ST_IDLE, ST_ARMED, ST_GREEN, ST_RACE, ST_FINISH };
static GwState s_state = ST_IDLE;
static char gwlog_state_char() {   // ★20261102 ログ行頭の状態文字：S=splash I=READY A=armed G=green R=race F=finish
  switch (s_state) { case ST_SPLASH: return 'S'; case ST_IDLE: return 'I'; case ST_ARMED: return 'A';
                     case ST_GREEN: return 'G'; case ST_RACE: return 'R'; case ST_FINISH: return 'F'; }
  return '?';
}

// ★フライング検知（レースモード・赤点灯中=ST_ARMED=スタート前のS/G通過）。20261029。
//   赤ボタン→赤点灯→ランダム→緑の「赤の間」にマシンがS/Gを通過したらフライング。
//   演出：赤を点滅＋長め「ピー」で FLYING_HOLD_MS(1秒) だけ“絶対優先”。この1秒間は
//         別マシンが通過しても緑の通過フラッシュ(detect_flash)は出さない（緑点灯しない）。
//   レースは止めない。通過の生データは通常どおりspool→サーバーが green_t_us 比較で
//   jump_start(JS=フライング)の印を付ける（アプリ側は実装済み）。
static constexpr uint32_t FLYING_HOLD_MS  = 1000;  // フライング優先表示の長さ（1秒・ユーザー確定）
static constexpr uint32_t FLYING_BLINK_MS = 120;   // 赤点滅の半周期
static uint32_t s_flying_until_ms = 0;             // 0=非／この millis まで優先窓
static uint32_t s_flying_blink_at = 0;             // 次の点滅トグル時刻
static bool     s_flying_red_on   = false;
static bool     s_flying_seen     = false;         // このレースでフライングがあった事実（灰リセットでクリア）
static inline bool flying_active() {
  return s_flying_until_ms && (int32_t)(millis() - s_flying_until_ms) < 0;
}
static inline void flying_trigger() {              // フライング通過の瞬間に呼ぶ
  s_flying_until_ms = millis() + FLYING_HOLD_MS;    // 1秒間フライング優先
  s_flying_blink_at = millis();
  s_flying_red_on   = true;
  s_flying_seen     = true;
  sig_green(false);                                 // 緑は絶対に出さない
  sig_red(true);
  buzzer_start(FLYING_HOLD_MS, BUZZER_F_ERR);        // 長め「ピー」＝1秒・1200Hz（エラー音＝最大音量）
}
static inline void flying_tick() {                 // loop毎：赤点滅と窓終了処理
  if (!s_flying_until_ms) return;
  uint32_t now = millis();
  // 窓終了、または赤フェーズを抜けた(緑/計測/待機へ)らフライングLED制御を手放す。
  if ((int32_t)(now - s_flying_until_ms) >= 0 || s_state != ST_ARMED) {
    s_flying_until_ms = 0;
    if (s_state == ST_ARMED) sig_red(true);          // まだ赤フェーズ：赤点灯で戻す
    return;
  }
  if ((int32_t)(now - s_flying_blink_at) >= 0) {     // 点滅トグル
    s_flying_red_on = !s_flying_red_on;
    sig_red(s_flying_red_on);
    s_flying_blink_at = now + FLYING_BLINK_MS;
  }
}
static uint32_t s_splash_ms = 0;                 // スプラッシュ開始時刻（ST_SPLASH中）
static constexpr uint32_t SPLASH_MS = 3200;      // スプラッシュ総時間（締めの大拡大ぶん延長）
// 赤押下でSETを「即座に」出すための一発フラグ。on_signal_pressed（ESP-NOW受信
// コールバック文脈からも呼ばれる）でSPIを叩くとクラッシュしうるため、ここでは
// フラグだけ立て、実描画は loop（安全な文脈）で render_set_now() が行う。
static volatile bool s_set_now_req = false;
static volatile bool s_green_now_req = false;   // ★即緑→ON TRACK即描画をloop文脈で行う（20261030）

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
// 20260831k：シグナル二度押しガード時間。ARMED成立からこの時間内の再受付を弾く。
//   赤の最短点灯(s_red_dur_ms=2s)より十分短く、かつ人間の誤連打(数百ms)を吸収する400ms。
static constexpr uint32_t SIGNAL_GUARD_MS = 400;
// ★20261012：旧CMD_RED再送（ESP-NOWでSGへ送る取りこぼし対策）は廃止。
//   信号灯がGW自機のGPIO13直結になり、赤はsig_on_red()で即・確実に点くため再送不要。
static uint32_t s_red_dur_ms = 0;       // 今回の赤の長さ（2〜5秒ランダム）
static uint64_t s_green_t_us = 0;       // 緑を出したGW時刻（F1式・green_t_us）。走行式は0のまま
// 20260831i：走行式（signalなし）の全車共通起点＝「最初に通過した1台の初通過時刻」。
//   START_MODE_SHARED=1（い）で使用。最初の正常通過(ST_IDLE)で1回だけ立て、灰リセットで0へ。
//   s_green_t_us（緑の有無＝F1/走行式の判別を兼ねる）とは役割を分けて濁らせない。
static uint64_t s_start_shared_us = 0;
// 20260831i：TFT右上の経過タイム／各種計測に使う「レース全体の起点」を返す。
//   緑があればF1式（緑時刻）、無ければ走行式の共通起点(s_start_shared_us)。どちらも無ければ0。
//   これで走行式(い)でもTFT右上のカウントが最初の1台の通過から進む。
static inline uint64_t race_start_us() {
  return s_green_t_us ? s_green_t_us : s_start_shared_us;
}

// ---- レース識別・参加レーン（20260821改修）--------------------------------
//  s_internal_rid：GW内部の連番。スプール各行に刻む“取り違え防止用”のID。サーバーの
//    race_id とは別物で、flush時に s_ridmap で翻訳する。0=まだレース未開始。
//  s_race_active：現在レースが進行中か（begin_internal_race で true、flush/RESETで false）。
//  s_lane_cross/s_lane_active：GWのS/G(自機3レーン)の物理レーン別クロス数と活性化。
//    活性化(1回目通過)＝参加確定。完走は cross ≥ target_laps+1（起点1回ぶん足す）。
static uint32_t s_internal_rid = 0;
static bool     s_race_active  = false;
static uint32_t s_lane_cross[4] = {0,0,0,0};   // index 1..3＝マシン(スタートレーン)。★20260830b：物理レーン→マシンに変更
static bool     s_lane_active[4] = {false,false,false,false};   // マシンの参加確定（1回目通過で活性）
// ラップ・合計計算用の打刻（index 1..3）。20260830追加（TFTラップ表示）。
//  ★20260830b改：index は物理レーンではなく「マシン（＝スタートレーン）」。
//    TFTのL列は“そのレーンでスタートしたマシン”を追う（レーンチェンジ対応）。
static uint64_t s_lane_first_us[4] = {0,0,0,0};  // 起点通過（走行式の合計の起点）
static uint64_t s_lane_last_us[4]  = {0,0,0,0};  // 直近通過（ラップ＝前回との差分）
// ---- 本日のベスト（20260901e・案A＝GW自己完結／電源ON毎に揮発・時間系のみ）----------
//  ★採用条件（マシン単体で判定・台数も発進方式も不問）：そのマシンが“過不足なく”完走したこと。
//    過不足なし＝再同期(取りこぼし+2)を使わず、クロス数=target+1・ラップ数=target が一致。
//    → 3台中1台だけ完走でも、その1台が綺麗なら採用。取りこぼし/DNFは非採用（発進方式は問わない）。
//  ⚠ 速度(Av./Max)はGWでは算出しない（時刻送出のみ・§2.2）→ draw_idle側で「-」据置き。
//  ⚠ 灰リセット(RESET)では消さない＝1電源セッション通しての“本日”。消えるのは電源OFFのみ。
static uint32_t s_best_total_ms   = 0;   // 完走total_msの最小（0=未記録）
static uint8_t  s_best_total_lane = 0;   // それを出したマシン(=スタートレーン) 1..3（0=無）
static uint32_t s_best_lap_ms     = 0;   // 確定ラップの最小（0=未記録）
static uint8_t  s_best_lap_lane   = 0;
// ★20261109：レーン別ベストラップ（READYのLap行 L1/L2/L3 用。0=未記録／電源ON毎に揮発）。
//   既存ベストと同じ「過不足なし完走」のみ採用。
static uint32_t s_best_lap_lane_ms[3] = {0,0,0};
// ---- ローテーション追跡（表示・完走判定用／DA7の簡易版・20260830b）---------
//  物理レーンpのヒットを「どのマシンか」に帰属させる。直前レーンからの遷移を
//  +1（レーンチェンジ標準）→ 0（LCなし・周回コース）→ +2 の優先順で照合し、
//  一度観測した遷移(step)は以降そのマシンに固定する。
//  ⚠ spool（サーバー送信データ）は従来どおり物理レーンのまま＝土管原則は不変。
//    マシン単位になるのはTFT表示と完走判定だけ。厳密な同定はアプリ側（DA7）。
static constexpr uint64_t MIN_LAP_US = 1000000ULL; // 1周の最短想定(1s)。未満の再ヒットは別マシンの通過とみなす
static uint8_t s_m_lastlane[4] = {0,0,0,0};        // マシンmの直前物理レーン（0=未走行）
static bool    s_m_clean[4]    = {true,true,true,true}; // 20260901d：そのマシンが取りこぼし(再同期)無しで走れているか（本日のベスト採用判定用）
// ※s_m_step（遷移学習 0/+1/+2）は20260831jで廃止。本コースはLC=+1固定（誤学習が誤レーン記録の根因だった）。
// s_need_flush：送信要求フラグ。全員完走 or 灰ボタンで立て、loopのtick_flushがHTTPSを実行。
//   （on_reset_pressed はESP-NOWコールバックからも呼ばれるためHTTPSを直接呼ばない・#20）
static volatile bool s_need_flush = false;   // ★P2：loopが立て、netタスクが消化（volatile）

// ---- 受信EVENTレコード -----------------------------------------------------
struct RawEvent {
  uint8_t src; uint32_t src_boot; uint32_t seq;
  uint8_t lane; uint8_t quality; uint64_t t_us; uint64_t t_us_b;
};

static const char* SPOOL = "/spool.jsonl";
// 20260904 改修⑨：s_send_blocked（送信失敗で次レース封じ）を撤去。
//   どこからも読まれない書き込み専用の死にフラグで、実際には何も封じていなかった。
//   唯一の手動解除「灰3秒長押し」も改修①で廃止済み。送信失敗しても止めずに続ける
//   （改修②：WiFi不通はスプールして続行）方針と相反するため完全削除した。

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
static uint8_t  s_race_q2_lane = 0;                                    // A1発生レーン（1..3・20260830c）
static uint8_t  s_race_q2_miss = 0;                                    // A1でどちら側の張り付きか：1=A/2=B/3=両方
static uint8_t  s_race_q1_lane = 0;                                    // A2発生レーン（1..3）
static uint8_t  s_race_q1_miss = 0;                                    // A2でどちら欠け：1=A/2=B
// srcごとの q=2 発生レーンbit（bit0=L1..bit2=L3）。全3bit揃ったノードは
//   「全ビーム断＝TX共通系(LED電源/38kHz)疑い」としてGWのTFTに出す（20260830d・SE対応）。
static uint8_t  s_q2_lane_bits[proto::NODE_ID_MAX + 1] = {0};
static uint8_t  s_alldown_src = 0xFF;                                  // 全断に達したsrc（0xFF=なし）
// ノードのライブビーム健全性（HEARTBEAT付帯・20260831）。7秒無更新で失効扱い。
static uint8_t  s_node_stuck_bits[proto::NODE_ID_MAX + 1] = {0};       // bit0=L1..bit2=L3
static uint8_t  s_node_stuck_miss[proto::NODE_ID_MAX + 1][3] = {{0}};  // 各レーンのA/B側
static uint32_t s_node_stuck_ms[proto::NODE_ID_MAX + 1] = {0};         // 最終受信millis
static bool     s_beam_bad = false;  // Beam×：片/両ビーム欠を検知したら保持（灰リセットで解除）
static uint32_t s_last_q3_ms     = 0;   // B1：未同期打刻(q=3)を最後に受けた時刻→Sync×(自動復帰)
static uint32_t s_post_err_until = 0;   // 24.4⑤：灰ボタン後3秒だけA1/A2を出す窓(millis基準)
// READY感知不能ラッチ（20260831c）：READYで3秒感知不能を検知したらONで固定。
//   ラッチ中は赤ボタン・ビーム計測を一切禁止し、エラー画面を出しっぱなし。灰で解除。
//   自動では消えない（原因が直っても灰を押すまでラッチ継続）。
static bool     s_nobeam_latched   = false;
// 全画面エラー表示中フラグ（20260831e）：排他重複・全ビーム断・感知不能ラッチ等、
//   画面いっぱいのエラーを出している間は true。赤ボタン（本体/リモコン）を全面禁止する。
//   tick_display のエラー描画パスで毎フレーム更新（エラーが消えれば false）。灰は常に有効。
static bool     s_err_screen = false;
// 全画面エラーの「灰による強制解除」フラグ（20260831g）：どのエラー画面でも灰を押したら
//   true になり、原因（全断/排他/感知不能/A1/A2/Lost）が継続していてもエラー描画をスキップして
//   通常画面へ抜ける＝強制運用を可能にする。全エラー条件が一度healthyに戻ったフレームで
//   自動的に false に戻り、次に新しく起きたエラーは通常どおり再表示される。
static bool     s_err_ack    = false;
static uint8_t  s_nobeam_src  = 0xFF;   // 発生機体（NODE_ID=自機 / 0..5=SQ）
static uint8_t  s_nobeam_lane = 0;      // レーン1..3
static uint8_t  s_nobeam_miss = 0;      // 1=A/2=B/3=両方
static uint8_t  s_lost_src       = 0;   // C1：give-up通知の発生SQ(Lostラベル用・24.14)

// 受信/自機の通過qualityを per-raceフラグへ反映（A1/A2/Sync集約の入口）。
//  lane/miss を添えて「どのレーンのA/Bか」まで保持する（20260830c）。
static inline void note_quality(uint8_t q, uint8_t src, uint8_t lane = 0, uint8_t miss = 0) {
  if (q == 2)      { if (!s_race_q2) { s_race_q2 = true; s_race_q2_src = src; s_race_q2_lane = lane; s_race_q2_miss = miss; }
                     if (lane >= 1 && lane <= 3 && src <= proto::NODE_ID_MAX) {   // 全断検知（20260830d）
                       s_q2_lane_bits[src] |= (uint8_t)(1 << (lane - 1));
                       if (s_q2_lane_bits[src] == 0x07) s_alldown_src = src;
                     } }
  else if (q == 1) { if (!s_race_q1) { s_race_q1 = true; s_race_q1_src = src; s_race_q1_lane = lane; s_race_q1_miss = miss; } }
  if (q == 1 || q == 2) s_beam_bad = true;   // 片/両ビーム欠でBeam×（灰リセットまで保持）
  else if (q == 3) { s_last_q3_ms = millis(); }
}

// ビーム欠エラーのラベル生成：「SQ0 L1 A」等（機体名＋レーン＋A/B）。20260830c。
//  src=node_id（0..5=SQ / 6,7=GW）。lane=1..3。miss=1(A欠け)/2(B欠け)/0(不明)。
// 20260901j：本文へ差し込む日本語ラベルを作る（例「GW6 レーン1 センサーA」）。
//  機体名(GW6/SQ0..)は英数字のまま。lane→レーンN、miss(1/2)→センサーA/B。
static void build_beam_label(char* out, size_t n, uint8_t src, uint8_t lane, uint8_t miss) {
  const char* kind = (src <= 5) ? "SQ" : "GW";
  if (lane >= 1 && lane <= 3) {
    if (miss == 1)      snprintf(out, n, "%s%u レーン%u センサーA", kind, src, lane);
    else if (miss == 2) snprintf(out, n, "%s%u レーン%u センサーB", kind, src, lane);
    else if (miss == 3) snprintf(out, n, "%s%u レーン%u センサーAB", kind, src, lane);  // 20260901k：両側
    else                snprintf(out, n, "%s%u レーン%u", kind, src, lane);
  } else {
    snprintf(out, n, "%s%u", kind, src);
  }
}
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
  bool gw_peer = false; int rc = 0;
  for (uint8_t id = 0; id <= proto::NODE_ID_MAX; id++) {
    if (!node_alive(id)) continue;
    switch (proto::kind_of(id)) {
      case proto::KIND_GW: if (id != NODE_ID) gw_peer = true; break;  // 自分以外のGW＝重複
      case proto::KIND_RC: rc++; break;
      default: break;   // SQ・SGは排他対象外（★20261012：信号灯GW一体化でSG機廃止・旧SG残置でも誤ブロックしない）
    }
  }
  disp::g_status.gw_dup = gw_peer;
  disp::g_status.rc_dup = (rc >= 2);
  disp::g_status.sg_dup = false;        // ★SG機廃止：常にfalse（ERR_SG_DUPは発火しない）
  return gw_peer || (rc >= 2);
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
// ★20261102 再点検で追加：WiFi再接続の「スキャン」がESP-NOWを殺す問題への対策。
//  (a) Arduinoコア2.xは既定で自動再接続ON＝APが見つからない/切れると、コアが内部で
//      WiFi.begin() を即・無限に繰り返す。begin()のたびにSTAは全13chをスキャンし（1〜3秒）、
//      その間ESP-NOWは自分のchにいない＝SE/RCのパケットを取りこぼし、ACKも出せない。
//      スマホのテザリングが落ちた/届かない状態では、これが連続して「リモコンがほぼ効かない・
//      SEがLOST」になる。→ 自動再接続を切り、再試行はここ（netタスク・15秒間隔）だけにする。
//  (b) 計測中（ARMED/GREEN/RACE）は再接続を仕掛けない（スキャンで通過EVENTを落とさない）。
//  (c) 一度つながったAPのchが分かっていれば、再接続は原則そのchだけをスキャン（約0.1秒・
//      ESP-NOWの空白が13分の1）。4回に1回は全chスキャン（スマホがchを変えた場合の追従）。
static uint8_t  s_wifi_known_ch = 0;    // 直近に接続できたAPのch（0=未知）
static uint8_t  s_wifi_tries    = 0;    // 再接続の試行回数（全chスキャンの周期用）

static bool wifi_up() {
  if (WiFi.status() == WL_CONNECTED) {
    if (!s_wifi_was_up) {                       // 接続完了の立ち上がりで1回だけ
      s_wifi_was_up = true;
      Serial.printf("[WIFI] connected ssid=\"%s\" ip=%s rssi=%d\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(), WiFi.RSSI());   // ★20261102
      WiFi.setSleep(false);                     // ★AP接続で省電力が戻るのを再無効化（ESP-NOW脱落防止・T-8）
      probe_dns(); gw_adopt_wifi_channel();     // DNS判定＋実ch採用（従来どおり）
      { uint8_t p = 0; wifi_second_chan_t s = WIFI_SECOND_CHAN_NONE;
        if (esp_wifi_get_channel(&p, &s) == ESP_OK && p >= 1 && p <= 13) s_wifi_known_ch = p; }
      s_wifi_tries = 0;
    }
    return true;
  }
  if (s_wifi_was_up) Serial.printf("[WIFI] lost status=%d\n", (int)WiFi.status());   // ★20261102 切断の瞬間
  s_wifi_was_up = false;
  uint32_t now = millis();
  // ★(d) chガード：接続失敗のスキャン後にSTAの実chが運用ch(g_channel)からズレたままなら戻す。
  //   （未接続STAのchは誰も守ってくれない。ズレたままだとSE/RCのパケットを一切受けられない）
  //   直近 begin() から8秒以上経っている＝スキャン/接続手続き中ではない時だけ・2秒に1回だけ確認する。
  if (s_wifi_began && (now - s_wifi_try_ms) >= 8000) {
    static uint32_t s_chk_ms = 0;
    if (now - s_chk_ms >= 2000) {
      s_chk_ms = now;
      uint8_t p = 0; wifi_second_chan_t s = WIFI_SECOND_CHAN_NONE;
      if (esp_wifi_get_channel(&p, &s) == ESP_OK && p != g_channel && g_channel >= 1 && g_channel <= 13) {
        esp_wifi_set_channel(g_channel, WIFI_SECOND_CHAN_NONE);
        Serial.printf("[CH] guard: STA ch=%u -> %u (ESP-NOW運用chへ戻す)\n", p, g_channel);
      }
    }
  }
  // ★(b) 計測中は再接続（＝スキャン）を仕掛けない。待機に戻ってから再試行する。
  if (s_state == ST_ARMED || s_state == ST_GREEN || s_state == ST_RACE) return false;
  if (!s_wifi_began || (now - s_wifi_try_ms) >= WIFI_RETRY_MS) {
    s_wifi_began  = true;
    s_wifi_try_ms = now;
    WiFi.setAutoReconnect(false);               // ★(a) コア内部の無限 begin() ループを止める（再試行はここだけ）
    // DNSを明示（ルーターがDNS未配布でも解決を試せるように）。SSID接続前に設定。
    WiFi.config(IPAddress(0,0,0,0), IPAddress(0,0,0,0), IPAddress(0,0,0,0),
                IPAddress(8,8,8,8), IPAddress(1,1,1,1));
    // ★(c) 既知chがあれば原則そのchだけ（4回に1回は全ch）。未知なら全chスキャン。
    uint8_t ch = (s_wifi_known_ch && (s_wifi_tries % 4) != 3) ? s_wifi_known_ch : 0;
    s_wifi_tries++;
    WiFi.begin(g_ssid.c_str(), g_pass.c_str(), ch); // 非ブロッキング：完了待ちしない
    Serial.printf("[WIFI] begin (non-blocking) scan=%s\n", ch ? "known-ch" : "all-ch");
  }
  return false;                                 // 未接続。呼び出し元は今回は見送る
}
// ★20261031 P2：loop側の判定用（begin()を仕掛けない純粋な状態読み）。再接続はnetタスクのwifi_up()が担う。
static inline bool wifi_connected() { return WiFi.status() == WL_CONNECTED; }

// ---- 新レース作成（layout_id・任意でgreen_t_usを付ける）--------------------
//  green あり → F1式 / green なし → 走行式（docs/14 DA4）。
static uint32_t create_race(bool with_green, uint64_t green_us) {
  if (!wifi_up()) return 0;
  JsonDocument doc;
  doc["target_laps"] = s_target_laps;
  doc["layout_id"]   = s_layout_id;
  if (with_green) doc["green_t_us"] = green_us;
  String body; serializeJson(doc, body);

  JsonDocument res; bool parsed = false;
  httpj::request("/api/timing/races", body, &res, &parsed, httpj::T_CREATE_MS);
  return parsed ? (uint32_t)(res["race_id"] | 0) : 0;
}

// ★20261012：旧 signal_cmd()（GW→別筐体SG機へ ESP-NOW で赤/緑/リセットを指示）を廃止。
//   信号灯はGW自機の GPIO13/14 直結になったため、赤=sig_on_red() / 緑=sig_on_green() /
//   消灯=sig_all_off() を直接呼ぶ（上の「信号灯」ブロック参照）。RC からの CMD_SIGNAL/
//   CMD_RESET 受信（＝リモコンでスタート/リセット）は従来どおり on_signal_pressed/
//   on_reset_pressed で処理する（PT_COMMAND 受信ハンドラは不変）。

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
  s_start_shared_us = 0;                          // 20260831i：走行式の共通起点も新レースでクリア
  for (int i = 1; i <= 3; i++) { s_lane_cross[i] = 0; s_lane_active[i] = false;
                                 s_lane_first_us[i] = 0; s_lane_last_us[i] = 0;
                                 s_m_lastlane[i] = 0; s_m_clean[i] = true; }   // s_m_stepは20260831jで廃止（s_m_cleanは20260901d）
  disp::run_reset();                              // TFTのラップ表示もクリア（20260830）
  s_race_q2 = false; s_race_q1 = false;          // 24.4：A1/A2 per-raceフラグをclear
  s_race_q2_lane = 0; s_race_q1_lane = 0; s_race_q1_miss = 0; s_race_q2_miss = 0;   // A/B・レーンもclear（20260830c）
  memset(s_q2_lane_bits, 0, sizeof(s_q2_lane_bits)); s_alldown_src = 0xFF;          // ノード全断もclear（20260830d）
}

// tick_display を介さず SET 画面を即時に1回描くための前方宣言（赤押下の即応用）。
static void render_set_now();
// 緑点灯と同時に計測中(ON TRACK)画面を即時描画するための前方宣言（緑の即応用）。
static void render_ontrack_now();
// ---- スタートシーケンス制御 -----------------------------------------------
//  赤ボタン/CMD_SIGNAL共通の入口（docs/03「本体とリモコンで同じ処理」）。
static void on_signal_pressed() {
  if (s_state == ST_SPLASH) { s_state = ST_IDLE; sig_ready_red(); Serial.println("[SPLASH] skip -> READY"); return; }  // ★READY赤点灯
  // 20260831k：二度押しガード（経路非依存）。本体ISRデバウンス・リモコンnonce冪等に加え、
  //   受付関数自体でも直近受付から SIGNAL_GUARD_MS 以内の再入を弾く。状態遷移の隙間や
  //   別経路の重複でも「SETが二度発火／ARMEDが二重開始」しないための最後の砦。
  static uint32_t s_last_signal_ms = 0;
  uint32_t nowm = millis();
  if (s_last_signal_ms != 0 && (nowm - s_last_signal_ms) < SIGNAL_GUARD_MS) {
    Serial.println("[SEQ] ignored: double-press guard");
    return;
  }
  // 全画面エラー表示中（排他重複・全ビーム断・感知不能ラッチ等）は赤を全面禁止（20260831e）。
  //   本体・リモコン両方この関数を通るので一括で効く。解除は灰ボタンのみ。
  if (s_err_screen || s_nobeam_latched) {
    Serial.println("[SEQ] blocked: error screen active (press GRAY)");
    return;
  }
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
  // ★20261030 動作変更：赤点灯→ランダム→緑のシーケンスを廃止。
  //   READYは常時赤点灯。赤ボタンを押したら「即・緑」で測定開始（ARMED/ランダム時間なし）。
  //   緑LED/ブザーはGPIO/ledc直なのでcallback文脈でも安全。ON TRACK描画だけloopへ遅延（SPI回避）。
  begin_internal_race();                          // 内部rid採番＋参加レーン初期化
  s_green_t_us = tsync::now_gw_us();              // 即・緑＝green_t_us（F1式起点・全車共通）
  s_state      = ST_RACE;                          // 計測中へ（ARMED/GREENは経由しない）
  s_last_signal_ms = nowm;                        // 受付成立→二度押しガード起点
  sig_on_green();                                 // ★緑点灯（50ms後に赤消灯・2秒で緑自動消灯）
  buzzer_start(BUZZER_START_MS, BUZZER_F_START);  // ★スタート音「ブー」0.5秒
  s_green_now_req = true;                          // ★ON TRACK即描画をloopで
  Serial.printf("[SEQ] GREEN(即) rid=%u t=%llu\n", s_internal_rid, (unsigned long long)s_green_t_us);
}

// flush_spool / spool_has_data は本関数より後方で定義されるため前方宣言。
static bool spool_has_data();
static void flush_spool();

static void on_reset_pressed() {
  // 24.3：灰ボタン押下時、スプールに溜まっていれば送信（空なら何もしない）。
  //   ⚠ 本関数はESP-NOWコールバック(CMD_RESET)からも呼ばれるため、ここでHTTPSは
  //     しない。送信要求(s_need_flush)だけ立て、実送信は loop の tick_flush が行う(#20)。
  bool was_idle = (s_state == ST_IDLE);   // F-1：この灰が「走行終了」か「待機での確認」かを区別
  // 20260904 改修②：WiFi未接続なら PUSH挙動ごとスキップ（A案・ユーザー確定）。
  //   s_need_flush を立てず、tick_flush の再試行ループにも入れない。データはspoolに
  //   残し（消さない）、次回の灰押下時にWiFiが繋がっていれば送出する。旧仕様は
  //   要求を立てっぱなしで1.5秒毎に wifi_up() を空振りポーリングし続けていた。
  if (wifi_connected() && spool_has_data()) s_need_flush = true;   // ★P2：loopではbeginしない
  s_layout_req = true;          // ★P3：灰リセットでレイアウト(周回数等)を再取得（netタスクが実行）

  // 24.4⑤：待機画面に入った直後、3秒間だけA1/A2エラーを表示する窓を開く。
  s_post_err_until = millis() + 3000;
  // C1(24.14/F-1)：Lostは待機画面で一度見せてから灰で解除する。
  //   走行を止めた灰(was_idle=false)では消さない＝走行中に届いたLostが
  //   未表示のまま消えるのを防ぐ。待機で見えている状態の灰(was_idle=true)が「確認＝解除」。
  if (was_idle) { disp::g_status.lost = false; s_lost_src = 0; }

  s_state      = ST_IDLE;
  s_race_active = false;        // 現レースを閉じる（次のS/G通過は新しい内部レースになる）
  beam::flush();                // 20260904 改修④①：残留エッジ・張り付きタイマ・通過中ラッチを一掃。
                                //   居座り車/残響でRESET直後に内部レースが誤起動＋誤ビープするのを防ぐ。
  s_beam_bad   = false;         // Beam×も待機復帰でクリア
  s_nobeam_latched = false; s_nobeam_src = 0xFF; s_nobeam_lane = 0; s_nobeam_miss = 0;  // 感知不能ラッチ解除（20260831c）
  s_err_ack = true;             // 20260831g：どの全画面エラーでも灰で強制解除→通常画面へ抜ける（強制運用）
  s_sg_beeped  = false;         // リセットで「次の1回」を再び鳴らせるようにする（新しい待機セッション）
  s_green_t_us = 0;
  s_start_shared_us = 0;        // 20260831i：走行式の共通起点も灰リセットでクリア
  sig_ready_red();              // ★20261030：灰リセット→READYは常時赤点灯（旧sig_all_off）
  s_flying_until_ms = 0; s_flying_seen = false;   // ★フライング優先/記録フラグも待機復帰でクリア
  buzzer_tone(0); s_buzz_off_ms = 0;   // ★スタート音/エラー音を即停止
  Serial.println("[SEQ] RESET -> IDLE (flush queued if data)");
}

// ARMED経過で緑へ。loopから呼ぶ。緑時刻はRAM保持のみ（POSTしない。flush時に同梱）。
static void tick_sequence() {
  // ★20261012：旧CMD_RED再送ループ（ESP-NOWでSGへ）は廃止。赤はsig_on_red()でGPIO直点灯済み。
  if (s_state == ST_ARMED && (millis() - s_armed_ms) >= s_red_dur_ms) {
    s_green_t_us = tsync::now_gw_us();          // 緑を出した瞬間＝green_t_us（F1式起点）
    s_state = ST_GREEN;
    sig_on_green();                             // ★GW自機の緑灯ON＋50ms後に赤消灯＋2秒で緑自動消灯（DC30/DC32）
    buzzer_start(BUZZER_START_MS, BUZZER_F_START);  // ★スタート音「ブー」0.5秒（外部基盤12V・他励SPT15・DC27/DC30）
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
// ★20261031 P1/P2：JOINのHTTPS転送は netタスクで行う。受信側(loop)はキューに積むだけ。
//   旧：ESP-NOW受信コールバック(WiFiタスク)内で直接HTTPS → 自分が止めたWiFiタスクの応答を
//       自分で待つ形になり最大0.8〜2.3秒ESP-NOW受信が全停止（RC/SEの取りこぼし＝反応悪化の一因）。
static constexpr int JOINQ_N = 4;
static proto::JoinBody   s_joinq[JOINQ_N];
static volatile uint8_t  s_joinq_head = 0, s_joinq_tail = 0;   // head=書き手(loop) tail=読み手(net)
static void forward_join(const proto::JoinBody& jb) {           // loop文脈：キューへ（満杯なら捨てる＝1秒後に再JOINが来る）
  uint8_t nh = (uint8_t)((s_joinq_head + 1) % JOINQ_N);
  if (nh == s_joinq_tail) return;
  s_joinq[s_joinq_head] = jb;
  __sync_synchronize();
  s_joinq_head = nh;
}
static void forward_join_now(const proto::JoinBody& jb) {       // netタスク文脈：実HTTPS
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

  JsonDocument res; bool parsed = false;
  httpj::request("/api/timing/join", body, &res, &parsed);
  if (parsed) {
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

// ---- EVENTスプール ---------------------------------------------------------
// スプール上限（超過分は古い順に破棄）。
static constexpr int SPOOL_MAX = 200;
// ★20261031 P4：スプール件数はRAMカウンタで持つ（旧：毎回ファイル全行読みで数えていた＝
//   通過1件ごと・3秒ごとのロスター表示ごとに最大200行/30KBを読んでいた無駄を撤廃）。
//   append/trim/rewrite の各書き手が正確に更新する。起動時はファイルを空にするので0から。
static volatile int s_spool_n = 0;
// ★20261031 P2：スプールは loop(追記) と netタスク(送信・書き戻し) の2タスクが触るため mutex で保護。
//   ⚠ HTTPS中は必ず解放する（計測中の追記を待たせない）。
static SemaphoreHandle_t s_spool_mtx = nullptr;
static inline void spool_lock()   { if (s_spool_mtx) xSemaphoreTake(s_spool_mtx, portMAX_DELAY); }
static inline void spool_unlock() { if (s_spool_mtx) xSemaphoreGive(s_spool_mtx); }
// 先頭（最古）から (count-keep) 行を削除して keep 行に収める。⚠呼び出し側がlock済みであること。
static void spool_trim_to_locked(int keep) {
  int total = s_spool_n;
  if (total <= keep) return;
  int drop = total - keep;
  File in = LittleFS.open(SPOOL, FILE_READ);
  if (!in) return;
  const char* TMP2 = "/spool_trim.jsonl";
  File out = LittleFS.open(TMP2, FILE_WRITE);
  if (!out) { in.close(); return; }
  int idx = 0, kept = 0;
  while (in.available()) {
    String line = in.readStringUntil('\n');
    if (line.length() == 0) continue;
    if (idx >= drop) { out.print(line); out.print('\n'); kept++; }   // 古いdrop行はスキップ
    idx++;
  }
  in.close(); out.close();
  LittleFS.remove(SPOOL);
  LittleFS.rename(TMP2, SPOOL);
  s_spool_n = kept;
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
  spool_lock();
  // 上限：追記前に (SPOOL_MAX-1) 行へ詰めておく（append後にちょうど上限）。RAM件数で判定＝上限到達時以外はI/Oなし。
  spool_trim_to_locked(SPOOL_MAX - 1);

  File f = LittleFS.open(SPOOL, FILE_APPEND);
  if (!f) { spool_unlock(); return; }
  JsonDocument doc;
  doc["device_id"]   = e.src;      // device_id と src は同じ（送信元node_id）
  doc["src"]         = e.src;
  doc["src_boot_id"] = e.src_boot;
  doc["seq"]         = e.seq;
  doc["lane"]        = e.lane;
  // 20260904 改修⑥：A欠け(miss=1)は e.t_us(=t_a_us)=0 のまま送るとサーバーの ORDER BY t_us で
  //   全通過の先頭にソートされ、周回・セクター構築を壊していた（TFTはB時刻に代用するので
  //   表示は正常＝アプリ側だけ壊れる厄介系）。対策：A欠け時は t_us をB時刻へ寄せ、
  //   t_us_b は両ビーム取得時のみ載せる（片ビームは速度算出不可＝サーバー側で「—」表示）。
  uint64_t t_use = e.t_us ? e.t_us : e.t_us_b;       // A欠けはBで代用（順序を正す）
  doc["t_us"]        = t_use;
  if (e.t_us && e.t_us_b) doc["t_us_b"] = e.t_us_b;  // 両ビーム揃ったときだけB時刻を送る
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
  s_spool_n = s_spool_n + 1;
  spool_unlock();
}

// ---- 受信ハンドラ ----------------------------------------------------------
// ★20261031 P1：ESP-NOW受信コールバック（WiFiタスク文脈）では「在席記録」と「時刻同期の即返信」
//   だけ行い、それ以外はリングバッファに積んで loop（rx_pump）で処理する。
//   旧：コールバック内で spool_append(LittleFS書込み)・forward_join(HTTPS)・on_reset_pressed 等を
//       直接実行 → WiFiタスクを塞ぎ、ESP-NOW受信が最大数秒止まる／フラッシュ書込みをWiFiタスクで行う危険。
//   ⚠ SYNC_REQ は受信瞬間の打刻が精度に効くため、従来どおりコールバック内で即返す。
struct RxSlot { proto::PktHeader h; int len; uint8_t mac[6]; uint8_t body[232]; };
static constexpr int RXQ_N = 16;
static RxSlot            s_rxq[RXQ_N];
static volatile uint8_t  s_rxq_head = 0, s_rxq_tail = 0;   // head=書き手(コールバック) tail=読み手(loop)
static volatile uint16_t s_rxq_drop = 0;                    // 満杯で捨てた数（ログ用）
static void on_recv_main(const proto::PktHeader& h, const uint8_t* body, int body_len, const uint8_t* mac);
static void on_recv(const proto::PktHeader& h, const uint8_t* body,
                    int body_len, const uint8_t* mac) {      // ← コールバック文脈（短く・I/Oなし）
  mark_seen(h.src);                    // 在席記録（排他検知の土台・全種別で更新）
  if (h.type == proto::PT_SYNC_REQ) {
    if (body_len >= (int)sizeof(proto::SyncBody)) {
      proto::SyncBody b; memcpy(&b, body, sizeof(b));
      tsync::gw_reply(h, b);           // 受信瞬間に打刻して即返す（精度維持）
    }
    return;
  }
  // ★ACKはここで即返す（変更前と同じタイミング）。RCはACK待ち70msなのでloop経由だと再送が増える。
  if (h.type == proto::PT_EVENT   && body_len >= (int)sizeof(proto::EventBody))   mesh::send_ack(proto::PT_EVENT_ACK,   h.src, h.seq);
  if (h.type == proto::PT_COMMAND && body_len >= (int)sizeof(proto::CommandBody)) mesh::send_ack(proto::PT_COMMAND_ACK, h.src, h.seq);
  uint8_t nh = (uint8_t)((s_rxq_head + 1) % RXQ_N);
  if (nh == s_rxq_tail) { s_rxq_drop = s_rxq_drop + 1; return; }   // 満杯（loop停止中のみ起こりうる）
  RxSlot& sl = s_rxq[s_rxq_head];
  sl.h = h;
  sl.len = body_len > (int)sizeof(sl.body) ? (int)sizeof(sl.body) : (body_len < 0 ? 0 : body_len);
  if (sl.len) memcpy(sl.body, body, sl.len);
  if (mac) memcpy(sl.mac, mac, 6); else memset(sl.mac, 0, 6);
  __sync_synchronize();            // ★R3：スロット書込みを head 更新より先に可視化（core0→core1）
  s_rxq_head = nh;
}
// loop先頭で呼ぶ：溜まった受信を安全な文脈で処理（1周あたり最大8件）。
static void rx_pump() {
  int n = 0;
  while (s_rxq_tail != s_rxq_head && n++ < 8) {
    __sync_synchronize();
    RxSlot& sl = s_rxq[s_rxq_tail];
    on_recv_main(sl.h, sl.body, sl.len, sl.mac);
    s_rxq_tail = (uint8_t)((s_rxq_tail + 1) % RXQ_N);
  }
  static uint16_t last_drop = 0;
  if (s_rxq_drop != last_drop) { last_drop = s_rxq_drop; Serial.printf("[RXQ] overflow drop=%u\n", last_drop); }
}
static void on_recv_main(const proto::PktHeader& h, const uint8_t* body,
                         int body_len, const uint8_t*) {    // ← loop文脈（I/O可）
  switch (h.type) {
    case proto::PT_EVENT: {
      if (body_len >= (int)sizeof(proto::EventBody)) {
        proto::EventBody b; memcpy(&b, body, sizeof(b));
        // ACKは受信コールバック(on_recv)で送信済み（ingest前ACK・S5）
        // ★20261031 A1：SEの再送（ACK擦れ違い）は seq が変わるため冪等キーでは重複を消せない。
        //   同一 (src, boot_id, lane, t_us) を直近16件で照合し、同じ通過なら記録しない（ACKは済）。
        {
          struct EvKey { uint8_t src, lane; uint32_t boot; uint64_t t; };
          static EvKey s_ev_recent[16]; static uint8_t s_ev_pos = 0;
          bool dup = false;
          for (int i = 0; i < 16; i++)
            if (s_ev_recent[i].t == b.t_us && s_ev_recent[i].src == h.src &&
                s_ev_recent[i].boot == h.boot_id && s_ev_recent[i].lane == b.lane && b.t_us != 0) { dup = true; break; }
          if (dup) { Serial.printf("[EV] dup src=SQ%u lane=%u t=%llu (再送・記録しない)\n", h.src, b.lane, (unsigned long long)b.t_us); break; }
          s_ev_recent[s_ev_pos] = { h.src, b.lane, h.boot_id, b.t_us }; s_ev_pos = (uint8_t)((s_ev_pos + 1) & 15);
        }
        RawEvent e{ h.src, h.boot_id, h.seq, b.lane, b.quality, b.t_us, b.t_us_b };
        spool_append(e);
        note_quality(b.quality, h.src, b.lane, b.miss);   // A1/A2/Sync集約（27章・A/Bまで）
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
        {
          proto::GwBeatBody gb = {};   // 20260831k：即応ビーコンにも計測中フラグを付帯
          gb.racing = (s_state == ST_ARMED || s_state == ST_GREEN || s_state == ST_RACE) ? 1 : 0;
          mesh::send(proto::PT_HEARTBEAT, proto::NODE_BROADCAST, &gb, sizeof(gb));
        }
        forward_join(jb);
      }
    } break;

    case proto::PT_COMMAND: {
      // リモコンからのCOMMAND。本体ボタンと同じ処理を呼ぶ（docs/03）。
      if (body_len >= (int)sizeof(proto::CommandBody)) {
        proto::CommandBody c; memcpy(&c, body, sizeof(c));
        // ★冪等化：同一押下(src,nonce=c.arg)の再送はアクションを実行しない。
        //   別ch再送・ACK駆動再送・中継の遅延重複でも SIGNAL が二度発火しない（絶対NG対策）。
        bool dup = cmd_nonce_seen(h.src, c.arg);
        Serial.printf("[CMD] from RC%u code=%s nonce=%08lx%s%s\n", h.src,
                      c.code == proto::CMD_SIGNAL ? "SIGNAL" : c.code == proto::CMD_RESET ? "RESET" : "?",
                      (unsigned long)c.arg, dup ? " (重複・無視)" : "", (h.flags & proto::FLAG_RELAYED) ? " via relay" : "");   // ★20261102
        if (!dup) {
          if (c.code == proto::CMD_SIGNAL) on_signal_pressed();
          else if (c.code == proto::CMD_RESET) on_reset_pressed();
        }
        // ACK（届いた確認・docs/12 COMMAND_ACK）は受信コールバック(on_recv)で送信済み
      }
    } break;

    case proto::PT_LOST_NOTICE: {
      // C1(24.14/案A・27章)：SQがEVENTをあきらめた通知。Lost×＋「セクター通信不良」を
      //   表示する（灰リセットまで出しっぱなし）。発生SQは h.src。
      disp::g_status.lost = true;
      s_lost_src = h.src;
      Serial.printf("[LOSTNOTE] src=SQ%u\n", h.src);
    } break;

    case proto::PT_HEARTBEAT: {
      // ビーム健全性の付帯ボディがあれば取り込む（旧ファームはボディ無し・20260831）
      if (body_len >= (int)sizeof(proto::BeamStatBody) && h.src <= proto::NODE_ID_MAX) {
        proto::BeamStatBody b; memcpy(&b, body, sizeof(b));
        s_node_stuck_bits[h.src] = b.stuck_bits;
        memcpy(s_node_stuck_miss[h.src], b.miss, sizeof(b.miss));
        s_node_stuck_ms[h.src] = millis();
      }
    } break;
    default: break;
  }
}

// ---- スプールに送るべき行があるか（空/存在しないなら false）----------------
//  24.3：灰ボタンは「溜まっていれば送信・空なら何もしない」。その判定に使う。
static bool spool_has_data() { return s_spool_n > 0; }   // ★P4：RAMカウンタ（ファイルI/Oなし）

// 未送信件数＝スプールの有効行数（TFTバーの Send:N 表示用・2026-08-24）。
static int spool_count() { return s_spool_n; }           // ★P4：RAMカウンタ（ファイルI/Oなし）

// ---- 指定 rid の行を先頭から最大 limit 件だけ除去し、残りを書き戻す（1-c）-----
//  flush で送れた group_rid の行（最大100件）をスプールから消す。別 rid の行と、
//  100件上限で送りきれなかった同 rid の残りは持ち越す。照合は冪等キーではなく
//  「rid一致かつ先頭から limit 件」で足りる（送信は先頭から詰めているため順序一致）。
//  ⚠呼び出し側がlock済みであること。
static void rewrite_spool_drop_group_locked(uint32_t rid, int limit) {
  File in = LittleFS.open(SPOOL, FILE_READ);
  if (!in) return;
  const char* TMP = "/spool.tmp";
  File out = LittleFS.open(TMP, FILE_WRITE);
  if (!out) { in.close(); return; }
  int removed = 0, kept = 0;
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
    out.print(t); out.print('\n'); kept++;         // 残りを持ち越し
  }
  in.close();
  out.close();
  LittleFS.remove(SPOOL);
  LittleFS.rename(TMP, SPOOL);
  s_spool_n = kept;
}

// ---- スプールPOST（200まで消さない・S8／内部ridグループ→サーバーrace_id翻訳）---
//  20260821：各行は受信時の“内部rid”を持つ。1回のflushでは「先頭有効行のグループ
//  (=同じ内部rid)」だけを最大100件送る。送信先サーバーrace_idは：
//    ・既に作成済み(s_ridmap)ならそれを再利用（100件超の分割2回目以降）。
//    ・未作成なら“今ここで” create_race（グループ内 grn の最大値＝緑があればF1式）で採番し、
//      s_ridmap に記録。これが「作成POSTも終了時に集約」の実体。
//  ★20261031 P2：netタスク(core0)から呼ぶ。loopは止まらない。スプール読み書きはlock、HTTPS中は解放。
//    （旧：loop文脈でTLS2回＝2〜6秒loopが止まり「灰→READYが出ない」原因。ファイル全読み3回も1回に集約）
static void flush_spool() {
  if (!wifi_up()) return;  // WiFi接続進行中は送らない（封じない・s_need_flush維持で再試行・2026-08-24e）

  // ★1回の走査で「先頭グループのrid」「そのgrn最大」「送るevents（最大100件）」を同時に集める。
  uint32_t group_rid = 0;
  bool     group_found = false;
  uint64_t group_green = 0;                        // グループ内 grn の最大値（0=走行式）
  JsonDocument out;
  JsonArray arr = out["events"].to<JsonArray>();
  int n = 0;
  bool capped = false;

  spool_lock();
  if (!LittleFS.exists(SPOOL)) { spool_unlock(); return; }
  File f = LittleFS.open(SPOOL, FILE_READ);
  if (!f) { spool_unlock(); return; }
  while (f.available()) {
    String line = f.readStringUntil('\n'); line.trim();
    if (line.length() == 0) continue;
    JsonDocument ev;
    if (deserializeJson(ev, line) != DeserializationError::Ok) continue; // 壊れた行は捨てる
    uint32_t r = ev["rid"] | 0;
    if (!group_found) { group_rid = r; group_found = true; }
    if (r != group_rid) continue;                           // 別グループは今回送らない
    uint64_t g = ev["grn"] | (uint64_t)0;
    if (g > group_green) group_green = g;
    if (capped) continue;                                   // 100件超は grn だけ見て積まない
    ev.remove("rid");                                       // rid/grn は内部用。送信payloadから除く
    ev.remove("grn");
    arr.add(ev);
    n++;
    if (n >= 100) capped = true;                            // 1回のPOSTは100件まで
  }
  f.close();
  spool_unlock();
  if (!group_found || n == 0) return;   // 有効行なし

  // 内部rid → サーバーrace_id を確定。未作成ならグループの緑(最大)で create_race（HTTPS・lock外）。
  uint32_t server_id = ridmap_lookup(group_rid);
  if (server_id == 0) {
    server_id = create_race(/*with_green=*/group_green != 0, group_green);  // ここで初めてサーバー作成
    if (server_id == 0) {                            // WiFi/TLS失敗：送らずデータは残す
      Serial.println("[POST] create_race 失敗（データ保持・再試行）");
      return;
    }
    ridmap_store(group_rid, server_id);
    Serial.printf("[POST] rid(int)=%u -> race=%u%s\n",
                  group_rid, server_id, group_green ? " (F1)" : " (run)");
  }
  String payload; serializeJson(out, payload);

  int code = httpj::request("/api/timing/races/" + String(server_id) + "/events", payload, nullptr, nullptr, httpj::T_EVENTS_MS);

  if (code != 200) {
    Serial.printf("[POST] events 失敗 code=%d\n", code);
    return;
  }

  // 送信成功：送った group_rid の行を（最大100件ぶん）スプールから除去。
  spool_lock();
  rewrite_spool_drop_group_locked(group_rid, n);
  spool_unlock();
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

  Serial.printf("[SEQ] 全員完走 rid=%u -> FINISH表示%s\n", s_internal_rid,
                wifi_connected() ? "＋自動送信" : "（WiFi未接続・送信は次の灰で）");
  s_race_active   = false;        // レースを閉じる
  // 20260904 改修②：WiFi未接続なら自動送信も立てない（FINISH中の空振りポーリング回避）。
  //   データはspoolに保持され、FINISH→灰でIDLE復帰時に on_reset_pressed が
  //   WiFi接続済みなら送出する（A案・全契機で一貫）。
  s_need_flush    = wifi_connected();   // 送信はnetタスクのtick_flushが実行（★P2）
  s_state         = ST_FINISH;    // 計測終了(FINISH)画面へ（灰リセットで待機に戻る・20260830）
  buzzer_start(GOAL_MS, GOAL_FREQ);   // ★ゴール(完走)音＝800Hz・500ms 単音（20261030）
  s_sg_beeped     = false;
  s_green_t_us    = 0;
  s_start_shared_us = 0;          // 20260831i：合計(total_ms)は各マシン完走時に保存済みなので安全
  s_post_err_until = millis() + 3000;   // 24.4⑤：A1/A2を3秒だけ表示
  sig_all_off();                        // ★信号灯消灯（念のため・GPIO直結）
}

// ---- 送信要求の実行（loop文脈・HTTPSはここだけ・#20）------------------------
//  s_need_flush が立っている間、throttleしつつ flush_spool を回す。flush_spool は
//  1回で1グループ(最大100件)送るので、スプールが空になるまで複数tickで送り切る。
//  送信失敗時は要求を残したまま再試行する（データはスプールに保持され消えない）。
//  ★20261031 P2：loopからは呼ばない。netタスク(net_task)が呼ぶ。
static void tick_flush() {
  if (!s_need_flush) return;
  // ★20261102：計測中（ARMED/GREEN/RACE）は送信を始めない（要求は保持＝FINISH/待機で再開）。
  //   送信自体はnetタスク(core0)なのでloopは止まらないが、flush_spool はスプールのファイル全読み・
  //   送信済み行の書き戻し（フラッシュ消去/書込み）を spool_lock 下で行う。計測中に走ると
  //   loop側の spool_append（通過記録）がロック待ちで止まり、TFT/ESP-NOW処理が遅れる。
  //   「灰→すぐ赤」で前レースの送信が計測にかぶるケースを切る（HTTPS進行中の1グループは完了まで続く）。
  if (s_state == ST_ARMED || s_state == ST_GREEN || s_state == ST_RACE) return;
  static uint32_t last = 0;
  if (millis() - last < 300) return;       // ★0.3秒間隔（接続使い回しで握手が無くなったので短縮。旧1.5秒）
  last = millis();
  if (!spool_has_data()) { s_need_flush = false; return; }
  flush_spool();                            // 1グループ送信（失敗ならデータ保持・再試行）
  if (!spool_has_data()) s_need_flush = false;   // 送り切ったら要求解除
}

// ---- レイアウト取得（GW向け軽量版 /for_gw・docs/19.16）--------------------
//  サーバーから target_laps / lap_length_m / nodes 等を取得してログ表示する。
//  戻り値：取得成功=true。呼び出し間隔は tick_fetch_layout がバックオフ管理（#14）。
static bool fetch_layout() {
  if (!wifi_up()) return false;
  JsonDocument res; bool ok = false;
  int code = httpj::request("/api/timing/layouts/" + String((int)s_layout_id) + "/for_gw", String(), &res, &ok);
  if (ok) {
    int laps  = res["target_laps"] | 0;
    int nodes = res["node_count"]  | 0;
    s_node_need = (nodes >= 0 && nodes <= 6) ? nodes : 0;   // 使用SQ数（0..6）。異常値は0=未取得扱い
    int lc    = res["lc_count"]    | 0;
    float len = res["lap_length_m"] | 0.0f;
    if (laps >= 1 && laps <= 99) s_target_laps = laps;     // #8-a：取得できた周回数を反映
    Serial.printf("[LAYOUT] id=%u laps=%d nodes=%d lc=%d len=%.1fm\n", s_layout_id, laps, nodes, lc, len);
  }
  if (!ok) Serial.printf("[LAYOUT] fetch NG code=%d\n", code);
  return ok;
}

// ---- アクティブレースのレーン別レーサー名取得（ロスターREADY用・20261109）------
//  GET /api/timing/current_race_for_gw
//   → {"lanes":[{"lane":1,"kana":"ユウミ"},{"lane":2,"kana":"グレタロウ"},...]}
//  kana はサーバーで正規化済み（ひらがな→カタカナ／半角英数）。空配列=アクティブ無し。
//  成功時のみ反映。lanes有り→race_active=true（READYはロスター表示）、空→false（通常READY）。
static bool fetch_race() {
  if (!wifi_up()) return false;
  JsonDocument res; bool ok = false;
  int code = httpj::request("/api/timing/current_race_for_gw", String(), &res, &ok);
  if (ok) {
    for (int i = 0; i < 5; i++) disp::g_status.lane_name[i][0] = '\0';
    int cnt = 0;
    for (JsonObject ln : res["lanes"].as<JsonArray>()) {
      int l = ln["lane"] | 0;
      const char* nm = ln["kana"] | "";
      if (l >= 1 && l <= 5) { snprintf(disp::g_status.lane_name[l - 1], sizeof(disp::g_status.lane_name[0]), "%s", nm); cnt++; }
    }
    disp::g_status.race_active = (cnt > 0);
    Serial.printf("[RACE] active=%d lanes=%d\n", disp::g_status.race_active ? 1 : 0, cnt);
  } else {
    Serial.printf("[RACE] fetch NG code=%d\n", code);
  }
  return ok;
}

// ---- fetch_layout の契機管理 ------------------------------------------------
//  ★20261031 P3：netタスクから呼ぶ（loopは止まらない）。周期は変更前と同じ 成功60s/失敗30s を維持し、
//    さらに灰リセット時は即時再取得（アプリで周回数を変えた直後に反映しやすく）。
//    旧：loop文脈で60秒ごとに0.8〜2.3秒止まり、その瞬間の赤/RCが効かなかった。
static void tick_fetch_layout() {
  if (s_state != ST_IDLE) return;   // SET〜計測中は取得しない（変更前と同じ：周回数をレース中に変えない）
  if (!wifi_connected()) return;                                // WiFi未接続は“失敗”に数えない（接続した瞬間に即取得）
  static uint32_t last = 0;
  uint32_t interval = s_layout_ok ? LAYOUT_OK_MS : LAYOUT_NG_MS;   // 成功60s/失敗30s（変更前と同じ周期）
  bool due = (last == 0) || ((millis() - last) >= interval);
  if (!s_layout_req && !due) return;                            // 灰リセット要求 or 周期到来で取得
  last = millis();
  s_layout_ok = fetch_layout();
  if (s_layout_ok) s_layout_req = false;
}

// ---- アクティブレース取得の契機（READY中のみ約5秒間隔・20261109）-------------
//  名前はロスターREADY（ST_IDLE）でのみ使うので、計測中(ARMED/GREEN/RACE)は取得しない。
//  アクティブ化やレーン変更を数秒で反映するため、layoutより短い周期にする。
static constexpr uint32_t RACE_POLL_MS = 5000;
static void tick_fetch_race() {
  if (s_state != ST_IDLE) return;        // READY(待機)でのみ取得
  if (!wifi_connected()) return;
  static uint32_t last = 0;
  if (last != 0 && (millis() - last) < RACE_POLL_MS) return;
  last = millis();
  fetch_race();
}

// ---- netタスク（core0）：HTTPSは全部ここ。loop(core1)は一切止まらない ----------
//  ★20261031 P2/P3：flush（送信）・layout取得・JOIN転送・WiFi再接続の面倒を見る。
//   ・s_need_flush / s_layout_req / JOINキュー は loop が立て、ここが消化する。
//   ・スプールのファイルI/Oは spool_lock で排他、HTTPS中は解放（計測中の追記を待たせない）。
//   ・wifi_up() を定期的に呼ぶことで切断時の再begin(15秒間隔)もここで継続する。
static void net_task(void*) {
  for (;;) {
    wifi_up();                                   // 接続維持（未接続なら15秒間隔でbegin）
    tick_flush();                                // 送信要求があれば1グループ送る
    tick_fetch_layout();                         // 60秒周期＋灰リセット即時
    tick_fetch_race();                           // ★アクティブレース(レーサー名)をREADY中5秒周期で取得（20261109）
    while (s_joinq_tail != s_joinq_head) {       // JOIN転送（レート制限はforward_join_now内）
      proto::JoinBody jb = s_joinq[s_joinq_tail];
      s_joinq_tail = (uint8_t)((s_joinq_tail + 1) % JOINQ_N);
      forward_join_now(jb);
    }
    gwlog::persist(!(s_state == ST_ARMED || s_state == ST_GREEN || s_state == ST_RACE));   // ★20261102 ログ保存（計測中はしない）
    vTaskDelay(pdMS_TO_TICKS(50));
  }
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
  if (s_red_evt) { s_red_evt = false; Serial.println("[BTN] red (本体)"); on_signal_pressed(); }

  // 灰：ISRラッチで単押し即RESET（長押し機能は廃止・2026-08-24）。
  if (s_gray_evt) { s_gray_evt = false; Serial.println("[BTN] gray (本体)"); on_reset_pressed(); }
}

// ---- GW自身の在席ビーコン（相手GWが「GW2台」を検知できるように）------------
//  GWはRC/SGのように定期ハートビートを出していなかったため、相手GWから見えない。
//  ここで自分の在席をブロードキャストし、GW6⇔GW7 が相互に重複検知できるようにする。
static void tick_gw_presence() {
  static uint32_t last = 0;
  if (millis() - last < GW_BEACON_MS) return;
  last = millis();
  // 20260831k：ビーコンに計測中フラグを付帯（SEの「計測中はch走査禁止」用・後方互換）。
  proto::GwBeatBody gb = {};
  gb.racing = (s_state == ST_ARMED || s_state == ST_GREEN || s_state == ST_RACE) ? 1 : 0;
  mesh::send(proto::PT_HEARTBEAT, proto::NODE_BROADCAST, &gb, sizeof(gb));
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
    if (p.isKey("slug"))  { g_slug  = p.getString("slug",  g_slug);  from_nvs = true; }
    if (p.isKey("ch"))    { uint8_t c = p.getUChar("ch", g_channel);
                            if (c >= 1 && c <= 13) { g_channel = c; from_nvs = true; } }
    p.end();
  }
  // 空を焼いた時の保険（ssid/host/ipは空なら既定へ。pass/tokenは空を許容）。
  if (g_ssid.isEmpty()) g_ssid = WIFI_SSID;
  if (g_host.isEmpty()) g_host = DEF_SERVER_HOST;
  if (g_ip.isEmpty())   g_ip   = DEF_SERVER_IP;
  Serial.printf("[CFG] src=%s ch=%d ssid=\"%s\" host=%s ip=%s slug=\"%s\" token=%dB\n",
                from_nvs ? "NVS" : "default", (int)g_channel,
                g_ssid.c_str(), g_host.c_str(), g_ip.c_str(), g_slug.c_str(), (int)g_token.length());
}

void setup() {
  Serial.begin(115200);
  delay(300);
  gwlog::set_state_fn(gwlog_state_char);
  gwlog::log_boot("GW");
  Serial.printf("\n=== M4LAPS Gateway GW%d (VE) ===\n", NODE_ID);
  load_config();   // NVS "m4cfg" を読む（無ければ secrets.h 既定）。以後は g_* を使用
  pinMode(PIN_BTN_RED,  INPUT_PULLUP);
  pinMode(PIN_BTN_GRAY, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_BTN_RED),  isr_btn_red,  FALLING);  // 押下取りこぼし防止（2026-08-24e）
  attachInterrupt(digitalPinToInterrupt(PIN_BTN_GRAY), isr_btn_gray, FALLING);
  // ブザー（他励式SPT15＋外部基盤12V直・ledc駆動）。周波数を与えて鳴らす（自励式のdigitalWriteから変更・20261012）。
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(PIN_BUZZER, BUZZER_F_BEEP, BUZZER_LEDC_BITS);  // pin指定（beam.hと同方式）
  ledcWriteTone(PIN_BUZZER, 0);                             // 起動直後は無音
#else
  ledcSetup(BUZZER_LEDC_CH, BUZZER_F_BEEP, BUZZER_LEDC_BITS);
  ledcAttachPin(PIN_BUZZER, BUZZER_LEDC_CH);                // ch指定（38kHz IRはbeam.hのch0・こちらはch1）
  ledcWriteTone(BUZZER_LEDC_CH, 0);                         // 起動直後は無音
#endif
  // 信号灯（外部基盤IRLU3410→12VテープLED・GPIO13赤/GPIO14緑・docs/40）。起動直後は必ず消灯（テープ薄点灯防止）。
  pinMode(PIN_SIG_RED,   OUTPUT);
  pinMode(PIN_SIG_GREEN, OUTPUT);
  sig_all_off();
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
  buzzer_jingle_start();   // ★起動音（パチンコ風・約0.85秒）をスプラッシュと同時に開始（20261109・a/b共通）
  disp::g_status.gw_id = NODE_ID;
  disp::g_status.ch    = g_channel;
  // ★20261031 P2：HTTPS・WiFi維持は netタスク(core0)へ。loop(core1)はTLSで止まらない。
  s_spool_mtx = xSemaphoreCreateMutex();
  if (xTaskCreatePinnedToCore(net_task, "net", 16384, nullptr, 1, nullptr, 0) != pdPASS)
    Serial.println("[NET] task create FAILED（送信・レイアウト取得が動きません）");
  Serial.println("稼働開始。赤=スタート(READY時のみ) / 灰=RESET(即時)。");
}

// 経過秒フィールド（案B）の設定。
//  s_show_ontrack：いまフルフレーム層にON TRACK画面が出ているか（tick_displayが4fpsで更新）。
//   これがtrueのときだけ経過秒フィールドを上に重ねる（エラー面の上に重ねない保険）。
static bool s_show_ontrack = false;
// 20260901f：フル画面再描画(4fps)直後に右上カウンタを1回“強制repaint”して、
//  250ms周期のヒッチ（カウントが一瞬止まって見える＝カクつき）を潰すためのワンショット。
static bool s_time_force   = false;
static constexpr uint32_t TIME_FIELD_MS = 16;   // 約60fps（より滑らか・2026-08-24）

// 経過秒フィールドだけを高頻度で部分更新する（案B）。loop()から毎周回呼ぶ。
//  tick_displayの250msゲートに縛られず、ON TRACK表示中のみ経過秒を機敏に動かす。
//  ⚠打刻はISR/tsync由来なので、この描画頻度は計測精度に一切影響しない。
static void tick_time_field() {
  if (!s_show_ontrack) return;                        // ON TRACK表示中のみ
  static uint32_t last_t = 0;
  if (!s_time_force && (millis() - last_t < TIME_FIELD_MS)) return;  // 通常は間引き／フル再描画直後は強制
  last_t = millis();
  uint32_t elapsed = race_start_us() ? (uint32_t)((tsync::now_gw_us() - race_start_us()) / 1000ULL) : 0;  // 20260831i：緑or共通起点
  // 20260901i：表示値(0.1s)が変わった時だけ転送する。従来は約60回/秒の全push＝同じ絵を6回連続で
  //  転送していた（SPI浪費）。値の切替(100ms)に同期した10回/秒へ＝SPI 1/6・歩進も均一で安定。
  //  フル再描画直後の強制repaint(s_time_force)は値が同じでも必ず通す。
  static uint32_t s_shown_ds = 0xFFFFFFFFu;
  uint32_t ds = elapsed / 100u;               // 0.1秒単位の表示値
  if (!s_time_force && ds == s_shown_ds) return;
  s_shown_ds = ds;
  s_time_force = false;
  disp::draw_time_field(elapsed, /*hundredths=*/false);  // コンマ1秒（右上）
}

// ---- SET画面の即時描画（赤押下の即応・tick_displayの250msゲートを迂回）------
//  on_signal_pressed から呼ぶ。ARMEDに入った瞬間、状態機械の次周(最大250ms後)を
//  待たずに SET 全面を1回転送する。以後の維持描画は tick_display(ST_ARMED)が担う。
static void frame_sig_invalidate();   // ★20261103 前方宣言（定義は静止画面シグネチャの所）
static void render_set_now() {
  if (!disp::ready()) return;
  disp::draw_set(false);        // blink未使用
  s_show_ontrack = false;       // 経過秒フィールドを重ねない
  frame_sig_invalidate();       // ★20261103
  disp::commit();               // ステータスバーを重ねて即転送
}

// ---- 計測中画面の即時描画（緑点灯の即応・tick_displayの250msゲートを迂回）----
//  tick_sequence が緑(CMD_GREEN)を出した瞬間に呼ぶ。状態機械の次周(最大250ms後)を
//  待たずに ON TRACK 全面を1回転送し、カウント表示をその場で始める。以後の維持描画は
//  tick_display(ST_RACE)＋経過秒フィールド(約30fps)が担う。
//  ⚠公式タイムは green_t_us(µs)基準。この描画タイミングは計測精度に影響しない。
static void render_ontrack_now() {
  if (!disp::ready()) return;
  uint32_t elapsed = race_start_us() ? (uint32_t)((tsync::now_gw_us() - race_start_us()) / 1000ULL) : 0;  // 20260831i：緑or共通起点
  disp::draw_ontrack(elapsed, /*blink=*/true, s_target_laps);
  s_show_ontrack = true;        // 以降、経過秒フィールドを約30fpsで重ねる
  s_time_force   = true;        // 20260901f：即repaintでフル描画直後の隙を無くす
  frame_sig_invalidate();       // ★20261103 画面はON TRACK＝次に静止画面へ戻るときは必ず描き直す
  disp::commit();               // ステータスバーを重ねて即転送
}

// ---- 20260901i：静止画面の再描画抑止（パフォーマンス）------------------------
//  READY/SET/FINISH/エラーは静止画面なのに、従来は内容不変でも毎250msに全画面
//  (320x240≒150KB)を作り直してSPI転送していた＝CPU/SPI/電力の純粋な無駄（発熱源・23章）。
//  「描く内容を全部ハッシュ」し、変化した時だけ描画・転送する。
//  ⚠ ON TRACKは20260901gの専用間引き（g_runのみ監視・計測中はバー凍結）をそのまま維持。
static uint32_t fnv1a(const void* p, size_t n, uint32_t h = 2166136261u) {
  const uint8_t* b = (const uint8_t*)p;
  while (n--) { h ^= *b++; h *= 16777619u; }
  return h;
}
static uint32_t s_frame_sig_prev = 0;
// ★20261103 バグ修正：「灰を押してもON TRACKのまま」の根本原因。
//   静止画面(READY/SET/FINISH)の再描画抑止は s_frame_sig_prev（状態＋g_status のハッシュ）と比較するが、
//   ON TRACK は別経路（render_ontrack_now / ST_RACE の ot_sig_prev）で描くため s_frame_sig_prev を更新しない。
//   READY→赤→（完走せず）灰→READY と戻ると、READYのハッシュが前回READYと同一＝「変化なし」と判定され
//   READY を描かず ON TRACK が画面に残った（g_status に変化が無い短い繰り返しで必ず再現）。
//   対策：状態が変わった最初のフレームは必ず描く（前回描いた状態を覚え、違えばシグネチャを無効化）。
static GwState s_drawn_state = ST_SPLASH;
static void frame_sig_invalidate() { s_frame_sig_prev = 0xFFFFFFFFu; }
//  状態＋g_status全域（バーの全項目・ID一覧・READYの本日ベスト）を含む共通シグネチャ。
//  g_status/g_runはstatic(ゼロ初期化)なのでパディングは常に0＝ハッシュは決定的。
static uint32_t frame_sig_base() {
  uint32_t h = fnv1a(&s_state, sizeof(s_state));
  return fnv1a(&disp::g_status, sizeof(disp::g_status), h);
}
// ---- TFT描画：状態機械に同期して画面を切り替える（docs/20・12.3）----------
//  ステータスバーの○×はここで g_status に反映してから各画面を描く。
static void tick_display() {
  // スプラッシュ（非ブロッキング）：250msゲートより前で自前タイミング描画。
  if (s_state == ST_SPLASH) {
    static uint32_t sf_last = 0;
    uint32_t e = millis() - s_splash_ms;
    if (e >= SPLASH_MS) { s_state = ST_IDLE; sig_ready_red(); Serial.println("[SPLASH] done -> READY"); return; }     // 終了→READY（★READY赤点灯）
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
  if (s_state != s_drawn_state) { s_drawn_state = s_state; frame_sig_invalidate(); }   // ★20261103 状態遷移の初回は必ず描く

  // ステータス更新（NODE充足は残課題#8で実数へ。今はJOIN実数の反映口のみ用意）
  disp::g_status.wifi_ok = (WiFi.status() == WL_CONNECTED);
  disp::g_status.ch      = g_channel;
  // 20260901：本日のベストをREADY(draw_idle)へ渡す（0=未記録→「-」表示）。
  disp::g_status.best_total_ms   = s_best_total_ms;
  disp::g_status.best_total_lane = s_best_total_lane;
  disp::g_status.best_lap_ms     = s_best_lap_ms;
  disp::g_status.best_lap_lane   = s_best_lap_lane;
  for (int i = 0; i < 3; i++) disp::g_status.best_lap_lane_ms[i] = s_best_lap_lane_ms[i];  // ★レーン別ベスト（READY Lap行）
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
        if (prev_wifi && !wifi_now) { buzzer_start(500); Serial.println("[BEEP] WiFi切断"); }
        if (have_now < prev_have)   { buzzer_start(500); Serial.println("[BEEP] ノード離脱"); }
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
  // Beamバー：ラッチ（片/両ビーム欠を一度でも受けたら×・灰で復帰）に加え、
  //   「いま」3秒以上張り付いているレーンがあれば×＝ライブ判定（20260830c）。
  //   SE（SQ）のライブ張り付き（HEARTBEAT付帯・7秒失効）も全数判定に含める（20260831）。
  bool node_stuck_now = false;
  for (uint8_t s2 = 0; s2 <= 5; s2++)
    if (s_node_stuck_bits[s2] != 0 && (millis() - s_node_stuck_ms[s2]) <= 7000) { node_stuck_now = true; break; }
  disp::g_status.beam_ok = !s_beam_bad && !beam::any_stuck() && !node_stuck_now;
  // unsent は各機能側から順次代入予定。

  // Sync集約（B1/24.11・27章）：自機未同期 or 直近q=3受信 で Sync×（自動復帰型）。
  //  20260831k：同期ズレの表示は READY(ST_IDLE) のときだけ（ユーザー確定）。
  //   計測中(ARMED/GREEN/RACE/FINISH)は Sync:OK 扱いにして赤バーを出さない。
  //   ※SEの時刻ズレ自体はアプリ側が生データで判定・放棄するので、走行中に画面を汚さない。
  {
    bool recent_q3 = (s_last_q3_ms != 0) && (millis() - s_last_q3_ms < 10000);
    bool synced    = tsync::is_synced() && !recent_q3;
    disp::g_status.sync_ok = (s_state == ST_IDLE) ? synced : true;
  }

  // 排他検知（GW/RC/SG）。gw_dup/rc_dup/sg_dup を更新し、生存SGも確定する。
  bool conflict = eval_exclusivity();
  // 20260831g：このフレームで「何らかの全画面エラー条件」が生きているか。
  //   灰ack の自動リセット判定に使う（条件が全て消えたフレームで ack を落とす）。
  bool err_cond_live = conflict;
  if (conflict && !s_err_ack) {   // 20260831g：灰でackされていれば排他でも描画せず素通り（強制運用）
    // 排他エラーは通常画面より優先して全面表示（優先順位：GW > SG > RC）。
    disp::ErrItem errs[3]; int cnt = 0;   // 既定でarg_i=0/label空。kindのみ下で設定。
    if (disp::g_status.gw_dup) errs[cnt++].kind = disp::ERR_GW_DUP;
    if (disp::g_status.sg_dup) errs[cnt++].kind = disp::ERR_SG_DUP;
    if (disp::g_status.rc_dup) errs[cnt++].kind = disp::ERR_RC_DUP;
    // 20260901i：内容が変わった時だけ描画（エラー表示中も毎250msの150KB転送をしない）
    uint32_t esig = fnv1a(errs, sizeof(disp::ErrItem) * cnt, frame_sig_base() ^ 0xE1u);
    if (esig != s_frame_sig_prev) {
      s_frame_sig_prev = esig;
      disp::draw_error(errs, cnt);
      disp::commit();             // ステータスバーを重ねて転送
    }
    s_show_ontrack = false;       // エラー面の上に経過秒を重ねない
    s_err_screen = true;          // 赤ボタン全面禁止（20260831e）
    buzzer_error_edge(true);      // エラー発生の立ち上がりで2秒1回
    return;
  }

  // 待機画面のエラー面（27章/24.4⑤・24.14）：
  //   C1 Lost は灰リセットまで出しっぱなし。A1/A2 は灰ボタン後3秒だけ。いずれも待機(ST_IDLE)で出す。
  //   排他系(GW/RC/SG重複)は上で return 済みなので、ここは A1/A2/Lost に絞れる（errs枠溢れ回避・27.3）。
  if (s_state == ST_IDLE) {
    // 20260904 改修⑧（案Z）：READY感知不能を「ラッチ・灰必須」→「ライブ追従」に変更。
    //   毎フレーム物理状態を評価し、張り付き中だけ表示（直れば自動で消える＝Beamバー等と統一）。
    //   ブザーは物理状態の立ち上がりで1回のみ（buzzer_error_edgeが false→true でのみ鳴動）。
    //   復帰まで再鳴動しない。s_nobeam_latched はライブ状態として set/clear し、赤ロック・
    //   ビーム破棄・灰クリアの既存参照はそのまま活かす（全断＝TX疑いはラッチ対象外で従来どおり）。
    bool nobeam_now = false;
    uint8_t ll = 0, lm = 0;
    if (!beam::all_stuck() && beam::first_stuck(ll, lm)) {          // 自機GW
      nobeam_now = true; s_nobeam_src = (uint8_t)NODE_ID;
      s_nobeam_lane = ll; s_nobeam_miss = lm;
    } else {                                                        // SE（HEARTBEAT・7秒失効）
      for (uint8_t s2 = 0; s2 <= 5 && !nobeam_now; s2++) {
        if (s_node_stuck_bits[s2] == 0 || s_node_stuck_bits[s2] == 0x07) continue;  // 0x07は全断→対象外
        if ((millis() - s_node_stuck_ms[s2]) > 7000) continue;
        for (int L = 0; L < 3; L++)
          if (s_node_stuck_bits[s2] & (1 << L)) {
            nobeam_now = true; s_nobeam_src = s2;
            s_nobeam_lane = (uint8_t)(L + 1); s_nobeam_miss = s_node_stuck_miss[s2][L]; break;
          }
      }
    }
    static bool s_nobeam_prev = false;
    if (nobeam_now && !s_nobeam_prev)
      Serial.printf("[NOBEAM] live src=%u lane=%u miss=%u\n",
                    s_nobeam_src, s_nobeam_lane, s_nobeam_miss);
    s_nobeam_prev    = nobeam_now;
    s_nobeam_latched = nobeam_now;      // ★ライブ追従（直れば自動false・案Z）
    if (nobeam_now) err_cond_live = true;
    // 張り付き中だけ専用エラー画面（他のIDLEエラーより優先）。灰ackで一時的に隠すのは
    //   従来の強制運用を踏襲（物理復帰でも自動で消える）。ブザーは他エラーと同じく
    //   描画ブロック内でbuzzer_error_edge(true)＝立ち上がり1回のみ（共有エッジ状態を壊さない）。
    if (nobeam_now && !s_err_ack) {
      disp::ErrItem e1[1];
      e1[0].kind = disp::ERR_READY_NOBEAM;
      build_beam_label(e1[0].label, sizeof(e1[0].label), s_nobeam_src, s_nobeam_lane,
                       s_nobeam_miss);   // 20260901k：miss=3(両側)→センサーAB
      disp::draw_error(e1, 1);
      disp::commit();
      s_show_ontrack = false;
      s_err_screen = true;       // 赤ボタン全面禁止（20260831e）
      buzzer_error_edge(true);   // 立ち上がりで2秒「ピー」1回・復帰まで再鳴動しない（案Z）
      return;
    }


    bool show_post = ((int32_t)(s_post_err_until - millis()) > 0);   // wrap安全な残時間判定
    bool a1 = show_post && s_race_q2;
    bool a2 = show_post && s_race_q1;
    bool alldown_self = beam::all_stuck();                     // 自機GW（ライブ・20260830c）
    bool alldown_node = show_post && (s_alldown_src != 0xFF);  // SQ等ノード（per-race・20260830d）
    bool alldown = alldown_self || alldown_node;
    // （感知不能の個別レーンは上のラッチで処理済み。ここでは全断＝TX疑いのみ扱う）
    // ノード（SE）の全断だけライブ検知（個別レーンはラッチ側・20260831c）。
    uint8_t nlv_src = 0xFF; bool node_all_live = false;
    for (uint8_t s2 = 0; s2 <= 5 && !node_all_live; s2++) {
      if (s_node_stuck_bits[s2] != 0x07) continue;
      if ((millis() - s_node_stuck_ms[s2]) > 7000) continue;   // 失効（切断・復旧）
      nlv_src = s2; node_all_live = true;
    }
    if (node_all_live) alldown = true;                          // SEの全断もライブでTX疑い表示
    if (disp::g_status.lost || a1 || a2 || alldown) err_cond_live = true;  // 20260831g：ack自動リセット判定
    if ((disp::g_status.lost || a1 || a2 || alldown) && !s_err_ack) {  // 20260831g：灰ackで素通り
      disp::ErrItem errs[4]; int cnt = 0;
      // F-2：描画は配列順。全断(TX疑い)→A1→A2→Lost の順に積む。
      if (alldown) {
        errs[cnt].kind = disp::ERR_ALL_BEAM;                          // 全ビーム断（TX側疑い）
        if (alldown_self)          snprintf(errs[cnt].label, sizeof(errs[cnt].label), "GW%u", (unsigned)NODE_ID);
        else if (node_all_live)    snprintf(errs[cnt].label, sizeof(errs[cnt].label), "SQ%u", nlv_src);
        else                       snprintf(errs[cnt].label, sizeof(errs[cnt].label), "%s%u",
                                            (s_alldown_src <= 5) ? "SQ" : "GW", s_alldown_src);
        cnt++;
      }
      if (a1 && cnt < 4) {
        errs[cnt].kind = disp::ERR_SENSOR_BOTH;                       // A1 両ビーム欠
        build_beam_label(errs[cnt].label, sizeof(errs[cnt].label), s_race_q2_src, s_race_q2_lane,
                         s_race_q2_miss); cnt++;   // 20260901k：miss=3(両側)→センサーAB
      }
      if (a2 && cnt < 4) {
        errs[cnt].kind = disp::ERR_SPEED_ONLY;                        // A2 片ビーム欠
        build_beam_label(errs[cnt].label, sizeof(errs[cnt].label), s_race_q1_src, s_race_q1_lane, s_race_q1_miss); cnt++;
      }
      if (disp::g_status.lost && cnt < 4) {
        errs[cnt].kind = disp::ERR_SECTOR_COMM;                       // C1（最後尾・出しっぱなし）
        snprintf(errs[cnt].label, sizeof(errs[cnt].label), "SQ%u", s_lost_src); cnt++;
      }
      // 20260901i：内容が変わった時だけ描画（同上）
      uint32_t esig = fnv1a(errs, sizeof(disp::ErrItem) * cnt, frame_sig_base() ^ 0xE2u);
      if (esig != s_frame_sig_prev) {
        s_frame_sig_prev = esig;
        disp::draw_error(errs, cnt);
        disp::commit();
      }
      s_show_ontrack = false;     // エラー面の上に経過秒を重ねない
      s_err_screen = true;        // 赤ボタン全面禁止（20260831e）
      buzzer_error_edge(true);    // エラー発生の立ち上がりで2秒1回
      return;
    }
  }
  s_err_screen = false;           // ここに到達＝全画面エラー無し。赤ボタン解禁（20260831e）
  buzzer_error_edge(false);       // 20260831g：エラー面を出していない＝ブザーエッジをリセット。
                                  //   灰ackでスキップ中もここを通るため、原因が直って再発した時に
                                  //   確実に「無→有」エッジが立って2秒鳴る（取りこぼし防止）。
  // 20260831g：全エラー条件が消えた（=err_cond_liveがfalse）フレームで灰ackを解除。
  //   これにより「一度healthyに戻ってから再発した新しいエラー」は通常どおり再表示される。
  //   逆に原因が継続している間は ack を保持し、灰で抜けた画面へ戻らない（強制運用を継続）。
  if (!err_cond_live) s_err_ack = false;

  switch (s_state) {
    case ST_IDLE: {
      // 20260901i：READYは静止画面。内容(状態＋g_status＝バー全項目/ID一覧/本日ベスト)が
      //  変わらない限り再描画・転送しない（従来は常時4fpsで150KB転送＝純無駄・発熱源）。
      uint32_t sig = frame_sig_base();
      if (sig == s_frame_sig_prev) { s_show_ontrack = false; return; }
      s_frame_sig_prev = sig;
      disp::draw_idle();
      s_show_ontrack = false;
      break;
    }
    case ST_ARMED: {
      // 赤押下を受け付けた「間」を埋める SET 画面（docs/20.6）。
      //   READY(待機)から即切り替わることで、押下が通ったことを視認できる。
      //   20260901i：SETは静止画面（blinkは2026-08-24bで未使用）。変化時のみ描画・転送。
      uint32_t sig = frame_sig_base();
      if (sig == s_frame_sig_prev) { s_show_ontrack = false; return; }
      s_frame_sig_prev = sig;
      disp::draw_set(blink);
      s_show_ontrack = false;
      break;
    }
    case ST_GREEN:
    case ST_RACE: {
      // 20260901g：カクつき根治。ON TRACK中はフル画面(320x240≒150KB)の転送を毎250ms行わず、
      //  「ラップ確定/完走の内容が変わった時」だけフル再描画する。変化の無いフレームは
      //  フル転送を一切打たず、右上カウンタ(小スプライト・tick_time_field)だけを更新＝滑らか。
      //  ⚠従来は毎250msに150KBを転送→その数十msだけカウンタが凍り、周期的ヒッチに見えていた。
      //  ● は点滅をやめ常時点灯（点滅のたびフル転送するとヒッチが復活するため）。
      //  公式タイムはµs確定＝この間引きは計測精度に一切無関係。
      static uint32_t ot_sig_prev = 0xFFFFFFFFu;
      uint32_t sig = 0;
      for (int i = 0; i < 3; i++)
        sig = sig * 131u + (uint32_t)disp::g_run[i].done * 2u + (disp::g_run[i].fin ? 1u : 0u);
      if (sig == ot_sig_prev && s_show_ontrack) {
        // 変化なし＆既にON TRACK表示中：フル転送を打たない（ヒッチの元を断つ）。
        //  右上カウンタは tick_time_field が毎ループ滑らかに更新し続ける。
        return;
      }
      ot_sig_prev = sig;
      uint32_t elapsed = race_start_us() ? (uint32_t)((tsync::now_gw_us() - race_start_us()) / 1000ULL) : 0;  // 20260831i：緑or共通起点
      disp::draw_ontrack(elapsed, /*blink=*/true, s_target_laps);   // ●は常時点灯（点滅でフル転送しない）
      s_show_ontrack = true;      // 以降、経過秒フィールドを重ね続ける
      s_time_force   = true;      // 20260901f：フル再描画の直後に右上カウンタを強制repaint
      break;
    }
    case ST_FINISH: {
      // 20260901i：FINISHも静止画面。結果(g_run)・周回数・バーが不変なら転送しない。
      //  FINISH中の余走は結果を上書きしない仕様＝g_run不変＝自然にスキップされる。
      uint32_t sig = fnv1a(disp::g_run, sizeof(disp::g_run),
                           fnv1a(&s_target_laps, sizeof(s_target_laps), frame_sig_base()));
      if (sig == s_frame_sig_prev) { s_show_ontrack = false; return; }
      s_frame_sig_prev = sig;
      disp::draw_finish(s_target_laps);   // 合計＋各周ラップ（docs/20.4・20260830実装）
      s_show_ontrack = false;             // 経過秒カウントは消す（docs/20.4）
      break;
    }
  }
  buzzer_error_edge(false);              // エラー無し＝立ち上がり検出をリセット（再発時にまた1回鳴る）
  disp::commit();                        // ステータスバーを重ねて一括転送
}

void loop() {
  gwlog::service_serial();    // ★20261102 PCから log / logtail / logclear / logstat
  rx_pump();                  // ★P1：ESP-NOW受信（EVENT/COMMAND/JOIN等）をloop文脈で処理
  tick_buttons();
  buzzer_tick();              // ブザー非ブロッキング消音（ledc・20261012）
  buzzer_jingle_tick();       // ★起動ジングル進行（パチンコ風・非ブロッキング・20261109）
  sig_tick();                 // ★信号灯の非ブロッキング消灯（赤は緑+50ms/緑は緑+2秒・DC30/DC32）
  tick_gw_presence();         // GW自身の在席ビーコン（GW2台検知の相互化）
  tick_roster();              // 接続機材の一覧をシリアルへ（設営確認用・3秒毎）
  tick_sequence();
  flying_tick();              // ★フライング優先中の赤点滅・窓終了処理
  if (s_set_now_req) { s_set_now_req = false; render_set_now(); }  // 赤押下の即SETをloop文脈で安全に描画
  if (s_green_now_req) { s_green_now_req = false; render_ontrack_now(); }  // ★即緑→ON TRACK描画（20261030）

  beam::Hit hit;
  int drain = 0;
  if (s_state == ST_SPLASH || s_nobeam_latched) {  // スプラッシュ中/感知不能ラッチ中はビームを捨てるだけ
    while (drain++ < 8 && beam::poll(hit)) { /* discard */ }  //   （計測開始も周回記録もしない・20260831c）
  } else
  while (drain++ < 8 && beam::poll(hit)) {   // 1周8件まで（浮きGPIO洪水でloopを占有させない）
    static uint32_t self_seq = 0;
    // 走行式(赤ボタン未使用)：最初のS/G通過で内部レースを開始する（F1式は既にactive）。
    //   これで s_internal_rid が採番され、以降の自機/SQイベントが同じ内部ridで刻まれる。
    // FINISH表示中は新レースを起こさない（余走の通過は完走レースのridのまま記録され、
    //   FINISH画面のラップ表示も保持される。次レースは灰リセット後・20260830）。
    // 20260904 改修④②：q=2（張り付き＝故障・断線・居座り）では内部レースを開始しない。
    //   通過ではないものでレースが始まると、TFTはREADYのまま内部だけ計測中になる（表示≠内部）。
    //   このガードで「READYなのに内部だけ計測中」を根治（RESET有無に関係なく）。
    if (!s_race_active && s_state != ST_FINISH && hit.quality != 2) begin_internal_race();
    bool was_idle_hit = (s_state == ST_IDLE);   // 通過ピッ判定用（遷移前の状態を保持）
    bool was_armed_hit = (s_state == ST_ARMED); // ★赤点灯中(スタート前)の通過＝フライング
    // 走行式(TA)：待機のまま最初の「正常通過(q=0/1)」が来たら ON TRACK＋計測開始。
    //   q=2（張り付き＝A1：故障・断線・CO）は通過ではないのでスタートさせない（20260830）。
    if (s_state == ST_IDLE && hit.quality != 2) {
      // 20260831i：走行式のTAスタート。s_green_t_us（緑時刻）は立てない（DA4の判別を濁さない）。
      //   代わりに s_start_shared_us（最初の1台の初通過＝全車共通起点・(い)）を1回だけ立てる。
      if (!s_start_shared_us) s_start_shared_us = hit.t_a_us ? hit.t_a_us : tsync::now_gw_us();
      s_state      = ST_RACE;          // TFTを ON TRACK に切替
      sig_all_off();                   // ★20261030：フリー走行はシグナル無し＝READYの赤を消す
      render_ontrack_now();            // 緑同様、即時に計測中画面を描く
      Serial.printf("[TA] START rid=%u t=%llu\n",
                    s_internal_rid, (unsigned long long)s_start_shared_us);
    }
    // マシン帰属＋周回・ラップ記録（自機S/Gのみ・DA15/DA8・20260830b）。
    //   q=2は通過ではないので数えない（張り付きで周回が水増しされるのを防ぐ）。
    //   TFTのL列＝「そのレーンでスタートしたマシン」。レーンチェンジしても同じ列に積む。
    // 20260831k：逆走(B→A)はTFT計測対象外＝帰属・ラップに使わない。ただし生データは後段でPUSH。
    //   quality!=2（張り付き除外）に加え、!hit.reverse（逆走除外）を条件に追加。
    // ★ST_ARMED(赤点灯中=スタート前)の通過はフライング。緑起点より前なのでTFTの周回/帰属には
    //   数えない（生データは後段で必ずspool→サーバーがjump_startで印を付ける）。
    // ★20261029：逆走(hit.reverse=B→A順)を周回から除外しない。実機(GW6)でB受光が不安定だと
    //   Bの早エッジで“偽逆走”になり、正規の通過が落ちて再同期(2区間合体)やDNFを生んでいた
    //   （race8 実証）。通過はA打刻で確定し、速度(A→B差)だけ使わない。サーバーも同じ扱い。
    if (hit.lane >= 1 && hit.lane <= 3 && hit.quality != 2 && s_state != ST_FINISH && s_state != ST_ARMED) {
      int p = hit.lane;                                    // 物理レーン
      uint64_t t = hit.t_a_us ? hit.t_a_us : hit.t_b_us;   // q=1でAが欠けたらBで代用
      // ---- ① 帰属（20260831j 全面改訂）：本コースは LC=+1 固定（1→2→3→1・ユーザー確定）----
      //  旧実装の遷移学習(ORDER{1,0,2}・s_m_step)は、1ゲート取りこぼしで +2/0 を誤学習・永久固定し、
      //  以後の帰属崩壊→幽霊マシン誕生→「実際と異なるレーンに記録」の根本原因だったため廃止。
      //  不変量：全車+1巡回＆スタートレーン相異 → 同一周回数の車同士は期待レーンが被らない。
      int  m = 0;
      bool resync = false;
      // (1) 通常帰属：期待レーン(直前レーン+1)が一致する走行中マシン（時間ガードあり）
      for (int c = 1; c <= 3; c++) {
        if (!s_lane_active[c] || disp::g_run[c - 1].fin) continue;   // 未走行/完走済みは対象外
        if ((t - s_lane_last_us[c]) < MIN_LAP_US) continue;          // 1秒未満の再ヒットは対象外
        if (((s_m_lastlane[c] - 1 + 1) % 3) + 1 == p) { m = c; break; }
      }
      // (2) 出走登録（0周目）：どのマシンにも合わず、そのレーンのマシンが未活性なら新規エントリー。
      //     signalあり=緑後の0周目／signalなし=時差スタートの初通過。どちらもここで参加確定。
      //     ただし完走後の惰走を新規マシンと誤認しないよう、先に(2.5)で吸収する（20260831k）。
      if (m == 0) {
        // (2.5) 完走後の惰走吸収：完走済みマシンが走り続けてゲートを踏むと、+1/+2 の期待レーンに
        //   合致するが (1)(3) では fin 除外され、そのまま(2)で幽霊マシンとして登録されてしまう。
        //   完走マシンの最後の通過レーンから +1/+2 に合致する通過は「完走後通過」として記録破棄。
        for (int c = 1; c <= 3; c++) {
          if (!s_lane_active[c] || !disp::g_run[c - 1].fin) continue;   // 完走済みのみ対象
          if ((t - s_lane_last_us[c]) < MIN_LAP_US) continue;
          int d1 = ((s_m_lastlane[c] - 1 + 1) % 3) + 1;
          int d2 = ((s_m_lastlane[c] - 1 + 2) % 3) + 1;
          if (p == d1 || p == d2) {
            s_m_lastlane[c] = (uint8_t)p; s_lane_last_us[c] = t;        // 位置だけ進めて追従継続
            Serial.printf("[SG] 完走後の惰走を無視 M%d lane=%d\n", c, p);
            m = -1;                                                     // 記録破棄マーク
            break;
          }
        }
      }
      if (m == 0 && !s_lane_active[p]) m = p;
      // (3) 再同期救済：+1候補なし＆エントリー不可（レーン活性済み）のとき、
      //     「1ゲート取りこぼした車（期待+2の位置）」が唯一に定まるなら同一車として再同期。
      //     A/B両欠けは保険構造上まれだが、起きても幽霊マシンを生まず追跡を維持する。
      if (m == 0) {
        int cand = 0;
        for (int c = 1; c <= 3; c++) {
          if (!s_lane_active[c] || disp::g_run[c - 1].fin) continue;
          if ((t - s_lane_last_us[c]) < MIN_LAP_US) continue;
          if (((s_m_lastlane[c] - 1 + 2) % 3) + 1 == p) {
            if (cand) { cand = -1; break; }                          // 複数候補=曖昧→救済しない
            cand = c;
          }
        }
        if (cand > 0) { m = cand; resync = true;
          Serial.printf("[SG] 再同期 M%d：1ゲート取りこぼしを補完 lane=%d\n", m, p); }
      }
      if (m > 0) {                        // m=-1（完走後惰走）は記録破棄・生PUSHのみ（20260831k）
        s_lane_active[m] = true;
        s_lane_cross[m] += resync ? 2 : 1;   // 再同期＝取りこぼしゲート分も加算し完走位置を維持（20260831j）
        if (resync) s_m_clean[m] = false;    // 20260901d：不足(取りこぼし)ありのマシンはベスト非採用
        s_m_lastlane[m] = (uint8_t)p;
        disp::LaneRun& r = disp::g_run[m - 1];
        if (s_lane_cross[m] == 1) {
          s_lane_first_us[m] = t;                          // 起点通過（走行式の合計起点）
        } else if (r.done < disp::MAX_LAPS) {              // 2回目以降＝1周確定
          //  ※再同期時は前回検知からの実測差＝2区間ぶんが1エントリに載る（捏造せず実測のまま・20260831j）
          r.lap_ms[r.done++] = (uint32_t)((t - s_lane_last_us[m]) / 1000ULL);
          Serial.printf("[LAP] M%d lap%u=%.2fs (lane%d)\n", m, (unsigned)r.done,
                        r.lap_ms[r.done - 1] / 1000.0, p);
        }
        s_lane_last_us[m] = t;
        // 完走判定：起点1回＋規定周回（maybe_autocomplete と同条件・マシン単位）
        if (!r.fin && s_lane_cross[m] >= (uint32_t)s_target_laps + 1) {
          r.fin = true;                                    // このマシンは完走（TFTにFIN表示）
          // 20260831i：合計タイムの起点。
          //   signalあり（F1式）：s_green_t_us=緑時刻（全車共通）。
          //   signalなし（走行式）：START_MODE_SHARED に従う。
          //     (い=1)：s_start_shared_us（最初の1台の初通過）を全車共通起点にする【既定・ユーザー確定】
          //     (あ=0)：各車 s_lane_first_us[m]（自分の初通過）を起点にする【将来戻す可能性・要見直し】
          uint64_t start;
          if (s_green_t_us) {
            start = s_green_t_us;                         // F1式：緑起点（最優先・全車共通）
          } else {
#if START_MODE_SHARED
            start = s_start_shared_us ? s_start_shared_us // (い)：最初の1台の初通過を全車共通に
                                      : s_lane_first_us[m];
#else
            start = s_lane_first_us[m];                   // (あ)：各車それぞれの初通過
#endif
          }
          r.total_ms = (uint32_t)((t - start) / 1000ULL);  // F1式=緑起点／走行式=初回通過起点
          Serial.printf("[SEQ] M%d 完走 total=%.2fs\n", m, r.total_ms / 1000.0);
          // 20260901e：本日のベスト採用（マシン単体・台数も発進方式も不問）。
          //  条件＝過不足なく完走：取りこぼし無し(s_m_clean)かつ
          //   クロス数==target+1・ラップ数==target が丁度一致（不足を弾く。過剰は不応期100ms/MIN_LAP1sで既に抑止）。
          bool best_ok = s_m_clean[m]
                       && (s_lane_cross[m] == (uint32_t)s_target_laps + 1)
                       && (r.done == (uint8_t)s_target_laps);
          if (best_ok) {
            if (s_best_total_ms == 0 || r.total_ms < s_best_total_ms) {
              s_best_total_ms = r.total_ms; s_best_total_lane = (uint8_t)m;
              Serial.printf("[BEST] total %.2fs (M%d) ★過不足なし完走\n", r.total_ms / 1000.0, m);
            }
            for (int k = 0; k < r.done && k < disp::MAX_LAPS; k++) {
              uint32_t lap = r.lap_ms[k];
              if (lap > 0 && (s_best_lap_ms == 0 || lap < s_best_lap_ms)) {
                s_best_lap_ms = lap; s_best_lap_lane = (uint8_t)m;
                Serial.printf("[BEST] lap %.2fs (M%d)\n", lap / 1000.0, m);
              }
              // ★20261109：レーン別ベストラップ（READYのLap行 L1/L2/L3）。m=1..3。
              if (lap > 0 && (s_best_lap_lane_ms[m - 1] == 0 || lap < s_best_lap_lane_ms[m - 1]))
                s_best_lap_lane_ms[m - 1] = lap;
            }
          } else {
            Serial.printf("[BEST] 非採用 M%d (clean=%d cross=%u/%d done=%u/%d)\n",
                          m, (int)s_m_clean[m],
                          (unsigned)s_lane_cross[m], s_target_laps + 1,
                          (unsigned)r.done, s_target_laps);
          }
        }
      } else if (m == 0) {
        Serial.printf("[SG] 帰属不能 lane=%u（表示のみスキップ・記録は継続）\n", p);
      }
    }
    if (hit.reverse) {   // 20260831k：逆走はTFT計測から除外済み。生データはこの後PUSHされる。
      Serial.printf("[SG] 逆走(B->A)検知 lane=%u（TFT対象外・生データはPUSH）\n", hit.lane);
    }
    RawEvent e{ (uint8_t)NODE_ID, mesh::boot_id(), ++self_seq,
                hit.lane, hit.quality, hit.t_a_us, hit.t_b_us };
    spool_append(e);
    note_quality(hit.quality, (uint8_t)NODE_ID, hit.lane, hit.miss);   // 自機S/GのA1/A2も拾う（27章・A/Bまで）
    // 20260904 改修⑦：miss を追記（1=A欠け/2=B欠け/3=両欠け/0=両取得）。実機でq=1の
    //   A欠け・B欠け分布を集計し、「速度が出ない」の主因が光学(穴B)か別要因かを切り分ける。
    Serial.printf("[SG] lane=%u q=%u miss=%u rid=%u cross=%u\n",
                  hit.lane, hit.quality, hit.miss, s_internal_rid,
                  (hit.lane>=1&&hit.lane<=3)?s_lane_cross[hit.lane]:0);
    // ★通過検知フィードバック：
    //   ・ST_ARMED(赤点灯中=スタート前)の通過＝フライング → 赤点滅＋長め「ピー」で1秒優先。
    //   ・それ以外は通常の通過フラッシュ（緑一瞬＋高音ピッ）。ただしフライング優先窓(1秒)の
    //     間は、別マシンが通過しても緑は出さない（フライングが分かるよう絶対優先）。
    //   ★20261030：フィードバックを出すのは「実通過(q!=2)」かつ「FINISH中でない」ときだけ。
    //     ・q=2(張り付き)は通過ではないので光らせない・鳴らさない。
    //     ・FINISH中は、完走後にS/Gへ居座った車や余走の再横切りで detect_flash が
    //       40ms間隔より速く連打され、buzzer_start が消灯予約を上書きし続けて音が途切れず
    //       “鳴り続ける”原因になっていた（居座り＋GW6のB不安定で顕著）。FINISHは抑止する。
    if (hit.quality != 2 && s_state != ST_FINISH) {
      if (was_armed_hit) {
        flying_trigger();
      } else if (!flying_active()) {
        detect_flash();
      }
    }
    s_sg_beeped = true;                       // 既存フラグ互換のため維持
    (void)was_idle_hit;
  }

  // 20260821：POSTは「全員完走(自動)」と「灰ボタン(手動)」の2契機のみ。
  //   計測中はサーバー通信ゼロ（受信→内部ridで刻む→LittleFSへ溜める→TFTのみ）。
  maybe_autocomplete();  // 有効レーンが全員規定周回完了なら s_need_flush を立てる（DA15）
  // ★20261031 P2/P3：tick_flush / tick_fetch_layout は netタスクへ移設（loopでHTTPSしない）。
  tick_display();        // TFT描画：全画面フル更新（状態機械に同期・約4fps）
  tick_time_field();     // 案B：ON TRACK経過秒だけ約30fpsで部分更新（機敏に動かす）
}
