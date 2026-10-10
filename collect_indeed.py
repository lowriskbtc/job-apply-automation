#!/usr/bin/env python
"""
collect_indeed.py - pull Indeed search results into a local queue file.

Same shape as collect_jobright.py: this is a source of URLs, autofill.py does
the form work. The difference is that Indeed hosts a lot of its own apply
flows, and those are NOT queued - see "the split" below.

Attaches to the Chrome started by start_chrome.ps1. Nothing here logs in for
you, nothing is submitted, and no CAPTCHA is answered.

The split
  "Apply on company site"  -> deep-links to Greenhouse/Lever/etc, which is
                              exactly what autofill.py handles. Queued.
  "Apply now" (Indeed)     -> Indeed's own multi-step wizard on
                              smartapply.indeed.com. autofill.py fills ONE
                              page and evaluates it once, so it cannot drive a
                              stepper safely. Recorded, tagged, NOT queued.

Usage
  python collect_indeed.py --what "AI engineer" --where Remote
  python collect_indeed.py --what "python developer" --resolve --limit 30
  python collect_indeed.py --what "llm engineer" --days 7 --resolve
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

HERE = Path(__file__).resolve().parent
JOBS_PATH = HERE / "jobs_indeed.jsonl"
DEFAULT_PORT = 9222
BASE = "https://www.indeed.com"

# Indeed is behind Cloudflare and is considerably more hostile to automation
# than jobright. A challenged page still returns 200 with a normal-looking
# body, so "we got HTML back" proves nothing - these markers are how you tell
# a real result page from an interstitial.
BLOCK_MARKERS = (
    "just a moment", "verifying you are human", "additional verification",
    "cf-turnstile", "challenge-form", "px-captcha", "/hcaptcha",
    "unusual traffic", "help us protect", "verify you are a human",
)

# Cards carry data-jk; the anchor form is the fallback for layouts that don't.
# Both are read because Indeed A/B-tests its results markup constantly, and a
# single selector that silently matches nothing looks identical to "no jobs".
HARVEST_JS = r"""
() => {
  const grab = t => (t || '').replace(/\s+/g, ' ').trim();
  const seen = new Map();

  const add = (id, el) => {
    if (!id || seen.has(id)) return;

    // The results list carries a hidden template row with a rotating fake id
    // ('789abcdef0123456'), duplicating a real posting. Left in, it becomes a
    // job you can never open.
    if (!el.getClientRects().length) return;

    // The [data-jk] element IS the title anchor (a.jcs-JobTitle), so the title
    // is its own text - not something to look for underneath it. The 'title'
    // attribute is the untruncated version when the card clips a long one.
    let title = '';
    const st = el.querySelector('span[title]') ||
               (el.matches('span[title]') ? el : null);
    if (st) title = st.getAttribute('title') || grab(st.innerText);
    if (!title) title = grab(el.innerText);
    if (!title) title = grab(el.getAttribute('aria-label'))
                          .replace(/^full details of /i, '');

    // Walk up to the whole card for company/location, which do sit underneath.
    let card = el, best = el;
    for (let i = 0; i < 6 && card; i++) {
      const t = grab(card.innerText);
      if (t.length > 25 && t.length < 800) best = card;
      if (t.length >= 800) break;
      card = card.parentElement;
    }
    const pick = sels => {
      for (const s of sels) {
        const n = best.querySelector(s);
        if (n) { const t = grab(n.innerText); if (t) return t; }
      }
      return '';
    };
    if (!title) {
      title = pick(['a.jcs-JobTitle', 'h2.jobTitle span[title]', 'h2.jobTitle',
                    '[data-testid="jobTitle"]', 'h2']);
    }

    seen.set(id, {
      id,
      title: title.slice(0, 120),
      company: pick(['[data-testid="company-name"]', '.companyName',
                     '[data-testid="companyName"]',
                     'span.css-1h7lukg']).slice(0, 80),
      location: pick(['[data-testid="text-location"]', '.companyLocation',
                      '[data-testid="jobLocation"]']).slice(0, 80),
      // Kept so a run with dead selectors is still diagnosable rather than
      // just yielding rows of empty strings.
      card_text: grab(best.innerText).slice(0, 200)
    });
  };

  for (const el of document.querySelectorAll('[data-jk]')) {
    add(el.getAttribute('data-jk'), el);
  }
  for (const a of document.querySelectorAll('a[href*="jk="]')) {
    const m = (a.getAttribute('href') || '').match(/[?&]v?jk=([a-z0-9]+)/i);
    if (m) add(m[1], a);
  }
  return [...seen.values()];
}
"""

# On the job page: read the button BEFORE clicking. Clicking "Apply now" opens
# Indeed's wizard and can mark the job as started, so an Indeed-hosted job is
# classified by its text and then left alone.
APPLY_JS = r"""
() => {
  const grab = t => (t || '').replace(/[\s ]+/g, ' ').trim();

  // Indeed's own flow is identified by the button ID, not its wording. The
  // label is "Apply with Indeed" today and was "Apply now" before that; a
  // text-only rule quietly filed these as 'unknown', which reads as "couldn't
  // reach the page" rather than "this one is Indeed-hosted".
  const ia = document.querySelector('#indeedApplyButton, [id^="indeedApply"]');
  if (ia && ia.getClientRects().length) {
    return { kind: 'indeed_hosted',
             text: grab(ia.innerText) || 'apply with indeed', href: '' };
  }

  const nodes = [...document.querySelectorAll(
    'button, a, [role="button"], #applyButtonLinkContainer a')];
  for (const n of nodes) {
    const txt = grab(n.innerText || n.getAttribute('aria-label')).toLowerCase();
    if (!txt || txt.length > 70) continue;
    if (/apply on (company|employer)|apply on the company|company site/.test(txt)) {
      return { kind: 'employer_site', text: txt,
               href: n.getAttribute('href') || '' };
    }
    if (/apply with indeed|^apply now|^apply$|easily apply|indeed apply/.test(txt)) {
      return { kind: 'indeed_hosted', text: txt,
               href: n.getAttribute('href') || '' };
    }
  }
  const body = grab(document.body.innerText).toLowerCase();
  if (/no longer accepting|job has expired|this job has expired/.test(body)) {
    return { kind: 'expired', text: '', href: '' };
  }
  return { kind: 'unknown', text: '', href: '' };
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


def check_not_blocked(page, where: str) -> None:
    """
    Bail loudly on a Cloudflare/PerimeterX interstitial.

    This has to be fatal, not a warning. A challenged page has no job cards on
    it, so a soft failure would write an empty queue and read as 'no results
    today' - the one outcome that looks like success while being nothing of
    the sort. Verified behaviour, not theory: an automation-controlled browser
    pointed at /jobs gets served 'Just a moment...' and never reaches results.
    """
    try:
        head = page.inner_text("body")[:4000].lower()
    except Exception:
        head = ""
    hit = next((m for m in BLOCK_MARKERS if m in head or m in page.url.lower()), None)
    if hit:
        sys.exit(
            f"\nIndeed served a bot challenge on {where} (matched: {hit!r}).\n"
            f"  landed on: {page.url[:100]}\n\n"
            f"Nothing here solves a CAPTCHA. Open that URL in the Chrome window\n"
            f"start_chrome.ps1 opened, clear the challenge by hand, then re-run -\n"
            f"the session cookie usually carries the rest of the run."
        )


def search_url(what: str, where: str, days: int, start: int) -> str:
    url = f"{BASE}/jobs?q={quote_plus(what)}&l={quote_plus(where)}"
    if days:
        url += f"&fromage={days}"
    if start:
        url += f"&start={start}"
    return url


def harvest(page, rows: dict, what: str, where: str, days: int, target: int,
            pages: int) -> None:
    """
    Fill `rows` (owned by the caller) with whatever we get.

    The accumulator belongs to the caller on purpose: a challenge partway
    through calls sys.exit(), and everything harvested up to that point should
    still reach disk rather than dying with the process.
    """
    # Indeed paginates with &start=N in steps of 10 - it is not an infinite
    # scroll, so scrolling the way the jobright harvester does gets you page 1
    # over and over.
    for p in range(pages):
        url = search_url(what, where, days, p * 10)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2500)
        check_not_blocked(page, f"page {p + 1}")

        if "/jobs" not in page.url:
            sys.exit(
                f"Asked for a search page, landed on {page.url[:90]}.\n"
                f"Indeed redirected the search - check that URL by hand."
            )

        before = len(rows)
        for r in page.evaluate(HARVEST_JS):
            rows.setdefault(r["id"], r)
        found = len(rows) - before
        print(f"  page {p + 1}: +{found} ({len(rows)} total)")

        if found == 0:
            # Either the end of the results or dead selectors. Both mean
            # 'stop', but they are worth telling apart out loud.
            if p == 0:
                print("  ! page 1 yielded nothing. Either no results for this "
                      "search, or Indeed changed its card markup.")
            break
        if len(rows) >= target:
            break
        page.wait_for_timeout(random.randint(9000, 16000))   # human pace


