#!/usr/bin/env python
"""
vision_answer.py - read the application form the way you would: by looking at it.

The rule layer in autofill.py infers a question from the DOM and matches a regex
against it. That answers "did a rule fire", which is not the same question as
"is the right answer on the screen". Two real failures came out of that gap on
one ServiceNow form:

  * a 762-character federal-employment question (STOCK Act) whose text was too
    long for the DOM extractor, so no rule was ever tested and neither option
    was selected - the actual blocker, reported to the operator as "[radio] Yes";
  * "have you been an employee of a state, local, or municipal government in
    the past 5 years", which Indeed had PRE-FILLED "Yes" from a previous
    application. Wrong for this user, and invisible to code that only ever
    asks whether something is selected.

So this module looks at the pixels. Two calls, deliberately separate:

  verify_screenshot()   what is ACTUALLY selected right now, vs what we meant
  answer_from_screenshot()  what the correct answer is, from profile facts only

Everything here is advisory. It returns text. It never clicks, never names a
selector, never decides that a form may be submitted. autofill.py takes the
verdict and applies it through the same radio_matches() gate a rule goes
through, or reports it and leaves it alone.

Degradation is total and silent-by-design at the boundary: no key, no
`anthropic` package, no network, a bad response - every function returns [] and
VISION_AVAILABLE goes False. A run must never crash because a model was
unreachable; it falls back to exactly the behaviour it had before this file
existed.
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"

# Fast enough to run per pane, good enough to read a radio. The hard model is
# used only when the fast one says it is unsure.
MODEL_FAST = "claude-sonnet-5"
MODEL_HARD = "claude-opus-5"

# A run is a handful of steps with a couple of panes each. This cap exists so a
# loop that goes wrong costs cents rather than dollars, and so an outage that
# manifests as slow retries cannot stall a queue of applications indefinitely.
MAX_CALLS = int(os.environ.get("VISION_MAX_CALLS", "60") or 60)

# Claude Code (and other harnesses) export ANTHROPIC_BASE_URL to point the SDK
# at a local proxy. If autofill.py is launched from such a shell the SDK picks
# it up silently and vision calls go somewhere unintended. Pin the real API
# unless the operator overrides it deliberately with a different variable.
BASE_URL = os.environ.get("ANTHROPIC_VISION_BASE_URL") or "https://api.anthropic.com"

VISION_AVAILABLE = True          # flipped False by the first hard failure
LAST_ERROR = ""

_calls = 0
_fails = 0                       # consecutive; see _ask
_client = None
_client_key = None


# --------------------------------------------------------------------------
# key + client
# --------------------------------------------------------------------------

def _env_file_key() -> str:
    """ANTHROPIC_API_KEY out of a .env sitting next to this file, if any."""
    try:
        if not ENV_PATH.exists():
            return ""
        for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip().upper() in ("ANTHROPIC_API_KEY", "ANTHROPIC_KEY"):
                return val.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def load_key(api_key: str | None = None) -> str:
    return (api_key or os.environ.get("ANTHROPIC_API_KEY") or _env_file_key() or "").strip()


def _fail(reason: str) -> list:
    """Record why vision is off and answer with nothing. Never raises."""
    global VISION_AVAILABLE, LAST_ERROR
    VISION_AVAILABLE = False
    LAST_ERROR = reason
    return []


def get_client(api_key: str | None = None):
    """An Anthropic client, or None. Constructing it never raises."""
    global _client, _client_key
    key = load_key(api_key)
    if not key:
        _fail("no ANTHROPIC_API_KEY in the environment or .env")
        return None
    if _client is not None and _client_key == key:
        return _client
    try:
        import anthropic                       # imported here, never at module scope
    except Exception:
        _fail("the `anthropic` package is not installed (pip install anthropic)")
        return None
    try:
        _client = anthropic.Anthropic(api_key=key, base_url=BASE_URL, max_retries=2)
        _client_key = key
        return _client
    except Exception as exc:
        _fail(f"could not create the Anthropic client: {type(exc).__name__}")
        return None


def status() -> str:
    """One line for the operator: is vision on, and if not, why not."""
    if not VISION_AVAILABLE:
        return f"vision OFF ({LAST_ERROR or 'unavailable'})"
    if not load_key():
        return "vision OFF (no ANTHROPIC_API_KEY)"
    host = BASE_URL.replace("https://", "").replace("http://", "")
    warn = f"  [last error: {LAST_ERROR[:80]}]" if LAST_ERROR else ""
    return (f"vision on ({MODEL_FAST} via {host}, "
            f"{_calls}/{MAX_CALLS} calls used){warn}")


# --------------------------------------------------------------------------
# what the model is allowed to know
# --------------------------------------------------------------------------

def profile_facts(profile: dict) -> str:
    """
    The ONLY ground truth the model may answer from.

    Everything in here comes out of profile.json. Nothing is inferred, nothing
    is padded out with plausible-sounding biography - if a fact is not in this
    block the model is required to return UNKNOWN, and that is the whole point:
    an invented employment history on a real application is worse than a form
    that stops and waits for you.
    """
    ident = profile.get("identity", {}) or {}
    work = profile.get("work", {}) or {}
    lines = ["FACTS ABOUT THE APPLICANT (the only ground truth you have):"]

    def put(label, value):
        if value:
            lines.append(f"- {label}: {value}")

    put("Name", ident.get("full_name"))
    put("Lives in", ", ".join(x for x in (ident.get("city"), ident.get("state"),
                                          ident.get("country")) if x))
    put("Current work", " / ".join(x for x in (work.get("current_title"),
                                               work.get("current_company")) if x))
    put("Legally authorized to work in the United States", work.get("work_authorized"))
    put("Requires visa sponsorship", work.get("requires_sponsorship"))
    put("Willing to relocate", work.get("willing_to_relocate"))
    put("Willing to work on-site / commute", work.get("willing_to_relocate"))
    put("Earliest start date", work.get("notice_period"))
    put("Years of professional experience", work.get("years_experience"))

    for f in profile.get("facts", []) or []:
        if isinstance(f, str) and f.strip():
            lines.append(f"- {f.strip()}")

    decided = []
    for ans in profile.get("answers", []) or []:
        match, val = ans.get("match"), ans.get("value")
        if not match or not val:
            continue
        vals = val if isinstance(val, list) else [val]
        vals = [str(v) for v in vals
                if v and not str(v).strip().upper().startswith("TODO")]
        if not vals:
            continue
        decided.append(f'- if the question is about /{match}/ -> answer "{vals[0]}"'
                       + (f' (also acceptable: {", ".join(vals[1:4])})' if len(vals) > 1 else ""))
    if decided:
        lines.append("")
        lines.append("ANSWERS THE APPLICANT HAS ALREADY DECIDED (authoritative, "
                     "use these when the question is the one described):")
        lines.extend(decided[:40])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# calling
# --------------------------------------------------------------------------

def _image_block(png_bytes: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png",
                   "data": base64.standard_b64encode(png_bytes).decode("ascii")},
    }


def _extract_json(text: str):
    """The first JSON array in the reply. Prose around it is tolerated."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _ask(system: str, blocks: list, api_key: str | None, model: str,
         max_tokens: int = 2000):
    """One API call. Returns a parsed list, or None on any failure."""
    global _calls, _fails, LAST_ERROR
    if not VISION_AVAILABLE:
        return None
    if _calls >= MAX_CALLS:
        _fail(f"hit the {MAX_CALLS}-call vision budget for this run")
        return None
    client = get_client(api_key)
    if client is None:
        return None
    try:
        _calls += 1
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": blocks}],
        )
    except Exception as exc:
        # A revoked key, an exhausted balance or a dropped network connection
        # must not take a run with it. Turn vision off and let the caller carry
        # on with the rule layer alone.
        LAST_ERROR = f"{type(exc).__name__}: {str(exc)[:160]}"
        if re.search(r"authentication|permission|credit|not_found|invalid.?api",
                     LAST_ERROR, re.I):
            return _fail(LAST_ERROR) or None
        _fails += 1
    else:
        text = "".join(getattr(b, "text", "") for b in getattr(resp, "content", []) or [])
        data = _extract_json(text)
        if data is not None:
            _fails = 0
            return data
        LAST_ERROR = f"could not read a JSON array out of the reply: {text[:120]!r}"
        _fails += 1

    # Three failures in a row is a broken configuration, not a bad moment - a
    # deprecated parameter, a model id that no longer exists, a proxy in the
    # way. Left alone it would spend the whole budget returning nothing while
    # status() still cheerfully said "on", and every question would silently
    # fall back to the rule layer with nobody told why. Stop and say so.
    if _fails >= 3:
        _fail(LAST_ERROR or "three consecutive failures")
    return None


