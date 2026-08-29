"""The pipeline: applied -> recruiters -> drafts -> queue -> send -> watch.

Four passes, run independently on the same cron. They are separate so that one
failing does not block the others: a send that cannot reach Gmail must not stop
replies being read, because reading replies is what stops follow-ups going to
people who already answered.
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import json
import time

from .. import apply as _apply
from .. import db as _db
from . import apify, board as _board, guards, polish as _polish, profile as _profile, titles as _titles
from .gmail import classify, inbound_since, send as gmail_send
from .templates import render


# ---- 1. prepare -----------------------------------------------------------

def prepare(con, limit: int = 5, per_company: int = 3, dry_run: bool = False,
            only: list[str] | None = None) -> dict:
    """For each company with new applications, find a recruiter and write one draft.

    Grouped by company, not by posting. Three roles at one employer are one
    note naming all three -- drafting per posting produced three near-identical
    emails to the same three people. But a company still gets up to
    `per_company` recruiters, written one per day: contacting three people
    about one application was the point, and the failure was only ever three
    people hearing about three postings separately.

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

    # Scoped when the caller names the jobs. A button press is consent for one
    # job, and drafting for everything else marked applied would turn one
    # click into mail nobody asked to send.
    if only is not None:
        wanted = set(only)
        rows = [r for r in rows if r["job_key"] in wanted]

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

        why = guards.campaign_allowed(con, cid)
        if why:
            stats["skipped"].append(f"{company}: {why}")
            continue

        # One batch at one company: the sends inside it do not count against
        # each other's cooldown, and the next batch waits on the last of them.
        campaign = f"{cid}-{int(time.time())}"
        roles, dropped, meta = _clean_roles(jobs, dry_run)
        stats["cost"] = stats.get("cost", 0.0) + meta["cost"]
        season = (jobs[0]["season"] or "").split(",")[0].strip()
        if len(meta["seasons"]) > 1:
            season = ""          # the postings disagree; name none of them
        for note in dropped:
            stats["skipped"].append(f"{company}: {note}")
        if not roles:
            stats["skipped"].append(
                f"{company}: no usable role title, nothing drafted")
            continue

        contacts, why = _pick_contacts(con, found, cid, per_company, campaign)
        if not contacts:
            stats["skipped"].append(f"{company}: {why}")
            continue

        for contact in contacts:
            subject, body, variant = render(
                contact, {"company": company, "roles": roles, "season": season})
            cur = con.execute(
                "INSERT OR IGNORE INTO outreach(job_key, contact_id, variant, "
                "subject, body, step, status, campaign, created_at) "
                "VALUES(?,?,?,?,?,0,'draft',?,?)",
                (jobs[0]["job_key"], contact["id"], variant, subject, body,
                 campaign, time.time()))
            if not cur.rowcount:
                continue
            # Record every application this note covers, or the ones it did
            # not name as primary look undrafted and get written again
            # tomorrow.
            for job in jobs:
                con.execute("INSERT OR IGNORE INTO outreach_job(outreach_id, "
                            "job_key) VALUES(?,?)", (cur.lastrowid, job["job_key"]))
            stats["drafts"] += 1
    con.commit()
    return stats


def _clean_roles(jobs, dry_run: bool) -> tuple[list[str], list[str], dict]:
    """Titles fit to put in an email, plus what was left out and why.

    A title the cleaner cannot rescue is dropped from the list rather than
    printed as it stands -- unless it is the only one, in which case there is
    no note to send and the whole draft waits for a human. Silently emailing
    a title that reads as machine output is the outcome worth avoiding; doing
    it because the alternative was awkward is not a reason.
    """
    from .templates import season_in

    # Read the seasons off the ORIGINAL titles, before trimming removes them.
    # Trimming is what makes two postings that disagree -- Tesla listed a
    # Summer 2027 and a Spring 2027 side by side -- look like they agree, and
    # the note then asserts one season and contradicts itself in the list.
    carried = {season_in(job["title"]) for job in jobs}
    carried.discard("")

    roles, dropped, spent = [], [], 0.0
    for job in jobs:
        title = job["title"]
        if dry_run:
            roles.append(title)
            continue
        out = _titles.clean(title)
        spent += out["cost"]
        if out["rejected"]:
            dropped.append(f"{title[:48]!r} left out -- {out['rejected'][:60]}")
            continue
        roles.append(out["title"])
    return roles, dropped, {"cost": spent, "seasons": carried}


