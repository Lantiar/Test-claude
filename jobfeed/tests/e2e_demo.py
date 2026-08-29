"""End-to-end walk over the real feed, on a simulated calendar.

Real database, real Simplify postings, real pipeline code. Two things are
stubbed and only two: the Apify recruiter search (it costs money and returns
different people each run) and the Gmail network hop (nothing may actually be
sent). Everything between them -- grouping, campaigns, cooldowns, scheduling,
the copy editor, reply classification, matching, suppression, the circuit
breaker -- is the code that would run in production.

    python jobfeed/tests/e2e_demo.py
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from jobfeed import db
from jobfeed.outreach import apify, guards, run as _run

PASS, FAIL = [], []
SENT = []          # (day, to, subject, message_id, thread_id)
DAY = [0]


def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print(f"    {'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))


def banner(text):
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


# ---- stubs ----------------------------------------------------------------

def fake_recruiters(company, n=3):
    slug = "".join(ch for ch in company.lower() if ch.isalnum())[:12]
    return [{"full_name": f"Recruiter {slug}{i}", "first_name": f"Sam{i}",
             "title": "University Recruiter", "linkedin_url": "",
             "email": f"r{i}@{slug}.example", "email_status": "verified"}
            for i in range(1, n + 1)]


def fake_send(to, subject, body, thread_id=None, in_reply_to=None):
    n = len(SENT) + 1
    mid, tid = f"<sent{n}@mail.example>", thread_id or f"T{n}"
    SENT.append((DAY[0], to, subject, mid, tid))
    return {"id": f"g{n}", "message_id": mid, "thread_id": tid}


def inbound(kind, to_message_id, thread_id, sender):
    """A message shaped exactly as gmail.inbound_since returns them."""
    base = {"id": f"in{to_message_id}", "thread_id": thread_id,
            "from": sender, "received_at": time.time()}
    if kind == "human":
        return {**base, "snippet": "Thanks for reaching out -- happy to take a look.",
                "headers": {"from": f"Someone <{sender}>", "subject": "Re: your note",
                            "in-reply-to": to_message_id, "references": to_message_id}}
    if kind == "auto":
        return {**base, "snippet": "I am out of the office until Monday.",
                "headers": {"from": sender, "subject": "Automatic reply: your note",
                            "auto-submitted": "auto-replied",
                            "in-reply-to": to_message_id}}
    return {**base, "from": "mailer-daemon@googlemail.com",
            "snippet": "550 5.1.1 The email account that you tried to reach does "
                       "not exist.",
            "headers": {"from": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                        "subject": "Delivery Status Notification (Failure)",
                        "return-path": "<>", "in-reply-to": to_message_id}}


# ---- calendar -------------------------------------------------------------

def tick(con, days=1):
    """Move every stored timestamp back, which moves 'now' forward."""
    for _ in range(days):
        DAY[0] += 1
        con.execute("UPDATE outreach SET sent_at=sent_at-86400 WHERE sent_at IS NOT NULL")
        con.execute("UPDATE outreach SET send_after=send_after-86400 "
                    "WHERE status='queued'")
        con.execute("UPDATE outreach SET created_at=created_at-86400")
        con.execute("UPDATE contact SET found_at=found_at-86400")
        con.execute("UPDATE reply SET received_at=received_at-86400")
        con.commit()
        _run.dispatch(con, dry_run=False, limit=20)


def apply_to(con, company, n, titles=None):
    rows = con.execute(
        "SELECT COALESCE(j.ats_key,j.url_key,j.canonical_url) k, j.title "
        "FROM job j JOIN company c ON c.id=j.company_id "
        "WHERE c.name=? AND j.status='open' AND j.title LIKE '%Intern%' "
        "ORDER BY j.posted_at DESC LIMIT ?", (company, n)).fetchall()
    for r in rows:
        con.execute("INSERT OR IGNORE INTO application(job_key,stage,updated_at) "
                    "VALUES(?,'applied',?)", (r["k"], time.time()))
    con.commit()
    if titles is not None:
        titles += [r["title"] for r in rows]
    return [r["title"] for r in rows]


def main():
    src = db.DEFAULT_PATH
    tmp = tempfile.mktemp(suffix=".sqlite3")
    shutil.copy(src, tmp)
    con = db.connect(tmp)
    for t in ("outreach", "outreach_job", "contact", "reply", "suppression",
              "send_health", "application"):
        con.execute(f"DELETE FROM {t}")
    con.commit()
    apify.find_recruiters = fake_recruiters
    _run.gmail_send = fake_send
    total = con.execute("SELECT COUNT(*) c FROM job WHERE status='open'").fetchone()["c"]
    print(f"real feed: {total} open postings, copied to a scratch database")

    # ---------------------------------------------------------------- 1
    banner("1. One posting -> three recruiters, one per day")
    titles = apply_to(con, "AMD", 1)
    print(f"    applied: {titles[0]}")
    d = _run.prepare(con, limit=5, per_company=3)
    check("three drafts from one application", d["drafts"] == 3, str(d["drafts"]))
    rows = con.execute("SELECT DISTINCT campaign FROM outreach").fetchall()
    check("all three share one campaign", len(rows) == 1)
    _run.schedule(con)
    tick(con, 8)
    amd = [s for s in SENT if "amd" in s[1]]
    check("all three sent", len(amd) == 3, f"{len(amd)} sent")
    days = [s[0] for s in amd]
    check("no two on the same day", len(set(days)) == 3, f"days {days}")
    check("nothing held by its own batch",
          not con.execute("SELECT 1 FROM outreach WHERE status='held'").fetchone())

    # ---------------------------------------------------------------- 2
    banner("2. Three postings at one company on one day -> one note each, "
           "every role named")
    tick(con, 9)                                    # clear AMD's cooldown
    titles = apply_to(con, "Tesla", 3)
    for t in titles:
        print(f"    applied: {t}")
    d = _run.prepare(con, limit=5, per_company=3)
    check("three drafts, not nine", d["drafts"] == 3, str(d["drafts"]))
    body = con.execute("SELECT body FROM outreach o JOIN contact c ON c.id=o.contact_id "
                       "WHERE c.email LIKE '%tesla%' LIMIT 1").fetchone()["body"]
    named = [t for t in titles if t.split(" - ")[0] in body]
    check("every role named in the one note", len(named) == 3, f"{len(named)}/3")
    covered = con.execute("SELECT COUNT(*) c FROM outreach_job").fetchone()["c"]
    check("every application recorded as covered", covered == 3 + 3 * 3,
          f"{covered} links")
    check("re-running prepare writes nothing new",
          _run.prepare(con, limit=5)["drafts"] == 0)

    # ---------------------------------------------------------------- 3
    banner("3. The copy editor runs on the drafts")
    p = _run.polish_drafts(con, limit=3)
    print(f"    {p['seen']} seen, {p['edited']} edited, {p['unchanged']} unchanged, "
          f"{p['rejected']} refused, ${p['cost']:.5f}")
    for problem in p["problems"][:1]:
        print(f"    reason: {problem[:110]}")
    unchanged = con.execute(
        "SELECT COUNT(*) c FROM outreach WHERE subject NOT LIKE '%[%'").fetchone()["c"]
    check("no draft was damaged by the editor", unchanged == 0)

    _run.schedule(con)
    tick(con, 8)
    tesla = [s for s in SENT if "tesla" in s[1]]
    check("three Tesla notes sent", len(tesla) == 3, f"{len(tesla)}")

    # ---------------------------------------------------------------- 4
    banner("4. A human reply stops the sequence")
    target = tesla[0]
    _run.inbound_since = lambda hid: (
        [inbound("human", target[3], target[4], target[1])], "h1")
    counts = _run.watch(con)
    check("classified as a human reply", counts.get("human") == 1, str(counts))
    row = con.execute("SELECT o.status FROM outreach o JOIN contact c ON c.id=o.contact_id "
                      "WHERE c.email=?", (target[1],)).fetchone()
    check("that thread marked replied", row["status"] == "replied", row["status"])

    tick(con, 6)
    made = _run.followups(con)
    who = [r["email"] for r in con.execute(
        "SELECT c.email FROM outreach o JOIN contact c ON c.id=o.contact_id "
        "WHERE o.step=1")]
    check("follow-up queued for the others", made["queued"] >= 1, str(made))
    check("no follow-up to the person who replied", target[1] not in who,
          f"queued for {who}")
    # Step 2 must wait on step 1 having gone out, not on the original being old
    # enough. Keyed off the original, both queued in the same pass -- so
    # "following up again" was written before the first follow-up was sent.
    step2 = con.execute("SELECT COUNT(*) c FROM outreach WHERE step=2").fetchone()["c"]
    check("no second nudge before the first has been sent", step2 == 0, str(step2))
    tick(con, 7)
    _run.followups(con)
    _run.schedule(con)
    both = con.execute(
        "SELECT COUNT(*) c FROM outreach f WHERE f.step=2 AND NOT EXISTS "
        "(SELECT 1 FROM outreach p WHERE p.contact_id=f.contact_id AND p.step=1 "
        " AND p.status='sent')").fetchone()["c"]
    check("every second nudge follows a sent first one", both == 0, str(both))
    # Follow-ups scheduled themselves one at a time and skipped the spacing:
    # five landed on one day, three of them at the same company.
    perday = {}
    for r in con.execute(
            "SELECT c.company_id, o.send_after FROM outreach o "
            "JOIN contact c ON c.id=o.contact_id WHERE o.step>0 AND o.send_after"):
        key = (r["company_id"], dt.date.fromtimestamp(r["send_after"] - 5 * 3600))
        perday[key] = perday.get(key, 0) + 1
    check("no company gets two nudges on one day",
          not perday or max(perday.values()) == 1, str(perday))

    # ---------------------------------------------------------------- 5
    banner("5. A hard bounce suppresses the address")
    bounced = tesla[1]
    _run.inbound_since = lambda hid: (
        [inbound("bounce", bounced[3], bounced[4], bounced[1])], "h2")
    counts = _run.watch(con)
    check("classified as a bounce", counts.get("bounce") == 1, str(counts))
    st = con.execute("SELECT email_status FROM contact WHERE email=?",
                     (bounced[1],)).fetchone()["email_status"]
    check("contact marked bounced", st == "bounced", st)
    check("address suppressed", bool(guards.suppressed(con, bounced[1], None)))
    check("hard bounce counted by the breaker",
          guards.health(con)["hard_bounced"] >= 1)

    # ---------------------------------------------------------------- 6
    banner("6. An out-of-office also stops the sequence, without counting as "
           "interest")
    auto = tesla[2]
    before = guards.health(con)["replied"]
    _run.inbound_since = lambda hid: (
        [inbound("auto", auto[3], auto[4], auto[1])], "h3")
    counts = _run.watch(con)
    check("classified as automatic", counts.get("auto") == 1, str(counts))
    check("not counted as a reply", guards.health(con)["replied"] == before)
    row = con.execute("SELECT o.status FROM outreach o JOIN contact c ON c.id=o.contact_id "
                      "WHERE c.email=? AND o.step=0", (auto[1],)).fetchone()
    check("sequence stopped anyway", row["status"] == "replied", row["status"])

    # ---------------------------------------------------------------- 7
    banner("7. A later application reaches recruiters who have not heard from you")
    tick(con, 10)
    apply_to(con, "Tesla", 4)
    _run.prepare(con, limit=5, per_company=3)
    fresh = {r["email"] for r in con.execute(
        "SELECT c.email FROM outreach o JOIN contact c ON c.id=o.contact_id "
        "WHERE o.status='draft'")}
    already = {s[1] for s in tesla}
    check("nobody written to twice", not (fresh & already),
          f"overlap {fresh & already}")
    check("a fresh batch was found", len(fresh) >= 1, f"{sorted(fresh)}")

    # ---------------------------------------------------------------- 8
    banner("8. The circuit breaker trips and stops everything")
    for company in ("RTX", "AMD", "Tesla", "American Express", "ByteDance"):
        guards.bump(con, "sent", n=6)
    guards.bump(con, "hard_bounced", n=3)
    h = guards.health(con)
    print(f"    {h['sent']} sent, {h['hard_bounced']} hard bounces "
          f"= {h['bounce_rate']:.1%} over {guards.BOUNCE_WINDOW_DAYS}d "
          f"(limit {guards.BOUNCE_LIMIT:.0%})")
    check("breaker reports over limit", h["over_limit"], str(h))
    con.execute("UPDATE outreach SET status='queued', send_after=? "
                "WHERE status='draft'", (time.time() - 60,))
    con.commit()
    out = _run.dispatch(con, dry_run=False, limit=20)
    check("dispatch refuses entirely while tripped",
          out["sent"] == 0 and out.get("paused"), str(out))
    check("and it stays paused", guards.health(con)["paused"])

    # ---------------------------------------------------------------- summary
    banner("summary")
    print(f"    {len(SENT)} emails sent over {DAY[0]} simulated days, "
          f"{len({s[1] for s in SENT})} distinct recipients")
    for day, to, subject, _, _ in SENT:
        print(f"      day {day:>3}  {to:<26} {subject[:46]}")
    orphan = con.execute(
        "SELECT COUNT(*) c FROM application a WHERE NOT EXISTS "
        "(SELECT 1 FROM outreach_job oj WHERE oj.job_key=a.job_key)").fetchone()["c"]
    print(f"\n    applications with no outreach: {orphan}")
    print(f"\n    {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"      FAILED  {f}")
    os.unlink(tmp)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
