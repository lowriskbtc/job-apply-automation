#!/usr/bin/env python
"""
tabs.py - what is open in Chrome waiting for you, and tidy up the duplicates.

    python tabs.py            list the application tabs, one line each
    python tabs.py --tidy     close exact duplicates, keeping one of each

Re-running a queue fills the same job again in a NEW tab, so two passes over
eight jobs leaves twenty-five tabs and no idea which is which. This only ever
closes a tab whose url is character-for-character identical to another open
tab AND which this project put there - anything else you opened yourself is
never touched, however many of them there are.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from autofill import connect, norm_url, DEFAULT_PORT       # noqa: E402

# Tabs the run itself always leaves behind, and never worth listing as work.
IGNORE = ("about:blank", "chrome://", "chrome-extension://",
          "jobright.ai/jobs/recommend")


def ours() -> set[str]:
    """Normalised urls this project has actually visited."""
    keys: set[str] = set()
    log = HERE / "applications.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            for k in ("url", "requested_url"):
                if rec.get(k):
                    keys.add(norm_url(rec[k]))
    for name in ("jobs.txt", "jobs_indeed.txt", "jobs_indeed_hosted.txt"):
        p = HERE / name
        if p.exists():
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    keys.add(norm_url(ln.split()[0]))
    return keys


def main() -> None:
    ap = argparse.ArgumentParser(description="List or tidy the open form tabs.")
    ap.add_argument("--tidy", action="store_true",
                    help="close exact-duplicate tabs, keeping one of each")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    pw, browser, ctx = connect(args.port)
    try:
        rows = []
        for p in ctx.pages:
            try:
                url, title = p.url, (p.title() or "")
            except Exception:
                continue
            if not url or any(m in url for m in IGNORE):
                continue
            rows.append({"page": p, "url": url, "title": title})

        mine = ours()
        seen: dict[str, dict] = {}
        dupes = []
        for r in rows:
            # Group on the exact url. Deliberately NOT norm_url: two tabs of
            # the same posting opened from different queues are still two
            # different pages mid-form, and closing one could throw away
            # typing. Only a byte-identical duplicate is safe to drop.
            if r["url"] in seen and norm_url(r["url"]) in mine:
                dupes.append(r)
            else:
                seen.setdefault(r["url"], r)

        if args.tidy and dupes:
            for r in dupes:
                try:
                    r["page"].close()
                except Exception:
                    pass
            print(f"closed {len(dupes)} duplicate tab(s)\n")
        elif dupes:
            print(f"{len(dupes)} duplicate tab(s) - run with --tidy to close them\n")

        keep = list(seen.values())
        work = [r for r in keep if norm_url(r["url"]) in mine]
        other = [r for r in keep if norm_url(r["url"]) not in mine]

        print(f"{len(work)} application tab(s) waiting for you:\n")
        for i, r in enumerate(work, 1):
            print(f"  {i}. {r['title'][:66]}")
            print(f"     {r['url'][:104]}")
        if other:
            print(f"\n({len(other)} other tab(s) of yours, left alone)")
    finally:
        pw.stop()          # never browser.close() - that is the operator's Chrome


if __name__ == "__main__":
    main()