def _hint_lines(questions_hint) -> str:
    """Render the DOM's view of the page for the model to line itself up with."""
    out = []
    for i, q in enumerate(questions_hint or [], 1):
        if isinstance(q, str):
            out.append(f"{i}. QUESTION: {q}")
            continue
        out.append(f"{i}. QUESTION: {str(q.get('question', ''))[:600]}")
        opts = q.get("options") or []
        if opts:
            out.append("   OPTIONS (copy one of these verbatim as your answer): "
                       + " | ".join(str(o)[:80] for o in opts))
        if q.get("selected"):
            out.append(f"   DOM thinks selected: {q['selected']}")
        if q.get("intended"):
            out.append(f"   WE INTENDED: {q['intended']}")
    return "\n".join(out)


def _same(option: str, value: str) -> bool:
    """
    Mirror of autofill.radio_matches - exact, or the option BEGINS with the
    answer at a non-alphanumeric boundary. Duplicated rather than imported
    because autofill.py imports this module; if you change one, change both.
    Never loosen it: "Yes - U.S. citizen, no sponsorship required" contains
    "no", and a floating substring test once answered a work-authorization
    question with No on a real application.
    """
    o = (option or "").strip().lower()
    v = (str(value) or "").strip().lower()
    if not o or not v:
        return False
    if o == v:
        return True
    return o.startswith(v) and not o[len(v):len(v) + 1].isalnum()


