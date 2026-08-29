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
    return (f"{ME['name']}\n{ME['school']} · {ME['degree']} · {ME['grad']}\n"
            f"{ME['portfolio']} · {ME['linkedin']}")
