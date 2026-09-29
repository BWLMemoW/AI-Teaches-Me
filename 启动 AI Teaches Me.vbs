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

' 先关掉正在运行的旧实例，保证双击后一定是最新代码
Dim wmi, procs, proc
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name LIKE 'python%'")
For Each proc In procs
    If Not IsNull(proc.CommandLine) Then
        If InStr(proc.CommandLine, "app.main") > 0 Then
            shell.Run "taskkill /PID " & proc.ProcessId & " /F", 0, True
        End If
    End If
Next
shell.CurrentDirectory = root
cmd = """" & pythonw & """ -m app.main"
shell.Run cmd, 1, False
