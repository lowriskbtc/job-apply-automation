# jobsearch

Autofills job application forms from a local profile, then stops so you can read
and submit. Built and tested against live Greenhouse and Lever forms.

## What this actually is

jobright's Apply button says **"Apply on Employer Site"** — it deep-links to the
company's own ATS (Greenhouse, Lever, Ashby, Workday). So jobright is a *source
of URLs*, and the form-filling happens on the employer's site.

- `Start-GUI.bat` / `gui.py` — the window: job list, fill, mark, jump around
- `Start.bat` / `run.py` — the terminal menu, same jobs one at a time
- `collect_jobright.py` — pulls your jobright matches into a local queue
- `collect_indeed.py` — same, for an Indeed keyword search
- `autofill.py` — fills one application form, reports, stops
- `applications.jsonl` — what you applied to, written as you go

## Setup

```bash
python -m pip install playwright
```

No `playwright install` needed — it drives a Chrome you already have.

1. Copy `profile.example.json` to `profile.json` and fill it in.
2. Put your resume somewhere and point `files.resume` at it.
3. Start the debug Chrome:

```bash
powershell -ExecutionPolicy Bypass -File start_chrome.ps1
```

Chrome 136+ refuses remote debugging on your normal profile, so this uses a
dedicated one in `chrome-profile/`. **First run, sign in to jobright in that
window** — it sticks after that.

## Use

**Double-click `Start-GUI.bat`.** A window with the whole job list on the
left and the current one on the right:

```
 [Start Chrome] [Find jobs...] [Refresh]            chrome: connected
 +----------------------------------+--------------------------------+
 | OK Senior AI Engineer   Acme     | Senior AI Engineer             |
 |    AI Engineer          Wayy ... | Acme - https://boards....      |
 |    Staff Engineer       Cisco    | [Fill form][Preview][Open]     |
 |    Consultant, SWE      Nation.. | [Mark submitted][Skip] [Prev][Next]
 |                                  |                                |
 |                                  | filled (6)                     |
 |                                  |    + Phone number <- +1 520... |
 |                                  | yours to handle (1)            |
 |                                  |    > [checkbox] I agree        |
 +----------------------------------+--------------------------------+
 8 jobs   1 submitted   5 Indeed-hosted (apply by hand)
```

Click any job in any order, fill it, look at it in Chrome, mark it, move on.
Nothing is submitted for you — **Fill form** fills and stops, exactly like the
terminal version. Indeed-hosted jobs stay in the list greyed out with their
Fill button disabled, because there's no employer form to drive; use **Open in
Chrome** and do those by hand.

**Preview only** is the `--dry` equivalent: it reports what would be filled
and types nothing.

The browser runs on its own thread, so the window stays responsive while a
page loads instead of freezing for the twenty seconds a slow careers site
takes.

### Or the terminal menu

**Double-click `Start.bat`** for a menu that knows the flags and the
order, and shows where you are:

```
  you      Your Name
  chrome   running on 9222
  queued   12 left of 25 (Indeed)   3 left of 8 (jobright)
  applied  6 submitted   4 needs_you   2 dead_link

  1   Find jobs on Indeed
  2   Find jobs on jobright
  3   Fill applications        you read each form and submit it yourself
  4   Hands-off mode           auto-submits ONLY the forms with nothing unanswered
  5   Report - what you've applied to
  6   Start Chrome
```

"12 left of 25" is jobs you haven't applied to yet, not lines in a file — the
queue is checked against `applications.jsonl`, so re-running a queue picks up
where you left off instead of walking the ones already sent.

Option 4 asks you to type `send` before it will submit anything. Everything
else stops and hands the form to you.

Your **first** Indeed run skips the apply-link lookup on purpose. That step
opens every job page one at a time — it's the slow part and the part most
likely to get challenged — so the first pass just harvests cards, which is
enough to prove the search still reads correctly. Check the titles and
companies look right, then run option 1 again and say yes.

The menu is only a shortcut; every script below still works on its own, and
nothing in the menu does anything they wouldn't.

```bash
python autofill.py https://job-boards.greenhouse.io/acme/jobs/123
```

Fills the form in front of you, prints what it did, and waits. You read it, fix
what it flagged, submit in Chrome, then answer the prompt so it gets logged.

```bash
python collect_jobright.py --resolve --limit 20
python autofill.py --queue jobs.txt
python autofill.py --report
python autofill.py URL --dry      # report only, touches nothing
```

