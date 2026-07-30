$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8

$inc = @'
#include <WiFiClientSecure.h>
#include "lwip/dns.h"
#include "lwip/ip_addr.h"
static void dns_pin(const char* host, const char* ip) {
  ip_addr_t a; ipaddr_aton(ip, &a);
  ip_addr_t r;
  if (dns_gethostbyname(host, &r, NULL, NULL) != ERR_OK) {
    dns_local_addhost(host, &a);
  }
}
'@
$t = $t -replace '#include <WiFiClientSecure\.h>', $inc

$t = $t -replace '(return WiFi\.status\(\) == WL_CONNECTED;)', "dns_pin(`"v133-117-77-69.sefs.static.cnode.jp`", `"133.117.77.69`");`r`n  `$1"

Set-Content $p $t -Encoding UTF8 -NoNewline