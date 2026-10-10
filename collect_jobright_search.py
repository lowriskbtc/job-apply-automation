#!/usr/bin/env python
"""
collect_jobright_search.py - harvest jobright's KEYWORD SEARCH, not its feed.

collect_jobright.py reads /jobs/recommend, which is jobright's algorithmic feed
and takes no query. /jobs/search?keyword=<q> does, which is the only way to go
looking for a specific kind of role (the user asked for crypto on 2026-08-08).

Same card parser as collect_jobright.py, same --resolve step through to the
employer ATS. Nothing is submitted.

  python collect_jobright_search.py crypto web3 blockchain --resolve
  python collect_jobright_search.py crypto --queue-out jobs_crypto.txt
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from collect_jobright import HARVEST_JS, connect, resolve_apply_url  # noqa: E402
import autofill as af  # noqa: E402

JOBS_URL = "https://jobright.ai/jobs/recommend"
# The search box, an antd combobox: `/jobs/search?keyword=` in the address bar
# redirects straight back to the feed, so the query has to be typed into the UI.
SEARCH_BOX = 'input[placeholder*="Search by title" i]'


def dismiss_modal(page) -> None:
    """jobright throws up promo/onboarding dialogs that swallow every click.

    Playwright's own error names it: "ant-modal-wrap ... intercepts pointer
    events". Close it rather than retrying the click for 30 seconds. Only the
    dialog's own close control is touched - nothing inside it is clicked, so a
    modal offering to do something is declined by closing, not by agreeing.
    """
    for _ in range(3):
        wrap = page.locator(".ant-modal-wrap:visible").first
        if not wrap.count():
            return
        closer = page.locator(".ant-modal-close:visible").first
        try:
            if closer.count():
                closer.click(timeout=3000)
            else:
                page.keyboard.press("Escape")
        except Exception:
            page.keyboard.press("Escape")
        page.wait_for_timeout(1200)


def harvest_query(page, query: str, scrolls: int) -> list[dict]:
    # Always reload the feed before each query. After a search, jobright swaps
    # the results view in and the search box's placeholder changes with it, so a
    # second query on the same page could not find the box at all - the first
    # run got "crypto" and "blockchain" and then died on "web3".
    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(4500)
    # Same signed-in check as collect_jobright: logged out, jobright bounces to
    # a marketing page that is also full of /jobs/info/ links, so "there are job
    # links" proves nothing.
    if "/jobs" not in page.url:
        sys.exit(f"  ! not signed in to jobright (landed on {page.url}).")

    box = page.locator(SEARCH_BOX).first
    if not box.count():
        box = page.locator('input[type="search"]:visible').first
    if not box.count():
        sys.exit("  ! jobright's search box is not on the page - layout changed.")

    # The promo dialog does not appear on load - it arrives a few seconds later,
    # so dismissing once before clicking is not enough. Dismiss, click, and if
    # one lands between the two, dismiss and try again.
    for attempt in range(4):
        dismiss_modal(page)
        try:
            box.click(timeout=5000)
            break
        except Exception:
            if attempt == 3:
                sys.exit("  ! jobright keeps a modal over the search box - "
                         "open that Chrome window, close the dialog, and re-run.")
            page.wait_for_timeout(1500)
    box.fill("")
    box.type(query, delay=60)
    page.keyboard.press("Enter")
    page.wait_for_timeout(5000)

    rows: dict[str, dict] = {}
    for _ in range(scrolls):
        for r in page.evaluate(HARVEST_JS):
            rows.setdefault(r["id"], r)
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1800)
    for r in rows.values():
        r["query"] = query
        r["collected"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        r["apply_url"] = ""
    return [r for r in rows.values() if (r.get("title") or "").strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest jobright by keyword.")
    ap.add_argument("queries", nargs="+")
    ap.add_argument("--scrolls", type=int, default=6)
    ap.add_argument("--resolve", action="store_true",
                    help="follow each Apply button through to the employer ATS")
    ap.add_argument("--queue-out", default="jobs_search.txt")
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args()

    pw, browser, ctx = connect(args.port)
    page = ctx.new_page()
    af.keep_minimized(page)
    jobs: dict[str, dict] = {}
    try:
        for q in args.queries:
            found = harvest_query(page, q, args.scrolls)
            fresh = [r for r in found if r["id"] not in jobs]
            for r in fresh:
                jobs[r["id"]] = r
            print(f"  {q:<28} {len(found):>3} results, {len(fresh):>3} new")
            time.sleep(3)

        # Drop anything already applied to before spending a resolve on it.
        skipped = 0
        for r in list(jobs.values()):
            if af.already_applied(r["jobright_url"]):
                jobs.pop(r["id"])
                skipped += 1
        if skipped:
            print(f"\n  {skipped} already applied to - dropped")

        if args.resolve:
            print("\nresolving employer apply links")
            for i, job in enumerate(list(jobs.values()), 1):
                url = resolve_apply_url(page, ctx, job)
                job["apply_url"] = url
                tag = url.split("/")[2] if url.startswith("http") and "jobright.ai" not in url else "(none)"
                print(f"  [{i}/{len(jobs)}] {job['company'][:22]:<22} {tag}")
                time.sleep(2)
    finally:
        try:
            page.close()
        except Exception:
            pass
        pw.stop()

    out = HERE / "jobs_search.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for j in jobs.values():
            fh.write(json.dumps(j) + "\n")
    print(f"\nwrote {len(jobs)} -> {out.name}")

    if args.resolve:
        urls = [j["apply_url"] for j in jobs.values()
                if j["apply_url"].startswith("http") and "jobright.ai" not in j["apply_url"]]
        (HERE / args.queue_out).write_text("\n".join(urls) + "\n", encoding="utf-8")
        print(f"wrote {len(urls)} apply urls -> {args.queue_out}")


if __name__ == "__main__":
    main()
