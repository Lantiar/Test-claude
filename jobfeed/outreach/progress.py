"""Read the last day's mail and move applications forward.

The tracker only knows what you tell it, and nobody remembers to tell it. An
online assessment arrives on Tuesday, the stage still says "applied" three
weeks later, and the board stops being a picture of where things stand --
which is the only thing it is for.

So: every run, read yesterday's inbound mail, work out which application each
message belongs to, and advance the stage if the message says it moved.

Three rules keep this from doing damage.

**It only moves forward.** A scheduling email arriving after an offer must not
drag "offer" back to "interview". The one exception is a rejection, which can
arrive at any stage and is the end of the line wherever it lands.

**It never sets `accepted`.** That is you accepting an offer, not an employer
telling you something, and no email can establish it.

**Rejection is checked first.** "Unfortunately we will not be moving forward
after your interview" contains the word interview, and reading that as an
interview invitation would move a dead application forward and hide it from
the list of things that are actually alive.

The classifier is keywords, not a model. It runs over every message in the
mailbox on every pass, the phrases employers use are formulaic, and a wrong
answer here silently rewrites your record of where you stand -- so the rules
are ones you can read and correct, not a probability.
"""
from __future__ import annotations

import re
import time

from .. import normalize as _norm
from . import gmail

# Where a stage sits on the path. Higher is further along. `rejected` is not
# on the path at all: it can arrive from anywhere and nothing follows it.
ORDER = {"interested": 0, "applied": 1, "oa": 2, "interview": 3,
         "final": 4, "offer": 5, "accepted": 6}
TERMINAL = "rejected"

# Checked in this order, and the first hit wins. Rejection leads because a
# rejection often names the stage it is rejecting you from.
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (TERMINAL, (
        "not moving forward", "not be moving forward", "will not be moving",
        "decided not to move forward", "decided to move forward with other",
        "move forward with other candidates", "no longer under consideration",
        "not selected", "were not selected", "unable to offer you",
        "regret to inform", "we have decided not to", "unfortunately we",
        "position has been filled", "pursuing other candidates",
        "will not be progressing", "not to proceed with your application")),
    ("offer", (
        "pleased to offer", "offer of employment", "your offer letter",
        "we would like to offer", "extend an offer", "offer package")),
    ("final", (
        "final round", "final interview", "final stage", "onsite interview",
        "on-site interview", "superday", "super day", "hiring manager round",
        "last round")),
    ("oa", (
        "online assessment", "coding assessment", "coding challenge",
        "technical assessment", "take-home", "take home assignment",
        "hackerrank", "codesignal", "codility", "karat", "hirevue",
        "assessment link", "complete the assessment")),
    ("interview", (
        "schedule an interview", "schedule your interview", "interview invitation",
        "invitation to interview", "invite you to interview", "interview request",
        "would like to interview", "interview with the team", "interview for the",
        "phone screen", "phone interview", "technical interview",
        "recruiter screen", "set up a call", "schedule a call", "set up time",
        "schedule some time", "book a time", "availability for a call",
        "your availability", "move forward with an interview", "next round",
        "like to speak with you", "like to chat")),
)


def classify(subject: str, body: str) -> str:
    """The stage this message says you reached, or "" for none of them."""
    text = f"{subject}\n{body}".lower()
    for stage, phrases in RULES:
        if any(p in text for p in phrases):
            return stage
    return ""


def advance(current: str, detected: str) -> str:
    """The stage to store, or "" to leave it alone.

    Forward only, and never into `accepted` -- accepting an offer is a
    decision you make, not news an employer sends.
    """
    if not detected or detected == "accepted":
        return ""
    if current == "accepted":
        return ""
    if detected == TERMINAL:
        return "" if current == TERMINAL else TERMINAL
    if current == TERMINAL:
        return ""
    return detected if ORDER.get(detected, 0) > ORDER.get(current, 0) else ""


