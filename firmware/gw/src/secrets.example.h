// firmware/gw/src/secrets.example.h  → secrets.h にコピーして編集（.gitignore）
#pragma once
#define WIFI_SSID      "your-ssid"
#define WIFI_PASS      "your-pass"
// クラウド（ConoHa）側。M4LAPSはクラウド版限定・ライセンス必須（docs/17.3）
#define SERVER_BASE    "https://v133-117-77-69.sefs.static.cnode.jp"
#define TIMING_TOKEN   ""        // サーバTIMING_TOKEN未設定なら空でよい（docs/19.7）
