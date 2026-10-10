#!/usr/bin/env python
"""
collect_jobright.py - pull your jobright matches into a local queue file.

jobright's Apply button says "Apply on Employer Site" - it deep-links to the
company's ATS (Greenhouse, Lever, Ashby, Workday). So jobright is a source of
URLs and this script harvests them; autofill.py does the form work.

Attaches to the Chrome started by start_chrome.ps1, which is where you're
signed in to jobright. Nothing here logs in for you and nothing is submitted.

Usage
  python collect_jobright.py                     # harvest jobright links
  python collect_jobright.py --resolve --limit 20  # also follow through to the ATS
  python collect_jobright.py --queue-out jobs.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
JOBS_PATH = HERE / "jobs.jsonl"
DEFAULT_PORT = 9222
RECOMMEND_URL = "https://jobright.ai/jobs/recommend"

# Read the card's STRUCTURE, not its rendered text.
#
# The previous version flattened the card to one string and split it on
# newlines - but the flattening replaced every run of whitespace with a single
# space, so there were no newlines left to split on. Every job came back with
# the timestamp as its company ("6 minutes ago Be an early ap") and an industry
# as its title ("Consulting"). The urls were right, so applications still went
# out; the log was fiction.
#
# There is also nothing to split ON: jobright prints "<TITLE> <COMPANY> /" with
# no delimiter between the two. Only the DOM knows where one ends, and it says
# so plainly - the title is an <h2>, and the company is the next text after it,
# ahead of a '/' separator:
#
#   <span> 52 minutes ago          <- publish time
#   <span> Be an early applicant   <- tag
#   <h2>   Generative AI Engineer  <- TITLE
#   <div>  89 Solutions            <- COMPANY
#   <div>  /                       <- separator
#   <div>  Artificial Intelligence (AI) · Consulting · Growth Stage
#   <span> United States / Full-time / Remote / Mid Level / 2+ years exp
HARVEST_JS = r"""
() => {
  const seen = new Map();
  for (const a of document.querySelectorAll('a[href*="/jobs/info/"]')) {
    const href = a.getAttribute('href');
    const id = (href.match(/\/jobs\/info\/([a-z0-9]+)/i) || [])[1];
    if (!id || seen.has(id)) continue;

    // Climb to the card: the nearest ancestor carrying the <h2> title. Keyed
    // on the title rather than on a text-length range, because a card is
    // reached from several anchors - the title link AND the "84% GOOD MATCH"
    // badge - and the badge's ancestors hold almost no text, which is how
    // some rows previously harvested with an empty card_text and no title.
    let card = a;
    for (let i = 0; i < 8 && card; i++) {
      if (card.querySelector && card.querySelector('h2')) break;
      card = card.parentElement;
    }
    if (!card || !card.querySelector || !card.querySelector('h2')) continue;

    // Leaf texts in document order - only nodes that own their text, so a
    // wrapper does not repeat everything its children say.
    const leaves = [];
    for (const e of card.querySelectorAll('*')) {
      const own = [...e.childNodes]
        .filter(n => n.nodeType === 3)
        .map(n => n.textContent.trim()).join(' ').trim();
      if (own && own.length <= 120) {
        leaves.push({ tag: e.tagName.toLowerCase(), text: own });
      }
    }
    const ti = leaves.findIndex(l => l.tag === 'h2');
    if (ti === -1) continue;

    let company = '';
    for (let j = ti + 1; j < leaves.length; j++) {
      const t = leaves[j].text;
      if (t && t !== '/' && t !== '·') { company = t; break; }
    }
    const all = leaves.map(l => l.text);
    const pick = (rx) => all.find(t => rx.test(t)) || '';

    seen.set(id, {
      id,
      jobright_url: 'https://jobright.ai/jobs/info/' + id,
      title: leaves[ti].text.slice(0, 120),
      company: company.slice(0, 80),
      location: pick(/^(united states|remote|.+,\s*[a-z]{2})$/i),
      employment: pick(/^(full-?time|part-?time|contract|internship)$/i),
      workplace: pick(/^(remote|hybrid|on-?site)$/i),
      posted: pick(/\bago\b/i)
    });
  }
  return [...seen.values()];
}
"""


def connect(port: int):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is not installed.  Run:  python -m pip install playwright")

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception:
        pw.stop()
        sys.exit(
            f"Could not reach Chrome on port {port}.\n"
            f"Start it first:  powershell -File \"{HERE / 'start_chrome.ps1'}\""
        )
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    return pw, browser, ctx


def harvest(page, target: int, scrolls: int) -> list[dict]:
    page.goto(RECOMMEND_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(12000)   # the feed renders skeleton cards for ~10s

    # Logged out, jobright bounces you to the marketing homepage - which is
    # itself full of /jobs/info/ links. So "are there job links on the page"
    # proves nothing; the only honest check is whether we stayed put. Getting
    # this wrong would silently hand back the public feed as "your matches".
    if "/jobs/recommend" not in page.url:
        sys.exit(
            f"Not signed in to jobright in this Chrome window.\n"
            f"  (asked for /jobs/recommend, landed on {page.url})\n"
            f"Sign in there, leave the window open, then re-run."
        )
    if "JOIN NOW" in page.inner_text("body")[:3000].upper():
        sys.exit(
            "jobright still shows a JOIN NOW button - you're not signed in.\n"
            "Sign in in that Chrome window, then re-run."
        )

    rows: dict[str, dict] = {}
    for i in range(scrolls):
        for r in page.evaluate(HARVEST_JS):
            rows.setdefault(r["id"], r)
        print(f"  scroll {i + 1}/{scrolls}: {len(rows)} jobs")
        if len(rows) >= target:
            break
        page.evaluate("()=>{const c=document.querySelector('[class*=jobs-page-main-content]');if(c)c.scrollTop=c.scrollHeight;}")
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(2500)

    out = []
    for r in list(rows.values())[:target]:
        # Title and company come straight off the DOM now; a card that yields
        # neither is a layout change, not a job, and is dropped rather than
        # written to the queue as an untitled row.
        if not (r.get("title") or "").strip():
            continue
        r["apply_url"] = ""
        r["collected"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out.append(r)
    return out


def resolve_apply_url(page, ctx, job: dict) -> str:
    import random, time
    time.sleep(random.uniform(8, 16))   # never hit pages in bursts
    """Open the jobright job page and follow its Apply button to the real ATS."""
    try:
        page.goto(job["jobright_url"], wait_until="domcontentloaded", timeout=25_000)
    except Exception:
        return ""   # one slow job page must not kill the whole run
    page.wait_for_timeout(2000)

    btn = page.locator("button", has_text=re.compile(r"APPLY WITH AUTOFILL|APPLY ON EMPLOYER|APPLY NOW", re.I)).first
    if not btn.count():
        btn = page.locator("button", has_text="Apply").first
    if not btn.count():
        return ""

    before = page.url
    try:
        # Short: a click that doesn't open a popup should fail fast, not stall
        # 12s per job and read as slow progress across a 40-job run.
        with ctx.expect_page(timeout=6_000) as pi:
            btn.click()
        popup = pi.value
        from autofill import keep_minimized
        keep_minimized(popup)           # the popup restores the window too
        for _ in range(30):   # the popup opens blank, then redirects through jobright to the employer
            if popup.url not in ("", "about:blank") and "jobright.ai" not in popup.url: break
            popup.wait_for_timeout(500)
        url = popup.url
        try: popup.close()
        except Exception: pass
        return url
    except Exception:
        page.wait_for_timeout(2500)
        return page.url if page.url != before else ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect jobright matches locally.")
    ap.add_argument("--limit", type=int, default=40, help="max jobs to keep")
    ap.add_argument("--scrolls", type=int, default=12)
    ap.add_argument("--resolve", action="store_true",
                    help="follow each Apply button through to the employer ATS "
                         "(slower; jobright may mark these as viewed)")
    ap.add_argument("--queue-out", default="jobs.txt",
                    help="write resolved ATS urls here for autofill.py --queue")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    pw, browser, ctx = connect(args.port)
    baseline_pages = list(ctx.pages)
    page = ctx.new_page()
    # Opening a tab restores a minimized Chrome, so push it back down.
    from autofill import keep_minimized
    keep_minimized(page)
    try:
        print(f"harvesting up to {args.limit} jobs from jobright")
        jobs = harvest(page, args.limit, args.scrolls)
        print(f"  {len(jobs)} jobs")

        if args.resolve:
            print("\nresolving employer apply links")
            unresolved = 0
            for i, job in enumerate(jobs, 1):
                url = resolve_apply_url(page, ctx, job)
                for _pg in list(ctx.pages):   # the user 2026-10-08: never leave employer tabs behind
                    if _pg is not page and _pg not in baseline_pages:
                        try: _pg.close()
                        except Exception: pass
                job["apply_url"] = url
                with open(HERE / "jr_resolved.jsonl", "a", encoding="utf-8") as fh:   # saved as we go
                    fh.write(json.dumps(job) + chr(10))
                if url.startswith("http") and "jobright.ai" not in url:
                    tag = url.split("/")[2]
                else:
                    tag, unresolved = "(none)", unresolved + 1
                print(f"  [{i}/{len(jobs)}] {job['company'][:22]:<22} {tag}"
                      f"{'   unresolved so far: %d' % unresolved if unresolved else ''}")
                if unresolved == 5 and unresolved == i:
                    print("\n  ! first 5 all failed to resolve - jobright's apply "
                          "flow has probably changed. Stopping rather than "
                          "grinding through the rest.")
                    break
    finally:
        try:
            page.close()          # don't leave a tab behind on every run
        except Exception:
            pass
        # NOT browser.close(): connect_over_cdp hands back the operator's own
        # signed-in Chrome, and closing it shuts the whole window rather than
        # detaching from it.
        pw.stop()

    with JOBS_PATH.open("w", encoding="utf-8") as fh:
        for job in jobs:
            fh.write(json.dumps(job) + "\n")
    print(f"\nwrote {len(jobs)} -> {JOBS_PATH}")

    if args.resolve:
        qp = HERE / args.queue_out
        urls = [j["apply_url"] for j in jobs
                if j["apply_url"].startswith("http") and "jobright.ai" not in j["apply_url"]]
        qp.write_text("\n".join(urls) + "\n", encoding="utf-8")
        print(f"wrote {len(urls)} apply urls -> {qp}")
        print(f"\nnext:  python autofill.py --queue {args.queue_out}")


if __name__ == "__main__":
    main()
