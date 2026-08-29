"""The pipeline: applied -> recruiters -> drafts -> queue -> send -> watch.

Four passes, run independently on the same cron. They are separate so that one
failing does not block the others: a send that cannot reach Gmail must not stop
replies being read, because reading replies is what stops follow-ups going to
people who already answered.
"""
from __future__ import annotations

import datetime as dt
import json
import time

from .. import apply as _apply
from .. import db as _db
from . import apify, guards
from .gmail import classify, inbound_since, send as gmail_send
from .templates import render


# ---- 1. prepare -----------------------------------------------------------

def prepare(con, limit: int = 5, per_company: int = 3, dry_run: bool = True) -> dict:
    """For each company with new applications, find a recruiter and write one draft.

    Grouped by company, not by posting. Three roles at one employer are one
    note naming all three: drafting per posting produced three near-identical
    emails to the same three people, and the company cooldown then held eight
    of the nine forever, so two of the three applications got no outreach at
    all. One person, one note, every role named.

    Drafts, not sends. Nothing leaves until `dispatch` runs, so the whole
    research half can be watched working before anything is irreversible.
    """
    stats = {"jobs": 0, "companies": 0, "contacts": 0, "drafts": 0,
             "skipped": [], "verify": {}}
    rows = con.execute("""
        SELECT a.job_key, j.title, j.season, c.name company, c.id company_id
        FROM application a
        JOIN job j ON COALESCE(j.ats_key, j.url_key, j.canonical_url) = a.job_key
        LEFT JOIN company c ON c.id = j.company_id
        WHERE a.stage != 'interested'
          AND NOT EXISTS (SELECT 1 FROM outreach_job oj WHERE oj.job_key = a.job_key)
        ORDER BY a.updated_at""").fetchall()

    by_company: dict[object, list] = {}
    for row in rows:
        if not row["company"]:
            stats["skipped"].append(f"{row['job_key'][:40]}: no company")
            continue
        by_company.setdefault(row["company_id"], []).append(row)

    for cid, jobs in list(by_company.items())[:limit]:
        stats["companies"] += 1
        stats["jobs"] += len(jobs)
        company = jobs[0]["company"]

        found = _recruiters(con, company, cid, per_company)
        stats["contacts"] += len(found)
        if not found:
            stats["skipped"].append(f"{company}: no recruiters found")
            continue

        emails = [c["email"] for c in found
                  if c.get("email") and c.get("email_status", "unknown") == "unknown"]
        statuses = apify.verify(emails) if emails and not dry_run else {}
        for c in found:
            if c.get("email") and c["email"] in statuses:
                _set_status(con, c["id"], statuses[c["email"]])
                c["email_status"] = statuses[c["email"]]
            key = c.get("email_status", "unknown")
            stats["verify"][key] = stats["verify"].get(key, 0) + 1

        contact, why = _next_contact(con, found, cid)
        if contact is None:
            stats["skipped"].append(f"{company}: {why}")
            continue

        subject, body, variant = render(
            contact, {"company": company,
                      "roles": [j["title"] for j in jobs],
                      "season": (jobs[0]["season"] or "").split(",")[0].strip()})
        cur = con.execute(
            "INSERT OR IGNORE INTO outreach(job_key, contact_id, variant, "
            "subject, body, step, status, created_at) "
            "VALUES(?,?,?,?,?,0,'draft',?)",
            (jobs[0]["job_key"], contact["id"], variant, subject, body, time.time()))
        if not cur.lastrowid:
            continue
        # Record every application this note covers, or the ones it did not
        # name as primary look undrafted and get written again tomorrow.
        for job in jobs:
            con.execute("INSERT OR IGNORE INTO outreach_job(outreach_id, job_key) "
                        "VALUES(?,?)", (cur.lastrowid, job["job_key"]))
        stats["drafts"] += 1
    con.commit()
    return stats


