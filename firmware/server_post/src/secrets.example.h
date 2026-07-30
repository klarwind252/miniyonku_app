// ============================================================================
//  secrets.example.h → 同じ場所に secrets.h という名前でコピーして値を入れる
//  ⚠ secrets.h は自分のWiFi情報。人に渡さない／GitHubに上げない。
// ============================================================================
#pragma once

// 自宅WiFiの名前とパスワード（2.4GHz帯のSSIDを使うこと。ESP32は5GHz非対応）
#define WIFI_SSID   "SSID"
#define WIFI_PASS   "PASS"

// 送り先サーバー（Vultrテスト）。末尾スラッシュなし。
#define SERVER_BASE "https://66.245.220.187"
