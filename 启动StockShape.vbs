Option Explicit
Dim shell, fso, root, executable
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
executable = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(executable) Then
    MsgBox "Python environment missing. Please install requirements-desktop.txt.", 48, "StockShape"
    WScript.Quit 1
End If
shell.Run Chr(34) & executable & Chr(34) & " " & Chr(34) & root & "\start_desktop.py" & Chr(34), 0, False
