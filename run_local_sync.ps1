# Runs the two chains that GitHub's cloud runners cannot reach (laibcatalog.co.il
# only answers to Israeli IP addresses, and silently drops everything else instead
# of rejecting it — every cloud attempt just times out).
#
# This machine already has an Israeli IP, so it needs no workaround: it calls the
# same sync_prices.py the cloud job uses, restricted to just these two chains.
#
# Scheduled via Windows Task Scheduler (task name: HomeQuestPricesLocalSync) to run
# daily and to fire as soon as the PC is next on if it was off at the scheduled time
# ("start when available" is set on the task, not here).

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# .env holds the real Supabase key and is git-ignored — never commit it, this repo is public.
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([A-Z_]+)=(.*)$') {
        [System.Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}

$env:SYNC_CHAINS = "VICTORY_NEW_SOURCE,MAHSANI_ASHUK_NEW_SOURCE"
# 40, not 100: this bypass path throttles itself (0.3s/request) to stay polite to
# the host, and a heavier request burst here is plausibly what made the very next
# chain's listing come back empty during testing. 40 still matches the cloud chains'
# coverage level.
$env:FILES_PER_CHAIN = "40"

$logFile = Join-Path $PSScriptRoot "run_local_sync.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"===== $timestamp =====" | Out-File -Append -Encoding utf8 $logFile

python sync_prices.py *>> $logFile
$exitCode = $LASTEXITCODE

"exit code: $exitCode" | Out-File -Append -Encoding utf8 $logFile
exit $exitCode