def _resume(step: int) -> list[str]:
    """The resume, on the first note only.

    Attached to a follow-up as well, the same PDF arrives twice in one thread
    -- which reads as a script that forgot what it had already sent. Missing
    or unset, the mail goes without it: an attachment that cannot be found is
    not a reason to hold a note whose text already links to the portfolio.
    """
    if step or not _profile.ATTACH_RESUME[0]:
        return []
    path = os.getenv("RESUME_PATH", "config/files/resume.pdf")
    return [path] if path and os.path.exists(path) else []


def _still_applied(con, row) -> bool:
    """Is the application this note refers to still on record?

    A draft can outlive its reason. Un-marking a job, or removing an
    application added by mistake, leaves the outreach rows queued and pointing
    at nothing -- and they still send, telling a recruiter you applied to
    something the tracker no longer says you applied to. Nothing else in the
    pipeline notices, because every other check is about the recipient.
    """
    keys = [r["job_key"] for r in con.execute(
        "SELECT job_key FROM outreach_job WHERE outreach_id=?", (row["id"],))]
    keys = keys or [row["job_key"]]        # rows written before outreach_job existed
    placeholders = ",".join("?" * len(keys))
    row = con.execute(
        f"SELECT COUNT(*) c FROM application WHERE job_key IN ({placeholders}) "
        f"AND stage != 'interested'", keys).fetchone()
    return bool(row and row["c"])


def _pick_contacts(con, found: list[dict], company_id: int | None,
                   want: int, campaign: str) -> tuple[list[dict], str]:
    """Up to `want` recruiters at this company, or ([], why not).

    People nobody has written to first. This is what makes a second
    application to the same employer weeks later reach different recruiters
    instead of writing to the first ones twice -- and what stops one
    application spending the whole roster.
    """
    writable, blocked = [], []
    for contact in found:
        why = _may_write(con, contact, company_id, campaign)
        (blocked if why else writable).append((contact, why))
    if not writable:
        return [], "; ".join(f"{c['full_name']}: {why}" for c, why in blocked) \
            or "no writable contact"

    def contacted_at(contact):
        row = con.execute(
            "SELECT MAX(sent_at) t FROM outreach WHERE contact_id=? "
            "AND sent_at IS NOT NULL", (contact["id"],)).fetchone()
        return row["t"] if row and row["t"] else 0.0

    fresh = [c for c, _ in writable if not contacted_at(c)]
    if len(fresh) >= want:
        return fresh[:want], ""

    # Topping up with people who have already had a note is defensible for a
    # genuinely new application months later, and is not for a second one the
    # same month.
    cutoff = time.time() - guards.RECONTACT_DAYS * 86400
    stale = sorted((c for c, _ in writable
                    if 0 < contacted_at(c) <= cutoff), key=contacted_at)
    picked = fresh + stale[:want - len(fresh)]
    if not picked:
        return [], ("every recruiter found has been contacted within "
                    f"{guards.RECONTACT_DAYS}d")
    return picked, ""


def _recruiters(con, company: str, company_id: int | None, limit: int) -> list[dict]:
    """Recruiters at this company who have not heard from us, cache first.

    Cached ninety days: applying to the same firm twice and paying twice for
    the same names is the least of it -- re-scraping also re-rolls *which*
    people you get, so the second application would contact a different set of
    strangers at a company you have already written to.

    Ordered by who has been written to least, and topped up from Apify when
    the untouched ones run short. Without the top-up a roster of three is
    spent by the first application and every later one finds nobody new; the
    ask is deliberately larger than the shortfall, since the search returns
    the same faces first and only a wider pool contains different ones.
    """
    def roster():
        return [dict(r) for r in con.execute(
            "SELECT c.* FROM contact c WHERE c.company_id=? AND c.found_at > ? "
            "ORDER BY (SELECT COUNT(*) FROM outreach o WHERE o.contact_id=c.id "
            "          AND o.sent_at IS NOT NULL) ASC, c.id",
            (company_id, time.time() - 90 * 86400)).fetchall()]

    have = roster()
    untouched = [c for c in have if not con.execute(
        "SELECT 1 FROM outreach WHERE contact_id=? AND sent_at IS NOT NULL",
        (c["id"],)).fetchone()]
    if len(untouched) >= limit:
        return untouched[:limit]

    try:
        people = apify.find_recruiters(company, len(have) + limit)
    except Exception as exc:
        print(f"  apify: {company}: {exc}")
        return (untouched or have)[:limit]

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
    return roster()[:limit]


