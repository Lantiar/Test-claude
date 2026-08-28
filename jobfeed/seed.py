"""Load a published snapshot back into an empty database.

The runner is stateless: every scheduled run starts on a fresh machine, so
whatever the last run knew has to come back from somewhere or it is gone. A
build cache was the obvious place to put the database and the wrong one, and
the reason is specific rather than general.

Simplify is idempotent -- lose it entirely and the next poll refetches all
2,200 listings in about a second. Stories are not. A link zero2sudo posted on
Tuesday is unreachable on Thursday: the story has expired, and for the jobs
Simplify does not carry there is no other copy anywhere. So a cache eviction
would not degrade the data, it would delete exactly the part of it that cannot
be re-derived, and the run afterwards would look completely healthy.

Hence: the published snapshot is the store, and this reads it back. What is
restored keeps its original first_seen_at -- the record of when a job appeared,
which is the other thing no amount of refetching can reconstruct.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from . import dedupe


def load(path_or_url: str) -> list[dict]:
    if path_or_url.startswith(("http://", "https://")):
        with urllib.request.urlopen(path_or_url, timeout=60) as r:
            return json.loads(r.read())
    if not os.path.exists(path_or_url):
        return []
    with open(path_or_url) as fh:
        text = fh.read().strip()
    if not text:
        return []
    if text.lstrip().startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def seed(con, path_or_url: str) -> dict:
    """Insert snapshot jobs that this database does not already hold."""
    jobs = load(path_or_url)
    added = skipped = 0
    for j in jobs:
        from .identity import canonical_url, identify, looks_like_one_posting

        ats_key, url_key = j.get("ats_key"), j.get("url_key")
        # Recomputed when a snapshot predates these fields, and canonicalised
        # rather than taken verbatim: the lookup this key is compared against
        # is canonical, so a raw one silently misses and drops the listing to
        # the text tier. That is how a restore turned 39 text matches into 279.
        if not ats_key and j.get("url"):
            ident = identify(j["url"])
            ats_key = ident.key if ident else None
        if not url_key and j.get("url"):
            url_key = (canonical_url(j["url"])
                       if looks_like_one_posting(j["url"]) else None)
        # Already here (this run polled it before seeding, or it is a repeat
        # seed): leave the live row alone rather than writing an older copy
        # over it.
        if ats_key and con.execute("SELECT 1 FROM job WHERE ats_key=?",
                                   (ats_key,)).fetchone():
            skipped += 1
            continue
        if url_key and con.execute("SELECT 1 FROM job WHERE url_key=?",
                                   (url_key,)).fetchone():
            skipped += 1
            continue

        cid = dedupe.company_id(con, j["company"]) if j.get("company") else None
        cur = con.execute(
            "INSERT INTO job(company_id,title,locations,canonical_url,ats,"
            "ats_key,url_key,season,sponsorship,category,posted_at,"
            "posted_at_is_estimate,first_seen_at,last_seen_at,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, j.get("title") or "", json.dumps(j.get("locations") or []),
             j.get("url"), j.get("ats"), ats_key, url_key, j.get("season"),
             j.get("sponsorship"), j.get("category"),
             j.get("posted_at_epoch"), 1 if j.get("posted_is_estimate") else 0,
             # The original first sighting, not this one. Restoring it as "now"
             # would quietly redate every job in the feed to the moment a cache
             # happened to expire.
             j.get("first_seen_epoch") or time.time(),
             time.time(), j.get("status") or "open"))
        for source in j.get("sources") or []:
            # Marked restored rather than dressed up as a fresh observation:
            # this is a record of what a previous run saw, and the audit trail
            # should say so.
            con.execute(
                "INSERT OR IGNORE INTO sighting(job_id,source,seen_at,"
                "source_reported_at,matched_by) VALUES(?,?,?,?,?)",
                (cur.lastrowid, source, j.get("first_seen_epoch") or time.time(),
                 j.get("posted_at_epoch"), "restored"))
        added += 1
    con.commit()
    return {"in_snapshot": len(jobs), "restored": added, "already_here": skipped}
