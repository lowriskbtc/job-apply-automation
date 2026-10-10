import json,urllib.request,sys
sys.stdout.reconfigure(encoding="utf-8")
tabs=[t for t in json.load(urllib.request.urlopen("http://127.0.0.1:9222/json")) if t["type"]=="page"]
rows=[json.loads(l) for l in open("applications.jsonl",encoding="utf-8") if l.strip()]
last={}
for r in rows: last[r.get("url")]=r
def key(u): return u if "job_app" in u else u.split("?")[0]
out=["# Open forms that need you (generated)\n","Finish and close each one; a closed tab counts as done.\n"]
n=0
for t in tabs:
    k=key(t["url"]); m=[r for u,r in last.items() if u and key(u)==k]
    if not m: print("non-job tab:",t["url"][:70]); continue
    r=m[-1]
    if r["status"]=="submitted": print("already submitted but open:",r.get("company")); continue
    n+=1; why=(r.get("note") or "").split("held.",1)[-1].strip()[:300]
    out.append(f"{n}. **{r.get('company')}** | {(r.get('title') or '')[:50]}\n   - needs: {why}\n")
open("OPEN_FORMS_TODO.md","w",encoding="utf-8").write("\n".join(out)); print(n,"unfinished")
