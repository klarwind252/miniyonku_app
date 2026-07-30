$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8

# addHeader("Content-Type"... の各所の直後に Host ヘッダを追加する代わりに、
# begin 後すぐ Host を足す。HTTPClient は URL の host を Host に使うが、
# IP URL では IP が入るので、明示的に上書きする。
$t = $t -replace '(WiFiClientSecure _c; _c\.setInsecure\(\); http\.begin\(_c, )String\(SERVER_BASE\)', '$1String("https://133.117.77.69")'
$t = $t -replace '(http\.addHeader\("Content-Type", "application/json"\);)', '$1' + [Environment]::NewLine + '  http.addHeader("Host", "v133-117-77-69.sefs.static.cnode.jp");'

Set-Content $p $t -Encoding UTF8 -NoNewline