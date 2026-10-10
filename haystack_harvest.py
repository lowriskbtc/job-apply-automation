"""haystack_harvest.py: collect Haystack job cards (list page anchors), then read each detail page, paced 4-7s, in the SAME tab. Writes haystack_jobs.json."""
from playwright.sync_api import sync_playwright
import sys, json, random, re
sys.stdout.reconfigure(encoding="utf-8")
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 30
NL = chr(10)
out = []
def after(t, label):
    m = re.search(label + r"[ ]*" + NL + r"[ ]*([^" + NL + r"]+)", t)
    return m.group(1).strip() if m else ""
with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222"); ctx = b.contexts[0]
    pg = [x for x in ctx.pages if "haystackapp.io" in x.url][0]
    pg.goto("https://haystackapp.io/app/jobs", timeout=40000); pg.wait_for_timeout(6000)
    for _ in range(3):
        n = pg.evaluate("()=>document.querySelectorAll('a[href*=\"/app/jobs/\"]').length")
        if n >= LIMIT: break
        lm = pg.get_by_text("load more", exact=False)
        if lm.count(): lm.first.click(); pg.wait_for_timeout(3000)
        pg.mouse.wheel(0, 3000); pg.wait_for_timeout(1500)
    cards = pg.evaluate("()=>[...new Set([...document.querySelectorAll('a[href*=\"/app/jobs/\"]')].map(a=>a.href))]")[:LIMIT]
    print(len(cards), "cards", flush=True)
    for h in cards:
        try:
            pg.goto(h, timeout=40000); pg.wait_for_timeout(random.randint(4000, 7000))
            t = pg.inner_text("body")
            title = pg.evaluate("()=>{const h=document.querySelector('h1');return h?h.innerText.trim():''}")
            comp = re.search(r"About ([A-Z][^" + NL + r"]{1,40})" + NL, t)
            rec = {"haystack": h, "title": title, "company": comp.group(1).strip() if comp else "",
                   "loc": after(t, "Location"), "mode": after(t, "Work mode"), "salary": after(t, "Salary")}
            out.append(rec)
            print(rec["title"][:46], "|", rec["company"][:16], "|", rec["mode"][:8], "|", rec["loc"][:20], "|", rec["salary"][:18], flush=True)
        except Exception as e:
            print("ERR", h[-12:], str(e)[:50], flush=True)
    p.stop()
json.dump(out, open("haystack_jobs.json", "w", encoding="utf-8"), ensure_ascii=False)
