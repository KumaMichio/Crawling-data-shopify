# Weekly crawl of all stores in stores.csv. Called by Windows Task Scheduler.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m mahjong_crawler run
exit $LASTEXITCODE
