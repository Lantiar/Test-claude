"""Turning what a source wrote into something two sources can be compared on.

This is the weakest tier of matching and the only one available when a listing
carries no usable link, so it has to be careful in a specific direction: it may
fail to notice that two records are the same job, and it may not decide that
two different jobs are one. The asymmetry is the whole design. A missed match
shows a duplicate in the list, which is visible and harmless. A false match
deletes a job nobody will ever know was there.
"""
from __future__ import annotations

import re

# Suffixes that are part of a legal name and never part of an identity.
_LEGAL = re.compile(
    r"[\s,]*\b(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|"
    r"corporation|co|co\.|company|plc|gmbh|s\.a\.|sa|ag|nv|bv|pty|llp|lp|"
    r"holdings|group|technologies|technology|labs|the)\b\.?", re.I)

_PUNCT = re.compile(r"[^a-z0-9]+")


def company(name: str) -> str:
    """A company name reduced to what two spellings of it have in common."""
    s = (name or "").lower()
    s = s.replace("&", " and ")
    prev = None
    while prev != s:                       # "Acme Technologies Group Inc."
        prev = s
        s = _LEGAL.sub(" ", s).strip()
    return _PUNCT.sub("", s)


# Season and year, which belong in their own field rather than in the title.
_SEASON = re.compile(r"\b(summer|fall|autumn|winter|spring)\b[\s'/-]*\b(20\d\d)?\b", re.I)
_YEAR = re.compile(r"\b20\d\d\b")
# A requisition number the employer put in the title.
_REQ = re.compile(r"\b(?:req|requisition|job|posting)?[\s#-]*(?:R|JR)?-?\d{4,}\b", re.I)
_NOISE = re.compile(
    r"\b(intern|internship|interns|co-?op|student|programme|program|"
    r"opportunity|opportunities|position|role|hiring|now|apply)\b", re.I)


def title(text: str) -> str:
    """A job title with the parts that vary between postings of it removed.

    Deliberately keeps the discriminating words. "Software Engineer Intern,
    Backend" and "Software Engineer Intern, Machine Learning" must not reduce
    to the same string: they are two jobs at one company and collapsing them
    loses one. So this strips season, year, requisition number and the words
    every listing carries, and leaves everything else alone.
    """
    s = (text or "").lower()
    s = _SEASON.sub(" ", s)
    s = _REQ.sub(" ", s)
    s = _YEAR.sub(" ", s)
    s = _NOISE.sub(" ", s)
    return " ".join(_PUNCT.sub(" ", s).split())


_STATE = re.compile(r"\b([a-z]{2})\b$", re.I)


def location(text: str) -> str:
    """The city, near enough to compare. 'NYC' and 'New York, NY' do not
    reconcile here and are not asked to: location breaks ties, it does not make
    matches on its own."""
    s = (text or "").lower().split("(")[0]
    s = re.sub(r"\b(remote in|remote|hybrid|onsite|on-site)\b", " ", s)
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return _PUNCT.sub("", parts[0]) if parts else ""


def season(terms) -> str:
    """Simplify gives ['Summer 2027']; a story gives nothing."""
    if not terms:
        return ""
    if isinstance(terms, str):
        terms = [terms]
    for t in terms:
        if m := _SEASON.search(str(t)):
            season_, year = m.group(1).lower(), m.group(2) or ""
            return f"{season_} {year}".strip()
    return str(terms[0]).strip().lower() if terms else ""
