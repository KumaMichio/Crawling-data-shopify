# Registers a Windows scheduled task that runs the crawler every Monday at 02:00.
# Run once:   powershell -ExecutionPolicy Bypass -File .\schedule_weekly.ps1
# Remove:     Unregister-ScheduledTask -TaskName "MahjongStoreCrawl" -Confirm:$false
param(
    [string]$Day = "Monday",
    [string]$Time = "02:00"
)
$script = Join-Path $PSScriptRoot "run_weekly.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 23) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "MahjongStoreCrawl" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Weekly crawl of mahjong stores (products, images, reviews, rankings)" -Force
Write-Host "Scheduled: every $Day at $Time (runs late if the PC was off)."
