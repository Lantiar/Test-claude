"""Is this link a job posting, or one of the other things people post?

Stories carry both, and the ask was to keep both, so this decides which table a
link lands in rather than whether it is worth keeping. It is deliberately
cheap: an ATS identity settles it with no guessing and no model call, and that
covers most of what a jobs account posts. Everything else gets a shallow read
of the URL, and anything still unclear is stored as unknown rather than
guessed at -- an unknown link is in the database and can be looked at, while a
link wrongly filed as a job puts a row in the job list that no amount of
deduplication will ever tidy up.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .identity import identify, unwrap

# Hosts that only ever serve job postings.
_JOB_HOSTS = re.compile(
    r"(^|\.)(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|"
    r"myworkdaysite\.com|icims\.com|smartrecruiters\.com|workable\.com|"
    r"rippling\.com|jobvite\.com|breezy\.hr|applytojob\.com|teamtailor\.com|"
    r"eightfold\.ai|oraclecloud\.com|taleo\.net|simplify\.jobs|"
    r"jobs\.apple\.com|careers\.[\w-]+\.com|"
    r"jobs\.bytedance\.com|lifeattiktok\.com|amazon\.jobs|metacareers\.com)$", re.I)

_JOB_PATH = re.compile(r"/(jobs?|careers?|opening|position|vacanc|apply|"
                       r"requisition|job-detail)s?(/|$)", re.I)

# Things a jobs account posts that are not jobs.
_NOT_JOB = re.compile(
    r"(^|\.)(github\.com|youtube\.com|youtu\.be|medium\.com|substack\.com|"
    r"docs\.google\.com|drive\.google\.com|notion\.so|notion\.site|x\.com|"
    r"twitter\.com|discord\.gg|t\.me|reddit\.com|leetcode\.com|"
    r"instagram\.com|luma\.com|lu\.ma|eventbrite\.com)$", re.I)

_SHORTENER = re.compile(
    r"(^|\.)(bit\.ly|lnkd\.in|tinyurl\.com|t\.co|rb\.gy|shorturl\.at|"
    r"buff\.ly|cutt\.ly|linktr\.ee)$", re.I)


def is_shortened(url: str) -> bool:
    """A link that has to be followed before anything can be said about it."""
    try:
        return bool(_SHORTENER.search(urlparse(url).netloc.lower()))
    except ValueError:
        return False


def kind(url: str) -> str:
    """'job' | 'article' | 'tool' | 'unknown'."""
    if not url:
        return "unknown"
    if identify(url):
        return "job"                    # an ATS identity is not ambiguous
    # Unwrapped first, for the same reason the index-page guard reads the
    # canonical form: judged on the link as given, every story link is
    # l.instagram.com, which matches the not-a-job list and filed Amazon,
    # Apple and QTS postings as articles.
    url = unwrap(url)
    try:
        p = urlparse(url)
    except ValueError:
        return "unknown"
    host = (p.netloc or "").lower()
    if _JOB_HOSTS.search(host):
        return "job"
    if _NOT_JOB.search(host):
        return "article"
    if _SHORTENER.search(host):
        return "unknown"                # nothing is known until it is followed
    if _JOB_PATH.search(p.path or ""):
        return "job"
    return "unknown"