def _next_contact(con, found: list[dict], company_id: int | None):
    """Who to write to at this company, or (None, why not).

    Someone nobody has written to yet, in preference to anyone who has already
    heard from us. This is what makes a second application to the same employer
    weeks later reach a different recruiter instead of writing to the first one
    twice -- and what stops the roster being spent on one application.
    """
    writable, blocked = [], []
    for c in found:
        why = _may_write(con, c, company_id)
        (blocked if why else writable).append((c, why))
    if not writable:
        return None, "; ".join(f"{c['full_name']}: {why}" for c, why in blocked) \
            or "no writable contact"

    def contacted_at(contact):
        row = con.execute(
            "SELECT MAX(sent_at) t FROM outreach WHERE contact_id=? "
            "AND sent_at IS NOT NULL", (contact["id"],)).fetchone()
        return row["t"] if row and row["t"] else 0.0

    fresh = [c for c, _ in writable if not contacted_at(c)]
    if fresh:
        return fresh[0], ""

    # Everyone here has already had a note. Writing again is defensible for a
    # genuinely new application months later, and is not for a second one the
    # same month.
    oldest = min((c for c, _ in writable), key=contacted_at)
    if time.time() - contacted_at(oldest) < guards.RECONTACT_DAYS * 86400:
        return None, ("every recruiter found has been contacted within "
                      f"{guards.RECONTACT_DAYS}d")
    return oldest, ""


def _recruiters(con, company: str, company_id: int | None, limit: int) -> list[dict]:
    """From cache when we have it, from Apify when we do not.

    Cached ninety days: you apply to the same firm more than once, and paying
    twice for the same three names is the least of it -- re-scraping also
    re-rolls which three you get, so the second application would contact
    different people at a company you have already written to.
    """
    have = con.execute(
        "SELECT * FROM contact WHERE company_id=? AND found_at > ? LIMIT ?",
        (company_id, time.time() - 90 * 86400, limit)).fetchall()
    if len(have) >= limit:
        return [dict(r) for r in have]

    try:
        people = apify.find_recruiters(company, limit)
    except Exception as exc:
        print(f"  apify: {company}: {exc}")
        return [dict(r) for r in have]

    for person in people:
        con.execute(
            "INSERT OR IGNORE INTO contact(company_id, full_name, first_name, "
            "title, linkedin_url, email, email_status, source, found_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (company_id, person["full_name"], person["first_name"], person["title"],
             person["linkedin_url"], person["email"],
             person.get("email_status") or "unknown", "apify", time.time()))
    con.commit()

    # Read the roster back rather than tracking ids through the insert loop.
    # cursor.lastrowid after an ignored INSERT OR IGNORE is not zero -- it is
    # whatever this connection inserted last, which for an already-known
    # recruiter is some other table's rowid. Trusting it attached drafts to
    # contact ids that did not exist.
    rows = con.execute(
        "SELECT * FROM contact WHERE company_id=? AND found_at > ? "
        "ORDER BY id LIMIT ?",
        (company_id, time.time() - 90 * 86400, limit)).fetchall()
    return [dict(r) for r in rows]


def _set_status(con, contact_id: int, status: str) -> None:
    con.execute("UPDATE contact SET email_status=?, verified_at=? WHERE id=?",
                (status, time.time(), contact_id))


def _may_write(con, contact: dict, company_id: int | None) -> str:
    email = contact.get("email")
    if not email:
        return "no email found"
    status = contact.get("email_status", "unknown")
    if status in ("invalid", "bounced"):
        return f"address is {status}"
    if status == "risky":
        return "address is risky"
    if status == "accept_all" and company_id is not None \
            and not guards.accept_all_allowed(con, company_id):
        return "accept_all quota for this company is spent"
    return guards.suppressed(con, email, company_id)


# ---- 2. schedule ----------------------------------------------------------

def schedule(con) -> dict:
    """Give every draft a send time, scattered across working days."""
    drafts = con.execute(
        "SELECT o.id, c.company_id FROM outreach o "
        "JOIN contact c ON c.id=o.contact_id "
        "WHERE o.status='draft' AND o.send_after IS NULL "
        "ORDER BY o.created_at").fetchall()
    if not drafts:
        return {"scheduled": 0}
    # One note per company per day. Interleaving the order is not enough on
    # its own: a day holds up to DAILY_MAX sends, so six drafts across two
    # companies still fit entirely inside day one however they are ordered,
    # and three strangers on one recruiting team compare three near-identical
    # emails over one lunch. So ask for enough slots to span as many days as
    # the busiest company needs, then fill them day-aware and drop the rest.
    per_company: dict[object, list] = {}
    for row in drafts:
        per_company.setdefault(row["company_id"], []).append(row["id"])
    days_needed = max(len(v) for v in per_company.values())
    times = guards.plan_sends(max(len(drafts), days_needed * guards.DAILY_MAX))

    pending = list(per_company.items())
    used: set[tuple] = set()
    assigned: list[tuple[int, float]] = []
    for when in times:
        if not pending:
            break
        day = dt.date.fromtimestamp(when - 5 * 3600)
        for i, (cid, ids) in enumerate(pending):
            if (cid, day) in used:
                continue
            assigned.append((ids.pop(0), when))
            used.add((cid, day))
            if not ids:
                pending.pop(i)
            break

    for oid, when in assigned:
        con.execute("UPDATE outreach SET status='queued', send_after=? WHERE id=?",
                    (when, oid))
    times = [w for _, w in assigned] or times[:1]
    con.commit()
    return {"scheduled": len(assigned),
            "first": dt.datetime.fromtimestamp(times[0]).isoformat(timespec="minutes"),
            "last": dt.datetime.fromtimestamp(times[-1]).isoformat(timespec="minutes")}


