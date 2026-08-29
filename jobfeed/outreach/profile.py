"""Who is writing, and what they have to say for themselves.

Kept apart from the template so the same facts can be reused by a different
message, and so that nothing here is invented at render time. Every claim in
this file is on the resume: an email to a recruiter that overstates anything
is worse than no email, because it is checkable and they will check.
"""
from __future__ import annotations

import os

ME = {
    "name": "Nideesh Bharath Kumar",
    "first_name": "Nideesh",
    "email": os.getenv("OUTREACH_FROM", "bknideesh@gmail.com"),
    "phone": "224-333-1045",
    "school": "Rutgers University - New Brunswick",
    "degree": "B.S. Computer Science and Data Science",
    "honors": "Honors College",
    "gpa": "4.0",
    "grad": "May 2028",
    "portfolio": "https://nideesh.ai",
    "linkedin": "https://linkedin.com/in/bknideesh",
    "github": "https://github.com/nb923",
}

# The bracket that opens every subject line -- the only reason a recruiter
# opens the mail at all, and the shortest way to say why this one is worth
# opening. One setting rather than composed parts: the wording is a judgement
# call about how you want to be read, not something to derive.
BRACKET = os.getenv("OUTREACH_BRACKET", "Prev Google/Zon")


def bracket() -> str:
    return BRACKET.strip()


# Three achievements, ordered strongest first. Each is one line, each carries a
# number, and each is verbatim from the resume rather than a paraphrase that
# drifts. A template picks the first two or three depending on length.
WINS = [
    ("Google SWE Intern",
     "built the context engine for Gemini Enterprise's orchestration layer, "
     "cutting plan hallucination from 33% to 2% via a self-healing A2A judge loop"),
    ("J&J SWE Intern",
     "shipped Databricks/Postgres pipelines over petabyte-scale data, "
     "saving ~$50K and 460 hours a year"),
    ("Rutgers IFH",
     "built ML infrastructure processing 500M+ healthcare records at "
     "300K rows/min"),
    ("HackRU",
     "won 1st in track and 2nd overall across two hackathons, most recently an "
     "AI dementia-care platform on FastAPI, OpenCV and Snowflake"),
]


def signature() -> str:
    """The block under the sign-off, or nothing.

    Empty by default. Everything it carried is already in the mail -- the
    school and degree in the opening paragraph, the portfolio in the closing
    line -- so it was the same three facts a second time under a rule, which
    is what made it read as a template rather than a note. Set
    OUTREACH_SIGNATURE to bring one back.
    """
    return os.getenv("OUTREACH_SIGNATURE", "").strip()


# ---- what the dashboard may change ----------------------------------------
#
# The facts in a cold email go stale -- a graduation date moves, a portfolio
# moves, and the three things worth saying about yourself change every few
# months. Editing profile.py for that means a commit and a deploy to change a
# sentence, so these live in the store and the runner applies them before it
# renders anything.
#
# Only these. Name, school and degree are not here: they are the identity the
# whole mail rests on, and a typo in one is not a setting.

EDITABLE = ("grad", "portfolio", "linkedin", "gpa", "honors")


def current() -> dict:
    """The settings as they stand, for the dashboard to show."""
    return {**{k: ME[k] for k in EDITABLE},
            "wins": [list(w) for w in WINS],
            "attach_resume": ATTACH_RESUME[0],
            "resume_name": RESUME_NAME[0]}


# Mutable so `apply` can change them without every caller re-importing.
ATTACH_RESUME = [True]
RESUME_NAME = ["resume.pdf"]


def apply(settings: dict | None) -> None:
    """Take the dashboard's settings, ignoring anything it may not set.

    Silently ignored rather than rejected: a stored profile from an older
    version of the page will carry keys this one does not know, and refusing
    the whole thing over one would drop every setting the user did make.
    """
    if not settings:
        return
    for key in EDITABLE:
        value = settings.get(key)
        if isinstance(value, str) and value.strip():
            ME[key] = value.strip()

    wins = settings.get("wins")
    if isinstance(wins, list):
        cleaned = [(str(w[0]).strip(), str(w[1]).strip())
                   for w in wins
                   if isinstance(w, (list, tuple)) and len(w) >= 2
                   and str(w[0]).strip() and str(w[1]).strip()]
        if cleaned:
            WINS[:] = cleaned

    if "attach_resume" in settings:
        ATTACH_RESUME[0] = bool(settings["attach_resume"])
    name = settings.get("resume_name")
    if isinstance(name, str) and name.strip():
        # A filename, never a path: this is used to name an attachment, and a
        # value from a web form must not be able to point at a file.
        RESUME_NAME[0] = os.path.basename(name.strip()) or "resume.pdf"
