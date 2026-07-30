$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8

# 11: PSRAM line - match by ASCII part only, replace whole printf line
$t = $t -replace 'Serial\.printf\("PSRAM=%u[^"]*"', 'Serial.printf("PSRAM=%u (VE normal: approx 4MB mapped of 8MB)\n"'

# 12a: enable fetch_layout - match by ASCII prefix, kill trailing comment
$t = $t -replace '// fetch_layout\(\);[^\r\n]*', 'fetch_layout();'

# 12b: add Host header to fetch_layout (only place where layouts URL directly precedes TOKEN line)
$find = '"/for_gw");
  if (strlen(TIMING_TOKEN))'
$repl = '"/for_gw");
  http.addHeader("Host", "v133-117-77-69.sefs.static.cnode.jp");
  if (strlen(TIMING_TOKEN))'
$t = $t.Replace($find, $repl)

Set-Content $p $t -Encoding UTF8 -NoNewline