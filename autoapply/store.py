"""SQLite persistence: applied log, mapping cache, review queue, corrections."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS applied (
    key           TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    company       TEXT, title TEXT, ats TEXT,
    status        TEXT NOT NULL,
    submitted_at  REAL,
    screenshot_path TEXT,
    fields_json   TEXT            -- what actually went out, for your own records
);
CREATE TABLE IF NOT EXISTS cache (
    sig         TEXT PRIMARY KEY,   -- ats|normalized-label, NOT host: boards are per-company
    ats         TEXT, label TEXT,
    action      TEXT, profile_path TEXT,
    confidence  REAL DEFAULT 0.0,
    unconfirmed INTEGER DEFAULT 1,  -- must survive one human review before it counts
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT UNIQUE, url TEXT, ats TEXT,
    company       TEXT, title TEXT,
    screenshot_path TEXT,
    fields_json   TEXT, mappings_json TEXT, reasons_json TEXT,
    created_at    REAL, resolved_at REAL, resolution TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sig TEXT, label TEXT, corrected_value TEXT, ts REAL
);
"""


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("DB_PATH", "data/autoapply.sqlite")
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- applied log -----------------------------------------------------
    def already_applied(self, key: str) -> bool:
        row = self.db.execute(
            "SELECT status FROM applied WHERE key=? AND status='applied'", (key,)
        ).fetchone()
        return row is not None

    def record_applied(self, job, status: str, screenshot: str = "", fields: Any = None):
        self.db.execute(
            """INSERT INTO applied(key,url,company,title,ats,status,submitted_at,
                                   screenshot_path,fields_json)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 status=excluded.status, submitted_at=excluded.submitted_at,
                 screenshot_path=excluded.screenshot_path,
                 fields_json=excluded.fields_json""",
            (job.key, job.url, job.company, job.title, job.ats, status, time.time(),
             screenshot, json.dumps(fields or {})),
        )
        self.db.commit()

    def submits_since(self, since_ts: float) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) c FROM applied WHERE status='applied' AND submitted_at>=?",
            (since_ts,),
        ).fetchone()
        return int(row["c"])

    # ---- mapping cache ---------------------------------------------------
    def cache_get(self, sig: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM cache WHERE sig=?", (sig,)).fetchone()

    def cache_put(self, sig: str, ats: str, label: str, action: str,
                  profile_path: str, confidence: float, unconfirmed: bool = True):
        self.db.execute(
            """INSERT INTO cache(sig,ats,label,action,profile_path,confidence,
                                 unconfirmed,updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(sig) DO UPDATE SET
                 action=excluded.action, profile_path=excluded.profile_path,
                 confidence=excluded.confidence,
                 unconfirmed=MIN(cache.unconfirmed, excluded.unconfirmed),
                 updated_at=excluded.updated_at""",
            (sig, ats, label, action, profile_path, confidence,
             1 if unconfirmed else 0, time.time()),
        )
        self.db.commit()

    def confirm_cache(self, sig: str):
        self.db.execute("UPDATE cache SET unconfirmed=0 WHERE sig=?", (sig,))
        self.db.commit()

    # ---- queue -----------------------------------------------------------
    def enqueue(self, job, outcome, reasons: list[str]):
        self.db.execute(
            """INSERT INTO queue(key,url,ats,company,title,screenshot_path,
                                 fields_json,mappings_json,reasons_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 screenshot_path=excluded.screenshot_path,
                 fields_json=excluded.fields_json,
                 mappings_json=excluded.mappings_json,
                 reasons_json=excluded.reasons_json,
                 created_at=excluded.created_at, resolved_at=NULL, resolution=NULL""",
            (job.key, job.url, job.ats, job.company, job.title,
             outcome.screenshot_path if outcome else "",
             json.dumps([f.__dict__ for f in (outcome.fields if outcome else [])]),
             json.dumps([m.__dict__ for m in (outcome.mappings if outcome else [])]),
             json.dumps(reasons), time.time()),
        )
        self.db.commit()

    def queue_list(self, include_resolved: bool = False) -> list[sqlite3.Row]:
        q = "SELECT * FROM queue"
        if not include_resolved:
            q += " WHERE resolved_at IS NULL"
        return self.db.execute(q + " ORDER BY created_at DESC").fetchall()

    def queue_get(self, qid: int) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM queue WHERE id=?", (qid,)).fetchone()

    def resolve_queue(self, qid: int, resolution: str):
        self.db.execute(
            "UPDATE queue SET resolved_at=?, resolution=? WHERE id=?",
            (time.time(), resolution, qid),
        )
        self.db.commit()

    # ---- learning --------------------------------------------------------
    def record_correction(self, sig: str, label: str, corrected_value: str):
        """A human touched this field, so the mapping is now trusted."""
        self.db.execute(
            "INSERT INTO feedback(sig,label,corrected_value,ts) VALUES(?,?,?,?)",
            (sig, label, corrected_value, time.time()),
        )
        self.db.execute("UPDATE cache SET unconfirmed=0, confidence=1.0 WHERE sig=?", (sig,))
        self.db.commit()

    def forget(self, sig: str) -> int:
        """Drop a taught answer. Returns how many rows went.

        There was no way to do this, and a taught answer outranks every other
        source and is held out of re-auditing -- so one wrong lesson was
        permanent and invisible. The store had accumulated "Last Name = Kumar"
        against a profile reading "Bharath Kumar", and three entries learned
        off a careers *search* page: "City, state, country = Search by
        Location", "Job title, skill, keyword = Tech - Software Engineering
        filter 106". Every future run would have replayed all of them with
        confidence 1.0.
        """
        cur = self.db.execute("DELETE FROM feedback WHERE sig = ?", (sig,))
        self.db.commit()
        return cur.rowcount

    def literal_for(self, sig: str) -> Optional[str]:
        """The last answer a human typed for this field signature.

        Distinct from the mapping cache: that stores which profile path answers
        a field, while this stores a literal answer the profile has no home for
        (an "additional information" box, a company-specific question).
        """
        row = self.db.execute(
            "SELECT corrected_value FROM feedback WHERE sig=? ORDER BY ts DESC LIMIT 1",
            (sig,),
        ).fetchone()
        return row["corrected_value"] if row and row["corrected_value"] else None

    def stats(self) -> dict[str, int]:
        g = lambda q: int(self.db.execute(q).fetchone()[0])
        return {
            "applied": g("SELECT COUNT(*) FROM applied WHERE status='applied'"),
            "queued": g("SELECT COUNT(*) FROM queue WHERE resolved_at IS NULL"),
            "skipped": g("SELECT COUNT(*) FROM applied WHERE status='skipped'"),
            "errored": g("SELECT COUNT(*) FROM applied WHERE status='errored'"),
            "cached": g("SELECT COUNT(*) FROM cache"),
            "cached_confirmed": g("SELECT COUNT(*) FROM cache WHERE unconfirmed=0"),
        }
