$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8
$t = $t -replace 'static WiFiClientSecure s_tls;\r?\n', ''
$t = $t -replace 'static bool s_tls_init = false;\r?\n', ''
$t = $t -replace 'static WiFiClientSecure& tls\(\)[^\n]*\n', ''
Set-Content $p $t -Encoding UTF8 -NoNewline