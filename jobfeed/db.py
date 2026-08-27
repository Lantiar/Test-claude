"""SQLite, one file, append-only where it matters.

Two tables carry the design:

`sighting` is append-only. A job row is never edited in place by an adapter --
each poll appends what that source said this time, and the job row is derived
from its sightings. It costs a little space and it buys the only question that
actually gets asked when something looks wrong: where did this value come from,
and when. A store that overwrites can answer what it believes and never why.

`source_run` records every poll: what was fetched, how many items came back,
how many were new, and what went wrong. Without it "no new jobs today" and
"the adapter broke three weeks ago" are the same observation, and the second
one is invisible for as long as nobody happens to look.
"""
from __future__ import annotations

import os
import sqlite3
import time

DEFAULT_PATH = os.getenv("JOBFEED_DB", "data/jobfeed.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS company (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  norm        TEXT NOT NULL UNIQUE,   -- normalised, what matching compares
  slug        TEXT,                   -- simplify.jobs/c/<slug>, when known
  created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS job (
  id           INTEGER PRIMARY KEY,
  company_id   INTEGER REFERENCES company(id),
  title        TEXT NOT NULL,
  locations    TEXT,                  -- json array
  canonical_url TEXT,
  ats          TEXT,
  ats_key      TEXT UNIQUE,           -- tier 1 identity; NULL when unknown
  url_key      TEXT,                  -- tier 2 identity; NULL for index pages
  season       TEXT,
  sponsorship  TEXT,
  category     TEXT,
  -- When the employer posted it, as best we can tell, and whether that is a
  -- real date from a source or our own first sighting standing in for one.
  posted_at    REAL,
  posted_at_is_estimate INTEGER NOT NULL DEFAULT 1,
  first_seen_at REAL NOT NULL,
  last_seen_at  REAL NOT NULL,
  status       TEXT NOT NULL DEFAULT 'open',   -- open | closed | unknown
  closed_at    REAL
);
CREATE INDEX IF NOT EXISTS job_url_key   ON job(url_key);
CREATE INDEX IF NOT EXISTS job_company   ON job(company_id);
CREATE INDEX IF NOT EXISTS job_first_seen ON job(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS job_posted    ON job(posted_at DESC);

CREATE TABLE IF NOT EXISTS sighting (
  id            INTEGER PRIMARY KEY,
  job_id        INTEGER NOT NULL REFERENCES job(id),
  source        TEXT NOT NULL,        -- simplify | instagram | ...
  source_record_id TEXT,              -- that source's own id for the record
  raw_url       TEXT,
  raw_payload   TEXT,                 -- json, exactly as the source gave it
  seen_at       REAL NOT NULL,        -- when we looked
  source_reported_at REAL,            -- when the source says it was posted
  matched_by    TEXT                  -- ats | url | text | new
);
CREATE INDEX IF NOT EXISTS sighting_job ON sighting(job_id);
CREATE UNIQUE INDEX IF NOT EXISTS sighting_once
  ON sighting(source, source_record_id, seen_at);

-- Links from stories that are not job postings. Kept, not discarded: a
-- resource, a writeup and a tool are the other half of what gets posted.
CREATE TABLE IF NOT EXISTS link (
  id            INTEGER PRIMARY KEY,
  url           TEXT NOT NULL,
  canonical_url TEXT NOT NULL UNIQUE,
  kind          TEXT NOT NULL DEFAULT 'unknown',  -- job|article|tool|unknown
  title         TEXT,
  note          TEXT,
  source        TEXT NOT NULL,
  story_ref     TEXT,
  first_seen_at REAL NOT NULL,
  last_seen_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS merge_log (
  id        INTEGER PRIMARY KEY,
  kept_job  INTEGER NOT NULL,
  merged_job INTEGER NOT NULL,
  rule      TEXT NOT NULL,
  score     REAL,
  at        REAL NOT NULL,
  undone_at REAL
);

CREATE TABLE IF NOT EXISTS source_run (
  id          INTEGER PRIMARY KEY,
  source      TEXT NOT NULL,
  started_at  REAL NOT NULL,
  finished_at REAL,
  http_status INTEGER,
  items_seen  INTEGER NOT NULL DEFAULT 0,
  items_new   INTEGER NOT NULL DEFAULT 0,
  items_merged INTEGER NOT NULL DEFAULT 0,
  error       TEXT
);
CREATE INDEX IF NOT EXISTS source_run_source ON source_run(source, started_at DESC);

-- Whatever an adapter needs to remember between polls: an ETag, the last
-- story id seen. Kept here rather than in a dotfile so one backup is the whole
-- state of the system.
CREATE TABLE IF NOT EXISTS adapter_state (
  source TEXT NOT NULL,
  key    TEXT NOT NULL,
  value  TEXT,
  PRIMARY KEY (source, key)
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DEFAULT_PATH
    if d := os.path.dirname(path):
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def get_state(con, source: str, key: str) -> str | None:
    row = con.execute("SELECT value FROM adapter_state WHERE source=? AND key=?",
                      (source, key)).fetchone()
    return row["value"] if row else None


def set_state(con, source: str, key: str, value: str) -> None:
    con.execute("INSERT INTO adapter_state(source,key,value) VALUES(?,?,?) "
                "ON CONFLICT(source,key) DO UPDATE SET value=excluded.value",
                (source, key, value))


class Run:
    """One poll of one source, recorded whether or not it succeeds."""

    def __init__(self, con, source: str):
        self.con, self.source = con, source
        self.seen = self.new = self.merged = 0
        self.http_status: int | None = None

    def __enter__(self):
        cur = self.con.execute(
            "INSERT INTO source_run(source, started_at) VALUES(?,?)",
            (self.source, time.time()))
        self.id = cur.lastrowid
        return self

    def __exit__(self, exc_type, exc, tb):
        self.con.execute(
            "UPDATE source_run SET finished_at=?, http_status=?, items_seen=?, "
            "items_new=?, items_merged=?, error=? WHERE id=?",
            (time.time(), self.http_status, self.seen, self.new, self.merged,
             f"{exc_type.__name__}: {exc}" if exc else None, self.id))
        self.con.commit()
        return False        # never swallow: a failed poll must look failed
