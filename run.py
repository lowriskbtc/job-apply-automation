#!/usr/bin/env python
"""
run.py - the whole thing from one menu. Double-click Start.bat, or:

    python run.py

Everything here is a shortcut to the same scripts you can still run by hand
(collect_indeed.py, collect_jobright.py, autofill.py) - it remembers the flags
and the order for you, and it will not do anything those scripts wouldn't.

Deliberately a terminal menu and not a window: filling an application means
reading the form in Chrome and deciding whether to send it, so the useful
screen space belongs to Chrome, not to a second GUI competing with it.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "profile.json"
LOG = HERE / "applications.jsonl"
PORT = 9222

QUEUES = [("jobs_indeed.txt", "Indeed"), ("jobs.txt", "jobright")]


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def chrome_up(port: int = PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def load_profile() -> dict:
    try:
        return json.loads(PROFILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def applied_keys() -> set[str]:
    """Normalised urls already submitted, so queue counts mean something."""
    if not LOG.exists():
        return set()
    try:
        sys.path.insert(0, str(HERE))
        from autofill import norm_url          # no playwright import at module level
    except Exception:
        return set()
    keys = set()
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("status") == "submitted":
            keys.add(norm_url(rec.get("url", "")))
            keys.add(norm_url(rec.get("requested_url", "")))
    return keys


def queue_state() -> list[tuple[str, str, int, int]]:
    """(filename, source, total, remaining) for every queue file present."""
    done = applied_keys()
    try:
        sys.path.insert(0, str(HERE))
        from autofill import norm_url
    except Exception:
        norm_url = lambda u: u                                    # noqa: E731

    out = []
    for fname, source in QUEUES:
        p = HERE / fname
        if not p.exists():
            continue
        urls = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
        left = sum(1 for u in urls if norm_url(u) not in done)
        out.append((fname, source, len(urls), left))
    return out


def log_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    if not LOG.exists():
        return counts
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        k = rec.get("status", "?")
        counts[k] = counts.get(k, 0) + 1
    return counts


# --------------------------------------------------------------------------
# ui
# --------------------------------------------------------------------------

def header() -> None:
    prof = load_profile()
    who = prof.get("identity", {}).get("full_name") or "(no profile)"
    up = chrome_up()

    print("\n" + "=" * 66)
    print("  jobsearch")
    print("=" * 66)
    print(f"  you      {who}")
    print(f"  chrome   {'running on ' + str(PORT) if up else 'NOT RUNNING  <- start it first (6)'}")

    qs = queue_state()
    if qs:
        bits = [f"{left} left of {total} ({source})" for _, source, total, left in qs]
        print(f"  queued   {'   '.join(bits)}")
    else:
        print("  queued   nothing collected yet")

    counts = log_counts()
    if counts:
        order = ["submitted", "needs_you", "skipped", "dead_link", "submit_failed"]
        bits = [f"{counts[k]} {k}" for k in order if k in counts]
        bits += [f"{v} {k}" for k, v in counts.items() if k not in order]
        print(f"  applied  {'   '.join(bits)}")
    print()


MENU = """  1   Find jobs on Indeed
  2   Find jobs on jobright
  3   Fill applications        you read each form and submit it yourself
  4   Hands-off mode           auto-submits ONLY the forms with nothing unanswered
  5   Report - what you've applied to
  6   Start Chrome
  q   Quit
