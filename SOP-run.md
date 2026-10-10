# SOP — running an application batch from a cold start

Written for whoever (or whatever) picks this up next session. Assumes nothing is
running. `README.md` explains what the pieces are; this is the order to do them
in and the traps that have actually bitten. Indeed specifics live in
`SOP-indeed.md`.

**The one-line summary: the bot fills, you submit.** Every ATS in this funnel —
Greenhouse, Lever, Ashby, applytojob — puts a CAPTCHA on the submit path for
every applicant. `--auto-submit` gates on CAPTCHA and therefore never fires. A
run that reports "0 submitted, N filled" is the system working, not failing.

---

## 0. Read these first

1. The newest `TODO-*.md` — current state, what's mid-flight.
2. `QUESTIONS-FOR-USER.md` — if he's filled any answers in, convert them to
   `profile.json` rules **before** the run, not after.

## 1. Start Chrome — with extensions off

```bash
powershell -File start_chrome.ps1
```

**`start_chrome.ps1` does not disable extensions, and it must for a Greenhouse
run.** The jobright Autofill extension is installed in `chrome-profile/` and
races the filler on `first_name` (`autocomplete="given-name"`), producing
`NameName`. Either add `--disable-extensions` to `$chromeArgs` in that
script, or launch by hand:

```bash
"/c/Program Files/Google/Chrome/Application/chrome.exe" --remote-debugging-port=9222 --user-data-dir="/path/to/jobsearch/chrome-profile" --no-first-run --no-default-browser-check --disable-extensions --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling about:blank
```

Confirm the port before doing anything else — every fill fails silently-ish
without it:

```bash
curl -s http://127.0.0.1:9222/json/version
```

The window starts minimized. Leave it that way until it's time to submit; the
three `--disable-*-backgrounding` flags are what keep a minimized window running
at full speed.

## 2. Check the resume the profile points at

```bash
python -c "import json;p=json.load(open('profile.json'));print(p['files']['resume'])"
```

Two places carry the filename, not one: `files.resume`, and the Indeed
"which resume" chooser rule under `answers[]`, which matches on the literal
string. Change both, newest first. Back up `profile.json` before editing —
it's hand-annotated with `_c`/`_note` keys, so edit the strings in place; never
round-trip it through `json.load`/`json.dump`.

Current: `Your_Name_Resume_2026_v3.pdf`. Build it from
`_build_resume_2026_v3.js` (node), then Word converts docx→PDF:
`$d.ExportAsFixedFormat($path, 17)`.

## 3. Build the queue

```bash
python collect_jobright.py --resolve --queue-out jobs_fresh.txt
python build_queue.py jobs_next.txt
```

`build_queue.py` is the dedupe pass: it reads every `jobs*.txt` plus every
`needs_you`/`submit_failed` URL in `applications.jsonl`, drops
`indeed.com/viewjob`, `smartapply.indeed.com` and `linkedin.com/jobs/view` (not
employer forms — this system has never driven them), drops anything already
logged `submitted` or `dead_link`, and collapses duplicates after stripping
`jr_id`/`token`/`utm_*`. `autofill.py` re-checks duplicates itself via
`already_applied()`, but only against status `submitted` — which is why step 7
matters.

If it prints `0 candidate ATS forms`, the funnel is genuinely empty; that is the
normal state after a batch goes out, not a bug.

Indeed needs a human to clear its bot challenge first (see `SOP-indeed.md`);
yield is about one automatable form in five.

## 4. Fill

```bash
python autofill.py --queue jobs_next.txt --fill-only --no-vision
```

- `--fill-only` — never prompts, never submits, leaves each tab open. The right
  mode for a CAPTCHA'd funnel.
- `--no-vision` — there is **no `ANTHROPIC_API_KEY`** in the environment or in a
  `.env` here, so the vision verification pass cannot run. Drop this flag only
  after putting a key in `jobsearch/.env`.
- Run it backgrounded and watch the log; ~1–2 minutes per form.

## 5. Fix the Greenhouse resume slot — required, every time

```bash
python fix_resume_slot.py greenhouse
```

