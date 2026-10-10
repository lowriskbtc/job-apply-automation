"""chat_step.py <text>: type one answer into the open careers-site chatbot (Scout), wait, print the new bot messages + buttons. Never closes the tab."""
from playwright.sync_api import sync_playwright
import sys
sys.stdout.reconfigure(encoding="utf-8")
ans=" ".join(sys.argv[1:])
with sync_playwright() as p:
    b=p.chromium.connect_over_cdp("http://127.0.0.1:9222"); ctx=b.contexts[0]
    pg=[x for x in ctx.pages if "careers.icf.com" in x.url][0]
    if ans.startswith("CLICK:"):
        pg.locator("#phenomChatbotWrapper").get_by_text(ans[6:],exact=True).last.click()
    elif ans:
        box=pg.locator("input:visible, textarea:visible").last
        box.click(); box.fill(ans); pg.keyboard.press("Enter")
    pg.wait_for_timeout(6000)
    txt=pg.evaluate("()=>{const e=[...document.querySelectorAll('*')].filter(x=>/Type your|Hi There/.test(x.innerText||'')&&x.children.length<40);const w=e.sort((a,b)=>(a.innerText||'').length-(b.innerText||'').length).pop();return w?w.innerText:document.body.innerText.slice(-600)}")
    print(txt[-700:].replace(chr(10)," | "))
    pg.screenshot(path="icf3.png")
    p.stop()