## Indeed

```bash
python collect_indeed.py --what "AI engineer" --where Remote --resolve
python autofill.py --queue jobs_indeed.txt
```

`--what` / `--where` / `--days` default to the `search` block in
`profile.json`, so the usual run is just `python collect_indeed.py --resolve`.

Indeed jobs come in two kinds and the collector separates them for you:

| Apply button | Where it goes | Queued? |
|---|---|---|
| "Apply on company site" | Greenhouse / Lever / etc | yes — `jobs_indeed.txt` |
| "Apply now" | `smartapply.indeed.com` | **no** |

Every job is recorded in `jobs_indeed.jsonl` with its `apply_kind` either way,
and the run prints the split. **Indeed-hosted applications are not supported**
— see below.

Two things to expect:

- **Indeed challenges automated browsers.** Pointed at `/jobs`, a browser
  under automation control gets Cloudflare's "Just a moment..." page instead
  of results. Your own signed-in Chrome from `start_chrome.ps1` is a normal
  profile with normal cookies and usually gets through, but when it doesn't
  the collector **stops with the URL** rather than writing an empty queue —
  an empty queue and "no jobs today" look identical otherwise. Clear the
  challenge yourself in that window and re-run.
- The collector **reads the Apply button without clicking it** when the job is
  Indeed-hosted, because clicking starts Indeed's wizard and can mark the job
  as begun.

### Why Indeed-hosted applications aren't automated

`autofill.py` fills **one page and judges it once**: "nothing filled" means
this isn't a form, the required-checkbox list is gathered in a single sweep,
and unanswered-required is a single verdict. Indeed's wizard is a multi-step
stepper, so each of those checks would have to become per-step plus an
aggregate gate before the final submit. That's a rewrite of the part that
decides whether it's safe to submit, and the failure mode is a real
application sent wrong. Those jobs are yours to do by hand.

## Different resumes for different jobs

`files.resume_by_keyword` picks a resume from the job title. `match` is a
regex, first match wins:

```json
"resume_by_keyword": [
  { "match": "cannabis|extraction|dispensary", "resume": "C:\\...\\canna.pdf" },
  { "match": "\\bai\\b|machine learning|llm",  "resume": "C:\\...\\ai.pdf" }
]
```

Leave it empty and every application gets `files.resume`. A rule that matches
but points at a file that isn't there is **reported**, then falls back — being
quietly sent the generic resume to the one job you tailored for is the failure
you'd never catch.

What the regex actually sees is the **page title plus company**, not a clean
job title — the browser tab says `Software Engineer - Acme | Greenhouse`, and
that whole string is what gets matched. So keep the patterns loose (`cannabis`,
`\bai\b`) rather than anchoring them.

## Hands-off mode

```bash
python autofill.py --queue jobs.txt --auto-submit --max 15
```

Fills, ticks the checkboxes the form *requires* to proceed (logging exactly
which), submits, and confirms it landed. It refuses — and logs why — when:

