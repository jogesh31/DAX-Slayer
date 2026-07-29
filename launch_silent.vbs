' Runs launch_external_tool.bat with a fully hidden window (no console
' flash) — cmd.exe run directly always shows a window even when the batch
' file itself only launches pythonw.exe, because cmd.exe IS the visible
' process. WScript.Shell.Run's windowStyle=0 hides that outer window too.
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = scriptDir & "\launch_external_tool.bat"

server = ""
database = ""
If WScript.Arguments.Count >= 1 Then server = WScript.Arguments(0)
If WScript.Arguments.Count >= 2 Then database = WScript.Arguments(1)

cmd = "cmd /c """"" & batPath & """ """ & server & """ """ & database & """"""

Set objShell = CreateObject("WScript.Shell")
objShell.Run cmd, 0, False
