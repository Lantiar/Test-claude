"""link -> apply.

Two lanes:
  * DOM lane (Greenhouse, Lever, Workday) — Playwright, deterministic, no model.
  * Agent lane (iCIMS, Ashby, Oracle/Taleo, unknown) — browser-use with a
    per-ATS playbook, verified by an independent judge pass.

A DOM fill that fails verification falls back to the agent lane once. The gate
is the only thing that decides whether anything is submitted, in either lane.
"""
from __future__ import annotations

import json
import os
import traceback
from typing import Callable

from . import mapper, router
from .browser import browser_page
from .gate import safety_gate
from .judge import judge
from .llm import get_provider
from .models import ApplyResult, FillOutcome, GateResult, Job
from .store import Store
from .verify import verify
from .workers import get_worker
from .workers.agent import AgentUnavailable, AgentWorker


def load_profile(path: str | None = None) -> dict:
    path = path or os.getenv("PROFILE_PATH", "config/profile.json")
    if not os.path.exists(path):
        raise SystemExit(
            f"No profile at {path}. Copy config/profile.example.json to {path} "
            "and fill it in."
        )
    with open(path) as fh:
        return json.load(fh)


def apply_to(url: str, mode: str | None = None, store: Store | None = None,
             profile: dict | None = None, dry_run: bool = False,
             ats_override: str | None = None,
             overrides: dict[str, str] | None = None) -> ApplyResult:
    mode = (mode or os.getenv("MODE", "approve")).lower()
    store = store or Store()
    profile = profile if profile is not None else load_profile()
    provider = get_provider()
    shots = os.getenv("SCREENSHOT_DIR", "data/screenshots")

    job = router.parse_job(url)
    if ats_override:
        job.ats = ats_override

    if store.already_applied(job.key):
        return ApplyResult(job, None, GateResult("skip", ["already applied"]),
                           "skipped", "already applied")

    try:
        if job.ats in router.DOM_WORKERS:
            result = _run_dom(job, profile, store, provider, shots, mode,
                              dry_run, overrides)
            # One fallback, never a loop.
            if result.status == "queued" and _needs_agent_fallback(result.outcome):
                fallback = _run_agent(job, profile, store, mode, dry_run, shots,
                                      note=_fallback_note(result.outcome))
                if fallback.status == "applied" or (
                        fallback.outcome and fallback.outcome.verified):
                    return fallback
                # Fallback didn't rescue it. Both lanes wrote a queue row for the
                # same job, so the later write would otherwise be all you see;
                # merge the reasons and record them once so the queue entry says
                # what the DOM worker hit *and* why the agent couldn't help.
                for reason in fallback.gate.reasons:
                    if reason not in result.gate.reasons:
                        result.gate.reasons.append(reason)
                store.enqueue(job, result.outcome, result.gate.reasons)
                result.detail = "; ".join(result.gate.reasons)
            return result

        return _run_agent(job, profile, store, mode, dry_run, shots)

    except Exception as exc:
        store.record_applied(job, "errored")
        return ApplyResult(job, None, GateResult("queue", ["error"]), "errored",
                           f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")


def _needs_agent_fallback(outcome: FillOutcome | None) -> bool:
    """Is this a failure the agent could plausibly do better on?

    Empty discovery counts: markup the DOM worker cannot read — an iframe, a
    shadow root, a tenant that renders differently — is exactly what the agent
    lane exists for. A CAPTCHA or a login wall is not: the agent is blocked by
    those too, so retrying just costs a run.
    """
    if outcome is None:
        return True
    if outcome.saw_captcha or outcome.needs_auth:
        return False
    return (not outcome.fields) or (not outcome.filled_ids) or (not outcome.verified)


def _fallback_note(outcome: FillOutcome | None) -> str:
    if outcome is None or not outcome.fields:
        return "agent fallback after empty discovery"
    if not outcome.filled_ids:
        return "agent fallback after nothing was filled"
    return "agent fallback after failed verify"


def _run_dom(job: Job, profile: dict, store, provider, shots: str, mode: str,
             dry_run: bool, overrides: dict[str, str] | None) -> ApplyResult:
    with browser_page() as page:
        worker = get_worker(job.ats, page)
        if worker is None:
            return ApplyResult(job, None, GateResult("skip", ["no worker"]),
                               "skipped", f"no worker for {job.ats}")

        # Overrides are applied inside the mapper call the worker makes, so they
        # are threaded through the profile-independent path here.
        if overrides:
            worker.overrides = overrides
        outcome = worker.run(job, profile, store, provider, shots)
        if overrides:
            _apply_overrides(outcome.mappings, overrides, job.ats, store)

        if not worker.verifies_internally:
            outcome = verify(page, outcome)

        return _decide(job, outcome, mode, store, dry_run, worker.submit)


def _run_agent(job: Job, profile: dict, store, mode: str, dry_run: bool,
               shots: str, note: str = "") -> ApplyResult:
    worker = AgentWorker()
    try:
        outcome = worker.run(job, profile, store, None, shots)
    except AgentUnavailable as exc:
        outcome = FillOutcome(job=job)
        outcome.errors.append(str(exc))
        store.enqueue(job, outcome, [f"agent unavailable: {exc}"])
        return ApplyResult(job, outcome, GateResult("queue", ["agent unavailable"]),
                           "queued", str(exc))

    # The agent does not get to grade itself; the judge reads the page fresh.
    outcome = judge(outcome)
    result = _decide(job, outcome, mode, store, dry_run, worker.submit)
    if note:
        result.detail = f"{note}; {result.detail}"
    return result


def _decide(job: Job, outcome: FillOutcome, mode: str, store, dry_run: bool,
            submitter: Callable[[], tuple[bool, str]]) -> ApplyResult:
    gate = safety_gate(job, outcome, mode, store=store)

    if dry_run:
        return ApplyResult(job, outcome, GateResult("queue", ["dry run"]),
                           "queued", "dry run: nothing submitted")

    if gate.decision == "submit":
        ok, detail = submitter()
        if ok:
            store.record_applied(job, "applied", outcome.screenshot_path,
                                 _submitted_values(outcome))
            return ApplyResult(job, outcome, gate, "applied", detail)
        gate.reasons.append(f"submit unconfirmed ({detail})")
        store.enqueue(job, outcome, gate.reasons)
        return ApplyResult(job, outcome, gate, "queued", detail)

    store.enqueue(job, outcome, gate.reasons)
    return ApplyResult(job, outcome, gate, "queued", "; ".join(gate.reasons))


def _submitted_values(outcome: FillOutcome) -> dict:
    """Keep a record of what actually went out under your name."""
    return {m.label or m.field_id: m.value
            for m in outcome.mappings if m.action in ("fill", "generate") and m.value}


def _apply_overrides(mappings, overrides: dict[str, str], ats: str, store) -> None:
    for m in mappings:
        key = m.label or m.field_id
        if key in overrides:
            m.value = overrides[key]
            m.action = "fill" if overrides[key] else "skip"
            m.confidence, m.source = 1.0, "human"
            if store is not None and m.label:
                store.record_correction(mapper.signature(ats, m.label), m.label, m.value)
