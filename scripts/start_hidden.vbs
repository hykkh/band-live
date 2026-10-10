' band-live hidden launcher (no console window at all).
' Task BandLive_AutoStart -> wscript.exe start_hidden.vbs -> python.exe app.py (SW_HIDE)
' Logs: C:\H-Programs\band-live\data\logs\band_YYYYMMDD.log
' Duplicate runs exit by themselves (app.py holds port 7960).
Set ws = CreateObject("Wscript.Shell")
ws.CurrentDirectory = "C:\H-Programs\band-live"
ws.Run """C:\Python312\python.exe"" ""C:\H-Programs\band-live\app.py""", 0, False
