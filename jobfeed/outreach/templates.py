"""The message.

Two families, because the same words do not work on both audiences.

`warm` is for someone who already knows you -- a recruiter you spoke to, or who
interviewed you. It can say "since we last spoke" because that happened.

`cold` is for a recruiter who has never heard of you, which is every contact
this pipeline finds. The warm template sent cold is worse than a bad email: a
stranger reading "turning down the previous offer was difficult" concludes you
have mistaken them for someone else, and the credibility you were borrowing
from the Google line evaporates in the same sentence.

Both are plain text, and plain text is what goes out -- there is no HTML part.
No tracking pixel, no unsubscribe footer, no shortened links, and no link at
all when the resume is attached instead. Each of those is a bulk-mail signal,
and a genuine one-to-one email carries none of them.

Variants exist so that fifty sends are not fifty identical strings. They rotate
per contact, deterministically from the contact id, so a re-render produces the
same message rather than a new one.
"""
from __future__ import annotations

import datetime as dt
import re

from .profile import ATTACH_RESUME, ME, WINS, bracket, signature

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
# Two families, and only two: "Applied to X" and "Interested in the X". The
# third used to end "- hello" or "- an applicant saying hello", which reads
# like a mailing list introducing itself rather than a person who applied.
SUBJECTS = [
    ["{prefix}Applied to the {season_short_role} role at {company}",
     "{prefix}Applied to the {short_role} role at {company}",
     "{prefix}Applied to the {season_short_role} role",
     "{prefix}Applied to the {short_role} role",
     "{prefix}Applied to {short_role}"],
    ["{prefix}Interested in the {season_short_role} role at {company}",
     "{prefix}Interested in the {short_role} role at {company}",
     "{prefix}Interested in the {season_short_role} role",
     "{prefix}Interested in the {short_role} role",
     "{prefix}Interested in {short_role}"],
]

# When one note covers several applications at the same company. Naming the
# roles individually is what makes it read as a person who applied to three
# things rather than a script that fired three times.
MULTI_SUBJECTS = [
    ["{prefix}Applied to {n} {season_and}intern roles at {company}",
     "{prefix}Applied to {n} intern roles at {company}",
     "{prefix}Applied to {n} roles at {company}"],
    ["{prefix}Interested in {n} {season_and}intern roles at {company}",
     "{prefix}Interested in {n} intern roles at {company}",
     "{prefix}Interested in {n} roles at {company}"],
]

# These open the note and are followed by the list of roles. Written to end on
# a colon so the list can be a sentence for two roles and a block for more:
# four long posting titles run into one sentence are unreadable, and reading
# like a mail merge is the one thing this copy cannot afford.
MULTI_OPENERS = [
    "I hope you're doing well. I recently submitted applications for {n} "
    "{company} openings{for_season} and thought it was worth reaching out "
    "once:",
    "I hope you're doing well. I recently applied to {n} roles at "
    "{company}{for_season}. Rather than send {n} separate notes, here is one:",
    "I hope you're doing well. I put in applications for {n} openings at "
    "{company}{for_season} recently and wanted to put a name to them:",
]

# What a subject line has to fit in. Gmail shows roughly this much on a
# desktop list and far less on a phone; past it the line is cut mid-word,
# which reads as a mail merge that nobody checked.
SUBJECT_MAX = 72

# ---- openers --------------------------------------------------------------
#
# His own wording, from the note he writes by hand: the pleasantry, then "the
# {company} {role} position" rather than "{company}'s". Three variants so
# fifty sends are not fifty identical strings, differing only in the verb --
# the shape is the same, because the shape is the part he liked.
#
# season_role, not role. His reference note names no season because Coinbase
# did not put one in the title; where an employer does, saying it once is the
# difference between an application a recruiter can find and one they cannot.
# The season logic upstream already guarantees it appears exactly once.
OPENERS = [
    "I hope you're doing well. I recently submitted an application for the "
    "{company} {season_role} position and thought it was worth reaching out.",
    "I hope you're doing well. I recently applied for the {company} "
    "{season_role} position and thought it was worth reaching out.",
    "I hope you're doing well. I put in an application for the {company} "
    "{season_role} position recently and thought it was worth reaching out.",
]