# --------------------------------------------------------------------------
# 1. what is the right answer?
# --------------------------------------------------------------------------

ANSWER_SYSTEM = """You are reading a screenshot of one page of a job \
application form, on behalf of a specific applicant. Your job is to say what \
the correct answer to each question is - not to fill anything in, not to \
advance the form, not to submit it. You have no controls.

RULES, in order of importance:
1. Answer ONLY from the FACTS block supplied below. If the facts do not state \
or directly entail the answer, the answer is exactly "UNKNOWN". A question you \
leave UNKNOWN is handed to the human, which is a good outcome. A question you \
guess at is a false statement on a real job application, which is not.
2. NEVER invent or extrapolate employment history, employers, dates, \
citizenship, immigration or visa status, security clearances, education, \
licences, criminal history, disability, or military service. If the facts do \
not list it, you do not know it.
3. "The applicant probably..." is not derivation. Neither is "most people...". \
Neither is "the form pre-selected Yes, so Yes". A pre-filled answer is \
evidence of nothing - it is often carried over from an unrelated application \
and being wrong is exactly why you are being asked.
4. Your "answer" MUST be copied VERBATIM from that question's OPTIONS list \
when one is supplied - character for character, including case ("NO" not \
"No"). If none of the offered options expresses the correct answer, return \
"UNKNOWN".
5. confidence is "high" only when a fact states the answer outright or the \
answer follows from it with no additional assumption; "medium" when it follows \
but needs an inference; "low" otherwise. Only "high" will be acted on.
6. If a question is about salary, demographics (race, gender, disability, \
veteran status), criminal history or anything else the FACTS block does not \
explicitly decide, return UNKNOWN.

Return ONLY a JSON array, no prose:
[{"question": "<first ~80 chars of the question as printed on screen>",
  "answer": "<exact option text, or UNKNOWN>",
  "confidence": "high|medium|low",
  "reason": "<which fact decided it, one short sentence>"}]
One object per question visible in the screenshot."""


