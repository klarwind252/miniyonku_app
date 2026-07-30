$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8
$ins = @'
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
static WiFiClientSecure s_tls;
static bool s_tls_init = false;
static WiFiClientSecure& tls() { if (!s_tls_init) { s_tls.setInsecure(); s_tls_init = true; } return s_tls; }
'@
$t = $t -replace '#include <HTTPClient\.h>', $ins
$t = $t -replace 'http\.begin\(String\(SERVER_BASE\)', 'http.begin(tls(), String(SERVER_BASE)'
Set-Content $p $t -Encoding UTF8 -NoNewline