// firmware/remote/src/main.cpp
// ============================================================================
//  M4LAPS リモコン（RC8/RC9・XIAO ESP32C3）ファーム —— ディープスリープ版
//  役割（docs/14 DA11）：赤=SIGNAL / 灰=RESET。押下でGWへ COMMAND を投げる。
//
//  【20260819 ①スリープ化】ボタン(GPIO3/4)のLOWで起床。待機~5µA。起床＝リブート。
//  【20260819 ②二重発火防止】1押下＝一意nonce(CommandBody.arg)。正chで在圏確認→送信。
//  【20260819 ③改善】
//     ・スタックボタンガード：解放されない起床が連続したら、GPIO起床を無効化して
//       タイマー起床のクールダウンへ逃がし、固着/ショートによる枯渇ループを断つ。
//     ・ACK駆動の同一nonce再送：送信後COMMAND_ACKを待ち、来なければ同一nonceで1回だけ
//       再送（GWがnonceで重複を殺すため“二度発火せず”配送率だけ上げられる）。
//
//  ⚠ 従来版（常時起動）に戻すときは USE_DEEP_SLEEP を 0 にしてビルド。
//  ⚠ esp_deep_sleep_enable_gpio_wakeup / esp_sleep_get_gpio_wakeup_status は
//     arduino-esp32 3.x(IDF5)・C3 で有効。古いコアでは名称が違う場合あり。
// ============================================================================
#include <Arduino.h>
#include <WiFi.h>
#include "esp_sleep.h"
#include "driver/gpio.h"
#include "protocol.h"
#include "espnow_link.h"
#include "chfollow.h"

#ifndef NODE_ID
#define NODE_ID 8
#endif
#ifndef ESPNOW_CHANNEL
#define ESPNOW_CHANNEL 1
#endif

// 1=ディープスリープ運用（内蔵LiPo） / 0=従来の常時起動（USB給電）
#ifndef USE_DEEP_SLEEP
#define USE_DEEP_SLEEP 1
#endif

static constexpr uint8_t FW_MAJOR = 0;
static constexpr uint8_t FW_MINOR = 4;      // 3→4：スタックガード＋ACK再送

static constexpr int PIN_RED  = 3;   // D1(GPIO3) 赤タクト SIGNAL
static constexpr int PIN_GRAY = 4;   // D2(GPIO4) 灰タクト RESET
static constexpr uint32_t DEBOUNCE_MS = 20;

// ★20261109：GWのREADY（＝赤を押せばシグナルを切れる状態）を知らせる手元表示LED。
//   D3(GPIO5)・active-HIGH。XIAO C3のストラップはGPIO2/8/9＝GPIO5は安全。LiPo直結(5Vレール無)でも
//   3.3Vで点くよう、緑LED+150〜220Ω（青なら68〜100Ω）を GPIO5→R→LED→GND で接続する。
//   ※常時起動版(USE_DEEP_SLEEP=0)でのみ駆動。ディープスリープ版は寝ているため常時表示できない。
static constexpr int      PIN_READY_LED  = 5;
static constexpr uint32_t READY_STALE_MS = 6000;  // この時間GWビーコン未受信ならGW状態不明＝消灯(2秒×3)

// --- 起床セッションのタイミング ---
static constexpr uint32_t RELEASE_TIMEOUT_MS = 1500;  // ボタン解放待ちの上限
static constexpr uint32_t ACK_WAIT_MS        = 70;    // COMMAND_ACK待ち（ESP-NOWは数ms）
// --- スタックボタンガード ---
static constexpr uint8_t  STUCK_STREAK_MAX   = 3;     // 未解放起床がこの回数連続でクールダウンへ
static constexpr uint32_t STUCK_COOLDOWN_S   = 300;   // クールダウン（GPIO起床を無効化して眠る）

static bool s_assigned = false;
static volatile bool s_cmd_acked = false;   // COMMAND_ACK受信フラグ（recvコールバックが立てる）
// ★20261109：GWのREADY表示（手元LED）。GW在席ビーコン(PT_HEARTBEAT)の2バイト目(ready)で更新。
static volatile bool     s_gw_ready    = false;  // 直近ビーコンでGWがREADYだったか
static volatile uint32_t s_ready_rx_ms = 0;      // READY付きビーコンを最後に受けた時刻（0=未受信）

