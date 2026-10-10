# SOP — Indeed SmartApply (`smartapply.indeed.com`)

Written from walking real applications, not from documentation. Everything
below was observed; where something is unverified it says so.

## What this path does

Fills each step, looks at what landed, clicks through, **stops at the review
screen**. Indeed's send control is a different testid from the advance button,
and every advance candidate must fail the send deny-list before it is clicked.

`--auto-submit` **does** reach this path (`main` → `run_one` → `run_stepper`),
and is the only thing that can make it press send. A previous version of this
document said the flag could not reach the stepper at all. That was wrong — the
behaviour was always correct, the sentence was not. `gui.py` does not pass the
argument, so the GUI never submits under any circumstances.

## Answering, in two layers

1. **Rules** — `answers[]` regexes matched against the question, values matched
   against the options by `radio_matches()` (exact, or a prefix ending on a
   word boundary).
2. **Vision** (`vision_answer.py`) — after filling, each screenful is
   photographed and read back. This exists because "a rule fired" and "the
   right answer is on the screen" are different claims, and one form showed
   both failures at once: a 762-character STOCK Act question too long for the
   DOM extractor to see (neither option selected, reported as `[radio] Yes`),
   and a government-employment question Indeed had **pre-filled `Yes` from a
   previous application** — false, and invisible to code that only asks whether
   *something* is selected.

Precedence is fixed and matters:

| Question | Authority |
|---|---|
| what is ticked right now | the **DOM** (`e.checked`) — a fact |
| what the answer should be | your **`answers[]` rule**, whenever one applies |
| what the answer should be, with no rule | the **model**, high confidence only |
| whether the picture agrees with the DOM | reported, never acted on |

The model cannot overrule a rule you wrote: where they disagree the page is
left as your rule says and the disagreement is printed. It cannot click a
button, reach the advance allow-list or the send deny-list, or make a form
submittable — unresolved answers now *hold back* `--auto-submit`. Its answers
go through `radio_matches()` like any other, so a paraphrase that matches no
option ticks nothing and is reported. `UNKNOWN` is a valid answer and means the
question is yours.

Turn it off with `--no-vision`. With no `ANTHROPIC_API_KEY`, no `anthropic`
package or no network it degrades to rules-only and says so on the first line
of the run.

## The flow

| # | URL segment after `/form/` | What's on it |
|---|---|---|
| 1 | `resume-selection-module/resume-selection` | radios: Indeed Resume vs an uploaded file; hidden file input |
| 2 | `resume-module/structured-data-intro` | no fields; button relabels to **Review details** |
| 3 | `questions-module/questions/N` | employer + ATS questions, mostly radios |
| 4 | `review-module` | **`[data-testid="submit-application-button"]` — never click** |

Step 3 repeats (`/questions/1`, `/2`, …) on jobs with more questions.

## Selectors

- **Advance:** `[data-testid="continue-button"]` — correct on most steps.
- **Advance, fallback:** the questions module **randomises `data-testid` and
  `class` per render** (seen: `data-testid="30e6dd3d81aa4705705e2ef002ff579b"`).
  The testid selector silently finds nothing there, so `find_advance()` falls
  back to matching the button's *label* against an allow-list
  (`continue|next|save and continue|review details`) — and every candidate,
  on both paths, must also fail the send deny-list (`submit|send application|
  apply now`) before it is clicked.
- **Enter the flow:** `#indeedApplyButton` on `indeed.com/viewjob?jk=…`.
  Opens in a popup tab usually, same tab occasionally — handle both.

## Hazards — each of these was a real bug, not a hypothetical

1. **Substring matching on radios answers the wrong option.** The value
   `"Yes - U.S. citizen, no sponsorship required"` contains `no`, so it matched
   **both** Yes and No on *"Are you legally authorized to work in the United
   States?"*, and the later tick won — answering **No**. Radios are now matched
   **exactly** against `answers[]` values. Never relax this.
