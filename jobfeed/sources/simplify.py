"""SimplifyJobs' listings.json -- the file their README is generated from.

Worth using directly rather than parsing the README table: it is the same data
with the markdown taken off, and it carries fields the table does not, notably
date_posted and date_updated as epochs and an active flag. 14,860 records, of
which 2,181 were active and visible when this was written.

The URL each record carries is the employer's real ATS link -- Greenhouse,
Ashby, Workday, Lever -- which is why this source alone gives an exact identity
for 85% of what it lists.

One trap in the data, and it is the reason identity.looks_like_one_posting
exists: for a minority of employers the link is the company's careers index
rather than the posting. Fifteen separate Zipline internships all carry
zipline.com/open-roles. Nothing in the record marks these as different from the
rest.
"""
from __future__ import annotations

import json
import time
import urllib.request

from .. import db as _db
from ..models import RawListing
from . import register

# The repo is renamed as seasons roll over -- Summer2026-Internships now
# redirects here -- so following redirects matters more than the name does.
URL = ("https://raw.githubusercontent.com/SimplifyJobs/"
       "Summer2027-Internships/dev/.github/scripts/listings.json")

NAME = "simplify"


def fetch(con=None, url: str = URL, timeout: int = 60) -> tuple[list[dict], int]:
    """The listings, and the HTTP status. 304 means nothing changed."""
    req = urllib.request.Request(url, headers={"User-Agent": "jobfeed/0.1"})
    etag = _db.get_state(con, NAME, "etag") if con is not None else None
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            if con is not None and (tag := r.headers.get("ETag")):
                _db.set_state(con, NAME, "etag", tag)
            return json.loads(body), r.status
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return [], 304
        raise


def listings(con=None, include_inactive: bool = False):
    """RawListings for everything currently on offer."""
    records, status = fetch(con)
    for r in records:
        active = bool(r.get("active")) and bool(r.get("is_visible", True))
        if not active and not include_inactive:
            continue
        yield RawListing(
            source=NAME,
            source_record_id=str(r.get("id") or ""),
            url=r.get("url") or "",
            title=r.get("title") or "",
            company=r.get("company_name") or "",
            locations=list(r.get("locations") or []),
            season=", ".join(r.get("terms") or []),
            sponsorship=r.get("sponsorship") or "",
            category=r.get("category") or "",
            company_slug=(r.get("company_url") or "").rstrip("/").rsplit("/", 1)[-1],
            # Epochs, and both are real: only 158 of 2,181 active records have
            # date_posted equal to date_updated, so the two carry different
            # information and date_posted is the one that means what we want.
            posted_at=_epoch(r.get("date_posted")),
            updated_at=_epoch(r.get("date_updated")),
            active=active,
            raw=r,
        )
    listings.last_status = status


def _epoch(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Guard against a source switching to milliseconds without telling anyone.
    if f > 1e11:
        f /= 1000.0
    return f if 9e8 < f < time.time() + 86400 * 400 else None


# Everything this source yields is a job posting, so ingest need not ask.
listings.jobs_only = True
register(NAME, listings)
