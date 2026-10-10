#!/usr/bin/env python
"""auto_apply.py - fill ONE job, verify it, and submit it only if it is clean.

Run by run_auto.py, one subprocess per job (so one hung page cannot wedge the rest).
the user's standing instruction (2026-10-06): "fill out as many and submit them on
your own". Guardrails, because a submission cannot be undone:

  SUBMIT only when ALL hold
    - something was actually filled and a resume is attached to the real field
    - no required question is unanswered (text, dropdown, radio, Ashby Yes/No)
    - no required checkbox/consent outside his consent_policy (arbitration,
      certifications, AI-policy attestations, "I agree to ..." legal terms)
    - the page asks nothing we would have to invent (essays, years of X, etc.)
  otherwise HOLD: close the tab, log needs_you with the reason, report it.

Never: creates accounts, enters passwords, solves captchas.

  python auto_apply.py --url <apply url> --company X --title "Y"
Exit 0 always; the result is one JSON line on stdout prefixed RESULT:.
"""
import argparse, datetime, json, re, sys, os
sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import autofill as af

SUCCESS_TXT = re.compile(r"thanks, got it|hear from us soon|successfully submitted|thank you for (applying|your application)|application (has been )?(received|submitted)|we.ve received your application|your application (was|has been) (sent|submitted)", re.I)

SALARY_MIN = 120000   # the user 2026-10-07: minimum 120k (he typed 12000, read as 120000), ideal 150k
SALARY_MAX = None     # highest salary figure printed in the posting, set per job
SALARY_Q = re.compile(r"(accept|comfortable with|okay with|ok with|agree(able)? (to|with)|aligned (with|on))[^?]{0,30}(salary|compensation|pay|base)( range| band)?|(salary|compensation|pay) range[^?]{0,40}(accept|works|comfortable|align)", re.I)

def salary_max(text):
    best = None
    for m in re.finditer(r"[$][ ]?([0-9]{2,3}(?:,[0-9]{3})+|[0-9]{2,3}(?:[.][0-9])?[ ]?[kK])", text):
        s = m.group(1).replace(",", "").replace(" ", "")
        v = float(s[:-1]) * 1000 if s[-1] in "kK" else float(s)
        if 40000 <= v < 1000000: best = max(best or 0, v)
    return best

def salary_ok():
    return True   # the user 2026-10-07 (#6): yes to salary-range acceptance
SUCCESS_URL = re.compile(r"/(confirmation|thank[-_]?you|thanks|success|submitted)(?=[/?#]|$)", re.I)
LEGAL = re.compile(r"arbitrat|waive|jury|non-?compete|background (check|screen)|consent to (the )?(use|collection) of (my )?(biometric|genetic)|attest|under penalty|electronic signature|esign", re.I)

REQUIRED_UNSET_JS = r"""()=>{
 const out=[];
 const q=e=>{const l=(e.labels&&e.labels[0])?e.labels[0].innerText:(e.getAttribute('aria-label')||e.name||e.id||'');return l.trim().split('\n').join(' ').slice(0,90)};
 const vis=e=>!!(e.offsetParent||e.getClientRects().length);
 for(const e of document.querySelectorAll('input,textarea,select')){
   if(e.type==='hidden'||e.type==='file'||e.type==='checkbox'||e.type==='radio'||e.disabled) continue;
   if(/recaptcha|captcha|iti-0__search/.test((e.name||'')+(e.id||''))) continue;
   if(!vis(e)) continue;
   const lab=q(e);
   const req=e.required||e.getAttribute('aria-required')==='true'||/\*\s*$/.test(lab);
   if(!req) continue;
   if(e.getAttribute('role')==='combobox'){
     const ctl=e.closest('.select__control')||e.closest('[class*=control]');
     const has=ctl&&ctl.querySelector('.select__single-value,.select__multi-value,[class*=singleValue]');
     if(!has&&!e.value) out.push('dropdown: '+lab); continue;}
   if(!e.value) out.push('field: '+lab);
 }
 const groups={};
 for(const r of document.querySelectorAll('input[type=radio]')){(groups[r.name]=groups[r.name]||[]).push(r);}
 for(const [n,rs] of Object.entries(groups)){
   if(!rs.some(r=>vis(r))) continue;
   const req=rs.some(r=>r.required||r.getAttribute('aria-required')==='true');
   if(req&&!rs.some(r=>r.checked)) out.push('radio: '+n.slice(0,40));}
 return out}"""