// ---- COMMAND 3連射（同一 nonce を全弾に載せる。ACKは上位で待つ）------------
static void send_command(uint8_t code, uint32_t nonce = 0, int shots = 3) {
  proto::CommandBody c = {};
  c.code = code;
  c.arg  = nonce;                            // ★押下ごとに一意（GW側で重複排除の鍵）
  for (int i = 0; i < shots; i++) {          // 既定3連射（スリープ版・docs/12 S10）。常時起動版は1発＋時間差再送
    mesh::send(proto::PT_COMMAND, 6 /*GW*/, &c, sizeof(c));
    if (i + 1 < shots) delay(3);
  }
}

// ---- 受信 -------------------------------------------------------------------
// ★20261102：COMMAND_ACK は「この押下で送ったseq」との照合を通ったときだけ受理する（常時起動版）。
//   スリープ版は1起床=1押下なので従来どおり無条件受理。
#if USE_DEEP_SLEEP
static inline bool ack_matches_pending(uint32_t) { return true; }
#else
static bool ack_matches_pending(uint32_t acked_seq);   // 定義は常時起動版ブロック（後方）
#endif
static void on_recv(const proto::PktHeader& h, const uint8_t* body,
                    int body_len, const uint8_t*) {
  if (h.type == proto::PT_COMMAND_ACK) {     // GWがCOMMANDを受領した確認
    if (ack_matches_pending(h.reserved1)) s_cmd_acked = true;
    return;
  }
  // ★20261031：GWビーコンの計測中フラグを取り込む（SEと同じ）。計測中はch走査に入らない＝押下が消えない。
  if (h.type == proto::PT_HEARTBEAT && proto::kind_of(h.src) == proto::KIND_GW && body_len >= 1) {
    chfollow::note_gw_racing(body[0] != 0);
    // ★20261109：GWビーコンのREADYフラグ(2バイト目)を取り込む＝手元READY表示LED用。
    //   旧GW(body_len=1)はready非搭載＝ここを通らず、READY_STALE_MSで自然に消灯側へ寄る。
    if (body_len >= 2) { s_gw_ready = (body[1] != 0); s_ready_rx_ms = millis(); }
    return;
  }
  if (h.type == proto::PT_JOIN_ACK && body_len >= (int)sizeof(proto::JoinAckBody)) {
    proto::JoinAckBody b; memcpy(&b, body, sizeof(b));
    if (b.node_id == NODE_ID) s_assigned = true;
    if (b.channel >= 1 && b.channel <= 13) mesh::set_channel(b.channel);  // GWのchへ
  }
}

static void send_join() {
  proto::JoinBody jb = {};
  WiFi.macAddress(jb.mac);
  jb.kind = proto::KIND_RC;
  jb.fw_major = FW_MAJOR; jb.fw_minor = FW_MINOR;
  jb.nvs_node_id = NODE_ID;
  mesh::send(proto::PT_JOIN, 6, &jb, sizeof(jb));
}

static inline bool in_contact() {
  uint32_t last = mesh::last_gw_rx_ms();
  return last != 0 && (millis() - last) < chfollow::SWEEP_AFTER_MS;
}

#if USE_DEEP_SLEEP
// 起床をまたいで保持（RTCメモリ）：未解放起床の連続回数（スタック検出）
RTC_DATA_ATTR static uint32_t s_stuck_streak = 0;

// COMMAND_ACK を最大 ms 待つ（アクションはしない・眠る前の確認だけ）
static bool wait_ack(uint32_t ms) {
  uint32_t t = millis();
  while (millis() - t < ms) { if (s_cmd_acked) return true; delay(2); }
  return s_cmd_acked;
}

// 正chで在圏を確定する。現ch(=前回学習ch)→ダメなら1..13を能動スキャン。
static bool acquire_contact(uint8_t start_ch) {
  const uint32_t PER_CH_MS = 120;
  const uint8_t  MAX_HOPS  = 14;
  uint8_t ch = start_ch;
  for (uint8_t hop = 0; hop < MAX_HOPS; hop++) {
    mesh::set_channel(ch);
    send_join();                    // GWは JOIN受信で即HEARTBEATを撒き返す
    uint32_t t = millis();
    while (millis() - t < PER_CH_MS) {
      if (in_contact()) return true;
      delay(3);
    }
    ch = (uint8_t)((ch % 13) + 1);
  }
  return in_contact();
}