def answer_from_screenshot(png_bytes: bytes, questions_hint, profile: dict,
                           api_key: str | None = None, hard: bool = False) -> list:
    """
    [{question, answer, confidence, reason}] - answer may be the literal
    "UNKNOWN". Returns [] if vision is unavailable for any reason.

    `questions_hint` is the DOM's own reading of the page: a list of
    {question, options[], selected, intended}. It exists so the model answers
    with an option string that actually exists on the page; the screenshot, not
    the hint, is what it reads the state from.
    """
    if not png_bytes or not VISION_AVAILABLE:
        return []
    try:
        system = ANSWER_SYSTEM + "\n\n" + profile_facts(profile)
        prompt = ("Here is the form as it looks right now, and the questions the "
                  "DOM reports on this screen:\n\n"
                  + (_hint_lines(questions_hint) or "(no DOM hint - read the image)")
                  + "\n\nFor each question, give the correct answer for THIS "
                    "applicant from the facts. UNKNOWN is the right answer "
                    "whenever the facts do not settle it.")
        data = _ask(system, [_image_block(png_bytes), {"type": "text", "text": prompt}],
                    api_key, MODEL_HARD if hard else MODEL_FAST)
        if data is None:
            return []
        out = []
        for row in data:
            if not isinstance(row, dict):
                continue
            ans = str(row.get("answer", "")).strip()
            conf = str(row.get("confidence", "")).strip().lower()
            if conf not in ("high", "medium", "low"):
                conf = "low"
            out.append({
                "question": str(row.get("question", "")).strip()[:300],
                "answer": ans if ans else "UNKNOWN",
                "confidence": conf,
                "reason": str(row.get("reason", "")).strip()[:200],
            })
        return out
    except Exception:                            # belt and braces - never raise
        return []


# --------------------------------------------------------------------------
# 2. what is actually on the screen?
# --------------------------------------------------------------------------

VERIFY_SYSTEM = """You are checking a screenshot of a job application form \
that a tool has just filled in. Report what is ACTUALLY SELECTED on screen, \
question by question.

This is a reading task, not a judgement task:
- Look at the radio buttons and checkboxes in the image. A selected radio is \
the filled/darkened one. If NO option of a question is selected, on_screen is \
the empty string "" - say so plainly, a blank question is the single most \
common reason the form refuses to advance.
- Copy on_screen VERBATIM from the option as printed on screen, including case.
- The DOM hint below can be stale or wrong. The IMAGE is the truth. If they \
disagree, report what you see.
- Some answers were pre-filled by the job site from the applicant's previous \
applications (you may see a banner saying so). Report them exactly like any \
other selection - a pre-filled answer is still an answer that will be sent.
- Do not decide whether an answer is correct here, and do not answer anything \
yourself. Only report what is on screen.
- Include every question visible in the screenshot, including ones the hint \
does not mention.

Return ONLY a JSON array, no prose:
[{"question": "<first ~80 chars of the question as printed on screen>",
  "on_screen": "<exact selected option text, or \\"\\" if nothing is selected>",
  "note": "<'prefilled' / 'blank' / '' - one word if anything stands out>"}]"""


