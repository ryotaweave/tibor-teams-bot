# Runs the TIBOR bot on this PC (Windows Task Scheduler entry point).
#
# The webhook URL lives OUTSIDE the git repo, in a local-only secrets file:
#   %USERPROFILE%\.tibor_webhook.txt
# The de-dup state also lives outside the repo so local runs and the GitHub
# Actions runs stay independent (each posts a given rate at most once; the
# first one to see a new rate wins, and Teams gets exactly one message per day
# as long as one of them succeeds).

$ErrorActionPreference = 'Stop'

$repo   = 'C:\Users\ryota\tibor-teams-bot'
$python = 'C:\Users\ryota\AppData\Local\Programs\Python\Python312\python.exe'
$secret = Join-Path $env:USERPROFILE '.tibor_webhook.txt'
$state  = Join-Path $env:USERPROFILE '.tibor_state\last_posted.txt'
$logdir = Join-Path $env:USERPROFILE '.tibor_state'
$log    = Join-Path $logdir 'run.log'

New-Item -ItemType Directory -Force -Path $logdir | Out-Null

if (-not (Test-Path $secret)) {
    "$(Get-Date -Format s) ERROR: webhook file not found at $secret" | Add-Content -Encoding utf8 $log
    exit 1
}

$env:TEAMS_WEBHOOK_URL = (Get-Content -Raw $secret).Trim()
$env:STATE_FILE        = $state
$env:PYTHONIOENCODING  = 'utf-8'

# Keep Japanese text readable in run.log (PowerShell 5.1 pipes native output
# through the console codepage, which mangles UTF-8 otherwise).
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Set-Location $repo

"$(Get-Date -Format s) --- run start ---" | Add-Content -Encoding utf8 $log
try {
    $out = & $python 'tibor_bot.py' 2>&1
    $out | Add-Content -Encoding utf8 $log
    "$(Get-Date -Format s) --- exit $LASTEXITCODE ---" | Add-Content -Encoding utf8 $log
    exit $LASTEXITCODE
} catch {
    "$(Get-Date -Format s) EXCEPTION: $_" | Add-Content -Encoding utf8 $log
    exit 1
}