def _set_status(con, contact_id: int, status: str) -> None:
    con.execute("UPDATE contact SET email_status=?, verified_at=? WHERE id=?",
                (status, time.time(), contact_id))


def _may_write(con, contact: dict, company_id: int | None,
               campaign: str | None = None, followup: bool = False) -> str:
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
    return guards.suppressed(con, email, company_id, campaign, followup)


# ---- 1b. polish -----------------------------------------------------------

def polish_drafts(con, limit: int = 20) -> dict:
    """Run the copy editor over unpolished drafts.

    Between drafting and reading, so what you review is what would be sent.
    A rejected revision is not a failure of the draft -- the original is
    already tested and reviewable -- so it is recorded and the draft stays
    exactly as the templates wrote it.
    """
    rows = con.execute(
        "SELECT o.*, c.first_name, cm.name company FROM outreach o "
        "JOIN contact c ON c.id=o.contact_id "
        "LEFT JOIN company cm ON cm.id=c.company_id "
        "WHERE o.status='draft' AND o.polished_at IS NULL "
        "ORDER BY o.created_at LIMIT ?", (limit,)).fetchall()
    stats = {"seen": len(rows), "edited": 0, "unchanged": 0, "rejected": 0,
             "retried": 0, "cost": 0.0, "notes": [], "problems": []}
    for row in rows:
        roles = [r["title"] for r in con.execute(
            "SELECT j.title FROM outreach_job oj JOIN job j ON "
            "COALESCE(j.ats_key,j.url_key,j.canonical_url)=oj.job_key "
            "WHERE oj.outreach_id=?", (row["id"],))]
        out = _polish.polish(row["subject"], row["body"],
                             {"company": row["company"], "roles": roles,
                              "first_name": row["first_name"]})
        stats["cost"] += out["cost"]
        stats["retried"] += bool(out.get("retried"))
        if out["rejected"]:
            stats["rejected"] += 1
            stats["problems"].append(f"[{row['id']}] " + "; ".join(out["rejected"][:3]))
            continue
        con.execute(
            "UPDATE outreach SET subject=?, body=?, polished_at=?, polish_notes=? "
            "WHERE id=?",
            (out["subject"], out["body"], time.time(),
             json.dumps(out["notes"]) if out["notes"] else None, row["id"]))
        if out["changed"]:
            stats["edited"] += 1
            stats["notes"] += [f"[{row['id']}] {n}" for n in out["notes"]]
        else:
            stats["unchanged"] += 1
    con.commit()
    return stats


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
        why = _may_write(con, dict(row), row["company_id"], row["campaign"],
                         followup=bool(row["step"]))
        if not why and not _still_applied(con, row):
            why = "the application it refers to is no longer on record"
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
                             in_reply_to=row["message_id"] if row["step"] else None,
                             attachments=_resume(row["step"]))
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


# ---- the board -------------------------------------------------------------

