"""Regression tests for the answer-matching rules. Several of these encode real mistakes
the filler once made on live applications (see the comments in autofill.py)."""
import pytest

import autofill as af

PROFILE = {
    "identity": {"first_name": "Ada", "last_name": "Lovelace", "full_name": "Ada Lovelace",
                 "email": "ada@example.com", "phone": "5550100", "location": "Springfield, ST",
                 "country": "United States", "city": "Springfield", "state": "ST"},
    "links": {"linkedin": "https://linkedin.com/in/example", "github": "https://github.com/example"},
    "work": {"years_experience": "5", "requires_sponsorship": "No", "work_authorized": "Yes",
             "willing_to_relocate": "Yes", "how_did_you_hear": "Company website"},
    "answers": [
        {"match": "favorite color", "value": "TODO ask him"},
        {"match": "pronouns", "value": ["they/them", "Prefer not to say"]},
    ],
}


@pytest.fixture(scope="module")
def rules():
    return af.build_rules(PROFILE)


def answer(rules, label):
    norm = af.norm_label(label)
    for rx, vals in rules:
        if rx.search(norm):
            return vals[0]
    return None


# ---------------- label rules ----------------

@pytest.mark.parametrize("label,expected", [
    ("First name*", "Ada"),
    ("Last Name", "Lovelace"),
    ("Name", "Ada Lovelace"),
    ("Name*", "Ada Lovelace"),
    ("Full name", "Ada Lovelace"),
    ("Your name", "Ada Lovelace"),
    ("Email address*", "ada@example.com"),
    ("LinkedIn Profile", "https://linkedin.com/in/example"),
    ("Will you now or in the future require sponsorship?", "No"),
    ("Are you legally authorized to work in the US?", "Yes"),
    ("How many years of experience do you have?", "5"),
    ("How many years of Python experience do you have?", "5"),
])
def test_label_answers(rules, label, expected):
    assert answer(rules, label) == expected


@pytest.mark.parametrize("label", [
    "Name two things you're proud of",
    "Name a time you failed and what you learned",
])
def test_essay_prompts_starting_with_name_get_no_name(rules, label):
    """Regression: '^name' as a prefix typed his full name into essay boxes."""
    assert answer(rules, label) != "Ada Lovelace"


def test_named_domain_he_lacks_is_not_answered(rules):
    """Regression: 'How many years of Hospitals and Health Care experience' once went out as 5."""
    assert answer(rules, "How many years of Hospitals and Health Care experience do you currently have?") is None


def test_location_question_not_answered_with_country(rules):
    assert answer(rules, "Where are you based? (city, country)") == "Springfield, ST"


def test_todo_values_are_never_typed(rules):
    assert answer(rules, "What is your favorite color?") is None


def test_user_answers_take_precedence_and_keep_alternatives(rules):
    first = rules[0]
    assert first[0].search("pronouns") and first[1] == ["they/them", "Prefer not to say"]


def test_count_explicit_ignores_todo():
    assert af.count_explicit(PROFILE) == 1


# ---------------- never-autofill list ----------------

@pytest.mark.parametrize("label", ["Gender", "Race / Ethnicity", "Are you Hispanic or Latino?",
                                   "Veteran status", "Disability status"])
def test_sensitive_questions_are_skipped(label):
    skips = af.build_skips(PROFILE)
    assert any(rx.search(label.lower()) for rx in skips)


def test_bad_never_autofill_regex_is_ignored_not_fatal():
    skips = af.build_skips({"never_autofill": ["salary", "(unclosed"]})
    assert any(rx.pattern == "salary" for rx in skips)


# ---------------- option matching ----------------

