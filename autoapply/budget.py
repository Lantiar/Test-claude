"""Keep a model call small enough that it actually happens.

Rate limits are the failure mode nobody sees. A 429 does not raise a red
banner; it makes a tier return nothing, and a tier that returns nothing looks
exactly like a tier that found nothing wrong. "audit corrected 0" meant the
audit never ran. The tiers were silently switched off for whole runs and the
fixes ended up being made by hand, which is precisely the thing the design is
supposed to make unnecessary.

The bulk is not the profile (~450 tokens) or the questions (~20 each). It is
the picklists. A Workday step carries Country and Country Phone Code at ~250
options apiece, and every tier -- map, answer, audit, repair -- ships both in
full. That is ~4k tokens of "Afghanistan (+93)" per call, four calls a step,
for a field whose answer any model already knows.

So a long list is sampled rather than sent: the entries that look like they
might be the answer, plus an even spread for shape, plus the true count. The
model replies in ordinary words and resolve_option() matches that against the
*full* list locally, which it already had to do anyway -- models paraphrase
options whether or not they were shown them.

A short list is sent whole. Lists under the limit are the bespoke ones ("How
did you hear about us?", a company's own EEO wording) where the options are the
question and sampling would destroy it.
"""
from __future__ import annotations

import os
import re

# Above this, a list is sampled. Chosen to sit above the bespoke lists (a "how
# did you hear about us" is 15-30 entries) and below the closed vocabularies
# (states ~60, countries ~250) that a model knows without being told.
MAX_OPTIONS = int(os.getenv("AUTOAPPLY_MAX_OPTIONS", "40"))


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def digest_options(options, hints=(), limit: int | None = None):
    """(what to show the model, the full count if it was sampled).

    hints are strings the answer is likely to resemble -- the question, the
    value already entered, the candidate's own facts. An option matching one is
    kept, so when the profile says "United States" the model is shown
    "United States of America (+1)" verbatim and can return it exactly.
    """
    options = [o for o in (options or []) if o]
    limit = MAX_OPTIONS if limit is None else limit
    if len(options) <= limit:
        return options, None

    hint_tokens: set[str] = set()
    phrases: list[str] = []
    for hint in hints:
        text = str(hint or "").strip()
        if not text:
            continue
        hint_tokens |= _tokens(text)
        if len(text) >= 3:
            phrases.append(text.lower())

    keep: set[int] = set()
    scored: list[tuple[int, int]] = []
    for i, option in enumerate(options):
        low = option.lower()
        score = len(_tokens(option) & hint_tokens)
        if any(p in low or low in p for p in phrases):
            score += 3
        if score:
            scored.append((-score, i))
    scored.sort()
    for _, i in scored[:limit]:
        keep.add(i)

    # Whatever budget the matches left over goes on an even spread, so the model
    # can see the shape of an entry ("Country (+code)") even when nothing in the
    # profile resembles the answer.
    room = limit - len(keep)
    if room > 0:
        step = max(1, len(options) // room)
        for i in range(0, len(options), step):
            if len(keep) >= limit:
                break
            keep.add(i)

    return [options[i] for i in sorted(keep)], len(options)


def describe(field_options, hints=(), limit: int | None = None) -> dict:
    """The options half of a field payload, sampled if long."""
    shown, total = digest_options(field_options, hints, limit)
    out: dict = {"options": shown}
    if total is not None:
        out["option_count"] = total
        out["options_sampled"] = True
    return out


# Every prompt that can receive a sampled list has to say so, or the model
# treats the sample as exhaustive and returns null for an answer that is in the
# full list but was not shown.
SAMPLED_NOTE = (
    "- When a field has \"options_sampled\": true, the options shown are a "
    "sample of a longer list (\"option_count\" gives its real size). Answer in "
    "the natural wording of the correct choice -- it is matched against the "
    "full list afterwards. Do not return null merely because the exact entry "
    "is not among the ones shown."
)
