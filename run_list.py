"""run_list.py - apply to a short list of already-open tabs ONE AT A TIME, in the background, 60-90s apart.
Reads jr_queue.json ([[slug, url], ...]). Uses auto_apply.py, which reuses the open tab and logs the result."""
import json, subprocess, sys, time, random, os
HERE = os.path.dirname(os.path.abspath(__file__))
q = json.load(open(os.path.join(HERE, "jr_queue.json")))
for i, (slug, url) in enumerate(q, 1):
    print(f"[{i}/{len(q)}] {slug} {url[:90]}", flush=True)
    try:
        p = subprocess.run([sys.executable, os.path.join(HERE, "auto_apply.py"), "--url", url, "--company", slug, "--title", "", "--loc", "Remote"],
                           capture_output=True, text=True, encoding="utf-8", timeout=300)
        line = [l for l in p.stdout.splitlines() if l.startswith("RESULT:")]
        r = json.loads(line[-1][7:]) if line else {"status": "held", "reason": "no result"}
    except subprocess.TimeoutExpired:
        r = {"status": "held", "reason": "timeout"}
    print(f"    -> {r['status']}: {str(r.get('reason',''))[:160]}", flush=True)
    if i < len(q): time.sleep(random.uniform(60, 90))
print("DONE", flush=True)