def finalize(rows: dict, target: int) -> list[dict]:
    out = []
    for r in list(rows.values())[:target]:
        r["job_url"] = f"{BASE}/viewjob?jk={r['id']}"
        r.setdefault("apply_url", "")
        r.setdefault("apply_kind", "")
        r["collected"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out.append(r)

    # Cards are scraped by selector and Indeed rewrites its results markup
    # often. Mostly-empty titles means the selectors moved, which otherwise
    # reads identically to a good run - "page 1: +15" either way.
    if out:
        blank = sum(1 for r in out if not r.get("title"))
        if blank > len(out) / 2:
            print(f"\n  ! {blank}/{len(out)} cards have no title - Indeed has "
                  f"probably changed its markup.\n"
                  f"    Check the card_text field in jobs_indeed.jsonl; the job "
                  f"ids are still good even if the labels aren't.")
    return out


def resolve_apply(page, ctx, job: dict) -> tuple[str, str, str]:
    """
    Open the job page and work out where its Apply button actually goes.

    Returns (kind, url, button_text). Only 'employer_site' comes back with a
    URL worth queueing; an Indeed-hosted job is deliberately left unclicked.

    The button text comes back so the classification is auditable. A job
    misread as Indeed-hosted just quietly doesn't get queued, and the patterns
    in APPLY_JS have not been checked against a live Indeed page - so the run
    prints what it actually matched on rather than only its own verdict.
    """
    try:
        page.goto(job["job_url"], wait_until="domcontentloaded", timeout=45_000)
    except Exception:
        return "error", "", ""
    page.wait_for_timeout(random.randint(3500, 6500))
    check_not_blocked(page, f"job {job['id']}")

    try:
        info = page.evaluate(APPLY_JS)
    except Exception:
        return "error", "", ""

    kind, btn = info["kind"], info.get("text", "")
    if kind != "employer_site":
        return kind, "", btn

    # An href straight to the employer saves a click; Indeed often uses a real
    # anchor here rather than a JS handler.
    href = info.get("href") or ""
    if href.startswith("http") and "indeed.com" not in href:
        return kind, href, btn

    before = page.url
    try:
        loc = page.locator("#indeedApplyButton, a, button").filter(
            has_text="Apply on company site").first
        if not loc.count():
            loc = page.locator("a, button").filter(has_text="company site").first
        if not loc.count():
            return kind, "", btn
        # A click that doesn't open a popup should fail fast rather than stall
        # six seconds on every job in a forty-job run.
        with ctx.expect_page(timeout=6_000) as pi:
            loc.click()
        popup = pi.value
        from autofill import keep_minimized
        keep_minimized(popup)           # the popup restores the window too
        popup.wait_for_load_state("domcontentloaded", timeout=20_000)
        url = popup.url
        popup.close()
        return kind, ("" if "indeed.com" in url else url), btn
    except Exception:
        page.wait_for_timeout(2000)
        if page.url != before and "indeed.com" not in page.url:
            return kind, page.url, btn
        return kind, "", btn


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect Indeed search results locally.")
    ap.add_argument("--what", help="job title / keywords")
    ap.add_argument("--where", help="location, e.g. Remote")
    ap.add_argument("--days", type=int, default=0,
                    help="only postings from the last N days (Indeed's fromage)")
    ap.add_argument("--limit", type=int, default=40, help="max jobs to keep")
    ap.add_argument("--pages", type=int, default=5, help="result pages to walk")
    ap.add_argument("--resolve", action="store_true",
                    help="open each job to classify its Apply button and "
                         "capture the employer URL (slower)")
    ap.add_argument("--queue-out", default="jobs_indeed.txt",
                    help="write employer-site urls here for autofill.py --queue")
    ap.add_argument("--profile", help="alternate profile.json path")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    # Search terms fall back to a `search` block in profile.json so the common
    # case is just `python collect_indeed.py --resolve`.
    prof_path = Path(args.profile) if args.profile else HERE / "profile.json"
    search_cfg = {}
    if prof_path.exists():
        try:
            search_cfg = json.loads(prof_path.read_text(encoding="utf-8")).get("search", {})
        except ValueError:
            pass
    what = args.what or search_cfg.get("what")
    where = args.where or search_cfg.get("where", "Remote")
    days = args.days or search_cfg.get("days", 0)
    if not what:
        ap.error("give --what, or add a \"search\" block to profile.json")

    raw: dict[str, dict] = {}
    jobs: list[dict] = []

    pw, browser, ctx = connect(args.port)
    page = ctx.new_page()
    # Opening a tab restores a minimized Chrome, so push it back down.
    from autofill import keep_minimized
    keep_minimized(page)
    try:
        print(f"searching Indeed: {what!r} in {where!r}"
              f"{f' (last {days}d)' if days else ''}")
        harvest(page, raw, what, where, days, args.limit, args.pages)
        jobs = finalize(raw, args.limit)
        print(f"  {len(jobs)} jobs")

        if args.resolve:
            print("\nclassifying apply buttons")
            misses = 0
            for i, job in enumerate(jobs, 1):
                kind, url, btn = resolve_apply(page, ctx, job)
                job["apply_kind"], job["apply_url"] = kind, url
                job["apply_button"] = btn
                tag = url.split("/")[2] if url.startswith("http") else kind
                note = f"   <- {btn[:34]!r}" if btn else ""
                print(f"  [{i}/{len(jobs)}] {(job['company'] or '?')[:22]:<22} "
                      f"{tag}{note}")

                # check_not_blocked can't always see a challenge - on a dying
                # tab inner_text() throws, the body reads empty, and every job
                # after that lands in 'error' looking like an ordinary miss.
                # So a run of failures stops the run, the same way the jobright
                # collector refuses to grind through a broken apply flow.
                misses = 0 if kind in ("employer_site", "indeed_hosted") else misses + 1
                if misses >= 5:
                    print(f"\n  ! 5 in a row failed to classify - Indeed has "
                          f"probably challenged this session or changed the "
                          f"job page.\n    Stopping; the {i} done so far are "
                          f"kept.")
                    break

                # Indeed rate-limits a fast crawl into a challenge page, and a
                # challenge kills the whole run. Jitter is cheaper than a retry.
                page.wait_for_timeout(random.randint(14000, 28000))   # human pace between job pages
    finally:
        try:
            page.close()          # don't leave a tab behind on every run
        except Exception:
            pass
        # NOT browser.close(): connect_over_cdp hands back the operator's own
        # signed-in Chrome, and closing it shuts the whole window rather than
        # detaching from it.
        pw.stop()
        # A challenge calls sys.exit(), which unwinds through here. Writing in
        # the finally is what stops a block on job 31 of 40 from throwing away
        # the 30 that already worked - and being blocked is the expected path
        # on Indeed, not the rare one.
        if not jobs:
            jobs = finalize(raw, args.limit)
        write_results(jobs, args)


def write_results(jobs: list[dict], args) -> None:
    if not jobs:
        print("\nnothing collected - nothing written.")
        return

    with JOBS_PATH.open("w", encoding="utf-8") as fh:
        for job in jobs:
            fh.write(json.dumps(job) + "\n")
    print(f"\nwrote {len(jobs)} -> {JOBS_PATH}")

    if not args.resolve:
        print("\nnext:  re-run with --resolve to find which ones autofill can drive")
        return

    urls = [j["apply_url"] for j in jobs
            if j["apply_kind"] == "employer_site" and j["apply_url"].startswith("http")]
    hosted = sum(1 for j in jobs if j["apply_kind"] == "indeed_hosted")
    other = len(jobs) - len(urls) - hosted

    qp = HERE / args.queue_out
    qp.write_text("\n".join(urls) + "\n", encoding="utf-8")

    print(f"\n  {len(urls):>3} employer-site  -> queued in {qp.name}")
    print(f"  {hosted:>3} Indeed-hosted  -> NOT queued (multi-step wizard, "
          f"apply by hand)")
    if other:
        print(f"  {other:>3} unresolved/expired/unclassified -> not queued")
    if urls:
        print(f"\nnext:  python autofill.py --queue {args.queue_out}")


if __name__ == "__main__":
    main()