- there's a **CAPTCHA** on the form
- any **required** question came out unanswered
- any **required** field failed to resolve (dropdown with no matching option,
  location typeahead that didn't commit)

So a queue splits itself: the clean ones go out unattended, the rest land in
`applications.jsonl` as `needs_you` for you to finish in one sitting:

```bash
python autofill.py --report
```

`--max` caps submissions per run (default 25) so a bad profile can't fire off
fifty applications before you notice.

A `TODO` value in `profile.json` counts as **unanswered**, not as an answer —
it will never be typed into a form, and it blocks auto-submit on that field.

## What it will not do

**It will not get past a CAPTCHA.** No solver service is wired in and none will
be. Those forms come back to you. Beyond that:

- Both Greenhouse and Lever put a **CAPTCHA** on the submit path
  (`g-recaptcha-response`, `h-captcha-response`). Nothing here solves those.
- Some forms carry deliberate bot traps. The Metabase posting used for testing
  asks *"What is the application code found in the browser console"* — a
  question only a human reading the page can answer.
- Screening questions ("are you enrolled in a program", "1st choice: area of
  interest") are real questions. It reports them rather than guessing.

It also leaves blank by default:

- Radio buttons, and every checkbox outside `--auto-submit` mode
- Anything labelled "if different from above" / "if applicable"
- Whatever is listed in `never_autofill` — currently disability status, DOB,
  SSN, and criminal-history questions

Everything about what gets answered lives in `profile.json`, not in the code.
An explicit `answers[]` entry **overrides** `never_autofill`, which is how
salary and the voluntary self-ID questions get answered while the skip rules
stay in place for forms you haven't thought about.

Self-identification (gender, ethnicity, veteran status) is answered because
those values are in `answers[]`. Delete those entries and the fields go back to
being left blank — every form also offers a decline-to-answer option.

## Adding your own answers

`answers[]` in `profile.json` is checked before everything else. `match` is a
regex tested against the field's label:

```json
{ "match": "why (are|do) you (interested|want)", "value": "..." }
```

**`value` can be a list** — acceptable answers in order of preference. A
dropdown only offers what it offers, so a single string loses constantly:
"Job board" is useless on a form whose options are LinkedIn / Indeed / Other.
The first entry the dropdown actually has wins.

```json
{ "match": "how did you hear", "value": ["LinkedIn", "Job board", "Other"] }
```

This matters most for the self-ID questions, where every ATS words the same
answer differently ("No, I don't have a disability" vs "No, I do not have a
disability and have not had one in the past").

Anything it can't match and that the form marks required shows up under
"required but no rule matched" — that list is your to-do for the next posting.

## Verified behaviour

Tested end to end against two live forms, checking the DOM afterwards rather
than trusting the script's own report:

| | Greenhouse (Cloudflare) | Lever (Metabase) |
|---|---|---|
| name / email / phone | yes | yes |
| resume upload | yes | yes |
| react-select dropdowns | yes | n/a |
| location typeahead | yes | flagged — pick it yourself |
| custom questions | matched by label | matched by label |

Two things worth knowing about how it was built:

- `.fill()` does **not** open a react-select menu; only real typing does. An
  earlier version reported those dropdowns as filled while leaving them empty.
  It now verifies against the options actually on offer, and when your answer
  isn't among them it prints the real list instead of guessing.
- Greenhouse renders a hidden `requiredInput` twin beside each dropdown.
  Writing to it looks successful and leaves the form in a state the site
  rejects, so those are skipped.

## Notes

- Custom-question field ids (`question_68005611`, `cards[<uuid>][field0]`) are
  per-posting. Matching is on label text, which is why it survives new postings.
- Duplicate detection keeps the job id out of the query string
  (`jk`, `gh_jid`, …). It used to drop the whole query, so every
  `indeed.com/viewjob?jk=…` normalised to `indeed.com/viewjob` — one submitted
  Indeed job would have made every later one read as already-applied and get
  skipped. Tracking params (`utm_*`, `from=`, `vjs=`) are still stripped, so
  genuine repeats still collapse to one.
- Phone boxes (`intl-tel-input`, `react-phone-number-input`) seed the field
  with the dial code before you type. That made the field read as *already
  filled*, so it was skipped without ever being listed as unanswered — forms
  went out with no phone number and the report said nothing. A bare `+N` is
  now treated as scaffolding rather than an answer.
- Many modern forms wire nothing at all: no `for=`, no `id`, no `name`, no
  `aria-label`. The caption is just text sitting above the box. Those get a
  label by walking up the wrappers, but **only** past elements that hold no
  other question's control — an earlier version searched too wide and gave an
  unrelated free-text box the label "First Name", which would have typed a
  name into it.
- Text fields are read back after filling, so the report shows what is
  actually in the box. A phone widget rewrites `(555) 555-0100` into
  `+1 555 555 0100`, and a React-controlled input can discard the write
  entirely — claiming success without looking is how a field ends up empty on
  a form you were told was ready.
- Blast-applying converts badly. The value here is the fifteen-minutes-to-one
  per application, not doing three hundred of them.

### Not carried over from the old Selenium bot

The previous `job-application-bot` was Selenium + `undetected_chromedriver`.
Deliberately left behind:

- **Login automation.** It stored an Indeed password in `.env` and typed it
  into the sign-in form. This project attaches to a Chrome you signed into
  yourself, so there is no password anywhere in it.
- **`_fill_application_form`.** It uploaded to the first file input, typed the
  cover letter into the first textarea, and clicked the first submit button —
  no labels read, no verification, and it returned `True` for "I clicked
  something".
- **`llm_helper.py`.** An Ollama call asking a model which button to press,
  complete with a `confidence` score. Guessing at an unrecognised control is
  the specific thing this project doesn't do; unmatched fields get reported.

Kept: the keyword→resume idea (now `files.resume_by_keyword`, regex instead of
three hundred hardcoded keywords) and jittered delays between jobs.