def serve_board(con, send: bool = False, per_company: int = 3) -> dict:
    """Act on what the web tracker has asked for, and report back what happened.

    One pass: mirror the stages the tracker holds, draft for the jobs whose
    button was pressed, polish, schedule, send what is due, read replies, and
    write each job's state back so the page shows the outcome rather than the
    intention.

    Scoped to the pressed keys throughout. Everything else marked applied is
    left alone -- the button is the consent, and one press must not become mail
    to every company on the board.
    """
    out = {"queued": 0, "drafted": 0, "sent": 0, "replied": 0, "problems": []}
    if not _board.available():
        out["problems"].append("no Upstash credentials; the board is unreadable")
        return out

    # Settings the dashboard has changed -- graduation date, portfolio, the
    # three achievements, whether the resume goes -- applied before anything
    # is rendered. Editing profile.py for these would mean a commit and a
    # deploy to change one sentence of a letter.
    try:
        _profile.apply(_board.profile())
        stored = _board.resume(tempfile.gettempdir())
        if stored:
            os.environ["RESUME_PATH"] = stored
    except Exception as exc:
        out["problems"].append(f"could not read the settings: {exc}")

    # Contacts, drafts and send times, from the last run. Without this the
    # database the runner just seeded knows nothing about outreach: the
    # recruiter search is paid for again, the batch is scheduled two days out
    # again, and no draft ever reaches its send time.
    try:
        out["restored"] = _board.load(con)
    except Exception as exc:
        out["problems"].append(f"could not read the outreach store: {exc}")
        return out

    # The tracker is the record of what has been applied to; the runner starts
    # from a published snapshot and holds none of it.
    for job_key, stage in _board.stages().items():
        con.execute(
            "INSERT INTO application(job_key, stage, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(job_key) DO UPDATE SET stage=excluded.stage, "
            "updated_at=excluded.updated_at",
            (job_key, stage, time.time()))
    con.commit()

    out["applied"] = _apply_commands(con)

    asked = _board.queued()
    out["queued"] = len(asked)
    for job_key in asked:
        try:
            stats = prepare(con, limit=1, per_company=per_company, only=[job_key])
            if stats["drafts"]:
                out["drafted"] += stats["drafts"]
            else:
                why = "; ".join(stats["skipped"][:2]) or "nothing to draft"
                _board.write(job_key, "held", note=why)
                out["problems"].append(f"{job_key[:40]}: {why}")
        except Exception as exc:
            _board.write(job_key, "failed", note=f"{type(exc).__name__}: {exc}")
            out["problems"].append(f"{job_key[:40]}: {exc}")

    if out["drafted"]:
        polish_drafts(con)
        schedule(con)

    result = dispatch(con, dry_run=not send, limit=10)
    out["sent"] = result.get("sent", 0)
    out["problems"] += result.get("errors", []) + result.get("held", [])

    try:
        out["replied"] = watch(con).get("human", 0)
    except Exception as exc:
        out["problems"].append(f"watch: {type(exc).__name__}: {exc}")

    # Everything still in flight, not only what was asked for on this pass.
    # Reporting on `asked` alone meant a job stopped being watched the moment
    # it left the queue -- so it could reach "reached out" and never move to
    # "replied", and the reply would exist in the database and nowhere the
    # page could see it.
    live = [key for key, record in _board.read().items()
            if record.get("state") in ("queued", "reached")]
    _report(con, sorted(set(asked) | set(live)))

    # Written last, and unconditionally: a pass that failed halfway still
    # learned something -- an address that bounced, a reply that arrived --
    # and throwing that away means discovering it again next run.
    try:
        out["saved"] = _board.save(con)
    except Exception as exc:
        out["problems"].append(f"could not write the outreach store: {exc}")
    return out


