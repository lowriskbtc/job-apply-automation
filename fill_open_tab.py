#!/usr/bin/env python
"""fill_open_tab.py - fill ONE already-open tab in place (no reload, no new tab).

  python fill_open_tab.py <substring of the tab's URL>

autofill_page only writes empty fields, so anything already typed is kept.
Exactly one tab must match; it refuses otherwise.
"""
import sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
import autofill as af

def main():
    key = sys.argv[1]
    prof = af.load_profile(None)
    rules, skips = af.build_rules(prof), af.build_skips(prof)
    n = af.count_explicit(prof)
    pw, browser, ctx = af.connect(af.DEFAULT_PORT)
    try:
        hits = [p for p in ctx.pages if key in p.url]
        if len(hits) != 1:
            sys.exit(f"{len(hits)} tabs match {key!r}; need exactly 1")
        page = hits[0]
        title, company = af.page_meta(page)
        print(page.url, "|", title)
        res = af.autofill_page(page, prof, rules, skips, False, n, title, company)
        res["consent_ticked"] = af.tick_consent(res, False, False,
            policy=bool(prof.get("consent_policy", {}).get("auto_tick")))
        if "ashbyhq.com" in page.url:
            # Ashby's resume parse (a) uploads into its "autofill" box, not the real
            # Resume field, and (b) writes the name in ALL CAPS. Fix both (seen on
            # Paradigm x2 and Cohere, 2026-10-06).
            # The parse finishes AFTER the upload and rewrites name/resume, so wait
            # for it to settle and re-check up to 3 times; trust only the read-back.
            try:
                for attempt in range(3):
                    page.wait_for_timeout(5000)
                    real = page.locator('input[type=file]#_systemfield_resume, input[type=file][name="_systemfield_resume"]').first
                    if real.count() and not real.evaluate("e=>e.files.length"):
                        real.set_input_files(prof["files"]["resume"]); page.wait_for_timeout(2500)
                        print("  ashby: attached resume to the real Resume field")
                    nm = page.get_by_label("Name", exact=True).first
                    v = nm.input_value()
                    if v and v == v.upper() and len(v) > 3:
                        nm.fill(v.title()); nm.press("Tab"); print(f"  ashby: name {v!r} -> {v.title()!r}")
                ok_name = nm.input_value() != nm.input_value().upper()
                ok_res = bool(real.count()) and real.evaluate("e=>e.files.length") > 0
                print(f"  ashby verify: name_ok={ok_name} resume_attached={ok_res}")
            except Exception as exc:
                print("  ashby post-fix skipped:", str(exc)[:80])
        print(f"\nfilled ({len(res['filled'])}):")
        for f in res["filled"]: print("  +", f[:130])
        for k, lab in (("missing", "required, no rule"), ("needs_you", "yours to handle")):
            if res[k]:
                print(f"\n{lab} ({len(res[k])}):")
                for m in res[k]: print("  ?", m[:130])
        for t in res.get("consent_ticked", []): print("  [x] consent:", t)
    finally:
        pw.stop()
main()
