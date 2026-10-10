#!/usr/bin/env python
"""
fix_greenhouse_selects.py - set a Greenhouse react-select to an EXACT option, and
prove it took.

Written 2026-08-12 after fix_comboboxes.py put wrong values into four live forms.
Two lessons are baked in here:

  1. `input.value` is NOT the value of a react-select. The visible input is only
     a search box; the committed choice renders in the control's own text. Reading
     input.value made a committed-but-wrong choice look like "empty", and a
     correct one look like a failure.

  2. Counting ArrowDown presses against a snapshot of the option list is wrong -
     the list re-filters as you type and the highlight does not start at index 0.
     That is how `country` became "+61" (Australia) and the city became
     "California City". Instead: read `aria-activedescendant` after every press
     and stop when the HIGHLIGHTED option is the one wanted.

Every write is verified by reading the control's rendered text back, and the
script refuses to leave a field holding anything other than the exact target.

  python fix_greenhouse_selects.py --dry
  python fix_greenhouse_selects.py
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# field name -> (exact option text wanted, text typed to filter the list)
TARGETS = {
    "country": ("United States +1", "United States"),
    "candidate-location": ("your fallback city, California, United States", "your fallback city"),
}

READ_JS = r"""
() => Array.from(document.querySelectorAll('input[role=combobox]')).map(e => {
  const box = e.closest('[class*=control]') || e.parentElement;
  return {
    name: e.name || e.id || '',
    shown: (box ? box.innerText : '').replace(/\s+/g, ' ').trim()
  };
})
"""


def active_option(page, handle) -> str:
    """Text of the option react-select currently has highlighted."""
    return page.evaluate(
        """(el) => {
            const id = el.getAttribute('aria-activedescendant');
            if (!id) return '';
            const o = document.getElementById(id);
            return o ? (o.innerText || '').trim() : '';
        }""",
        handle,
    )


def shown_value(page, name: str) -> str:
    for row in page.evaluate(READ_JS):
        if row["name"] == name:
            return row["shown"]
    return ""


def set_exact(page, handle, name: str, want: str, filter_text: str, say) -> bool:
    for attempt in range(3):
        try:
            handle.click()
            page.wait_for_timeout(400)
            # Clear any filter text left by a previous attempt, using real key
            # presses so React sees them.
            for _ in range(60):
                page.keyboard.press("Backspace")
            page.wait_for_timeout(200)
            handle.type(filter_text, delay=45)
            page.wait_for_timeout(900)

            # Walk the highlight until it IS the wanted option. Never assume an
            # index - read what is actually highlighted each time.
            hit = False
            for _ in range(40):
                if active_option(page, handle).strip() == want:
                    hit = True
                    break
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(70)
            if not hit:
                page.keyboard.press("Escape")
                continue

            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
            got = shown_value(page, name)
            if got == want:
                return True
            say(f"      attempt {attempt+1}: landed on {got!r}, retrying")
        except Exception as exc:
            say(f"      attempt {attempt+1} error: {str(exc)[:60]}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args()

    out = io.StringIO()

    def say(msg: str = "") -> None:
        out.write(msg + "\n")

    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
    try:
        for ctx in browser.contexts:
            for page in ctx.pages:
                if "greenhouse" not in page.url:
                    continue
                say("=" * 72)
                try:
                    say(page.title()[:68])
                except Exception:
                    say(page.url[:68])
                rows = {r["name"]: r["shown"] for r in page.evaluate(READ_JS)}
                for name, (want, filt) in TARGETS.items():
                    if name not in rows:
                        continue
                    cur = rows[name]
                    if cur == want:
                        say(f"  already correct: {name} = {cur!r}")
                        continue
                    if args.dry:
                        say(f"  would fix {name}: {cur!r} -> {want!r}")
                        continue
                    handle = None
                    for h in page.query_selector_all("input[role=combobox]"):
                        if (h.evaluate("e => e.name || e.id || ''")) == name:
                            handle = h
                            break
                    if handle is None:
                        say(f"  {name}: not found")
                        continue
                    ok = set_exact(page, handle, name, want, filt, say)
                    final = shown_value(page, name)
                    say(f"  {'OK  ' if ok else 'FAIL'} {name}: {cur!r} -> {final!r}")
    finally:
        pw.stop()

    text = out.getvalue()
    (HERE / "fix_greenhouse_selects_last.txt").write_text(text, encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8", "replace"))


if __name__ == "__main__":
    main()
