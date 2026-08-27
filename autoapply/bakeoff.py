"""Run several fillers over the same application and report which did best.

The point is that the comparison is fair, which is harder than it sounds. Each
contender must get the same page in the same state, be scored by the same
readback rather than by its own account of itself, and be given the same chance
to fail for reasons that are not its fault.

So the harness does the parts that are not filling: it signs in, gets through
to the application, and only then hands over. It reads the answers back off the
page itself afterwards -- a filler reporting its own success is precisely what
made the last branch hard to reason about, where "audit corrected 0" meant the
audit never ran and "verified: True" once meant an email address had been typed
into a marketing form.

Nothing is ever submitted here. A bake-off that submitted would be scoring
itself on real applications sent to real employers.
"""
from __future__ import annotations

import json
import os
import time

from . import log as _log
from .browser import browser_page
from .fillers import FillReport, get, load_all, names
from .models import Job
from .router import detect

log = _log.get("bakeoff")


def prepare(page, job: Job, profile: dict) -> tuple[bool, str]:
    """Get the page to the application: consent, apply click-through, sign-in.

    This is the part the last branch got right and is worth keeping whatever
    fills the form afterwards. A filler should never spend its budget on a
    cookie dialog.
    """
    from .workers import get_worker

    worker = get_worker(job.ats, page) or get_worker("generic", page)
    worker.open(job)                        # consent + apply click-through
    if worker.needs_auth():
        ok, detail = worker.try_sign_in(job)
        log.info("sign-in -> %s (%s)", ok, detail)
        if not ok:
            return False, f"sign-in failed: {detail}"
        # Settle, do not re-open. Signing in leaves the browser inside the
        # flow; navigating back to the job URL sends Workday through Apply
        # again on an application already in progress, and what comes back is
        # a page with no form on it -- "page did not render (timed out)" twice
        # and then a contender scored zero for a reason that was mine.
        worker.settle_step()
    if not worker.discover():
        worker.settle_step()

    from .gate import looks_like_an_application
    from .models import FillOutcome

    fields = worker.discover()
    if not fields:
        return False, "no fields on the page"
    if not looks_like_an_application(FillOutcome(job=job, fields=fields)):
        return False, ("not an application: "
                       + ", ".join((f.label or f.id or "?")[:30]
                                   for f in fields[:4]))
    # Hand it over at the top of the form. Discovery walks the whole page and
    # leaves it wherever it finished, which for an agent that reads a viewport
    # is not a neutral starting position -- it is most of the answer. browser-use
    # opened on the bottom of a 32-field Greenhouse form, saw Veteran Status and
    # Disability Status, filled both correctly, and stopped: from where it was
    # standing the application looked finished, and it scored 2 of 32 for a
    # judgement that was reasonable about the page it was shown. The contenders
    # that read markup cannot tell the difference, so this costs them nothing
    # and is the difference between a fair comparison and an unfair one.
    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
    except Exception:
        pass
    return True, f"{len(fields)} field(s) on the application"


def read_back(page, job: Job) -> tuple[dict[str, str], str]:
    """What the form actually holds now, read by the harness, and what went wrong.

    Scoring a filler on its own report is how you end up ranking the one that
    is most confident rather than the one that is most correct.

    The second half of the return exists because this failed silently and was
    scored as a zero. Skyvern spent thirty actions on a Greenhouse form --
    entering a phone country code, matching Rutgers University and Bachelor's
    Degree out of two custom dropdowns -- ran past AGENT_TIMEOUT, and its
    thread kept driving the page while this read it. Every per-field read threw
    and every throw was swallowed, so the scan returned an empty dict, and the
    table reported 0 filled of 0 found: not "we could not measure this" but
    "this contender did nothing", which is a different claim and a false one.
    """
    from .verify import _read_field
    from .workers import get_worker

    worker = get_worker(job.ats, page) or get_worker("generic", page)
    held: dict[str, str] = {}
    seen = failed = 0
    for f in worker.discover():
        seen += 1
        try:
            el = worker.frame_for(f).query_selector(f.selector)
            if el is None:
                continue
            value = (_read_field(page, f, el)[0] or "").strip()
        except Exception:
            failed += 1
            continue
        if value:
            held[f.label or f.id] = value
    if not seen:
        return held, "the readback found no fields to read"
    if failed == seen:
        return held, f"every one of the {seen} field(s) failed to read back"
    return held, ""


