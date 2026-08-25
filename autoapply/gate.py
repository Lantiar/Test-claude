"""The safety gate: the only place that decides submit vs queue.

approve mode queues everything. auto mode submits unless something makes the
submission wrong rather than merely unreviewed:

  * nothing discovered, or nothing filled -> the page defeated us; submitting
                                             would send an empty application
  * a required field nothing could answer -> we'd send blanks or invented data
  * verification failed                   -> we don't know what's on the form
  * a CAPTCHA is present                  -> the run cannot proceed unattended
  * the daily cap or kill switch is set   -> operational brake

Sensitive fields (work authorization, demographics, salary) deliberately do NOT
queue: they are answered from explicit values in profile.json, and anything the
profile doesn't answer already lands in the missing-required check above.
"""
from __future__ import annotations

import os
import time

from .models import FillOutcome, GateResult, Job

KILL_SWITCH = "data/STOP"


def safety_gate(job: Job, outcome: FillOutcome, mode: str, store=None,
                profile: dict | None = None, provider=None) -> GateResult:
    if mode != "auto":
        return GateResult("queue", ["approve mode"])
    reasons = blockers(outcome, store, profile=profile, provider=provider)
    return GateResult("submit" if not reasons else "queue", reasons)


def blockers(outcome: FillOutcome, store=None, profile: dict | None = None,
             provider=None) -> list[str]:
    """Everything that would make this submission wrong, independent of mode.

    Split out from safety_gate so a dry run can report what *would* stop a real
    submit. Without it a dry run only ever says "dry run", which is the one
    thing the operator already knows.
    """
    reasons: list[str] = []

    if os.path.exists(os.getenv("KILL_SWITCH", KILL_SWITCH)):
        reasons.append("kill switch present")

    if store is not None:
        cap = int(os.getenv("DAILY_SUBMIT_CAP", "25"))
        if store.submits_since(time.time() - 86400) >= cap:
            reasons.append(f"daily submit cap reached ({cap})")

    # A page whose markup defeats discovery yields no fields, an empty
    # missing_required, and a verification pass that succeeds vacuously. Without
    # these two checks that reads as success and authorizes a submit on a form
    # where nothing was filled.
    if not outcome.fields:
        reasons.append("no form fields discovered")
    elif not outcome.filled_ids:
        reasons.append("nothing was filled")

    if outcome.saw_captcha:
        reasons.append("CAPTCHA present")
    if outcome.needs_auth:
        # Capability limit, not a permission gate: the login / account-creation
        # and mail-code services aren't built, so the run cannot get past this.
        reasons.append("sign-in or account creation required")
    if missing := outcome.missing_required:
        reasons.append(f"no answer for required: {', '.join(missing[:5])}")
    if not outcome.verified:
        reasons.append("verification failed")

    # The model's read of the answers themselves. Runs in both submit modes:
    # verification passing only means the form holds what we meant to type, not
    # that we meant the right thing, and in approve mode a human sees a list of
    # values rather than the reasoning behind them. Only ever adds reasons --
    # it cannot clear a block the deterministic rules already raised.
    if profile is not None and not reasons:
        from .presubmit import presubmit_review

        safe, blocking, note = presubmit_review(outcome, profile, provider)
        if not safe:
            reasons.extend(blocking or ["presubmit review declined"])
        outcome.verify_detail["_presubmit"] = {
            "safe": safe, "blocking": blocking, "note": note}

    return reasons