2. **A radio's own label is the option, not the question.** `[radio] Yes` tells
   you nothing. The question comes from the fieldset legend / role=group /
   nearest enclosing text — that's `info["group"]`, and it's what rules match.
3. **Scroll before reading.** Content below the fold may not be rendered. An
   unrendered question is absent from the DOM, so it is missed *and* missing
   from the report, and the step then fails validation for no visible reason.
4. **`g-recaptcha-response` is present on every step** (invisible v3). It means
   nothing here, so the CAPTCHA warning is suppressed on this path — reporting
   it every step would train you to ignore it on forms where it's real.
5. **Indeed stores its own resume.** Step 1 offers "Use your Indeed Resume"
   alongside any file. The stepper uploads `files.resume`, which then appears
   as an option — but **neither radio is auto-selected**, so confirm which one
   is chosen before submitting.
6. **You are not necessarily the only thing writing to that page.** Measured on
   this machine, in the `chrome-profile` debug Chrome, with a plain local HTML
   form open and *nothing* driving it: about five seconds after load, **trusted**
   `mousedown`/`click`/`change` events selected "Yes" on a criminal-history
   question, then "No", then clicked a button. `isTrusted: true` means they came
   through the browser's own input path — a CDP client attached to port 9222,
   not an ordinary extension content script. Separately, **"Jobright Autofill —
   Instant Job Applications"** is installed in that profile with content scripts
   on `<all_urls>`.

   So an answer verified thirty seconds ago is not necessarily the answer on
   screen when Continue is pressed. `guard_before_advance()` re-reads every
   rule-decided question straight from the DOM immediately before the click and
   puts back anything that moved. Before trusting a live run, make sure nothing
   else is attached to port 9222 and consider disabling that extension in the
   debug profile — otherwise a second writer can answer a question nobody asked
   it to answer.

## Stopping conditions, and what each means

| Outcome | Meaning | What to do |
|---|---|---|
| `review` | reached the review screen — the goal | read it in Chrome, submit yourself |
| `stuck` | Continue clicked, page didn't move | a required question is unanswered — the report lists them |
| `no_continue` | no advance button found | unfamiliar screen; look at it |
| `max_steps` | 12 steps without reaching review | probably looping; look at it |

`stuck` is the common one and it is **not** a failure of the tool — it means
the form is waiting on something only you should decide.

## What stays manual, on purpose

**Checkbox** consents are never ticked — by rules or by vision. That is the
real guard, and it is why a required consent checkbox reads as `stuck`.

Consent **radios** are a different story, and the old version of this section
was wrong about them: `answers[]` carries deliberate standing entries that *do*
answer several, and they will fire.

- *"I declare that all statements and answers in this application are true…"* —
  **answered** ("I understand" / "I agree"), by an entry you wrote
- ATS terms-of-service *"I understand"* — **answered**, same way
- text-message / email marketing opt-ins — **answered "No"**, by design
- *"Have you been employed by \<company\> before?"* — **answered "No"**, on the
  standing assumption that your only employers are Accentian and your own
  practice. Delete that entry before applying to a company you have worked for.

Anything in `never_autofill` or the hard-skip list — criminal history, date of
birth, SSN — is off-limits to **both** layers. Until the vision layer existed
that guard did not actually cover radio groups: it was tested against a radio's
label, which is only ever "Yes".

To automate a question you *are* comfortable automating, add an `answers[]`
entry whose `match` hits the **question** text and whose `value` **exactly**
equals the option label:

```json
{ "match": "legally authorized to work|authorized to work",
  "value": ["Yes", "Yes - U.S. citizen, no sponsorship required"] }
```

The bare option first (radios need exact), the sentence after (free-text
boxes). First rule whose `match` hits the question wins.

## Checking a change to this path

1. `python autofill.py "https://www.indeed.com/viewjob?jk=<jk>" --dry` —
   enters the flow, fills nothing, reports step 1.
2. Real run on **a job already started**, not a fresh employer, so you aren't
   opening applications to iterate.
3. Confirm the outcome line matches what the screen actually shows.
4. Never test by letting it reach a submit button "to see what happens".