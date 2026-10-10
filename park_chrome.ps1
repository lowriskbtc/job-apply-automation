# Keeps the jobsearch debug-Chrome window OFF-SCREEN instead of minimized.
#
# Why: minimizing is not enough. Every new tab a run opens makes Chrome restore
# and raise the window, so a 10-job run pops up in your face 10 times. Moving the
# window to negative coordinates means "raise" has nothing to raise into view -
# it stays parked off the edge of the display while still rendering at full speed
# (that's what the --disable-*-backgrounding flags on the launch are for; a
# minimized window would get throttled, an off-screen one does not).
#
#   powershell -File park_chrome.ps1            # park once
#   powershell -File park_chrome.ps1 -Watch     # park and keep it parked
#   powershell -File show_chrome.ps1            # bring it back when you submit
#
# Only touches the Chrome started on jobsearch\chrome-profile. Your normal
# Chrome and the solseller popout are matched by user-data-dir and left alone.

param(
    [switch]$Watch,
    [int]$Seconds = 3600
)

$ErrorActionPreference = 'Stop'

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class Win {
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }

    public static System.Collections.Generic.List<IntPtr> TopLevel(uint[] pids) {
        var found = new System.Collections.Generic.List<IntPtr>();
        EnumWindows(delegate(IntPtr h, IntPtr l) {
            uint p; GetWindowThreadProcessId(h, out p);
            if (Array.IndexOf(pids, p) >= 0 && IsWindowVisible(h) && GetWindowTextLength(h) > 0) found.Add(h);
            return true;
        }, IntPtr.Zero);
        return found;
    }
}
"@

# SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE - move only, never steal focus.
$SWP = 0x0001 -bor 0x0004 -bor 0x0010
$PARK_X = -3200
$PARK_Y = -2400
$SW_RESTORE = 9

function Get-DebugChromePids {
    Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*jobsearch\chrome-profile*' -and $_.CommandLine -notlike '*--type=*' } |
        Select-Object -ExpandProperty ProcessId
}

function Park {
    $pids = @(Get-DebugChromePids)
    if ($pids.Count -eq 0) { return 0 }
    $moved = 0
    foreach ($h in [Win]::TopLevel([uint32[]]$pids)) {
        $r = New-Object Win+RECT
        [void][Win]::GetWindowRect($h, [ref]$r)
        if ($r.Left -ne $PARK_X -or $r.Top -ne $PARK_Y) {
            # A minimized window reports bogus coords and ignores moves; restore
            # it off-screen first so the move actually lands.
            [void][Win]::ShowWindow($h, $SW_RESTORE)
            [void][Win]::SetWindowPos($h, [IntPtr]::Zero, $PARK_X, $PARK_Y, 0, 0, $SWP)
            $moved++
        }
    }
    return $moved
}

if (-not $Watch) {
    $n = Park
    Write-Host "parked $n window(s) off-screen at $PARK_X,$PARK_Y"
    exit 0
}

Write-Host "watching - re-parks the debug Chrome window whenever it comes back on-screen"
Write-Host "run show_chrome.ps1 to bring it back; this exits after $Seconds s"
$deadline = (Get-Date).AddSeconds($Seconds)
$total = 0
while ((Get-Date) -lt $deadline) {
    $total += Park
    Start-Sleep -Milliseconds 400
}
Write-Host "watcher done - re-parked $total time(s)"