# Portals that hold their own account, under an address the note is not sent
# from. A recruiter who searches their system for bknideesh@gmail.com finds
# nothing, and concludes the application does not exist -- so the note has to
# say which address to look under. Keyed on the posting's own URL, because
# that is how the pipeline already knows where the job came from.
PORTALS = {
    "ripplematch.com": "RippleMatch",
    "simplify.jobs": "Simplify",
    "handshake.com": "Handshake",
    "joinhandshake.com": "Handshake",
}

APPLIED_AS = ("I applied through {portal}, where my account is under my "
              "university address ({school_email}); this one is my primary "
              "address.")


def portal_for(url: str) -> str:
    """The named portal this posting came from, or "" for a direct careers page."""
    host = (url or "").lower()
    for match, name in PORTALS.items():
        if match in host:
            return name
    return ""


# ---- follow-ups -----------------------------------------------------------
FOLLOWUPS = {
    1: ("Following up on my note about the {role} role at {company}. Happy to "
        "send anything useful from my side, and I understand if timelines are "
        "tight."),
    2: ("Last note from me on the {role} role at {company} - if the timing is "
        "wrong I completely understand. I would welcome the chance to be "
        "considered for anything else on the team."),
}


def _applied_as(job: dict) -> str:
    """The paragraph naming the portal account, or nothing at all.

    Only when the posting came from a portal that holds a different address,
    and only when that address is set -- an unconditional line explaining an
    email address is noise on the nine notes out of ten that do not need it.
    """
    portal = portal_for(job.get("url") or "")
    other = (ME.get("school_email") or "").strip()
    if not portal or not other or other.lower() == (ME.get("email") or "").lower():
        return ""
    return APPLIED_AS.format(portal=portal, school_email=other) + "\n\n"


def _where_the_resume_is() -> str:
    """Point at the attachment when there is one, at the site when there is not.

    A bare URL in a cold email is a link a stranger is being asked to click,
    and it is what most bulk mail leads with -- while the attachment is right
    there in the same message. So the link only appears when it is the only
    way to see anything.
    """
    # A one-element list, so the dashboard can flip it at runtime. Testing
    # the box rather than its contents is always true.
    if ATTACH_RESUME[0]:
        return "My resume is attached to this email for your reference."
    return f"Resume and projects are at {ME['portfolio']}."


def _pick(options, contact_id: int):
    """Deterministic per contact, so re-rendering does not rewrite history."""
    return options[contact_id % len(options)]


# A space is required before the dash. Without it, "ASIC Package Engineer
# Intern Co-op" was cut at the hyphen inside "Co-op" and the subject went out
# reading "ASIC Package Engineer Intern Co".
_TEAM_SUFFIX = re.compile(r"\s+[-\u2013]\s.*$|\s*\(.*$")

# A season the employer put in the title: "... - Summer 2027". Stripped when
# the note names the season itself, or the sentence says Summer 2027 three
# times and still contradicts itself when one of the roles is a Spring one.
_TRAILING_SEASON = re.compile(
    r"[\s,\u2013-]+(spring|summer|fall|autumn|winter)\s*20\d\d\s*$", re.I)


def clean_title(role: str) -> str:
    """A posting title as it should read inside a sentence."""
    return _TRAILING_SEASON.sub("", (role or "").strip()).strip(" ,-\u2013")


def season_in(role: str) -> str:
    """The season the title carries, if it carries one."""
    m = _TRAILING_SEASON.search(role or "")
    return f"{m.group(1).title()} {m.group(0).strip()[-4:]}" if m else ""


