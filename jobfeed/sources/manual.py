"""Postings added by hand, because the other sources do not carry them.

Simplify lists what Simplify lists, and a story feed lists what someone
happened to post. Neither is the set of jobs one person has actually applied
to: Lyft's Summer 2027 software internships are on Lyft's own Greenhouse
board and in neither source, so an application to one had nothing in the feed
to attach to -- and the outreach that was queued instead attached to Lyft's
full-time Software Engineer role, which is a different job with a different
letter. Two drafts named the wrong role and the first was nine minutes from
sending.

A pinned file rather than a one-off insert. The runner is stateless and the
published snapshot is the store, so a row added once to a database that is
rebuilt every run is a row that survives until the next poll overwrites the
snapshot without it. Everything in this file is re-yielded on every poll, and
dedupe matches it back to the same job by ATS key, so it cannot be lost and
cannot be duplicated.

Titles and companies are recorded in the file rather than resolved at poll
time. Resolving is an HTTP fetch per entry per poll, for an answer that does
not change; `jobfeed add` does it once, when the entry is written.
"""
from __future__ import annotations

import json
import pathlib
import time

from ..models import RawListing
from . import register

NAME = "manual"
PATH = pathlib.Path(__file__).resolve().parents[1] / "manual.json"


def entries(path: pathlib.Path | None = None) -> list[dict]:
    """What is in the file, or nothing. An absent file is the normal state."""
    p = path or PATH
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        return []
    except Exception as exc:
        # Malformed is worth saying out loud: silently yielding nothing here
        # looks exactly like an empty file, and the jobs would quietly leave
        # the feed.
        print(f"  manual: {p.name} is not readable JSON: {exc}")
        return []
    return [e for e in data if isinstance(e, dict) and e.get("url")]


def listings(con=None, path: pathlib.Path | None = None):
    for e in entries(path):
        yield RawListing(
            source=NAME,
            # The URL is the record id. Adding the same posting twice is then
            # the same record, not a second one.
            source_record_id=e["url"],
            url=e["url"],
            title=e.get("title") or "",
            company=e.get("company") or "",
            locations=list(e.get("locations") or []),
            season=e.get("season") or "",
            # When it was added here, which is not when the employer posted
            # it and does not claim to be.
            posted_at=e.get("added_at") or time.time(),
            posted_at_is_real=False,
            active=e.get("active", True),
            raw=e,
        )
    listings.last_status = 200


# Every entry is a posting someone applied to, so ingest need not classify.
listings.jobs_only = True
register(NAME, listings)
