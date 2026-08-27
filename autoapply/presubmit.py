"""A model's read of a filled form, immediately before it would be sent.

Distinct from verify.py and from judge.py, and it runs in both submit modes:

  * verify.py asks "is what we typed what is on the form?" -- deterministic
    readback, and it cannot tell a correct value from a wrong one that stuck.
  * judge.py grades an *agent's* fill, because the agent must not grade itself.
  * this asks the question neither of those does: is this answer set right for
    this candidate, and is it safe to send under their name?

Verification passing means the form holds what we meant to put there. It says
nothing about whether we meant the right thing -- a required question answered
from a rule that pattern-matched the wrong profile field verifies perfectly.
That is the gap this closes, and it is why it runs even in approve mode, where
a human is also looking: the human sees a list of values, not the reasoning
about whether "Are you currently enrolled?" should really say "Rutgers
University".

It can only ever add reasons to block. It cannot approve a submission that the
gate's deterministic rules already stopped, and with no model configured it
declines rather than passing by default.
"""
from __future__ import annotations

import json
import os

from .models import FillOutcome

REVIEW_SYSTEM = (
    "You are the last check before a job application is submitted under a real "
    "person's name. You are given their profile and every answer about to be "
    "sent.\n"
    "Block the submission if any of these is true:\n"
    "  - an answer contradicts the profile (wrong school, employer, dates, "
    "authorization, degree)\n"
    "  - an answer is a placeholder, a truncated fragment, or obviously the "
    "wrong kind of value for the question (a school name answering a yes/no, "
    "a phone number in a name field)\n"
    "  - a required question is unanswered or answered with something that "
    "does not respond to it\n"
    "  - an answer asserts something the profile does not support\n"
    "Do NOT block for style, brevity, or an answer merely being unremarkable. "
    "An answer that is truthful and responsive is fine.\n"
    "Do NOT block for any of these, which look like contradictions and are "
    "not:\n"
    "  - a picklist answer that is the closest offered option rather than the "
    "profile's exact wording. A form can only be answered with what it offers: "
    "if the profile says the source was a company website and the menu lists "
    "only Job Board, University/College and Recruiter, the closest of those is "
    "the correct answer, not a contradiction.\n"
    "  - the form's own wording of a profile fact -- \"Bachelors\" for "
    "\"Bachelor's Degree\", \"New Jersey\" for \"NJ\", a URL with or "
    "without www.\n"
    "  - an answer whose source is \"account\": that value was already on the "
    "candidate's saved profile at this employer and was deliberately left "
    "untouched. It is not something being submitted on our say-so.\n"
    "  - a signature or acknowledgement date holding today's date, given "
    "above. Signing a form today is not an unsupported claim.\n"
    'Reply with JSON only: {"safe_to_submit": true|false, '
    '"blocking": ["short reason", ...], "notes": "one sentence"}'
)


def _payload(outcome: FillOutcome, profile: dict) -> str:
    by_id = {f.id: f for f in outcome.fields}

    def entry(m):
        field = by_id.get(m.field_id)
        row = {"question": m.label or m.field_id,
               "answer": m.value,
               "source": m.source,
               "required": bool(field.required) if field else False}
        # What the field actually offered. Told that the closest offered option
        # is a correct answer, the reviewer still blocked "University Job
        # Board" against a profile saying "Company website" -- because it was
        # never shown that the menu holds no such entry. A rule it cannot check
        # against evidence is a rule it cannot apply.
        if field is not None and field.options:
            row["options_offered"] = field.options[:25]
        return row

    answers = [entry(m) for m in outcome.mappings
               if m.action in ("fill", "generate") and m.value
               # Values already on the candidate's saved profile at this
               # employer, which the filler deliberately left untouched. We are
               # reviewing the answers we produced.
               and m.source != "account"]
    unanswered = [
        (f.label or f.id) for f in outcome.fields
        if f.required and f.id not in outcome.filled_ids
    ]
    from .llm import today_note

    return (
        today_note() + "\n\nCandidate profile:\n" + json.dumps(profile, indent=2)
        + "\n\nAnswers about to be submitted:\n" + json.dumps(answers, indent=2)
        + "\n\nRequired questions left unanswered:\n" + json.dumps(unanswered)
    )


def presubmit_review(outcome: FillOutcome, profile: dict,
                     provider=None) -> tuple[bool, list[str], str]:
    """(safe_to_submit, blocking reasons, note).

    A model that cannot be reached, replies with nothing usable, or is not
    configured returns False. Declining to submit costs a queued application;
    passing by default costs a wrong one sent under someone's name.
    """
    if os.getenv("PRESUBMIT_REVIEW", "1") in ("0", "false", "no"):
        return True, [], "presubmit review disabled"

    if provider is None:
        from .llm import get_provider
        provider = get_provider()
    if getattr(provider, "name", "rules") == "rules":
        return False, ["presubmit review unavailable (no model configured)"], ""

    try:
        raw = provider._chat(REVIEW_SYSTEM, _payload(outcome, profile))
    except AttributeError:
        return False, ["presubmit review unavailable (provider has no chat)"], ""
    except Exception as exc:
        return False, [f"presubmit review failed: {type(exc).__name__}"], ""

    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return False, ["presubmit review returned nothing usable"], ""
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return False, ["presubmit review returned nothing usable"], ""

    blocking = [str(b) for b in (data.get("blocking") or []) if str(b).strip()]
    safe = bool(data.get("safe_to_submit")) and not blocking
    return safe, blocking, str(data.get("notes") or "")
