Option Explicit
Dim oShell, strDir, cmd

Set oShell = CreateObject("WScript.Shell")
strDir = oShell.ExpandEnvironmentStrings("%USERPROFILE%") & "\Documents\miniyonku_app\"

' WindowStyle=1: 通常表示（最前面・標準サイズ）
' start.bat を直接呼び出す（コンソールウィンドウが確実に表示される）
cmd = "cmd /c """ & strDir & "start\start.bat"""
oShell.Run cmd, 1, False