def _tokens(company: str) -> set[str]:
    """The words that identify an employer, minus the ones that identify none.

    "Inc" and "Technologies" match every other employer, so a message from
    anyone would look like a message from this one.
    """
    stop = {"inc", "llc", "ltd", "corp", "corporation", "company", "co",
            "the", "group", "holdings", "technologies", "technology", "labs",
            "systems", "solutions", "services", "global", "international"}
    words = re.findall(r"[a-z0-9]+", _norm.company(company).lower())
    return {w for w in words if len(w) > 2 and w not in stop}


def belongs_to(company: str, sender: str, subject: str, body: str) -> bool:
    """Is this message about that employer?

    The sender's domain first, because it is the hardest thing to coincide
    with. Applications are answered through an ATS as often as not, so a
    Greenhouse or Workday address will not carry the employer's name -- for
    those the name has to appear in the subject or the opening of the body.
    """
    want = _tokens(company)
    if not want:
        return False
    domain = (sender.rsplit("@", 1)[-1] if "@" in sender else "").lower()
    host = re.sub(r"[^a-z0-9]", "", domain.split(".")[0]) if domain else ""
    if host and any(w in host or host in w for w in want if len(w) > 3):
        return True
    # The employer's name in the words of the message. Only the first part of
    # the body: a signature block or a legal footer naming half the industry
    # is not this message being about them.
    hay = f"{subject}\n{body[:600]}".lower()
    return all(w in hay for w in want)


def recent(days: float = 1.0, token: str | None = None,
           limit: int = 120) -> list[dict]:
    """Inbound mail from the last `days`, as {from, subject, body}.

    Its own query rather than the reply watcher's history cursor: that one is
    consumed by `watch` and moves on, and two readers sharing one cursor means
    whichever runs second sees an empty mailbox.
    """
    token = token or gmail._token()
    query = f"in:inbox newer_than:{max(1, int(round(days)))}d"
    listing = gmail._get("messages", token, q=query, maxResults=limit)
    out = []
    for stub in listing.get("messages", []) or []:
        try:
            msg = gmail._get(f"messages/{stub['id']}", token, format="full")
        except Exception:
            continue
        headers = {h["name"].lower(): h["value"]
                   for h in msg.get("payload", {}).get("headers", [])}
        out.append({
            "id": stub["id"],
            "from": _address(headers.get("from", "")),
            "subject": headers.get("subject", ""),
            # The snippet, not the whole body: it is already decoded, it is the
            # opening of the message, and the phrases that matter are in the
            # first line or two rather than the legal footer.
            "body": msg.get("snippet", "") or "",
            "at": int(msg.get("internalDate", 0)) / 1000,
        })
    return out


def _address(value: str) -> str:
    at = value.rfind("<")
    return (value[at + 1:].rstrip(">") if at >= 0 else value).strip().lower()


def scan(applications: dict[str, str], jobs: dict[str, str],
         messages: list[dict]) -> dict[str, dict]:
    """job_key -> {"stage": new, "was": old, "why": subject}.

    Pure: the caller supplies what it knows and gets back what changed, so the
    decision can be tested without a mailbox. `applications` is job_key ->
    current stage, `jobs` is job_key -> company name.
    """
    moves: dict[str, dict] = {}
    for msg in messages:
        detected = classify(msg.get("subject", ""), msg.get("body", ""))
        if not detected:
            continue
        for job_key, current in applications.items():
            company = jobs.get(job_key) or ""
            if not company:
                continue
            # Anything already decided this pass wins if it is further along,
            # so two messages about one job settle on the furthest.
            settled = moves.get(job_key, {}).get("stage") or current
            if not belongs_to(company, msg.get("from", ""),
                              msg.get("subject", ""), msg.get("body", "")):
                continue
            nxt = advance(settled, detected)
            if nxt:
                moves[job_key] = {"stage": nxt, "was": current,
                                  "why": (msg.get("subject") or "")[:120],
                                  "from": msg.get("from", "")}
    return moves
