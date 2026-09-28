' AI Teaches Me —— 双击启动（无控制台窗口）
Option Explicit
Dim shell, fso, root, pythonw, venv, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)

venv = root & "\.venv\Scripts\pythonw.exe"
If fso.FileExists(venv) Then
    pythonw = venv
ElseIf fso.FileExists("D:\Python\Python\pythonw.exe") Then
    pythonw = "D:\Python\Python\pythonw.exe"
Else
    pythonw = "pythonw.exe"
End If

shell.CurrentDirectory = root
cmd = """" & pythonw & """ -m app.main"
shell.Run cmd, 0, False
