"""The shared board between the web page and the runner.

The page can ask for outreach but cannot perform it -- sourcing is Apify,
sending is Gmail, and both are Python somewhere else. So a click writes an
intent to Upstash and this reads it back, does the work, and writes what
actually happened. The state on the page is therefore a report, never a hope.

Same store as the stage tracker, reached over HTTP with the standard library.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

KEY = "jobfeed:outreach"
STAGE_KEY = "jobfeed:stages"


def _creds() -> tuple[str, str] | None:
    """Found by suffix, matching what api/outreach.js accepts.

    Vercel's Upstash integration names these differently depending on how the
    store was added, and the runner's secrets are copied from there by hand --
    so a correctly-set variable under a slightly different name must not read
    as "no store".
    """
    env = os.environ

    def find(suffix: str) -> str | None:
        if env.get(suffix):
            return env[suffix]
        for name, value in env.items():
            if name.endswith(suffix) and value:
                return value
        return None

    url = find("KV_REST_API_URL") or find("UPSTASH_REDIS_REST_URL") \
        or find("REDIS_REST_URL")
    token = find("KV_REST_API_TOKEN") or find("UPSTASH_REDIS_REST_TOKEN") \
        or find("REDIS_REST_TOKEN")
    return (url, token) if url and token else None


def available() -> bool:
    return _creds() is not None


def _redis(command: list):
    creds = _creds()
    if not creds:
        raise RuntimeError("no Upstash credentials in the environment")
    url, token = creds
    req = urllib.request.Request(
        url, data=json.dumps(command).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read())
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(body["error"])
    return body.get("result") if isinstance(body, dict) else body


def read() -> dict[str, dict]:
    """job_key -> record."""
    flat = _redis(["HGETALL", KEY]) or []
    out: dict[str, dict] = {}
    for i in range(0, len(flat) - 1, 2):
        try:
            out[flat[i]] = json.loads(flat[i + 1])
        except Exception:
            out[flat[i]] = {"state": str(flat[i + 1])}
    return out


def queued() -> list[str]:
    """The job keys someone has pressed the button on."""
    return [k for k, v in read().items() if v.get("state") == "queued"]


def write(job_key: str, state: str, note: str = "", thread: str = "",
          sent: int = 0) -> None:
    record = {"state": state, "at": int(time.time())}
    if note:
        record["note"] = note[:300]
    if thread:
        record["thread"] = thread[:120]
    if sent:
        record["sent"] = int(sent)
    _redis(["HSET", KEY, job_key, json.dumps(record)])


def stages() -> dict[str, str]:
    """job_key -> stage, as the web tracker holds it.

    The runner starts from a published snapshot every time and has no
    application table of its own, so without this it sees nobody as having
    applied to anything and outreach finds nothing to do. The tracker is the
    record; this is how the runner reads it.
    """
    flat = _redis(["HGETALL", STAGE_KEY]) or []
    return {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}


# ---- the outreach store ---------------------------------------------------
#
# The runner's database is rebuilt from a published snapshot on every run, and
# that snapshot holds the feed only -- no contacts, no drafts, no send times.
# Left there, a draft written at 09:00 is gone by 09:30: the recruiter search
# is paid for again, the batch is rescheduled two days out again, and nothing
# ever reaches its send time. So outreach keeps its state here.
#
# Rows are keyed by things that survive a rebuild -- a company's name, a job
# key, an address -- and never by rowid, which is assigned fresh each time the
# feed is seeded and would silently attach a draft to the wrong contact.

STATE_KEY = "jobfeed:outreach:state"

_CONTACT = ("full_name", "first_name", "title", "linkedin_url", "email",
            "email_status", "source", "found_at", "verified_at")
_OUTREACH = ("job_key", "variant", "subject", "body", "step", "status",
             "message_id", "thread_id", "send_after", "sent_at", "campaign",
             "created_at", "polished_at", "polish_notes")


def save(con) -> dict:
    """Write every outreach table to the store. Returns what was written."""
    contacts = [
        {**{k: r[k] for k in _CONTACT},
         "company": r["company"]}
        for r in con.execute(
            "SELECT c.*, cm.name company FROM contact c "
            "LEFT JOIN company cm ON cm.id = c.company_id")]

    outreach = []
    for r in con.execute(
            "SELECT o.*, c.email FROM outreach o JOIN contact c ON c.id=o.contact_id"):
        row = {k: r[k] for k in _OUTREACH}
        row["email"] = r["email"]
        row["covers"] = [x["job_key"] for x in con.execute(
            "SELECT job_key FROM outreach_job WHERE outreach_id=?", (r["id"],))]
        outreach.append(row)

    replies = [
        {"email": r["email"], "job_key": r["job_key"], "received_at": r["received_at"],
         "from_email": r["from_email"], "kind": r["kind"],
         "bounce_type": r["bounce_type"], "snippet": r["snippet"]}
        for r in con.execute(
            "SELECT rp.*, o.job_key, c.email FROM reply rp "
            "JOIN outreach o ON o.id = rp.outreach_id "
            "JOIN contact c ON c.id = o.contact_id")]

    suppression = [dict(r) for r in con.execute(
        "SELECT s.email, s.reason, s.until, s.created_at, cm.name company "
        "FROM suppression s LEFT JOIN company cm ON cm.id = s.company_id")]
    health = [dict(r) for r in con.execute("SELECT * FROM send_health")]

    payload = {"v": 1, "at": int(time.time()), "contacts": contacts,
               "outreach": outreach, "replies": replies,
               "suppression": suppression, "health": health}
    _redis(["SET", STATE_KEY, json.dumps(payload)])
    return {"contacts": len(contacts), "outreach": len(outreach),
            "replies": len(replies)}


def load(con) -> dict:
    """Read outreach state back into a freshly seeded database."""
    raw = _redis(["GET", STATE_KEY])
    if not raw:
        return {"contacts": 0, "outreach": 0, "replies": 0}
    payload = json.loads(raw)

    def company_id(name):
        if not name:
            return None
        row = con.execute("SELECT id FROM company WHERE name=?", (name,)).fetchone()
        if row:
            return row["id"]
        # A company can be in the outreach store and absent from the feed --
        # its postings closed. The contact is still worth keeping, so the row
        # is recreated rather than dropped.
        con.execute("INSERT OR IGNORE INTO company(name, norm, created_at) "
                    "VALUES(?,?,?)", (name, name.lower(), time.time()))
        row = con.execute("SELECT id FROM company WHERE name=?", (name,)).fetchone()
        return row["id"] if row else None

    by_email = {}
    for c in payload.get("contacts", []):
        con.execute(
            "INSERT OR IGNORE INTO contact(company_id, full_name, first_name, "
            "title, linkedin_url, email, email_status, source, found_at, "
            "verified_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (company_id(c.get("company")), c["full_name"], c["first_name"],
             c["title"], c["linkedin_url"], c["email"], c["email_status"],
             c.get("source") or "apify", c["found_at"], c.get("verified_at")))
        row = con.execute("SELECT id FROM contact WHERE email=?",
                          (c["email"],)).fetchone()
        if row:
            by_email[c["email"]] = row["id"]

    for o in payload.get("outreach", []):
        contact_id = by_email.get(o.get("email"))
        if contact_id is None:
            continue
        con.execute(
            "INSERT OR IGNORE INTO outreach(job_key, contact_id, variant, subject, "
            "body, step, status, message_id, thread_id, send_after, sent_at, "
            "campaign, created_at, polished_at, polish_notes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o["job_key"], contact_id, o["variant"], o["subject"], o["body"],
             o["step"], o["status"], o["message_id"], o["thread_id"],
             o["send_after"], o["sent_at"], o.get("campaign"), o["created_at"],
             o.get("polished_at"), o.get("polish_notes")))
        row = con.execute("SELECT id FROM outreach WHERE job_key=? AND contact_id=? "
                          "AND step=?", (o["job_key"], contact_id, o["step"])).fetchone()
        for job_key in (o.get("covers") or []):
            if row:
                con.execute("INSERT OR IGNORE INTO outreach_job(outreach_id, job_key) "
                            "VALUES(?,?)", (row["id"], job_key))

    for r in payload.get("replies", []):
        contact_id = by_email.get(r.get("email"))
        if contact_id is None:
            continue
        row = con.execute("SELECT id FROM outreach WHERE job_key=? AND contact_id=? "
                          "AND step=0", (r["job_key"], contact_id)).fetchone()
        if row:
            con.execute(
                "INSERT INTO reply(outreach_id, received_at, from_email, kind, "
                "bounce_type, snippet) VALUES(?,?,?,?,?,?)",
                (row["id"], r["received_at"], r["from_email"], r["kind"],
                 r.get("bounce_type"), r.get("snippet")))

    for s in payload.get("suppression", []):
        con.execute("INSERT INTO suppression(email, company_id, reason, until, "
                    "created_at) VALUES(?,?,?,?,?)",
                    (s.get("email"), company_id(s.get("company")), s["reason"],
                     s.get("until"), s["created_at"]))
    for h in payload.get("health", []):
        con.execute("INSERT OR REPLACE INTO send_health(day, sent, hard_bounced, "
                    "soft_bounced, replied, paused, paused_reason) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (h["day"], h["sent"], h["hard_bounced"], h["soft_bounced"],
                     h["replied"], h.get("paused", 0), h.get("paused_reason")))
    con.commit()
    return {"contacts": len(payload.get("contacts", [])),
            "outreach": len(payload.get("outreach", [])),
            "replies": len(payload.get("replies", []))}


# ---- instructions from the page -------------------------------------------

CMD_KEY = "jobfeed:outreach:cmds"


def commands() -> list[dict]:
    """What the dashboard has asked for, oldest first."""
    flat = _redis(["HGETALL", CMD_KEY]) or []
    out = []
    for i in range(0, len(flat) - 1, 2):
        try:
            out.append({"id": flat[i], **json.loads(flat[i + 1])})
        except Exception:
            pass
    return sorted(out, key=lambda c: c.get("at", 0))


def done(command_id: str) -> None:
    """Applied, so it must not be applied again on the next pass."""
    _redis(["HDEL", CMD_KEY, command_id])
