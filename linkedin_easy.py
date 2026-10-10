#!/usr/bin/env python
"""
linkedin_easy.py - drive LinkedIn "Easy Apply" from the same profile.json.

Reuses autofill.py's rule engine (build_rules / radio_matches / pick_option /
consent policy / the application log) but scopes every read and write to the
Easy Apply MODAL. autofill.py itself scans the whole page, which on LinkedIn
would type into the site search box.

Default is fill-only: it fills each step, advances to the review screen, and
STOPS. Nothing is submitted unless you pass --submit.

  python linkedin_easy.py                        # fill one, leave it on review
  python linkedin_easy.py --limit 8 --submit     # fill and send up to 8
  python linkedin_easy.py --dry                  # look, change nothing

Three things worth knowing before using --submit:

  * The flow carries an invisible reCAPTCHA (a g-recaptcha-response field that
    only materialises at submit). It is not the hard stop the Greenhouse/Lever
    CAPTCHAs are, but it is there, and this never touches it.
  * Driving LinkedIn's UI can get an account restricted. That account is your
    professional presence. Nothing here spoofs a user agent or touches a
    challenge screen - if LinkedIn puts up a checkpoint, this stops and hands it
    to you.
  * It moves SLOWLY on purpose - see PACE below. the user has tripped LinkedIn's
    flags himself by going too fast. Raise --pace, don't lower it.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import autofill as af  # noqa: E402

SEARCH = ("https://www.linkedin.com/jobs/search/?keywords={kw}"
          "&f_AL=true&f_WT={wt}&sortBy=DD")
# LinkedIn's current Easy Apply ("SDUI") flow renders INLINE on the job page,
# not in a [role=dialog] modal - the old scoping matched nothing and every step
# read as "modal closed". Scope by exclusion instead: everything except the site
# chrome (global search, the job-alert toggle, language picker, reCAPTCHA).
OUT_OF_SCOPE = re.compile(
    r"skip to |search by title|city, state|set alert|select language|"
    r"g-recaptcha|jobs-search-box|typeahead", re.I)
# The flow is present when its footer is: that button only exists inside it.
# As of 2026-08-12 LinkedIn ships the footer button with NO aria-label and the
# visible text "Next", so the aria-label-only selector matched nothing and every
# posting reported "the flow never rendered" even though it had opened fine.
# Text selectors are kept LAST so an aria-labelled build still wins.
IN_FLOW = ('button[aria-label*="Continue to next step" i], '
           'button[aria-label*="Review your application" i], '
           'button[aria-label*="Submit application" i], '
           'button:has-text("Submit application"), '
           'button:text-is("Next"), '
           'button:text-is("Review")')

# Set from --accept-account-email in main(); read inside fill_modal.
ACCEPT_ACCOUNT_EMAIL = [False]

# Pacing. the user has tripped LinkedIn's flags himself by moving too fast, so
# the defaults here are deliberately slow and the volume per run is small.
#
# This is throttling, not evasion: fixed, generous waits that put LESS load on
# LinkedIn than a person clicking quickly would. It is not jitter tuned to look
# human, it does not touch a checkpoint, and it does not disguise anything - if
# LinkedIn challenges the session the run stops and hands it over. Keep it that
# way; making this "look human" would be a different thing entirely.
PACE = {
    "between_jobs": 60.0,   # after finishing one posting, before opening the next
    "after_load": 6.0,      # after any navigation, before reading the page
    "between_steps": 5.0,   # after clicking Continue, before filling the next step
    "scroll": 2.5,          # between scrolls on the search results
}


def pause(kind: str) -> None:
    time.sleep(PACE[kind])
CHALLENGE = re.compile(r"security verification|let's do a quick security check|"
                       r"/checkpoint/challenge|are you a human|unusual activity", re.I)


def canonical(job_id: str) -> str:
    """The URL to LOG. Always /jobs/view/<id>/ - never the search URL.

    autofill.norm_url() keeps `currentjobid`, so a search URL normalises to
    .../jobs/search?currentjobid=<id> while the posting normalises to
    .../jobs/view/<id>. Both are unique per job, but they are DIFFERENT keys for
    the same job - log one and apply from the other and already_applied() misses
    the duplicate. Verified 2026-08-08.
    """
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def blocked(page) -> str:
    try:
        head = page.inner_text("body")[:1500]
    except Exception:
        return ""
    if CHALLENGE.search(head) or "/checkpoint/" in page.url:
        return "LinkedIn put up a security checkpoint"
    return ""


def find_jobs(page, keywords: str, remote: bool, want: int) -> list[dict]:
    url = SEARCH.format(kw=quote(keywords), wt="2" if remote else "")
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    pause("after_load")
    if b := blocked(page):
        sys.exit(f"  ! {b} - clear it yourself in that Chrome window, then re-run.")
    if "/jobs/search" not in page.url:
        sys.exit(f"  ! not signed in to LinkedIn (landed on {page.url}).")

    out: dict[str, dict] = {}
    for _ in range(8):
        rows = page.evaluate("""
        () => [...document.querySelectorAll('a[href*="/jobs/view/"]')].map(a => {
          const m = a.getAttribute('href').match(/\\/jobs\\/view\\/(\\d+)/);
          if (!m) return null;
          const card = a.closest('li, .job-card-container') || a;
          const t = (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
          return {id: m[1], title: t[0] || '', company: t[1] || '',
                  easy: /easy apply/i.test(card.innerText || '')};
        }).filter(Boolean)
        """)
        for r in rows:
            out.setdefault(r["id"], r)
        if len(out) >= want * 3:
            break
        page.mouse.wheel(0, 2500)
        pause("scroll")
    return list(out.values())


def modal_fields(page):
    """autofill's field reader, with LinkedIn's own site chrome dropped."""
    kept = []
    for h, info in af.collect_fields(page):
        blob = f"{info.get('label','')} {info.get('name','')} {info.get('id','')}"
        if OUT_OF_SCOPE.search(blob):
            continue
        kept.append((h, info))
    return kept


def fill_modal(page, prof, rules, n_explicit, dry: bool) -> dict:
    """One step of the modal. Mirrors autofill_page's decisions, modal-scoped."""
    res = {"filled": [], "missing": [], "needs_you": [], "consent": [], "resume": []}

    fields = modal_fields(page)

    # Same multi-select guard as autofill_page(): a checkbox sharing its question
    # with other checkboxes is never auto-ticked, whatever it says. Without this,
    # an "I consent to..." option sitting inside a required multi-select would be
    # ticked here even though autofill.py refuses to.
    cb_group_size: dict[str, int] = {}
    for _h, _i in fields:
        if _i["type"] != "checkbox":
            continue
        _k = af.norm_label(_i.get("group", "") or _i.get("group_long", "") or "")
        if _k:
            cb_group_size[_k] = cb_group_size.get(_k, 0) + 1

    for h, info in fields:
        ftype = info["type"]
        label = info["label"] or info["name"] or info["id"]
        ident = f"{label}"[:70]
        blob = f"{label} {info['name']} {info['id']}"

        if ftype == "file":
            path = af.resolve_file(prof, info["label"], info["name"], info["id"], "", "")
            if not path:
                res["needs_you"].append(f"{ident} (no file configured)")
                continue
            if dry:
                res["resume"].append(f"{ident} <- {Path(path).name} (dry)")
                continue
            try:
                h.set_input_files(path)
                res["resume"].append(f"{ident} <- {Path(path).name}")
            except Exception as exc:
                res["needs_you"].append(f"{ident} (upload failed: {exc})")
            continue

        if ftype in ("checkbox", "radio"):
            group = info.get("group", "") or info.get("group_long", "") or label
            if ftype == "radio":
                done = False
                for pat, vals in rules[:n_explicit]:
                    if not pat.search(group):
                        continue
                    if any(af.radio_matches(label, v) for v in vals):
                        if not dry:
                            try:
                                h.check()
                            except Exception:
                                break
                        res["filled"].append(f"[radio] {group[:50]} <- {label[:30]}")
                        done = True
                        break
                if done:
                    continue
            # Only a pattern the operator wrote may tick a box, and only when the
            # box stands alone. Required-ness must NOT reach here: a required
            # multi-select marks every option required, so "tick what's required"
            # ticks every option of it (see the WelbeHealth note in
            # autofill.tick_consent).
            multi = cb_group_size.get(af.norm_label(group), 0) > 1
            if (ftype == "checkbox" and not multi
                    and af.consent_policy_match(prof, group, ident)):
                res["consent"].append((h, ident))
                continue
            res["needs_you"].append(f"[{ftype}] {ident}")
            if group:
                af.unmatched_q(group, label)
            continue

        # A pre-selected dropdown is NOT left alone. LinkedIn defaults the email
        # picker to whichever address is on the account - linkedin-account@example.com
        # here, not the you@example.com in profile.json - so "already filled"
        # would silently send applications to the wrong inbox.
        if info["filled"] and ftype != "select":
            continue

        values = None
        for pat, vals in rules:
            if pat.search(blob):
                values = vals
                break
        if values is None:
            (res["missing"] if info["required"] else res["needs_you"]).append(ident)
            if info["required"]:
                af.unmatched_q(label, "")
            continue

        vals = values if isinstance(values, list) else [values]
        vals = [af.expand_tokens(str(v)) for v in vals]
        if dry:
            res["filled"].append(f"{ident} <- {vals[0][:40]} (dry)")
            continue
        try:
            if ftype == "select":
                opts = h.evaluate("el => [...el.options].map(o => o.text)")
                pick = None
                for v in vals:
                    pick = af.pick_option(opts, v)
                    if pick:
                        break
                if not pick:
                    # The email picker only ever offers addresses verified on
                    # the LinkedIn ACCOUNT. the user's is linkedin-account@example.com;
                    # profile.json (and the resume) say you@example.com. Sending
                    # with the wrong one silently is the same class of bug as
                    # the resumai.pdf attachment, so this stops instead - unless
                    # --accept-account-email says the account's address is fine.
                    if re.search(r"e-?mail", ident, re.I):
                        if ACCEPT_ACCOUNT_EMAIL[0] and len(opts) > 1:
                            h.select_option(index=1)
                            res["filled"].append(
                                f"{ident} <- {opts[1][:40]}  (LinkedIn account address, "
                                f"NOT {vals[0][:24]})")
                            continue
                        res["missing"].append(
                            f"{ident}: LinkedIn only offers {[o for o in opts if 'Select' not in o]}, "
                            f"and profile.json says {vals[0][:30]!r}. Add that address to "
                            f"your LinkedIn account, or re-run with --accept-account-email.")
                        continue
                    res["missing"].append(f"{ident} (no option matched {vals[0][:24]!r})")
                    continue
                h.select_option(label=pick)
                res["filled"].append(f"{ident} <- {pick[:40]}")
            else:
                h.fill(vals[0])
                got = h.input_value()
                if got.strip() != vals[0].strip():
                    got = af.repair_value(h, vals[0], got)
                res["filled"].append(
                    f"{ident} <- {vals[0][:40]}"
                    + ("  (form rewrote it)" if got.strip() != vals[0].strip() else ""))
        except Exception as exc:
            res["needs_you"].append(f"{ident} (could not fill: {exc})")

    for h, ident in res["consent"]:
        if dry:
            res["filled"].append(f"[x] {ident[:50]} (dry)")
            continue
        try:
            h.check()
            res["filled"].append(f"[x] consent ticked: {ident[:50]}")
        except Exception:
            res["needs_you"].append(f"[checkbox] {ident} (COULD NOT TICK)")
    return res


def resume_on_page(page) -> bool:
    """Is the profile resume actually showing on the review screen?

    The upload report is not evidence - that lesson cost a batch of Greenhouse
    applications on 08-07. Read the filename back off the page.
    """
    want = Path(af.load_profile()["files"]["resume"]).name.lower()
    try:
        return want in (page.inner_text("body") or "").lower()
    except Exception:
        return False


def footer_button(page, *labels):
    """Find a flow footer button by aria-label, falling back to visible text.

    The 2026-08-12 LinkedIn build renders these with no aria-label at all - the
    Next button is just <button>Next</button> - so the aria-label lookup returned
    None and the flow could never be advanced. aria-label is still tried first so
    a build that has it keeps the more precise match.
    """
    for lab in labels:
        b = page.locator(f'button[aria-label*="{lab}"]').last
        try:
            if b.count() and b.is_visible():
                return b
        except Exception:
            continue
    for lab in labels:
        try:
            b = page.get_by_role("button", name=lab, exact=True).last
            if b.count() and b.is_visible():
                return b
        except Exception:
            continue
    # Looser last resort: accessible names drift ("Submit application" vs
    # "Submit Application", trailing whitespace), and an exact-only match left the
    # 2026-08-12 run stranded on the review step reporting "no Continue/Review
    # button at step 4". Exact is tried first above so this cannot override a
    # precise hit.
    for lab in labels:
        try:
            b = page.get_by_role("button", name=lab, exact=False).last
            if b.count() and b.is_visible():
                return b
        except Exception:
            continue
    return None


SDUI_RADIOGROUP_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('fieldset[role=radiogroup]').forEach((fs, gi) => {
    const opts = Array.from(fs.querySelectorAll('[role=radio]'));
    if (!opts.length) return;
    // The question is NOT inside the fieldset - the fieldset's own text is just
    // the option labels. Climb until an ancestor holds more text than the
    // options do, and subtract the options to leave the question.
    const optText = opts.map(o => (o.innerText || '').trim()).join(' ');
    let q = '', n = fs;
    for (let i = 0; i < 4 && n; i++) {
      n = n.parentElement;
      if (!n) break;
      const t = (n.innerText || '').replace(/\s+/g, ' ').trim();
      if (t.length > optText.length + 3) { q = t; break; }
    }
    opts.forEach(o => {
      const t = (o.innerText || '').replace(/\s+/g, ' ').trim();
      if (q.endsWith(t)) q = q.slice(0, -t.length).trim();
    });
    const answered = opts.some(o => o.getAttribute('aria-checked') === 'true');
    // Address everything by INDEX, never by an injected data- attribute: React
    // re-renders this flow and wipes added attributes, which made the verify
    // step look up a node that no longer existed and report a successful click
    // as "did not stick".
    out.push({
      gi: gi,
      question: q.slice(0, 160),
      answered: answered,
      options: opts.map((o, oi) => ({
        oi: oi,
        text: (o.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60)
      })),
    });
  });
  return out;
}
"""


def fill_sdui_radiogroups(page, rules, dry: bool):
    """Answer LinkedIn's SDUI radio groups, which autofill.py cannot see.

    Why this is needed (2026-08-12): in this flow the <input type=radio> carries
    an EMPTY <label>, no aria-label, and `value="on"` for BOTH options - so
    nothing autofill inspects distinguishes Yes from No. The visible option text
    lives on an ancestor div[role=radio], and the question text lives OUTSIDE the
    fieldset entirely. So collect_fields() saw two indistinguishable radios and
    skipped them silently, the step went out blank, and LinkedIn rejected it -
    "form rejected step 4: Are you legally authorized to work in the United
    States?" - which is the blocker that has stopped every Easy Apply run since
    2026-07-28.

    Answers only from the SAME profile.json rules the rest of the system uses.
    Returns (filled, unanswered).
    """
    filled, unanswered = [], []
    try:
        groups = page.evaluate(SDUI_RADIOGROUP_JS)
    except Exception:
        return filled, unanswered
    for g in groups:
        if g["answered"] or not g["question"]:
            continue
        values = None
        for pat, vals in rules:
            if pat.search(g["question"]):
                values = vals
                break
        if values is None:
            unanswered.append(g["question"][:90])
            continue
        pick = None
        for v in values:
            for o in g["options"]:
                if o["text"].strip().lower() == str(v).strip().lower():
                    pick = o
                    break
            if pick:
                break
        if pick is None:
            unanswered.append(f"{g['question'][:70]} (offers {[o['text'] for o in g['options']][:4]})")
            continue
        if dry:
            filled.append(f"[radio] {g['question'][:60]} <- {pick['text']} (dry)")
            continue
        # Click IN THE PAGE rather than through Playwright. The <input> is
        # positioned off-screen, and Playwright refuses to click an element
        # outside the viewport even with force=True ("Element is outside of the
        # viewport") - that exception was being swallowed, so a click that never
        # happened was reported as one that did not stick. A JS click has no
        # viewport constraint and still fires React's handler.
        page.evaluate(
            """([gi, oi]) => {
                const fs = document.querySelectorAll('fieldset[role=radiogroup]')[gi];
                if (!fs) return;
                const o = fs.querySelectorAll('[role=radio]')[oi];
                if (!o) return;
                const inp = o.querySelector('input');
                if (inp) { inp.click(); } else { o.click(); }
            }""",
            [g["gi"], pick["oi"]])
        page.wait_for_timeout(500)
        # Read the state back rather than trust the click: this widget is exactly
        # the kind that accepts a click and stays unset. Re-query from scratch so
        # a React re-render cannot make a real success look like a failure.
        ok = page.evaluate(
            """([gi, oi]) => {
                const fs = document.querySelectorAll('fieldset[role=radiogroup]')[gi];
                if (!fs) return false;
                const o = fs.querySelectorAll('[role=radio]')[oi];
                if (!o) return false;
                const inp = o.querySelector('input');
                return o.getAttribute('aria-checked') === 'true'
                       || !!(inp && inp.checked);
            }""",
            [g["gi"], pick["oi"]])
        if ok:
            filled.append(f"[radio] {g['question'][:60]} <- {pick['text']}")
        else:
            unanswered.append(f"{g['question'][:70]} (clicked {pick['text']}, did not stick)")
    return filled, unanswered


def in_flow(page) -> bool:
    """Is the Easy Apply flow on screen?

    Detection cannot be a CSS string any more. On the 2026-08-12 build the footer
    button's accessible name is "Next" while its DOM text is nested, so
    `button:text-is("Next")` counts ZERO on a flow that is plainly open - the run
    reported "the flow never rendered" while the page showed "Apply to Atos,
    1/4 pages". get_by_role() computes the accessible name and finds it, so the
    role lookup in footer_button() is the reliable check and the CSS list is only
    a fast path.
    """
    try:
        if page.locator(IN_FLOW).count():
            return True
    except Exception:
        pass
    return footer_button(page, "Continue to next step", "Review your application",
                         "Submit application", "Next", "Review") is not None


def apply_one(page, job, prof, rules, n_explicit, dry: bool, submit: bool) -> tuple[str, str]:
    url = canonical(job["id"])
    if prior := af.already_applied(url):
        return "duplicate", f"already submitted {prior['ts'][:10]}"

    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    pause("after_load")
    if b := blocked(page):
        return "blocked", b

    # LinkedIn's current UI renders Easy Apply as an <a aria-label="Easy Apply to
    # this job">, not a <button class="jobs-apply-button"> - the old selector
    # matched nothing and every posting read as "not Easy Apply". Both forms are
    # accepted so this survives the next redesign in one direction at least.
    btn = page.locator(
        'a[aria-label*="Easy Apply" i], button[aria-label*="Easy Apply" i], '
        'button.jobs-apply-button, a.jobs-apply-button').first
    if not btn.count():
        return "not_easy_apply", "no Easy Apply control on this posting"
    label = ((btn.get_attribute("aria-label") or "") + " " + (btn.inner_text() or "")).strip()
    if re.search(r"\bapplied\b", label, re.I):
        return "duplicate", "LinkedIn already shows this as applied"
    if "easy apply" not in label.lower():
        return "not_easy_apply", f"apply control reads {label[:40]!r}"

    # Two different controls have shipped here, so handle both.
    #
    #  * <a href=".../apply/?openSDUIApplyFlow=true"> - navigate to the href; a
    #    synthetic click on an anchor leaves the page where it is.
    #  * <button aria-label="Easy Apply to this job"> with NO href - click it,
    #    because there is nowhere to navigate to. This is what LinkedIn serves as
    #    of 2026-08-12, and the old href-only path rejected every posting with
    #    "Easy Apply link goes somewhere else:" and an empty href - 7 of 7 on that
    #    day's search, so the driver looked broken when it was just out of date.
    href = btn.get_attribute("href") or ""
    if "/apply" in href:
        page.goto(href, wait_until="domcontentloaded", timeout=60_000)
    elif (btn.evaluate("e => e.tagName") or "").upper() == "BUTTON":
        btn.click(timeout=15_000)
        # The flow renders inline rather than as a dialog, so there is no
        # navigation to wait on - wait for the flow's own controls to appear.
        deadline = 20
        while deadline > 0 and not in_flow(page):
            page.wait_for_timeout(1000)
            deadline -= 1
        if not in_flow(page):
            return "needs_you", "clicked Easy Apply but the flow never rendered"
    else:
        return "not_easy_apply", f"Easy Apply link goes somewhere else: {href[:60]}"
    pause("after_load")
    if b := blocked(page):
        return "blocked", b

    steps, seen_sigs = [], set()
    for n in range(1, 12):
        if not in_flow(page):
            return "needs_you", (f"the apply flow is not on screen at step {n} "
                                 f"- no Continue/Review/Submit control")
        res = fill_modal(page, prof, rules, n_explicit, dry)
        # LinkedIn's SDUI radio groups are invisible to fill_modal - see
        # fill_sdui_radiogroups(). Run it every step and fold the results into
        # this step's record so the submit gate below still sees anything left
        # unanswered.
        sdui_filled, sdui_missing = fill_sdui_radiogroups(page, rules, dry)
        res["filled"].extend(sdui_filled)
        res["missing"].extend(sdui_missing)
        steps.append(res)
        for f in res["filled"] + res["resume"]:
            print(f"      + {f}")
        for m in res["missing"]:
            print(f"      ? {m}   (required, no rule)")
        if res["missing"]:
            return "needs_you", f"required question no rule answers: {res['missing'][0][:60]}"

        # Easy Apply is not CAPTCHA-free: this flow ships a g-recaptcha-response
        # field (invisible v3, so it only materialises at submit). Report it -
        # do not touch it.
        if page.locator('textarea[name="g-recaptcha-response"], .g-recaptcha').count():
            res["needs_you"].append("reCAPTCHA present on this flow")

        sub = footer_button(page, "Submit application")
        if sub:
            if not submit:
                return "filled", "on the review step - submit it yourself (or pass --submit)"
            if dry:
                return "dry", "would submit here"
            # Everything the run could not answer, across EVERY step - not just
            # this one. Gating on the review step alone would send an application
            # whose step 2 had an untickable consent box or a field we failed to
            # fill, which is exactly what --fill-only exists to prevent.
            unresolved = [n for s in steps for n in s["needs_you"]]
            unanswered = [m for s in steps for m in s["missing"]]
            if unanswered or unresolved:
                return "needs_you", ("not submitting - unresolved: "
                                     + "; ".join((unanswered + unresolved)[:3])[:150])
            if not any(s["resume"] for s in steps) and not resume_on_page(page):
                return "needs_you", "not submitting - no resume verified on this application"
            sub.click()
            pause("between_steps")
            return "submitted", "Submit application clicked"

        # "Review" is its own label on the 2026-08-12 build - the step before the
        # review page shows Back / Review, and looking only for the longer
        # "Review your application" stranded the run at step 4 with
        # "no Continue/Review button". Substring matching does not save us here:
        # a button named "Review" does not contain "Review your application".
        nxt = footer_button(page, "Continue to next step", "Review your application",
                            "Next", "Review")
        if not nxt:
            return "needs_you", f"no Continue/Review button at step {n}"
        sig = af.field_signature(page)
        if sig in seen_sigs:
            return "needs_you", f"step {n} would not advance - something on it is unanswered"
        seen_sigs.add(sig)
        if dry:
            return "dry", f"step {n} filled; stopping (dry)"
        nxt.click()
        pause("between_steps")
        errs = af.read_validation(page)
        if errs:
            return "needs_you", f"form rejected step {n}: {str(errs[0])[:70]}"
    return "needs_you", "more than 11 steps - stopped rather than looping"


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply to LinkedIn Easy Apply jobs.")
    ap.add_argument("--keywords", default="AI engineer")
    ap.add_argument("--onsite", action="store_true", help="drop the remote-only filter")
    ap.add_argument("--limit", type=int, default=1,
                    help="jobs to process (default 1: without --submit only the "
                         "last one stays on screen, so a big number wastes work)")
    ap.add_argument("--submit", action="store_true",
                    help="click Submit application. This really does send them.")
    ap.add_argument("--pace", type=float, default=PACE["between_jobs"],
                    help=f"seconds between postings (default {PACE['between_jobs']:.0f}). "
                         "Raise it if LinkedIn gets twitchy; lowering it is how "
                         "the account gets flagged.")
    ap.add_argument("--accept-account-email", action="store_true",
                    help="send with the address verified on the LinkedIn account "
                         "when profile.json's address is not offered. Default is "
                         "to stop, because the resume says something else.")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args()
    ACCEPT_ACCOUNT_EMAIL[0] = args.accept_account_email
    PACE["between_jobs"] = max(0.0, args.pace)

    prof = af.load_profile()
    rules = af.build_rules(prof)
    n_explicit = af.count_explicit(prof)

    pw, browser, ctx = af.connect(args.port)
    page = ctx.new_page()
    af.keep_minimized(page)
    counts: dict[str, int] = {}
    # A run opens exactly one tab and used to never close it, so every re-run
    # stacked another window on the same search - eight debug runs on 2026-08-12
    # left eight tabs, several of them half-filled copies of the SAME job, which
    # is both confusing and the kind of thing LinkedIn notices. Only leave the
    # tab behind when there is genuinely something for the user to act on.
    leave_open = False
    try:
        jobs = find_jobs(page, args.keywords, not args.onsite, args.limit)
        easy = [j for j in jobs if j["easy"]]
        print(f"\n  {len(jobs)} postings on the search, {len(easy)} marked Easy Apply\n")
        done = 0
        for job in easy:
            if done >= args.limit:
                break
            if done:
                # Between postings, not just between clicks. This is the gap that
                # matters - the user has been flagged for moving through jobs too
                # quickly, and a run is not in a hurry.
                print(f"      (waiting {PACE['between_jobs']:.0f}s before the next posting)")
                pause("between_jobs")
            print(f"  [{done + 1}/{args.limit}] {job['title'][:44]:<44} {job['company'][:22]}")
            try:
                status, note = apply_one(page, job, prof, rules, n_explicit,
                                         args.dry, args.submit)
            except Exception as exc:
                status, note = "error", f"{type(exc).__name__}: {exc}"
            counts[status] = counts.get(status, 0) + 1
            print(f"      -> {status}: {note}\n")
            if status in ("duplicate", "not_easy_apply"):
                continue
            done += 1
            if not args.dry and status in ("submitted", "needs_you", "filled"):
                af.log_application(canonical(job["id"]), "linkedin_easy",
                                   "submitted" if status == "submitted" else "needs_you",
                                   job["title"][:120], job["company"][:60],
                                   requested=canonical(job["id"]), note=note)
            if status == "blocked":
                leave_open = True
                print("  ! stopping: hand the checkpoint to the user.")
                break
            if status == "filled":
                leave_open = True
                print("  ! left on the review step. Submit it in Chrome, then re-run "
                      "for the next one (or pass --submit).")
                break
    finally:
        if not leave_open:
            try:
                page.close()
            except Exception:
                pass
        pw.stop()

    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "  nothing processed")


if __name__ == "__main__":
    main()
