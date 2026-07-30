$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8
$t = $t -replace 'http\.begin\(tls\(\), ', 'WiFiClientSecure _c; _c.setInsecure(); http.begin(_c, '
Set-Content $p $t -Encoding UTF8 -NoNewline