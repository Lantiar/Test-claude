"""Deciding whether a listing we have just seen is one we already have.

The ladder is in identity.py; this walks it and writes the answer down. Two
rules do most of the work, and both are about refusing to merge:

  * Two records that each carry an ATS identity and carry different ones are
    different jobs. No amount of textual similarity overrides that -- "Software
    Engineer Intern, Backend" and "Software Engineer Intern, Machine Learning"
    at one company read almost identically and are two jobs.

  * A URL is only an identity if it names a posting. The careers-index case is
    handled in identity.looks_like_one_posting and matters here because the
    text tier is where those listings land, and it is the tier that keeps
    fifteen Zipline internships apart.

Merging is biased to say no. A missed match shows a duplicate, which anyone can
see and dismiss. A false match deletes a job, and what is left looks exactly
like a correct row.
"""
from __future__ import annotations

import json
import time
from difflib import SequenceMatcher

from . import normalize
from .identity import canonical_url, identify, looks_like_one_posting
from .models import RawListing

# How alike two normalised titles must read, for the same company, before they
# are called the same posting. High on purpose: this tier only ever runs when
# neither record has a usable link, so there is nothing behind it to catch a
# mistake.
TITLE_MATCH = 0.92


def keys(listing: RawListing) -> tuple[str | None, str | None]:
    """(tier 1 identity, tier 2 identity) for a listing, either may be None."""
    ident = identify(listing.url) if listing.url else None
    url_key = None
    if listing.url and looks_like_one_posting(listing.url):
        url_key = canonical_url(listing.url)
    return (ident.key if ident else None), url_key


def company_id(con, name: str, slug: str = "") -> int | None:
    if not (norm := normalize.company(name)):
        return None
    row = con.execute("SELECT id FROM company WHERE norm=?", (norm,)).fetchone()
    if row:
        if slug:
            con.execute("UPDATE company SET slug=COALESCE(slug,?) WHERE id=?",
                        (slug, row["id"]))
        return row["id"]
    cur = con.execute(
        "INSERT INTO company(name, norm, slug, created_at) VALUES(?,?,?,?)",
        (name.strip(), norm, slug or None, time.time()))
    return cur.lastrowid


def _conflicts(job_row, ats_key: str | None, url_key: str | None = None) -> bool:
    """Would matching this job contradict an identity both records carry?

    Two identities of the same kind that disagree mean two jobs. That was
    enforced for the ATS tier and not for the URL tier, and the URL tier is
    where it was actually needed: the employers whose links this system cannot
    parse into an ATS identity are exactly the ones with their own careers
    domain, and their URLs still carry a perfectly good posting id.

    The first full ingest merged 55 pairs on text alone, every one of them two
    live postings with distinct ids. AMD requisition 90925 in San Jose was
    merged with 90926 in Secaucus because one is titled "Research Engineer
    Intern/Co-op" and the other "Research Engineering Intern/Co-op"; three
    separate Hardware Design Verification requisitions became one; Apple's
    Machine Learning and AI Intern absorbed the PhD Intern posting next to it,
    which is a different role with different requirements. Citadel's London and
    Greenwich postings of one title became a single row that could only show
    one of the two cities.

    None of that is visible afterwards. The surviving row looks exactly like a
    correctly deduplicated job, which is what makes this the failure worth
    spending a rule on rather than the duplicate it was trying to avoid.
    """
    if (held := job_row["ats_key"]) and ats_key and held != ats_key:
        return True
    if (held := job_row["url_key"]) and url_key and held != url_key:
        return True
    return False


def find(con, listing: RawListing, ats_key, url_key, cid) -> tuple[int | None, str]:
    """The job this listing is, and which tier said so."""
    if ats_key:
        row = con.execute("SELECT * FROM job WHERE ats_key=?", (ats_key,)).fetchone()
        if row:
            return row["id"], "ats"

    if url_key:
        row = con.execute("SELECT * FROM job WHERE url_key=?", (url_key,)).fetchone()
        # A job already carrying a different ATS identity is not this one, even
        # though the URLs agree -- that combination means one of the two URLs is
        # shared by more than one posting, and the identity is the better
        # evidence.
        if row and not _conflicts(row, ats_key):
            return row["id"], "url"

    if cid is None:
        return None, "new"

    want_title = normalize.title(listing.title)
    want_season = normalize.season(listing.season)
    if not want_title:
        return None, "new"
    best, best_score = None, 0.0
    for row in con.execute("SELECT * FROM job WHERE company_id=?", (cid,)):
        if _conflicts(row, ats_key, url_key):
            continue
        score = SequenceMatcher(None, want_title, normalize.title(row["title"])).ratio()
        if score < TITLE_MATCH:
            continue
        # A season that disagrees is a different posting: employers run the
        # same title every term, and Summer 2027 is not Spring 2027.
        theirs = normalize.season(row["season"] or "")
        if want_season and theirs and want_season != theirs:
            continue
        if score > best_score:
            best, best_score = row["id"], score
    return (best, "text") if best else (None, "new")


