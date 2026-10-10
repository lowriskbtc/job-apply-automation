#!/usr/bin/env python
"""watchdog.py - keeps run_auto.py from getting stuck (the user 2026-10-08).

  python watchdog.py            # watch forever; restart a stalled run
  python watchdog.py --once     # one check, then exit

Every 30s it looks at the newest auto_run_live_*.log:
  * log silent > STALL_CHILD_S with an auto_apply.py child running -> kill that child
    (run_auto records the job as held 'no result' and moves to the next one)
  * log silent > STALL_RUN_S -> kill run_auto and its children, relaunch `run_auto.py --limit 40`
    (dedupe means finished/closed jobs are never repeated)
  * Chrome debug port not answering -> start_chrome.ps1 (it never touches his normal Chrome)
It never closes an application tab and never restarts Chrome while port 9222 answers.
"""
import glob, json, os, subprocess, sys, time, urllib.request, datetime
HERE = os.path.dirname(os.path.abspath(__file__))
STALL_CHILD_S, STALL_RUN_S, CHECK_S = 300, 900, 30
LOG = os.path.join(HERE, "watchdog.log")

def say(m):
    line = f"{datetime.datetime.now():%H:%M:%S} {m}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh: fh.write(line + "\n")

def procs():
    ps = ("Get-CimInstance Win32_Process -Filter \"name like 'python%'\" | "
          "Where-Object { $_.CommandLine -match 'run_auto|auto_apply' } | "
          "ForEach-Object { \"$($_.ProcessId)|$([int]((Get-Date) - $_.CreationDate).TotalSeconds)|$($_.CommandLine)\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    res = []
    for l in out.splitlines():
        if l.count("|") >= 2:
            pid, age, cmd = l.split("|", 2); res.append((int(pid), cmd, int(age)))
    return res

def kill(pid):
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)

def chrome_ok():
    try:
        urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=6); return True
    except Exception:
        return False

def newest_log():
    logs = glob.glob(os.path.join(HERE, "auto_run_live_*.log"))
    return max(logs, key=os.path.getmtime) if logs else None

def check():
    if not chrome_ok():
        say("Chrome debug port is down: starting it with start_chrome.ps1")
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(HERE, "start_chrome.ps1")], capture_output=True)
        return
    ps = procs()
    runs = [p for p in ps if "run_auto.py" in p[1]]
    kids = [p for p in ps if "auto_apply.py" in p[1]]
    if not runs: return
    log = newest_log()
    log_age = time.time() - os.path.getmtime(log) if log else 0
    young = [k for k in kids if k[2] < STALL_CHILD_S]
    kid_age = max([k[2] for k in kids], default=0)
    # Judge by the job process's own age, not the log (foreground runs do not write the log).
    if kids and kid_age > STALL_CHILD_S:
        say(f"job running {kid_age}s: killing the stuck auto_apply child")
        for pid, _, _ in kids: kill(pid)
        return
    age = log_age if not young else 0
    if not kids and age > STALL_RUN_S and max(r[2] for r in runs) > STALL_RUN_S and "--only" not in " ".join(r[1] for r in runs):   # idle between jobs for 15 min
        say(f"run silent {int(age)}s: killing run_auto and restarting")
        for pid, _, _ in runs + kids: kill(pid)
        n = len(glob.glob(os.path.join(HERE, "auto_run_live_*.log"))) + 100
        out = open(os.path.join(HERE, f"auto_run_live_{n}.log"), "w", encoding="utf-8")
        subprocess.Popen([sys.executable, os.path.join(HERE, "run_auto.py"), "--limit", "40"], stdout=out, stderr=subprocess.STDOUT, cwd=HERE)

def main():
    say("watchdog started")
    while True:
        try: check()
        except Exception as exc: say("check error: " + str(exc)[:100])
        if "--once" in sys.argv: break
        time.sleep(CHECK_S)

main()