"""


def clean(s: str) -> str:
    """Piped input can carry a BOM (PowerShell adds one), which makes an
    answer of 'q' arrive as '﻿q' and silently miss every menu branch."""
    return s.replace("﻿", "").strip()


def ask(prompt: str, default: str = "") -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    try:
        got = clean(input(shown))
    except (EOFError, KeyboardInterrupt):
        return ""
    return got or default


def run(*args: str) -> None:
    """Hand off to a real script, stdio inherited so its prompts still work."""
    cmd = [sys.executable, *args]
    print(f"\n> {' '.join(str(c) for c in cmd[1:])}\n")
    # The child writes to this same handle. Redirect the menu to a file and an
    # unflushed parent buffer lands *after* the child's output, so the log
    # reads as though the report printed before the command that made it.
    sys.stdout.flush()
    try:
        subprocess.call(cmd, cwd=str(HERE))
    except KeyboardInterrupt:
        print("\n(interrupted)")


def need_chrome() -> bool:
    if chrome_up():
        return True
    print("\n  Chrome isn't running with remote debugging - option 6 starts it.")
    print("  (and sign in to Indeed / jobright inside that window once)")
    return False


def pick_queue() -> str | None:
    qs = queue_state()
    if not qs:
        print("\n  No queue yet - collect some jobs first (1 or 2).")
        return None
    live = [q for q in qs if q[3] > 0]
    if not live:
        # A queue file with zero urls is not the same as a queue you've worked
        # through - an Indeed run where every job was Indeed-hosted writes an
        # empty file, and "you've applied to them all" would be a lie.
        if all(total == 0 for _, _, total, _ in qs):
            print("\n  The queue is empty - the last collection found nothing "
                  "autofill can drive.")
            print("  (Indeed-hosted jobs aren't queued; see jobs_indeed.jsonl "
                  "for what was found.)")
        else:
            print("\n  Every collected job is already applied to. "
                  "Collect more (1 or 2).")
        return None
    if len(live) == 1:
        return live[0][0]
    print()
    for i, (fname, source, total, left) in enumerate(live, 1):
        print(f"  {i}  {fname:<20} {left} left of {total}  ({source})")
    got = ask("which", "1")
    try:
        return live[int(got) - 1][0]
    except (ValueError, IndexError):
        return None


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------

def do_indeed() -> None:
    if not need_chrome():
        return
    search = load_profile().get("search", {})
    what = ask("  what", search.get("what", "AI engineer"))
    if not what:
        return
    where = ask("  where", search.get("where", "Remote"))
    days = ask("  posted within N days (0 = any)", str(search.get("days", 7)))
    limit = ask("  how many jobs", "25")

    # Looking up apply links opens every job page one at a time. That is the
    # slow part AND the part most likely to get challenged, so the first run
    # defaults to a plain harvest: it proves the card selectors still work
    # before spending a session's goodwill finding out they don't.
    first = not (HERE / "jobs_indeed.jsonl").exists()
    if first:
        print("\n  First Indeed run. Doing a plain harvest first - it's quick,")
        print("  opens no job pages, and tells you whether the search still")
        print("  reads correctly before the slow part.")
    resolve = ask("  look up apply links too (slower)? y/n",
                  "n" if first else "y").lower().startswith("y")

    args = ["collect_indeed.py", "--what", what, "--where", where,
            "--limit", limit]
    if days and days != "0":
        args += ["--days", days]
    if resolve:
        args.append("--resolve")
    run(*args)

    if not resolve:
        print("  Check the titles and companies above look right, then run "
              "option 1 again\n  and say yes to the apply-link lookup.")


def do_jobright() -> None:
    if not need_chrome():
        return
    limit = ask("  how many jobs", "25")
    run("collect_jobright.py", "--limit", limit, "--resolve")


def do_fill(auto: bool = False) -> None:
    if not need_chrome():
        return
    queue = pick_queue()
    if not queue:
        return

    if not auto:
        # --dry is worth offering because a queue is only as good as the
        # profile behind it: it reports what WOULD be filled and touches
        # nothing, which is the cheap way to catch a bad rule before it's
        # typed into twelve real forms.
        dry = ask("  dry run first - report only, fill nothing? y/n", "n")
        args = ["autofill.py", "--queue", queue]
        if dry.lower().startswith("y"):
            args.append("--dry")
        run(*args)
        return

    # Auto-submit sends real applications with no further prompt, so getting
    # here must take more than one keystroke.
    print("\n  Hands-off mode fills, ticks the checkboxes the form requires,")
    print("  and SUBMITS - without showing you the form first.")
    print("  It refuses on anything with a CAPTCHA or an unanswered required")
    print("  question; those land in the report for you instead.")
    cap = ask("\n  stop after how many submissions", "10")
    if ask("  type 'send' to go ahead") != "send":
        print("  cancelled.")
        return
    run("autofill.py", "--queue", queue, "--auto-submit", "--max", cap)


def main() -> None:
    while True:
        header()
        print(MENU)
        try:
            choice = clean(input("  > ")).lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice in ("q", "quit", "exit"):
            return
        elif choice == "1":
            do_indeed()
        elif choice == "2":
            do_jobright()
        elif choice == "3":
            do_fill(auto=False)
        elif choice == "4":
            do_fill(auto=True)
        elif choice == "5":
            run("autofill.py", "--report")
        elif choice == "6":
            print()
            subprocess.call(["powershell", "-ExecutionPolicy", "Bypass",
                             "-File", str(HERE / "start_chrome.ps1")])
            print("\n  Sign in to Indeed and jobright in that window if you "
                  "haven't - it sticks after the first time.")
        else:
            continue

        try:
            input("\n  [enter] for the menu ")
        except (EOFError, KeyboardInterrupt):
            return


if __name__ == "__main__":
    if not PROFILE.exists():
        sys.exit(f"No profile at {PROFILE}\n"
                 f"Copy profile.example.json to profile.json and fill it in first.")
    main()
