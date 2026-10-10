# Starts Chrome with remote debugging so autofill.py can attach to it.
#
# Chrome 136+ refuses --remote-debugging-port on your normal profile, so this
# uses a dedicated profile folder next to this script. First run you'll need to
# sign in to jobright (and Google/LinkedIn if you want) inside THIS window -
# after that the logins stick.

param(
    [int]$Port = 9222,
    # Chrome starts MINIMIZED by default. A queue run opens a tab per job, and
    # each new tab raises the window - so a 17-job run steals focus 17 times
    # while you are trying to work. Pass -Show if you want to watch it.
    [switch]$Show
)

$ErrorActionPreference = 'Stop'
$profileDir = Join-Path $PSScriptRoot 'chrome-profile'

if (-not (Test-Path $profileDir)) {
    New-Item -ItemType Directory -Path $profileDir | Out-Null
}

# Already listening? Don't start a second one.
$live = Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
if ($live) {
    Write-Host "Chrome already listening on port $Port."
    exit 0
}

$candidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chrome = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) {
    throw "chrome.exe not found. Edit `$candidates in this script."
}

Write-Host "Launching Chrome on port $Port"
Write-Host "  profile: $profileDir"

$chromeArgs = @(
    "--remote-debugging-port=$Port",
    "--user-data-dir=`"$profileDir`"",
    "--no-first-run",
    "--no-default-browser-check",
    # A minimized or covered window gets its timers and rendering throttled by
    # Chrome, which makes a form take longer to settle than the script waits
    # for - the run would start failing purely because you looked away. These
    # three keep a background window running at normal speed.
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "https://jobright.ai/jobs/recommend"
)

$style = if ($Show) { 'Normal' } else { 'Minimized' }
Start-Process -FilePath $chrome -ArgumentList $chromeArgs -WindowStyle $style
if (-not $Show) {
    Write-Host "  started minimized - restore it from the taskbar when you want"
    Write-Host "  to finish the tabs the run leaves open. Use -Show to watch it."
}

Start-Sleep -Seconds 3
$live = Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
if ($live) {
    Write-Host "Ready. Leave this Chrome window open, then run autofill.py."
} else {
    Write-Warning "Port $Port is not listening yet - give it a few seconds and re-check."
}
