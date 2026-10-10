#!/usr/bin/env python
"""make_report.py - REPORT-<date>.md from applications.jsonl for the current session."""
import json, re, collections, sys
sys.stdout.reconfigure(encoding="utf-8")
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-10-06T14"
apps = [json.loads(l) for l in open("applications.jsonl", encoding="utf-8") if l.strip()]
cur = [d for d in apps if d["ts"] >= SINCE]
norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower().split("@")[-1])
sub = {}
for d in cur:
    if d["status"] == "submitted":
        sub[norm(d.get("company")) + "|" + re.sub(r"[^a-z0-9]", "", (d.get("title") or "").lower())[:30]] = d
by = collections.OrderedDict()
for d in sorted(sub.values(), key=lambda x: x["ts"]):
    nm = (d.get("company") or "?").split("@")[-1].strip()
    by.setdefault(nm.title() if nm.islower() else nm, []).append((d.get("title") or "")[:72].split(" @ ")[0])
last = {}
for d in cur:
    if "auto_apply" in d.get("note", ""):
        last[(d.get("company") or "").lower().split("@")[-1].strip()] = d
subco = {norm(c) for c in by}
held = [(k, v) for k, v in last.items() if v["status"] == "needs_you" and ": held" in v["note"] and norm(k) not in subco]
fail = [(k, v) for k, v in last.items() if v["status"] == "submit_failed" and norm(k) not in subco]
skipped = [(k, v) for k, v in last.items() if v["status"] == "skipped"]
L = ["# Auto-apply report, 2026-10-06", "", "## Submitted this session", ""]
tot = 0
for co, ts in by.items():
    for t in ts: L.append(f"- **{co}**: {t}"); tot += 1
L += ["", f"**Total submitted: {tot}**", "", "## Held, not submitted (needs a fact or decision from you)", ""]
for k, v in sorted(held): L.append(f"- **{k}**: {v['note'].split('held.', 1)[-1].strip()[:150]}")
L += ["", "## Submit attempted, site rejected it (nothing was sent)", ""]
for k, v in sorted(fail): L.append(f"- **{k}**: {v['note'][:150]}")
L += ["", "## Skipped", ""]
for k, v in skipped: L.append(f"- **{k}**: {v['note'][:120]}")
open("REPORT-2026-10-06.md", "w", encoding="utf-8").write("\n".join(L) + "\n")
print("submitted", tot, "| held", len(held), "| failed", len(fail), "| skipped", len(skipped))
print(", ".join(f"{c} ({len(t)})" for c, t in by.items()))
