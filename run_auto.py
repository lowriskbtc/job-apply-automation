#!/usr/bin/env python
"""run_auto.py - orchestrate auto_apply.py over the screened candidates.

One subprocess per job (timeout), hygiene between jobs (close stray tabs, probe for
hung ones), one application per company, skips companies already applied to.
Writes auto_report_<date>.md. the user's goal: come back to a report.

  python run_auto.py --limit 25 [--dry]
"""
import argparse, datetime, json, os, re, subprocess, sys, time, urllib.request
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_OPEN_TABS = 4   # the user 2026-10-08: never pile up tabs
SKIP_CO = {"jamf", "justworks", "nextdoor", "warp", "elevenlabs", "databricks", "cursor", "clickup", "arizeai", "cerebras", "cohere", "brex", "baseten", "anthropic",
           "paradigm", "stripe", "okx", "moonpay", "drw", "d3", "sofi"}
GOOD = re.compile(r"\b(ai|ml|machine learning|llm|agent|agents|agentic|applied|solana|blockchain|smart contract|web3|defi|rust|forward deployed)\b", re.I)
BAD = re.compile(r"recruit|operations|planner|architect|specialist|director|manager|sales|designer|analyst|counsel|marketing|partnerships|policy|economist|scientist|fellows|intern|staff|principal|lead\b|head|tutor|annotat|evaluator|advocate|evangelist|community manager|developer experience|technical writer|solutions engineer|sales engineer|customer engineer|support engineer|implementation|professional services|campus|new grad|graduate|early career|university|apprentice|co-?op", re.I)
LOC = re.compile(r"remote|united states|usa|\bus\b|new york|san francisco|nyc|\bsf\b", re.I)

def norm(u):
    u = u.split("?")[0].rstrip("/")
    return re.sub(r"/(apply|application)$", "", u)

def applied():
    urls, cos = set(), set()
    for l in open(os.path.join(HERE, "applications.jsonl"), encoding="utf-8"):
        try: d = json.loads(l)
        except Exception: continue
        if d.get("status") in ("submitted", "skipped", "dead_link"):
            urls.add(norm(d["url"]))
        if d.get("status") == "submitted":
            cos.add(re.sub(r"[^a-z0-9]", "", (d.get("company") or "").lower()))
            cos.add(re.sub(r"[^a-z0-9]", "", (d.get("slug") or "").lower()))
    # the user 2026-10-08: a form I left open that he later closed was finished by him. Never re-apply.
    try:
        open_urls = [t["url"].split("?")[0] if "job_app" not in t["url"] else t["url"] for t in tabs()]
        last = {}
        for l in open(os.path.join(HERE, "applications.jsonl"), encoding="utf-8"):
            try: d = json.loads(l)
            except Exception: continue
            last[d.get("url")] = d
        for u, d in last.items():
            if d.get("status") in ("needs_you", "submit_failed") and d.get("ts", "") >= "2026-10-06T00:00":
                if not any(norm(u) == norm(o) or u in o or o in u for o in open_urls):
                    urls.add(norm(u)); cos.add(re.sub(r"[^a-z0-9]", "", (d.get("company") or "").lower()))
    except Exception:
        pass
    return urls, cos

def apply_url(slug, url):
    if "greenhouse" in url:
        m = re.search(r"/jobs/(\d+)", url)
        if m: return f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={m.group(1)}"
    if "ashbyhq" in url and not url.endswith("/application"): return url.rstrip("/") + "/application"
    if "lever.co" in url and not url.endswith("/apply"): return url.rstrip("/") + "/apply"
    return url

def tabs():
    return [t for t in json.load(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=8)) if t["type"] == "page"]

