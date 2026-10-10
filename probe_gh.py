import sys
from playwright.sync_api import sync_playwright
sys.stdout.reconfigure(encoding="utf-8")
urls=sys.argv[1:]
with sync_playwright() as p:
    b=p.chromium.connect_over_cdp("http://127.0.0.1:9222"); ctx=b.contexts[0]
    for u in urls:
        pg=ctx.new_page(); pg.goto(u,timeout=60000); pg.wait_for_timeout(4000)
        print("==",u)
        n=pg.locator("input[role=combobox]").count()
        for i in range(n):
            inp=pg.locator("input[role=combobox]").nth(i)
            lab=inp.evaluate("e=>{const l=e.labels&&e.labels[0];return l?l.innerText:(e.getAttribute('aria-label')||e.id)}").replace("\n"," ")[:110]
            
            if not inp.is_visible(): continue
            inp.scroll_into_view_if_needed(); inp.click(); pg.wait_for_timeout(500)
            opts=[o.inner_text().strip()[:45] for o in pg.query_selector_all("[role=option]") if o.is_visible()][:14]
            pg.keyboard.press("Escape")
            print(i,lab,"|",opts)
        pg.close()
    p.stop()