// 起床要因のボタンを返す（quickタップで既に離されていても wake status で判定）
static uint8_t woke_by_command() {
  if (esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_GPIO) return 0;  // 冷起動/タイマー
  uint64_t st = esp_sleep_get_gpio_wakeup_status();
  bool red  = st & (1ULL << PIN_RED);
  bool gray = st & (1ULL << PIN_GRAY);
  if (!red && !gray) {
    red  = (digitalRead(PIN_RED)  == LOW);
    gray = (digitalRead(PIN_GRAY) == LOW);
  }
  if (red)  return proto::CMD_SIGNAL;  // 両押しは赤（SIGNAL）優先
  if (gray) return proto::CMD_RESET;
  return 0;
}

static void arm_and_sleep() {
  // 解放待ち（HIGH復帰）。LOWのまま眠るとwake-on-LOWで即再起床する。
  uint32_t t0 = millis();
  while (millis() - t0 < RELEASE_TIMEOUT_MS) {
    if (digitalRead(PIN_RED) == HIGH && digitalRead(PIN_GRAY) == HIGH) break;
    delay(10);
  }
  delay(30);
  bool still_low = (digitalRead(PIN_RED) == LOW || digitalRead(PIN_GRAY) == LOW);

  WiFi.mode(WIFI_OFF);
  // 入力＋内部プルアップ保持
  gpio_set_direction((gpio_num_t)PIN_RED,  GPIO_MODE_INPUT);
  gpio_set_direction((gpio_num_t)PIN_GRAY, GPIO_MODE_INPUT);
  gpio_pullup_en((gpio_num_t)PIN_RED);   gpio_pulldown_dis((gpio_num_t)PIN_RED);
  gpio_pullup_en((gpio_num_t)PIN_GRAY);  gpio_pulldown_dis((gpio_num_t)PIN_GRAY);

  if (still_low) {
    // ★スタックガード：解放されない起床が連続 → 固着/ショート疑い。
    //   GPIO起床のまま眠ると即再起床の枯渇ループになるため、規定回数でタイマー起床へ逃がす。
    s_stuck_streak++;
    if (s_stuck_streak >= STUCK_STREAK_MAX) {
      Serial.printf("[SLEEP] stuck? streak=%lu → timer cooldown %lus（GPIO起床は無効化）\n",
                    (unsigned long)s_stuck_streak, (unsigned long)STUCK_COOLDOWN_S);
      Serial.flush();
      esp_sleep_enable_timer_wakeup((uint64_t)STUCK_COOLDOWN_S * 1000000ULL);
      esp_deep_sleep_start();          // GPIO起床は敢えて設定しない＝固着でも起きない
    }
  } else {
    s_stuck_streak = 0;                // 正常解放でリセット
  }

  esp_deep_sleep_enable_gpio_wakeup((1ULL << PIN_RED) | (1ULL << PIN_GRAY),
                                    ESP_GPIO_WAKEUP_GPIO_LOW);
  Serial.println("[SLEEP] deep sleep（ボタンで起床）");
  Serial.flush();
  esp_deep_sleep_start();
}

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);   // ホスト未読でもブロックしない（XIAO CDCハング対策）
  pinMode(PIN_RED,  INPUT_PULLUP);
  pinMode(PIN_GRAY, INPUT_PULLUP);

  uint8_t cmd = woke_by_command();   // 冷起動/タイマーなら0
  Serial.printf("\n=== M4LAPS Remote RC%d (deep-sleep fw) cmd=%u streak=%lu ===\n",
                NODE_ID, cmd, (unsigned long)s_stuck_streak);

  uint8_t ch0 = chfollow::initial_channel(ESPNOW_CHANNEL);
  if (!mesh::begin(NODE_ID, ch0, on_recv)) {
    Serial.println("ESP-NOW init 失敗→スリープ");
    arm_and_sleep();
  }
  delay(15);

  // 押下ごとに一意 nonce（0は使わない＝旧互換の“nonce無し”と区別）
  uint32_t nonce = 0;
  if (cmd) { nonce = esp_random(); if (nonce == 0) nonce = 1; }

  // ★送る前に正chで在圏を確定（別ch再送を作らない）
  bool contact = acquire_contact(ch0);
  Serial.printf("[LINK] contact=%d ch=%u\n", contact ? 1 : 0, mesh::channel());

  if (cmd) {
    // 1発目
    s_cmd_acked = false;
    send_command(cmd, nonce);
    bool ok = wait_ack(ACK_WAIT_MS);
    // ★ACKが来なければ“同一nonce”で1回だけ再送（GWがnonceで重複を殺す＝二度発火なし）
    if (!ok) {
      send_command(cmd, nonce);
      ok = wait_ack(ACK_WAIT_MS);
    }
    Serial.printf("[BTN] %s nonce=%08x ack=%d\n",
                  cmd == proto::CMD_SIGNAL ? "SIGNAL" : "RESET", nonce, ok ? 1 : 0);
  }

  arm_and_sleep();
}