# ---- 3. dispatch ----------------------------------------------------------

def dispatch(con, dry_run: bool = True, limit: int = 10) -> dict:
    """Send whatever is due. Refuses entirely while the breaker is tripped."""
    h = guards.health(con)
    if h["paused"] or h["over_limit"]:
        if h["over_limit"] and not h["paused"]:
            guards.pause(con, f"bounce rate {h['bounce_rate']:.1%} over limit")
        return {"sent": 0, "paused": True, "health": h}

    due = con.execute(
        "SELECT o.*, c.email, c.full_name, c.email_status, c.company_id "
        "FROM outreach o JOIN contact c ON c.id=o.contact_id "
        "WHERE o.status='queued' AND o.send_after <= ? ORDER BY o.send_after LIMIT ?",
        (time.time(), limit)).fetchall()
    sent, errors, held = 0, [], []
    for row in due:
        # Re-checked here, not only at draft time. A draft can sit in the queue
        # for a week, and in that week the address may have bounced or the
        # company may have been written to for a different application -- both
        # of which happened after the only check the first version made.
        why = _may_write(con, dict(row), row["company_id"])
        if why:
            held.append(f"{row['email']}: {why}")
            con.execute("UPDATE outreach SET status='held' WHERE id=?", (row["id"],))
            continue
        if dry_run:
            sent += 1
            continue
        try:
            res = gmail_send(row["email"], row["subject"], row["body"],
                             thread_id=row["thread_id"] or None,
                             in_reply_to=row["message_id"] if row["step"] else None)
        except Exception as exc:
            errors.append(f"{row['email']}: {type(exc).__name__}: {exc}")
            continue
        con.execute(
            "UPDATE outreach SET status='sent', sent_at=?, message_id=?, "
            "thread_id=? WHERE id=?",
            (time.time(), res["message_id"], res["thread_id"], row["id"]))
        guards.bump(con, "sent")
        sent += 1
    con.commit()
    return {"sent": sent, "dry_run": dry_run, "errors": errors, "held": held,
            "paused": False}


# ---- 4. watch -------------------------------------------------------------

def watch(con) -> dict:
    """Read new mail, match it to what we sent, and act on it."""
    hid = _db.get_state(con, "outreach", "history_id")
    messages, new_hid = inbound_since(hid)
    _db.set_state(con, "outreach", "history_id", new_hid)

    counts = {"seen": len(messages), "human": 0, "auto": 0, "bounce": 0}
    for m in messages:
        row = _match(con, m)
        if row is None:
            continue
        kind, bounce_type = classify(m["headers"], m["snippet"])
        counts[kind] = counts.get(kind, 0) + 1
        con.execute(
            "INSERT INTO reply(outreach_id, received_at, from_email, kind, "
            "bounce_type, snippet) VALUES(?,?,?,?,?,?)",
            (row["id"], m["received_at"], m["from"], kind, bounce_type or None,
             m["snippet"][:300]))

        if kind == "bounce":
            guards.bump(con, "hard_bounced" if bounce_type == "hard" else "soft_bounced")
            if bounce_type == "hard":
                con.execute("UPDATE outreach SET status='bounced' WHERE job_key=? "
                            "AND contact_id=?", (row["job_key"], row["contact_id"]))
                con.execute("UPDATE contact SET email_status='bounced' WHERE id=?",
                            (row["contact_id"],))
                guards.suppress(con, row["email"], None, "hard bounce")
            else:
                con.execute("UPDATE outreach SET send_after=? WHERE id=?",
                            (time.time() + 2 * 86400, row["id"]))
        else:
            # Auto-replies stop the sequence too. A follow-up landing after
            # someone has already answered, even automatically, is the thing
            # that turns a polite note into a complaint.
            con.execute("UPDATE outreach SET status='replied' WHERE job_key=? "
                        "AND contact_id=? AND status IN ('sent','queued','draft')",
                        (row["job_key"], row["contact_id"]))
            if kind == "human":
                guards.bump(con, "replied")
    con.commit()

    h = guards.health(con)
    if h["over_limit"] and not h["paused"]:
        guards.pause(con, f"bounce rate {h['bounce_rate']:.1%} over limit")
        counts["paused"] = True
    return counts