def close_new_tabs(keep_ids):
    for t in tabs():
        if t["id"] not in keep_ids:
            try: urllib.request.urlopen("http://127.0.0.1:9222/json/close/" + t["id"], timeout=5)
            except Exception: pass

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--limit", type=int, default=20); ap.add_argument("--dry", action="store_true")
    ap.add_argument("--per-job-timeout", type=int, default=240)
    ap.add_argument("--only", default=""); ap.add_argument("--max-open", type=int, default=0)
    ap.add_argument("--skip-held", action="store_true")
    a = ap.parse_args()
    cand = json.load(open(os.path.join(HERE, "jobs_candidates.json"), encoding="utf-8"))
    done_urls, done_cos = applied()
    def score(row):
        title, loc = row[1].lower(), (row[2] or "").lower()
        s = 0
        s += 4 if re.search(r"agent|llm|applied ai|ai engineer|machine learning|ml engineer|forward deployed", title) else 0
        s += 3 if re.search(r"solana|blockchain|smart contract|web3|defi|protocol|rust", title) else 0
        s += 1 if re.search(r"remote|united states", loc) else 0
        s -= 2 if re.search(r"senior|sr\.", title) and not re.search(r"agent|ai|ml", title) else 0
        return -s
    attempts = {}
    for l in open(os.path.join(HERE, "applications.jsonl"), encoding="utf-8"):
        try: d = json.loads(l)
        except Exception: continue
        if "auto_apply" in d.get("note", "") and d.get("ts", "")[:10] == datetime.date.today().isoformat():
            k = re.sub(r"[^a-z0-9]", "", (d.get("company") or "").lower().split("@")[-1])
            attempts[k] = attempts.get(k, 0) + 1
    def tries(row): return attempts.get(re.sub(r"[^a-z0-9]", "", row[0].lower()), 0)
    cand = sorted(cand, key=lambda row: (tries(row), score(row)))
    seen_co, queue = set(), []
    for slug, title, loc, url in cand:
        co = re.sub(r"[^a-z0-9]", "", slug.lower())
        if slug in SKIP_CO or co in done_cos or co in seen_co: continue
        if not (GOOD.search(title) and not BAD.search(title) and LOC.search(loc or "remote")): continue
        if re.search(r"ontario|british columbia|canada|toronto|vancouver|montreal|london|uk|united kingdom|europe|emea|apac|india|singapore|germany|berlin|paris|dublin|ireland|australia|sydney|israel|tel aviv|brazil|mexico", title + " " + (loc or ""), re.I) and not re.search(r"united states|usa|new york|san francisco|sf|nyc", loc or "", re.I):
            continue
        if norm(url) in done_urls: continue
        seen_co.add(co); queue.append((slug, title, loc, apply_url(slug, url)))
    if a.only:
        want = {x.strip().lower() for x in a.only.split(",")}
        queue = [q for q in queue if q[0].lower() in want]
    queue = queue[: a.limit]
    print(f"{len(queue)} jobs queued (one per company)", flush=True)
    keep = {t["id"] for t in tabs()}
    results = []
    for i, (slug, title, loc, url) in enumerate(queue, 1):
        print(f"[{i}/{len(queue)}] {slug} | {title} | {url[:80]}", flush=True)
        if len(tabs()) >= (a.max_open or MAX_OPEN_TABS) and not a.only:   # the user 2026-10-08: "you have like 50 tabs open"
            print(f"    -> PAUSED: {len(tabs())} tabs are open (limit {MAX_OPEN_TABS}). Finish or close them, then re-run.", flush=True); break
        cmd = [sys.executable, os.path.join(HERE, "auto_apply.py"), "--url", url, "--company", slug, "--title", title, "--loc", loc or ""] + (["--no-submit"] if a.dry else [])
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=a.per_job_timeout)
            line = [l for l in p.stdout.splitlines() if l.startswith("RESULT:")]
            r = json.loads(line[-1][7:]) if line else {"status": "held", "reason": "no result: " + (p.stderr or p.stdout)[-160:]}
        except subprocess.TimeoutExpired:
            r = {"status": "held", "reason": "timeout (page hung)", "company": slug, "title": title, "url": url}
        r["slug"], r["loc"] = slug, loc
        results.append(r)
        print(f"    -> {r['status']}: {r.get('reason','')[:140]}", flush=True)
        for v in r.get("values", [])[:14]: print("         ", v, flush=True)
        for v in r.get("radios", [])[:8]: print("          (radio)", v, flush=True)
        if r["status"] in ("held", "needs_you", "submit_failed", "needs_code", "ready") and "timeout" not in r.get("reason", ""):
            keep |= {t["id"] for t in tabs()}   # the user 2026-10-07: never close an application I could not finish
        else:
            close_new_tabs(keep)
        import random
        time.sleep(random.uniform(40, 80))   # human pace: one application at a time, 40-80s apart (he was getting flagged)
    stamp = datetime.date.today().isoformat()
    path = os.path.join(HERE, f"auto_report_{stamp}.md")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"\n## Run {datetime.datetime.now():%H:%M} ({'DRY' if a.dry else 'LIVE'})\n\n")
        for st in ("submitted", "ready", "held", "skipped", "dead_link", "submit_failed"):
            rows = [r for r in results if r["status"] == st]
            if not rows: continue
            fh.write(f"### {st} ({len(rows)})\n")
            for r in rows: fh.write(f"- **{r.get('company') or r['slug']}** | {r.get('title','')} | {r.get('loc','')} | {r.get('url','')[:90]}" + (f"\n    - {r['reason']}" if st != "submitted" else "") + "\n")
    print("report:", path)
    import collections; print(collections.Counter(r["status"] for r in results))

if __name__ == "__main__":
    main()
