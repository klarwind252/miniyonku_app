$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8

$bad = @'
if (WiFi.status() == WL_CONNECTED) {
    IPAddress dns1(8,8,8,8), dns2(1,1,1,1);
    WiFi.setDNS(dns1, dns2);
  }
  return WiFi.status() == WL_CONNECTED;
'@
$good = @'
if (WiFi.status() == WL_CONNECTED) {
    ip_addr_t d1, d2;
    ipaddr_aton("8.8.8.8", &d1);
    ipaddr_aton("1.1.1.1", &d2);
    dns_setserver(0, &d1);
    dns_setserver(1, &d2);
  }
  return WiFi.status() == WL_CONNECTED;
'@
$t = $t -replace [regex]::Escape($bad), $good

# lwip/dns.h を include（無ければ追加）
if ($t -notmatch 'lwip/dns\.h') {
  $t = $t -replace '#include <WiFiClientSecure\.h>', "#include <WiFiClientSecure.h>`r`n#include `"lwip/dns.h`"`r`n#include `"lwip/ip_addr.h`""
}

Set-Content $p $t -Encoding UTF8 -NoNewline