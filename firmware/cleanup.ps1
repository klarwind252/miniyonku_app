$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8

$t = $t.Replace('  Serial.println("*** DEBUG BUILD v2: WiFi OFF / ESP-NOW isolation test ***");
', '')
$t = $t.Replace('  Serial.printf("[FS] enter exists=%d race=%u\n", (int)LittleFS.exists(SPOOL), s_race_id);
', '')
$t = $t.Replace('  Serial.printf("[FS] read n=%d size=%u\n", n, (unsigned)LittleFS.open(SPOOL, FILE_READ).size());
', '')
$t = $t.Replace('  Serial.printf("[ER] wifi=%d\n", (int)(WiFi.status() == WL_CONNECTED));
', '')
$t = $t.Replace('  Serial.printf("[ER] race_id=%u\n", s_race_id);
', '')
$t = $t.Replace('  Serial.printf("[RACE] POST code=%d\n", code);
', '')
$t = $t.Replace('    if (n >= 100) break;', '    if (n >= 100) break;  // cap per POST: avoid RAM exhaustion on huge spool')

Set-Content $p $t -Encoding UTF8 -NoNewline