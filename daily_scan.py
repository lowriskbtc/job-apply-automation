#!/usr/bin/env python
"""daily_scan.py - the nightly AI/ML + crypto job scan and paced apply (the user 2026-10-09).

  python daily_scan.py            # full run: ensure Chrome, collect, screen, apply up to --limit, log
  python daily_scan.py --dry      # collect + screen only, no applying

Steps: (1) make sure the job-search Chrome is up, (2) collect open roles from every Greenhouse/Ashby/Lever
slug we know, (3) screen (degree/pay/location/language) and rebuild candidates, (4) run_auto applies one
company at a time, 40-80s apart, never more than --max-open unfinished tabs (he allows up to 20 left for
him), (5) results land in applications.jsonl and the 8 AM daily report email.
"""
import subprocess, sys, os, time, json, urllib.request, datetime
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
LIMIT = sys.argv[sys.argv.index("--limit") + 1] if "--limit" in sys.argv else "30"
DRY = "--dry" in sys.argv
LOG = open(os.path.join(HERE, "daily_scan.log"), "a", encoding="utf-8")
def say(m):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {m}"; print(line, flush=True); LOG.write(line + "\n"); LOG.flush()
def chrome_ok():
    try: urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=6); return True
    except Exception: return False
def run(*cmd, timeout=3600):
    return subprocess.run([sys.executable, *cmd], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)

say("daily scan start")
if not chrome_ok():
    say("Chrome debug port down: starting it")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(HERE, "start_chrome.ps1")], capture_output=True)
    time.sleep(8)
if not chrome_ok() and not DRY:
    say("Chrome still down, stopping"); sys.exit(1)

out = "jobs_boards_daily.txt"
r = run("collect_boards.py", "--out", out, timeout=1800)
say("collect: " + (r.stdout.strip().splitlines() or ["(no output)"])[-1][:120])
# filter_roles / build_candidates read this fixed name
src, dst = out + ".json", "jobs_boards_2026-10-06.txt.json"
if os.path.exists(src):
    import shutil; shutil.copy(src, dst)
r = run("filter_roles.py", timeout=3000); say("screen done")
r = run("build_candidates.py"); say("candidates: " + (r.stdout.strip().splitlines() or [""])[-1][:120])
if DRY: say("dry run, stopping before apply"); sys.exit(0)
log_path = os.path.join(HERE, f"auto_run_daily_{datetime.date.today():%Y%m%d}.log")
with open(log_path, "w", encoding="utf-8") as fh:
    p = subprocess.run([sys.executable, "run_auto.py", "--limit", LIMIT, "--max-open", "20"], stdout=fh, stderr=subprocess.STDOUT, timeout=4 * 3600)
say("apply run finished, exit " + str(p.returncode))
try:   # second source: Jobright, one job at a time start to finish (jobright_apply.py)
    jr_log = os.path.join(HERE, f"jobright_apply_daily_{datetime.date.today():%Y%m%d}.log")
    with open(jr_log, "w", encoding="utf-8") as fh:
        q = subprocess.run([sys.executable, "jobright_apply.py", "--limit", "20", "--max-open", "20"], stdout=fh, stderr=subprocess.STDOUT, timeout=3 * 3600)
    say("jobright run finished, exit " + str(q.returncode))
except Exception as exc:
    say("jobright run error: " + str(exc)[:100])
try:
    last = [l for l in open(log_path, encoding="utf-8", errors="replace") if l.startswith("Counter")]
    say("result: " + (last[-1].strip() if last else "(no counter line)"))
except Exception: pass
say("daily scan end")
