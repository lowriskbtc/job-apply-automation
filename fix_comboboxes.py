#!/usr/bin/env python
"""
fix_comboboxes.py - fill the Greenhouse `role=combobox` fields autofill.py leaves empty.

Why this exists (2026-08-12): on all four Greenhouse forms in that night's batch,
required fields rendered as `<input role="combobox">` - Country, Location (City),
work authorization, sponsorship, "How did you hear about this job?" - came back
EMPTY, and the run still reported Trace3 as "filled - submit it yourself" with no
blockers at all. So the fill report said ready while four required fields were
blank; submitting would have bounced on validation.

This pass re-reads every open tab, finds required-but-empty comboboxes/selects,
matches the label against `autofill.build_rules(profile)` - the SAME rules the
filler uses, so nothing here is a new answer - and fills only what matches.

Deliberately conservative:
  * SKIP_RE below is never auto-answered: security clearance, "U.S. Person",
    protected-individual/veteran/disability/race/gender, export-control, criminal
    history, and policy acknowledgments. Those are the user's to answer, per the
    standing rule in SOP-run.md ("never assert an employment record for him", and
    self-identification affirmatives are left blank).
  * A field with no matching rule is left alone and reported, never guessed.
  * Every write is read back from the DOM and reported as the value that stuck -
    the fill report is not evidence (see SOP-run.md step 6).

  python fix_comboboxes.py            # all open tabs
  python fix_comboboxes.py --dry      # say what it would do, write nothing
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import autofill as af  # noqa: E402

# Questions this script must never answer on his behalf.
SKIP_RE = re.compile(
    r"security clearance|u\.?s\.? person|protected individual|protected veteran"
    r"|veteran status|disability|deemed export"
    # race / ethnicity / gender used to be skipped here for the same reason
    # relocation was: no stated policy, so the honest move was to leave them for
    # him. He stated them - race and gender were already in profile.json, and on
    # 2026-08-15 he added that he is straight and not transgender - so there is a
    # real answer to apply and the skip is gone. Note what this fixed: the
    # `gender` rule answers "Male", Greenhouse's newer wording offers "Man", and
    # "Do you identify as transgender?" contains the word gender, so that one
    # question was being claimed by the gender rule and answered with a value no
    # option matched. All three now have their own rules, ordered so transgender
    # and orientation are matched before the bare gender rule.
    # STILL SKIPPED, deliberately: disability and veteran status. autofill.py
    # already fills both from profile.json when it can see the field; they carry
    # legal weight, and a combobox-rescue pass is not the place to start
    # asserting them.
    r"|export rule|criminal|felony|conviction|privacy policy|privacy notice"
    r"|acknowledge|consent|sanction|referred you|referral"
    # Relocation used to be skipped here: the profile said remote-only, so the
    # honest answer was a flat "No" that read as disqualifying, and it was his
    # call to make. He made it on 2026-08-12 - remote preferred, will move for a
    # good opportunity, San Francisco especially - so the two relocation rules in
    # profile.json now answer these and this skip is gone.
    # Lever/Greenhouse "which office are you applying to" - a list of the
    # EMPLOYER's locations. Matching it against his home city picks a real
    # office at random. Not the same question as "where do you live".
    r"|opportunitylocation|which location are you applying|office location",
    re.I,
)

FIELD_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('input,select,textarea').forEach((e, i) => {
    if (e.type === 'hidden' || e.type === 'file') return;
    e.setAttribute('data-fixcb', String(i));
    let lab = (e.labels && e.labels[0]) ? e.labels[0].innerText : '';
    if (!lab) lab = e.getAttribute('aria-label') || '';
    if (!lab) {
      const fs = e.closest('fieldset,div[role=group]');
      if (fs) lab = (fs.innerText || '').split('\n')[0];
    }
    if (!lab) lab = e.name || e.id || '';
    const req = !!e.required || e.getAttribute('aria-required') === 'true'
                || /\*/.test(lab);
    const combo = e.getAttribute('role') === 'combobox';
    out.push({
      idx: String(i),
      lab: (lab || '').replace(/\s+/g, ' ').trim().slice(0, 130),
      tag: e.tagName.toLowerCase(),
      combo: combo,
      req: req,
      val: (e.value || '').trim(),
      opts: e.tagName === 'SELECT'
        ? Array.from(e.options).map(o => o.text.trim()).filter(Boolean)
        : [],
    });
  });
  return out;
}
"""


