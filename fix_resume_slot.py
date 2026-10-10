"""Force the correct resume into a Greenhouse-style upload widget.

Greenhouse renders no <input type=file> once a file is already showing, so the
filler cannot overwrite a prefilled attachment. Click 'Remove file', wait for
the input to come back, upload v2, then read the displayed filename back.
"""
import json, os, sys, time
from pathlib import Path

from playwright.sync_api import sync_playwright

# Read the resume out of profile.json rather than keeping a second copy of the
# path here. A hardcoded filename in this script would silently REPLACE the file
# autofill.py just attached - it runs after the fill, so it wins - and the
# read-back would report a plausible-looking name while sending the wrong PDF.
PROFILE = Path(__file__).with_name("profile.json")
RESUME = os.path.expandvars(
    json.loads(PROFILE.read_text(encoding="utf-8"))["files"]["resume"])
if not Path(RESUME).exists():
    sys.exit(f"profile.json points at a resume that does not exist: {RESUME}")
WANT = Path(RESUME).name
print(f"resume from profile.json: {WANT}\n")

match = sys.argv[1] if len(sys.argv) > 1 else ""


def shown(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('.file-upload__filename p, .file-upload__filename')]"
        ".map(e => (e.innerText||'').trim()).filter(Boolean)")


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    pages = [pg for c in b.contexts for pg in c.pages
             if not pg.url.startswith(('about:', 'chrome:')) and match in pg.url]
    for page in pages:
        print("=" * 70)
        print(page.title()[:70])
        print("  before:", shown(page))

        groups = page.query_selector_all('.file-upload')
        for g in groups:
            label = (g.inner_text() or '').split('\n')[0][:40]
            if 'resume' not in label.lower() and 'cv' not in label.lower():
                print(f"  skip group {label!r} (not the resume slot)")
                continue
            name_el = g.query_selector('.file-upload__filename p')
            current = (name_el.inner_text().strip() if name_el else '')
            if current == WANT:
                print(f"  {label!r}: already {WANT}")
                continue
            if current:
                rm = g.query_selector('button[aria-label="Remove file"]')
                if not rm:
                    print(f"  !! {label!r}: holds {current!r} and no remove button")
                    continue
                rm.click()
                page.wait_for_timeout(900)
                print(f"  {label!r}: removed {current!r}")
            inp = g.query_selector('input[type=file]')
            if not inp:
                inp = page.query_selector('input[type=file]')
            if not inp:
                print(f"  !! {label!r}: no file input after removal")
                continue
            inp.set_input_files(RESUME)
            page.wait_for_timeout(1500)
            name_el = g.query_selector('.file-upload__filename p')
            print(f"  {label!r}: now -> {name_el.inner_text().strip() if name_el else 'EMPTY'}")

        time.sleep(0.5)
        print("  after :", shown(page))
