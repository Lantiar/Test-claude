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
# Fills a gap when the search finds the right person without an address.
FINDER_ACTOR = os.getenv("APIFY_FINDER_ACTOR",
                         "snipercoder~email-finder-by-name-and-domain")

# Where you are. A recruiter in Amsterdam or Bengaluru does not hire for a US
# internship, and writing to them is a note nobody can act on -- Philips
# returned a Dutch, a German and three Indian recruiters before this.
COUNTRY = os.getenv("OUTREACH_COUNTRY", "US").upper()
COUNTRY_NAMES = {"US": "United States", "GB": "United Kingdom", "CA": "Canada",
                 "IN": "India", "DE": "Germany", "NL": "Netherlands",
                 "AU": "Australia", "SG": "Singapore", "IE": "Ireland"}

# How far to keep widening when a company is not yielding anybody usable. Each
# rung costs about a cent per profile, so this stops rather than spending
# without limit -- but it goes far enough that a big employer whose campus
# recruiter sits outside the first page is still found.
LADDER = tuple(int(n) for n in
               os.getenv("OUTREACH_SEARCH_LADDER", "15,45,100,200").split(","))


def in_country(item: dict, code: str = "") -> bool:
    """Is this person somewhere they could hire for a role where you are?

    Unknown counts as yes. LinkedIn leaves the country off some profiles, and
    dropping everybody it could not place would throw away good contacts to
    avoid a bad one -- the cost of each is not the same.
    """
    code = (code or COUNTRY).upper()
    location = item.get("location") or {}
    found = (location.get("countryCode")
             or (location.get("parsed") or {}).get("countryCode") or "")
    return not found or found.upper() == code

# What a recruiter's title looks like. Deliberately narrow: "recruiter" alone
# pulls in agency recruiters and recruiting coordinators at other companies,
# and a technical sourcer is a better target than a VP of Talent.
RECRUITER_TITLES = ("university recruiter", "campus recruiter", "early career",
                    "early careers", "technical recruiter", "technical sourcer",
                    "recruiter", "talent acquisition", "recruiting")

# What LinkedIn is asked for, which is a shorter list: the filter is exact
# against LinkedIn's own job titles, while RECRUITER_TITLES is matched loosely
# against the free-text headline afterwards.
SEARCH_TITLES = ("University Recruiter", "Campus Recruiter",
                 "Early Career Recruiter", "Early Careers Recruiter",
                 "Graduate Recruiter", "Emerging Talent",
                 "Technical Recruiter", "Recruiter", "Talent Acquisition")

# Who should get the note, in order. A campus recruiter owns the intern
# pipeline; a talent-acquisition partner may never touch it, and a VP of Talent
# does not read cold mail from students. Ranked rather than filtered, because
# at a company with one findable recruiter the generic one is still the right
# person, and better than nobody.
_TITLE_TIERS = (
    ("university", "campus", "early career", "early careers", "earlycareer",
     "graduate", "emerging talent", "student", "new grad", "intern", "entry level"),
    ("technical recruiter", "technical sourcer", "engineering recruiter",
     "tech recruiter", "technology recruiter"),
    ("recruiter", "recruiting", "recruitment", "talent acquisition",
     "talent partner", "sourcer", "talent", "hiring"),
)
# Seniority that makes someone a worse target, not a better one.
_SENIOR = ("vice president", "vp ", "head of", "director", "chief", "executive")


def title_rank(title: str) -> int:
    """Lower is a better person to write to."""
    text = (title or "").lower()
    tier = next((i for i, words in enumerate(_TITLE_TIERS)
                 if any(w in text for w in words)), len(_TITLE_TIERS))
    return tier * 2 + (1 if any(w in text for w in _SENIOR) else 0)


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
    # Subsidiaries read differently on LinkedIn than in a job posting --
    # "Amazon Web Services (AWS)" against "Amazon" -- so one may extend the
    # other. But only as a PREFIX, never as a substring anywhere: containment
    # accepted "Morgan Philips Group", a recruitment agency, as Philips, and
    # the note would have gone to an agency recruiter claiming to have applied
    # at the client. A subsidiary carries the parent's name at the front;
    # an unrelated firm that merely contains it does not.
    return want == got or (len(want) >= 4 and got.startswith(want)) \
        or (len(got) >= 4 and want.startswith(got))


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
        if not addr or is_personal(addr):
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

# Somebody's personal mailbox. A recruiter's work address is a professional
# contact and writing to it is ordinary; their private Outlook is not, and a
# cold email there is visibly scraped whatever it says. A recruiter reachable
# only this way is, for this purpose, not reachable.
FREE_MAIL = frozenset("""
gmail.com googlemail.com outlook.com hotmail.com live.com msn.com
yahoo.com yahoo.co.uk ymail.com aol.com icloud.com me.com mac.com
proton.me protonmail.com gmx.com gmx.de mail.com zoho.com yandex.com
qq.com 163.com 126.com naver.com rediffmail.com
""".split())


def is_personal(address: str) -> bool:
    return (address or "").rsplit("@", 1)[-1].lower() in FREE_MAIL


