#!/usr/bin/env python
"""
autofill.py - fill a job application form from a local profile, then stop.

It attaches to a Chrome you started yourself (see start_chrome.ps1), so your
logins persist, nothing is headless, and you watch every keystroke land.

It fills what it can match, refuses to touch anything sensitive, and NEVER
presses submit. You read the form and submit it. That is deliberate: both
Greenhouse and Lever put a CAPTCHA on the submit path, and a form filled by a
machine and submitted without a human read is how you send your resume to the
wrong job.

Usage
  python autofill.py https://job-boards.greenhouse.io/acme/jobs/123
  python autofill.py --queue jobs.txt        # one at a time, you drive the pace
  python autofill.py URL --dry               # report only, touch nothing
  python autofill.py --report                # what you've applied to
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:                                    # job posts are full of en-dashes
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# The vision layer is an improvement on the answering, never a dependency of
# it. If the file is missing, the anthropic package is not installed or there
# is no API key, autofill falls back to exactly the rule-only behaviour it had
# before - so this import can never be allowed to stop a run.
try:
    import vision_answer
except Exception:
    vision_answer = None

HERE = Path(__file__).resolve().parent
PROFILE_PATH = HERE / "profile.json"
LOG_PATH = HERE / "applications.jsonl"
DEFAULT_PORT = 9222

# Anything whose label matches these is left blank no matter what, on top of
# whatever the profile adds. EEO questions are voluntary and yours to answer;
# salary is a negotiating position, not a form field.
HARD_SKIP = [
    r"gender", r"\brace\b", r"ethnic", r"hispanic", r"veteran", r"disab",
    r"salary", r"compensation", r"expected pay", r"desired pay",
    r"date of birth", r"social security", r"\bssn\b",
]

# Elements we never write to, by id/name.
# Plumbing only. What gets answered vs left blank is decided by never_autofill
# and answers[] in profile.json, so you stay in control of it from one place -
# dropping fields by id here would silently outrank your own explicit answers.
SKIP_FIELD_IDS = re.compile(
    r"recaptcha|captcha|"
    r"^origin$|^referer$|^timezone$|baseTemplate|accountId|csrf|honeypot|"
    r"search-input|^iti-",          # intl-tel-input's country search box
    re.I,
)

# "Legal name (if different than above)", "Other name (if different)" - these
# want a value only when it differs from what you already typed. Leave them.
IF_DIFFERENT = re.compile(r"if different|if applicable|if any|if other", re.I)

# Greenhouse's react-select ships a sibling <input required> purely to trip
# native validation. It is not writable by us.
MIRROR_INPUT = re.compile(r"requiredInput|select-shell", re.I)

CAPTCHA_MARKERS = ["g-recaptcha-response", "h-captcha-response", "cf-turnstile"]

# Phone widgets (intl-tel-input, react-phone-number-input) seed the box with
# the dial code before you type anything. That is scaffolding, not an answer -
# but it makes the field read as already-filled, so it was skipped in silence
# and the form went out with no phone number on it.
PREFILL_ONLY = re.compile(r"^\+?\d{0,4}$")


# --------------------------------------------------------------------------
# profile + rules
# --------------------------------------------------------------------------

def load_profile(path: Path | None = None) -> dict:
    path = path or PROFILE_PATH
    if not path.exists():
        sys.exit(
            f"No profile at {path}\n"
            f"Copy profile.example.json to profile.json and fill it in first."
        )
    with path.open(encoding="utf-8") as fh:
        prof = json.load(fh)

    ident = prof.setdefault("identity", {})
    if not ident.get("full_name"):
        both = f"{ident.get('first_name', '')} {ident.get('last_name', '')}".strip()
        if both:
            ident["full_name"] = both
    return prof


def expand_tokens(value: str) -> str:
    """
    Expand date placeholders in an answers[] value.

    Self-ID blocks on applytojob end in a signature pair - a `Name` box and a
    `Date` box - and the date wanted there is the day you apply. Hardcoding it
    in profile.json means every application after the first carries a stale
    date, which is the kind of wrong nobody re-reads a form to catch.

    {today}     -> 08/02/2026   (US, what these forms show alongside)
    {today_iso} -> 2026-08-02
    """
    if "{today" not in value:
        return value
    now = datetime.now()
    return (value.replace("{today_iso}", now.strftime("%Y-%m-%d"))
                 .replace("{today}", now.strftime("%m/%d/%Y")))


def count_explicit(prof: dict) -> int:
    """How many leading rules came from your own answers[] list."""
    n = 0
    for a in prof.get("answers", []):
        if not a.get("match"):
            continue
        vals = a.get("value") if isinstance(a.get("value"), list) else [a.get("value")]
        if any(v and not str(v).strip().upper().startswith("TODO") for v in vals):
            n += 1
    return n


def build_rules(prof: dict) -> list[tuple[re.Pattern, str]]:
    """Ordered (label_pattern, value). First match wins."""
    ident = prof.get("identity", {})
    links = prof.get("links", {})
    work = prof.get("work", {})

    rules: list[tuple[re.Pattern, str]] = []

    def add(pattern: str, value) -> None:
        # A value may be a list of acceptable answers, tried in order. Dropdowns
        # only offer what they offer - "Job board" is useless on a form whose
        # options are LinkedIn / Indeed / Other, so you give it alternatives.
        vals = value if isinstance(value, list) else [value]
        # A TODO placeholder is an unanswered question, not an answer. Typing
        # it into a real application is worse than leaving the field blank.
        vals = [expand_tokens(str(v)) for v in vals
                if v and not str(v).strip().upper().startswith("TODO")]
        if vals:
            rules.append((re.compile(pattern, re.I), vals))

    # User-supplied answers go first so they can override anything below.
    for ans in prof.get("answers", []):
        if ans.get("match"):
            add(ans["match"], ans.get("value"))

    add(r"first\s*name|given name|forename", ident.get("first_name"))
    add(r"last\s*name|surname|family name", ident.get("last_name"))
    add(r"full name|preferred name|legal name|^name\b|your name", ident.get("full_name"))
    add(r"e-?mail", ident.get("email"))
    add(r"phone|mobile|telephone|cell", ident.get("phone"))

    add(r"linked\s*-?\s*in", links.get("linkedin"))
    add(r"git\s*hub", links.get("github"))
    add(r"portfolio|personal (web)?site|^website|your site", links.get("portfolio"))
    add(r"twitter|\bx\.com", links.get("twitter"))

    # Location first: "Where are you based? (city, country)" must NOT fall
    # through to the bare-country rule and get answered "United States".
    add(r"location|where are you (currently )?(located|based)|current location|"
        r"city\s*[,/&]\s*(country|state)|city and (country|state)",
        ident.get("location"))
    add(r"\bcountry\b", ident.get("country"))
    add(r"zip|postal", ident.get("postal_code"))
    add(r"street|address line|^address", ident.get("address"))
    add(r"\bstate\b|province|region", ident.get("state"))
    add(r"\bcity\b", ident.get("city") or ident.get("location"))

    add(r"current (company|employer)|company name|^company|^employer|organization",
        work.get("current_company"))
    add(r"current (job )?title|^job title|your title|current role", work.get("current_title"))
    add(r"how did you hear|referral source|where did you (hear|find)|source",
        work.get("how_did_you_hear"))
    add(r"sponsor|visa status|immigration", work.get("requires_sponsorship"))
    add(r"legally (authorized|eligible)|authorized to work|work authorization|right to work",
        work.get("work_authorized"))
    add(r"relocat", work.get("willing_to_relocate"))
    add(r"notice period|when can you start|earliest start|start date|availability",
        work.get("notice_period"))
    # NARROWED 2026-08-12. This used to be `years of (relevant )?experience|how
    # many years`, which answered his years_experience to ANY years-of-X
    # question. A LinkedIn form asked "How many years of Hospitals and Health
    # Care experience do you currently have?" and it went out as 5 - asserting
    # experience he does not have, which the standing rule in SOP-run.md forbids.
    # "years of experience" with no domain named still answers, because there the
    # subject IS the role applied for; a NAMED domain he does not work in now
    # falls through to needs_you and stops the form for him on purpose.
    add(r"years of (relevant |total |professional )?experience\b|"
        r"how many years.{0,40}(python|programming|software|engineering|develop|"
        r"comput|technical|artificial intelligence|\bai\b|machine learning|\bml\b|"
        r"\bllm\b|data scien|data engineer|analytics|cloud|devops|back.?end|"
        r"front.?end|full.?stack|automation|agent)",
        work.get("years_experience"))

    return rules


def build_skips(prof: dict) -> list[re.Pattern]:
    pats = list(HARD_SKIP) + list(prof.get("never_autofill", []))
    out = []
    for p in pats:
        try:
            out.append(re.compile(p, re.I))
        except re.error:
            print(f"  ! bad regex in never_autofill, ignoring: {p}")
    return out


# --------------------------------------------------------------------------
# page introspection
# --------------------------------------------------------------------------

FIELD_INFO_JS = r"""
e => {
  const rects = e.getClientRects();
  const cs = getComputedStyle(e);
  const visible = rects.length > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';

  let lbl = '';
  const grab = t => (t || '').replace(/\s+/g, ' ').trim();

  if (e.id) {
    const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
    if (l) lbl = grab(l.innerText);
  }
  if (!lbl) {
    const by = e.getAttribute('aria-labelledby');
    if (by) { const n = document.getElementById(by); if (n) lbl = grab(n.innerText); }
  }
  if (!lbl) lbl = grab(e.getAttribute('aria-label'));
  if (!lbl) { const l = e.closest('label'); if (l) lbl = grab(l.innerText); }
  if (!lbl) {
    const sels = ['.application-question', '.application-field', 'li',
                  '[class*="question"]', '[class*="field"]', 'fieldset', '.form-group'];
    for (const s of sels) {
      const c = e.closest(s);
      if (c) { const t = grab(c.innerText); if (t && t.length < 300) { lbl = t; break; } }
    }
  }
  if (!lbl) lbl = grab(e.placeholder);

  // Div-based forms often wire nothing at all: no for=, no id, no name, no
  // aria-label, no placeholder - the caption is just text sitting above the
  // control. Walk up a few wrappers for the nearest short text before it.
  // Without this such a field gets an empty label, matches no rule, and isn't
  // even reported as unanswered (an empty label reads as not-required), which
  // is exactly how a phone box stays blank without anyone noticing.
  if (!lbl) {
    // Reject widget chrome rather than caption text: a phone box's flag flyout
    // reads as '🇨🇦 +1', which is not what the question is called.
    const junk = t => !t || t.length > 120 || /^[^\p{L}]*\+?\d{1,4}$/u.test(t);
    let node = e;
    for (let up = 0; up < 4 && node && !lbl; up++) {
      const holder = node.parentElement;
      if (!holder) break;
      // Once a wrapper holds a control belonging to a DIFFERENT question we
      // have climbed too far, and any text here labels something else.
      // Controls inside the part we came from don't count: a phone widget
      // ships its own country <select>, and counting that stopped the climb
      // one level short of the caption every time.
      const others = [...holder.querySelectorAll('input, select, textarea')]
                       .filter(c => c !== e && !node.contains(c));
      if (up > 0 && others.length) break;
      // Direct children only, and only those BEFORE the control. Searching the
      // whole subtree for a <label> returns the first one on the page as soon
      // as we reach <body> - which silently labelled an unrelated free-text
      // box 'First Name' and would have typed a name into it.
      for (const child of holder.children) {
        if (child === node || child.contains(e)) break;
        const t = grab(child.innerText);
        if (!junk(t)) lbl = t;          // keep the nearest one above the field
      }
      node = holder;
    }
  }

  // Lever custom questions put the card title OUTSIDE the <li>, so the label
  // above can come back as a fragment like "(city, country) *". Walk back to
  // the preceding heading and prepend it, or the question reads as nonsense.
  if (!lbl || /^[(\[]/.test(lbl)) {
    const blk = e.closest('li, .application-question, .form-group');
    let prev = blk ? ((blk.parentElement || blk).previousElementSibling) : null;
    for (let i = 0; i < 3 && prev; i++) {
      const t = grab(prev.innerText);
      if (t && t.length < 200) { lbl = (t + ' ' + lbl).trim(); break; }
      prev = prev.previousElementSibling;
    }
  }

  return {
    tag: e.tagName.toLowerCase(),
    type: (e.type || '').toLowerCase(),
    cls: (typeof e.className === 'string' ? e.className : '') + ' ' +
         ((e.parentElement && typeof e.parentElement.className === 'string')
           ? e.parentElement.className : ''),
    name: e.name || '',
    id: e.id || '',
    visible: visible,
    disabled: !!e.disabled || e.readOnly,
    required: !!e.required || e.getAttribute('aria-required') === 'true'
              || /\*/.test(lbl),
    filled: !!(e.value && e.value.trim()),
    value: (e.value || '').slice(0, 80),
    combo: e.getAttribute('role') === 'combobox'
           || !!e.getAttribute('aria-autocomplete')
           || e.getAttribute('aria-haspopup') === 'listbox',
    options: e.tagName === 'SELECT'
             ? [...e.options].map(o => o.text.trim()).slice(0, 60) : [],
    // A radio's own label is the OPTION ('Yes'), never the question. Without
    // the group text there is nothing to match a rule against, so every radio
    // looks identical and none can be answered.
    group: (() => {
      if (!/^(radio|checkbox)$/.test((e.type || '').toLowerCase())) return '';
      const fs = e.closest('fieldset');
      if (fs) { const lg = fs.querySelector('legend');
                if (lg) return grab(lg.innerText).slice(0, 220); }
      const grp = e.closest('[role="group"], [role="radiogroup"]');
      if (grp) { const al = grp.getAttribute('aria-label');
                 if (al) return grab(al).slice(0, 220);
                 const lb = grp.querySelector('legend, h2, h3, p');
                 if (lb) return grab(lb.innerText).slice(0, 220); }
      // Walk up and take the SMALLEST block that still reads like one whole
      // question: it has to look like a question (? or the required *) and
      // hold only this question's own options. The old rule - first block
      // between 12 and 400 chars - grabbed whatever happened to fit, so on
      // a page of stacked Yes/No questions it returned the wrong text and a
      // rule that WOULD have matched was never tested. That, not the rules,
      // is why forms kept stalling on questions already covered.
      const mine = e.name
        ? document.querySelectorAll('input[name="' + CSS.escape(e.name) + '"]').length
        : 2;
      let n = e.parentElement, best = '';
      for (let i = 0; i < 8 && n; i++) {
        const t = grab(n.innerText);
        const radios = n.querySelectorAll('input[type=radio],input[type=checkbox]').length;
        if (radios > Math.max(mine, 2)) break;      // now spanning other questions
        if (t.length > 12 && t.length < 400 && /[?*]/.test(t)) best = t;
        n = n.parentElement;
      }
      return best.slice(0, 220);
    })(),

    // Same walk-up, but generous, and ADDITIVE - `group` above is left exactly
    // as it was so no rule that matches today starts matching something else.
    //
    // `group` requires the block to be under 400 chars and to look like a
    // question. A 762-character federal-employment question (the STOCK Act
    // one) satisfies neither, so it came back '', no rule was ever tested
    // against it, it was never banked in unanswered.jsonl, and the operator
    // was shown two bare lines reading "[radio] Yes" / "[radio] No". It was
    // the question blocking the whole application.
    //
    // The overflow break is kept, and it is what makes a generous cap safe:
    // the moment the block holds more radios than this question owns it is
    // spanning a neighbouring question, so we stop and use the last good one.
    group_long: (() => {
      if (!/^(radio|checkbox)$/.test((e.type || '').toLowerCase())) return '';
      const mine = e.name
        ? document.querySelectorAll('input[name="' + CSS.escape(e.name) + '"]').length
        : 2;
      let n = e.parentElement, best = '';
      for (let i = 0; i < 8 && n; i++) {
        const t = grab(n.innerText);
        const radios = n.querySelectorAll('input[type=radio],input[type=checkbox]').length;
        if (radios > Math.max(mine, 2)) break;
        if (t.length > 12 && t.length < 2000) best = t;
        n = n.parentElement;
      }
      return best.slice(0, 600);
    })(),

    // Whether this option is selected RIGHT NOW. Nothing in this file used to
    // read it, which is why a wrong answer the site had pre-filled from a
    // previous application was indistinguishable from an unanswered one:
    // `filled` above is meaningless for a radio, since e.value is the option
    // text ('Yes') and is always truthy.
    checked: !!e.checked,
    label: lbl.slice(0, 200)
  };
}
"""


def detect_ats(url: str) -> str:
    u = url.lower()
    if "greenhouse.io" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    if "myworkdayjobs.com" in u or "workday" in u:
        return "workday"
    if "smartapply.indeed.com" in u or "apply.indeed.com" in u:
        return "indeed_apply"
    if "indeed.com" in u:
        return "indeed"
    if "smartrecruiters.com" in u:
        return "smartrecruiters"
    if "icims.com" in u:
        return "icims"
    return "generic"


# --------------------------------------------------------------------------
# filling
# --------------------------------------------------------------------------

UNMATCHED_PATH = HERE / "unanswered.jsonl"
_seen_q: set[str] = set()


def unmatched_q(question: str, option: str) -> None:
    """
    Record a question no rule answered, with the options it offered.

    This is the to-do list for profile.json: every line is a form that stopped,
    and one answers[] entry retires it for every employer that words it the
    same way. Options accumulate per question so you can see the exact wording
    a radio needs to match.
    """
    q = re.sub(r"\s+", " ", question).strip()[:200]
    if not q:
        return
    key = q.lower()
    try:
        rows = {}
        if UNMATCHED_PATH.exists():
            for line in UNMATCHED_PATH.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    rows[r["question"].lower()] = r
                except (ValueError, KeyError):
                    pass
        rec = rows.get(key, {"question": q, "options": [], "seen": 0})
        if option and option not in rec["options"]:
            rec["options"] = (rec["options"] + [option])[:12]
        if key not in _seen_q:
            rec["seen"] = rec.get("seen", 0) + 1
            _seen_q.add(key)
        rows[key] = rec
        with UNMATCHED_PATH.open("w", encoding="utf-8") as fh:
            for r in sorted(rows.values(), key=lambda x: -x.get("seen", 0)):
                fh.write(json.dumps(r) + "\n")
    except Exception:
        pass


def radio_matches(option: str, value) -> bool:
    """
    Does this radio option mean this answer?

    Deliberately NOT pick_option. Its substring fallback is right for a
    dropdown and lethal on a radio: the value "Yes - U.S. citizen, no
    sponsorship required" contains "no", so it matched BOTH options on "Are
    you legally authorized to work?" and the later tick won - answering No on
    a real application.

    So: an outright match, or the option *begins* with the answer at a word
    boundary ("No" answers "No, I have not been employed by PwC"). A leading
    match cannot select the opposite option the way a floating substring can,
    and the boundary stops "No" from claiming "None of the above".
    """
    o = option.strip().lower()
    v = str(value).strip().lower()
    if not o or not v:
        return False
    if o == v:
        return True
    return o.startswith(v) and not o[len(v):len(v) + 1].isalnum()


def pick_option(options: list[str], value: str) -> str | None:
    v = value.strip().lower()
    for o in options:
        if o.strip().lower() == v:
            return o
    import re as _re
    for o in options:   # whole-word only: 'man' must not match 'cisgender WOman', 'male' not 'FEmale'
        if v and _re.search(r"(?<![a-z])" + _re.escape(v) + r"(?![a-z])", o.strip().lower()):
            return o
    for o in options:
        ol = o.strip().lower()
        if ol and ol in v:
            return o
    return None


def _visible_options(page, cap: int = 60) -> list[str]:
    opts = page.locator('[role="option"]:visible')
    n = min(opts.count(), cap)
    out = []
    for i in range(n):
        try:
            out.append(opts.nth(i).inner_text().strip())
        except Exception:
            break
    return out


def fill_combobox(page, handle, values) -> tuple[bool, list[str], str]:
    """
    Greenhouse/Ashby react-select. Open the menu, look at what's actually on
    offer, and only click an option that genuinely matches. A dropdown whose
    choices don't include your answer is not something to guess at - it comes
    back False with the option list so you can answer it yourself.

    Note: .fill() does NOT open a react-select menu; only real typing does.
    """
    try:
        handle.click()
        page.wait_for_timeout(350)
    except Exception:
        return False, [], ""

    # Try each acceptable answer, and for each also the part before the comma -
    # "Austin, TX" won't match the option "Austin, Texas, United States".
    candidates = []
    for v in ([values] if isinstance(values, str) else values):
        candidates.append(v)
        head = v.split(",")[0].strip()
        if head and head.lower() != v.lower():
            candidates.append(head)

    texts = _visible_options(page)
    want = next((pick_option(texts, c) for c in candidates
                 if texts and pick_option(texts, c)), None)

    if want is None:                       # long list? type to filter, retry
        for cand in candidates:
            try:
                handle.evaluate("e => { e.value = '' }")
                handle.type(cand, delay=45)
                page.wait_for_timeout(800)
            except Exception:
                return False, texts, ""
            filtered = _visible_options(page)
            if filtered:
                want = pick_option(filtered, cand) or filtered[0]
                texts = filtered
                break

    if want is None:                       # nothing fits - leave it clean
        try:
            handle.evaluate("e => { e.value = '' }")
            handle.press("Escape")
        except Exception:
            pass
        return False, texts, ""

    try:
        opts = page.locator('[role="option"]:visible')
        opts.nth(texts.index(want)).click()
        page.wait_for_timeout(300)
        return True, texts, want
    except Exception:
        return False, texts, ""


TYPEAHEAD_OPTIONS = (".dropdown-location li, .tt-suggestion, "
                     "[class*='typeahead'] li, [class*='suggestion'] li, "
                     "ul[class*='dropdown'] li")


def is_typeahead(info: dict) -> bool:
    """Lever's location box: free text, but backed by a geocoder."""
    return info["name"] == "location" or info["id"] == "location"


def _location_committed(page) -> bool:
    try:
        v = page.evaluate(
            "() => { const e = document.querySelector('[name=selectedLocation]');"
            "return e ? e.value : 'n/a'; }")
    except Exception:
        return True
    if v == "n/a":
        return True
    return bool(v) and '"name":""' not in v


def fill_typeahead(page, handle, values) -> tuple[bool, str]:
    """
    Typing text into Lever's location box is not enough - it stores the pick in
    a hidden `selectedLocation`, and submitting without it fails. Small towns
    are often absent from their geocoder ("No location found" for a small town), so work down the list until one actually commits.
    """
    for cand in ([values] if isinstance(values, str) else values):
        try:
            handle.click()
            handle.fill("")
            handle.type(cand, delay=40)
        except Exception:
            continue
        page.wait_for_timeout(1800)
        try:
            opts = page.locator(".dropdown-results > *, .dropdown-results li")
            if opts.count():
                opts.first.click(timeout=4000)
                page.wait_for_timeout(400)
        except Exception:
            pass
        if _location_committed(page):
            return True, cand
    return False, ""


def pick_resume(prof: dict, job_title: str, company: str) -> tuple[str | None, str]:
    """
    Choose a resume by what the job is, falling back to files.resume.

    Returns (path_or_None, note). `resume_by_keyword` is an ordered list of
    {match, resume}; `match` is a regex tested against "<title> <company>", so
    it reads the same way answers[] does rather than being a second, different
    matching language.

    A rule that matches but points at a missing file is reported, not silently
    swallowed - quietly sending the generic resume to the one job you tailored
    for is exactly the failure you would never notice.
    """
    files = prof.get("files", {})
    blob = f"{job_title} {company}".lower()

    for rule in files.get("resume_by_keyword", []):
        pat, path = rule.get("match"), rule.get("resume")
        if not pat or not path:
            continue
        try:
            if not re.search(pat, blob, re.I):
                continue
        except re.error:
            print(f"  ! bad regex in resume_by_keyword, ignoring: {pat}")
            continue
        p = Path(os.path.expandvars(path)).expanduser()
        if p.exists():
            return str(p), f"matched {pat!r}"
        print(f"  ! resume_by_keyword matched {pat!r} but no file at {p} "
              f"- falling back to the default resume")
        break

    path = files.get("resume")
    if not path:
        return None, ""
    p = Path(os.path.expandvars(path)).expanduser()
    return (str(p), "default") if p.exists() else (None, "")


def resolve_file(prof: dict, label: str, name: str, fid: str,
                 job_title: str = "", company: str = "") -> str | None:
    blob = f"{label} {name} {fid}".lower()
    files = prof.get("files", {})
    if re.search(r"cover", blob):
        path = files.get("cover_letter")
        if not path:
            return None
        p = Path(os.path.expandvars(path)).expanduser()
        return str(p) if p.exists() else None
    return pick_resume(prof, job_title, company)[0]


def already_offered(page, filename: str) -> bool:
    """Is this resume already listed as a choice on the page?"""
    stem = Path(filename).stem.lower()
    if not stem:
        return False
    try:
        return bool(page.evaluate(
            "s => [...document.querySelectorAll('label, [role=radio], li')]"
            ".some(e => (e.innerText||'').toLowerCase().includes(s))", stem))
    except Exception:
        return False


UI_NOISE = ("select...", "this field is required.", "locate me",
            "use up and down to choose options", "press enter to select",
            "the currently focused option", "drop file here", "attach, drop")

# A required marker ENDING a question ("... privacy notice. *"). Anchored to the
# end on purpose: Indeed leaves the required/aria-required attribute off its
# consent checkboxes and marks them only in the question text, but a '*' matched
# loosely anywhere in a long block would let an opt-in toggle that merely sits
# near a required question ("Share these answers with Indeed") be ticked as
# though the form demanded it. That is consenting on your behalf to something
# nobody asked for.
REQUIRED_MARK = re.compile(r"[*✱＊]\s*$")


def repair_value(handle, want: str, got: str) -> str:
    """
    Rewrite a box whose read-back doesn't match what we asked for, and return
    what it holds afterwards.

    The case this exists for: Chrome autofills any field carrying an
    `autocomplete` hint the moment it takes focus. On Greenhouse's first_name
    (`autocomplete="given-name"`) Chrome's "the user" and .fill()'s "the user"
    both land and the box reads "NameName" - reproduced on all five
    Greenhouse forms in the 08-02 run, and on 07-31 before that.

    .fill() can't win that race because it types. Assigning through React's own
    value setter does: it REPLACES rather than inserts, and dispatching input +
    change afterwards keeps React's internal state in step with the DOM. Writing
    `e.value` directly would desync the two, which is the same mistake that
    makes fill_combobox() double when it clears with `e.value = ''`.

    Returns the post-repair value, which the caller reports. A field that still
    disagrees is reported as-is rather than claimed - being told a form is ready
    when a name is wrong is the failure this whole read-back path exists to stop.
    """
    try:
        handle.evaluate(
            """(e, v) => {
                const proto = e instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const set = Object.getOwnPropertyDescriptor(proto, 'value').set;
                set.call(e, v);
                e.dispatchEvent(new Event('input',  {bubbles: true}));
                e.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            want,
        )
        return (handle.evaluate("e => (e.value || '').trim()") or "")
    except Exception:
        return got


def norm_label(label: str) -> str:
    """
    Collapse a field's label to something stable enough to match its twin.
    Greenhouse labels end at the required marker; everything after it is
    widget chrome ('Select...', 'Locate me', screen-reader hints).
    """
    text = label.strip().lower()
    # Greenhouse marks required with '*', Lever with '✱'.
    star = min((i for i in (text.find(c) for c in "*✱＊") if i != -1),
               default=-1)
    if star != -1:
        text = text[: star + 1]
    for noise in UI_NOISE:
        text = text.replace(noise, " ")
    return re.sub(r"\s+", " ", text).strip()[:60]


def collect_fields(page) -> list:
    """(handle, info) for every field worth considering, twins removed."""
    rows = []
    for h in page.query_selector_all("input, textarea, select"):
        try:
            info = h.evaluate(FIELD_INFO_JS)
        except Exception:
            continue
        if info["type"] in ("hidden", "submit", "button", "image", "search"):
            continue
        if SKIP_FIELD_IDS.search(info["name"]) or SKIP_FIELD_IDS.search(info["id"]):
            continue
        # react-select's hidden validation twin - it only accepts a value the
        # widget itself writes, so touching it errors and proves nothing.
        if MIRROR_INPUT.search(info.get("cls", "")) and not info["combo"]:
            continue
        rows.append((h, info))

    # A react-select renders a visible combobox plus a required mirror input
    # carrying the same label. Keep the combobox: writing to the mirror looks
    # like it worked and leaves the form in a state the site rejects.
    best: dict[tuple, tuple] = {}
    for h, info in rows:
        if info["type"] == "file":
            key = ("file", info["name"] or info["id"])
        elif info["type"] in ("radio", "checkbox"):
            # A radio's label is its OPTION, and every question on the page has
            # a "Yes" and a "No". Keying on the label alone collapsed all of
            # them into one pair, so a page of six questions presented two
            # radios and the rest were invisible to filling AND to the report.
            # The question has to be part of the identity.
            # group_long as the fallback matters: with an empty group the key
            # collapsed to (option, name, type), so two long questions sharing
            # an input name kept only one of their Yes/No pairs and the other
            # vanished from filling AND from the report.
            key = (norm_label(info.get("group", "") or info.get("group_long", "")),
                   info["label"], info["name"], info["type"])
        else:
            key = (norm_label(info["label"]) or info["name"] or info["id"], info["type"])
        prev = best.get(key)
        if prev is None:
            best[key] = (h, info)
            continue
        pinfo = prev[1]
        better = (info["combo"], info["visible"]) > (pinfo["combo"], pinfo["visible"])
        if better:
            best[key] = (h, info)
    return list(best.values())


def autofill_page(page, prof: dict, rules, skips, dry: bool,
                  n_explicit: int = 0, job_title: str = "",
                  company: str = "") -> dict:
    filled, skipped, needs_you, missing = [], [], [], []
    blockers, consent, consent_policy = [], [], []
    answered_groups: set[str] = set()          # radio questions already decided

    def stuck(text: str, required: bool) -> None:
        """Report it - and if the form marks it required, it blocks submit."""
        needs_you.append(text)
        if required:
            blockers.append(text)

    html = page.content()
    has_captcha = any(m in html for m in CAPTCHA_MARKERS)

    fields = collect_fields(page)

    # How many checkboxes share each question? More than one means a
    # multi-SELECT ("which of these have you used?"), where required-ness says
    # the form wants an answer and says nothing about which option is true.
    # Those are never auto-ticked in any mode - see tick_consent().
    cb_group_size: dict[str, int] = {}
    for _h, _i in fields:
        if _i["type"] != "checkbox":
            continue
        _k = norm_label(_i.get("group", "") or _i.get("group_long", "") or "")
        if _k:
            cb_group_size[_k] = cb_group_size.get(_k, 0) + 1

    for h, info in fields:
        ftype, name, fid = info["type"], info["name"], info["id"]
        label = info["label"]
        ident = f"{label or name or fid or ftype}"[:70]

        if ftype == "file":
            path = resolve_file(prof, label, name, fid, job_title, company)
            # Indeed keeps every resume you send it. Once this file is already
            # an option on the page there is nothing to gain by uploading it
            # again, and doing so piles duplicates into the account.
            if path and already_offered(page, Path(path).name):
                skipped.append(f"{ident}  (already on file with Indeed)")
                continue
            if not path:
                stuck(f"{ident}  (no file configured)", info["required"])
                continue
            if dry:
                filled.append(f"{ident}  <- {Path(path).name}")
            else:
                try:
                    h.set_input_files(path)
                    filled.append(f"{ident}  <- {Path(path).name}")
                except Exception as exc:
                    stuck(f"{ident}  (upload failed: {exc})", info["required"])
            continue

        if not info["visible"] or info["disabled"]:
            continue

        blob = f"{label} {name} {fid}"
        # An explicit answers[] entry is a deliberate decision by you, so it
        # outranks the skip list - that's how salary gets answered when you
        # actually want it answered.
        explicit = any(pat.search(blob) for pat, _ in rules[:n_explicit])
        if not explicit and any(s.search(blob) for s in skips):
            skipped.append(ident)
            continue
        if IF_DIFFERENT.search(label):
            stuck(f"{ident}  (conditional - only you know)", info["required"])
            continue

        if ftype in ("checkbox", "radio"):
            # Yours to tick by default. The one exception is a radio whose
            # QUESTION matches a rule you wrote yourself in answers[] - the
            # same principle that lets salary through the skip list. Nothing
            # is inferred: a legal attestation or a consent box matches no
            # rule, so it stays untouched and gets reported.
            # The question text. `group` is the tight reading and stays first;
            # `group_long` only ever fills in where `group` came back empty,
            # which is exactly the long legal/attestation questions that no
            # rule could previously even be tested against.
            group = info.get("group", "") or info.get("group_long", "")
            answered = False
            if ftype == "radio" and group:
                # One question, one answer. This loop visits each radio OPTION
                # separately, so once a rule has ticked an option, a later rule
                # could tick a DIFFERENT option of the same question - last
                # writer wins, and which one that is depends on DOM order.
                # Measured on a live Nymbus form: "Are you authorized to work in
                # the United States without current or future visa sponsorship?"
                # matched the work-authorization rule on the "Yes" option, then
                # the sponsorship rule on the "No" option, and the form was left
                # reading No - the opposite of the truth, on a required
                # question. Only guard_before_advance put it back, and that
                # guard is a backstop, not a licence to leave this wrong.
                gkey = norm_label(group)
                if gkey in answered_groups:
                    continue
                for pat, vals in rules[:n_explicit]:
                    if not pat.search(group):
                        continue
                    if any(radio_matches(label, v) for v in vals):
                        if not dry:
                            try:
                                h.check()
                            except Exception:
                                break
                        filled.append(f"[radio] {group[:52]}  <- {label[:34]}")
                        answered = True
                        answered_groups.add(gkey)
                        break
                    # No break on a miss. Stopping at the FIRST rule whose
                    # pattern hit the question meant "would you consider
                    # relocating?" was claimed by the relocation rule, whose
                    # value ("Seeking fully remote roles") matches no radio -
                    # and the question could then never be answered by any
                    # later, better-fitting rule.
            if answered:
                continue
            needs_you.append(f"[{ftype}] {ident}")
            # Bank the question so the rule set converges instead of you
            # rediscovering the same stopper on the next employer's form.
            if group:
                unmatched_q(group, label)
            # Indeed marks a required consent box with '*' on the QUESTION and
            # leaves the input itself without required/aria-required, so the DOM
            # flag alone missed "You declare that you have read and understand
            # the privacy notice" - and every ServiceNow application stopped on
            # that step with the box untouched and nothing on screen explaining
            # which control was blocking.
            # TWO LISTS, deliberately, and they are not interchangeable.
            #
            # consent[]        - every checkbox this form marks required. Ticked
            #                    only under --auto-submit, which is gated by
            #                    why_not_submittable() first.
            # consent_policy[] - boxes matching consent_policy.auto_tick in
            #                    profile.json. Ticked in --fill-only too.
            #
            # Merging them was a real defect (WelbeHealth, 2026-08-08): a
            # REQUIRED multi-select marks every one of its options required, so
            # "tick everything required" ticked all 36 skill boxes - Pinecone,
            # Ollama, Milvus and "None of the above" all at once, which is both
            # false and self-contradictory. Required-ness says the form needs an
            # answer; it never says WHICH option is true. Only a pattern the
            # operator wrote himself may tick a box outside auto-submit.
            if ftype == "checkbox":
                # profile.json `checkbox_answers`: [{"group": regex, "tick": [labels]}].
                # Lets a multi-select question ("which countries do you anticipate
                # working in?") tick ONLY the options he named. Nothing else in a
                # group is ever ticked, and a group no entry names stays his.
                _hit = next((e for e in prof.get("checkbox_answers", [])
                             if group and re.search(e.get("group", "^$"), group, re.I)), None)
                if _hit is not None:
                    if any(radio_matches(label, v) for v in _hit.get("tick", [])):
                        if not dry:
                            try:
                                h.check()
                            except Exception:
                                pass
                        filled.append(f"[checkbox] {group[:52]}  <- {label[:34]}")
                    continue
                multi = cb_group_size.get(norm_label(group), 0) > 1
                if consent_policy_match(prof, group, ident) and not multi:
                    consent_policy.append((h, ident))
                elif (info["required"] or REQUIRED_MARK.search(group or "")) and not multi:
                    consent.append((h, ident))
            continue

        # Kept narrow to tel on purpose: on any other field a short numeric
        # value is plausibly a real answer, and overwriting one you typed
        # yourself would be worse than leaving a dial code in place.
        scaffold = (info["type"] == "tel"
                    and bool(PREFILL_ONLY.match(info.get("value", "").strip())))
        if info["filled"] and not scaffold:
            continue

        values = None
        for pat, vals in rules:
            if pat.search(blob):
                values = vals
                break
        if values is None:
            if info["required"]:
                missing.append(ident)
            continue
        value = values[0]

        if dry:
            filled.append(f"{ident}  <- {value[:60]}")
            continue

        try:
            if info["tag"] == "select":
                choice = next((c for c in
                               (pick_option(info["options"], v) for v in values)
                               if c), None)
                if choice is None:
                    stuck(f"[select] {ident}  (none of {values} on offer)",
                          info["required"])
                    continue
                h.select_option(label=choice)
                filled.append(f"{ident}  <- {choice}")
            elif info["combo"]:
                ok, offered, chosen = fill_combobox(page, h, values)
                if ok:
                    filled.append(f"{ident}  <- {chosen[:60]}")
                else:
                    hint = f"  offers: {', '.join(offered[:6])}" if offered else ""
                    stuck(f"[dropdown] {ident}{hint}", info["required"])
            elif is_typeahead(info):
                ok, used = fill_typeahead(page, h, values)
                if ok:
                    filled.append(f"{ident}  <- {used[:60]}")
                else:
                    stuck(f"{ident}  (typed, but pick the suggestion yourself)",
                          info["required"])
            else:
                h.fill(value)
                # React-controlled inputs can quietly revert or reformat what
                # you write. Reporting 'filled' without looking is how a box
                # ends up empty on a form you were told was ready to send, so
                # read it back and report what's actually in there.
                try:
                    got = (h.evaluate("e => (e.value || '').trim()") or "")
                except Exception:
                    got = value
                # Chrome's own autofill races us on any field carrying an
                # autocomplete hint (`given-name` on every Greenhouse
                # first_name). Chrome fills "the user" on focus, .fill()'s
                # insertText lands after it, and the box ends up
                # "NameName" - on all 5 Greenhouse forms, every run.
                # The old read-back only asked "is it empty?", so a doubled
                # name was reported as filled and went out that way.
                if got and got != value and info["type"] != "tel":
                    got = repair_value(h, value, got)
                if not got or (info["type"] == "tel" and PREFILL_ONLY.match(got)):
                    stuck(f"{ident}  (typed, but the field didn't keep it)",
                          info["required"])
                elif got != value and info["type"] != "tel" and not info["combo"]:
                    # Reformatting by a date/currency widget is fine and common.
                    # Say what is actually in the box either way - the operator
                    # decides whether the difference matters.
                    filled.append(f"{ident}  <- {got[:60]}   (form rewrote it)")
                else:
                    filled.append(f"{ident}  <- {got[:60]}")
        except Exception as exc:
            stuck(f"{ident}  ({type(exc).__name__})", info["required"])

    return {
        "filled": filled,
        "skipped": skipped,
        "needs_you": needs_you,
        "missing": missing,
        "blockers": blockers,
        "consent": consent,
        "consent_policy": consent_policy,
        "captcha": has_captcha,
    }


# --------------------------------------------------------------------------
# auto-submit
# --------------------------------------------------------------------------

SUBMIT_SELECTORS = (
    "button#submit_app", "button[type=submit]", "input[type=submit]",
    "button:has-text('Submit application')", "button:has-text('Submit Application')",
    "button:has-text('Submit')", "a:has-text('Submit application')",
)

SUCCESS_MARKERS = re.compile(
    r"thank you|thanks for applying|application (was )?(received|submitted|"
    r"sent)|successfully (applied|submitted)|we('ve| have) received", re.I)


def why_not_submittable(res: dict) -> list[str]:
    """Every reason this form must not be auto-submitted. Empty == good to go."""
    reasons = []
    # A page where nothing matched is not an application form - a closed
    # posting 404s with zero inputs, and every other check passes vacuously.
    if not res["filled"]:
        reasons.append("nothing filled - this doesn't look like an application form")
    if res["captcha"]:
        reasons.append("CAPTCHA present")
    if res["missing"]:
        reasons.append(f"{len(res['missing'])} required question(s) unanswered")
    if res["blockers"]:
        reasons.append(f"{len(res['blockers'])} required field(s) unresolved")
    return reasons


def auto_submit(page, res: dict) -> tuple[bool, str]:
    """
    Tick the checkboxes the form *requires* to proceed, then submit. Only ever
    called once why_not_submittable() comes back empty, so nothing here is
    papering over a question we failed to answer.
    """
    ticked = []
    for handle, ident in list(res["consent"]) + list(res.get("consent_policy", [])):
        try:
            handle.check()
            ticked.append(ident)
        except Exception:
            return False, f"could not tick required checkbox: {ident}"
    if ticked:
        print(f"    ticked required: {'; '.join(t[:50] for t in ticked)}")

    btn = None
    for sel in SUBMIT_SELECTORS:
        try:
            loc = page.locator(sel).filter(visible=True)
            if loc.count():
                btn = loc.first
                break
        except Exception:
            continue
    if btn is None:
        return False, "no submit button found"

    before = page.url
    try:
        btn.click()
    except Exception as exc:
        return False, f"submit click failed: {type(exc).__name__}"

    page.wait_for_timeout(5000)
    try:
        body = page.inner_text("body")[:6000]
    except Exception:
        body = ""
    if SUCCESS_MARKERS.search(body):
        return True, "confirmed"
    if page.url != before:
        return True, f"navigated to {page.url[:60]}"
    return False, "clicked, but no confirmation seen - check it yourself"


# --------------------------------------------------------------------------
# Indeed SmartApply - the multi-step wizard
# --------------------------------------------------------------------------
#
# Everything above assumes one page judged once. Indeed's own apply flow is a
# stepper, so it gets its own path rather than more branches in run_one().
#
# The stepper fills each step, advances, and stops at the review screen for you
# to read and send. Indeed's send control is a distinct testid from the advance
# button below, and every advance candidate - on the testid path and the label
# path alike - must fail the send deny-list before it is clicked.
#
# It submits ONLY if you passed --auto-submit, and only after the gate at the
# review branch passes. (An earlier version of this comment claimed
# --auto-submit could not reach this code at all. That was false: main() ->
# run_one() -> run_stepper(auto=...) has always carried it. The behaviour was
# right, the comment was not, and a false claim in a safety comment is worse
# than no comment - somebody relaxes a real check on the strength of it.
# gui.py omits the argument, so the GUI genuinely never submits.)

SHOTS = HERE / "run-shots"
STEP_CONTINUE = '[data-testid="continue-button"]'
SEND_LABEL = re.compile(r"submit|send application|apply now", re.I)
# Exact labels that mean "next step". An allow-list, because the fallback
# below has to pick a button by its words alone.
# "Review your application" is Indeed's label on the last step before the
# review screen. It was missing here, so the demographic step - the final one
# on every ServiceNow posting - ended as "no continue button" and the whole
# application stopped one click short of review. It is an ADVANCE, not a send:
# it opens review-module, where the gate below decides about submitting.
ADVANCE_LABEL = re.compile(
    r"^(continue|next|save and continue|review details|review your application)$", re.I)
MAX_STEPS = 12


def find_advance(page):
    """
    (locator, label) for the control that moves to the next step, or (None,'').

    The questions module randomises data-testid and class per render - its
    Continue button came back as data-testid="30e6dd3d81aa4705..." - so the
    stable testid alone silently finds nothing there and the walk stops one
    step in. The fallback picks by label, and a candidate must BOTH match the
    advance allow-list AND fail the send deny-list before it is ever clicked.
    """
    loc = page.locator(STEP_CONTINUE)
    if loc.count():
        try:
            label = (loc.first.inner_text() or "").strip()
        except Exception:
            label = ""
        return loc.first, label

    btns = page.locator("button:visible")
    for i in range(min(btns.count(), 40)):
        b = btns.nth(i)
        try:
            label = (b.inner_text() or "").strip()
        except Exception:
            continue
        if ADVANCE_LABEL.match(label) and not SEND_LABEL.search(label):
            return b, label
    return None, ""

# Indeed runs an invisible reCAPTCHA v3 on every step, so res["captcha"] is
# true throughout and means nothing here - reporting it each step would train
# you to ignore the warning on the forms where it is real.


def step_slug(url: str) -> str:
    return (url.split("/form/")[-1].split("?")[0] or "?")[:48]


VALIDATION_ERRORS = r"""() => {
  const g = t => (t || '').replace(/[\s ]+/g, ' ').trim();
  const out = [];
  const rx = /choose an option|select an option|required|please (answer|select|enter)|this field/i;
  for (const e of document.querySelectorAll('*')) {
    if (e.children.length) continue;                 // leaf nodes only
    const t = g(e.innerText);
    if (!t || t.length > 90 || !rx.test(t)) continue;
    // Walk up to the question this error belongs to.
    let n = e, q = '';
    for (let i = 0; i < 6 && n; i++) {
      const bloc = g(n.innerText);
      if (bloc.length > t.length + 12 && bloc.length < 420) { q = bloc; break; }
      n = n.parentElement;
    }
    q = q.replace(t, ' ').replace(/\s+/g, ' ').trim();
    if (q && !out.some(o => o.question === q)) out.push({ question: q.slice(0, 180), error: t });
  }
  return out;
}"""


def read_validation(page) -> list[dict]:
    """
    What the form is complaining about, in its own words.

    The page says exactly which questions it is waiting on ('Choose an option
    to continue'). Clicking Continue, noticing nothing moved, and dumping every
    control on screen throws that away and makes the user find it themselves.
    """
    try:
        return page.evaluate(VALIDATION_ERRORS)
    except Exception:
        return []


def field_signature(page) -> str:
    """
    Fingerprint of the controls on screen.

    Some steps swap their contents without changing the URL, so 'did the URL
    change' alone can't tell a legitimate next step from a step that refused
    to advance. Reporting one as the other is how you'd believe a form was
    ready when validation had actually rejected it.
    """
    try:
        return page.evaluate(
            "() => [...document.querySelectorAll('input,textarea,select')]"
            ".filter(e => e.getClientRects().length)"
            ".map(e => e.getAttribute('data-testid')||e.name||e.id||e.type).join('|')")
    except Exception:
        return ""


def enter_smartapply(page, ctx):
    """From an indeed.com/viewjob page, click through to the wizard."""
    btn = page.locator("#indeedApplyButton").first
    if not btn.count():
        btn = page.locator("button", has_text="Apply with Indeed").first
    if not btn.count():
        return None
    try:
        with ctx.expect_page(timeout=8_000) as pi:
            btn.click()
        ap = pi.value
        ap.wait_for_load_state("domcontentloaded", timeout=30_000)
    except Exception:
        ap = page                       # same-tab navigation
    # Indeed opens the wizard in a tab of its own, which restores the window.
    keep_minimized(ap)
    ap.wait_for_timeout(4000)
    return ap if "smartapply.indeed.com" in ap.url else None


# The send control. Only ever used by the --auto-submit branch below, which
# is unreachable unless you pass that flag; every other path stops at review.
STEP_SUBMIT = '[data-testid="submit-application-button"]'


def find_in_frames(page, selector: str, need_enabled: bool = False):
    """
    First VISIBLE match for `selector` in any frame, or None.

    Frame-aware because Indeed splits the review screen: the card (name, resume,
    the answers about to be sent) renders in an `about:srcdoc` iframe while the
    send control stays in the main document. A helper that reads one frame sees
    half the page.

    `need_enabled` is off by default on purpose - see wait_enabled below. A
    disabled control is still the control; refusing to return it is how the
    caller ends up reporting "no submit button" about a button plainly on screen.
    """
    for fr in page.frames:
        try:
            loc = fr.locator(selector)
            n = min(loc.count(), 5)
        except Exception:
            continue
        for i in range(n):
            cand = loc.nth(i)
            try:
                if cand.is_visible() and (not need_enabled or cand.is_enabled()):
                    return cand
            except Exception:
                continue
    return None


def wait_enabled(page, btn, seconds: int = 45) -> bool:
    """
    Poll until the control is actually pressable.

    Indeed ships the send button DISABLED and enables it once its background
    checks settle - the invisible reCAPTCHA v3 among them. Measured on a live
    review screen: data-testid="submit-application-button", visible=True,
    enabled=False on arrival.

    This is the whole reason submitting was erratic. The original code clicked
    straight away and leaned on Playwright's actionability wait, which enabled
    in time on one job and timed out on five - reported as "submit click
    failed", which sounds like a broken selector and is really a race. Waiting
    for the state we need, and saying so when it never arrives, turns an
    intermittent failure into a determinate one.
    """
    for _ in range(max(1, seconds * 2)):
        try:
            if btn.is_enabled():
                return True
        except Exception:
            return False
        page.wait_for_timeout(500)
    return False


def submit_stepper(page) -> tuple[bool, str]:
    """Press send on the review screen. Only called once the gate passes."""
    btn = find_in_frames(page, STEP_SUBMIT)
    if btn is None:
        btn = find_in_frames(
            page, "button:has-text('Submit your application')")
    if btn is None:
        btn = find_in_frames(page, "button:has-text('Submit application')")
    if btn is None:
        return False, "no submit button on the review screen"
    if not wait_enabled(page, btn):
        return False, ("the submit button was still disabled after 45s - "
                       "Indeed had not finished its own checks")
    try:
        btn.click(timeout=15_000)
    except Exception as exc:
        return False, f"submit click failed: {type(exc).__name__}"
    page.wait_for_timeout(6000)
    body = all_frames_text(page, 8000)
    if re.search(r"application submitted|your application (has been |was )?sent|"
                 r"thanks for applying|applied on", body, re.I):
        return True, "confirmed"
    if "post-apply" in page.url or "application-submitted" in page.url:
        return True, f"landed on {page.url[:60]}"
    return False, "clicked, but no confirmation seen - check it yourself"


# --------------------------------------------------------------------------
# the vision layer - look at the page, don't just query it
# --------------------------------------------------------------------------
#
# The rule layer answers "did a rule fire". That is not the same question as
# "is the right answer on the screen", and one ServiceNow form showed the gap
# in both directions at once: a 762-character federal-employment question with
# NEITHER option selected (the actual blocker, reported as "[radio] Yes"), and
# a state-government question Indeed had PRE-FILLED "Yes" from an unrelated
# previous application - false for this user, and invisible to code that only
# ever asks whether something is selected.
#
# So each step is now: fill with rules -> photograph -> read the photograph ->
# fix what the rules could not -> photograph again -> only then advance.
#
# What this layer may do is deliberately tiny. It returns TEXT. It picks a
# radio option only by handing that text to radio_matches(), the same gate a
# rule goes through. It cannot click a button, cannot name a selector, cannot
# reach find_advance's allow-list or the send deny-list, and cannot make a form
# submittable - the --auto-submit gate below only ever gets HARDER because of
# it. If it is unsure the answer is UNKNOWN and the question is left alone and
# reported, which is the whole point.

VISION_PANES = 6                 # a step taller than ~6 screens is not a form
VISION_OVERLAP = 0.82            # scroll by this much of a viewport per pane


def safe_name(text: str, cap: int = 18) -> str:
    """A filename Windows will actually accept. ':' in a job title used to
    raise inside the screenshot try/except and produce no shot and no warning."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", text or "").strip()[:cap].strip() or "job"


def _qkey(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())[:120].strip()


_Q_MATCH = 0.72          # good enough to be the same question
_Q_MARGIN = 0.06         # ...and clearly better than the runner-up


def _match_row(qtext: str, rows: list) -> dict | None:
    """
    Line up the DOM's reading of a question with the model's, or None.

    Whole-string similarity, NOT a shared prefix. Two things break a prefix
    test on real forms, and both were observed on one page:

      * the model drops a leading enumerator, transcribing "B. Are you a
        current employee..." as "Are you a current employee..." - the DOM text
        and the model text then share no prefix at all, the verdict is silently
        dropped, and the question is reported as one the DOM never offered;
      * "A. Within the past 5 years, have you ever been an employee of..." and
        "C. Within the past 5 years, have you ever been an employee of..." are
        identical for their first FORTY characters. A prefix test happily joins
        those two, which would file one question's answer against the other -
        on this form, the federal one against the state one.

    Requiring the best candidate to also beat the runner-up means an ambiguous
    reading matches nothing rather than matching the wrong question.
    """
    ka = _qkey(qtext)
    if not ka or not rows:
        return None
    scored = []
    for r in rows:
        kb = _qkey(str(r.get("question", "")) if isinstance(r, dict) else str(r))
        if kb:
            scored.append((difflib.SequenceMatcher(None, ka, kb).ratio(), r))
    if not scored:
        return None
    scored.sort(key=lambda s: -s[0])
    best = scored[0][0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best >= _Q_MATCH and best - second >= _Q_MARGIN:
        return scored[0][1]
    return None


def question_map(page, rules, n_explicit) -> list[dict]:
    """
    One entry per radio QUESTION on the page - not per option.

    Each carries what the rules intended, what is selected right now, and live
    handles for the options. `intended` is computed exactly the way the rule
    path computes it (explicit answers[] only, radio_matches on the value), so
    "the rules meant X" and "the rules did X" cannot drift apart.
    """
    qs: dict[str, dict] = {}
    for h, info in collect_fields(page):
        if info["type"] != "radio" or not info["visible"] or info["disabled"]:
            continue
        qtext = (info.get("group") or info.get("group_long") or "").strip()
        if not qtext:
            continue
        key = norm_label(qtext)
        q = qs.setdefault(key, {"question": qtext, "options": [], "handles": [],
                                "selected": "", "intended": "", "explicit": False})
        if len(qtext) > len(q["question"]):
            q["question"] = qtext
        if info["label"] and info["label"] not in q["options"]:
            q["options"].append(info["label"])
        q["handles"].append((info["label"], h))
        if info.get("checked") and info["label"]:
            q["selected"] = info["label"]

    for q in qs.values():
        for pat, vals in rules[:n_explicit]:
            if not pat.search(q["question"]):
                continue
            q["explicit"] = True                # an answers[] entry addresses this
            hit = next((o for o in q["options"]
                        if any(radio_matches(o, v) for v in vals)), None)
            if hit:
                q["intended"] = hit
                break                           # no break on a miss - a later
                                                # rule may still fit, same as
                                                # the rule path
    return list(qs.values())


def _span_in_viewport(handles: list) -> tuple[float, float]:
    """Top and bottom of a question in VIEWPORT coordinates, recomputed after
    every scroll. Used to send the model only the questions it can actually
    see - a hint listing questions that are off-image invites it to answer
    from the hint instead of from the pixels."""
    top, bot = 1e9, -1e9
    for _, h in handles:
        try:
            r = h.evaluate("e => { const b = e.getBoundingClientRect();"
                           " return [b.top, b.bottom]; }")
        except Exception:
            continue
        top, bot = min(top, r[0]), max(bot, r[1])
    return top, bot


def _shoot(page, path: Path) -> bytes:
    """
    One viewport, at CSS scale.

    full_page=True produced 2134x3589 images: the API downsamples anything that
    big anyway and bills it at nearly the per-image maximum, so it was the most
    expensive way to send a picture that had already been shrunk. A viewport at
    CSS pixels reads identically for about a fifth of the tokens, and it is the
    right unit for a scroll-and-look loop regardless of cost.
    """
    png = page.screenshot(scale="css")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
    except Exception:
        pass
    return png


def _apply_vision(q: dict, answer: str, dry: bool) -> tuple[bool, str]:
    """
    Tick the option named - through radio_matches, never around it.

    The caller hands over TEXT, whether it came from a rule or from the model.
    If that text does not resolve to exactly one option on this question,
    nothing is ticked and it is reported: a paraphrase ("No, I have not")
    against an option printed "NO" must fail loudly here rather than half-match
    something else. This is the same gate that stops "Yes - U.S. citizen, no
    sponsorship required" from selecting "No".
    """
    hits = [o for o in q["options"] if radio_matches(o, answer)]
    if len(hits) != 1:
        return False, (f"answer {answer[:40]!r} is not one of "
                       f"{'; '.join(o[:24] for o in q['options'][:4])}")
    want = hits[0]
    if q["selected"] and radio_matches(q["selected"], want):
        return False, ""                        # already right, nothing to do
    if dry:
        q["selected"] = want
        return True, ""
    handle = next((h for lab, h in q["handles"] if lab == want), None)
    if handle is None:
        return False, "the option went away between reading and clicking"
    try:
        handle.check()
    except Exception as exc:
        return False, f"could not select it ({type(exc).__name__})"
    try:
        if not handle.evaluate("e => !!e.checked"):
            return False, "clicked, but the option did not take"
    except Exception:
        pass
    q["selected"] = want
    return True, ""


def vision_step(page, prof, rules, skips, n_explicit, dry, shot_stem) -> dict:
    """
    Screenshot -> verify -> fix -> screenshot -> re-verify, one viewport at a
    time. Returns plain strings only: this dict crosses a thread boundary into
    the GUI, where a Playwright handle would break the post.
    """
    out = {"changed": [], "flags": [], "seen": 0, "panes": 0, "note": ""}
    if vision_answer is None:
        out["note"] = "vision OFF (vision_answer.py not importable)"
        return out
    # Settle availability BEFORE walking the page. VISION_AVAILABLE starts
    # optimistic and only drops on the first attempt, so checking it alone let
    # a keyless run walk the whole form, get [] back from every call, and
    # report every question as "unverified" - which then held back
    # --auto-submit for a reason that was really just "vision is switched off".
    # get_client() resolves the key and the import without touching the network.
    vision_answer.get_client()
    if not vision_answer.VISION_AVAILABLE:
        out["note"] = vision_answer.status()
        return out

    try:
        vh, total = page.evaluate(
            "() => [window.innerHeight, Math.max(document.body.scrollHeight,"
            " document.documentElement.scrollHeight)]")
    except Exception:
        vh, total = 900, 900
    vh = max(int(vh or 900), 200)

    handled: set[str] = set()
    y = 0
    for pane in range(VISION_PANES):
        try:
            page.evaluate("y => window.scrollTo(0, y)", y)
            page.wait_for_timeout(450)
        except Exception:
            pass

        on_page = question_map(page, rules, n_explicit)
        qs = [q for q in on_page if norm_label(q["question"]) not in handled]
        here = []
        for q in qs:
            top, bot = _span_in_viewport(q["handles"])
            if bot > 4 and top < vh - 4:                 # any part of it visible
                here.append(q)
        if here:
            out["panes"] += 1
            out["seen"] += len(here)
            png = _shoot(page, SHOTS / f"{shot_stem}-p{pane + 1}.png")
            hint = [{"question": q["question"], "options": q["options"],
                     "selected": q["selected"], "intended": q["intended"]}
                    for q in here]

            changed_before = len(out["changed"])
            verdicts = vision_answer.verify_screenshot(png, hint, prof)
            # A key revoked, a budget spent or a network dropped mid-run: stop
            # here rather than marking the rest of the form unverified because
            # nobody was left to verify it.
            if not vision_answer.VISION_AVAILABLE:
                out["seen"] -= len(here)
                out["panes"] -= 1
                break
            _pane_pass(page, here, verdicts, prof, rules, skips, n_explicit,
                       dry, png, out)

            for q in here:
                handled.add(norm_label(q["question"]))

            # A question the model reads off the screen that the DOM never
            # offered is worth knowing about: an unrendered or oddly-marked-up
            # question is missed by filling AND by the report, and then the
            # step fails validation for a reason nothing on screen explains.
            # Only blank ones are raised - those are the ones that block.
            # Checked against every question on the PAGE, not just this pane: a
            # long question's text can sit in one screenful and its options in
            # the next, so the model legitimately reports a question this pane
            # was not filling. Only a question no part of the DOM offered is
            # worth raising.
            known = [{"question": q["question"]} for q in on_page]
            for r in verdicts:
                rq = str(r.get("question", "")).strip()
                if (rq and not r.get("on_screen")
                        and _match_row(rq, known) is None):
                    out["flags"].append(
                        f"{rq[:64]} - visible on screen with nothing selected, but "
                        f"not among the fields the DOM offered")

            # Photograph it again and read it back. "I clicked the right thing"
            # is a promise; a second look is evidence, and it is the only way to
            # catch a control that reverted or a click that landed on nothing.
            if changed_before != len(out["changed"]) and not dry:
                page.wait_for_timeout(500)
                png2 = _shoot(page, SHOTS / f"{shot_stem}-p{pane + 1}-after.png")
                fresh = {norm_label(q["question"]): q
                         for q in question_map(page, rules, n_explicit)}
                after = []
                for q in here:
                    want = q.get("want") or ""
                    if not want:
                        continue
                    got = (fresh.get(norm_label(q["question"])) or {}).get("selected", "")
                    if not (got and radio_matches(got, want)):
                        out["flags"].append(
                            f"{re.sub(r'[ ]+', ' ', q['question'])[:64]} - after fixing, "
                            f"the page holds {(got or '(blank)')[:30]!r}, not {want[:30]!r}")
                    after.append({"question": q["question"], "options": q["options"],
                                  "selected": got, "intended": want})
                # And check the picture agrees with the DOM about it, because a
                # control can read as set in the DOM while the page shows the
                # user something else entirely.
                for r in vision_answer.verify_screenshot(png2, after, prof):
                    want = r.get("intended") or ""
                    if want and not r.get("on_screen"):
                        out["flags"].append(
                            f"{str(r.get('question', ''))[:64]} - the DOM says "
                            f"{want[:24]!r} but the screenshot shows nothing selected")

        y += int(vh * VISION_OVERLAP)
        if y >= total - vh * 0.2:
            break

    # Anything the pane walk never got to, usually because the page grew or
    # relaid out while we were reading it. Bring it into view by hand rather
    # than leaving it silently unverified - unverified is the state this whole
    # layer exists to eliminate.
    for _ in range(2):
        left = [q for q in question_map(page, rules, n_explicit)
                if norm_label(q["question"]) not in handled]
        if not left:
            break
        try:
            left[0]["handles"][0][1].scroll_into_view_if_needed(timeout=4000)
            page.wait_for_timeout(400)
        except Exception:
            break
        here = []
        for q in left:
            top, bot = _span_in_viewport(q["handles"])
            if bot > 4 and top < vh - 4:
                here.append(q)
        if not here:
            break
        out["panes"] += 1
        out["seen"] += len(here)
        png = _shoot(page, SHOTS / f"{shot_stem}-sweep{out['panes']}.png")
        hint = [{"question": q["question"], "options": q["options"],
                 "selected": q["selected"], "intended": q["intended"]} for q in here]
        _pane_pass(page, here, vision_answer.verify_screenshot(png, hint, prof),
                   prof, rules, skips, n_explicit, dry, png, out)
        for q in here:
            handled.add(norm_label(q["question"]))

    try:
        page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass
    out["note"] = vision_answer.status()
    return out


def _pane_pass(page, here, verdicts, prof, rules, skips, n_explicit, dry,
               png, out) -> None:
    """
    Decide and fix one screenful. The ordering here is the safety model:

    1. The DOM is authoritative for WHAT IS TICKED. `e.checked` is a fact; a
       model reading a picture of a radio is a judgement, and on one run it
       reported an answer selected on a question where nothing was. Believing
       that reading would have re-created the original bug - a blank required
       question, reported as fine.
    2. A rule you wrote in answers[] is authoritative for WHAT THE ANSWER IS.
       If the page disagrees with your rule, the page is corrected towards the
       RULE, never towards the model. A hostile or mistaken model answering
       "No" to "are you legally authorized to work" must not be able to
       overwrite the "Yes" your profile asked for.
    3. The model gets the last word only where you have no rule at all, and
       only at high confidence, and only through radio_matches.
    4. Where the model disagrees with a rule, nothing is touched and it is
       reported. That is the case worth reading by hand - it is how you would
       find out that a blanket "No, I have not been employed by this company"
       is about to go to a company you have in fact worked for.
    """
    problems = []
    for q in here:
        v = _match_row(q["question"], verdicts)
        q["vision_read"] = (v or {}).get("on_screen", "")
        if v is not None and bool(q["selected"]) != bool(q["vision_read"]):
            out["flags"].append(
                f"{re.sub(r'[ ]+', ' ', q['question'])[:64]} - the DOM says "
                f"{(q['selected'] or '(blank)')[:24]!r} but the screenshot reads "
                f"{(q['vision_read'] or '(blank)')[:24]!r} - look at this one")
        ok = (bool(q["intended"]) and bool(q["selected"])
              and radio_matches(q["selected"], q["intended"]))
        q["_problem"] = not ok
        if not ok:
            problems.append(q)

    if not problems:
        return

    # One question is enough to justify the call, and asking about the whole
    # screenful costs nothing extra - the settled ones are how a rule that is
    # wrong for THIS employer gets caught.
    answers = vision_answer.answer_from_screenshot(
        png, [{"question": q["question"], "options": q["options"],
               "selected": q["selected"]} for q in here], prof)

    for q in here:
        a = _match_row(q["question"], answers)
        proposed = (a or {}).get("answer", "UNKNOWN")
        confident = bool(a) and a["confidence"] == "high" and proposed.upper() != "UNKNOWN"
        short = re.sub(r"[ ]+", " ", q["question"])[:64]
        was = q["selected"] or "(blank)"

        # (2) your rule decides. Vision only reported that the page did not
        # match it; the value put on the page is still the one you wrote.
        if q["intended"]:
            if q["_problem"]:
                done, why = _apply_vision(q, q["intended"], dry)
                if done:
                    q["want"] = q["intended"]
                    out["changed"].append(
                        f"{short}  {was[:24]!r} -> {q['selected'][:34]!r}  "
                        f"({'would set' if dry else 'set'}; your answers[] rule, "
                        f"which the page was not showing)")
                elif why:
                    out["flags"].append(f"{short} - {why[:110]}")
            if confident and not radio_matches(q["intended"], proposed):
                out["flags"].append(
                    f"{short} - your rule answers {q['intended'][:24]!r}, but reading "
                    f"the form it should be {proposed[:24]!r} ({(a or {}).get('reason', '')[:60]}) "
                    f"- left as your rule says, check it")
            continue

        if not q["_problem"]:
            continue

        # (3) no rule of yours covers this one.
        if not a:
            # Not a refusal - nothing came back that could be lined up with
            # this question. Different fault, so it says so rather than
            # claiming the model considered it and declined.
            out["flags"].append(
                f"{short} - was {was[:24]!r}, unverified (no answer came back "
                f"for this question)")
            continue
        if proposed.upper() == "UNKNOWN":
            out["flags"].append(
                f"{short} - was {was[:24]!r}, left alone "
                f"(UNKNOWN: {a.get('reason', '')[:70]})")
            continue
        if not confident:
            out["flags"].append(
                f"{short} - was {was[:24]!r}, left alone "
                f"({a['confidence']} confidence: {proposed[:24]!r})")
            continue
        # An explicit answers[] entry outranks the skip list, exactly as it does
        # on the rule path - but this question has none, so never_autofill and
        # the hard skips apply in full. Criminal history, date of birth and the
        # rest are off-limits to the model, and until now that guard did not
        # cover radio groups at all: it was tested against a radio's label,
        # which is just "Yes".
        if any(s.search(q["question"]) for s in skips):
            out["flags"].append(
                f"{short} - was {was[:24]!r}, left alone "
                f"(never_autofill/skip-listed - yours to answer)")
            continue
        done, why = _apply_vision(q, proposed, dry)
        if done:
            q["want"] = q["selected"]
            out["changed"].append(
                f"{short}  {was[:24]!r} -> {q['selected'][:34]!r}  "
                f"({'would set' if dry else 'set'}; {a['reason'][:60]})")
        elif why:
            out["flags"].append(f"{short} - {why[:110]}")


def guard_before_advance(page, rules, n_explicit, dry) -> tuple[list, list]:
    """
    Last look before Continue: does every question still say what we decided?

    Verifying a step and then advancing leaves a window, and the window is not
    theoretical. Measured on this machine: something else attached to the same
    debug Chrome dispatched TRUSTED click events into a loaded page about five
    seconds after load - it selected "Yes" on a criminal-history question, then
    "No", then clicked a button. Whatever it was (see the note in README/SOP),
    the lesson is that this tool is not necessarily the only writer on the
    page, and an answer verified thirty seconds ago is not an answer that is
    still there.

    Cheap and DOM-only: no screenshot, no API call. Repairs what it can through
    radio_matches and reports what it cannot.
    """
    repaired, wrong = [], []
    try:
        qs = question_map(page, rules, n_explicit)
    except Exception:
        return repaired, wrong
    for q in qs:
        if not q["intended"]:
            continue
        if q["selected"] and radio_matches(q["selected"], q["intended"]):
            continue
        was = q["selected"] or "(blank)"
        done, why = _apply_vision(q, q["intended"], dry)
        short = re.sub(r"[ ]+", " ", q["question"])[:64]
        if done:
            repaired.append(f"[late] {short}  {was[:24]!r} -> {q['selected'][:30]!r} "
                            f"(changed after we checked it - put back)")
        else:
            wrong.append(f"[late] {short} - shows {was[:24]!r}, not the "
                         f"{q['intended'][:24]!r} your rule asks for{': ' + why if why else ''}")
    return repaired, wrong


def all_frames_text(page, cap: int = 40_000) -> str:
    """
    Every frame's text, main frame first.

    Indeed renders the review CARD - the name, the resume, the answers about to
    be sent - inside an `about:srcdoc` iframe. page.inner_text("body") returns
    only the chrome around it: 1156 characters of nav and legal boilerplate,
    with the applicant's name nowhere in it. Measured on a live review screen,
    frame[4] of 8 was the one carrying the name, so a check that reads the main
    frame alone concludes the review screen "never names Your Name" and
    holds back a perfectly good application. That is worse than no check: it
    fails in the direction that looks like caution.
    """
    parts = []
    for fr in page.frames:
        try:
            parts.append(fr.inner_text("body"))
        except Exception:
            continue                       # detached or cross-origin - skip it
    return re.sub(r"\s+", " ", " ".join(parts)).strip()[:cap]


def review_sanity(page, prof: dict) -> tuple[bool, str]:
    """
    Keyless check of the review screen: is this actually OUR application?

    Reached when the stepper lands STRAIGHT on review having filled nothing.
    Indeed carries a whole application over from the account, so this happens
    on any posting with no screening questions - and it is the one path where
    nothing in the run typed a character. With no API key there is then no
    verification of any kind, and "the gate passed" degrades to "we found
    nothing to object to", which is not the same statement at all.

    So read what Indeed is about to send and require it to name the applicant
    and carry a resume. This cannot judge a screening answer - on a page like
    this there are none - but it catches the case that matters: a review screen
    belonging to a half-built application, an empty profile, or another account.
    """
    body = all_frames_text(page)
    if not body:
        return False, "could not read the review screen"
    name = ((prof.get("identity", {}) or {}).get("full_name") or "").strip()
    if not name:
        return False, "profile.json has no identity.full_name to check against"
    if name.lower() not in body.lower():
        return False, f"the review screen never names {name}"
    if not re.search(r"\.pdf\b|\.docx?\b", body, re.I):
        return False, "no resume file is attached on the review screen"
    return True, f"names {name} and carries a resume"


_CONSENT_PATTERNS: list = []


def consent_policy_match(prof: dict, group: str, ident: str) -> bool:
    """Does this checkbox match a consent pattern the operator opted into?

    Empty/missing `consent_policy.auto_tick` means the old behaviour exactly:
    only boxes the form itself marks required are ever ticked.
    """
    pats = prof.get("consent_policy", {}).get("auto_tick") or []
    if not pats:
        return False
    global _CONSENT_PATTERNS
    if len(_CONSENT_PATTERNS) != len(pats):
        _CONSENT_PATTERNS = []
        for p in pats:
            try:
                _CONSENT_PATTERNS.append(re.compile(p, re.I))
            except re.error:
                print(f"  ! bad regex in consent_policy.auto_tick, ignoring: {p}")
    blob = f"{group or ''} {ident or ''}"
    return any(rx.search(blob) for rx in _CONSENT_PATTERNS)


def tick_consent(res: dict, auto: bool, dry: bool, policy: bool = False) -> list[str]:
    """
    Tick the checkboxes THIS STEP marks required, and report exactly which.

    Under --auto-submit, or when `policy` is set by consent_policy.auto_tick in
    profile.json. The single-page path has always done this inside
    auto_submit(); the stepper never did, so Indeed's last step - "You declare
    that you have read and understand the privacy notice" - stayed unticked and
    the step could not advance. The application died at 88% with "no continue
    button", which named the symptom and hid the cause.

    Two scopes, and which one applies depends on the mode:

      res["consent_policy"]  boxes matching consent_policy.auto_tick in
                             profile.json. Ticked whenever `policy` is on,
                             including --fill-only.
      res["consent"]         boxes the FORM marks required. Ticked only under
                             --auto-submit, and only after why_not_submittable()
                             has already come back empty.

    Required-ness must never reach the first scope. A required multi-select
    marks every option required, so ticking "everything required" ticks every
    option of it - measured on WelbeHealth 2026-08-08, where that put Pinecone,
    Ollama, Milvus and "None of the above" all on at once. Nothing is ticked for
    being nearby either: "Share these answers with Indeed" sits beside a
    required consent box and matches no pattern, so it stays untouched.
    """
    # `policy` is the consent_policy.auto_tick opt-in: it lets the tick happen in
    # --fill-only too, which is the only mode this funnel can actually use. `dry`
    # still changes nothing, ever.
    if dry:
        return []
    boxes = list(res.get("consent_policy", [])) if policy else []
    if auto:
        boxes += list(res.get("consent", []))
    ticked = []
    for handle, ident in boxes:
        try:
            handle.check()
            ticked.append(ident)
        except Exception:
            # Not fatal, and not silent: the step will refuse to advance and
            # read_validation() will name the box in the operator's report.
            ticked.append(f"{ident}  (COULD NOT TICK)")
    return ticked


def run_stepper(page, prof, rules, skips, dry, n_explicit, title, company,
                auto: bool = False, vision: bool = True):
    """Fill each step, advance, stop at review. Returns (steps, outcome)."""
    steps = []
    outcome = "max_steps"
    retried = False

    for n in range(1, MAX_STEPS + 1):
        slug = step_slug(page.url)
        # Walk the page to the bottom first. A question that has not been
        # rendered yet is not in the DOM, so it is not merely missed - it is
        # invisible to the report too, and the step then fails validation for
        # a reason nothing on screen explains.
        # Questions arrive after the URL changes, so filling straight after
        # Continue can read a half-built page. A fixed pause is used rather
        # than waiting for networkidle: that wait coincided with step 1
        # starting to stall on a page it had previously cleared, and an
        # unexplained regression on the resume step is not worth the trade.
        page.wait_for_timeout(1500)
        try:
            for _ in range(8):
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(320)
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(800)
        except Exception:
            pass
        # Snapshot the radios BEFORE touching anything. Whatever is already
        # selected at this instant was chosen by the SITE, not by us - Indeed
        # pre-fills from your previous applications. After the fill pass the
        # two are indistinguishable, which is precisely why one carried-over
        # "Yes" on a federal-employment question once went out wrong and no
        # check in this file could have caught it.
        try:
            prefilled = {norm_label(q["question"])
                         for q in question_map(page, rules, n_explicit)
                         if q["selected"]}
        except Exception:
            prefilled = set()

        res = autofill_page(page, prof, rules, skips, dry, n_explicit,
                            title, company)
        steps.append((n, slug, res))

        # Before the screenshot below, so the proof shows the step exactly as
        # it was left rather than one action short of it.
        res["consent_ticked"] = tick_consent(
            res, auto, dry, policy=bool(prof.get("consent_policy", {}).get("auto_tick")))

        # Of those pre-selected answers, which does a rule of yours actually
        # confirm? One that no rule addresses is an answer this application will
        # send that nothing here ever agreed with.
        res["carried_over"] = []
        try:
            for q in question_map(page, rules, n_explicit):
                if norm_label(q["question"]) not in prefilled:
                    continue
                if (q["intended"] and q["selected"]
                        and radio_matches(q["selected"], q["intended"])):
                    continue                      # a rule of yours agrees with it
                res["carried_over"].append(
                    f"{re.sub(r'[ ]+', ' ', q['question'])[:70]} - shows "
                    f"{(q['selected'] or '(blank)')[:30]!r}, pre-selected by the "
                    f"site and confirmed by no rule of yours")
        except Exception:
            pass

        # Proof, not a promise. Auto-submit means nobody watched this happen,
        # so every step is photographed before it is left behind - if an answer
        # went out wrong you can see exactly what the form looked like.
        stem = f"{safe_name(company)}-step{n}"
        try:
            SHOTS.mkdir(parents=True, exist_ok=True)
            shot = SHOTS / f"{stem}.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"    [shot] {shot.name}")
        except Exception:
            pass

        print(f"\n  step {n}  [{slug}]")
        for f in res["filled"]:
            print(f"    + {f}")
        # Kept on its own marker: a consent you agreed to by policy is not the
        # same act as an answer a rule worked out, and the log should not blur
        # them months from now.
        for t in res.get("consent_ticked", []):
            print(f"    [x] required checkbox ticked: {t}")
        for c in res.get("carried_over", []):
            print(f"    ! [carried over] {c}")
        for m in res["missing"]:
            print(f"    ? {m}   (required, nothing in your profile matched)")
        for y in res["needs_you"]:
            print(f"    > {y}")
        if not (res["filled"] or res["missing"] or res["needs_you"]):
            print("    (nothing to fill here)")

        # Now LOOK at what we just did. Rules answer "did a rule fire"; this
        # answers "is the right answer on the screen", which is a different
        # question and the one that matters - a question the site pre-filled
        # wrongly from a previous application passes the first test and fails
        # this one. Kept separate from res["filled"] on purpose: you should
        # always be able to see which layer touched which answer.
        vis = {"changed": [], "flags": [], "seen": 0, "panes": 0, "note": "off"}
        if vision and "review-module" not in page.url:
            try:
                vis = vision_step(page, prof, rules, skips, n_explicit, dry, stem)
            except Exception as exc:
                # Vision is an improvement, never a dependency. A failure here
                # leaves the step exactly as the rule layer left it.
                vis["note"] = f"vision error, ignored ({type(exc).__name__})"
            for c in vis["changed"]:
                print(f"    ~ [vision] {c}")
            for fl in vis["flags"]:
                print(f"    ! [vision] {fl}")
            if vis["seen"] and not (vis["changed"] or vis["flags"]):
                print(f"    [vision] {vis['seen']} question(s) verified on screen")
        res["vision"] = list(vis["changed"])
        res["vision_flags"] = list(vis["flags"])
        res["vision_note"] = str(vis["note"])

        if "review-module" in page.url:
            outcome = "review"
            # Indeed refuses to advance a step with a required question
            # unanswered, so reaching review proves every one of them is
            # ANSWERED. It proves nothing about whether the answers are right -
            # the site pre-fills from previous applications, and one of those
            # carried-over answers was false for this user.
            if auto and not dry:
                # Reaching review proves every required question had SOMETHING
                # selected. It does not prove the selection was right, and that
                # was the flaw in this gate: a step answered entirely by the
                # site's carried-over pre-fill has missing == [] and sailed
                # through. Anything vision could not resolve now holds it back
                # too. Deliberately not all of needs_you - consent checkboxes
                # live there and the gate would never pass.
                unanswered = [m for _, _, r in steps for m in r["missing"]]
                unanswered += [f"unverified: {f}"
                               for _, _, r in steps for f in r.get("vision_flags", [])]
                # The gate above is made entirely of objections, so a run that
                # examined nothing passes it vacuously. Two different holes:
                #
                # 1. An answer the SITE pre-selected that no rule confirms. This
                #    is the one with a body count - Indeed carried a "Yes" onto
                #    a federal-employment question and it was sent. It is NOT
                #    caught by counting filled fields: the step that did it had
                #    fifteen of them. Only gated when nothing looked at the
                #    page, because vision_step already reports the same thing
                #    through vision_flags and would double-count.
                looked = any(str(r.get("vision_note", "")).startswith("vision on")
                             for _, _, r in steps)
                if not looked:
                    unanswered += [f"carried over, unconfirmed: {c}"
                                   for _, _, r in steps
                                   for c in r.get("carried_over", [])]
                # 2. Nothing filled AND nothing verified - a posting with no
                #    screening questions puts the stepper straight on review.
                #    The first live auto-submit went out that way: 0 filled,
                #    0 verified, logged "SUBMITTED", which read like a clean
                #    result and meant "we sent whatever Indeed already had".
                filled_total = sum(len(r["filled"]) for _, _, r in steps)
                if not filled_total and not looked:
                    ok_sane, why = review_sanity(page, prof)
                    print(f"    [review check] {why}")
                    if not ok_sane:
                        unanswered.append(
                            f"nothing in this run was filled or verified, and "
                            f"the review screen did not check out: {why}")
                if unanswered:
                    outcome = "held_back"
                    break
                ok, detail = submit_stepper(page)
                outcome = f"submitted:{detail}" if ok else f"submit_failed:{detail}"
            break

        btn, label = find_advance(page)
        if btn is None:
            outcome = "no_continue"
            break
        # The advance button is relabelled per step ('Continue', 'Review
        # details', ...). The deny-list is checked on every path, including
        # the testid one, so a step that renames it to a send is never clicked.
        if SEND_LABEL.search(label):
            outcome = "review"
            break
        if dry:
            outcome = "dry"
            break

        # Nothing between here and the click. Anything that moved since the
        # verification pass is put back now, or reported if it cannot be.
        late_fixed, late_wrong = guard_before_advance(page, rules, n_explicit, dry)
        for f in late_fixed:
            print(f"    ~ {f}")
        for w in late_wrong:
            print(f"    ! {w}")
        res["vision"].extend(late_fixed)
        res["vision_flags"].extend(late_wrong)

        before_url, before_sig = page.url, field_signature(page)
        try:
            btn.click()
        except Exception as exc:
            outcome = f"click_failed ({type(exc).__name__})"
            break
        page.wait_for_timeout(3200)
        if page.url == before_url and field_signature(page) == before_sig:
            # Check the form's own complaints before declaring a mystery, and
            # bank each blocking question so the rule set can grow to cover it.
            errs = read_validation(page)
            if errs and not retried:
                # Everything is rendered NOW. Questions that arrive late are
                # absent from the first pass entirely - the sanctions question
                # never even appeared in the collected fields - so fill again
                # against the complete page before calling it blocked.
                print(f"    {len(errs)} question(s) appeared late - refilling")
                retried = True
                res2 = autofill_page(page, prof, rules, skips, dry, n_explicit,
                                     title, company)
                for f in res2["filled"]:
                    print(f"    + {f}")
                res["filled"].extend(res2["filled"])
                # A required checkbox that rendered late blocks exactly like one
                # that was there from the start, so it gets ticked here too.
                late_consent = tick_consent(
                    res2, auto, dry,
                    policy=bool(prof.get("consent_policy", {}).get("auto_tick")))
                for t in late_consent:
                    print(f"    [x] required checkbox ticked: {t}")
                res.setdefault("consent_ticked", []).extend(late_consent)
                # Look at these too. Late-arriving questions are exactly the
                # ones the rule layer has never been tested against - the
                # sanctions question did not appear in the first pass at all -
                # so advancing them on rules alone would leave the one part of
                # the step that nothing ever verified.
                if vision and res2["filled"]:
                    try:
                        vis2 = vision_step(page, prof, rules, skips, n_explicit,
                                           dry, f"{stem}-late")
                        for c in vis2["changed"]:
                            print(f"    ~ [vision] {c}")
                        for fl in vis2["flags"]:
                            print(f"    ! [vision] {fl}")
                        res["vision"].extend(vis2["changed"])
                        res["vision_flags"].extend(vis2["flags"])
                    except Exception as exc:
                        res["vision_flags"].append(
                            f"late questions were not verified ({type(exc).__name__})")
                if res2["filled"]:
                    late_fixed, late_wrong = guard_before_advance(
                        page, rules, n_explicit, dry)
                    res["vision"].extend(late_fixed)
                    res["vision_flags"].extend(late_wrong)
                    try:
                        btn2, lab2 = find_advance(page)
                        if btn2 is not None and not SEND_LABEL.search(lab2):
                            btn2.click()
                            page.wait_for_timeout(3200)
                            if page.url != before_url:
                                continue          # it moved - carry on
                    except Exception:
                        pass
                errs = read_validation(page)
            if errs:
                print(f"\n    the form is waiting on {len(errs)} question(s):")
                for e in errs:
                    print(f"      ! {e['question'][:110]}")
                    unmatched_q(e["question"], "")
                res["blocked_by"] = errs
            outcome = "stuck"
            break

    return steps, outcome


def report_stepper(steps, outcome) -> tuple[str, str]:
    """(status, note) for the log, plus the closing line for the human."""
    filled = sum(len(r["filled"]) for _, _, r in steps)
    missing = [m for _, _, r in steps for m in r["missing"]]
    needs = [y for _, _, r in steps for y in r["needs_you"]]
    vchanged = [c for _, _, r in steps for c in r.get("vision", [])]
    vflags = [f for _, _, r in steps for f in r.get("vision_flags", [])]
    # Did anything actually look at the page? "0 corrected" reads like a clean
    # verification and must not be printed when there was no verification -
    # that is the same class of misleading report this feature exists to kill.
    notes = [str(r.get("vision_note", "")) for _, _, r in steps]
    looked = any(nt.startswith("vision on") for nt in notes)

    print(f"\n  {len(steps)} step(s), {filled} field(s) filled by rules, "
          + (f"{len(vchanged)} corrected after looking at the page" if looked
             else f"{len(vchanged)} corrected - NOTHING LOOKED AT THE PAGE "
                  f"({next((nt for nt in notes if nt), 'vision off')})"))
    # Kept apart from the rule count on purpose. A number that mixes them tells
    # you nothing about which layer to fix when an answer comes out wrong.
    for c in vchanged:
        print(f"    ~ [vision] {c}")
    if vflags:
        print(f"\n  {len(vflags)} answer(s) vision could NOT settle - read these "
              f"in Chrome before sending:")
        for f in vflags[:8]:
            print(f"    ! {f}")

    # Goes into applications.jsonl, so the two layers stay distinguishable
    # months later when you are reading the log and not the screen.
    tail = (f"; vision corrected {len(vchanged)}, unresolved {len(vflags)}"
            if looked else "; NOT vision-verified")

    if outcome.startswith("submitted:"):
        print(f"  SUBMITTED - {outcome.split(':', 1)[1]}")
        return "submitted", outcome.split(":", 1)[1] + tail
    if outcome.startswith("submit_failed:"):
        print(f"  SUBMIT FAILED - {outcome.split(':', 1)[1]}")
        return "submit_failed", outcome.split(":", 1)[1] + tail
    if outcome == "held_back":
        print("\n  Reached review, but NOT submitting.")
        if missing:
            print("  A required field had nothing in your profile to answer it:")
            for m in missing[:6]:
                print(f"    ? {m}")
        if vflags:
            print("  An answer on screen could not be verified as correct - "
                  "see the list above.")
        return "needs_you", ("required field unanswered" if missing
                             else "answer unverified") + tail
    if outcome == "review":
        print("  Reached the review screen. Read it in Chrome and submit there.")
        return "needs_you", f"reached review; {filled} filled{tail}"
    if outcome == "dry":
        return "dry", "dry run" + tail
    if outcome == "stuck":
        blocked = [e for _, _, r in steps for e in r.get("blocked_by", [])]
        if blocked:
            print(f"\n  Stopped. The form is waiting on {len(blocked)} question(s) "
                  f"no rule answered:")
            for e in blocked:
                print(f"    ! {e['question'][:110]}")
            print("\n  Each one is in unanswered.jsonl - add an answers[] entry "
                  "and it\n  stops blocking on every employer that words it "
                  "the same way.")
            return "needs_you", f"{len(blocked)} unanswered: " + \
                   "; ".join(e["question"][:40] for e in blocked[:3]) + tail
        print("\n  Stopped: the form did not accept this step.")
        if missing:
            print("  Unanswered required question(s) here:")
            for m in missing[:6]:
                print(f"    ? {m}")
        if needs:
            print("  Things only you can set (radios, checkboxes, uploads):")
            for y in needs[:6]:
                print(f"    > {y}")
        if not missing and not needs:
            print("  Nothing obvious was unanswered - look at it in Chrome.")
        return "needs_you", "stuck on a step" + tail
    if outcome == "no_continue":
        print("\n  Stopped: no Continue button on this page - unfamiliar screen.")
        return "needs_you", "no continue button" + tail
    if outcome == "max_steps":
        print(f"\n  Stopped after {MAX_STEPS} steps without reaching review.")
        return "needs_you", "too many steps" + tail
    print(f"\n  Stopped: {outcome}")
    return "needs_you", outcome + tail


# --------------------------------------------------------------------------
# tracker
# --------------------------------------------------------------------------

# Boards that identify a posting in the QUERY STRING, not the path. Dropping
# the query on these makes every Indeed job normalise to ".../viewjob", so the
# first one logged as submitted would mask every later one as a duplicate -
# silently skipping real jobs. Only identifier params are kept; tracking junk
# (utm_*, from=, vjs=) still gets stripped, so true duplicates still collapse.
# Greenhouse embeds are the same trap in a second costume: every posting is
# ".../embed/job_app" and the company (for=) and posting (token=) live only in
# the query, so without these two one logged embed application masks every
# other company's.
ID_PARAMS = ("jk", "vjk", "gh_jid", "currentjobid", "jobid", "job_id",
             "requisitionid", "postingid", "for", "token")


# Tabs a human has to finish: a CAPTCHA, a question no rule answers, a send
# control the site never enabled. The run leaves these OPEN in Chrome and lists
# them at the end, because the alternative is a report telling you nine jobs
# need you and no way back to any of them - the URL of a half-completed
# smartapply step is not something you can retype, and re-running the queue
# starts the whole wizard again.
LEFT_OPEN: list[dict] = []


def leave_open(page, why: str, title: str = "", company: str = "") -> None:
    """Keep this tab open for the operator, and remember why."""
    if page is None:
        return
    what = " - ".join(x for x in (title[:52], company[:30]) if x) or "(untitled)"
    try:
        url = page.url
    except Exception:
        url = ""
    LEFT_OPEN.append({"page": page, "why": why, "what": what, "url": url})


def kept_pages() -> set:
    return {e["page"] for e in LEFT_OPEN}


def report_left_open() -> None:
    if not LEFT_OPEN:
        return
    print(f"\n{'=' * 72}")
    print(f"  {len(LEFT_OPEN)} tab(s) left open in Chrome for you to finish:")
    for e in LEFT_OPEN:
        print(f"    - {e['what']}")
        print(f"        {e['why']}")
        if e["url"]:
            print(f"        {e['url'][:96]}")
    print("  Finish them in that window, then log them with:  "
          "python autofill.py --report")


def norm_url(url: str) -> str:
    base, _, query = url.split("#")[0].partition("?")
    base = base.rstrip("/").lower()

    keep = []
    for part in query.split("&"):
        key, _, val = part.partition("=")
        if val and key.lower() in ID_PARAMS:
            keep.append(f"{key.lower()}={val}")
    return f"{base}?{'&'.join(sorted(keep))}" if keep else base


def log_application(url: str, ats: str, status: str, title: str, company: str,
                    requested: str = "", note: str = "") -> None:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": url,
        # ATS links redirect constantly, so remember the URL we were given as
        # well as the one we landed on - the duplicate check needs both.
        "requested_url": requested or url,
        "ats": ats,
        "status": status,
        "title": title,
        "company": company,
        "note": note,
    }
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _tab_key(url: str) -> str:
    """norm_url, with the /apply and /application suffix Lever and Ashby add
    to the same posting treated as the same job."""
    return re.sub(r"/(apply|application)$", "", norm_url(url))


def already_applied(url: str) -> dict | None:
    if not LOG_PATH.exists():
        return None
    key = norm_url(url)
    with LOG_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("status") != "submitted":
                continue
            known = {norm_url(rec.get("url", "")),
                     norm_url(rec.get("requested_url", ""))}
            if key in known:
                return rec
    return None


def show_report() -> None:
    if not LOG_PATH.exists():
        print("No applications logged yet.")
        return
    rows = []
    with LOG_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
    print(f"\n{len(rows)} entries in {LOG_PATH.name}")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<12} {v}")
    print()
    for r in rows[-25:]:
        mark = "OK " if r.get("status") == "submitted" else "-- "
        print(f"  {mark}{r['ts'][:10]}  {(r.get('company') or '?')[:24]:<24} "
              f"{(r.get('title') or '?')[:40]}")
    print()


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

# Set JOBSEARCH_SHOW=1 to watch a run happen.
SHOW_WINDOW = os.environ.get("JOBSEARCH_SHOW", "").strip() not in ("", "0")


def keep_minimized(page) -> None:
    """
    Push the window back down after opening a tab.

    Starting Chrome minimized is not enough: every ctx.new_page() creates a new
    target, and Chrome RESTORES the window to show it. A queue run therefore
    pops up once per job - seventeen times across a full queue - on the same
    machine the operator is trying to work on. Minimizing the window once, by
    hand or at launch, cannot survive the next tab.

    Best-effort and silent: if the CDP call is unavailable the run carries on
    exactly as before, just visible. Never worth failing an application over.
    """
    if SHOW_WINDOW:
        return
    try:
        s = page.context.new_cdp_session(page)
        win = s.send("Browser.getWindowForTarget")
        s.send("Browser.setWindowBounds",
               {"windowId": win["windowId"],
                "bounds": {"windowState": "minimized"}})
        s.detach()
    except Exception:
        pass


def connect(port: int):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is not installed.  Run:  python -m pip install playwright")

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception:
        pw.stop()
        sys.exit(
            f"Could not reach Chrome on port {port}.\n"
            f"Start it first:  powershell -File \"{HERE / 'start_chrome.ps1'}\""
        )
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    return pw, browser, ctx


def count_fields(page, seconds: int = 12) -> int:
    """
    Form fields on the page, across every frame, waiting for a late render.

    "Zero fields means the posting is gone" is true of a 404 and false of a
    JS application that builds its form after domcontentloaded. Judged once at
    2.5s in the main frame only, this call reported DEAD LINK on a live Workday
    posting and on a live SmartRecruiters one titled "Easy apply - AI Engineer
    III - Vericast" - both skipped, logged as dead, and never applied to. A
    posting wrongly called dead is invisible: it never appears in the report as
    something needing attention, because as far as the log is concerned it does
    not exist.

    Frames matter for the same reason they did on Indeed's review screen: the
    form is often not in the main document.
    """
    best = 0
    for _ in range(max(1, seconds)):
        for fr in page.frames:
            try:
                best = max(best, fr.evaluate(
                    "() => document.querySelectorAll("
                    "'input, textarea, select').length"))
            except Exception:
                continue                      # detached or cross-origin frame
        if best:
            return best
        page.wait_for_timeout(1000)
    return best


def page_meta(page) -> tuple[str, str]:
    title = (page.title() or "").strip()
    company = ""
    m = re.search(r"\bat\s+([^|]+?)\s*(?:\||$)", title)
    if m:
        company = m.group(1).strip()
    else:
        m = re.match(r"^([^-|]+)[-|]", title)
        if m:
            company = m.group(1).strip()
    return title[:120], company[:60]


def run_one(page, url: str, prof, rules, skips, dry: bool,
            n_explicit: int = 0, auto: bool = False, ctx=None,
            vision: bool = True, fill_only: bool = False) -> str:
    prior = already_applied(url)
    if prior:
        print(f"\n  Already submitted on {prior['ts'][:10]} - skipping.")
        return "duplicate"

    print(f"\n{'=' * 72}\n{url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2500)

    ats = detect_ats(page.url)
    title, company = page_meta(page)
    print(f"  ATS: {ats}   {company}  |  {title}")

    # Postings close constantly; a stale queue is mostly dead links. Say so
    # plainly instead of reporting an empty form as a clean run.
    n_fields = count_fields(page)
    if n_fields == 0 or re.search(r"404|not found|no longer|closed", title, re.I):
        print("  DEAD LINK - posting closed or removed. Skipping.")
        # --dry promises to touch nothing, and the tracker is something. A dry
        # run that quietly files dead_link entries makes --report disagree with
        # what you actually did.
        if not dry:
            log_application(page.url, ats, "dead_link", title, company,
                            requested=url, note=f"{n_fields} form fields")
        return "dead_link"

    # Indeed's own apply flow is a stepper - different path entirely, and one
    # that never submits. --auto-submit is deliberately not passed through.
    if ats in ("indeed", "indeed_apply"):
        target = page
        if ats == "indeed" and ctx is not None:
            target = enter_smartapply(page, ctx)
            if target is None:
                print("  Could not open Indeed's apply flow from this page.")
                log_application(page.url, ats, "needs_you", title, company,
                                requested=url, note="no apply button")
                return "needs_you"
        elif ats == "indeed":
            print("  Indeed job page, but no browser context to open the apply "
                  "flow - open it from the GUI or pass a smartapply URL.")
            return "needs_you"

        steps, outcome = run_stepper(target, prof, rules, skips, dry,
                                     n_explicit, title, company, auto, vision)
        status, note = report_stepper(steps, outcome)
        if dry:
            return "dry"
        log_application(target.url, "indeed_apply", status, title, company,
                        requested=url, note=note)
        if status == "submitted":
            # Done with it. Closing here rather than in main() because this tab
            # is one Indeed opened, not one main() made, so main() cannot know
            # about it.
            if target is not page:
                try:
                    target.close()
                except Exception:
                    pass
        else:
            leave_open(target, note, title, company)
        return status

    res = autofill_page(page, prof, rules, skips, dry, n_explicit, title, company)

    # Single-page forms used to have their consent boxes ticked only inside
    # auto_submit(), which never runs on a CAPTCHA'd funnel - so in --fill-only
    # every consent box was left for you, including the ones you have since said
    # to tick. Reported on its own line below, never blurred into `filled`.
    res["consent_ticked"] = tick_consent(
        res, auto, dry, policy=bool(prof.get("consent_policy", {}).get("auto_tick")))
    if res["consent_ticked"]:
        # Ticked here, so drop them from the list of things left for you.
        ticked_idents = {t.split("  (COULD NOT TICK)")[0] for t in res["consent_ticked"]}
        res["needs_you"] = [n for n in res["needs_you"]
                            if not any(t in n for t in ticked_idents)]

    print(f"\n  filled ({len(res['filled'])}):")
    for f in res["filled"]:
        print(f"    + {f}")
    if res["missing"]:
        print(f"\n  required but no rule matched ({len(res['missing'])}):")
        for m in res["missing"]:
            print(f"    ? {m}")
    if res["needs_you"]:
        print(f"\n  yours to handle ({len(res['needs_you'])}):")
        for n in res["needs_you"]:
            print(f"    > {n}")
    if res["skipped"]:
        print(f"\n  deliberately left blank ({len(res['skipped'])}): "
              f"{', '.join(s[:28] for s in res['skipped'])}")
    for t in res.get("consent_ticked", []):
        print(f"    [x] consent checkbox ticked: {t}")
    if res["captcha"]:
        print("\n  ! CAPTCHA on this form - you have to clear it yourself.")

    if dry:
        return "dry"

    if auto:
        reasons = why_not_submittable(res)
        if reasons:
            print(f"\n  NOT auto-submitting: {'; '.join(reasons)}")
            log_application(page.url, ats, "needs_you", title, company,
                            requested=url, note="; ".join(reasons))
            # Filled and waiting. A CAPTCHA is the common case here and no
            # amount of re-running clears it, so the form stays on screen with
            # the typing already done and you finish the last step yourself.
            leave_open(page, "; ".join(reasons), title, company)
            return "needs_you"
        ok, detail = auto_submit(page, res)
        status = "submitted" if ok else "submit_failed"
        print(f"\n  {'SUBMITTED' if ok else 'SUBMIT FAILED'}: {detail}")
        log_application(page.url, ats, status, title, company,
                        requested=url, note=detail)
        return status

    if fill_only:
        # Fill it, leave it on screen, move on. The prompt below is the reason
        # this mode exists: it reads from stdin, and a queue run started from a
        # script or a background shell has no stdin - input() raises EOF, which
        # returns "quit", which stops the whole queue after the FIRST form. A
        # queue of eight would fill one and look like it had finished.
        why = "; ".join(why_not_submittable(res)) or "filled - submit it yourself"
        log_application(page.url, ats, "needs_you", title, company,
                        requested=url, note=f"fill-only: {why}")
        leave_open(page, why, title, company)
        print("\n  Filled and left open in Chrome for you.")
        return "filled"

    print("\n  Read the form in Chrome, fix anything above, then submit it there.")
    try:
        ans = input("  [s]ubmitted  [k] skipped  [q] quit > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "quit"

    if ans.startswith("q"):
        return "quit"
    status = "submitted" if ans.startswith("s") else "skipped"
    log_application(page.url, ats, status, title, company, requested=url)
    print(f"  logged: {status}")
    return status


def main() -> None:
    ap = argparse.ArgumentParser(description="Autofill job application forms.")
    ap.add_argument("url", nargs="?", help="application URL")
    ap.add_argument("--queue", help="text file of URLs, one per line")
    ap.add_argument("--dry", action="store_true", help="report only, change nothing")
    ap.add_argument("--report", action="store_true", help="show application log")
    ap.add_argument("--profile", help="alternate profile.json path")
    ap.add_argument("--auto-submit", action="store_true",
                    help="submit without asking, but ONLY when the form has no "
                         "CAPTCHA and every required field came out answered")
    ap.add_argument("--fill-only", action="store_true",
                    help="fill every form in the queue and leave each tab open in "
                         "Chrome for you to submit. Never prompts, never submits - "
                         "the right mode for a queue full of CAPTCHA'd forms")
    ap.add_argument("--no-vision", action="store_true",
                    help="skip the look-at-the-page verification pass and answer "
                         "from rules alone (no API calls, no cost)")
    ap.add_argument("--max", type=int, default=25,
                    help="hard cap on submissions in one run (default 25)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    if args.report:
        show_report()
        return

    if args.fill_only and args.auto_submit:
        ap.error("--fill-only and --auto-submit are opposites; pick one")

    urls: list[str] = []
    if args.url:
        urls.append(args.url)
    if args.queue:
        qp = Path(args.queue)
        if not qp.is_absolute():
            qp = HERE / qp
        for line in qp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line.split()[0])
    if not urls:
        ap.error("give a URL or --queue")

    prof = load_profile(Path(args.profile) if args.profile else None)
    rules = build_rules(prof)
    skips = build_skips(prof)
    print(f"profile: {prof['identity'].get('full_name') or '(unnamed)'}  "
          f"| {len(rules)} rules | {len(urls)} url(s)")

    n_explicit = count_explicit(prof)
    use_vision = not args.no_vision
    if use_vision and vision_answer is not None:
        # Say which way it went at startup rather than letting a silent
        # fallback look like a successful verification. ANTHROPIC_BASE_URL is
        # pinned inside vision_answer for the same reason: a harness that
        # exports it would otherwise reroute these calls without a word.
        print(vision_answer.status())
    elif use_vision:
        print("vision OFF (vision_answer.py could not be imported)")
    if args.auto_submit:
        print(f"AUTO-SUBMIT on (cap {args.max}). Anything with a CAPTCHA or an "
              f"unanswered required question is left for you.")

    pw, browser, ctx = connect(args.port)
    sent = 0
    _seen_this_run: set = set()
    try:
        for i, url in enumerate(urls, 1):
            # A tab per job, so an unfinished one can be left on screen without
            # holding up the rest of the queue. The old single reused tab made
            # that impossible: keeping it open meant the next job had nowhere
            # to load.
            # One tab per job, ever. A rerun of the same queue used to open a
            # second copy of every form still sitting in Chrome.
            _k = _tab_key(url)
            if _k in {_tab_key(pg.url) for pg in ctx.pages if pg.url} or _k in _seen_this_run:
                print(f"\n[{i}/{len(urls)}] already open in Chrome - skipping {url[:80]}")
                continue
            _seen_this_run.add(_k)
            page = ctx.new_page()
            keep_minimized(page)
            print(f"\n[{i}/{len(urls)}]", end="")
            out = run_one(page, url, prof, rules, skips, args.dry,
                          n_explicit, args.auto_submit, ctx, use_vision,
                          args.fill_only)
            # Close the ones that are finished with; a 20-job queue must not
            # leave 20 tabs behind, but the handful that need you must survive.
            if page not in kept_pages():
                try:
                    page.close()
                except Exception:
                    pass
            if out == "quit":
                print("stopping.")
                break
            if out == "submitted":
                sent += 1
                if args.auto_submit and sent >= args.max:
                    print(f"\nhit --max {args.max}, stopping.")
                    break
    finally:
        # NOT browser.close(). This is a Chrome the operator started and is
        # signed into; connect_over_cdp gives us a handle to THEIR browser, and
        # closing it shuts the whole window - taking every tab we just decided
        # to leave open with it. It also cost a debugging session earlier:
        # Chrome vanishing mid-run looked like a crash and was this line.
        pw.stop()

    report_left_open()
    if not args.dry:
        show_report()


if __name__ == "__main__":
    main()
