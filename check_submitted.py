"""Auto-detect which of the open tabs you actually submitted, and log them.

Why this exists: `--fill-only` logs every form as `needs_you`. Nothing marks them
`submitted`, so the next `build_queue.py` offers them all again and re-applies to
jobs already sent. SOP step 7 says to append those records by hand. This does it
by reading the tabs instead.

The signal it trusts is the confirmation state the ATS renders *after* a submit.
That only exists while the tab is open, which matches the workflow: the bot
fills, you submit in that window, then you run this before closing anything.

  python check_submitted.py           # detect and log
  python check_submitted.py --dry     # show what it would log, write nothing

Deliberately conservative. A false positive is the expensive error - it means a
job silently never gets applied to - so a tab is only called submitted on an
explicit confirmation (URL or wording). A form merely disappearing is not
enough; SPAs blank themselves for other reasons.
"""
import json, os, re, sys, shutil, datetime
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofill

BASE = os.path.dirname(os.path.abspath(__file__))
DRY = "--dry" in sys.argv


def datetime_today():
    return datetime.date.today().isoformat()

# Same normalisation build_queue.py uses, so keys line up with the queue.
def key_of(u: str) -> str:
    return re.sub(r"[?&](jr_id|token|gh_src|utm_[a-z]+)=[^&]*", "", u or "").rstrip("/?&")

# The trailing (/|?|#|end) matters: a bare 'success' alternative also matches
# successfactors.com, which is a live ATS, not a confirmation page.
CONFIRM_URL = re.compile(
    r"/(confirmation|thank[-_]?you|thanks|success|submitted|post[-_]?apply)(?=[/?#]|$)"
    r"|application[-_]?(submitted|complete|received)"
    r"|[?&]submitted=(true|1)", re.I)

CONFIRM_TEXT = re.compile(
    r"thank(s| you) for (applying|your application|your interest in applying)"
    r"|your application (has been |was )?(successfully )?(submitted|received|sent)"
    r"|we('ve| have) received your application"
    r"|application (has been |was )?(successfully )?(submitted|received)"
    r"|successfully applied"
    # Workable submits inside a modal without changing the URL and then renders a
    # bare 'Already applied' badge - no 'you have', so anchor it loosely.
    r"|already applied", re.I)

# Wording that looks like a confirmation but is not one - present on plenty of
# un-submitted forms (privacy blurbs, "we will review applications received by").
NOT_CONFIRM = re.compile(
    r"applications? received (by|before|after|until)"
    r"|before (you )?submit|prior to submitting", re.I)

JS = r"""
() => {
  const t = document.body ? document.body.innerText : '';
  return {
    url: location.href,
    title: document.title,
    text: t.slice(0, 6000),
    formish: !!document.querySelector(
      'input[type=file], input[id*="email" i], input[name*="email" i], ' +
      'input[name*="first" i], input[id*="first_name" i]'),
  };
}
"""