def test_pick_option_whole_word_man_vs_woman():
    """Regression: substring 'man' used to pick 'Cisgender woman'."""
    opts = ["Cisgender woman", "Man", "Non-binary"]
    assert af.pick_option(opts, "man") == "Man"
    assert af.pick_option(["Female", "Male"], "male") == "Male"
    # No exact option: the whole-word pass must skip "woman" and "female".
    assert af.pick_option(["Cisgender woman", "Cisgender man"], "man") == "Cisgender man"
    assert af.pick_option(["Female (cis)", "Male (cis)"], "male") == "Male (cis)"


def test_pick_option_exact_then_none():
    assert af.pick_option(["Yes", "No"], "yes") == "Yes"
    assert af.pick_option(["Red", "Blue"], "green") is None


def test_radio_does_not_pick_opposite_answer():
    """Regression: 'Yes - U.S. citizen, no sponsorship required' contains 'no' and ticked No."""
    value = "Yes - U.S. citizen, no sponsorship required"
    assert af.radio_matches("Yes - U.S. citizen, no sponsorship required", value)
    assert not af.radio_matches("No", value)


@pytest.mark.parametrize("option,value,expected", [
    ("No, I have not been employed by this company", "No", True),
    ("None of the above", "No", False),
    ("Yes", "yes", True),
    ("", "Yes", False),
])
def test_radio_leading_match_at_word_boundary(option, value, expected):
    assert af.radio_matches(option, value) is expected


# ---------------- question alignment ----------------

def test_match_row_tolerates_dropped_enumerator():
    rows = [{"question": "Are you a current employee of the company?"}, {"question": "What is your salary expectation?"}]
    assert af._match_row("B. Are you a current employee of the company?", rows) is rows[0]


def test_match_row_refuses_ambiguous_twins():
    """Two questions identical for 40+ characters must not be joined by a guess."""
    rows = [{"question": "A. Within the past 5 years, have you ever been an employee of the federal government?"},
            {"question": "C. Within the past 5 years, have you ever been an employee of the state government?"}]
    assert af._match_row("Within the past 5 years, have you ever been an employee of the government?", rows) is None


# ---------------- small utilities ----------------

@pytest.mark.parametrize("url,ats", [
    ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
    ("https://jobs.lever.co/acme/123", "lever"),
    ("https://jobs.ashbyhq.com/acme/x", "ashby"),
    ("https://acme.wd1.myworkdayjobs.com/x", "workday"),
    ("https://smartapply.indeed.com/beta/x", "indeed_apply"),
    ("https://example.com/careers", "generic"),
])
def test_detect_ats(url, ats):
    assert af.detect_ats(url) == ats


def test_norm_url_keeps_only_job_ids_and_sorts():
    a = af.norm_url("https://Boards.greenhouse.io/acme/jobs/?utm_source=x&gh_jid=42#apply")
    b = af.norm_url("https://boards.greenhouse.io/acme/jobs?gh_jid=42&utm_campaign=y")
    assert a == b == "https://boards.greenhouse.io/acme/jobs?gh_jid=42"


def test_norm_label_cuts_widget_chrome_after_required_marker():
    assert af.norm_label("Country*Select...") == "country*"


@pytest.mark.parametrize("raw", ['Engineer: Backend/API "Lead"', "", "a" * 50])
def test_safe_name_is_windows_safe(raw):
    out = af.safe_name(raw)
    assert out and not any(c in out for c in '<>:"/\\|?*') and len(out) <= 18


def test_expand_tokens_dates():
    import re
    out = af.expand_tokens("{today_iso} / {today}")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} / \d{2}/\d{2}/\d{4}", out)
    assert af.expand_tokens("no tokens") == "no tokens"


def test_why_not_submittable():
    ok = {"filled": 5, "captcha": False, "missing": [], "blockers": []}
    assert af.why_not_submittable(ok) == []
    bad = {"filled": 0, "captcha": True, "missing": ["q"], "blockers": ["b"]}
    assert len(af.why_not_submittable(bad)) == 4


def test_consent_policy_defaults_to_never_tick():
    assert af.consent_policy_match({}, "I agree to the privacy policy", "x") is False