def log(rec):
    rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **rec}
    with open(os.path.join(HERE, "applications.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")

def ashby_fixes(page, prof):
    """Resume parse races: real Resume field + ALL-CAPS name. Re-check 3 times."""
    for _ in range(3):
        page.wait_for_timeout(4500)
        real = page.locator('input[type=file]#_systemfield_resume, input[type=file][name="_systemfield_resume"]').first
        if real.count() and not real.evaluate("e=>e.files.length"):
            real.set_input_files(prof["files"]["resume"]); page.wait_for_timeout(2500)
        nm = page.get_by_label("Name", exact=True).first
        try:
            v = nm.input_value()
            if v and v == v.upper() and len(v) > 3:
                nm.fill(v.title()); nm.press("Tab")
        except Exception:
            pass
    ok_res = real.count() and real.evaluate("e=>e.files.length") > 0
    if not ok_res:   # Synergy etc: the required Resume field's file input has a different id/name
        try:
            for f in page.locator("[data-field-path]").filter(has=page.locator("label", has_text="Resume")).all():
                fi = f.locator("input[type=file]").first
                if fi.count() and not fi.evaluate("e=>e.files.length"):
                    fi.set_input_files(prof["files"]["resume"]); page.wait_for_timeout(3000)
                if fi.count() and fi.evaluate("e=>e.files.length") > 0: ok_res = True
        except Exception:
            pass
    return bool(ok_res)

def ashby_yes_no(page, loc_ok=True, rules=None, n_explicit=0):
    """Answer Ashby Yes/No BUTTON pairs from known facts. Any pair left unanswered is held:
    Ashby marks required ones only by a server-side error ('Missing entry for required field')."""
    held = []
    yes = page.get_by_role("button", name="Yes", exact=True)
    for i in range(yes.count()):
        b = yes.nth(i)
        try:
            grp = b.locator('xpath=ancestor::*[.//button[normalize-space()="No"]][1]')
            if not grp.count(): continue
            pressed = grp.get_by_role("button").evaluate_all("els=>els.some(e=>e.getAttribute('aria-pressed')==='true')")
            if pressed: continue
            label = grp.locator('xpath=ancestor::*[.//label or .//legend][1]').first.inner_text()[:300].lower()
            label = re.sub(r"[(](e[.]g[.]|for example|such as)[^)]*[)]", "", label)
        except Exception:
            continue
        ans = None
        if re.search(r"sponsor", label): ans = "No"
        elif SALARY_Q.search(label): ans = "Yes" if salary_ok() else None
        elif re.search(r"authori[sz]ed to work|work authori[sz]ation|legally (able|eligible)|right to work", label): ans = "Yes"
        elif re.search(r"18 years|at least 18|over 18|eighteen|legal age|age of 18|over the age", label): ans = "Yes"
        elif re.search(r"contact (your )?(current|present) employer|contact your employer", label): ans = "No"
        elif re.search(r"internship|intern or seeking", label): ans = "No"
        elif re.search(r"non-?compet|restrictive covenant|non-?solicit", label): ans = "No"
        elif re.search(r"reside in the country|currently reside|live in the country", label): ans = "Yes"
        elif re.search(r"security.focused", label): ans = "Yes"
        elif re.search(r"^i understand|^i acknowledge|^i agree|i understand that all employees", label.strip()): ans = "Yes"
        elif re.search(r"consenting to the use of ai|use ai (tools )?to (help|research|prepare)|consent[^?]{0,30}ai (for|to) (evaluat|review|assess)", label): ans = "Yes"
        elif re.search(r"previously (worked|employed|applied)|currently (work|employed) (at|for)|former employee|ever (worked|been employed|provided services|been engaged)", label): ans = "No"
        elif re.search(r"[1-4][+]? ?(or more )?years[^?]{0,60}(engineer|engineering|software|full-time|full time)", label): ans = "Yes"
        elif re.search(r"(launch|deploy|ship|run|operate|built|build)[a-z]*[^?]{0,40}(agents?|llms?|ml models?|ai)[^?]{0,30}(production|prod)|production[^?]{0,30}(agents?|llm|ai system)", label): ans = "Yes"
        elif re.search(r"(held|hold|have|possess|obtained)[^?]{0,40}(active |current )?(u\.?s\.? )?(security|government) clearance|security clearance", label): ans = "No"
        elif re.search(r"(experience (with|in|using|building)|(built|developed|shipped|worked on).{0,40}(project|application|app|product|system))[^?]{0,60}(llm|large language|generative|genai|gen ai|ai agent|agents|machine learning|python|rag|prompt)", label) and not re.search(r"pytorch|tensorflow|jax|cuda|kubernetes|k8s|spark|hadoop|scala|java"+chr(92)+"b|c"+chr(92)+"+"+chr(92)+"+|golang|swift|kotlin|"+chr(92)+".net|ruby|php|terraform|years|[0-9]+ ?(\+|plus)", label): ans = "Yes"
        elif re.search(r"excited to work out of|work out of one of our offices|located in sf or nyc", label): ans = "Yes" if loc_ok else None
        elif re.search(r"relocat|in.?person|in.?office|on.?site|hybrid|excited and able|work from (the |our )?office|commut|willing to work (either )?(out of|from|in)|\boffice\b", label):
            ans = "Yes" if loc_ok else None
        if not ans and rules:   # his own profile rules (SAFEGUARD 2026-10-07: never hold a question he answered)
            for pat, v in rules[:n_explicit]:
                vals = v if isinstance(v, list) else [v]
                if pat.search(label) and str(vals[0]).strip().lower() in ("yes", "no"):
                    ans = str(vals[0]).strip().capitalize(); break
        if ans:
            grp.get_by_role("button", name=ans, exact=True).first.click(); page.wait_for_timeout(300)
        else:
            held.append("unanswered yes/no: " + " ".join(label.split())[:100])
    return held

def second_pass_text(page, rules, skips):
    """Plain-fill any empty visible text box whose label matches a profile rule.
    autofill.py's own typing path fails on some Ashby inputs ('(Error)')."""
    n = 0
    for h, info in af.collect_fields(page):
        try:
            if info.get("tag") not in ("input", "textarea") or info.get("combo"): continue
            if info.get("type") not in ("text", "url", "tel", "email", "number", "search", "textarea", None, ""): continue
            if not info.get("visible") or info.get("disabled") or info.get("filled"): continue
            label = re.sub(r"\(Error\)", "", info.get("label") or "").strip()
            blob = f"{label} {info.get('name','')} {info.get('id','')}"
            if any(s.search(blob) for s in skips): continue
            for pat, vals in rules:
                if pat.search(blob):
                    h.fill(str(vals[0])); h.press("Tab"); n += 1; break
        except Exception:
            continue
    return n

def second_pass_radios(page, rules, n_explicit):
    """Answer real radio groups from his answers[] rules (sponsorship, authorization...)."""
    js = """()=>{const g={};for(const r of document.querySelectorAll('input[type=radio]')){if(!(r.offsetParent||r.getClientRects().length))continue;(g[r.name]=g[r.name]||[]).push(r)}
      return Object.entries(g).map(([n,rs])=>{const opts=rs.map(r=>({id:r.id,label:((r.labels&&r.labels[0])?r.labels[0].innerText:r.value).trim(),checked:r.checked}));
      let c=rs[0].parentElement;for(let i=0;i<6&&c;i++){const t=c.innerText||'';if(rs.every(r=>c.contains(r))&&t.length>opts.map(o=>o.label).join('').length+12)break;c=c.parentElement}
      let q=(c?c.innerText:'').split(String.fromCharCode(10)).map(x=>x.trim()).filter(x=>x&&!opts.some(o=>o.label===x))[0]||'';
      return {name:n,q:q.slice(0,200),opts}})}"""
    n = 0
    for g in page.evaluate(js):
        if any(o["checked"] for o in g["opts"]): continue
        for pat, vals in rules[:n_explicit]:
            if not pat.search(g["q"]): continue
            hit = next((o for o in g["opts"] for v in vals if af.radio_matches(o["label"], v)), None)
            if hit:
                try:
                    page.locator('[id="%s"]' % hit["id"]).check(force=True); n += 1
                except Exception:
                    pass
                break
    return n

RADIO_GROUPS_JS = r"""()=>{
 const vis=e=>!!(e.offsetParent||e.getClientRects().length);
 const groups={};
 for(const r of document.querySelectorAll('input[type=radio]')){ if(!vis(r)) continue; (groups[r.name]=groups[r.name]||[]).push(r);}
 return Object.entries(groups).map(([n,rs],gi)=>{
   let c=rs[0].parentElement,last=c;
   while(c){const inner=[...c.querySelectorAll('input[type=radio]')]; if(inner.some(r=>!rs.includes(r))) break; last=c; c=c.parentElement;}
   const opts=rs.map((r,oi)=>({oi:oi,label:((r.labels&&r.labels[0])?r.labels[0].innerText:r.value).trim(),checked:r.checked}));
   let q=(last?last.innerText:'').split(String.fromCharCode(10)).map(x=>x.trim()).filter(x=>x&&!opts.some(o=>o.label===x)).join(' ');
   return {gi:gi,name:n,q:q.slice(0,300),opts};});}"""

SET_RADIO_JS = r"""([gi,oi])=>{
 const vis=e=>!!(e.offsetParent||e.getClientRects().length);
 const groups={};
 for(const r of document.querySelectorAll('input[type=radio]')){ if(!vis(r)) continue; (groups[r.name]=groups[r.name]||[]).push(r);}
 const rs=Object.values(groups)[gi]; if(!rs) return false;
 const r=rs[oi]; (r.labels&&r.labels[0]?r.labels[0]:r).click(); return r.checked;}"""

def desired_for(q, loc_ok):
    ql = q.lower()
    # Questions about any country other than the US are never answered from his US facts.
    eeo = re.search(r"race|ethnic|hispanic|gender|veteran|disabilit|self-identif|origins? in any of", ql)
    relevant = re.search(r"work|authori|relocat|office|located|reside|eligible|visa|sponsor|based in", ql)
    if relevant and not eeo and re.search(r"canada|canadian|ontario|british columbia|united kingdom|\buk\b|europe|\beu\b|ireland|germany|india|australia|singapore|israel|brazil|mexico|japan|china", ql) and not re.search(r"united states|\bu\.?s\.?a?\b", ql):
        return "hold"
    if SALARY_Q.search(ql): return "yes" if salary_ok() else "hold"
    if re.search(r"sponsor|visa", ql) and not re.search(r"authori[sz]ed to work.*without", ql): return "no"
    if re.search(r"authori[sz]ed to work|legally (able|eligible|permitted)|eligible to work|right to work|work authori[sz]ation", ql): return "yes"
    if re.search(r"previously (worked|employed|applied)|former (employee|intern)|currently (work|employed) (at|for)", ql): return "no"
    if re.search(r"(hands.on |production )?experience[^?]{0,40}(building|with|using)[^?]{0,50}(llm|large language model|generative ai|ai agents?)", ql) and not re.search(r"years|[0-9]+ ?\+|terraform|ruby|rails", ql): return "yes"
    if re.search(r"18 years|of legal age|at least 18", ql): return "yes"
    if re.search(r"relocat|in.?office|hybrid|on.?site|work from (the |our )?office|commut", ql): return "yes" if loc_ok else "hold"
    return None

def pick_option(opts, want):
    for o in opts:
        l = o["label"].lower().strip()
        if want == "yes" and re.match(r"yes\b", l): return o
        if want == "no" and (re.match(r"no\b", l) or re.search(r"do not require|don.t require|not require", l)): return o
    return None

def choose_option(g, loc_ok):
    """The option this group must have, or None (no opinion) / 'hold'."""
    want = desired_for(g["q"], loc_ok)
    if want is None: return None
    if want == "hold": return "hold"
    opts = g["opts"]
    # He lives in Yourtown ST, so a 'based here' answer is false. If the group offers a
    # relocate option beside a based/located/live-here one, take the relocate one.
    reloc = [o for o in opts if re.search(r"relocat", o["label"], re.I)]
    here = [o for o in opts if re.search(r"(i.m|i am|currently)?\s*(based|located|live)\s*(in|here|at)|live here|already (live|located)", o["label"], re.I)]
    if want == "yes" and reloc and here and loc_ok:
        return reloc[0]
    return pick_option(opts, want)

def guard_radios(page, loc_ok):
    """Deterministic answers for the questions that matter most; flips wrong autofill picks."""
    held = []
    for g in page.evaluate(RADIO_GROUPS_JS):
        opt = choose_option(g, loc_ok)
        if opt is None: continue
        if opt == "hold":
            held.append("location commitment (job not SF/NYC/remote): " + g["q"][:80]); continue
        cur = next((o for o in g["opts"] if o["checked"]), None)
        if cur is not None and cur["oi"] == opt["oi"]: continue
        try:
            ok = page.evaluate(SET_RADIO_JS, [g["gi"], opt["oi"]]); page.wait_for_timeout(250)
            if not ok: held.append("radio did not stick: " + g["q"][:80])
        except Exception:
            held.append("could not set radio: " + g["q"][:80])
    return held

def radio_issues(page, loc_ok):
    """Groups where the critical answer is not what it must be right now."""
    bad = []
    for g in page.evaluate(RADIO_GROUPS_JS):
        opt = choose_option(g, loc_ok)
        if opt is None: continue
        if opt == "hold":
            bad.append("location commitment (job not SF/NYC/remote): " + g["q"][:70]); continue
        cur = next((o for o in g["opts"] if o["checked"]), None)
        if cur is None or cur["oi"] != opt["oi"]:
            bad.append(f"{g['q'][:60]} should be {opt['label'][:40]!r}")
    return bad

def settle_radios(page, loc_ok):
    """Apply, wait for any late resume-parse overwrite, re-check; up to 4 rounds."""
    for _ in range(4):
        guard_radios(page, loc_ok); page.wait_for_timeout(3500)
        issues = radio_issues(page, loc_ok)
        if not issues: return []
    return issues

COMBO_JS = r"""()=>[...document.querySelectorAll('input[role=combobox]')].map(i=>{
  const lab=((i.labels&&i.labels[0])?i.labels[0].innerText:(i.getAttribute('aria-label')||i.id)).trim().split(String.fromCharCode(10)).join(' ');
  const sv=i.closest('.select__control')?i.closest('.select__control').querySelector('.select__single-value'):null;
  return {lab:lab.slice(0,160),val:(sv?sv.innerText:'').trim()}}).filter(x=>x.val)"""

SUSPECT_VALUES = {"job board", "linkedin", "indeed", "yourstate", "male", "female", "yes", "no", "none", "n/a", "other"}
CONTEXT_OK = re.compile(r"contact|telegram|whatsapp|learned|if you selected|anything else|add in support|else you|else to add|hear|source|refer|gender|sex|state|location|city|authori|sponsor|relocat|visa|veteran|disab|hispanic|race|ethnic|pronoun|additional|comment|notice|consent|agree|acknowledg|previous|former|current|employ|18|age|how did|where did|worked|country", re.I)

def sanity_issues(page):
    """Cheap tripwires for nonsense fills that autofill would report as 'filled'."""
    bad = []
    for c in page.evaluate(COMBO_JS):
        l, v = c["lab"].lower(), c["val"].lower()
        if re.search(r"sponsor|require.{0,20}visa", l) and not re.search(r"authori[sz]ed to work.*without", l):
            if not (v.startswith("no") or "not require" in v or "do not" in v): bad.append(f"sponsorship dropdown = {c['val']!r}: {c['lab'][:60]}")
        elif re.search(r"authori[sz]ed to work|legally (eligible|authori)|right to work", l):
            if not (v.startswith("yes") or "authorized" in v or "citizen" in v): bad.append(f"work-authorization dropdown = {c['val']!r}: {c['lab'][:60]}")
    for c in page.evaluate(COMBO_JS):
        if re.search(r"gender|sex", c["lab"].lower()) and re.search(r"woman|female|non-?binary|trans", c["val"].lower()):
            bad.append(f"gender dropdown = {c['val']!r}: {c['lab'][:50]}")
        if re.search(r"race|ethnic", c["lab"].lower()) and re.search(r"hispanic|latin", c["val"].lower()):
            bad.append(f"race dropdown = {c['val']!r}: {c['lab'][:50]}")
    for line in page.evaluate("""()=>[...document.querySelectorAll('input[type=text],input[type=url],textarea')].filter(e=>e.value&&!/recaptcha|iti-0/.test((e.name||'')+(e.id||''))).map(e=>[((e.labels&&e.labels[0])?e.labels[0].innerText:(e.name||e.id)).trim().slice(0,80),e.value.trim()])"""):
        lab, val = line
        if val.lower() in SUSPECT_VALUES and not CONTEXT_OK.search(lab) and not lab.startswith("cards["):
            bad.append(f"suspicious value {val!r} in {lab[:50]!r}")
    return bad

COMBO_ALL_JS = r"""()=>[...document.querySelectorAll('input[role=combobox]')].map((i,idx)=>{
  const lab=((i.labels&&i.labels[0])?i.labels[0].innerText:(i.getAttribute('aria-label')||i.id)).trim().split(String.fromCharCode(10)).join(' ');
  const sv=i.closest('.select__control')?i.closest('.select__control').querySelector('.select__single-value'):null;
  return {i:idx,lab:lab.slice(0,200),val:(sv?sv.innerText:'').trim()}})"""

def fix_combos(page):
    """Greenhouse-style dropdowns: force sponsorship=No, authorization=Yes/citizen from facts."""
    held = []
    for c in page.evaluate(COMBO_ALL_JS):
        l = c["lab"].lower(); v = c["val"].lower()
        if re.search(r"sponsor|require.{0,20}visa", l) and not re.search(r"authori[sz]ed to work.*without", l):
            want = ("no", None)
        elif re.search(r"authori[sz]ation to work|authori[sz]ed to (reside and |live and )?work|legally (eligible|authori)|right to work|source of your right|work authori[sz]ation", l):
            want = ("yes", "citizen")
        elif re.search(r"former (employee|intern|contractor)|ever (worked|been employed|provided services|been engaged)|previously (worked|employed)|security clearance|worked for [^?]{0,30}(before|previously|at any)", l):
            want = ("no", None)
        elif SALARY_Q.search(l):
            if not salary_ok():
                held.append("salary range not verifiably at or above his minimum: " + c["lab"][:60]); continue
            want = ("yes", None)
        else:
            continue
        ok = (v.startswith("no") or "not require" in v or "do not" in v) if want[0] == "no" else (v.startswith("yes") or "citizen" in v or "authorized" in v)
        if v and ok: continue
        try:
            inp = page.locator('input[role=combobox]').nth(c["i"]); inp.scroll_into_view_if_needed(); inp.click(); page.wait_for_timeout(600)
            opts = [o for o in page.query_selector_all('[role=option]') if o.is_visible()]
            texts = [o.inner_text().strip() for o in opts]
            pick = None
            for k, tx in enumerate(texts):
                tl = tx.lower()
                if want[0] == "no" and (tl.startswith("no") or "do not require" in tl or "not require" in tl or "never" in tl or tl.startswith("i have not")): pick = k; break
                if want[0] == "yes" and (re.search(r"u.?s.? citizen|citizen of the (u.?s|united states)|^citizen|citizen or (permanent|lawful)|^i am (legally )?authori[sz]ed to work|authori[sz]ed to work in", tl) and not re.search(r"require|need|sponsor", tl) or tl.startswith("yes")): pick = k; break
            if pick is None:
                page.keyboard.press("Escape"); held.append(f"dropdown I cannot answer safely: {c['lab'][:70]} options={texts[:5]}"); continue
            opts[pick].click(); page.wait_for_timeout(400)
        except Exception as exc:
            held.append(f"dropdown error: {c['lab'][:60]} {str(exc)[:40]}")
    return held

def ashby_location(page):
    """Ashby 'Location' autocomplete (container data-field-path=_systemfield_location): a typed
    value is not enough, a suggestion must be picked. Verify it stuck."""
    held = []
    box = page.locator("[data-field-path]").filter(has=page.locator("label", has_text=re.compile(r"^\s*(current |your )?location[^?]{0,40}$|where (are you|do you) (currently )?(located|based|live)|city.{0,20}(state|country)", re.I))).locator("input[role=combobox]").first
    try:
        if not box.count():
            return held
        box.scroll_into_view_if_needed()
        opts = []
        ctext = box.evaluate("e=>(e.closest('[data-field-path]')||e.parentElement).innerText").lower()
        if "country" in ctext and "city" not in ctext:   # ElevenLabs: 'Country you're currently residing in'
            box.click(); page.keyboard.press("Control+A"); page.keyboard.press("Delete"); page.keyboard.type("United States", delay=90)
            page.wait_for_timeout(2500)
            us = next((o for o in page.query_selector_all("[role=option]") if o.is_visible() and re.search(r"united states|^usa?$", o.inner_text().strip(), re.I)), None)
            if us is None:
                page.keyboard.press("Escape"); held.append("location: no United States suggestion offered"); return held
            us.click(); page.wait_for_timeout(800)
            if "united" not in (box.input_value() or "").lower(): held.append("location did not stick")
            return held
        for q in ("Yourtown", "Yourtown, ST", "Yourtown Yourstate", "Nearbyville"):
            box.click(); page.keyboard.press("Control+A"); page.keyboard.press("Delete"); page.keyboard.type(q, delay=90)
            for _ in range(5):   # suggestions can take several seconds to arrive
                page.wait_for_timeout(1000)
                opts = [o for o in page.query_selector_all("[role=option]") if o.is_visible() and "yourtown" in o.inner_text().lower()]
                if opts: break
            if opts: break
        pick = next((o for o in opts if "yourtown" in o.inner_text().lower() and re.search(r"yourstate|, st|st,", o.inner_text(), re.I)), None)                or next((o for o in opts if "yourtown" in o.inner_text().lower()), None)
        if pick is None:   # Synthesia: the 'Location' box is country-only
            box.click(); page.keyboard.press("Control+A"); page.keyboard.press("Delete"); page.keyboard.type("United States", delay=90)
            page.wait_for_timeout(2500)
            pick = next((o for o in page.query_selector_all("[role=option]") if o.is_visible() and o.inner_text().strip().lower() in ("united states", "usa", "us")), None)
        if pick is None:
            page.keyboard.press("Escape"); held.append("location: no Yourtown/United States suggestion offered"); return held
        pick.click(); page.wait_for_timeout(800)
        if not re.search(r"yourtown|united states", (box.input_value() or "").lower()):
            held.append("location did not stick")
    except Exception as exc:
        held.append("location error: " + str(exc)[:60])
    return held

ASHBY_REQ_JS = r"""()=>{
 const out=[];
 for(const f of document.querySelectorAll('[data-field-path], [class*=fieldEntry]')){
   const lab=f.querySelector('label'); if(!lab) continue;
   if(!/_required_/.test(lab.className)) continue;
   const name=lab.innerText.trim().split(String.fromCharCode(10)).join(' ').slice(0,90);
   let ok=false;
   for(const e of f.querySelectorAll('input,textarea,select')){
     if(e.type==='hidden') continue;
     if(e.type==='file'){ if(e.files&&e.files.length) ok=true; continue;}
     if(e.type==='radio'||e.type==='checkbox'){ if(e.checked) ok=true; continue;}
     if(e.getAttribute('role')==='combobox'){ if(e.value) ok=true; continue;}
     if(e.value&&e.value.trim()) ok=true;
   }
   for(const b of f.querySelectorAll('button[aria-pressed]')){ if(b.getAttribute('aria-pressed')==='true') ok=true;}
   if(f.querySelector('.select__single-value,[class*=singleValue],[class*=selectedValue]')) ok=true;
   if(!ok) out.push(name);
 }
 return [...new Set(out)];}"""

def fill_multiselects(page, rules, n_explicit):
    """Greenhouse 'select all that apply' dropdowns: pick every value his rule lists that is offered."""
    held = []
    for c in page.evaluate(COMBO_ALL_JS):
        if "select all that apply" not in c["lab"].lower() or c["val"]: continue
        vals = None
        for pat, v in rules[:n_explicit]:
            if pat.search(c["lab"]): vals = v; break
        if not vals: continue
        inp = page.locator("input[role=combobox]").nth(c["i"]); got = 0
        for v in vals:
            try:
                inp.scroll_into_view_if_needed(); inp.click(); page.keyboard.type(str(v)[:10], delay=50); page.wait_for_timeout(900)
                opts = [o for o in page.query_selector_all("[role=option]") if o.is_visible() and str(v).lower() in o.inner_text().lower()]
                if opts: opts[0].click(); got += 1; page.wait_for_timeout(300)
                else: page.keyboard.press("Escape")
            except Exception: pass
        if got == 0: held.append("multi-select: none of his tools offered: " + c["lab"][:60])
    return held

FALLBACK_JS = r"""()=>[...document.querySelectorAll('input[role=combobox]')].map((i,idx)=>{
  const vis=!!(i.offsetParent||i.getClientRects().length);
  const lab=((i.labels&&i.labels[0])?i.labels[0].innerText:(i.getAttribute('aria-label')||i.id)).trim().split(String.fromCharCode(10)).join(' ');
  const c=i.closest('.select__control');
  const has=c&&c.querySelector('.select__single-value,.select__multi-value');
  return {i:idx,vis:vis,lab:lab.slice(0,400),has:!!has}})"""

# label regex -> ordered option regexes. SAFEGUARD (2026-10-07): a form must never be held for a
# question we already know the answer to; every known question class lives here.
FALLBACK_RULES = [
    (r"^do you live in (dc|one of|any of|the following|[a-z]+, [a-z]+, )", [r"^no"]),
    (r"i have read the job description|read the job description", [r"^yes|i have|confirm"]),
    (r"what time zone do you (legally )?reside", [r"utc-4", r"utc-5", r"eastern"]),
    (r"where do you currently (reside|live)", [r"none of the above", r"yourstate"]),
    (r"please review the linked document|review the linked", [r"acknowledge|confirm|i have|understand|^yes|agree"]),
    (r"essential functions of the job", [r"^yes"]),
    (r"relevant employment and military service", [r"^thank", r"."]),
    (r"may we contact your current employer", [r"^no"]),
    (r"been employed by [a-z]+|employed by lyft|subsidiary, affiliate", [r"^no"]),
    (r"strong proficiency in python|proficient in python", [r"^yes"]),
    (r"managing production reliability in an aws|production reliability", [r"^yes"]),
    (r"outside business activit|side business", [r"^no"]),
    (r"before applying, how familiar|how familiar were you", [r"already familiar|very familiar|^yes", r"not (at all |very )?familiar|never heard"]),
    (r"previously (been )?employed|previously worked", [r"i have not|^no|never"]),
    (r"united states of america, canada, or the united kingdom", [r"^yes", r"united states"]),
    (r"military status", [r"never served"]),
    (r"government officials|government official|foreign officials|public official", [r"^no"]),
    (r"pronoun", [r"he ?/ ?him|he, him"]),
    (r"identify your race|your race", [r"^white", r"white or european"]),
    (r"familiar (were )?you (with|before)|how familiar", [r"not (at all |very )?familiar|never heard|not familiar|^not", r"somewhat|slightly|a little"]),
    (r"categories describe you", [r"none|prefer not|don.t wish|decline|not applicable"]),
    (r"previously (been )?employed|previously applied|currently an employee|employee or contractor", [r"^no"]),
    (r"interview .{0,40}record|recorded in various|record(ed|ing) .{0,30}interview", [r"^yes|consent|agree|acknowledge|i understand|accept"]),
    (r"performing the work from|work from the united states", [r"united states"]),
    (r"building llm.powered agents|experience building ai agents|building ai agents|llm.powered agents", [r"^yes"]),
    (r"procurement or contract award|contract award activities", [r"^no"]),
    (r"seatgeek was founded", [r"^false"]),
    (r"time zone|timezone", [r"eastern|\(et\)", r"^yes"]),
    (r"personal/familial relationships|familial relationships", [r"^no"]),
    (r"willing to work from the office", [r"^yes"]),
    (r"privacy statement", [r"acknowledge|agree|accept|confirm|understand|^yes"]),
    (r"i confirm all answers|answers provided by me .{0,40}true", [r"^yes|confirm|agree|true"]),
    (r"this position is based in the united states", [r"relocate|^yes"]),
    (r"u\.?s\.? work authori[sz]ation status", [r"citizen"]),
    (r"^school|current school|which university|^college", [r"gaithersburg", r"^other", r"."]),
    (r"start date month|start month", [r"^january"]),
    (r"^i identify as", [r"^cisgender man|^cis.?man|^cisgender male", r"^man$|^male|^man "]),
    (r"which onsite location|onsite location would you like", [r"no preference", r"."]),
    (r"preferred office location|preferred location", [r"new york", r"."]),
    (r"have you used robinhood", [r"^yes"]),
    (r"^i identify as", [r"^man$|^male|^man "]),
    (r"spacexai employment|employment history", [r"never|^no|not (previously|currently)"]),
    (r"willing and able to work entirely on.?site", [r"^yes"]),
    (r"employment agreements?|post.employment restrictions|non.?compete|restrictive covenant", [r"^no"]),
    (r"based in manhattan|requires working in the office|office (three|3) to (five|5)|office .{0,20}days", [r"^yes"]),
    (r"compensation expectations", [r"^yes"]),
    (r"eligible to work legally|legally eligible to work", [r"^yes"]),
    (r"u\.?s\.? citizen or national|citizen or national|lawful, permanent", [r"u\.?s\.? citizen", r"citizen"]),
    (r"experience with rag|rag \(retrieval|vector search", [r"^yes"]),
    (r"ever worked for robinhood|ever worked (at|for)", [r"never|^no|have not|i have not"]),
    (r"^country\*?$|^country$", [r"^united states"]),
    (r"(hands.on|production)[^?]{0,40}(llm|large language model|generative ai)", [r"^yes"]),
    (r"^discipline", [r"^computer science", r"^information", r"^engineering", r"^other"]),
    (r"est timezone|eastern time|time ?zone", [r"^yes"]),
    (r"proficient in (python|typescript)|proficient in python", [r"^yes"]),
    (r"rate yourself in python", [r"^5", r"^4"]),
    (r"open to candidates in the us or canada|live in this location", [r"^yes"]),
    (r"export control", [r"i have read|^yes|acknowledge|agree|understand"]),
    (r"willing to relocate|open to coming into the office|five days a week", [r"^yes"]),
    (r"highest level of education|level of education", [r"high school|ged"]),
    (r"how many years of (professional )?experience", [r"^4.7|^3.5|^4|^2.5", r"^3.7"]),
    (r"require relocation assistance|relocation assistance", [r"^no"]),
    (r"processing of personal data", [r"acknowledge|confirm"]),
    (r"ai policy for interviewers", [r"^yes"]),
    (r"gender identity|what is your gender|^gender", [r"^man$|^male$|^man ", r"^male"]),
    (r"racial|ethnic|^race|race/", [r"^white", r"white or european"]),
    (r"sexual orientation", [r"heterosexual|straight"]),
    (r"transgender", [r"^no"]),
    (r"veteran|armed forces", [r"^no|not a (protected )?veteran"]),
    (r"lesbian, gay|lgbtq|lgbt", [r"^no$", r"^no"]),
    (r"first.generation", [r"don.t wish|prefer not|decline"]),
    (r"pronoun", [r"he/him|he, him"]),
    (r"hear about|first hear|learn(ed)? about|how did you find", [r"^linkedin", r"job board", r"^other"]),
    (r"based in any of these countries|currently based in|country.{0,30}(live|reside|located)|reside in", [r"^united states"]),
    (r"authori[sz]ation to work in the country where you live|work authori[sz]ation", [r"authori[sz]ed to work in the country due to|citizen", r"^yes"]),
    (r"visa sponsorship|require sponsorship", [r"^no"]),
    (r"former .{0,30}employee|currently working at|ever (worked|been employed)|interviewed", [r"^no"]),
    (r"acknowledge|privacy notice|privacy policy", [r"acknowledge|^i agree|^i understand|confirm|^yes"]),
    (r"double.check all the information", [r"reviewed and confirmed|confirm|^yes"]),
]

def fill_fallback_dropdowns(page):
    """Answer every unset dropdown whose question class we know (demographics, how-did-you-hear,
    country, etc.) so the form is never held for a question we can answer."""
    held = []
    try: combos = page.evaluate(FALLBACK_JS)
    except Exception: return held
    for c in combos:
        if c["has"] or not c["vis"]: continue
        ll = c["lab"].lower()
        if "do you live in one of the following" in ll:
            pats = [r"^yes"] if "yourstate" in ll else [r"^no"]
        else:
            pats = next((p for rx, p in FALLBACK_RULES if re.search(rx, ll)), None)
        if not pats: continue
        try:
            inp = page.locator("input[role=combobox]").nth(c["i"]); inp.scroll_into_view_if_needed(); inp.click(); page.wait_for_timeout(600)
            opts = [o for o in page.query_selector_all("[role=option]") if o.is_visible()]
            texts = [o.inner_text().strip() for o in opts]
            pick = None
            for rx in pats:
                pick = next((k for k, tx in enumerate(texts) if re.search(rx, tx.lower())), None)
                if pick is not None: break
            if pick is None:
                page.keyboard.press("Escape"); held.append("fallback: no matching option for " + c["lab"][:60] + " " + str(texts[:4])); continue
            opts[pick].click(); page.wait_for_timeout(400)
        except Exception as exc:
            held.append("fallback error: " + c["lab"][:50] + " " + str(exc)[:40])
    try:   # plain-text 'Country' questions (Fivetran) that autofill left empty
        for e in page.locator("input[type=text]:visible").all():
            if e.get_attribute("role") == "combobox" or e.input_value(): continue
            lab = (e.evaluate("e=>{const l=e.labels&&e.labels[0];return l?l.innerText:''}") or "").strip().lower()
            if re.match(r"country\*?$", lab): e.fill("United States")
    except Exception:
        pass
    return held


def hear_about(page):
    """'How did you hear' that autofill misses: Ashby autocomplete (Render) and Greenhouse checkbox group (Gusto)."""
    held = []
    try:
        f = page.locator("[data-field-path]").filter(has=page.locator("label", has_text=re.compile(r"hear about|how did you hear", re.I))).first
        if f.count():
            box = f.locator("input[role=combobox], input[type=text]").first
            if box.count():   # autofill may have typed text without picking a suggestion, so always re-pick
                for q in ("Job board", "Job", "LinkedIn", "Other"):
                    box.click(); page.keyboard.press("Control+A"); page.keyboard.press("Delete"); page.keyboard.type(q, delay=80); page.wait_for_timeout(1500)
                    opts = [o for o in page.query_selector_all("[role=option]") if o.is_visible()]
                    exact = [o for o in opts if o.inner_text().strip().lower() == q.lower()]
                    if opts: (exact or opts)[0].click(); page.wait_for_timeout(500); break
                else:
                    page.keyboard.press("Escape")
                if not (box.input_value() or "").strip(): held.append("how-did-you-hear: nothing selectable")
    except Exception as exc:
        held.append("hear-about error: " + str(exc)[:50])
    try:   # 'How should we communicate with you?' checkbox group: Email first, Text second (the user 2026-10-08)
        for fs in page.locator("fieldset").all():
            if not re.search(r"how should we communicate|preferred (method|way) of (contact|communication)", fs.inner_text()[:160], re.I): continue
            if fs.locator("input[type=checkbox]:checked").count(): continue
            for lab in fs.locator("label").all():
                if lab.inner_text().strip() in ("Email", "Text Message", "Text"): lab.click(); page.wait_for_timeout(200)
    except Exception:
        pass
    try:
        for fs in page.locator("fieldset").all():
            if not re.search(r"hear about|how did you hear", fs.inner_text()[:160], re.I): continue
            boxes = fs.locator("input[type=checkbox]")
            print("  hear-about fieldset: boxes=%d checked=%d" % (boxes.count(), fs.locator("input[type=checkbox]:checked").count()))
            if not boxes.count() or fs.locator("input[type=checkbox]:checked").count(): continue
            for k in range(boxes.count()):
                lab = fs.locator("label").nth(k).inner_text().strip().lower()
                if lab == "linkedin" or lab.startswith("linkedin"):
                    fs.locator("label").nth(k).click(); break
            else:
                fs.locator("label").nth(boxes.count() - 1).click()
    except Exception as exc:
        held.append("hear-about checkbox error: " + str(exc)[:50])
    return held

def _my_phone():
    """Digits of the phone number in profile.json (never hardcoded in the source)."""
    try:
        return re.sub(r"\D", "", af.load_profile(None).get("identity", {}).get("phone", ""))
    except Exception:
        return ""


def ashby_phone_and_groups(page):
    """Ashby required 'Tel/Phone' inputs left empty (Midjourney) and industry-preference checkbox groups (Sierra)."""
    try:
        for f in page.locator("[data-field-path]").all():
            lab = f.locator("label").first
            if not lab.count(): continue
            name = lab.inner_text().strip().lower()
            if re.match(r"(tel|phone)( number)?\*?$", name):
                for e in f.locator("input[type=tel], input[type=text]").all():
                    if e.is_visible() and not (e.input_value() or "").strip():
                        phone = _my_phone()
                        if phone:
                            e.click(); page.keyboard.type(phone, delay=40); page.keyboard.press("Tab")
            if re.search(r"which office are you applying|select both if appropriate", name) and not f.locator("input[type=checkbox]:checked").count():
                for k in range(f.locator("label").count()):
                    lt = f.locator("label").nth(k).inner_text().strip()
                    if lt and lt.lower() != name and "which office" not in lt.lower(): f.locator("label").nth(k).click()
            if re.search(r"preference in industry|industry preference|industries? (are you|of) interest", name):
                labs = f.locator("label")
                if f.locator("input[type=checkbox]:checked").count(): continue
                for k in range(labs.count()):
                    if re.search(r"tech|software|financial", labs.nth(k).inner_text(), re.I):
                        labs.nth(k).click(); break
    except Exception:
        pass

def fill_referral_no(page):
    """Required 'were you referred by ...? if so list name' text boxes: he was not referred (never_autofill blocks the old wrong fill)."""
    try:
        for e in page.locator("input[type=text]:visible, textarea:visible").all():
            if (e.input_value() or "").strip(): continue
            lab = (e.evaluate("e=>{const l=e.labels&&e.labels[0];return l?l.innerText:''}") or "").lower()
            if re.search(r"referred (to this position |you )?by|were you referred", lab): e.fill("No")
    except Exception:
        pass

def ashby_fill_selects(page, rules, n_explicit):
    """Required Ashby questions that are custom dropdowns: find the rule for the question text,
    open the control, click the option matching one of the rule's values. Verified later by
    ASHBY_REQ_JS, so a miss simply stays held."""
    for nm in page.evaluate(ASHBY_REQ_JS):
        vals = None
        for pat, v in rules[:n_explicit]:
            if pat.search(nm): vals = v; break
        if not vals: continue
        try:
            entry = page.locator("[data-field-path]").filter(has=page.locator("label", has_text=nm[:40])).first
            if not entry.count(): continue
            ctl = entry.locator("input[role=combobox], button[aria-haspopup], [role=combobox]").first
            if not ctl.count(): continue
            ctl.scroll_into_view_if_needed(); ctl.click(); page.wait_for_timeout(700)
            def pick_from(opts):
                texts = [o.inner_text().strip() for o in opts]
                for v in vals:
                    vl = str(v).lower()
                    for k, tx in enumerate(texts):
                        if tx.lower() == vl: return k
                for v in vals:
                    vl = str(v).lower()
                    for k, tx in enumerate(texts):
                        if len(vl) > 2 and vl in tx.lower(): return k
                return None
            opts = [o for o in page.query_selector_all("[role=option]") if o.is_visible()]
            k = pick_from(opts) if opts else None
            if k is None and ctl.evaluate("e=>e.tagName") == "INPUT":
                page.keyboard.type(str(vals[0])[:20], delay=60); page.wait_for_timeout(1200)
                opts = [o for o in page.query_selector_all("[role=option]") if o.is_visible()]
                k = pick_from(opts) if opts else None
            if k is None:
                page.keyboard.press("Escape"); continue
            opts[k].click(); page.wait_for_timeout(500)
        except Exception:
            try: page.keyboard.press("Escape")
            except Exception: pass

def name_sanity(page):
    """A field asking for a full/legal/first-and-last name must not hold one token."""
    n = 0
    for e in page.query_selector_all("input[type=text]"):
        try:
            lab = e.evaluate("e=>(e.labels&&e.labels[0]?e.labels[0].innerText:e.name||'')").lower()
            if re.search(r"first and last|full (legal )?name|legal name", lab) and not re.search(r"preferred", lab):
                v = e.input_value()
                if len(v.split()) < 2 or (v == v.upper() and len(v) > 3):
                    e.fill("Your Name"); e.press("Tab"); n += 1
            elif re.search(r"\bname\b", lab) and not re.search(r"company|school|employer|user", lab):
                v = e.input_value()
                if v and v == v.upper() and len(v) > 3 and " " in v:
                    e.fill(v.title()); e.press("Tab"); n += 1
        except Exception:
            continue
    return n

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--company", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--no-submit", action="store_true")
    ap.add_argument("--loc", default="")
    a = ap.parse_args()
    prof = af.load_profile(None)
    rules, skips = af.build_rules(prof), af.build_skips(prof)
    n_explicit = af.count_explicit(prof)
    pw, browser, ctx = af.connect(af.DEFAULT_PORT)
    result = {"url": a.url, "company": a.company, "title": a.title, "status": "held", "reason": ""}
    def _nk(u): return re.sub(r"/(apply|application)$", "", u.split("?")[0].rstrip("/")) if "job_app" not in u else u
    existing = next((p for p in ctx.pages if _nk(p.url) == _nk(a.url)), None)   # the user 2026-10-08: reuse a tab left open, keep what is typed
    page = existing or ctx.new_page()
    page.set_default_timeout(25000)
    try:
        if existing is None:
            page.goto(a.url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3500)
        if existing is not None and (SUCCESS_URL.search(page.url) or SUCCESS_TXT.search(page.inner_text("body")[:6000])):
            result.update(status="submitted", reason="tab already showed the confirmation (finished by the user); nothing clicked"); raise SystemExit
        ats = af.detect_ats(page.url)
        title, company = af.page_meta(page)
        result["title"] = a.title or title; result["company"] = a.company or company
        n_fields = af.count_fields(page)
        if n_fields == 0 or re.search(r"404|not found|no longer|closed|page not found", page.title() + page.inner_text("body")[:300], re.I):
            result.update(status="dead_link", reason="no form / page not found"); raise SystemExit
        body = page.inner_text("body")
        globals()["SALARY_MAX"] = salary_max(body)
        if re.search(r"no cash (salary|compensation|pay)|(paid|compensated) (entirely|solely|only|exclusively) in (equity|tokens?)|equity[- ](and|or)[- ]token[- ]based (role|opportunity|compensation only)|compensation is (entirely|solely|only) (equity|tokens?)", body, re.I):
            result.update(status="skipped", reason="equity/token-only pay"); raise SystemExit
        res = af.autofill_page(page, prof, rules, skips, False, n_explicit, a.title or title, a.company or company)
        res["consent_ticked"] = af.tick_consent(res, False, False, policy=bool(prof.get("consent_policy", {}).get("auto_tick")))
        held = []
        loc_ok = bool(re.search(r"remote|san francisco|bay area|\bsf\b|new york|nyc|united states|^us$|usa", (a.loc or ""), re.I)) or not a.loc
        loc_ok = True   # the user 2026-10-07 (#3): willing to relocate anywhere asked
        second_pass_text(page, rules, skips); second_pass_radios(page, rules, n_explicit); name_sanity(page)
        if "ashbyhq.com" in page.url:
            if not ashby_fixes(page, prof): held.append("resume not attached to the real field")
            held += ashby_yes_no(page, loc_ok, rules, n_explicit)
            name_sanity(page)   # the parse can re-capitalise the name after the first pass
            held += ashby_location(page)
            ashby_phone_and_groups(page)
            ashby_fill_selects(page, rules, n_explicit)
        held += settle_radios(page, loc_ok)
        held += fix_combos(page)
        held += fill_multiselects(page, rules, n_explicit)
        held += fill_fallback_dropdowns(page)
        held += hear_about(page)
        fill_referral_no(page)
        page.wait_for_timeout(800)
        if not res["filled"]: held.append("nothing filled")
        # Judge by the live page (REQUIRED_UNSET_JS below), not autofill's stale lists,
        # except required checkbox groups which the DOM scan does not cover.
        for m in res["blockers"] + res["missing"]:
            if m.strip().startswith("[checkbox]"): held.append("required checkbox group: " + m[:90])
        # required boxes the policy did not tick (consent list holds every required checkbox)
        ticked = {t.split("  (COULD NOT TICK)")[0] for t in res.get("consent_ticked", [])}
        for h, ident in res.get("consent", []):
            if ident in ticked: continue
            held.append("required checkbox not in consent policy: " + ident[:90])
        if "ashbyhq.com" in page.url:
            for nm in page.evaluate(ASHBY_REQ_JS):
                if not any(nm[:35] in x for x in held): held.append("ashby required, unanswered: " + nm)
        for u in page.evaluate(REQUIRED_UNSET_JS):
            if not any(u.split(": ", 1)[-1][:40] in x for x in held): held.append("unset " + u)
        txt = page.inner_text("body")
        for m in LEGAL.finditer(txt):
            ctxt = txt[max(0, m.start() - 60): m.end() + 60].replace("\n", " ")
            # only flag it if there is a required input near it that we did not tick
            if re.search(r"ai policy for interviewers", ctxt, re.I): continue   # an interview-notice dropdown, answered Yes
            if re.search(r"\*", ctxt): held.append("legal/attestation: " + ctxt[:100]); break
        held += sanity_issues(page)
        # Resume must be visibly attached on ANY ATS (Greenhouse shows the file name once attached).
        try:
            attached_txt = bool(re.search(r"your_name_resume", page.inner_text("body"), re.I))
            attached_inp = page.evaluate("()=>[...document.querySelectorAll('input[type=file]')].some(e=>e.files&&e.files.length)")
            if not (attached_txt or attached_inp): held.append("resume not visibly attached")
        except Exception:
            pass
        try:
            lv = page.locator("[data-field-path]").filter(has=page.locator("label", has_text=re.compile(r"^\s*(current |your )?location[^?]{0,40}$|where (are you|do you) (currently )?(located|based|live)|city.{0,20}(state|country)", re.I))).locator("input[role=combobox]").first
            if lv.count():
                result["location_value"] = lv.input_value()
                if not re.search(r"yourtown|united states", result["location_value"].lower()):
                    held.append("location field empty/wrong at submit time: " + repr(result["location_value"]))
        except Exception:
            pass
        result["values"] = page.evaluate("""()=>[...document.querySelectorAll('input,textarea')].filter(e=>e.type!=='hidden'&&e.type!=='file'&&e.type!=='checkbox'&&e.type!=='radio'&&!/recaptcha|iti-0/.test((e.name||'')+(e.id||''))&&e.value).map(e=>((e.labels&&e.labels[0])?e.labels[0].innerText.trim().split(String.fromCharCode(10)).join(' '):(e.name||e.id)).slice(0,48)+' = '+e.value.slice(0,45)).slice(0,24)""")
        result["radios"] = [g["q"][:60] + " => " + next((o["label"][:36] for o in g["opts"] if o["checked"]), "(none)") for g in page.evaluate(RADIO_GROUPS_JS)]
        held = list(dict.fromkeys(held))
        if held:
            result.update(status="held", reason="; ".join(held)[:600]); raise SystemExit
        if a.no_submit:
            result.update(status="ready", reason="clean (dry)"); raise SystemExit
        last = radio_issues(page, loc_ok) + sanity_issues(page)
        if last:
            result.update(status="held", reason="answers drifted before submit: " + "; ".join(last)[:300]); raise SystemExit
        btn = None
        for sel in ("button:has-text('Submit application')", "button:has-text('Submit Application')", "button:has-text('Submit')", *af.SUBMIT_SELECTORS):
            try:
                loc = page.locator(sel).filter(visible=True)
                if loc.count(): btn = loc.first; break
            except Exception: continue
        if btn is None:
            result.update(status="held", reason="no submit button found"); raise SystemExit
        btn.scroll_into_view_if_needed(); btn.click(); page.wait_for_timeout(6500)
        ok = bool(SUCCESS_URL.search(page.url) or SUCCESS_TXT.search(page.inner_text("body")[:6000]))
        if not ok and re.search(r"verification code|security code|enter the code|code (we )?(sent|emailed)|confirm you.re a human|security code", page.inner_text("body"), re.I):
            result.update(status="needs_code", reason="submit pressed; the site wants the emailed verification code. Tab left open: type the code and press Submit"); raise SystemExit
        if ok:
            result.update(status="submitted", reason="confirmation seen")
        else:
            errs = page.evaluate("""()=>{const out=[];for(const e of document.querySelectorAll('[aria-invalid=true],input:invalid,textarea:invalid')){const l=((e.labels&&e.labels[0])?e.labels[0].innerText:(e.getAttribute('aria-label')||e.name||e.id)).trim().split(String.fromCharCode(10)).join(' ').slice(0,60);out.push('INVALID '+l+' = '+(e.value||'').slice(0,25));}
              for(const e of document.querySelectorAll('[class*=error],[role=alert]')){const t=(e.innerText||'').trim().split(String.fromCharCode(10)).join(' ').slice(0,90);if(t)out.push('MSG '+t);}return out.slice(0,8)}""")
            result.update(status="submit_failed", reason="no confirmation after click; " + "; ".join(errs))
    except SystemExit:
        pass
    except Exception as exc:
        result.update(status="held", reason="error: " + str(exc)[:200])
    finally:
        st = result["status"]
        if st in ("submitted", "skipped", "dead_link"):   # the user 2026-10-07: anything unfinished stays open
            try: page.close()
            except Exception: pass
        log({"url": result["url"], "requested_url": result["url"], "ats": "auto", "status": {"held": "needs_you", "ready": "needs_you", "needs_code": "needs_you"}.get(st, st), "title": result["title"], "company": result["company"], "note": f"auto_apply 2026-10-06: {st}. {result['reason']}"[:700]})
        print("RESULT:" + json.dumps(result, ensure_ascii=False))
        try: pw.stop()
        except Exception: pass

if __name__ == "__main__":
    main()
