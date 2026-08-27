"""One poll of one source, from fetch to stored, with the run recorded."""
from __future__ import annotations

import time

from . import db as _db
from . import classify, dedupe
from .sources import get, load_all


def poll(con, source: str, **kwargs) -> dict:
    """Run one source. Returns a summary; the same numbers go into source_run."""
    load_all()
    fn = get(source)
    counts = {"seen": 0, "new": 0, "ats": 0, "url": 0, "text": 0, "link": 0}
    with _db.Run(con, source) as run:
        for listing in fn(con, **kwargs):
            counts["seen"] += 1
            # A source that only yields jobs says so and skips the question. A
            # story feed carries both, and the ask was to keep both -- so what
            # is not a job is filed as a link rather than dropped, and what
            # cannot be told apart is filed as unknown rather than guessed at.
            # A link wrongly promoted into the job list is a row that no amount
            # of deduplication will ever tidy away.
            if getattr(fn, "jobs_only", False):
                kind = "job"
            else:
                kind = classify.kind(listing.url)
            if kind == "job":
                _, how = dedupe.record(con, listing)
                counts[how] = counts.get(how, 0) + 1
            else:
                store_link(con, listing, kind)
                counts["link"] += 1
        run.seen = counts["seen"]
        run.new = counts["new"]
        run.merged = counts["ats"] + counts["url"] + counts["text"]
        run.http_status = getattr(fn, "last_status", None)
    return counts


def retire_missing(con, source: str, older_than_days: float = 14) -> int:
    """Close jobs this source has stopped listing.

    Only for a source that publishes its whole catalogue every poll, which
    Simplify does and a story feed cannot: a link that appeared in a story once
    is not withdrawn by not appearing again. Passing a source that only ever
    reports additions would close everything it ever found.
    """
    cutoff = time.time() - older_than_days * 86400
    cur = con.execute(
        "UPDATE job SET status='closed', closed_at=? "
        "WHERE status='open' AND last_seen_at < ? AND id IN "
        "  (SELECT job_id FROM sighting WHERE source=?)", (time.time(), cutoff, source))
    con.commit()
    return cur.rowcount


def store_link(con, listing, kind: str) -> None:
    """A story link that is not a job posting. Kept, with when it appeared."""
    from .identity import canonical_url

    now = time.time()
    canon = canonical_url(listing.url)
    con.execute(
        "INSERT INTO link(url,canonical_url,kind,title,source,story_ref,"
        "first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(canonical_url) DO UPDATE SET last_seen_at=excluded.last_seen_at",
        (listing.url, canon, kind, listing.title or None, listing.source,
         listing.story_ref or None, listing.posted_at or now, now))
