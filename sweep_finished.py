"""sweep_finished.py - any open tab already showing a confirmation was finished by the user: log it submitted."""
from playwright.sync_api import sync_playwright
import sys,re,json,datetime
sys.stdout.reconfigure(encoding="utf-8")
SUC=re.compile(r"successfully submitted|thank you for (applying|your application)|application (has been )?(received|submitted)|we.ve received your application|your application (was|has been) (sent|submitted)",re.I)
SURL=re.compile(r"/(confirmation|thank[-_]?you|thanks|success|submitted)(?=[/?#]|$)",re.I)
rows=[json.loads(l) for l in open("applications.jsonl",encoding="utf-8") if l.strip()]
byurl={}
for r in rows: byurl[r.get("url")]=r
with sync_playwright() as p:
    b=p.chromium.connect_over_cdp("http://127.0.0.1:9222"); ctx=b.contexts[0]
    for pg in list(ctx.pages):
        try: t=pg.inner_text("body")[:6000]
        except Exception: continue
        if SURL.search(pg.url) or SUC.search(t):
            print("FINISHED:",pg.url[:90])
            m=[r for u,r in byurl.items() if u and (u.split("?")[0] in pg.url or pg.url.split("?")[0] in u)]
            if m:
                n=dict(m[-1]); n.update(status="submitted",ts=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),note="tab shows confirmation (the user finished it)")
                open("applications.jsonl","a",encoding="utf-8").write(json.dumps(n,ensure_ascii=False)+"\n")
    p.stop()
print("done")
