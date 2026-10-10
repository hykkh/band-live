# Register BandLive_AutoStart as a hidden launch (wscript -> python, no console). RunLevel Limited.
# Stops only the running band-live copy (process listening on port 7960) so exactly one remains.
$log = "C:\H-Programs\band-live\data\register_task.log"
$old = @(Get-NetTCPConnection -State Listen -LocalPort 7960 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
$old | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
$action  = New-ScheduledTaskAction -Execute "wscript.exe" -Argument '"C:\H-Programs\band-live\scripts\start_hidden.vbs"'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:COMPUTERNAME\$env:USERNAME"
$trigger.Delay = "PT1M"
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances Parallel
$pr  = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName BandLive_AutoStart -Action $action -Trigger $trigger -Settings $set -Principal $pr -Force | Out-Null
Start-ScheduledTask -TaskName BandLive_AutoStart
Start-Sleep -Seconds 6
$now = @(Get-NetTCPConnection -State Listen -LocalPort 7960 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
"$(Get-Date -Format s) stopped=$($old.Count) running=$($now.Count) pids=$($now -join ',')" | Out-File -FilePath $log -Encoding utf8 -Append
