$p = 'D:\firmware\gw\src\main.cpp'
$lines = Get-Content $p -Encoding UTF8
$out = @()
$removed = $false
foreach ($ln in $lines) {
  if (-not $removed -and $ln -eq '}') { $removed = $true; continue }
  $out += $ln
}
Set-Content $p $out -Encoding UTF8