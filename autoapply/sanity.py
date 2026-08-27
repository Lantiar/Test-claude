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
    "sensible. You are given the job URL, the page it ended on, the verdicts "
    "it reached, and \"form\": every question it found, in page order, each "
    "beside the answer it gave.\n"
    "An \"answer\" of null means the field was left blank, which is ordinary "
    "and is never itself a problem. A question repeating -- three \"Company "
    "name\", eleven \"Description\" -- is a repeating section with several "
    "entries in it, not a mistake; judge each entry against the ones next to "
    "it.\n"
    "An entry marked \"answer_shortened\": true was cut to fit this payload. "
    "Never report such an answer as incomplete, cut off, or as not finishing "
    "its thought -- you are looking at an excerpt, not at what is on the "
    "form.\n"
    "An entry marked \"already_on_the_account\": true was NOT entered by this "
    "run. The ATS keeps the candidate's profile between applications, and "
    "writing into an entry that already holds an answer is what makes these "
    "forms reject a step, so the run deliberately leaves those alone. They are "
    "shown for context. Judge the run on what it entered, and do not report a "
    "value it did not put there.\n"
    "Report only what you can see. Metadata that is absent from this payload "
    "is absent from the payload, not missing from the application.\n"
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
    "Most runs are fine. An empty \"problems\" list is the normal result and "
    "the right answer whenever nothing here is actually wrong -- you are not "
    "expected to find something, and a report with nothing in it is a good "
    "report.\n"
    'Reply with JSON only: {"plausible": true|false, "problems": [{"question": '
    '"<the question this is about, copied exactly from "form", or null if the '
    'finding is about the page rather than about one answer>", "answer": '
    '"<the answer you are objecting to, copied exactly from "form">", '
    '"problem": "<short phrase>"}, ...]}\n'
    "A finding about an answer must quote both the question and the answer, "
    "exactly as they appear in \"form\". A finding whose question or answer is "
    "not there is discarded, so do not report one you cannot point to. If you "
    "are claiming two answers disagree, they must be two different answers -- "
    "the same answer given twice is a repeated question, not a contradiction. "
    "A finding about the page rather than about one answer carries no question "
    "and no answer."
)


# How many question/answer pairs the reviewer is shown. A TikTok application is
# 66 fields; sending them all is a few thousand characters and worth it, since
# what this tier judges is the run as a whole.
MAX_PAIRS = int(os.getenv("SANITY_MAX_PAIRS", "80"))

# How much of one answer. A "why do you want to work here" answer is two to
# four sentences, so this has to hold one whole -- the previous 80 characters
# cut every one of them mid-word, and the reviewer duly reported that they did
# not finish.
MAX_ANSWER = int(os.getenv("SANITY_MAX_ANSWER", "500"))


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

    Where an answer came from belongs beside it too. Workday keeps the
    candidate profile between applications, so a repeatable section arrives
    populated and the filler correctly leaves it alone -- writing into a
    populated entry is what produced "You can't add duplicate website URLs".
    Mastercard's account holds https://www.nideesh.ai under both of its "URL*"
    entries, and the reviewer reported "repeated 'URL*' question with the same
    answer" as a fault in the run. The run had not touched either field, and
    was right not to.
    """
    answers, sources = {}, {}
    for m in outcome.mappings:
        if m.action in ("fill", "generate") and m.value:
            answers[m.field_id] = str(m.value)
            sources[m.field_id] = m.source or ""

    pairs = []
    for f in outcome.fields:
        answer = answers.get(f.id)
        pair = {"question": (f.label or f.id or "?")[:120],
                "answer": None if answer is None else answer[:MAX_ANSWER]}
        if answer is not None and len(answer) > MAX_ANSWER:
            # Say so, because otherwise the reviewer judges our scissors. Every
            # answer was cut to 80 characters, so Notion's "Why do you want to
            # work at Notion?" -- 342 characters of finished prose -- reached
            # the reviewer severed mid-word, and it reported, accurately, that
            # the answer does not complete the thought.
            pair["answer_shortened"] = True
        if sources.get(f.id) == "account":
            pair["already_on_the_account"] = True
        pairs.append(pair)
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
        "form": _form_digest(outcome),
        "verdicts": {
            "saw_captcha": outcome.saw_captcha,
            "needs_sign_in": outcome.needs_auth,
            "reached_the_end": outcome.reached_end,
            "fields_filled": len(outcome.filled_ids),
            "fields_found": len(outcome.fields),
        },
    }
    # Only when we have one. Sent empty, it became a finding in its own right:
    # Mastercard's run was blocked on "Page title is empty", which says nothing
    # whatever about whether the application was filled in correctly. Workday
    # sets the title asynchronously and reading it a moment early returns "",
    # so the payload was describing our own timing.
    if page_title:
        payload["page_title"] = page_title

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

    problems = _cited(data.get("problems"), payload["form"])
    plausible = bool(data.get("plausible", True)) and not problems
    if not plausible:
        _log.get("sanity").warning("run looks wrong: %s", "; ".join(problems))
    return plausible, problems


def _cited(problems, pairs: list[dict]) -> list[str]:
    """Keep the findings that point at a question actually on this form.

    This tier can only ever add blockers -- by design, since a reviewer able to
    clear one could talk itself past the deterministic checks. The cost of that
    design is that an invented finding has no counterweight, and Mastercard's
    run was blocked by three of them at once, on an application that reached
    review with 40 of 47 fields filled and verified. One of the three was
    "Answer for 'Have you ever worked for Mastercard?' contradicts previous
    answer". The form asks that twice, in two wordings, and the run answered
    "No" to both.

    So a finding about an answer has to say which question it is about, and
    the question has to exist. That does not let the reviewer approve anything
    -- it still cannot clear a block -- it just holds it to the evidence it was
    given. A finding about the page rather than about an answer carries no
    question and is kept as it stands; those are the ones this tier exists for.
    """
    known = {(p.get("question") or "").strip().lower() for p in pairs}
    given = {((p.get("question") or "").strip().lower(),
              str(p.get("answer") or "").strip().lower()) for p in pairs}
    kept: list[str] = []
    for problem in (problems or []):
        if isinstance(problem, dict):
            text = str(problem.get("problem") or problem.get("text") or "").strip()
            question = str(problem.get("question") or "").strip()
            answer = str(problem.get("answer") or "").strip()
            if question and question.lower() not in known:
                _log.get("sanity").info(
                    "dropped a finding about %r, which is not on this form: %s",
                    _log.brief(question, 60), _log.brief(text, 80))
                continue
            # The same discipline, one level down. Citing a real question was
            # not enough: Mastercard's run was blocked twice more by findings
            # correctly addressed to real questions and wrong about them --
            # "What is your Race/Ethnicity?*: answer entered into a field that
            # plainly asks something else" (the answer was "Asian"), and "Have
            # you ever worked for Mastercard?*: duplicate question with
            # conflicting answers" (both said "No"). The payload it was shown
            # said exactly that. An objection has to be to something that is
            # actually on the form.
            if question and answer and (question.lower(), answer.lower()) not in given:
                _log.get("sanity").info(
                    "dropped a finding quoting %r as the answer to %r, which is "
                    "not what was entered: %s", _log.brief(answer, 40),
                    _log.brief(question, 50), _log.brief(text, 80))
                continue
            problem = f"{question}: {text}" if question else text
        problem = str(problem).strip()
        if problem:
            kept.append(problem)
    return kept[:5]
