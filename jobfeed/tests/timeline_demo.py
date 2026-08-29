"""Sixty simulated days: apply to Microsoft jobs on given days, run the
pipeline once a day as the scheduler would, and print every send."""
import sys, time, datetime as dt, tempfile; sys.path.insert(0, ".")
from jobfeed import db
from jobfeed.outreach import run as _run, apify

con = db.connect(tempfile.mktemp(suffix=".sqlite3")); now = time.time()
con.execute("INSERT INTO company(name,norm,created_at) VALUES('Microsoft','microsoft',?)", (now,))
con.commit()
apify.find_recruiters = lambda co, n=3: [
    {"full_name": f"Rec {i}", "first_name": f"R{i}", "title": "University Recruiter",
     "linkedin_url": "", "email": f"rec{i}@microsoft.com", "email_status": "verified"}
    for i in range(1, n + 1)]

log = []
_run.gmail_send = lambda to, s, b, **k: (
    log.append((DAY[0], to, s)),
    {"message_id": f"<{len(log)}@x>", "thread_id": f"T{len(log)}"})[1]

APPLY = {0:  [(1, "Software Engineer Intern"),
              (2, "SWE Intern - Azure Networking"),
              (3, "Data Scientist Intern")],
         3:  [(4, "SWE Intern - Xbox")],
         14: [(5, "Research Intern")],
         45: [(6, "SWE Intern - Security")]}
DAY = [0]

for day in range(61):
    DAY[0] = day
    for i, title in APPLY.get(day, []):
        con.execute("INSERT INTO job(company_id,title,season,canonical_url,"
                    "first_seen_at,last_seen_at,status) "
                    "VALUES(1,?,'Summer 2027',?,?,?,'open')", (title, f"https://ms/{i}", now, now))
        con.execute("INSERT INTO application(job_key,stage,updated_at) "
                    "VALUES(?,'applied',?)", (f"https://ms/{i}", now))
        con.commit()
        print(f"day {day:>2}  APPLIED  {title}")

    d = _run.prepare(con, limit=10, per_company=3)
    if d["drafts"]:
        print(f"day {day:>2}  drafted {d['drafts']} note(s)")
    _run.schedule(con)
    out = _run.dispatch(con, dry_run=False, limit=10)
    for h in out.get("held", []):
        print(f"day {day:>2}  HELD {h}")

    # one day passes
    con.execute("UPDATE outreach SET sent_at=sent_at-86400 WHERE sent_at IS NOT NULL")
    con.execute("UPDATE outreach SET send_after=send_after-86400 WHERE status='queued'")
    con.commit()

print(f"\n{len(log)} emails, {len({t for _, t, _ in log})} distinct recipients")
for day, to, subj in log:
    print(f"  day {day:>2}  {to:<22} {subj[:52]}")
never = con.execute(
    "SELECT a.job_key FROM application a WHERE NOT EXISTS "
    "(SELECT 1 FROM outreach_job oj WHERE oj.job_key=a.job_key)").fetchall()
print("\napplications with no outreach:", [r["job_key"] for r in never] or "none")
