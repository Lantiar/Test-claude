"""Apify: recruiters, and whether their addresses are real.

Two actors, both chosen for one property: neither needs a LinkedIn session
cookie. Cookie-based scrapers drive your own logged-in account, and the account
that gets restricted is the one you need to job-hunt with -- the risk lands
exactly where it must not.

Actor ids are configurable because the Apify store churns: an actor that is the
best option today may be unlisted in three months, and a hardcoded id would
turn that into a silent zero-results run rather than a setting to change.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .. import normalize as _norm

ENDPOINT = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"

# LinkedIn people search, no cookie required.
PEOPLE_ACTOR = os.getenv("APIFY_PEOPLE_ACTOR", "harvestapi~linkedin-profile-search")
# SMTP mailbox verification. Not overpowered~verify-email, which is the better
# known one: it demands full-account permissions before it will run at all, so
# it cannot be used without a manual approval click in the Apify console.
VERIFY_ACTOR = os.getenv("APIFY_VERIFY_ACTOR", "michael.g~email-verifier-validator")

# What a recruiter's title looks like. Deliberately narrow: "recruiter" alone
# pulls in agency recruiters and recruiting coordinators at other companies,
# and a technical sourcer is a better target than a VP of Talent.
RECRUITER_TITLES = ("university recruiter", "campus recruiter", "early career",
                    "early careers", "technical recruiter", "technical sourcer",
                    "recruiter", "talent acquisition", "recruiting")

# What LinkedIn is asked for, which is a shorter list: the filter is exact
# against LinkedIn's own job titles, while RECRUITER_TITLES is matched loosely
# against the free-text headline afterwards.
SEARCH_TITLES = ("University Recruiter", "Technical Recruiter",
                 "Early Career Recruiter", "Recruiter", "Talent Acquisition")


def _call(actor: str, payload: dict, timeout: int = 300) -> list[dict]:
    token = os.getenv("APIFY_TOKEN", "")
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set")
    url = f"{ENDPOINT.format(actor=actor)}?token={token}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace")
        # Named, because "actor not found" and "out of credit" are different
        # problems with the same symptom of an empty list.
        raise RuntimeError(f"apify {actor} returned {exc.code}: {body}") from exc


def _employer(item: dict) -> str:
    for pos in (item.get("currentPosition") or []):
        if pos.get("companyName"):
            return pos["companyName"]
    return ""


def _at_company(item: dict, company: str) -> bool:
    """Does this person actually work there?

    searchQuery is free text and constrains nothing: a search for "Amazon
    university recruiter" returned recruiters at Intuit, Roku and Modo Energy
    in the first ten results. The actor's own company filter takes LinkedIn
    company URLs, not names, so it cannot be used from a job listing -- which
    leaves this. Without it the pipeline writes to a Roku recruiter about an
    Amazon application, and there is nothing in the draft that would look
    wrong on review.
    """
    want, got = _norm.company(company), _norm.company(_employer(item))
    if not want or not got:
        return False
    # Subsidiaries read differently on LinkedIn than in a job posting:
    # "Amazon Web Services (AWS)" against "Amazon". Containment either way,
    # but only for names long enough that it means something -- "IBM" inside
    # "IBMC" would otherwise pass.
    return want == got or (len(want) >= 4 and want in got) \
        or (len(got) >= 4 and got in want)


def _best_email(item: dict) -> tuple[str | None, str]:
    """Pick an address and carry the actor's own verdict on it.

    This actor probes as it goes and returns status/deliverable/catchAllDomain
    per address, so a separate verification pass is wasted money for anyone it
    answered for. It is a list, not a string: reading item["email"] -- which is
    what the first version did -- finds nothing at all and every contact is
    skipped as "no email found".
    """
    found = item.get("emails") or []
    if isinstance(found, str):
        found = [{"email": found}]
    scored = []
    for e in found:
        if isinstance(e, str):
            e = {"email": e}
        addr = (e.get("email") or "").strip().lower()
        if not addr:
            continue
        status = _status({**e, "catch_all": e.get("catchAllDomain"),
                          "status": e.get("status")})
        if status == "unknown" and e.get("deliverable") is True:
            status = "verified"
        scored.append((_RANK.get(status, 9), addr, status))
    if not scored:
        return None, "unknown"
    _, addr, status = min(scored)
    return addr, status


_RANK = {"verified": 0, "accept_all": 1, "unknown": 2, "risky": 3, "invalid": 4}


def find_recruiters(company: str, limit: int = 3) -> list[dict]:
    """Recruiters at one company, best guess first.

    "Full + email search" and not the cheaper modes: the others return no
    address at all, so the whole run would cost money and produce nothing that
    can be written to.
    """
    items = _call(PEOPLE_ACTOR, {
        "profileScraperMode": "Full + email search",
        "searchQuery": f"{company} university recruiter early career",
        "currentJobTitles": list(SEARCH_TITLES),
        "maxItems": max(limit * 5, 15),
    })
    out = []
    for it in items:
        if not _at_company(it, company):
            continue
        title = (it.get("headline") or _employer(it) and
                 (it.get("currentPosition") or [{}])[0].get("position") or "")
        if not any(t in title.lower() for t in RECRUITER_TITLES):
            continue
        name = (it.get("fullName") or
                " ".join(x for x in (it.get("firstName"), it.get("lastName")) if x))
        if not name:
            continue
        email, status = _best_email(it)
        out.append({
            "full_name": name.strip(),
            "first_name": (it.get("firstName") or name.split()[0]).strip(),
            "title": title.strip()[:160],
            "linkedin_url": it.get("linkedinUrl") or it.get("url") or "",
            "email": email,
            "email_status": status,
            "employer": _employer(it),
        })
    # Deliverable addresses first: three contacts is the whole budget for a
    # company, and spending one on a catch-all guess is a wasted slot.
    out.sort(key=lambda c: _RANK.get(c["email_status"], 9))
    return out[:limit]


def verify(emails: list[str]) -> dict[str, str]:
    """address -> verified | accept_all | risky | invalid | unknown.

    accept_all is its own answer, not a shade of verified. Google Workspace and
    Microsoft 365 accept mail for any local part and sort it out afterwards, so
    for most large employers the probe cannot distinguish a real mailbox from a
    typo. Folding that into "verified" is how a pipeline convinces itself an
    invented address is safe to write to.
    """
    if not emails:
        return {}
    items = _call(VERIFY_ACTOR, {"emails": emails})
    out: dict[str, str] = {}
    for it in items:
        addr = (it.get("email") or it.get("address") or "").strip().lower()
        if not addr:
            continue
        out[addr] = _status(it)
    for e in emails:                     # anything the actor did not answer
        out.setdefault(e.strip().lower(), "unknown")
    return out


# The probe never actually reached the mail server. Whatever verdict the actor
# printed alongside this, it is not one: a live probe over 33 addresses came
# back with the SAME domain scored "bad" for one address and "risky" for
# another seconds apart, both on smtp_unreachable -- the difference was the
# actor's luck getting a connection, not anything about the mailbox. Reading
# either as a verdict throws away good addresses (apple.com) or invents
# confidence in bad ones.
_NO_PROBE = ("smtp_unreachable", "timeout", "connection_refused", "greylisted",
             "blocked", "rate_limited")


def _status(item: dict) -> str:
    """Normalise across actors, which each name these differently."""
    if item.get("isDisposable") or item.get("disposable"):
        return "risky"
    if item.get("isCatchAll") or item.get("catchAll") or item.get("acceptAll") \
            or item.get("catch_all"):
        return "accept_all"
    if str(item.get("reason") or "").lower() in _NO_PROBE:
        return "unknown"

    raw = str(item.get("status") or item.get("result") or
              item.get("deliverability") or "").lower()
    # "good" is this actor's word for deliverable. Its absence here scored
    # every genuinely-verified address as "unknown", which _may_write lets
    # through -- so the bug was invisible in the send path and only showed up
    # against a control address known to be real.
    if raw in ("valid", "deliverable", "ok", "safe", "good"):
        return "verified"
    if raw in ("invalid", "undeliverable", "bad"):
        return "invalid"
    if raw in ("catch_all", "catch-all", "accept_all", "unknown_catchall"):
        return "accept_all"
    if raw in ("risky", "do_not_mail"):
        return "risky"
    if raw == "unknown":
        return "unknown"
    if item.get("isValid") is True:
        return "verified"
    if item.get("isValid") is False:
        return "invalid"
    return "unknown"