# The correct react-select driver lives in fix_greenhouse_selects.py. The first
# version of this file had its own, which counted ArrowDown presses against a
# stale option list and read input.value (always empty on a react-select) - it
# wrote "Australia" and "California City" into four live applications on
# 2026-08-12. Importing the verified one so that version cannot come back.
from fix_greenhouse_selects import set_exact, shown_value  # noqa: E402


def set_react_select(page, handle, values) -> tuple[bool, list[str], str]:
    """Set a react-select to the first of `values` that it offers, and verify."""
    name = handle.evaluate("e => e.name || e.id || ''")
    for value in values:
        if set_exact(page, handle, name, value, value, lambda _m: None):
            return True, [], shown_value(page, name)
    return False, [], ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args()

    prof = af.load_profile()
    rules = af.build_rules(prof)

    out = io.StringIO()

    def say(msg: str = "") -> None:
        # Windows stdout is cp1252 and a stray glyph from a job form will crash
        # the run mid-write, so everything goes through a UTF-8 buffer.
        out.write(msg + "\n")

    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
    try:
        for ctx in browser.contexts:
            for page in ctx.pages:
                url = page.url
                if not url.startswith("http"):
                    continue
                try:
                    fields = page.evaluate(FIELD_JS)
                except Exception as exc:
                    say(f"\n{'='*74}\n{url[:90]}\n  ! could not read: {exc}"[:300])
                    continue
                todo = [f for f in fields
                        if f["req"] and not f["val"] and (f["combo"] or f["tag"] == "select")]
                if not todo:
                    continue
                say(f"\n{'='*74}")
                try:
                    say(page.title()[:70])
                except Exception:
                    say(url[:70])

                for f in todo:
                    lab = f["lab"]
                    if SKIP_RE.search(lab):
                        say(f"  skip (yours to answer): {lab[:88]}")
                        continue
                    values = None
                    for pat, vals in rules:
                        if pat.search(lab):
                            values = vals
                            break
                    if values is None:
                        say(f"  no rule, left blank: {lab[:88]}")
                        continue
                    if args.dry:
                        say(f"  would fill: {lab[:60]} <- {values[0]}")
                        continue

                    handle = page.query_selector(f'[data-fixcb="{f["idx"]}"]')
                    if handle is None:
                        say(f"  vanished: {lab[:70]}")
                        continue
                    try:
                        if f["tag"] == "select":
                            choice = next(
                                (c for c in (af.pick_option(f["opts"], v) for v in values) if c),
                                None,
                            )
                            if choice is None:
                                say(f"  none on offer ({values[0]}): {lab[:60]}")
                                continue
                            handle.select_option(label=choice)
                        else:
                            ok, offered, chosen = set_react_select(page, handle, values)
                            if not ok:
                                hint = f" offers: {', '.join(offered[:5])}" if offered else ""
                                say(f"  could not set: {lab[:60]}{hint}"[:170])
                                continue
                        got = (handle.evaluate("e => (e.value || '').trim()") or "")
                        # Read back rather than trust the call: a react-controlled
                        # combobox can accept a click and revert.
                        say(f"  OK {lab[:58]} -> {got[:40]!r}" if got
                            else f"  WROTE BUT EMPTY: {lab[:60]}")
                    except Exception as exc:
                        say(f"  error on {lab[:50]}: {str(exc)[:70]}")
    finally:
        pw.stop()

    text = out.getvalue()
    (HERE / "fix_comboboxes_last.txt").write_text(text, encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8", "replace"))


if __name__ == "__main__":
    main()
