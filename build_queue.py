"""Build one deduped queue of drivable ATS forms for `autofill.py --queue`.

Sources: every jobs*.txt in the folder, plus every needs_you / submit_failed URL
in applications.jsonl. Drops Indeed/LinkedIn/jobright links (not employer forms -
this system has never driven them), anything already logged submitted or
dead_link, and collapses duplicates after stripping tracking params.

  python build_queue.py [out.txt]        default: jobs_next.txt
"""
import json, os, re, glob, sys

base = os.path.dirname(os.path.abspath(__file__))
out_name = sys.argv[1] if len(sys.argv) > 1 else "jobs_next.txt"
ATS = ("greenhouse.io", "lever.co", "ashbyhq.com", "applytojob.com", "myworkdayjobs.com",
       "jobvite.com", "smartrecruiters.com", "workable.com", "bamboohr.com",
       "icims.com", "paylocity.com", "breezy.hr", "rippling.com", "dover.com")
SKIP = ("indeed.com", "linkedin.com", "glassdoor", "ziprecruiter", "jobright.ai")

seen = {}
order = []

def add(url, src):
    u = url.strip()
    if not u or u.startswith("#"):
        return
    if any(s in u for s in SKIP):
        return
    if not any(a in u for a in ATS):
        return
    key = re.sub(r"[?&](jr_id|token|gh_src|utm_[a-z]+)=[^&]*", "", u).rstrip("/?&")
    if key in seen:
        return
    seen[key] = (u, src)
    order.append(key)

for f in sorted(glob.glob(os.path.join(base, "jobs*.txt"))):
    for line in open(f, encoding="utf-8", errors="ignore"):
        add(line, os.path.basename(f))

status = {}
for line in open(os.path.join(base, "applications.jsonl"), encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    u = d.get("url") or d.get("requested_url") or ""
    key = re.sub(r"[?&](jr_id|token|gh_src|utm_[a-z]+)=[^&]*", "", u).rstrip("/?&")
    status[key] = d.get("status")
    if d.get("status") in ("needs_you", "submit_failed"):
        add(u, "applications.jsonl")

out = []
for key in order:
    u, src = seen[key]
    st = status.get(key, "-")
    # 'skipped' is here because autofill.already_applied() only dedupes on
    # 'submitted', so a form abandoned on purpose (UST/ripplehire threw errors
    # for the user on 2026-08-08) would otherwise be offered again every run.
    if st in ("submitted", "dead_link", "skipped"):
        continue
    out.append((u, src, st))

print(f"{len(out)} candidate ATS forms\n")
for u, src, st in out:
    print(f"[{st}] {src}: {u}")

with open(os.path.join(base, out_name), "w", encoding="utf-8") as fh:
    for u, src, st in out:
        fh.write(u + "\n")
print(f"\nwrote {out_name}")