def verify_screenshot(png_bytes: bytes, intended, profile: dict,
                      api_key: str | None = None) -> list:
    """
    [{question, on_screen, intended, matches}] - read the page and compare it
    to what we meant to put there.

    THE POINT OF THIS FUNCTION is that `matches` is False for a question that
    is answered but answered WRONG, not merely for one that is blank. Indeed
    pre-fills answers from previous applications; the DOM cannot tell a
    carried-over "Yes" from one we chose, and every earlier version of this
    tool treated "something is selected" as "this step is fine".

    `intended` is a list of {question, options[], intended, selected}, one per
    question on screen. An entry whose `intended` is empty means no rule
    covered it - that is reported with matches=False so the caller looks at it,
    because "we had no opinion" is not the same as "it is right".
    """
    if not png_bytes or not VISION_AVAILABLE:
        return []
    try:
        system = VERIFY_SYSTEM
        prompt = ("The tool has just filled this page. Questions the DOM reports "
                  "on this screen:\n\n"
                  + (_hint_lines(intended) or "(no DOM hint - read the image)")
                  + "\n\nRead the image and report what is selected for each "
                    "question right now.")
        data = _ask(system, [_image_block(png_bytes), {"type": "text", "text": prompt}],
                    api_key, MODEL_FAST)
        if data is None:
            return []

        # Line the model's reading up with our intentions by question text, then
        # decide `matches` HERE, in Python, with the same rule a radio is ticked
        # by. The model reports pixels; it does not get a vote on whether the
        # form is correct.
        wants = [q for q in (intended or []) if isinstance(q, dict)]
        out = []
        for row in data:
            if not isinstance(row, dict):
                continue
            qtext = str(row.get("question", "")).strip()[:300]
            on = str(row.get("on_screen", "") or "").strip()
            hit = _best(qtext, wants)
            want = str((hit or {}).get("intended") or "")
            out.append({
                "question": qtext,
                "on_screen": on,
                "intended": want,
                # No intention and nothing selected -> still a problem (the form
                # will block). No intention and something selected -> unverified,
                # which is how the wrong pre-filled answer gets caught.
                "matches": bool(want) and bool(on) and _same(on, want),
                "note": str(row.get("note", "")).strip()[:60],
            })
        return out
    except Exception:
        return []


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())[:120].strip()


def _best(qtext: str, rows: list):
    """
    The row describing the same question, or None. Mirrors autofill._match_row.

    Whole-string similarity rather than a shared prefix, for two reasons seen
    on one real form: the model drops a leading "B." and the prefix test then
    matches nothing, while "A. Within the past 5 years, have you ever been an
    employee of..." and "C. Within the past 5 years, ..." are identical for
    forty characters and the prefix test matches the WRONG one. Beating the
    runner-up by a margin means an ambiguous reading matches nothing at all.
    """
    ka = _key(qtext)
    if not ka or not rows:
        return None
    scored = []
    for r in rows:
        kb = _key(str(r.get("question", "")))
        if kb:
            scored.append((difflib.SequenceMatcher(None, ka, kb).ratio(), r))
    if not scored:
        return None
    scored.sort(key=lambda s: -s[0])
    if scored[0][0] >= 0.72 and scored[0][0] - (scored[1][0] if len(scored) > 1 else 0) >= 0.06:
        return scored[0][1]
    return None


# --------------------------------------------------------------------------
# offline check:  python vision_answer.py run-shots\<file>.png
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        print(status())
        raise SystemExit(0)
    shot = Path(sys.argv[1])
    if not shot.is_absolute():
        shot = HERE / shot
    prof = json.loads((HERE / "profile.json").read_text(encoding="utf-8"))
    png = shot.read_bytes()
    print(status())
    print("\n--- what is on screen ---")
    for r in verify_screenshot(png, [], prof):
        print(f"  {r['question'][:70]!r:<74} on_screen={r['on_screen'][:40]!r}")
    print("\n--- what the answer should be ---")
    for r in answer_from_screenshot(png, [], prof):
        print(f"  {r['question'][:70]!r:<74} -> {r['answer'][:40]!r} "
              f"({r['confidence']}) {r['reason'][:70]}")
    print("\n" + status())
