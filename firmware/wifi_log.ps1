$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8
$find = 'static bool ensure_race() {
  if (s_race_id) return true;
  s_race_id = create_race(/*with_green=*/false, 0);
  return s_race_id != 0;
}'
$repl = 'static bool ensure_race() {
  if (s_race_id) return true;
  Serial.printf("[ER] wifi=%d\n", (int)(WiFi.status() == WL_CONNECTED));
  s_race_id = create_race(/*with_green=*/false, 0);
  Serial.printf("[ER] race_id=%u\n", s_race_id);
  return s_race_id != 0;
}'
$t = $t.Replace($find, $repl)
Set-Content $p $t -Encoding UTF8 -NoNewline