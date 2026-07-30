$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8
$find = '  int code = http.POST(body);
  if (code == 200) {
    JsonDocument res;
    if (deserializeJson(res, http.getString()) == DeserializationError::Ok)
      rid = res["race_id"] | 0;
  }'
$repl = '  int code = http.POST(body);
  Serial.printf("[RACE] POST code=%d\n", code);
  if (code == 200) {
    JsonDocument res;
    if (deserializeJson(res, http.getString()) == DeserializationError::Ok)
      rid = res["race_id"] | 0;
  }'
$t = $t.Replace($find, $repl)
Set-Content $p $t -Encoding UTF8 -NoNewline