def _apply_commands(con) -> list[str]:
    """Carry out what the dashboard asked for: cancel, move, send now, retry.

    Applied here rather than in the page, because the database they act on is
    rebuilt on the runner every pass -- a browser writing to it directly would
    be writing to something that no longer exists by the time it matters.

    Each is removed only after it has been applied, so a pass that dies
    halfway leaves the instruction to be carried out next time rather than
    losing it.
    """
    done = []
    for cmd in _board.commands():
        try:
            where = ["c.email = ?"]
            params: list = [cmd.get("email", "")]
            if cmd.get("job_key"):
                where.append("o.job_key = ?")
                params.append(cmd["job_key"])
            if cmd.get("step") is not None:
                where.append("o.step = ?")
                params.append(int(cmd["step"]))
            rows = con.execute(
                "SELECT o.id, o.status FROM outreach o "
                "JOIN contact c ON c.id = o.contact_id "
                f"WHERE {' AND '.join(where)}", params).fetchall()

            for row in rows:
                action = cmd.get("action")
                if action == "cancel" and row["status"] in ("draft", "queued", "held"):
                    # Kept as a row rather than deleted: it is a record that
                    # this person was going to be written to and deliberately
                    # was not, and prepare would draft it again from nothing.
                    con.execute("UPDATE outreach SET status='cancelled' WHERE id=?",
                                (row["id"],))
                elif action == "reschedule" and row["status"] in ("draft", "queued", "held"):
                    con.execute(
                        "UPDATE outreach SET status='queued', send_after=? WHERE id=?",
                        (float(cmd["when"]), row["id"]))
                elif action == "send_now" and row["status"] in ("draft", "queued", "held"):
                    con.execute(
                        "UPDATE outreach SET status='queued', send_after=? WHERE id=?",
                        (time.time() - 60, row["id"]))
                elif action == "edit" and row["status"] in ("draft", "queued", "held"):
                    # Marked polished so the copy editor leaves it alone. Its
                    # job is to tidy generated text; this text was written by
                    # the person sending it, and "improving" that would undo
                    # the edit on the next pass.
                    con.execute(
                        "UPDATE outreach SET subject=?, body=?, polished_at=?, "
                        "polish_notes='edited by hand' WHERE id=?",
                        (cmd["subject"], cmd["body"], time.time(), row["id"]))
                elif action == "retry" and row["status"] in ("held", "cancelled", "failed"):
                    con.execute("UPDATE outreach SET status='queued' WHERE id=?",
                                (row["id"],))
            con.commit()
            _board.done(cmd["id"])
            done.append(f"{cmd.get('action')} {cmd.get('email','')}")
        except Exception as exc:
            print(f"  command {cmd.get('id')}: {type(exc).__name__}: {exc}")
    return done


def _report(con, keys: list[str]) -> None:
    """Write each asked-for job's real state back to the board.

    Reads the outreach rows rather than trusting what this pass did, so a job
    whose mail went out on an earlier run is still reported correctly.
    """
    for job_key in keys:
        rows = con.execute(
            "SELECT o.status, o.thread_id, o.sent_at FROM outreach o "
            "JOIN outreach_job oj ON oj.outreach_id = o.id "
            "WHERE oj.job_key = ? AND o.step = 0", (job_key,)).fetchall()
        if not rows:
            continue
        thread = next((r["thread_id"] for r in rows if r["thread_id"]), "")
        sent = sum(1 for r in rows if r["sent_at"])
        if any(r["status"] == "replied" for r in rows):
            _board.write(job_key, "replied", thread=thread, sent=sent)
        elif sent:
            _board.write(job_key, "reached", thread=thread, sent=sent)
        # still queued otherwise: drafted but not yet due, which the page
        # already shows as queued.


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
    """Queue the next nudge for anything sent, unanswered and old enough.

    Each step waits on the step before it, not on the original. Keyed off the
    original, both queued in the same pass the moment it was nine days old --
    so "following up again" was written before the first follow-up had been
    sent, and the two would land together.
    """
    made = 0
    for step, days in ((1, 4), (2, 5)):
        rows = con.execute(
            "SELECT o.* FROM outreach o WHERE o.step=? AND o.status='sent' "
            "AND o.sent_at < ? AND NOT EXISTS (SELECT 1 FROM outreach f "
            "  WHERE f.job_key=o.job_key AND f.contact_id=o.contact_id AND f.step=?) "
            "AND NOT EXISTS (SELECT 1 FROM outreach r WHERE r.job_key=o.job_key "
            "  AND r.contact_id=o.contact_id AND r.status IN ('replied','bounced'))",
            (step - 1, time.time() - days * 86400, step)).fetchall()
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
            # Left for schedule() to place, rather than given a time here.
            # Scheduling each one on its own put five follow-ups on one day,
            # three of them at the same company -- the very burst the first
            # sends are spaced to avoid, arriving through the one path that
            # was not going through the spacing.
            con.execute(
                "INSERT OR IGNORE INTO outreach(job_key, contact_id, variant, "
                "subject, body, step, status, message_id, thread_id, "
                "campaign, created_at) VALUES(?,?,?,?,?,?,'draft',?,?,?,?)",
                (row["job_key"], row["contact_id"], variant, subject, body, step,
                 row["message_id"], row["thread_id"], row["campaign"],
                 time.time()))
            made += 1
    con.commit()
    return {"queued": made}