def short_role(role: str) -> str:
    """The role, minus the part a subject line has no room for.

    Postings carry the team on the end -- "Software Development Engineer
    Intern - Annapurna Labs" is 52 characters before anything else is said.
    The team is worth keeping in the body, where the recruiter is already
    reading, and worth dropping from the subject, where it pushes the rest of
    the line past the cut.
    """
    short = _TEAM_SUFFIX.sub("", clean_title(role)).strip()
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


def _role_list(titles: list[str]) -> str:
    """Inline for one or two, a block for more."""
    if len(titles) == 1:
        return ""
    if len(titles) == 2:
        return f" {_join(titles)}."
    return "\n\n" + "\n".join(f"  - {t}" for t in titles)


def _fit(ladder: list[str], fields: dict) -> str:
    """The longest rung that fits, or a clean word-boundary cut of the last."""
    # Collapsed, not just avoided in the templates above: a rung is one edit
    # away from reintroducing a gap around an empty field, and "Tesla  intern
    # applications" in a subject line reads as a bug in the sender.
    rendered = [re.sub(r"\s{2,}", " ", rung.format(**fields)).strip()
                for rung in ladder]
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
    titles = [clean_title(r) for r in roles]

    # Employers put the season in the title, and the seasons need not agree:
    # Tesla listed a Summer 2027 and a Spring 2027 posting side by side. When
    # they disagree the note names none of them rather than asserting one and
    # then contradicting it two lines later in the list.
    carried = {season_in(r) for r in roles if season_in(r)}
    season = usable_season(job.get("season"))
    if len(carried) == 1 and len(roles) == 1:
        season = usable_season(carried.pop()) or season
    elif len(carried) > 1:
        season = ""
    fields = {
        "prefix": f"[{bracket()}] " if bracket() else "",
        "company": job.get("company") or "your team",
        # The cleaned title, or a single-role note reads "Summer 2027 Software
        # Engineer Intern - Vehicle Software - Summer 2027".
        "role": titles[0],
        "season": season,
        "season_role": f"{season} {titles[0]}".strip(),
        "short_role": short,
        "season_short_role": f"{season} {short}".strip(),
        "for_season": f" for {season}" if season else "",
        # LinkedIn's firstName is whatever the person typed, and people put
        # more than one word in it: "Jeevan Lobo S." arrives as first name
        # "Jeevan Lobo". Greeting someone by two names is the tell that a
        # script wrote it.
        "first_name": (contact.get("first_name") or "there").split()[0],
        "n": _count(len(roles)),
        "N": _count(len(roles)).capitalize(),
        # Trailing space folded in, so an absent season leaves one gap and not
        # two. Tesla's postings carry conflicting seasons, so this is empty
        # exactly where a subject is most likely to be read.
        "season_and": f"{season} " if season else "",
        # Full titles here, not shortened ones. The team suffix is the only
        # thing telling "Software Engineer Intern" from "Software Engineer
        # Intern - Azure Networking", and dropping it turns a list of three
        # roles into the same role written twice.
        "roles": _join(titles),
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

    # Four, as he writes it. The lead-in sits directly on top of the list
    # rather than trailing the education sentence, and there is no blank line
    # between them: a heading belongs to what it introduces.
    wins = "\n".join(f"  - {label}: {text}" for label, text in WINS[:4])
    body = (
        f"Hi {fields['first_name']},\n\n"
        f"{_pick(MULTI_OPENERS if multi else OPENERS, cid).format(**fields)}"
        f"{_role_list(titles)}\n\n"
        f"{_applied_as(job)}"
        f"I am a {ME['degree']} student at {ME['school']} "
        f"({ME['honors']}, {ME['gpa']} GPA), graduating {ME['grad']}.\n\n"
        f"A few things I have worked on:\n"
        f"{wins}\n\n"
        f"I would love to learn more about the next steps in the application "
        f"process. {_where_the_resume_is()}\n\n"
        f"Thanks,\n{ME['first_name']}\n"
        + (f"\n{signature()}\n" if signature() else "")
    )
    return subject, body, variant
