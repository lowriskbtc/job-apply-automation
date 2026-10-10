#!/usr/bin/env python
"""
audit_tabs.py - read every open application tab and report which REQUIRED fields
are still unset, from the DOM.

Why this exists: neither of the existing checks can answer that question.

  * the `autofill.py` run log lists what the filler *tried*, and it does not know
    about Greenhouse `role=combobox` fields at all - it neither fills them nor
    flags them, which is how a half-empty form reports "filled, submit it
    yourself" (TODO-2026-08-12, "worth fixing before the next run").
  * `verify_tabs.py` reads name / email / attached filename / CAPTCHA only. A
    form can pass it with every dropdown on the page still empty.

So a handoff built from either source overstates how done a form is. This reads
the page.

Reading rules that are not optional here (learned the hard way on 08-12):

  * `input.value` is meaningless on a react-select - it is the search box and is
    always ''. The committed choice renders as the control's own text.
  * 'Select...' is Greenhouse's placeholder, not a value. So is ''.
  * required-ness comes from the label text ending in '*', or aria-required.

    python audit_tabs.py
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

PLACEHOLDERS = {"", "select...", "select", "-", "--", "choose", "please select"}

SCAN_JS = r"""
() => {
  const out = [];
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();

  const labelFor = (el) => {
    // walk up looking for the field wrapper, then take its first label-ish text
    let n = el;
    for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
      const lab = n.querySelector && n.querySelector('label, legend, .label, [class*=label]');
      if (lab && clean(lab.innerText)) return clean(lab.innerText);
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.innerText);
    }
    return clean(el.name || el.id || '');
  };

  // 1. react-select / aria comboboxes - value is the CONTROL's rendered text
  document.querySelectorAll('input[role=combobox], [role=combobox]').forEach(e => {
    if (e.tagName === 'INPUT' && e.type === 'hidden') return;
    // intl-tel-input's country search box. It is a combobox with no committed
    // text of its own; the phone number lives in the tel input next to it, so
    // reading this one reports a filled phone as an empty required field.
    if ((e.id || '').includes('iti-') || (e.name || '').includes('iti-')) return;
    const box = e.closest('[class*=control]') || e.parentElement;
    const label = labelFor(e);
    const raw = clean(box ? box.innerText : '');
    // The control's text can include the label when they share a wrapper. Strip
    // it for display only - NEVER for the is-it-set test. When labelFor() fails
    // it falls back to returning the wrapper text, which IS the value; stripping
    // that leaves '' and turns every correctly-filled field into a false alarm.
    let shown = raw;
    if (label && raw.startsWith(label) && raw !== label) shown = clean(raw.slice(label.length));
    out.push({kind: 'combobox', name: e.name || e.id || '', label, shown, raw,
              required: /\*\s*$/.test(label) || e.getAttribute('aria-required') === 'true'});
  });

  // 2. native selects
  document.querySelectorAll('select').forEach(e => {
    const label = labelFor(e);
    const opt = e.selectedOptions[0];
    out.push({kind: 'select', name: e.name || e.id || '', label,
              shown: clean(opt ? opt.text : ''),
              required: /\*\s*$/.test(label) || e.required || e.getAttribute('aria-required') === 'true'});
  });

  // 3. text / textarea that are marked required and still empty.
  //
  // A react-select's search box IS an input[type=text] carrying role=combobox,
  // and its .value is permanently '' - counting it here reported every dropdown
  // twice and called the filled ones empty. Skip anything already read as a
  // combobox, and anything hidden.
  document.querySelectorAll('input[type=text], input[type=email], input[type=tel], input[type=url], textarea').forEach(e => {
    if (e.getAttribute('role') === 'combobox') return;
    if (e.closest('[class*=control]')) return;          // inside a select widget
    if (!e.offsetParent && e.type !== 'hidden') return; // not rendered
    const label = labelFor(e);
    const req = /\*\s*$/.test(label) || e.required || e.getAttribute('aria-required') === 'true';
    if (!req) return;
    out.push({kind: 'text', name: e.name || e.id || '', label, shown: clean(e.value), required: true});
  });

  // 4. required radio/checkbox groups with nothing chosen
  const groups = {};
  document.querySelectorAll('input[type=radio], input[type=checkbox]').forEach(e => {
    const key = e.name || (e.closest('fieldset') ? 'fs:' + labelFor(e) : '');
    if (!key) return;
    (groups[key] = groups[key] || []).push(e);
  });
  Object.entries(groups).forEach(([key, els]) => {
    const label = labelFor(els[0]);
    const req = /\*\s*$/.test(label) || els.some(e => e.required || e.getAttribute('aria-required') === 'true');
    if (!req) return;
    out.push({kind: els[0].type, name: key, label,
              shown: els.some(e => e.checked) ? 'CHECKED' : '', required: true});
  });

  return out;
}
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--all", action="store_true", help="print every field, not just unset required ones")
    args = ap.parse_args()

    out = io.StringIO()

    def say(m: str = "") -> None:
        out.write(m + "\n")

    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
    totals = []
    try:
        for ctx in browser.contexts:
            for page in ctx.pages:
                if page.url.startswith("about:"):
                    continue
                try:
                    title = page.title()[:66]
                    rows = page.evaluate(SCAN_JS)
                except Exception as exc:
                    say(f"!! could not read a tab: {str(exc)[:70]}")
                    continue

                # Test the RAW control text, not the label-stripped display text
                # - see the note in SCAN_JS on why stripping cannot be trusted
                # for this decision.
                missing = [r for r in rows
                           if r["required"]
                           and r.get("raw", r["shown"]).strip().lower() in PLACEHOLDERS]
                totals.append((title, len(missing)))
                say("=" * 74)
                say(title)
                say(f"  {len(rows)} field(s) read, {len(missing)} REQUIRED still unset")
                for r in missing:
                    lab = r["label"] or r["name"]
                    say(f"    [{r['kind']:<8}] {lab[:72]}")
                if args.all:
                    for r in rows:
                        if r not in missing:
                            say(f"    ok  {(r['label'] or r['name'])[:48]:<48} = {r['shown'][:34]!r}")
    finally:
        pw.stop()

    say()
    say("=" * 74)
    say("SUMMARY - required fields still unset, per form")
    for t, n in sorted(totals, key=lambda x: -x[1]):
        say(f"  {n:>3}  {t[:66]}")

    text = out.getvalue()
    (HERE / "audit_tabs_last.txt").write_text(text, encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8", "replace"))


if __name__ == "__main__":
    main()
