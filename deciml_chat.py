"""deciml_chat.py <answer text>: type one answer in the open Deciml apply dialog, wait, print the assistant's next message + chips."""
from playwright.sync_api import sync_playwright
import sys
sys.stdout.reconfigure(encoding="utf-8")
ans=" ".join(sys.argv[1:])
with sync_playwright() as p:
    b=p.chromium.connect_over_cdp("http://127.0.0.1:9222"); ctx=b.contexts[0]
    pg=[x for x in ctx.pages if "deciml.io/jobs" in x.url][0]
    if ans.startswith("START:"):
        title=ans[6:]
        h=pg.get_by_text(title,exact=True).first; h.scroll_into_view_if_needed(); pg.wait_for_timeout(500)
        card=h.locator("xpath=ancestor::*[.//button[normalize-space()='Quick apply']][1]")
        card.get_by_role("button",name="Quick apply").first.click()
    elif ans.startswith("CHIP:"):
        pg.get_by_role("button",name=ans[5:]).first.click()
    elif ans:
        box=pg.locator("[role=dialog] input[type=text], [role=dialog] textarea, input[placeholder*='answer']").first
        box.click(); box.fill(ans); pg.keyboard.press("Enter")
    pg.wait_for_timeout(7000)
    t=pg.evaluate("()=>{const d=document.querySelector('[role=dialog]');return d?d.innerText:'(no dialog)'}")
    print(t.replace("\n"," | ")[-1500:])
    print("CHIPS:",[b2.inner_text().strip()[:70] for b2 in pg.query_selector_all("[role=dialog] button") if b2.is_visible() and b2.inner_text().strip()])
    p.stop()
