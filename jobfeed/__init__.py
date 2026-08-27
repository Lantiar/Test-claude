"""jobfeed -- one deduplicated list of jobs, from several sources, with dates."""
from __future__ import annotations

import time


def log_note(con, source: str, note: str) -> None:
    """Attach a note to this source's most recent run.

    Notes are the things that are not errors and are not nothing: no OCR
    available, a field missing from an API response, an account with no live
    stories. A run that returns zero items for a good reason and a run that
    returns zero because it broke look identical in a count, and the note is
    what tells them apart afterwards.
    """
    row = con.execute("SELECT id, error FROM source_run WHERE source=? "
                      "ORDER BY started_at DESC LIMIT 1", (source,)).fetchone()
    if row is None:
        return
    existing = row["error"] or ""
    if note in existing:
        return
    con.execute("UPDATE source_run SET error=? WHERE id=?",
                (f"{existing}; {note}".strip("; "), row["id"]))
