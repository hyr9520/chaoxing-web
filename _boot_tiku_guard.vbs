' Launch tiku guard hidden (no console window).
' Keeps 8060 alive: restarts tikuAdapter.exe whenever it goes down.
'
' IMPORTANT 1: keep this file PURE ASCII. Windows Script Host parses .vbs as
'   ANSI, so UTF-8 non-ASCII bytes corrupt the script (error 800A0401).
'   That is why the python path is built from %USERPROFILE% instead of being
'   written out - the real user folder name contains non-ASCII characters.
' IMPORTANT 2: pythonw.exe (not python.exe) so no console window appears.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = "D:\work\2026-09-09-13-50-17\chaoxing"
py = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.workbuddy\binaries\python\envs\chaoxing\Scripts\pythonw.exe"

If Not fso.FileExists(py) Then
    MsgBox "pythonw.exe not found: " & py, 16, "tiku guard"
    WScript.Quit 1
End If

sh.CurrentDirectory = base
sh.Run """" & py & """ """ & base & "\_tiku_guard.py""", 0, False
