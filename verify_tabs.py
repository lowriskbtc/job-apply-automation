"""Read back what is ACTUALLY in each open tab: name fields + attached filename.

The fill report is not evidence (see HANDOFF-2026-08-02) - something else in the
browser has historically written after us. This reads the live DOM.
"""
from playwright.sync_api import sync_playwright

JS = r"""
() => {
  const out = {url: location.href, title: document.title};
  const val = sel => {
    const e = document.querySelector(sel);
    return e ? e.value : null;
  };
  out.first = val('input[name="first_name"], input[id*="first_name" i], input[name*="first" i]');
  out.last  = val('input[name="last_name"], input[id*="last_name" i], input[name*="last" i]');
  // Greenhouse renders the email box as id="email" with type="text" and NO name,
  // so the first two selectors miss it and the tab reads 'email None' on a form
  // that is actually filled. id is the one that matches there.
  out.email = val('input[type="email"], input[name*="email" i], input[id*="email" i]');
  // filenames: both the <input type=file>.files and any text the ATS renders
  const files = [];
  document.querySelectorAll('input[type=file]').forEach(i => {
    files.push({name: i.name || i.id || '(file)',
                picked: i.files && i.files.length ? [...i.files].map(f => f.name) : []});
  });
  out.fileInputs = files;
  const text = document.body ? document.body.innerText : '';
  out.pdfMentions = [...new Set((text.match(/[\w .()-]+\.pdf/gi) || []))];
  out.captcha = /recaptcha|hcaptcha|cf-turnstile|grecaptcha/i.test(document.documentElement.innerHTML);
  return out;
}
"""

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    for ctx in b.contexts:
        for page in ctx.pages:
            if page.url.startswith(('about:', 'chrome:')):
                continue
            try:
                data = page.evaluate(JS)
            except Exception as exc:
                print(f"!! {page.url[:80]} -> {exc}")
                continue
            frames = []
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                try:
                    d = fr.evaluate(JS)
                    if d['fileInputs'] or d['first']:
                        frames.append(d)
                except Exception:
                    pass
            print('=' * 78)
            print(data['title'][:70])
            print('  url    :', data['url'][:100])
            for d in [data] + frames:
                tag = '  ' if d is data else '  [iframe] '
                if d['first'] or d['last'] or d['email']:
                    print(f"{tag}name   : {d['first']!r} {d['last']!r}  email {d['email']!r}")
                for fi in d['fileInputs']:
                    print(f"{tag}file   : {fi['name'][:40]} -> {fi['picked'] or 'EMPTY'}")
                if d['pdfMentions']:
                    print(f"{tag}pdf txt: {d['pdfMentions']}")
            print('  captcha:', data['captcha'])
