$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8

# dns_pin ヘルパを削除
$t = $t -replace '(?s)#include "lwip/dns\.h".*?static void dns_pin.*?\}\r?\n', ''
# lwip include 残骸を削除
$t = $t -replace '#include "lwip/ip_addr\.h"\r?\n', ''
# wifi_up 内の dns_pin 呼び出しを削除
$t = $t -replace '  dns_pin\([^\n]*\);\r?\n', ''

Set-Content $p $t -Encoding UTF8 -NoNewline