def find_recruiters(company: str, limit: int = 3) -> list[dict]:
    """Recruiters at one company, best guess first.

    "Full + email search" and not the cheaper modes: the others return no
    address at all, so the whole run would cost money and produce nothing that
    can be written to.
    """
    def search(payload: dict) -> list:
        return _call(PEOPLE_ACTOR, {
            "currentJobTitles": list(SEARCH_TITLES),
            "locations": [COUNTRY_NAMES.get(COUNTRY, COUNTRY)],
            **payload})

    def usable(pool: list) -> list:
        """Right company, right country, right sort of job, has an address."""
        return [it for it in pool
                if _at_company(it, company) and in_country(it)
                and _best_email(it)[0]]

    # Two passes, because LinkedIn's free-text search is not a company filter.
    # Searching "Philips recruiter" returns recruiters at Anduril and Synopsys,
    # people whose surname is Philips, and agencies with Philips in their name;
    # of the first fifteen results exactly one worked at Philips, and she had
    # no address. The actor does have an exact company filter, but it wants a
    # LinkedIn company URL rather than a name -- and the profiles themselves
    # carry that URL. So: one cheap pass to learn the URL, then an exact one.
    votes: dict[str, int] = {}
    for item in search({"profileScraperMode": "Full",
                        "searchQuery": f"{company} recruiter", "maxItems": 10}):
        url = ((item.get("currentPosition") or [{}])[0]).get("companyLinkedinUrl")
        # Only a real company page, and only from somebody who actually works
        # there -- a search-results URL is not an employer.
        if url and "/company/" in url and _at_company(item, company):
            votes[url] = votes.get(url, 0) + 1

    page = ({"profileScraperMode": "Full + email search",
             "currentCompanies": [max(votes, key=votes.get)]} if votes else
            {"profileScraperMode": "Full + email search",
             "searchQuery": f"{company} university recruiter early career"})

    # Widen until there are enough people worth writing to, or the ladder runs
    # out. A pool of fifteen at a large employer is mostly whoever LinkedIn
    # ranked highest: the campus recruiter is often not in it, and after the
    # country filter neither is anyone else. Each rung is a bigger bill, so it
    # stops -- but it goes far enough to find them.
    items: list = []
    for want in LADDER:
        items = search({**page, "maxItems": want})
        good = usable(items)
        if len(good) >= limit and any(title_rank(it.get("headline") or "") <= 1
                                      for it in good):
            break

    out = []
    for it in items:
        if not _at_company(it, company) or not in_country(it):
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
    # Reachable first, then whose job it is, then how good the address is.
    # Role fit alone put three American Express campus recruiters at the top
    # with not one address between them -- a perfect target nobody can write
    # to is worth less than a generic one who answers. Among the people we can
    # actually reach, the campus recruiter still wins.
    # A campus recruiter we found but cannot write to is the one gap worth
    # paying to close -- but only when a colleague's real address has already
    # told us the domain. Without that we would be guessing the domain as well
    # as the local part, and a guess that lands wrong is a bounce against your
    # own sending reputation.
    domain = corporate_domain(out)
    missing = [c for c in out
               if not c["email"] and title_rank(c["title"]) <= 1][:limit]
    if domain and missing:
        try:
            filled = find_emails([(c["full_name"], domain) for c in missing])
            for c in missing:
                addr = filled.get(c["full_name"])
                if addr:
                    c["email"] = addr
                    # Never "verified" on the finder's say-so: at a catch-all
                    # domain its validation check passes for anything.
                    c["email_status"] = verify([addr]).get(addr, "unknown")
        except Exception as exc:
            print(f"  apify: email finder: {exc}")

    out.sort(key=lambda c: (0 if c["email"] else 1,
                            title_rank(c["title"]),
                            _RANK.get(c["email_status"], 9)))
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


# ---- filling a gap --------------------------------------------------------

def find_emails(people: list[tuple[str, str]]) -> dict[str, str]:
    """[(full name, domain)] -> {full name: address}.

    Only ever called with a domain taken from a colleague's real address at the
    same company. The domain is never guessed: "aexp.com" or
    "americanexpress.com" is a coin flip, and a coin flip that lands wrong is a
    bounce against your own sending reputation.

    Checked against two people whose addresses were already known, and it
    returned both exactly. It also returned "rg@aexp.com" for a Reason Gomez,
    marked valid -- two initials at a domain that accepts everything, which is
    what "valid" means at a catch-all. So nothing here is trusted as verified;
    `verify` decides that separately.
    """
    if not people:
        return {}
    items = _call(FINDER_ACTOR, {"names": [n for n, _ in people],
                                 "domains": [d for _, d in people]})
    out = {}
    for it in items:
        name = (it.get("Name") or it.get("name") or "").strip()
        addr = (it.get("Email") or it.get("email") or "").strip().lower()
        if name and addr and not is_personal(addr):
            out[name] = addr
    return out


def corporate_domain(contacts: list[dict]) -> str:
    """The company's mail domain, learned from an address we actually found."""
    seen: dict[str, int] = {}
    for c in contacts:
        if c.get("email") and not is_personal(c["email"]):
            d = c["email"].rsplit("@", 1)[-1]
            seen[d] = seen.get(d, 0) + 1
    return max(seen, key=seen.get) if seen else ""
