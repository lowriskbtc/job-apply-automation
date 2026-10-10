#!/usr/bin/env python
"""daily_report.py - build DAILY-REPORT-<date>.md (and print it) from applications.jsonl.

  python daily_report.py            # last 24 hours
  python daily_report.py --hours 48
Counts are by the LAST status per application URL, so a held form he finished by hand
and I re-logged as submitted counts once. Times in the log are UTC.
"""
import json, sys, datetime, os, re, urllib.request
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
hours = int(sys.argv[sys.argv.index("--hours") + 1]) if "--hours" in sys.argv else 24
now = datetime.datetime.now(datetime.UTC)
since = (now - datetime.timedelta(hours=hours)).isoformat(timespec="seconds")
rows = []
for l in open(os.path.join(HERE, "applications.jsonl"), encoding="utf-8"):
    try: rows.append(json.loads(l))
    except Exception: pass
last = {}
for r in rows: last[r.get("url")] = r
def co(r): return (r.get("company") or r.get("url", "")[:30]).strip()
new_sub = [r for r in last.values() if r.get("status") == "submitted" and r.get("ts", "") >= since]
held = [r for r in last.values() if r.get("status") in ("needs_you",) and r.get("ts", "") >= since]
failed = [r for r in last.values() if r.get("status") == "submit_failed" and r.get("ts", "") >= since]
skipped = [r for r in last.values() if r.get("status") == "skipped" and r.get("ts", "") >= since]
total = [r for r in last.values() if r.get("status") == "submitted"]
try:
    tabs = [t for t in json.load(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5)) if t["type"] == "page"]
except Exception:
    tabs = None
def why(r):
    n = (r.get("note") or "")
    return re.sub(r"\s+", " ", n.split("held.", 1)[-1].split("submit_failed.", 1)[-1])[:170]
L = [f"# Job search daily report, {now:%Y-%m-%d}", "",
     f"Window: last {hours} hours. Applications are counted once, by their latest status.", "",
     f"- **Submitted in this window: {len(new_sub)}**",
     f"- Submitted all time (in the log): {len(total)}",
     f"- Waiting on you (held): {len(held)}",
     f"- Failed at submit: {len(failed)}",
     f"- Skipped on purpose: {len(skipped)}",
     f"- Chrome tabs open: {len(tabs) if tabs is not None else 'job-search Chrome not reachable'}", ""]
L.append("## Submitted"); L += [f"- {co(r)} | {(r.get('title') or '')[:60]}" for r in sorted(new_sub, key=lambda r: r["ts"])] or ["- none"]
L += ["", "## Needs you"]; L += [f"- **{co(r)}** | {(r.get('title') or '')[:50]}: {why(r)}" for r in held] or ["- nothing"]
L += ["", "## Failed at submit"]; L += [f"- **{co(r)}**: {why(r)}" for r in failed] or ["- none"]
L += ["", "## Skipped"]; L += [f"- {co(r)}: {why(r)}" for r in skipped] or ["- none"]
text = "\n".join(L) + "\n"
open(os.path.join(HERE, f"DAILY-REPORT-{now:%Y-%m-%d}.md"), "w", encoding="utf-8").write(text)
print(text)