def load_log():
    """(url-key -> record, [records in file order])."""
    by_key, rows = {}, []
    with open(os.path.join(BASE, "applications.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            u = d.get("url") or d.get("requested_url") or ""
            if u:
                by_key[key_of(u)] = d
                rows.append(d)
    return by_key, rows


# A submitted form navigates AWAY from the URL we filled - Greenhouse goes to
# /job_app/confirmation, dropping jr_id on the way. That URL normalises to a
# different key than the one in the log, so a plain key lookup misses and we
# would log the confirmation URL instead of the application URL. build_queue.py
# keys on the application URL, so the job would be offered all over again.
# Match on the identifying tokens both URLs still share instead.
def tokens_of(u: str) -> set:
    host = re.sub(r"^https?://", "", u or "").split("/")[0].lower()
    vals = set(re.findall(r"[?&](?:for|token|dcr_ci|publication|gh_jid|job)=([^&]+)", u or "", re.I))
    vals |= set(re.findall(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", u or "", re.I))
    return {host} | {f"v:{v.lower()}" for v in vals}


def match_record(url: str, by_key: dict, rows: list):
    """Find the log record this tab came from, tolerating a post-submit redirect."""
    rec = by_key.get(key_of(url))
    if rec:
        return rec
    t = tokens_of(url)
    host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
    best, best_n = None, 0
    for d in rows:
        u = d.get("url") or ""
        ot = tokens_of(u)
        if host not in ot:
            continue
        # need a shared identifier, not merely the same ATS host
        n = len({x for x in (t & ot) if x.startswith("v:")})
        # >= not > : rows are in file order, and the LAST record for a URL is the
        # current one. With > we kept the oldest 'needs_you' row and re-reported
        # jobs that had already been logged 'submitted'.
        if n and n >= best_n:
            best, best_n = d, n
    return best if best_n else None


def verdict(d):
    """(is_submitted, reason). Conservative on purpose."""
    text = d["text"]
    clean = NOT_CONFIRM.sub("", text)
    m_url = CONFIRM_URL.search(d["url"])
    m_txt = CONFIRM_TEXT.search(clean)
    if m_url:
        return True, f"url matched {m_url.group(0)!r}"
    if m_txt:
        return True, f"page says {m_txt.group(0)[:60]!r}"
    if not d["formish"]:
        # Form fields gone but nothing said "thanks" - suspicious, not proof.
        return False, "form fields gone but no confirmation wording (check by hand)"
    return False, "still showing a fillable form"


def from_history(hours=12):
    """Recover submissions from tabs that are already closed.

    A submit navigates to a confirmation URL, and Chrome's history outlives the
    tab - so a batch that got closed before anyone ran this is still recoverable.
    Caveat worth knowing: an ATS that submits inside a modal without changing the
    URL (Workable) leaves no history trace, so this finds nothing for those.
    """
    import sqlite3, shutil, glob, datetime
    cands = [p for p in (glob.glob(os.path.join(BASE, "chrome-profile/Default/History")) +
                         glob.glob(os.path.join(BASE, "chrome-profile/History")))
             if os.path.exists(p)]
    if not cands:
        print("no Chrome history db found under chrome-profile/")
        return []
    tmp = os.path.join(os.environ.get("TEMP", BASE), "_hist_check.db")
    shutil.copy(cands[0], tmp)          # locked while Chrome runs
    con = sqlite3.connect(tmp)
    cutoff = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(hours=hours)
    out = []
    for u, t in con.execute("select url, last_visit_time from urls order by last_visit_time desc limit 3000"):
        when = datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=t)
        if when < cutoff:
            continue
        if CONFIRM_URL.search(u or ""):
            out.append((u, when))
    con.close()
    return out


def main():
    by_key, log_rows = load_log()

    if "--history" in sys.argv:
        hits = from_history()
        print(f"{len(hits)} confirmation URL(s) in Chrome history\n")
        newly = []
        for u, when in hits:
            rec = match_record(u, by_key, log_rows)
            if not rec:
                print(f"  ? {when:%m-%d %H:%M} no log match: {u[:82]}")
            elif rec.get("status") == "submitted":
                print(f"  = {when:%m-%d %H:%M} already logged: {(rec.get('company') or '')[:34]}")
            else:
                print(f"  + {when:%m-%d %H:%M} SUBMITTED: {(rec.get('company') or rec.get('title') or '')[:34]}")
                newly.append((rec, u, when))
        if newly and not DRY:
            stamp = datetime_today()
            shutil.copy(os.path.join(BASE, "applications.jsonl"),
                        os.path.join(BASE, f"applications.jsonl.bak-history-{stamp}"))
            for rec, u, when in newly:
                autofill.log_application(rec["url"], rec.get("ats"), "submitted",
                                         rec.get("title"), rec.get("company"),
                                         note=f"auto-detected from Chrome history {when:%Y-%m-%d %H:%M}")
                print(f"    logged {(rec.get('company') or '')[:34]}")
        elif newly:
            print("\n--dry: nothing written")
        else:
            print("\nnothing new to log")
        return

    rows = []
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        for ctx in b.contexts:
            for page in ctx.pages:
                if page.url.startswith(("about:", "chrome:", "devtools:", "file:")):
                    continue
                try:
                    d = page.evaluate(JS)
                except Exception as exc:
                    print(f"!! {page.url[:70]} -> {exc}")
                    continue
                ok, why = verdict(d)
                rows.append((d, ok, why))

    if not rows:
        print("no application tabs open - nothing to check")
        return

    to_log, already, pending = [], [], []
    for d, ok, why in rows:
        rec = match_record(d["url"], by_key, log_rows) or {}
        prior = rec.get("status")
        if prior == "submitted":
            already.append((d, why))
        elif ok:
            if not rec:
                # No log record to attribute this to. Logging the confirmation
                # URL would not dedupe anything, so say so rather than pretend.
                pending.append((d, why + " -- SUBMITTED but no matching log entry; log it by hand"))
                continue
            to_log.append((d, why, rec))
        else:
            pending.append((d, why))

    print("=" * 74)
    print(f"{len(rows)} tab(s) checked")
    if to_log:
        print(f"\nSUBMITTED - will log ({len(to_log)}):")
        for d, why, _ in to_log:
            print(f"  + {d['title'][:58]}\n      {why}")
    if pending:
        print(f"\nNOT submitted - left as-is ({len(pending)}):")
        for d, why in pending:
            print(f"  - {d['title'][:58]}\n      {why}")
    if already:
        print(f"\nalready logged submitted ({len(already)}):")
        for d, _ in already:
            print(f"  = {d['title'][:58]}")

    if not to_log:
        print("\nnothing new to log")
        return
    if DRY:
        print("\n--dry: nothing written")
        return

    stamp = datetime.date.today().isoformat()
    src = os.path.join(BASE, "applications.jsonl")
    shutil.copy(src, f"{src}.bak-autocheck-{stamp}")
    print(f"\nbacked up applications.jsonl -> .bak-autocheck-{stamp}")

    for d, why, rec in to_log:
        url = rec.get("url") or d["url"]
        autofill.log_application(
            url, rec.get("ats"), "submitted",
            rec.get("title") or d["title"], rec.get("company"),
            note=f"auto-detected {stamp}: {why}")
        ok = autofill.already_applied(url) is not None
        print(f"  logged {'OK ' if ok else 'FAILED'} {(rec.get('company') or d['title'])[:45]}")

    print("\nre-run `python build_queue.py` - these will drop out of the queue")


if __name__ == "__main__":
    main()
