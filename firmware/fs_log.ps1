$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8
$find = 'static void flush_spool() {
  if (!LittleFS.exists(SPOOL)) return;'
$repl = 'static void flush_spool() {
  Serial.printf("[FS] enter exists=%d race=%u\n", (int)LittleFS.exists(SPOOL), s_race_id);
  if (!LittleFS.exists(SPOOL)) return;'
$t = $t.Replace($find, $repl)
Set-Content $p $t -Encoding UTF8 -NoNewline