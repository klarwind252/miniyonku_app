$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8
$find = '  beam::Hit hit;
  while (beam::poll(hit)) {'
$repl = '  beam::Hit hit;
  int _drain = 0;
  while (_drain++ < 8 && beam::poll(hit)) {'
$t = $t.Replace($find, $repl)
Set-Content $p $t -Encoding UTF8 -NoNewline