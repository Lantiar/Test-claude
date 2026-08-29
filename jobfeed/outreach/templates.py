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

from .profile import ME, WINS, bracket, signature

# ---- subjects -------------------------------------------------------------
# The bracket does the work. A recruiter scanning an inbox decides in the
# subject line whether this is a student who applied or a student worth
# opening, and the previous employer is the only fact short enough to fit.
# Each subject is a ladder, longest first. A posting title can be 52
# characters on its own ("Software Development Engineer Intern - Annapurna
# Labs"), so for some roles the full line will not fit however it is worded --
# and blunt truncation cuts exactly the words that carry the meaning, leaving
# "Summer 2027 Software Development Engineer" with no "Intern" and no
# "application". Dropping a whole optional phrase loses less than cutting the
# middle out of the one that matters.
SUBJECTS = [
    ["[{bracket}] {season_short_role} - applied, would love to connect",
     "[{bracket}] {short_role} - applied, would love to connect",
     "[{bracket}] {short_role} - applied",
     "[{bracket}] {short_role}"],
    ["[{bracket}] Rutgers '28 - {season_short_role} application",
     "[{bracket}] Rutgers '28 - {short_role} application",
     "[{bracket}] Rutgers '28 - {short_role}",
     "[{bracket}] {short_role} - Rutgers '28"],
    ["[{bracket}] {company} {season_short_role} - an applicant saying hello",
     "[{bracket}] {company} {short_role} - hello from an applicant",
     "[{bracket}] {company} {short_role} - hello",
     "[{bracket}] {company} {short_role}"],
]

# When one note covers several applications at the same company. Naming the
# roles individually is what makes it read as a person who applied to three
# things rather than a script that fired three times.
MULTI_SUBJECTS = [
    ["[{bracket}] {company} {season} intern applications - {n} roles",
     "[{bracket}] {company} intern applications - {n} roles",
     "[{bracket}] {company} intern applications"],
    ["[{bracket}] Rutgers '28 - {n} {company} intern applications",
     "[{bracket}] Rutgers '28 - {company} intern applications",
     "[{bracket}] {company} intern applications"],
    ["[{bracket}] {n} applications at {company} - a quick hello",
     "[{bracket}] {company} applications - a quick hello",
     "[{bracket}] {company} intern applications"],
]

MULTI_OPENERS = [
    "I applied to {n} openings at {company}{for_season} -- {roles} -- and "
    "wanted to put a name to the applications.",
    "I have just applied to {n} roles at {company}{for_season}: {roles}. "
    "Rather than send you three notes, here is one.",
    "I submitted applications for {n} {company} openings{for_season} -- "
    "{roles} -- and thought it was worth reaching out once.",
]

# What a subject line has to fit in. Gmail shows roughly this much on a
# desktop list and far less on a phone; past it the line is cut mid-word,
# which reads as a mail merge that nobody checked.
SUBJECT_MAX = 72

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


_TEAM_SUFFIX = re.compile(r"\s*[-\u2013(].*$")


def short_role(role: str) -> str:
    """The role, minus the part a subject line has no room for.

    Postings carry the team on the end -- "Software Development Engineer
    Intern - Annapurna Labs" is 52 characters before anything else is said.
    The team is worth keeping in the body, where the recruiter is already
    reading, and worth dropping from the subject, where it pushes the rest of
    the line past the cut.
    """
    short = _TEAM_SUFFIX.sub("", role or "").strip()
    return short if 4 <= len(short) <= 46 else (role or "").strip()


_WORDS = {2: "two", 3: "three", 4: "four", 5: "five"}


def _count(n: int) -> str:
    """Spelled out up to five. "3 roles" in a sentence reads like a report."""
    return _WORDS.get(n, str(n))


def _join(items: list[str]) -> str:
    """a, b and c -- no Oxford comma, to match the rest of the copy."""
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _fit(ladder: list[str], fields: dict) -> str:
    """The longest rung that fits, or a clean word-boundary cut of the last."""
    rendered = [rung.format(**fields) for rung in ladder]
    for subject in rendered:
        if len(subject) <= SUBJECT_MAX:
            return subject
    shortest = rendered[-1]
    return shortest[:SUBJECT_MAX].rsplit(" ", 1)[0].rstrip(" -,")


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
    """(subject, body, variant name). `job` needs company, role, season.

    `roles` may carry more than one title, for the case where several
    applications at one company are covered by a single note.
    """
    roles = list(dict.fromkeys(r for r in (job.get("roles") or []) if r)) \
        or [job.get("role") or "Software Engineer Intern"]
    role = roles[0]
    short = short_role(role)
    season = usable_season(job.get("season"))
    fields = {
        "bracket": bracket(),
        "company": job.get("company") or "your team",
        "role": role,
        "season": season,
        "season_role": f"{season} {role}".strip(),
        "short_role": short,
        "season_short_role": f"{season} {short}".strip(),
        "for_season": f" for {season}" if season else "",
        # LinkedIn's firstName is whatever the person typed, and people put
        # more than one word in it: "Jeevan Lobo S." arrives as first name
        # "Jeevan Lobo". Greeting someone by two names is the tell that a
        # script wrote it.
        "first_name": (contact.get("first_name") or "there").split()[0],
        "n": _count(len(roles)),
        # Full titles here, not shortened ones. The team suffix is the only
        # thing telling "Software Engineer Intern" from "Software Engineer
        # Intern - Azure Networking", and dropping it turns a list of three
        # roles into the same role written twice.
        "roles": _join(roles),
    }
    cid = int(contact.get("id") or 0)
    multi = len(roles) > 1
    subject = _fit(_pick(MULTI_SUBJECTS if multi else SUBJECTS, cid), fields)
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
        f"{_pick(MULTI_OPENERS if multi else OPENERS, cid).format(**fields)}\n\n"
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
