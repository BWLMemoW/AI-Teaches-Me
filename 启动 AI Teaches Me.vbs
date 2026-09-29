' AI Teaches Me —— 双击启动（pythonw 无需控制台；窗口样式必须用 1=正常显示，用 0 会把主窗口一起隐藏）
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
shell.Run cmd, 1, False
