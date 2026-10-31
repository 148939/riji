Option Explicit
Dim sh, fso, here, root, py
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(here)
py = "D:\Anaconda\pythonw.exe"
If Not fso.FileExists(py) Then py = "pythonw.exe"
sh.CurrentDirectory = root
sh.Run """" & py & """ """ & root & "\app\server.py"" --data """ & root & "\data"" --port 8802 --lan --app", 0, False