"""Trim noise out of a posting title, without letting a model write one.

A title arrives from the employer and goes into the email verbatim, so it is
the one place text a stranger wrote reaches a recruiter under your name.
Most are fine. A minority carry a fragment that reads as machine output:
"- 2026 Start", "- 4 Months", "- Plus one semester".

A regex cannot make this call. In this feed "Geometry and 3D Vision" and
"- 2027 Summer" both contain digits, and only one of them is noise. So the
judgement is a model's -- but the model's only permitted action is to DELETE.
Its answer is accepted only if it is a subsequence of the original: same words,
same order, nothing introduced. It can never write a title, only choose which
of the employer's words to keep.

That is what keeps the email free of generated prose. Every word a recruiter
reads comes from the template or from the posting itself.
"""
from __future__ import annotations

import json
import os
import re

from .polish import MODEL, PRICES, _ask

# What makes a title worth asking about. Deliberately loose -- it decides only
# whether to spend a fraction of a cent, not what happens to the title.
_FLAGS = (
    ("a run of four or more digits", re.compile(r"\d{4,}")),
    ("a requisition id", re.compile(r"\b(?:req|jr|r)[-_ ]?\d{3,}\b", re.I)),
    ("a trailing fragment", re.compile(
        r"[-–|(]\s*(?:\d|plus\b|\w+\s+months?\b|\w+\s+start\b)", re.I)),
    ("a duration", re.compile(r"\b\d+\s*(?:months?|weeks?|semesters?)\b", re.I)),
    ("three or more separators", re.compile(r"(?:[-–|,].*){3,}")),
    ("a shouted word", re.compile(r"\b[A-Z]{5,}\b")),
    ("a repeated word", re.compile(r"\b(\w+)\b[\s,-]+\b\1\b", re.I)),
)

# Below this many words, a trim has taken the title with it.
MIN_WORDS = 2


def needs_review(title: str) -> str:
    """Why this title is worth a model's opinion, or '' if it plainly is not."""
    for reason, pattern in _FLAGS:
        if pattern.search(title or ""):
            return reason
    return ""


def _tokens(text: str) -> list[str]:
    return (text or "").split()


def is_subsequence(candidate: str, original: str) -> bool:
    """Every word of `candidate`, in order, drawn from `original`.

    The whole guarantee lives here. A model that reorders, rephrases,
    corrects a spelling or adds a word fails this, and the original is kept.
    """
    want, have = _tokens(candidate), _tokens(original)
    i = 0
    for token in have:
        if i < len(want) and want[i] == token:
            i += 1
    return i == len(want) and bool(want)


def tidy(title: str) -> str:
    """Drop a separator left dangling by a deletion. Punctuation only."""
    return re.sub(r"[\s,\-–|:/(]+$", "", (title or "").strip()).strip()


SYSTEM = """You clean up job posting titles for use in an email. You may only \
DELETE words. You may not add, reorder, rephrase, correct or abbreviate \
anything -- your answer is checked against the original and discarded if any \
word is new or out of order.

Remove only trailing or parenthetical noise that a person would not say aloud \
when naming the role:
- requisition or job numbers
- start dates, terms and seasons appended to the end ("- 2026 Start")
- durations ("- 4 Months", "Plus one semester")
- duplicated words

KEEP everything that identifies the role, including the team or product \
("- Global E-Commerce", "- Network Security"), and any digit that is part of \
the work itself ("3D Vision", "Front End 2").

If the title is already clean, return it unchanged. If nothing is left once the \
noise is gone, return an empty string.

Reply with JSON only: {"keep": "...", "reason": "a few words"}"""


def clean(title: str) -> dict:
    """{title, changed, reason, flagged, rejected, cost}.

    On any failure the original title is returned. A title that cannot be
    cleaned is not a reason to invent one.
    """
    out = {"title": title, "changed": False, "reason": "", "cost": 0.0,
           "flagged": needs_review(title), "rejected": ""}
    if not out["flagged"]:
        return out
    if not os.getenv("OPENAI_API_KEY"):
        out["rejected"] = "OPENAI_API_KEY is not set"
        return out

    answer, err, cost = _ask(
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": title}], os.environ["OPENAI_API_KEY"])
    out["cost"] = cost
    if err:
        out["rejected"] = err
        return out

    candidate = tidy(str(answer.get("subject") or answer.get("keep") or ""))
    if not candidate:
        out["rejected"] = "nothing left after trimming"
        return out
    if not is_subsequence(candidate, title):
        # The one outcome this design exists to make harmless.
        out["rejected"] = f"not a subsequence of the original: {candidate!r}"
        return out
    if len(_tokens(candidate)) < MIN_WORDS:
        out["rejected"] = f"trimmed to {candidate!r}, too little left to name a role"
        return out

    out.update(title=candidate, changed=candidate != tidy(title),
               reason=str(answer.get("reason") or ""))
    return out
