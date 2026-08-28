"""Write the job list as static JSON for other things to read.

The consumer is a viewer somebody else is building, so the contract matters
more than the convenience: field names are stable, every timestamp is an ISO-8601
string in UTC as well as an epoch, and nothing is abbreviated to save bytes.
A viewer that has to guess whether `posted` means the employer's date or ours
will guess wrong, so both are present and the estimate is flagged.

Three files rather than one, because a phone loading a job list should not
download the entire corpus to show this week's:

  jobs.json    every open job
  recent.json  the ones posted in the last fortnight
  meta.json    when this was generated, and whether each source is actually
               working -- so a viewer can say "stale" instead of quietly
               showing yesterday's list as though it were today's
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time


def _iso(ts) -> str | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def _job(row) -> dict:
    return {
        "id": row["id"],
        "company": row["company"],
        "title": row["title"],
        "url": row["canonical_url"],
        "locations": json.loads(row["locations"] or "[]"),
        "season": row["season"] or None,
        "category": row["category"] or None,
        "sponsorship": row["sponsorship"] or None,
        "ats": row["ats"],
        # The ATS's own id for the posting, and the stable key across runs.
        # Absent from the first version of this file, which mattered more than
        # it looked: the snapshot is what a fresh runner restores from, so
        # leaving it out meant every restored job came back without its
        # identity and had to be re-matched on its URL. Useful to a viewer for
        # the same reason -- it is the one field that does not change when an
        # employer edits a title or moves a posting.
        "ats_key": row["ats_key"],
        "url_key": row["url_key"],
        # The employer's posting date where a source knew it, otherwise our
        # first sighting standing in. Never render the second as the first:
        # posted_is_estimate is the whole reason both fields are here.
        "posted_at": _iso(row["posted_at"]),
        "posted_at_epoch": row["posted_at"],
        "posted_is_estimate": bool(row["posted_at_is_estimate"]),
        "first_seen_at": _iso(row["first_seen_at"]),
        "first_seen_epoch": row["first_seen_at"],
        "status": row["status"],
        "sources": sorted((row["sources"] or "").split(",")) if row["sources"] else [],
    }


SELECT = """
  SELECT j.*, c.name AS company,
         (SELECT GROUP_CONCAT(DISTINCT s.source) FROM sighting s
           WHERE s.job_id = j.id) AS sources
  FROM job j LEFT JOIN company c ON c.id = j.company_id
  WHERE j.status = 'open'
  ORDER BY j.posted_at DESC
"""


def publish(con, out: str = "site", recent_days: int = 14) -> dict:
    os.makedirs(out, exist_ok=True)
    rows = [_job(r) for r in con.execute(SELECT)]
    # Recent by the employer's posting date, not by when this feed noticed it.
    # first_seen_at collapses whenever the database is rebuilt -- every job is
    # then "new today" and recent.json becomes a copy of jobs.json, which is
    # exactly what it is meant to save a viewer from downloading. posted_at is
    # also the date a person is actually asking about.
    cutoff = time.time() - recent_days * 86400
    recent = [j for j in rows if (j["posted_at_epoch"] or 0) >= cutoff]

    sources = []
    for r in con.execute(
            "SELECT source, MAX(started_at) AS started_at, finished_at, "
            "items_seen, items_new, error FROM source_run GROUP BY source"):
        sources.append({
            "source": r["source"],
            "last_run_at": _iso(r["started_at"]),
            "items_seen": r["items_seen"],
            "items_new": r["items_new"],
            # Present and null when the run was clean. A viewer showing source
            # health needs to tell "ran fine" from "has never run".
            "note": r["error"],
        })

    meta = {
        "generated_at": _iso(time.time()),
        "generated_at_epoch": time.time(),
        "counts": {
            "open_jobs": len(rows),
            "recent_jobs": len(recent),
            "recent_days": recent_days,
            "companies": con.execute("SELECT COUNT(*) FROM company").fetchone()[0],
            "with_real_posted_date": sum(1 for j in rows if not j["posted_is_estimate"]),
            "other_links": con.execute("SELECT COUNT(*) FROM link").fetchone()[0],
        },
        "sources": sources,
        "schema": "https://github.com/Lantiar/Test-claude/blob/main/jobfeed/README.md",
    }

    links = [{
        "url": r["url"], "kind": r["kind"],
        "first_seen_at": _iso(r["first_seen_at"]), "source": r["source"],
    } for r in con.execute("SELECT * FROM link ORDER BY first_seen_at DESC")]

    # The dashboard, served from the same place as the data it reads. Relative
    # fetches, so it works unchanged on GitHub Pages, on Vercel, or from any
    # directory these files are copied into.
    import shutil

    shutil.copyfile(os.path.join(os.path.dirname(__file__), "web.html"),
                    os.path.join(out, "index.html"))
    # The write endpoint, for the deployment that has one. Harmless where
    # nothing runs it: GitHub Pages serves the file as text, the page's probe
    # fails, and it falls back to keeping stages in the browser.
    api_src = os.path.join(os.path.dirname(__file__), "api")
    if os.path.isdir(api_src):
        shutil.copytree(api_src, os.path.join(out, "api"), dirs_exist_ok=True)

    # For Vercel, if this directory is deployed there instead of served by
    # Pages. Ignored everywhere else. The page must not be cached longer than
    # the data behind it: an hour-old index.html against a fresh jobs.json is
    # harmless, but a cached jobs.json is a dashboard confidently showing
    # yesterday, which is the one failure this whole project keeps guarding
    # against.
    with open(os.path.join(out, "vercel.json"), "w") as fh:
        json.dump({"headers": [
            {"source": "/(.*).json",
             "headers": [{"key": "Cache-Control",
                          "value": "public, max-age=0, must-revalidate"},
                         {"key": "Access-Control-Allow-Origin", "value": "*"}]},
        ]}, fh, indent=1)

    for name, payload in (("jobs.json", rows), ("recent.json", recent),
                          ("links.json", links), ("meta.json", meta)):
        with open(os.path.join(out, name), "w") as fh:
            json.dump(payload, fh, indent=1 if name == "meta.json" else None)
    return {"jobs": len(rows), "recent": len(recent), "links": len(links)}
