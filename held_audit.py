"""held_audit.py - safeguard: list every held question across today's holds so a rule gap is
visible (a question we can answer should never appear here twice)."""
import json,re,sys,collections
sys.stdout.reconfigure(encoding="utf-8")
last={}
for l in open("applications.jsonl",encoding="utf-8"):
    try: d=json.loads(l)
    except Exception: continue
    if d.get("ts","")>="2026-10-06T14": last[d.get("url")]=d
cnt=collections.Counter(); who=collections.defaultdict(set)
for d in last.values():
    if d.get("status")!="needs_you": continue
    for part in (d.get("reason") or d.get("note") or "").split("held.",1)[-1].split("; "):
        k=re.sub(r"\s+"," ",part.strip())[:90]
        if k: cnt[k]+=1; who[k].add(d.get("company") or "")
for k,n in cnt.most_common(): print(n,k,"|",",".join(sorted(who[k]))[:60])
