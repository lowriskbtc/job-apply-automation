#!/usr/bin/env python
"""
gui.py - a window for working through collected jobs.

The terminal queue walks you through applications one at a time in a fixed
order with no way back. This shows the whole list, keeps your place, and lets
you jump around: click any job, fill it, look at it in Chrome, mark it, click
another. Nothing is submitted for you.

    python gui.py          (or double-click Start-GUI.bat)

Browser work happens on a single worker thread because Playwright's sync API
belongs to the thread that created it - the UI thread only ever posts commands
and reads results, so the window never freezes mid-navigation.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from autofill import (                                            # noqa: E402
    DEFAULT_PORT, LOG_PATH, autofill_page, build_rules, build_skips,
    count_explicit, detect_ats, enter_smartapply, load_profile, log_application,
    norm_url, page_meta, report_stepper, run_stepper, why_not_submittable,
)

JOB_FILES = [("jobs_indeed.jsonl", "Indeed"), ("jobs.jsonl", "jobright")]


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

def applied_status() -> dict[str, str]:
    """normalised url -> last status recorded for it."""
    out: dict[str, str] = {}
    if not LOG_PATH.exists():
        return out
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        for key in ("url", "requested_url"):
            if rec.get(key):
                out[norm_url(rec[key])] = rec.get("status", "?")
    return out


def load_jobs() -> list[dict]:
    seen = applied_status()
    jobs: list[dict] = []
    for fname, source in JOB_FILES:
        p = HERE / fname
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            # An Indeed-hosted job has no employer form to drive, but it is
            # still a real job you may want to open by hand - so it belongs in
            # the list, marked, rather than quietly dropped.
            url = r.get("apply_url") or r.get("jobright_url") or r.get("job_url") or ""
            jobs.append({
                "title": r.get("title") or "(untitled)",
                "company": r.get("company") or "",
                "source": source,
                "kind": r.get("apply_kind") or ("employer_site" if url else ""),
                "url": url,
                "job_url": r.get("job_url") or url,
                "status": seen.get(norm_url(url), "") if url else "",
            })
    return jobs


# --------------------------------------------------------------------------
# browser worker - owns Playwright, one thread, forever
# --------------------------------------------------------------------------

class Browser(threading.Thread):
    def __init__(self, out: queue.Queue):
        super().__init__(daemon=True)
        self.cmds: queue.Queue = queue.Queue()
        self.out = out
        self.pw = self.browser = self.page = None

    def post(self, kind, **kw):
        self.out.put({"kind": kind, **kw})

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.post("fatal", msg="playwright is not installed.\n"
                                   "Run:  python -m pip install playwright")
            return
        self.pw = sync_playwright().start()
        while True:
            cmd = self.cmds.get()
            op = cmd["op"]
            try:
                if op == "quit":
                    break
                getattr(self, f"_{op}")(cmd)
            except Exception as exc:
                self.post("error", msg=f"{type(exc).__name__}: {exc}")
                self.post("busy", on=False)
        try:
            self.pw.stop()
        except Exception:
            pass

    # -- helpers ----------------------------------------------------------
    def _need_page(self):
        if self.page is not None:
            try:
                self.page.title()
                return self.page
            except Exception:
                self.page = None        # tab was closed under us
        if self.browser is None:
            self.browser = self.pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{DEFAULT_PORT}")
        ctx = (self.browser.contexts[0] if self.browser.contexts
               else self.browser.new_context())
        self.page = ctx.new_page()
        return self.page

    # -- operations -------------------------------------------------------
    def _ping(self, cmd):
        try:
            b = self.pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{DEFAULT_PORT}")
            self.browser = b
            self.post("chrome", up=True)
        except Exception:
            self.browser = None
            self.post("chrome", up=False)

    def _open(self, cmd):
        self.post("busy", on=True)
        page = self._need_page()
        page.goto(cmd["url"], wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1500)
        page.bring_to_front()
        self.post("opened", url=page.url)
        self.post("busy", on=False)

    def _fill(self, cmd):
        self.post("busy", on=True)
        page = self._need_page()
        page.goto(cmd["url"], wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2000)
        page.bring_to_front()

        prof = load_profile()
        title, company = page_meta(page)

        # Indeed's own flow is a stepper - different code path, and one that
        # stops at the review screen instead of ever submitting.
        ats0 = detect_ats(page.url)
        if ats0 in ("indeed", "indeed_apply"):
            target = page
            if ats0 == "indeed":
                target = enter_smartapply(page, page.context)
                if target is None:
                    self.post("error", msg="Could not open Indeed's apply flow "
                                           "from this page.")
                    self.post("busy", on=False)
                    return
            self.page = target
            steps, outcome = run_stepper(target, prof, build_rules(prof),
                                         build_skips(prof), cmd["dry"],
                                         count_explicit(prof), title, company)
            self.post("steps", steps=[
                (n, slug, {k: v for k, v in r.items() if k != "consent"})
                for n, slug, r in steps], outcome=outcome,
                url=target.url, title=title, company=company, dry=cmd["dry"])
            self.post("busy", on=False)
            return

        n = page.evaluate(
            "() => document.querySelectorAll('input, textarea, select').length")
        if n == 0:
            self.post("report", res=None, dead=True, url=page.url,
                      ats=detect_ats(page.url), title=title, company=company)
            self.post("busy", on=False)
            return
        res = autofill_page(page, prof, build_rules(prof), build_skips(prof),
                            cmd["dry"], count_explicit(prof), title, company)
        # Handles aren't picklable across threads and aren't needed by the UI.
        res = {k: v for k, v in res.items() if k != "consent"}
        self.post("report", res=res, dead=False, url=page.url,
                  ats=detect_ats(page.url), title=title, company=company,
                  dry=cmd["dry"])
        self.post("busy", on=False)


# --------------------------------------------------------------------------
# ui
# --------------------------------------------------------------------------

MARK = {"submitted": "OK", "needs_you": "!", "skipped": "-",
        "dead_link": "x", "submit_failed": "!"}


class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=8)
        self.root = root
        self.pack(fill="both", expand=True)
        self.events: queue.Queue = queue.Queue()
        self.worker = Browser(self.events)
        self.worker.start()
        self.jobs: list[dict] = []
        self.busy = False
        self.last = None                # last report, for the mark buttons

        self._build()
        self.refresh()
        self.worker.cmds.put({"op": "ping"})
        self.after(80, self._pump)

    # -- layout -----------------------------------------------------------
    def _build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Start Chrome", command=self.start_chrome).pack(side="left")
        ttk.Button(bar, text="Find jobs...", command=self.collect).pack(side="left", padx=4)
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="left")
        self.chrome_lbl = ttk.Label(bar, text="chrome: checking")
        self.chrome_lbl.pack(side="right")

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.Frame(pane)
        cols = ("st", "title", "company", "kind")
        self.tree = ttk.Treeview(left, columns=cols, show="headings",
                                 selectmode="browse")
        for c, w, t in (("st", 34, ""), ("title", 250, "Job"),
                        ("company", 140, "Company"), ("kind", 105, "Apply via")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w",
                             stretch=(c == "title"))
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select())
        self.tree.tag_configure("done", foreground="#2e7d32")
        self.tree.tag_configure("hosted", foreground="#888888")
        pane.add(left, weight=3)

        right = ttk.Frame(pane, padding=(10, 0, 0, 0))
        self.head = ttk.Label(right, text="Select a job", font=("", 11, "bold"),
                              wraplength=380, justify="left")
        self.head.pack(anchor="w")
        self.sub = ttk.Label(right, text="", foreground="#666",
                             wraplength=380, justify="left")
        self.sub.pack(anchor="w", pady=(2, 8))

        b1 = ttk.Frame(right); b1.pack(fill="x")
        self.btn_fill = ttk.Button(b1, text="Fill form", command=lambda: self.fill(False))
        self.btn_fill.pack(side="left")
        self.btn_dry = ttk.Button(b1, text="Preview only", command=lambda: self.fill(True))
        self.btn_dry.pack(side="left", padx=4)
        self.btn_open = ttk.Button(b1, text="Open in Chrome", command=self.open_only)
        self.btn_open.pack(side="left")

        b2 = ttk.Frame(right); b2.pack(fill="x", pady=(6, 8))
        self.btn_sub = ttk.Button(b2, text="Mark submitted",
                                  command=lambda: self.mark("submitted"))
        self.btn_sub.pack(side="left")
        self.btn_skip = ttk.Button(b2, text="Mark skipped",
                                   command=lambda: self.mark("skipped"))
        self.btn_skip.pack(side="left", padx=4)
        ttk.Button(b2, text="Prev", command=lambda: self.step(-1)).pack(side="right")
        ttk.Button(b2, text="Next", command=lambda: self.step(1)).pack(side="right", padx=4)

        self.out = tk.Text(right, height=18, wrap="word", font=("Consolas", 9),
                           state="disabled", background="#fbfbfb")
        self.out.pack(fill="both", expand=True)
        for tag, col in (("good", "#2e7d32"), ("warn", "#b26a00"),
                         ("bad", "#c62828"), ("dim", "#777777")):
            self.out.tag_configure(tag, foreground=col)
        pane.add(right, weight=4)

        self.status = ttk.Label(self, text="", foreground="#555")
        self.status.pack(fill="x", pady=(6, 0))

    # -- data -------------------------------------------------------------
    def refresh(self):
        self.jobs = load_jobs()
        self.tree.delete(*self.tree.get_children())
        for i, j in enumerate(self.jobs):
            tags = ()
            if j["status"] == "submitted":
                tags = ("done",)
            elif j["kind"] == "indeed_hosted":
                tags = ("hosted",)
            kind = {"employer_site": "employer site",
                    "indeed_hosted": "Indeed (by hand)"}.get(j["kind"], j["kind"] or "-")
            self.tree.insert("", "end", iid=str(i), tags=tags, values=(
                MARK.get(j["status"], ""), j["title"][:70], j["company"][:30], kind))
        done = sum(1 for j in self.jobs if j["status"] == "submitted")
        hosted = sum(1 for j in self.jobs if j["kind"] == "indeed_hosted")
        self.status.config(text=f"{len(self.jobs)} jobs   {done} submitted   "
                                f"{hosted} Indeed-hosted (apply by hand)")

    def current(self) -> dict | None:
        sel = self.tree.selection()
        return self.jobs[int(sel[0])] if sel else None

    def on_select(self):
        j = self.current()
        if not j:
            return
        self.head.config(text=f"{j['title']}")
        self.sub.config(text=f"{j['company'] or '(no company)'}  -  {j['url'] or 'no url'}")
        hosted = j["kind"] == "indeed_hosted"
        for b in (self.btn_fill, self.btn_dry):
            b.state(["disabled"] if (hosted or not j["url"]) else ["!disabled"])
        self.say("")
        if hosted:
            self.say("Indeed hosts this application itself - a multi-step wizard\n"
                     "this can't drive safely. Open it in Chrome and do it by hand,\n"
                     "then mark it here.\n", "dim")
        elif j["status"]:
            self.say(f"already recorded as: {j['status']}\n", "dim")

    def step(self, d: int):
        if not self.jobs:
            return
        sel = self.tree.selection()
        i = (int(sel[0]) + d) if sel else 0
        i = max(0, min(len(self.jobs) - 1, i))
        self.tree.selection_set(str(i))
        self.tree.see(str(i))

    # -- output -----------------------------------------------------------
    def say(self, text: str, tag: str = ""):
        self.out.config(state="normal")
        if text == "":
            self.out.delete("1.0", "end")
        else:
            self.out.insert("end", text, tag or ())
        self.out.see("end")
        self.out.config(state="disabled")

    def set_busy(self, on: bool):
        self.busy = on
        self.root.config(cursor="watch" if on else "")
        for b in (self.btn_fill, self.btn_dry, self.btn_open):
            b.state(["disabled"] if on else ["!disabled"])
        if not on:
            self.on_select()

    # -- actions ----------------------------------------------------------
    def fill(self, dry: bool):
        j = self.current()
        if not j or self.busy:
            return
        self.say("")
        self.say(f"{'previewing' if dry else 'filling'} {j['url']}\n\n", "dim")
        self.worker.cmds.put({"op": "fill", "url": j["url"], "dry": dry})

    def open_only(self):
        j = self.current()
        if not j or self.busy:
            return
        url = j["url"] or j["job_url"]
        self.say("")
        self.say(f"opening {url}\n", "dim")
        self.worker.cmds.put({"op": "open", "url": url})

    def mark(self, status: str):
        j = self.current()
        if not j:
            return
        r = self.last or {}
        log_application(r.get("url") or j["url"], r.get("ats") or "", status,
                        r.get("title") or j["title"], r.get("company") or j["company"],
                        requested=j["url"])
        self.say(f"\nrecorded: {status}\n", "good")
        self.refresh()

    def start_chrome(self):
        subprocess.Popen(["powershell", "-ExecutionPolicy", "Bypass",
                          "-File", str(HERE / "start_chrome.ps1")])
        self.say("\nstarting Chrome - sign in to Indeed/jobright in that window\n"
                 "if you haven't, then press Refresh.\n", "dim")
        self.after(6000, lambda: self.worker.cmds.put({"op": "ping"}))

    def collect(self):
        Collect(self.root, self)

    # -- event pump -------------------------------------------------------
    def _pump(self):
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle(ev)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _handle(self, ev):
        k = ev["kind"]
        if k == "chrome":
            up = ev["up"]
            self.chrome_lbl.config(
                text=f"chrome: {'connected' if up else 'NOT RUNNING'}",
                foreground="#2e7d32" if up else "#c62828")
        elif k == "busy":
            self.set_busy(ev["on"])
        elif k == "opened":
            self.say(f"open in Chrome: {ev['url'][:90]}\n", "dim")
        elif k == "error":
            self.say(f"\n{ev['msg']}\n", "bad")
            if "connect_over_cdp" in ev["msg"] or "ECONNREFUSED" in ev["msg"]:
                self.say("Chrome isn't running with remote debugging - "
                         "press 'Start Chrome'.\n", "bad")
                self.worker.cmds.put({"op": "ping"})
        elif k == "fatal":
            messagebox.showerror("jobsearch", ev["msg"])
        elif k == "report":
            self._report(ev)

    def _report(self, ev):
        self.last = ev
        if ev.get("dead"):
            self.say("DEAD LINK - no form on this page. The posting is probably "
                     "closed.\n", "bad")
            return
        res = ev["res"]
        self.say(f"{ev['ats']}  |  {ev['company']}\n\n", "dim")
        self.say(f"filled ({len(res['filled'])})\n", "good")
        for f in res["filled"]:
            self.say(f"   + {f}\n")
        if res["missing"]:
            self.say(f"\nrequired, but nothing in your profile matched "
                     f"({len(res['missing'])})\n", "bad")
            for m in res["missing"]:
                self.say(f"   ? {m}\n")
        if res["needs_you"]:
            self.say(f"\nyours to handle ({len(res['needs_you'])})\n", "warn")
            for n in res["needs_you"]:
                self.say(f"   > {n}\n")
        if res["skipped"]:
            self.say(f"\nleft blank on purpose: {', '.join(res['skipped'][:8])}\n", "dim")
        if res["captcha"]:
            self.say("\nCAPTCHA on this form - you have to clear it yourself.\n", "bad")
        blockers = why_not_submittable(res)
        if ev.get("dry"):
            self.say("\npreview only - nothing was typed.\n", "dim")
        elif blockers:
            self.say(f"\nnot ready to send: {'; '.join(blockers)}\n", "warn")
        else:
            self.say("\nnothing outstanding. Read it in Chrome, submit there, "
                     "then press 'Mark submitted'.\n", "good")


class Collect(tk.Toplevel):
    """Run a collector and stream its output, without freezing the window."""

    def __init__(self, root, app: App):
        super().__init__(root)
        self.title("Find jobs")
        self.app = app
        self.resizable(False, False)
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        prof = load_profile().get("search", {})
        self.what = tk.StringVar(value=prof.get("what", "AI engineer"))
        self.where = tk.StringVar(value=prof.get("where", "Remote"))
        self.days = tk.StringVar(value=str(prof.get("days", 7)))
        self.limit = tk.StringVar(value="25")
        for r, (lbl, var) in enumerate((("What", self.what), ("Where", self.where),
                                        ("Posted within (days)", self.days),
                                        ("How many", self.limit))):
            ttk.Label(frm, text=lbl).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Entry(frm, textvariable=var, width=32).grid(row=r, column=1, pady=2)

        self.src = tk.StringVar(value="indeed")
        ttk.Radiobutton(frm, text="Indeed", variable=self.src,
                        value="indeed").grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Radiobutton(frm, text="jobright", variable=self.src,
                        value="jobright").grid(row=4, column=1, sticky="w", pady=(6, 0))

        self.log = tk.Text(frm, height=12, width=64, font=("Consolas", 9),
                           state="disabled")
        self.log.grid(row=5, column=0, columnspan=2, pady=8)
        self.go = ttk.Button(frm, text="Go", command=self.start)
        self.go.grid(row=6, column=1, sticky="e")

    def start(self):
        self.go.state(["disabled"])
        args = ([sys.executable, "collect_indeed.py",
                 "--what", self.what.get(), "--where", self.where.get(),
                 "--limit", self.limit.get(), "--resolve"]
                + (["--days", self.days.get()] if self.days.get().strip("0 ") else []))
        if self.src.get() == "jobright":
            args = [sys.executable, "collect_jobright.py",
                    "--limit", self.limit.get(), "--resolve"]
        threading.Thread(target=self._run, args=(args,), daemon=True).start()

    def _run(self, args):
        try:
            p = subprocess.Popen(args, cwd=str(HERE), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace")
            for line in p.stdout:
                self.after(0, self._append, line)
            p.wait()
        except Exception as exc:
            self.after(0, self._append, f"\n{type(exc).__name__}: {exc}\n")
        self.after(0, self._done)

    def _append(self, line):
        self.log.config(state="normal")
        self.log.insert("end", line)
        self.log.see("end")
        self.log.config(state="disabled")

    def _done(self):
        self.go.state(["!disabled"])
        self.app.refresh()


def main():
    if not (HERE / "profile.json").exists():
        sys.exit("No profile.json - copy profile.example.json and fill it in.")
    root = tk.Tk()
    root.title("jobsearch")
    root.geometry("1080x640")
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