def _free_port() -> int:
    """A port nobody is on, for this run's browser to listen for agents on."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_one(filler_name: str, job: Job, profile: dict,
            shots: str = "data/screenshots") -> FillReport:
    filler = get(filler_name)
    ok, why = filler.available()
    if not ok:
        report = FillReport(filler=filler_name, job=job)
        report.errors.append(f"unavailable: {why}")
        return report

    started = time.time()
    # Open a debugging port for whichever contender needs one. browser_page
    # keeps this off by default and is right to -- a debugging port is not
    # something to open on an ordinary application run -- but the bake-off is
    # the one caller for which attaching is the entire point: browser-use and
    # Skyvern drive a browser, and the browser they must drive is the one the
    # harness already signed in and clicked through with. Without it both
    # returned "no CDP endpoint" and scored -0.7 in every family, which reads
    # as two agents that cannot fill a form and was really a setting nobody
    # set. A fresh port per contender, because the previous one's browser may
    # still be releasing its own.
    port = _free_port()
    os.environ["AUTOAPPLY_CDP_PORT"] = str(port)
    os.environ.pop("AUTOAPPLY_CDP_URL", None)

    # An agent's step budget, set for the bake-off rather than inherited. These
    # are the same variables the pipeline's agent fallback uses, and there they
    # are a cost brake on a lane that exists to get one stuck page moving --
    # .env pins AGENT_MAX_STEPS at 40. Here the job is a whole application, and
    # a budget that small measures the budget: browser-use worked steadily down
    # a Greenhouse form, filling name, education and eligibility, and stopped at
    # step 39 of 40 saying it would answer the rest "if time allows". Its own
    # default was raised to 150 the last time this happened; .env quietly put
    # it back. So set it explicitly, where the reason for the number is the
    # thing being measured.
    budget = os.getenv("BAKEOFF_AGENT_STEPS", "150")
    os.environ["AGENT_MAX_STEPS"] = budget
    os.environ["SKYVERN_MAX_STEPS"] = os.getenv("BAKEOFF_SKYVERN_STEPS", "60")
    # One action per step for the vision loop, so its budget has to exceed the
    # number of questions rather than the number of sections.
    os.environ["VISION_MAX_STEPS"] = os.getenv("BAKEOFF_VISION_STEPS", "80")
    with browser_page() as page:
        ready, detail = prepare(page, job, profile)
        if not ready:
            report = FillReport(filler=filler_name, job=job)
            report.errors.append(f"could not reach the application: {detail}")
            report.seconds = time.time() - started
            return report
        log.info("[%s] %s", filler_name, detail)

        try:
            report = filler.fill(page, job, profile)
        except Exception as exc:
            report = FillReport(filler=filler_name, job=job)
            report.errors.append(f"{type(exc).__name__}: {exc}")

        report.filler = filler_name
        report.seconds = time.time() - started

        # Whether the application reached its end is measured here, off the
        # page, not taken from the contender. Each filler was setting it
        # itself: computeruse and skyvern assert True when their agent says it
        # is done, dom passes through its own reached_end -- and browseruse
        # never set it at all, so the one term worth a hundred points was
        # nought for it in every run it has ever been in. It could not win a
        # bake-off no matter what it did on the page. The class this fills in
        # already promises the harness does the reading; this is the field
        # that was exempt from it.
        try:
            from .workers import get_worker

            probe = get_worker(job.ats, page)
            report.reached_review = bool(probe and probe.at_review())
        except Exception as exc:
            report.errors.append(f"could not tell whether it reached the end: {exc}")
            report.reached_review = False

        try:
            held, unread = read_back(page, job)
            if unread:
                report.errors.append(unread)
        except Exception as exc:
            held = {}
            report.errors.append(f"readback failed: {exc}")
        # Only when there is something to read. On a wizard's review page there
        # is not, and letting an empty scan overwrite what was verified step by
        # step scored dom at 0 filled on a form it had just walked to the end.
        if held:
            report.verified = held
            report.fields_found = max(report.fields_found, len(held))
        try:
            os.makedirs(shots, exist_ok=True)
            path = os.path.join(shots, f"bakeoff-{filler_name}-{int(time.time())}.png")
            page.screenshot(path=path, full_page=True)
            report.screenshot = path
        except Exception:
            pass
    return report


def bake_off(url: str, profile: dict, only: list[str] | None = None
             ) -> list[FillReport]:
    load_all()
    job = Job(url=url, ats=detect(url))
    contenders = only or names()
    reports = []
    for name in contenders:
        log.info("=== %s on %s", name, job.ats)
        reports.append(run_one(name, job, profile))
    reports.sort(key=lambda r: r.score(), reverse=True)
    return reports


def table(reports: list[FillReport]) -> str:
    rows = [f"{'filler':<14} {'score':>6} {'filled':>7} {'found':>6} "
            f"{'steps':>6} {'review':>7} {'secs':>6}  notes"]
    rows.append("-" * 92)
    rows.append("  (moves counts whatever each contender calls a step -- wizard "
                "steps for dom, actions for an\n   agent -- so it is shown, not "
                "scored.)")
    for r in reports:
        note = r.errors[0][:30] if r.errors else r.scored_by
        rows.append(f"{r.filler:<14} {r.score():>6.1f} {r.filled:>7} "
                    f"{r.fields_found:>6} {r.steps_advanced:>6} "
                    f"{'yes' if r.reached_review else 'no':>7} "
                    f"{r.seconds:>6.0f}  {note}")
    return "\n".join(rows)
