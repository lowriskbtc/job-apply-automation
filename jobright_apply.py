#!/usr/bin/env python
"""jobright_apply.py - Jobright, one job at a time, START TO FINISH (the user 2026-10-09).

For each fresh job in the Jobright feed: follow Apply to the employer URL, and if it is a board the
pipeline handles (Greenhouse, Lever, Ashby) fill and submit it immediately with auto_apply.py before moving to
the next job. Jobs on boards I cannot do (Workday, LinkedIn, ...) are logged with their link and skipped; no
page is opened that is not then filled. Paced 40-80s apart. Stops at MAX_OPEN unfinished tabs.

  python jobright_apply.py --limit 25
"""
import sys, os, re, json, time, random, subprocess, argparse, datetime, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__)); os.chdir(HERE); sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8")
import collect_jobright as cj
import run_auto as ra

ap = argparse.ArgumentParser(); ap.add_argument("--limit", type=int, default=25); ap.add_argument("--max-open", type=int, default=8)
a = ap.parse_args()
LOGF = os.path.join(HERE, "jobright_apply_log.jsonl")
def log(d):
    with open(LOGF, "a", encoding="utf-8") as fh: fh.write(json.dumps(d, ensure_ascii=False) + "\n")
def tabs(): return [t for t in json.load(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=8)) if t["type"] == "page"]
def slug_of(u):
    m = re.search(r"for=([a-z0-9-]+)", u) or re.search(r"(?:ashbyhq\.com|lever\.co)/([a-z0-9-]+)/", u)
    return m.group(1) if m else ""
SUPPORTED = re.compile(r"job-boards\.greenhouse\.io|boards\.greenhouse\.io|jobs\.lever\.co|jobs\.ashbyhq\.com")

done_urls, done_cos = set(), set()   # NOT ra.applied(): its closed-tab inference treats held forms I closed as finished
# the user 2026-10-09: match company names loosely ('Curai Health' == 'curai') and include skipped ones, so nothing is ever applied to twice
_names = set(done_cos)
for _l in open(os.path.join(HERE, "applications.jsonl"), encoding="utf-8"):
    try: _d = json.loads(_l)
    except Exception: continue
    if _d.get("status") in ("submitted", "skipped"):
        for _k in ((_d.get("company") or ""), (_d.get("slug") or ""), (slug_of(_d.get("url") or ""))):
            _k = re.sub(r"[^a-z0-9]", "", _k.lower())
            if len(_k) >= 3: _names.add(_k)
def already(co):
    return any(co == n or (len(n) >= 4 and len(co) >= 4 and (co.startswith(n) or n.startswith(co))) for n in _names)
seen_before = set()
if os.path.exists(LOGF):
    for l in open(LOGF, encoding="utf-8"):
        try: seen_before.add(json.loads(l)["jobright_url"])
        except Exception: pass

pw, browser, ctx = cj.connect(cj.DEFAULT_PORT)
baseline = list(ctx.pages)
feed = ctx.new_page()
try:
    jobs = cj.harvest(feed, a.limit * 3, 25)
    print(f"{len(jobs)} jobs in the feed", flush=True)
    n_done = 0
    for job in jobs:
        if n_done >= a.limit: break
        co = re.sub(r"[^a-z0-9]", "", (job.get("company") or "").lower())
        if job["jobright_url"] in seen_before: continue
        if already(co):
            log({"jobright_url": job["jobright_url"], "company": job["company"], "result": "already applied to this company"}); continue
        ttl = (job.get("title") or "").lower()
        GOODT = re.compile(r"\b(ai|ml|machine learning|llm|agent|agents|agentic|applied|solana|blockchain|smart contract|web3|defi|rust|forward deployed|data scientist|research engineer|backend|software engineer|full.?stack|platform)\b")
        BADT = re.compile(r"\b(intern|co-?op|junior|entry|graduate|growth|marketing|sales|recruit|manager|director|support|operations|analyst|designer|trainer|clearance|prompt writer)\b")
        if not GOODT.search(ttl) or BADT.search(ttl):
            log({"jobright_url": job["jobright_url"], "company": job["company"], "title": job.get("title"), "result": "title not a fit"}); continue
        if len(tabs()) - 1 >= a.max_open:
            print(f"PAUSED: {len(tabs())} tabs open (max {a.max_open}). Finish or close them.", flush=True); break
        url = cj.resolve_apply_url(feed, ctx, job)
        for pg in list(ctx.pages):   # nothing the resolve opened may stay
            if pg is not feed and ("jr_id" in pg.url or not SUPPORTED.search(pg.url)) and "jobright.ai" not in pg.url:   # popups only; a held form tab (clean board URL) must stay
                try: pg.close()
                except Exception: pass
        clean = re.split(r"[?&]jr_id", url)[0] if url else ""
        if not clean or not SUPPORTED.search(clean):
            host = re.sub(r"^https?://", "", clean).split("/")[0] if clean else "(none)"
            print(f"  skip {job['company'][:22]:<22} {host[:34]} (not a board I can fill)", flush=True)
            log({"jobright_url": job["jobright_url"], "company": job["company"], "title": job.get("title"), "link": clean, "result": "other board"}); continue
        slug = slug_of(clean) or co
        apply_url = ra.apply_url(slug, clean)
        print(f"[{n_done+1}] {job['company'][:22]} | {job['title'][:40]} -> filling", flush=True)
        try:
            p = subprocess.run([sys.executable, os.path.join(HERE, "auto_apply.py"), "--url", apply_url, "--company", slug, "--title", job.get("title") or "", "--loc", "Remote"],
                               capture_output=True, text=True, encoding="utf-8", timeout=300)
            line = [l for l in p.stdout.splitlines() if l.startswith("RESULT:")]
            r = json.loads(line[-1][7:]) if line else {"status": "held", "reason": "no result"}
        except subprocess.TimeoutExpired:
            r = {"status": "held", "reason": "timeout"}
        print(f"    -> {r['status']}: {str(r.get('reason',''))[:120]}", flush=True)
        log({"jobright_url": job["jobright_url"], "company": job["company"], "title": job.get("title"), "link": clean, "result": r["status"], "reason": r.get("reason", "")})
        n_done += 1; done_cos.add(co); baseline = list(ctx.pages)   # a held form's tab stays; later cleanups leave it alone
        time.sleep(random.uniform(40, 80))
finally:
    try: feed.close()
    except Exception: pass
    pw.stop()
print("DONE", flush=True)