def record(con, listing: RawListing, now: float | None = None) -> tuple[int, str]:
    """Store one listing. Returns (job id, how it was matched)."""
    now = now or time.time()
    ats_key, url_key = keys(listing)
    cid = company_id(con, listing.company, listing.company_slug)
    job_id, how = find(con, listing, ats_key, url_key, cid)

    if job_id is None:
        # posted_at from the source when there is one. When there is not, our
        # own first sighting stands in and is flagged as doing so -- an
        # estimate rendered as a fact is how a list of "posted today" quietly
        # becomes a list of "noticed today".
        posted = listing.posted_at
        cur = con.execute(
            "INSERT INTO job(company_id,title,locations,canonical_url,ats,"
            "ats_key,url_key,season,sponsorship,category,posted_at,"
            "posted_at_is_estimate,first_seen_at,last_seen_at,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, listing.title, json.dumps(listing.locations),
             canonical_url(listing.url) if listing.url else None,
             (ats_key or ":").split(":")[0] or None, ats_key, url_key,
             listing.season, listing.sponsorship, listing.category,
             posted or now, 0 if (posted and listing.posted_at_is_real) else 1,
             now, now,
             "open" if listing.active else "closed"))
        job_id, how = cur.lastrowid, "new"
    else:
        _enrich(con, job_id, listing, ats_key, url_key, now, how)

    con.execute(
        "INSERT OR IGNORE INTO sighting(job_id,source,source_record_id,raw_url,"
        "raw_payload,seen_at,source_reported_at,matched_by) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (job_id, listing.source, listing.source_record_id, listing.url,
         json.dumps(listing.raw)[:20000], now, listing.posted_at, how))
    return job_id, how


def _enrich(con, job_id: int, listing: RawListing, ats_key, url_key,
            now: float, how: str = "ats") -> None:
    """Let a second sighting fill in what the first one did not know.

    A story gives a link and nothing else; Simplify gives the season, the
    sponsorship and a real posted date. Whichever arrives second should leave
    the row better than it found it, so a field is written only where it is
    currently absent -- with one exception. An earlier posted date from a real
    source replaces a later one, and replaces an estimate outright: the
    question the row answers is when the employer posted it, and the earliest
    source that actually knows is the best answer available.
    """
    row = con.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
    sets, args = ["last_seen_at=?"], [now]

    # Title, employer and locations are here for a reason that only shows up
    # when the sources arrive in the other order. A story link carries a URL
    # and nothing else, so it creates a row with an empty title; Simplify then
    # matches that row and knows all three. Without backfilling them the answer
    # depended on which source was polled first -- poll Simplify first and five
    # jobs were fully described, poll Instagram first and the same five stayed
    # blank forever, because the enrich pass only looks at rows that are still
    # missing a title and this one had already been "seen".
    if listing.company and not row["company_id"]:
        if cid := company_id(con, listing.company, listing.company_slug):
            sets.append("company_id=?")
            args.append(cid)
    if listing.locations and not json.loads(row["locations"] or "[]"):
        sets.append("locations=?")
        args.append(json.dumps(listing.locations))
    if listing.title and not (row["title"] or "").strip():
        sets.append("title=?")
        args.append(listing.title)

    fillable = [("season", listing.season), ("sponsorship", listing.sponsorship),
                ("category", listing.category)]
    # Identity is only ever written by a match that was itself made on
    # identity. A text match is the weakest evidence there is -- two titles
    # that read alike at the same employer -- and letting it stamp an ATS id
    # and a URL onto the row it landed on turns that guess into something the
    # next run will treat as fact.
    #
    # It did. Restoring from a snapshot that carried no identities sent 279
    # listings to the text tier, and three of them wrote their keys onto the
    # wrong row: Tencent requisition R107344's id and URL ended up on R107363's
    # job, so the published feed had two rows claiming one URL and a row whose
    # url_key pointed at a different posting entirely. Nothing looked wrong.
    if how in ("ats", "url"):
        fillable = [("ats_key", ats_key), ("url_key", url_key)] + fillable
        if ats_key and not row["ats"]:
            sets.append("ats=?")
            args.append(ats_key.split(":")[0])
    for column, value in fillable:
        if value and not row[column]:
            sets.append(f"{column}=?")
            args.append(value)

    # A real date always beats an estimate. Between two real dates the earlier
    # wins. An estimate never overwrites a real one, however early it looks --
    # a story shared before Simplify noticed the job is still not the employer
    # telling us when they posted it.
    held_is_estimate = bool(row["posted_at_is_estimate"])
    if listing.posted_at and listing.posted_at_is_real and (
            held_is_estimate or listing.posted_at < (row["posted_at"] or 9e18)):
        sets += ["posted_at=?", "posted_at_is_estimate=0"]
        args.append(listing.posted_at)
    elif (listing.posted_at and not listing.posted_at_is_real and held_is_estimate
            and listing.posted_at < (row["posted_at"] or 9e18)):
        sets.append("posted_at=?")
        args.append(listing.posted_at)

    if not listing.active and row["status"] == "open":
        sets += ["status=?", "closed_at=?"]
        args += ["closed", now]

    args.append(job_id)
    con.execute(f"UPDATE job SET {', '.join(sets)} WHERE id=?", args)
