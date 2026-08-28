"""Where you are with each job.

Everything else in jobfeed is derived: delete it and the next poll rebuilds it.
This is the opposite -- it is the only thing here you cannot get back, so it is
kept apart from the pipeline that rewrites itself every hour, keyed on
something that outlives a rebuild, and never published.
"""
from __future__ import annotations

import time

# In the order an application actually moves. Kept as an ordered tuple rather
# than a set because a viewer wants to show them in this order, and because
# "further along than" is a question worth being able to ask.
STAGES = ("interested", "applied", "oa", "interview", "final", "offer",
          "accepted", "rejected")

# The ones that mean the application is over, either way.
CLOSED = ("accepted", "rejected")

DEFAULT = "interested"


def job_key(row) -> str:
    """The handle a status hangs on, stable across rebuilds of the database.

    job.id is not it: the scheduled runner rebuilds from a snapshot every hour
    and hands out fresh ids, so a status keyed on one would end up pointing at
    whatever job happened to land on that row next.
    """
    return (row["ats_key"] or row["url_key"] or row["canonical_url"]
            or f"job:{row['id']}")


def get(con, key: str) -> dict | None:
    row = con.execute("SELECT * FROM application WHERE job_key=?", (key,)).fetchone()
    return dict(row) if row else None


def all_stages(con) -> dict[str, dict]:
    return {r["job_key"]: dict(r) for r in con.execute("SELECT * FROM application")}


def set_stage(con, key: str, stage: str, note: str | None = None) -> dict:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of "
                         f"{', '.join(STAGES)}")
    now = time.time()
    existing = get(con, key)
    # When you actually applied, recorded once and never moved. Later stages
    # imply it happened, so a status that jumps straight to "interview" still
    # gets a date rather than a blank.
    applied_at = (existing or {}).get("applied_at")
    if applied_at is None and stage != "interested":
        applied_at = now
    con.execute(
        "INSERT INTO application(job_key,stage,note,applied_at,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(job_key) DO UPDATE SET "
        "stage=excluded.stage, note=COALESCE(excluded.note, application.note), "
        "applied_at=excluded.applied_at, updated_at=excluded.updated_at",
        (key, stage, note, applied_at, now))
    con.commit()
    return get(con, key)


def clear(con, key: str) -> None:
    con.execute("DELETE FROM application WHERE job_key=?", (key,))
    con.commit()


def counts(con) -> dict[str, int]:
    out = {s: 0 for s in STAGES}
    for r in con.execute("SELECT stage, COUNT(*) n FROM application GROUP BY stage"):
        out[r["stage"]] = r["n"]
    return out