void loop() { /* 使わない（setupで完結して眠る） */ }

#else  // ===== 従来の常時起動版（USB給電・スリープしない）=====================
struct Button {
  int pin; bool last; uint32_t t;
  bool pressed() {
    bool now = (digitalRead(pin) == LOW);
    uint32_t nowm = millis();
    if (now != last && (nowm - t) > DEBOUNCE_MS) { t = nowm; last = now; return now; }
    return false;
  }
};
static Button s_red  { PIN_RED,  false, 0 };
static Button s_gray { PIN_GRAY, false, 0 };

static void tick_presence() {
  static uint32_t last = 0;
  uint32_t nowm = millis();
  uint32_t interval = s_assigned ? 3000 : 1000;
  if (nowm - last < interval) return;
  last = nowm;
  if (!s_assigned) send_join();
  else             mesh::send(proto::PT_HEARTBEAT, 6, nullptr, 0);
}

// ★20261031：常時起動版の送信を「送りっぱなし3連射」から「ACK待ち＋時間差再送（ノンブロッキング）」へ。
//   旧：3発を約6msに固めて送るだけ。全ノードが同時に中継するので3発がまとめて衝突し、ACKも見ていなかった。
//       ch走査中（GWの声が12秒途絶→0.5秒ずつch巡回）の押下はそのまま消えていた。
//   新：1発送って COMMAND_ACK を待つ。来なければ 40〜70ms（乱数）あけて再送（最初の4発）、
//       以降は120ms間隔で、押下から最大2.5秒まで粘る（20261102・旧「4発/約165msで諦め」を拡張）。
//       在圏外（走査中）は 100ms 間隔の保留再送＝在圏が戻った瞬間に届く。
//       nonce は同一なので GW 側は二重発火しない（冪等化・docs/12）。
// ★20261102 再点検で修正：
//   (1) 旧コードは4発目を送った直後のloop周回で即「give up」していた＝4発目のACKを待たずに捨てる
//       （実質3発・約165msしか粘らない）。GWはTFT全面描画(約60〜100ms)・LittleFS書込み・WiFi再接続
//       スキャン(1〜3秒)で受信処理や送信が止まる時間があり、165msの窓だと押下がまるごと消えていた。
//   (2) 再送窓を「最初の4発は40〜71ms間隔、以降は120ms間隔で最大 CMD_WINDOW_MS(2.5秒)」へ拡張。
//       ACK駆動なので正常時は1発で止まる（電波を増やさない）。nonce同一＝GWは二度発火しない。
//   (3) ACKは「自分がこの押下で送ったseq」と照合する（GWは受領seqを reserved1 に載せて返す）。
//       旧コードは種別だけで判定していたため、直前の押下のACKが遅れて届くと新しい押下が
//       送りもせずに「ACK済み」扱いになり得た。
static constexpr uint32_t CMD_WINDOW_MS   = 2500;  // 押下を粘る総時間（在圏・走査中とも）
static constexpr uint8_t  CMD_FAST_TRIES  = 4;     // 速い再送（40〜71ms）の回数
static constexpr uint32_t CMD_SLOW_GAP_MS = 120;   // 以降の再送間隔
static constexpr uint8_t  CMD_SEQ_N       = 24;    // この押下で送ったseqの記録数（2.5s/40ms≒最大22発）
static uint8_t  s_pend_code  = 0;      // 0=保留なし
static uint32_t s_pend_nonce = 0;
static uint32_t s_pend_born  = 0;      // 押下時刻
static uint32_t s_pend_last  = 0;      // 直近送信時刻
static uint8_t  s_pend_tries = 0;      // 送信回数（在圏・走査中を問わず）
static uint32_t s_pend_seqs[CMD_SEQ_N];
static uint8_t  s_pend_seq_n = 0;
static void fire(uint8_t code) {
  uint32_t n = esp_random(); if (!n) n = 1;
  s_pend_code = code; s_pend_nonce = n; s_pend_born = millis(); s_pend_tries = 0; s_pend_last = 0;
  s_pend_seq_n = 0;
  s_cmd_acked = false;
}
// COMMAND_ACK（ヘッダ reserved1=受領seq）が、この押下で送ったどれかのseqに一致するか。
//   reserved1==0 は旧GW互換（seq未搭載）＝従来どおり種別だけで受理。
static bool ack_matches_pending(uint32_t acked_seq) {
  if (acked_seq == 0) return true;
  for (uint8_t i = 0; i < s_pend_seq_n; i++) if (s_pend_seqs[i] == acked_seq) return true;
  return false;
}
static void service_cmd() {
  if (!s_pend_code) return;
  uint32_t now = millis();
  if (s_cmd_acked) { Serial.printf("[CMD] ack tries=%u %lums\n", s_pend_tries, (unsigned long)(now - s_pend_born)); s_pend_code = 0; return; }
  if (now - s_pend_born > CMD_WINDOW_MS) {
    Serial.printf("[CMD] give up (no ACK tries=%u contact=%d)\n", s_pend_tries, in_contact() ? 1 : 0);
    s_pend_code = 0; return;
  }
  bool contact = in_contact();
  uint32_t gap;
  if (!contact)                          gap = 100u;                         // 走査中：100ms保留再送（在圏復帰の瞬間に届く）
  else if (s_pend_tries < CMD_FAST_TRIES) gap = 40u + (esp_random() & 31u);  // 在圏：最初は40〜71ms
  else                                   gap = CMD_SLOW_GAP_MS;              // 在圏：以降120ms
  if (s_pend_last != 0 && (now - s_pend_last) < gap) return;
  send_command(s_pend_code, s_pend_nonce, /*shots=*/1);
  if (s_pend_seq_n < CMD_SEQ_N) s_pend_seqs[s_pend_seq_n++] = mesh::last_seq();   // 送ったseqを記録（ACK照合用）
  s_pend_last = now;
  s_pend_tries++;
}

