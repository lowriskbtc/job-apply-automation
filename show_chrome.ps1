# Brings the parked jobsearch debug-Chrome window back on-screen.
#
# park_chrome.ps1 moves it to negative coordinates so a run can't pop it in your
# face. It is still running with every filled tab intact - this just puts it back
# where you can see it, for the part only you can do: clearing each CAPTCHA and
# hitting submit.
#
#   powershell -File show_chrome.ps1
#
# Stop the watcher first if one is running, or it will park the window again
# while you are working in it.

$ErrorActionPreference = 'Stop'

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class WinShow {
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);

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

$SWP_NOZORDER = 0x0004
$SW_RESTORE = 9

# Fit the actual work area (screen minus taskbar). A hardcoded 1500x1000 hangs
# off the right and bottom of a 1536x960 display, which puts the window controls
# where you cannot reach them.
Add-Type -AssemblyName System.Windows.Forms
$wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$W = [Math]::Min(1500, $wa.Width - 16)
$H = [Math]::Min(1000, $wa.Height - 16)
$X = $wa.X + 8
$Y = $wa.Y + 8

$procIds = @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*jobsearch\chrome-profile*' -and $_.CommandLine -notlike '*--type=*' } |
    Select-Object -ExpandProperty ProcessId)

if ($procIds.Count -eq 0) {
    Write-Warning "the jobsearch debug Chrome is not running - nothing to show"
    exit 1
}

$n = 0
$SW_MAXIMIZE = 3

foreach ($h in [WinShow]::TopLevel([uint32[]]$procIds)) {
    # Order matters. Restore first, then move it back onto the display, THEN
    # maximize. Chrome re-applies its own remembered size shortly after a
    # restore, so sizing by hand here loses a race and leaves the window hanging
    # off the bottom of the screen; maximizing wins because Windows clamps it to
    # the work area of whichever monitor the window is currently on - which is
    # why it has to be moved on-screen before the maximize, not after.
    [void][WinShow]::ShowWindow($h, $SW_RESTORE)
    [void][WinShow]::SetWindowPos($h, [IntPtr]::Zero, $X, $Y, $W, $H, $SWP_NOZORDER)
    Start-Sleep -Milliseconds 250
    [void][WinShow]::ShowWindow($h, $SW_MAXIMIZE)
    [void][WinShow]::SetForegroundWindow($h)
    $n++
}
Write-Host "restored $n window(s) - your filled tabs are in there"
