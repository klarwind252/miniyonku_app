$p = 'D:\firmware\gw\src\main.cpp'
$nl = [Environment]::NewLine
$find = '  http.addHeader("Content-Type", "application/json");'
$repl = $find + $nl + '  http.addHeader("Host", "v133-117-77-69.sefs.static.cnode.jp");'
$t = Get-Content $p -Raw -Encoding UTF8
$t = $t.Replace($find, $repl)
Set-Content $p $t -Encoding UTF8 -NoNewline