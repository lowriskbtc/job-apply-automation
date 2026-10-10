#!/usr/bin/env python
"""filter_roles.py - screen board roles BEFORE filling anything.

Reads jobs_boards_2026-10-06.txt.json (collect_boards.py output), pulls each posting's
description from the public Greenhouse/Ashby/Lever APIs, and drops roles the user
should not spend time on (2026-10-06 rules, see memory):
  - hard bachelor's/degree requirement with no 'or equivalent experience' clause
  - equity/token-only pay, "no cash salary", unpaid
  - Mandarin/other-language required, APAC/EMEA-only
  - already applied (applications.jsonl)
Prints survivors, best fit first. Writes jobs_screened.json.
"""
import json, re, sys, html, urllib.request, concurrent.futures as cf
sys.stdout.reconfigure(encoding="utf-8")
rows = json.load(open("jobs_boards_2026-10-06.txt.json"))
def get(u):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
            return json.load(r)
    except Exception: return None
def text(h): return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(h or "")))
cache_ash, cache_lev = {}, {}
def desc(row):
    slug, title, loc, url = row
    if "greenhouse" in url:
        m = re.search(r"/jobs/(\d+)", url)
        d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{m.group(1)}") if m else None
        return text(d.get("content")) if d else ""
    if "ashbyhq" in url:
        if slug not in cache_ash:
            cache_ash[slug] = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}") or {}
        for j in cache_ash[slug].get("jobs", []):
            if j.get("jobUrl") == url or j.get("id") in url: return j.get("descriptionPlain", "")
    if "lever.co" in url:
        if slug not in cache_lev:
            cache_lev[slug] = get(f"https://api.lever.co/v0/postings/{slug}?mode=json") or []
        for j in cache_lev[slug]:
            if j.get("hostedUrl") == url: return j.get("descriptionPlain", "")
    return ""
DEG = re.compile(r"(bachelor|b\.s\.|bs/ms|undergraduate degree|university degree|degree in)[^.]{0,120}", re.I)
EQV = re.compile(r"equivalent|or related experience|or comparable|or practical|in lieu|preferred|a plus|nice to have|or experience|or similar", re.I)
NOCASH = re.compile(r"no cash (salary|compensation|pay)|(paid|compensated) (entirely|solely|only|exclusively) in (equity|tokens?)|equity[- ](and|or)[- ]token[- ]based (role|opportunity)|compensation is (entirely|solely|only) (equity|tokens?)|unpaid", re.I)
LANG = re.compile(r"(fluen\w+|proficien\w+|native)[^.]{0,40}(mandarin|chinese|japanese|korean|german|french|spanish|portuguese|arabic)|(mandarin|japanese|korean)[^.]{0,30}(required|is a must)", re.I)
REGION = re.compile(r"\b(apac|emea|latam)\b|remote[- ]+(roles?[- ]+)?apac", re.I)
done = set()
for l in open("applications.jsonl", encoding="utf-8"):
    try: d = json.loads(l)
    except Exception: continue
    if d["status"] in ("submitted", "skipped"):
        done.add(re.sub(r"/(apply|application)$", "", d["url"].split("?")[0].rstrip("/")))
def screen(row):
    slug, title, loc, url = row
    key = re.sub(r"/(apply|application)$", "", url.split("?")[0].rstrip("/"))
    if key in done: return row, "already applied"
    t = desc(row)
    if not t: return row, "ok (no description fetched)"
    why = []
    hits = [m.group(0) for m in DEG.finditer(t)]
    if hits and not any(EQV.search(h) for h in hits) and re.search(r"(required|must|minimum|requires|need)", " ".join(hits), re.I):
        why.append("hard degree: " + hits[0][:70])
    if NOCASH.search(t): why.append("equity/token-only pay")
    if LANG.search(t): why.append("language required")
    if REGION.search(t) or REGION.search(loc or ""): why.append("APAC/EMEA region")
    return row, ("; ".join(why) if why else "ok")
with cf.ThreadPoolExecutor(8) as ex: res = list(ex.map(screen, rows))
keep = [r for r, v in res if v.startswith("ok")]
drop = [(r, v) for r, v in res if not v.startswith("ok")]
print(f"{len(rows)} roles: {len(keep)} kept, {len(drop)} dropped")
import collections
print(collections.Counter(v.split(":")[0] for _, v in drop))
json.dump({"keep": keep, "drop": [[r, v] for r, v in drop]}, open("jobs_screened.json", "w"), ensure_ascii=False, indent=0)
for r in keep[:25]: print(f"  {r[0]:12} | {r[1][:62]:62} | {r[2][:22]}")
