// ============================================================================
//  server_post — GW→クラウド 送信確認ファーム（検証専用・GW1枚で完結）
//  目的: GWがWiFi経由でサーバーへ「レース開始」→「通過イベント」をPOSTできるか確認。
//        → 設計書 13.3 段階C（サーバーへのバッチ送信）の最小版
//
//  流れ:
//    1) 自宅WiFiに接続
//    2) POST /api/timing/races        {"target_laps":3}          → race_id を受領
//    3) POST /api/timing/races/{id}/events  疑似イベント数件      → inserted を確認
//
//  ⚠ センサー未接続のため通過データは疑似（S/G→G1→G2 を1周ぶん×数周）。
//     HTTPSはサーバー証明書を検証せず接続（検証用・自己署名/警告環境のため）。
// ============================================================================
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include "secrets.h"     // WIFI_SSID / WIFI_PASS / SERVER_BASE

static const char* BASE = SERVER_BASE;

// --- 簡易POST（JSON本文を送り、ステータスと本文を返す） ----------------------
static int postJson(const String& url, const String& body, String& outResp) {
  WiFiClientSecure client;
  client.setInsecure();               // 検証用：証明書を検証しない
  HTTPClient http;
  if (!http.begin(client, url)) { outResp = "begin失敗"; return -1; }
  http.addHeader("Content-Type", "application/json");
  int code = http.POST((uint8_t*)body.c_str(), body.length());
  outResp = http.getString();
  http.end();
  return code;
}

// 本文から "race_id": 数字 を素朴に取り出す
static long extractRaceId(const String& s) {
  int k = s.indexOf("race_id");
  if (k < 0) return -1;
  int c = s.indexOf(':', k);
  if (c < 0) return -1;
  long v = 0; bool got = false;
  for (int i = c + 1; i < (int)s.length(); i++) {
    char ch = s[i];
    if (ch >= '0' && ch <= '9') { v = v * 10 + (ch - '0'); got = true; }
    else if (got) break;
  }
  return got ? v : -1;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== GW→クラウド 送信確認 (server_post) ===");

  // --- 1) WiFi接続 ---
  Serial.printf("WiFi接続中: %s ", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(500); Serial.print(".");
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\n✗ WiFiに接続できませんでした。SSID/パスワード/2.4GHzを確認。");
    return;
  }
  Serial.printf("\n✓ 接続成功  IP=%s\n", WiFi.localIP().toString().c_str());

  // --- 2) レース開始 ---
  String resp;
  String urlRace = String(BASE) + "/api/timing/races";
  int code = postJson(urlRace, "{\"target_laps\":3}", resp);
  Serial.printf("[レース開始] HTTP %d  応答: %s\n", code, resp.c_str());
  if (code != 200) { Serial.println("✗ レース開始に失敗。URL/サーバー状態を確認。"); return; }

  long race_id = extractRaceId(resp);
  if (race_id < 0) { Serial.println("✗ race_id が取れませんでした。"); return; }
  Serial.printf("✓ race_id = %ld を受領\n", race_id);

  // --- 3) 疑似イベントを送信（3周ぶん・S/G=gate0想定、lane1） ---
  //   device_id: GWのMAC下位を流用 / src=6(GW想定) / boot_id 固定 / seq連番
  //   t_us は 35秒/周 で increment（01のコース諸元と整合）
  String events = "{\"events\":[";
  const uint32_t boot_id = 12345;
  uint64_t t = 1000000;                 // 1秒地点から開始（適当）
  int seq = 1;
  bool first = true;
  for (int lap = 0; lap < 3; lap++) {
    // S/G 通過（lane1）を1周1回ぶん
    if (!first) events += ",";
    first = false;
    events += "{\"device_id\":6,\"src\":6,\"src_boot_id\":" + String(boot_id) +
              ",\"seq\":" + String(seq++) +
              ",\"lane\":1,\"t_us\":" + String((unsigned long)(t)) +
              ",\"quality\":0}";
    t += 35000000ULL;                    // +35秒
  }
  events += "]}";

  String urlEv = String(BASE) + "/api/timing/races/" + String(race_id) + "/events";
  String resp2;
  int code2 = postJson(urlEv, events, resp2);
  Serial.printf("[イベント送信] HTTP %d  応答: %s\n", code2, resp2.c_str());
  if (code2 == 200) Serial.println("✓✓ 送信成功！ サーバーに届きました。");
  else              Serial.println("✗ イベント送信に失敗。");
}

void loop() {
  delay(5000);
  Serial.println("（完了。もう一度試すにはリセットボタンを押す）");
}
