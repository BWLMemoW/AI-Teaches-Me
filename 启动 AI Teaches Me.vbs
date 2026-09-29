' AI Teaches Me launcher.
' NOTE: Windows Script Host reads .vbs as ANSI, so this file is kept PURE ASCII.
' Never use window style 0 here: it hides Qt's main window (SW_HIDE).
Option Explicit
Dim shell, fso, root, pythonw, venv, cmd, wmi, procs, proc, pid
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)

' 1) stop previous instances, so a double-click always runs the latest code
On Error Resume Next
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
If Not IsEmpty(wmi) Then
    Set procs = wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name LIKE 'python%'")
    For Each proc In procs
        If Not IsNull(proc.CommandLine) Then
            If InStr(proc.CommandLine, "app.main") > 0 Then
                pid = proc.ProcessId
                shell.Run "taskkill /PID " & pid & " /F", 0, True
            End If
        End If
    Next
End If
Err.Clear
On Error GoTo 0

' 2) choose pythonw: project venv first, then system Python
venv = root & "\.venv\Scripts\pythonw.exe"
If fso.FileExists(venv) Then
    pythonw = venv
ElseIf fso.FileExists("D:\Python\Python\pythonw.exe") Then
    pythonw = "D:\Python\Python\pythonw.exe"
Else
    pythonw = "pythonw.exe"
End If

' 3) launch (window style 1 = normal)
shell.CurrentDirectory = root
cmd = """" & pythonw & """ -m app.main"
shell.Run cmd, 1, False