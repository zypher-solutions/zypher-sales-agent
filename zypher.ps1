# One-command launcher. Run from the project folder:  .\zypher.ps1 <command>
#
#   .\zypher.ps1 status                                    where every lead is, what needs you
#   .\zypher.ps1 find "freight forwarding company" "Dubai, United Arab Emirates" 20
#                                                          find new companies on Google Maps (costs Apify credit)
#   .\zypher.ps1 draft 5                                   research 5 leads, write emails, send cards to Telegram
#   .\zypher.ps1 bot                                       listen for your Telegram taps (leave this window open)
#   .\zypher.ps1 refresh                                   rewrite drafts that are still waiting, with the latest rules
#   .\zypher.ps1 preflight                                 check DNS and mailbox login before the first send (sends nothing)
#   .\zypher.ps1 start                                     RUN EVERYTHING in one window (buttons, sending, inbox, follow-ups)
#                                                          (only one copy runs: while the server runs it, this one waits)
#   .\zypher.ps1 send                                      one sending pass now (only when SENDING_ENABLED=true)
#   .\zypher.ps1 worker                                    keep sending as the rules allow (leave open)
#   .\zypher.ps1 stats                                     emails sent, replies, bounces, opt-outs and what they mean
#   .\zypher.ps1 inbox                                     read the mailbox once for bounces and replies to YOUR emails
#   .\zypher.ps1 health                                    are the running agent's loops alive (Docker uses this)
#   .\zypher.ps1 test                                      run the self-checks
#   .\zypher.ps1 demo                                      the whole pipeline on fake data (free, no keys)
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(Position = 1)][string]$A,
    [Parameter(Position = 2)][string]$B,
    [Parameter(Position = 3)][int]$N = 20
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "Virtual environment missing. Run: python -m venv .venv ; .\.venv\Scripts\pip install -e '.[dev]'"; exit 1 }

switch ($Command) {
    "status"  { & $py -m zypher_agent status }
    "find"    {
        if (-not $A -or -not $B) { Write-Host 'Usage: .\zypher.ps1 find "freight forwarding company" "Dubai, United Arab Emirates" 20'; exit 1 }
        & $py -m zypher_agent source --query $A --location $B --max $N
    }
    "draft"   { $limit = if ($A) { [int]$A } else { 5 }; & $py -m zypher_agent run --limit $limit }
    "bot"     { & $py -m zypher_agent bot }
    "refresh" { & $py -m zypher_agent refresh }
    "preflight" { & $py -m zypher_agent preflight }
    "start"   { & $py -m zypher_agent start }
    "send"    { & $py -m zypher_agent send }
    "worker"  { & $py -m zypher_agent worker }
    "stats"   { & $py -m zypher_agent stats }
    "inbox"   { & $py -m zypher_agent inbox }
    "health"  { & $py -m zypher_agent health }
    "test"    { & $py -m pytest -q }
    "demo"    { & $py -m zypher_agent run --dry-run }
    default   { Get-Content $PSCommandPath -TotalCount 18 | Select-Object -Skip 1 | ForEach-Object { $_ -replace '^# ?', '' } }
}
