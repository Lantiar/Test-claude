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
    return True, f"{len(fields)} field(s) on the application"


def read_back(page, job: Job) -> dict[str, str]:
    """What the form actually holds now, read by the harness.

    Scoring a filler on its own report is how you end up ranking the one that
    is most confident rather than the one that is most correct.
    """
    from .verify import _read_field
    from .workers import get_worker

    worker = get_worker(job.ats, page) or get_worker("generic", page)
    held: dict[str, str] = {}
    for f in worker.discover():
        try:
            el = worker.frame_for(f).query_selector(f.selector)
            if el is None:
                continue
            value = (_read_field(page, f, el)[0] or "").strip()
        except Exception:
            continue
        if value:
            held[f.label or f.id] = value
    return held


def run_one(filler_name: str, job: Job, profile: dict,
            shots: str = "data/screenshots") -> FillReport:
    filler = get(filler_name)
    ok, why = filler.available()
    if not ok:
        report = FillReport(filler=filler_name, job=job)
        report.errors.append(f"unavailable: {why}")
        return report

    started = time.time()
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
        try:
            held = read_back(page, job)
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
    rows.append("-" * 86)
    for r in reports:
        note = r.errors[0][:30] if r.errors else r.scored_by
        rows.append(f"{r.filler:<14} {r.score():>6.1f} {r.filled:>7} "
                    f"{r.fields_found:>6} {r.steps_advanced:>6} "
                    f"{'yes' if r.reached_review else 'no':>7} "
                    f"{r.seconds:>6.0f}  {note}")
    return "\n".join(rows)
