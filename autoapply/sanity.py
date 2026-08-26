"""Audit the run, not just the fields.

Everything else here learns answers. The unit of learning is one row --
(ats, label) -> value -- so the loop can be taught that Country Phone Code is
"United States of America (+1)" and cannot be taught anything about whether the
page was an application, whether a CAPTCHA was really there, or whether a tier
silently stopped working. Those are capability failures, one level below where
the loop operates, and each of them arrives as silence rather than as a wrong
answer:

  * "audit corrected 0" meant the audit never ran, not that the form was clean.
  * "'No' would not stick" was the whole record of the explore tier breaking on
    its first click, every time, on every input-backed control.
  * "CAPTCHA present" was a JSON translation bundle containing the word.
  * BNY's expired posting was swapped for the careers home page client-side,
    and the run typed the job title into the site's search box and the
    candidate's portfolio URL into a customer-service chat widget. Nothing
    objected, so the loop concluded it was doing well and learned
    "Tech - Software Engineering filter 106".

The common shape: a failure indistinguishable from a clean result, and every
tier below the perception layer working perfectly on the wrong thing. The form
accepting a step is good evidence that an answer is right; it is no evidence at
all that the form was the right form.

So this asks a model the question none of the other tiers can: given what the
run says it did, is any of this plausible? It only ever adds blockers -- a
reviewer that could clear one would be able to talk itself past the
deterministic checks, which is the opposite of the point.
"""
from __future__ import annotations

import json
import os

from . import log as _log
from .models import FillOutcome

SYSTEM = (
    "You are checking whether an automated job-application run did something "
    "sensible. You are given the job URL, the page it ended on, that page's "
    "title, the questions it found, what it entered, and the verdicts it "
    "reached.\n"
    "Report anything IMPLAUSIBLE. In particular:\n"
    "- The page is not a job application at all. A job search box, a location "
    "filter, a newsletter or lead-capture box, a support or careers chat "
    "widget, a cookie dialog, a talent-community sign-up: filling any of those "
    "means the run is on the wrong page, however willingly they accepted the "
    "values.\n"
    "- A verdict that does not match the evidence -- a CAPTCHA reported on a "
    "page whose fields are an ordinary form, a sign-in wall reported on a page "
    "asking application questions.\n"
    "- An answer entered into a field that plainly asks something else.\n"
    "Say nothing about answers that are merely terse, and nothing about a run "
    "that is simply incomplete: stopping early is normal and is not itself "
    "implausible.\n"
    'Reply with JSON only: {"plausible": true|false, "problems": '
    '["short phrase", ...]}'
)


def review_run(outcome: FillOutcome, landed_url: str = "",
               page_title: str = "", provider=None) -> tuple[bool, list[str]]:
    """(plausible, problems). Never clears a block, only adds one."""
    if os.getenv("SANITY_REVIEW", "1") == "0":
        return True, []
    if provider is None or getattr(provider, "name", "rules") == "rules":
        return True, []
    if not outcome.fields:
        return True, []

    entered = {(m.label or m.field_id): m.value for m in outcome.mappings
               if m.action in ("fill", "generate") and m.value}
    payload = {
        "job_url": outcome.job.url,
        "ended_on": landed_url or outcome.job.url,
        "page_title": page_title,
        "questions_found": [f.label or f.id for f in outcome.fields][:25],
        "answers_entered": {k: str(v)[:80] for k, v in list(entered.items())[:25]},
        "verdicts": {
            "saw_captcha": outcome.saw_captcha,
            "needs_sign_in": outcome.needs_auth,
            "reached_the_end": outcome.reached_end,
            "fields_filled": len(outcome.filled_ids),
            "fields_found": len(outcome.fields),
        },
    }

    try:
        raw = provider._chat(SYSTEM, json.dumps(payload, indent=2))
    except Exception as exc:
        # A reviewer that cannot be reached must not quietly approve. Say so.
        _log.get("sanity").info("run review unavailable: %s", type(exc).__name__)
        return True, []

    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return True, []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return True, []

    problems = [str(p) for p in (data.get("problems") or []) if p][:5]
    plausible = bool(data.get("plausible", True)) and not problems
    if not plausible:
        _log.get("sanity").warning("run looks wrong: %s", "; ".join(problems))
    return plausible, problems
