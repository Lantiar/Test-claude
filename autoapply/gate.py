"""The safety gate: the only place that decides submit vs queue.

approve mode queues everything. auto mode submits unless something makes the
submission wrong rather than merely unreviewed:

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


def safety_gate(job: Job, outcome: FillOutcome, mode: str, store=None) -> GateResult:
    if mode != "auto":
        return GateResult("queue", ["approve mode"])

    reasons: list[str] = []

    if os.path.exists(os.getenv("KILL_SWITCH", KILL_SWITCH)):
        reasons.append("kill switch present")

    if store is not None:
        cap = int(os.getenv("DAILY_SUBMIT_CAP", "25"))
        if store.submits_since(time.time() - 86400) >= cap:
            reasons.append(f"daily submit cap reached ({cap})")

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

    return GateResult("submit" if not reasons else "queue", reasons)
