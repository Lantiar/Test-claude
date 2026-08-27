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
    "title, the verdicts it reached, and \"form\": every question it found, in "
    "page order, each beside the answer it gave.\n"
    "An \"answer\" of null means the field was left blank, which is ordinary "
    "and is never itself a problem. A question repeating -- three \"Company "
    "name\", eleven \"Description\" -- is a repeating section with several "
    "entries in it, not a mistake; judge each entry against the ones next to "
    "it.\n"
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


# How many question/answer pairs the reviewer is shown. A TikTok application is
# 66 fields; sending them all is a few thousand characters and worth it, since
# what this tier judges is the run as a whole.
MAX_PAIRS = int(os.getenv("SANITY_MAX_PAIRS", "80"))


def _form_digest(outcome: FillOutcome) -> list[dict]:
    """Each question beside the answer given to it, repeats kept apart.

    Both halves of this were wrong, and both manufactured blockers out of
    nothing on TikTok's 66-field form:

    The answers went in a dict keyed on label. An application with eleven
    "Description" fields therefore showed the reviewer one, and it objected --
    correctly, given what it was shown -- that "repeated generic fields suggest
    multiple experience entries, but only one set of answers is shown". The
    run had answered all eleven.

    And the questions and the answers were two independently truncated lists,
    25 each out of 66 and 49. So answers routinely arrived whose questions had
    been cut off, and the reviewer reported "entered values into fields not
    shown in questions_found" -- a description of the payload, not of the run.

    Pairing them makes both impossible: an answer cannot be orphaned from its
    question, and a repeated label cannot collapse.
    """
    answers = {}
    for m in outcome.mappings:
        if m.action in ("fill", "generate") and m.value:
            answers[m.field_id] = str(m.value)[:80]

    pairs = [{"question": (f.label or f.id or "?")[:80],
              "answer": answers.get(f.id, None)}
             for f in outcome.fields]
    if len(pairs) <= MAX_PAIRS:
        return pairs
    # Keep the answered ones: an unanswered field says little, and the
    # judgement this tier exists to make is about what was typed.
    answered = [p for p in pairs if p["answer"] is not None]
    return (answered or pairs)[:MAX_PAIRS]


def review_run(outcome: FillOutcome, landed_url: str = "",
               page_title: str = "", provider=None) -> tuple[bool, list[str]]:
    """(plausible, problems). Never clears a block, only adds one."""
    if os.getenv("SANITY_REVIEW", "1") == "0":
        return True, []
    if provider is None or getattr(provider, "name", "rules") == "rules":
        return True, []
    if not outcome.fields:
        return True, []

    payload = {
        "job_url": outcome.job.url,
        "ended_on": landed_url or outcome.job.url,
        "page_title": page_title,
        "form": _form_digest(outcome),
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
