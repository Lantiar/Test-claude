"""The message.

Two families, because the same words do not work on both audiences.

`warm` is for someone who already knows you -- a recruiter you spoke to, or who
interviewed you. It can say "since we last spoke" because that happened.

`cold` is for a recruiter who has never heard of you, which is every contact
this pipeline finds. The warm template sent cold is worse than a bad email: a
stranger reading "turning down the previous offer was difficult" concludes you
have mistaken them for someone else, and the credibility you were borrowing
from the Google line evaporates in the same sentence.

Both are plain text. No HTML, no tracking pixel, no unsubscribe footer, no
shortened links -- each of those is a bulk-mail signal, and a genuine
one-to-one email carries none of them.

Variants exist so that fifty sends are not fifty identical strings. They rotate
per contact, deterministically from the contact id, so a re-render produces the
same message rather than a new one.
"""
from __future__ import annotations

import datetime as dt
import re

from .profile import ME, WINS, signature

# ---- subjects -------------------------------------------------------------
# The bracket does the work. A recruiter scanning an inbox decides in the
# subject line whether this is a student who applied or a student worth
# opening, and the previous employer is the only fact short enough to fit.
SUBJECTS = [
    "[Prev {prev}] {season_role} - applied, would love to connect",
    "[Prev {prev}] Rutgers '28 - {season_role} application",
    "[Prev {prev}] {company} {season_role} - quick note from an applicant",
]

# ---- openers --------------------------------------------------------------
OPENERS = [
    "I applied for the {role} role at {company}{for_season} and wanted to "
    "put a name to the application.",
    "I just applied to {company}'s {season_role} opening and wanted to "
    "introduce myself directly.",
    "I submitted an application for {company}'s {season_role} position and "
    "thought it was worth reaching out.",
]

# ---- follow-ups -----------------------------------------------------------
FOLLOWUPS = {
    1: ("Following up on my note about the {role} role at {company}. Happy to "
        "send anything useful from my side, and I understand if timelines are "
        "tight."),
    2: ("Last note from me on the {role} role at {company} - if the timing is "
        "wrong I completely understand. I would welcome the chance to be "
        "considered for anything else on the team."),
}


def _pick(options, contact_id: int):
    """Deterministic per contact, so re-rendering does not rewrite history."""
    return options[contact_id % len(options)]


def usable_season(season: str | None, now: dt.date | None = None) -> str:
    """The season, or nothing if quoting it would be a mistake.

    Sources disagree: Simplify listed the Annapurna Labs role as "Fall 2026"
    while its own URL ended in -2027. There is no way to tell from here which
    is right, and naming the wrong intake to a recruiter reads worse than
    naming none -- so a season that has already started is dropped rather than
    guessed at. Omitted, the sentence still says exactly what it needs to.
    """
    if not season:
        return ""
    m = re.search(r"(spring|summer|fall|autumn|winter)\s*(20\d\d)", season, re.I)
    if not m:
        return ""
    today = now or dt.date.today()
    starts = {"spring": 3, "summer": 5, "fall": 9, "autumn": 9, "winter": 12}
    year, month = int(m.group(2)), starts[m.group(1).lower()]
    # Two months of lead, not zero. Applications for an intake close long
    # before it starts, so "Fall 2026" written in late August is either the
    # wrong intake or a role you are too late for -- either way not a thing to
    # assert to the person who runs the process.
    if (year * 12 + month) <= (today.year * 12 + today.month + 2):
        return ""
    return f"{m.group(1).title()} {year}"


def render(contact: dict, job: dict, step: int = 0) -> tuple[str, str, str]:
    """(subject, body, variant name). `job` needs company, role, season."""
    role = job.get("role") or "Software Engineer Intern"
    season = usable_season(job.get("season"))
    fields = {
        "prev": ME["prev"],
        "company": job.get("company") or "your team",
        "role": role,
        "season": season,
        "season_role": f"{season} {role}".strip(),
        "for_season": f" for {season}" if season else "",
        # LinkedIn's firstName is whatever the person typed, and people put
        # more than one word in it: "Jeevan Lobo S." arrives as first name
        # "Jeevan Lobo". Greeting someone by two names is the tell that a
        # script wrote it.
        "first_name": (contact.get("first_name") or "there").split()[0],
    }
    cid = int(contact.get("id") or 0)
    subject = _pick(SUBJECTS, cid).format(**fields)
    variant = f"s{cid % len(SUBJECTS)}o{cid % len(OPENERS)}"

    if step > 0:
        body = (f"Hi {fields['first_name']},\n\n"
                + FOLLOWUPS[step].format(**fields)
                + f"\n\nThanks,\n{ME['first_name']}\n")
        # A follow-up keeps the original subject: it threads, and a new one
        # reads as a second cold email rather than a nudge on the first.
        return subject, body, f"{variant}f{step}"

    wins = "\n".join(f"  - {label}: {text}" for label, text in WINS[:3])
    body = (
        f"Hi {fields['first_name']},\n\n"
        f"{_pick(OPENERS, cid).format(**fields)}\n\n"
        f"I am a {ME['degree']} student at {ME['school']} "
        f"({ME['honors']}, {ME['gpa']} GPA), graduating {ME['grad']}. "
        f"A few things I have worked on:\n\n"
        f"{wins}\n\n"
        f"I would be glad to be considered, and would appreciate any guidance "
        f"on the process. Resume and projects are at {ME['portfolio']}.\n\n"
        f"Thanks,\n{ME['first_name']}\n\n"
        f"{signature()}\n"
    )
    return subject, body, variant