// ★20261109：READY表示LED更新（常時起動版のみ）。GWの在席ビーコンが届いていて(READY_STALE_MS内)、
//   且つそのreadyが1のときだけ点灯。GW不明(無受信)・計測中(ON TRACK)・ARMED/GREEN/FINISH・
//   エラー画面・感知不能ラッチ中は消灯＝「いま赤を押してもシグナルは切れない」が一目で分かる。
static void tick_ready_led() {
  bool fresh = (s_ready_rx_ms != 0) && (millis() - s_ready_rx_ms < READY_STALE_MS);
  digitalWrite(PIN_READY_LED, (fresh && s_gw_ready) ? HIGH : LOW);
}

void setup() {
  Serial.setTxTimeoutMs(0);   // ★ホスト未読でもブロックしない（XIAO CDC：モニタ未接続時に println が数十ms止まる対策）
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n=== M4LAPS Remote RC%d ===\n", NODE_ID);
  pinMode(PIN_RED,  INPUT_PULLUP);
  pinMode(PIN_GRAY, INPUT_PULLUP);
  pinMode(PIN_READY_LED, OUTPUT); digitalWrite(PIN_READY_LED, LOW);   // ★20261109 READY表示LED（初期は消灯）
  uint8_t ch0 = chfollow::initial_channel(ESPNOW_CHANNEL);
  if (!mesh::begin(NODE_ID, ch0, on_recv)) { Serial.println("ESP-NOW init 失敗"); return; }
  Serial.printf("稼働開始（ch=%d・GWへ自動追従）。赤=SIGNAL / 灰=RESET。READY LED=GPIO%d。\n", ch0, PIN_READY_LED);
}

void loop() {
  chfollow::tick(send_join);
  tick_presence();
  if (s_red.pressed())  { fire(proto::CMD_SIGNAL); Serial.println("[BTN] SIGNAL"); }
  if (s_gray.pressed()) { fire(proto::CMD_RESET);  Serial.println("[BTN] RESET");  }
  service_cmd();              // ★ACK待ち＋時間差再送（ノンブロッキング）
  tick_ready_led();           // ★20261109：GWのREADYを手元LEDへ反映（GW在席ビーコンのreadyバイト）
}
#endif