def _match(con, message: dict):
    """Which outreach a message is answering.

    On the threading headers, never the subject: a recruiter who forwards it
    internally, or replies with a rewritten subject, still carries References.
    Falling back to the sender covers clients that strip them.
    """
    refs = " ".join(filter(None, (message["headers"].get("in-reply-to"),
                                  message["headers"].get("references"))))
    for mid in set(part.strip() for part in refs.replace(",", " ").split()
                   if part.strip().startswith("<")):
        row = con.execute(
            "SELECT o.*, c.email FROM outreach o JOIN contact c ON c.id=o.contact_id "
            "WHERE o.message_id=?", (mid,)).fetchone()
        if row:
            return row
    if message["thread_id"]:
        row = con.execute(
            "SELECT o.*, c.email FROM outreach o JOIN contact c ON c.id=o.contact_id "
            "WHERE o.thread_id=?", (message["thread_id"],)).fetchone()
        if row:
            return row
    return con.execute(
        "SELECT o.*, c.email FROM outreach o JOIN contact c ON c.id=o.contact_id "
        "WHERE LOWER(c.email)=? AND o.status='sent' ORDER BY o.sent_at DESC LIMIT 1",
        (message["from"],)).fetchone()


# ---- 5. follow-ups --------------------------------------------------------

def followups(con) -> dict:
    """Queue step 1 and 2 for anything sent, unanswered and old enough."""
    import random
    made = 0
    for step, days in ((1, 4), (2, 9)):
        rows = con.execute(
            "SELECT o.* FROM outreach o WHERE o.step=0 AND o.status='sent' "
            "AND o.sent_at < ? AND NOT EXISTS (SELECT 1 FROM outreach f "
            "  WHERE f.job_key=o.job_key AND f.contact_id=o.contact_id AND f.step=?) "
            "AND NOT EXISTS (SELECT 1 FROM outreach r WHERE r.job_key=o.job_key "
            "  AND r.contact_id=o.contact_id AND r.status IN ('replied','bounced'))",
            (time.time() - days * 86400, step)).fetchall()
        for row in rows:
            c = con.execute("SELECT * FROM contact WHERE id=?",
                            (row["contact_id"],)).fetchone()
            job = con.execute(
                "SELECT j.title, j.season, cm.name company FROM job j "
                "LEFT JOIN company cm ON cm.id=j.company_id "
                "WHERE COALESCE(j.ats_key,j.url_key,j.canonical_url)=?",
                (row["job_key"],)).fetchone()
            _, body, variant = render(
                dict(c), {"company": job["company"] if job else "",
                          "role": job["title"] if job else "",
                          "season": (job["season"] or "").split(",")[0] if job else ""},
                step=step)
            # The subject that was actually sent, not a re-render of it. They
            # agree today, but a re-render reads live profile and job rows: a
            # retitled posting or an edited draft would silently give the
            # follow-up a different subject, and a different subject is a new
            # thread -- which is the one thing a follow-up must not be.
            subject = row["subject"]
            # Scattered independently of when the first went out, and re-slotted
            # into a working window rather than firing at the original hour.
            # The wobble goes into plan_sends' start, not onto its result: added
            # afterwards it would walk the send back out of the window it was
            # just placed in, and onto a Saturday about two times in seven.
            when = guards.plan_sends(1, start=dt.datetime.now(dt.timezone.utc)
                                     + dt.timedelta(hours=random.uniform(2, 30)))[0]
            con.execute(
                "INSERT OR IGNORE INTO outreach(job_key, contact_id, variant, "
                "subject, body, step, status, send_after, message_id, thread_id, "
                "created_at) VALUES(?,?,?,?,?,?,'queued',?,?,?,?)",
                (row["job_key"], row["contact_id"], variant, subject, body, step,
                 when, row["message_id"], row["thread_id"], time.time()))
            made += 1
    con.commit()
    return {"queued": made}