**Why this is not optional:** Greenhouse renders **no `<input type=file>` at all**
once a file is showing in the widget. `set_input_files` has nothing to write to,
so the filler reports the upload as successful while the old file stays. The old
file is `resumai.pdf` (jobright's AI-tailored resume), held as Greenhouse-side
prefill in this Chrome profile — it comes back **even with extensions disabled**,
so turning off jobright does not fix it. The script clicks `Remove file`, waits
for the input to reappear, uploads, and reads the filename back.

## 6. Verify against the DOM, then hand over

```bash
python verify_tabs.py
```

Prints name / email / attached filename / CAPTCHA per open tab. **The fill report
is not evidence** — for the resume slot it is structurally incapable of being
right. A form you filled but didn't confirm the filename on is not done.

Do **not** reload a tab to check something. A reload wipes every other field.

Then tell the user: restore the Chrome window, work top to bottom, submit each
one before moving on. **Filled values do not survive ~30 minutes idle** — a batch
of open tabs is not a to-do list. Say plainly what's left per form, and which
questions were left blank on purpose.

## 7. After he submits — log it, or the next run re-applies

`--fill-only` logs everything as `needs_you`. Nothing marks them submitted, so
the next queue build offers all of them again and re-applies to jobs already
sent.

**Try the automatic paths first — in this order.**

```bash
python check_submitted.py
```

Reads the **open tabs**. Run it before he closes the window.

```bash
python check_submitted.py --history
```

**This is the one that survives a closed window**, and it is the answer to "is
there no way to auto-check?" — yes, there is. A submit navigates to a
confirmation URL, and Chrome's history outlives the tab, so a batch closed hours
ago is still recoverable. It copies `chrome-profile/Default/History` (locked while
Chrome runs), matches each confirmation URL back to a log record by shared token
plus host, and logs only explicit confirmations. Backs up `applications.jsonl`
first. On 2026-08-12 it recovered five of eight submissions with no input.

**What it cannot see, and this is the important limit:** an ATS that confirms
**in place without changing the URL** leaves no history trace at all. Confirmed
cases: **Ashby, BambooHR, Workable, Rippling**. Greenhouse (`/confirmation`),
Lever (`/thanks`), SmartRecruiters and Dayforce all navigate and are detected.
So a clean `--history` run is **not** evidence that the rest were not submitted —
ask him about the in-place ones specifically rather than assuming.

**Watch for one job holding two URLs.** CapTech was logged submitted on its
`oneclick-ui` form URL while its job-description URL stayed `needs_you`, so the
queue still offered it. If a job survives the queue build after being submitted,
log the other URL too.

Only if both automatic paths miss it, append records by hand using the module's
own function so the shape can't drift:

```python
import sys; sys.path.insert(0, r"/path/to/jobsearch")
import autofill
autofill.log_application(url, ats, "submitted", title, company, note="submitted by hand YYYY-MM-DD")
print(bool(autofill.already_applied(url)))   # must be True
```

Back up `applications.jsonl` first. Then `python autofill.py --report`.

---

## LinkedIn Easy Apply

`linkedin_easy.py`, same Chrome, same profile.json. Default is fill-only.

```bash
python linkedin_easy.py --limit 1
```

- Easy Apply is an `<a href=".../apply/?openSDUIApplyFlow=true">`, not a button;
  the driver navigates rather than clicks. The flow renders **inline**, not in a
  `[role=dialog]`.
- **It is not CAPTCHA-free** — the flow carries a `g-recaptcha-response` field
  (invisible v3). Don't touch it; report it.
- LinkedIn's email picker only offers addresses verified on the ACCOUNT
  (`linkedin-account@example.com`), not profile.json's `you@example.com`. The driver
  stops rather than send from an address the resume doesn't mention;
  `--accept-account-email` overrides.
- Screening questions are unfinished: the label association on that step is not
  what `collect_fields()` expects, so the step will not advance. Fix that before
  promising volume here.
- Driving LinkedIn's UI can get an account restricted. Never spoof a user agent,
  never touch a checkpoint screen — stop and hand it over.
- **Go slow.** the user has tripped LinkedIn's flags himself by moving too fast,
  so the driver waits 60s between postings, 6s after each navigation and 5s
  between steps. `--pace` raises the gap between postings; there is no reason to
  lower it. This is throttling — fixed, generous waits that put *less* load on
  LinkedIn than a fast human — not jitter tuned to look human, which would be
  evasion and is not what this does.
- Small runs. `--limit 3` at 60s spacing is a fifteen-minute session; that is the
  right shape here, not a fifty-job sweep.

## Single-page apps (ripplehire and friends)

`autofill.py` decides "this doesn't look like an application form" before the SPA
has rendered one. If a queue entry comes back with 0 fields filled but the tab
clearly shows a form, fill the OPEN TAB in place rather than re-running the URL:
connect over CDP, find the page, and call `autofill.autofill_page()` on it
directly. Expect the SPA to drop sections between passes — these are manual.

## Standing rules — do not relitigate these

- **Never assert an employment record for him.** Anything hinging on
  *professionally*, on years of experience, or on a degree goes in
  `QUESTIONS-FOR-USER.md` and stays blank until he answers.
- **Consent boxes: only what `consent_policy.auto_tick` names.** As of 2026-08-08
  the user opted in to privacy notices, data-processing consent, NDA agreement
  and terms-of-use acceptance. Everything else is left for him, and **a checkbox
  that shares its question with other checkboxes is never auto-ticked in any
  mode** — a required multi-select marks every option required, and ticking them
  all put Pinecone, Ollama, Milvus and "None of the above" on one form at once.
  `test_consent_scope.py` pins this; run it after touching that branch.
- **Never invent a resume number.** Every figure on the resume traces to
  something real or to the role dates. Unknowns go in section 4 of the questions
  file.
- **Don't try to route around a CAPTCHA**, and don't clear Indeed's bot
  challenge programmatically. Both are his to do by hand.
- **`--dry` under-reports CAPTCHA on Ashby.** reCAPTCHA v3 is invisible until
  submit, and the detector looks for the response field. A clean dry report there
  does not mean submittable.

## Known-bad, not yet fixed in code

- `fix_resume_slot.py` is a separate pass because `autofill.py` still can't
  overwrite an occupied Greenhouse upload widget. Folding remove-and-reattach
  into the file-field branch of `run_one()` would make step 5 unnecessary.
- `start_chrome.ps1` still doesn't pass `--disable-extensions` (step 1).
- **No `answers[]` rule can fill a checkbox.** In the `checkbox`/`radio` branch
  (`autofill.py:857`) the rule loop is guarded by `if ftype == "radio" and group`
  — checkboxes skip it entirely and land in `needs_you`, or in `consent[]` if the
  form marks them required. Radios *are* rule-matched (that changed after the
  07-31 note said otherwise). Consequence: the "which LLM providers" multi-select
  on Bjak goes out blank no matter what's in the profile, and every optional
  consent box stays untouched by design.
