"""link -> apply. Orchestrates route, fill, verify, gate, submit|queue."""
from __future__ import annotations

import json
import os
import traceback

from . import mapper, router
from .browser import browser_page
from .gate import safety_gate
from .llm import get_provider
from .models import ApplyResult, FillOutcome, GateResult
from .store import Store
from .verify import verify
from .workers import get_worker


def load_profile(path: str | None = None) -> dict:
    path = path or os.getenv("PROFILE_PATH", "config/profile.json")
    if not os.path.exists(path):
        example = "config/profile.example.json"
        raise SystemExit(
            f"No profile at {path}. Copy {example} to {path} and fill it in."
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
        # For a company hosting a Greenhouse/Lever-shaped form on its own domain,
        # and for driving the local fixtures in tests.
        job.ats = ats_override

    if store.already_applied(job.key):
        return ApplyResult(job, None, GateResult("skip", ["already applied"]),
                           "skipped", "already applied")

    if job.ats not in router.SUPPORTED:
        # Workday/Ashby/iCIMS/unknown need the agent path, which isn't built yet.
        store.record_applied(job, "skipped")
        return ApplyResult(job, None, GateResult("skip", [f"{job.ats} has no worker yet"]),
                           "skipped", f"{job.ats} not supported yet")

    try:
        with browser_page() as page:
            worker = get_worker(job.ats, page)
            worker.open(job)

            fields = worker.discover()
            mappings = mapper.map_fields(fields, profile, job.ats,
                                         store=store, provider=provider)
            if overrides:
                # Values a human corrected in the dashboard win over everything.
                _apply_overrides(mappings, overrides, job.ats, store)
            outcome = worker.fill(job, fields, mappings, shots)
            outcome = verify(page, outcome)

            gate = safety_gate(job, outcome, mode, store=store)

            if dry_run:
                return ApplyResult(job, outcome, GateResult("queue", ["dry run"]),
                                   "queued", "dry run: nothing submitted")

            if gate.decision == "submit":
                ok, detail = worker.submit()
                if ok:
                    store.record_applied(job, "applied", outcome.screenshot_path,
                                         _submitted_values(outcome))
                    return ApplyResult(job, outcome, gate, "applied", detail)
                # Clicked but never confirmed: do not claim an application.
                gate.reasons.append(f"submit unconfirmed ({detail})")
                store.enqueue(job, outcome, gate.reasons)
                return ApplyResult(job, outcome, gate, "queued", detail)

            store.enqueue(job, outcome, gate.reasons)
            return ApplyResult(job, outcome, gate, "queued", "; ".join(gate.reasons))

    except Exception as exc:
        store.record_applied(job, "errored")
        return ApplyResult(job, None, GateResult("queue", ["error"]), "errored",
                           f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")


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
                store.record_correction(mapper.signature(ats, m.label),
                                        m.label, m.value)
