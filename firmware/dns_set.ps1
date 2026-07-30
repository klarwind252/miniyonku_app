$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8
$anchor = 'return WiFi.status() == WL_CONNECTED;'
$add = @'
if (WiFi.status() == WL_CONNECTED) {
    IPAddress dns1(8,8,8,8), dns2(1,1,1,1);
    WiFi.setDNS(dns1, dns2);
  }
  return WiFi.status() == WL_CONNECTED;
'@
$t = $t -replace [regex]::Escape($anchor), $add
Set-Content $p $t -Encoding UTF8 -NoNewline