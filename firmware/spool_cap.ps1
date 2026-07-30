$p = 'D:\firmware\gw\src\main.cpp'
$t = Get-Content $p -Raw -Encoding UTF8
$find = '    arr.add(ev);
    n++;
  }
  f.close();'
$repl = '    arr.add(ev);
    n++;
    if (n >= 100) break;
  }
  f.close();
  Serial.printf("[FS] read n=%d size=%u\n", n, (unsigned)LittleFS.open(SPOOL, FILE_READ).size());'
$t = $t.Replace($find, $repl)
Set-Content $p $t -Encoding UTF8 -NoNewline