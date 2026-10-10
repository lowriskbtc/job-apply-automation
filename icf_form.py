"""icf_form.py: list the visible fields of the ICF apply step in the open tab."""
from playwright.sync_api import sync_playwright
import sys
sys.stdout.reconfigure(encoding="utf-8")
with sync_playwright() as p:
    b=p.chromium.connect_over_cdp("http://127.0.0.1:9222"); ctx=b.contexts[0]
    pg=[x for x in ctx.pages if "careers.icf.com/us/en/apply" in x.url][0]
    for e in pg.query_selector_all("input,select,textarea"):
        if not e.is_visible() or e.get_attribute("type")=="hidden": continue
        lab=e.evaluate("e=>{const l=e.labels&&e.labels[0];return l?l.innerText.slice(0,70):(e.getAttribute('aria-label')||e.placeholder||e.name||'')}")
        print(e.evaluate("e=>e.tagName"),e.get_attribute("type"),repr(lab),repr((e.input_value() or "")[:25]))
    print([b2.inner_text().strip()[:25] for b2 in pg.query_selector_all("button") if b2.is_visible() and b2.inner_text().strip()][:14])
    p.stop()
