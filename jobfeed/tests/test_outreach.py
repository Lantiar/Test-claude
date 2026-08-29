"""The rules that stop this pipeline embarrassing you.

Everything here guards a failure that is silent in production: a send that
looks scheduled but lands on a Saturday, a follow-up to someone who already
replied, an address scored deliverable because the probe never ran.
"""
import datetime as dt
import json
import pathlib
import random
import time

import pytest

from jobfeed import db as _db
from jobfeed.outreach import apify, guards, templates


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def con(tmp_path):
    c = _db.connect(str(tmp_path / "t.sqlite3"))
    yield c
    c.close()


# ---- address verification -------------------------------------------------
#
# Scored against a live probe of 33 addresses across four domains, kept as a
# fixture. Eight of them are nonsense local parts that cannot correspond to a
# mailbox -- those are the assertions that matter.

def _probe():
    return json.loads((FIXTURES / "verify_probe.json").read_text())


CONTROL = "zq7x-no-such-person-4f81"


def test_no_invented_mailbox_is_ever_called_verified():
    """The one result that would put mail on a dead address."""
    for item in _probe():
        if CONTROL in item["email"]:
            assert apify._status(item) != "verified", item["email"]


def test_a_domain_that_accepts_everything_is_called_accept_all():
    """citadel.com answers yes to every local part, controls included.

    Reported as accept_all rather than verified, so the per-company quota
    applies instead of the pipeline believing it has six good addresses.
    """
    rows = [i for i in _probe() if i["email"].endswith("@citadel.com")]
    assert rows and all(apify._status(i) == "accept_all" for i in rows)


def test_a_probe_that_never_connected_is_not_a_verdict():
    """smtp_unreachable came back as both 'bad' and 'risky' for one domain in
    a single run, seconds apart. Neither is information about the mailbox."""
    seen = 0
    for item in _probe():
        if item.get("reason") == "smtp_unreachable":
            seen += 1
            assert apify._status(item) == "unknown", item
    assert seen, "fixture no longer exercises the unreachable path"


def test_good_means_deliverable():
    """This actor's word for it. Missing from the mapping, every genuinely
    verified address scored 'unknown' -- and unknown is allowed to send, so
    nothing downstream would have shown the bug."""
    assert apify._status({"email": "a@b.com", "status": "good", "reason": "ok"}) \
        == "verified"


def test_status_words_from_other_actors_still_map():
    """The actor id is configurable, so the vocabulary has to be forgiving."""
    for word in ("valid", "deliverable", "ok", "safe", "good"):
        assert apify._status({"status": word}) == "verified"
    for word in ("invalid", "undeliverable", "bad"):
        assert apify._status({"status": word}) == "invalid"
    assert apify._status({"isValid": True}) == "verified"
    assert apify._status({"disposable": True, "status": "good"}) == "risky"
    assert apify._status({}) == "unknown"


# ---- scheduling -----------------------------------------------------------

def test_nothing_is_scheduled_outside_a_working_window():
    """Volume, weekday and hour all at once, over a queue big enough to span
    several days. A Sunday 3am send is the single most legible bot signal."""
    times = guards.plan_sends(40, rng=random.Random(7))
    per_day = {}
    for stamp in times:
        local = dt.datetime.utcfromtimestamp(stamp) + dt.timedelta(hours=-5)
        assert local.weekday() < 5, f"weekend send at {local}"
        minute = local.hour * 60 + local.minute
        assert guards.WINDOW_START - guards.JITTER <= minute \
            <= guards.WINDOW_END + guards.JITTER, f"out of window at {local}"
        per_day[local.date()] = per_day.get(local.date(), 0) + 1
    assert len(times) == 40
    for day, n in per_day.items():
        assert n <= guards.DAILY_MAX, f"{n} sends on {day}"


def test_no_send_is_scheduled_in_the_past():
    """Otherwise the first dispatch after building a queue empties whatever
    already expired, in one burst -- the exact pattern the scatter prevents."""
    before = time.time()
    for seed in range(20):
        for stamp in guards.plan_sends(6, rng=random.Random(seed)):
            assert stamp >= before, seed


def test_sends_are_not_evenly_spaced():
    """Identical gaps are the other legible signal, and would survive every
    other check here."""
    times = guards.plan_sends(12, rng=random.Random(3))
    gaps = {round((b - a) / 60) for a, b in zip(times, times[1:])
            if b - a < 6 * 3600}
    assert len(gaps) > 3, gaps


def test_a_queue_built_after_hours_does_not_all_fire_at_once():
    """Friday night: every slot has already passed in the recipient's day, and
    a naive plan would dump the lot the moment dispatch next runs."""
    friday_night = dt.datetime(2026, 8, 28, 23, 30, tzinfo=dt.timezone.utc)
    times = guards.plan_sends(8, start=friday_night, rng=random.Random(11))
    assert times[-1] - times[0] > 3600


# ---- suppression ----------------------------------------------------------

def test_a_hard_bounce_suppresses_the_address(con):
    guards.suppress(con, "someone@example.com", None, "hard bounce")
    assert guards.suppressed(con, "someone@example.com", None)
    assert not guards.suppressed(con, "other@example.com", None)


def test_two_people_at_one_company_are_not_written_to_in_one_week(con):
    """Three notes from one stranger in a week is what a recruiting team
    forwards to each other, and that is how a domain gets blocked."""
    con.execute("INSERT INTO company(name, norm, created_at) "
                "VALUES('Acme','acme',?)", (time.time(),))
    cid = con.execute("SELECT id FROM company").fetchone()["id"]
    con.execute("INSERT INTO contact(company_id, full_name, email, found_at) "
                "VALUES(?,'A','a@acme.com',?)", (cid, time.time()))
    con.execute(
        "INSERT INTO outreach(job_key, contact_id, subject, body, step, status, "
        "sent_at, created_at) VALUES('k',1,'s','b',0,'sent',?,?)",
        (time.time(), time.time()))
    con.commit()
    assert guards.suppressed(con, "b@acme.com", cid)


# ---- templates ------------------------------------------------------------

def test_a_draft_has_no_unfilled_placeholders():
    for step in (0, 1, 2):
        subject, body, _ = templates.render(
            {"id": 4, "first_name": "Dana", "full_name": "Dana Reed",
             "title": "University Recruiter"},
            {"company": "Acme", "role": "Software Engineer Intern",
             "season": "Summer 2027"}, step=step)
        for text in (subject, body):
            assert "{" not in text and "}" not in text, text
            assert "None" not in text, text
        assert "Acme" in body

def test_variant_is_stable_per_contact():
    """Re-rendering must not reroll the wording: a follow-up that opens
    differently from the note it is following up on reads as a mail merge."""
    person = {"id": 9, "first_name": "Dana", "full_name": "Dana Reed", "title": "x"}
    job = {"company": "Acme", "role": "SWE Intern", "season": "Summer 2027"}
    first = templates.render(person, job)
    assert templates.render(person, job) == first
    assert templates.render({**person, "id": 10}, job)[0] != first[0] \
        or templates.render({**person, "id": 11}, job)[0] != first[0]


def test_a_followup_keeps_the_original_subject():
    """A new subject starts a new thread, and the recruiter loses the context
    that made the first note worth answering."""
    person = {"id": 4, "first_name": "Dana", "full_name": "Dana Reed", "title": "x"}
    job = {"company": "Acme", "role": "SWE Intern", "season": "Summer 2027"}
    assert templates.render(person, job, step=1)[0] == \
        templates.render(person, job, step=0)[0]


# ---- recruiter sourcing ---------------------------------------------------
#
# Against a live search for "Amazon university recruiter", kept as a fixture.
# Three of those ten profiles work somewhere else entirely, which is the point.

def _people(monkeypatch):
    items = json.loads((FIXTURES / "people_probe.json").read_text())
    monkeypatch.setattr(apify, "_call", lambda *a, **k: items)
    return items


def test_recruiters_at_other_companies_are_dropped(monkeypatch):
    """The search query does not constrain the employer, so this filter is the
    only thing standing between an Amazon application and a note to a
    recruiter at Roku."""
    _people(monkeypatch)
    for c in apify.find_recruiters("Amazon", 3):
        assert "amazon" in c["employer"].lower(), c


def test_a_subsidiary_still_counts_as_the_employer(monkeypatch):
    """LinkedIn says "Amazon Web Services (AWS)" where the posting says
    "Amazon". Requiring an exact match here finds nobody at all."""
    _people(monkeypatch)
    assert len(apify.find_recruiters("Amazon", 3)) == 3


def test_the_address_is_read_out_of_the_list(monkeypatch):
    """The field is `emails`, a list of dicts. Reading `email` returns None
    for everyone, and every contact is skipped as having no address -- a run
    that costs full price and writes nothing."""
    _people(monkeypatch)
    found = apify.find_recruiters("Amazon", 3)
    assert all(c["email"] and "@" in c["email"] for c in found), found


def test_deliverable_addresses_are_offered_first(monkeypatch):
    """Three contacts is the entire budget for a company; a catch-all guess
    should not displace an address that was actually confirmed."""
    _people(monkeypatch)
    found = apify.find_recruiters("Amazon", 3)
    assert found[0]["email_status"] == "verified"
    assert [c["email_status"] for c in found] == \
        sorted((c["email_status"] for c in found),
               key=lambda s: apify._RANK[s])


def test_someone_with_no_address_is_still_returned(monkeypatch):
    """Worth keeping: the name and LinkedIn URL are useful even when the
    pipeline cannot write to them, and _may_write does the skipping."""
    _people(monkeypatch)
    found = apify.find_recruiters("Roku", 3)
    assert len(found) == 1 and found[0]["email"] is None


# ---- what goes out, and when ----------------------------------------------

def _company(con, name):
    con.execute("INSERT INTO company(name, norm, created_at) VALUES(?,?,?)",
                (name, name.lower(), time.time()))
    return con.execute("SELECT id FROM company WHERE name=?", (name,)).fetchone()["id"]


def _draft(con, cid, email, key="k", status="verified"):
    con.execute("INSERT INTO contact(company_id, full_name, first_name, email, "
                "email_status, found_at) VALUES(?,?,?,?,?,?)",
                (cid, email, email.split("@")[0], email, status, time.time()))
    contact_id = con.execute("SELECT MAX(id) m FROM contact").fetchone()["m"]
    con.execute("INSERT INTO outreach(job_key, contact_id, subject, body, step, "
                "status, created_at) VALUES(?,?,'s','b',0,'draft',?)",
                (key, contact_id, time.time()))
    con.commit()
    return contact_id


def test_one_company_does_not_get_three_notes_on_one_day(con):
    """Three recruiters on one team comparing three near-identical emails over
    one lunch is the exact outcome this pipeline exists to avoid, and it is
    what insertion order produces: one application's contacts are adjacent, so
    they take adjacent slots, which are all on day one."""
    from jobfeed.outreach import run as _run
    a, b = _company(con, "Acme"), _company(con, "Globex")
    for i in range(3):
        _draft(con, a, f"a{i}@acme.com", key=f"acme-{i}")
        _draft(con, b, f"b{i}@globex.com", key=f"globex-{i}")
    _run.schedule(con)

    days = {}
    for r in con.execute(
            "SELECT c.company_id, o.send_after FROM outreach o "
            "JOIN contact c ON c.id=o.contact_id"):
        local = dt.datetime.utcfromtimestamp(r["send_after"]) - dt.timedelta(hours=5)
        key = (r["company_id"], local.date())
        days[key] = days.get(key, 0) + 1
    assert days and max(days.values()) == 1, days


def test_a_draft_is_rechecked_before_it_is_sent(con):
    """Drafts sit in the queue for days. An address that bounced in the
    meantime was only ever checked at the time it was written."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _draft(con, cid, "gone@acme.com")
    _run.schedule(con)
    con.execute("UPDATE outreach SET send_after=?", (time.time() - 60,))
    guards.suppress(con, "gone@acme.com", None, "hard bounce")

    out = _run.dispatch(con, dry_run=True)
    assert out["sent"] == 0 and out["held"], out
    assert con.execute("SELECT status FROM outreach").fetchone()["status"] == "held"


def test_dispatch_refuses_entirely_while_paused(con):
    """A tripped breaker must stop everything, not throttle it: the bounce
    rate that tripped it is evidence the addresses are bad, and the next send
    makes it worse."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _draft(con, cid, "someone@acme.com")
    _run.schedule(con)
    con.execute("UPDATE outreach SET send_after=?", (time.time() - 60,))
    guards.pause(con, "test")
    assert _run.dispatch(con, dry_run=True)["sent"] == 0


def test_a_season_that_has_effectively_passed_is_not_quoted():
    """Simplify called the Annapurna Labs role "Fall 2026" while its own URL
    ended in -2027. Naming the wrong intake to the person who runs it reads
    worse than naming none."""
    from jobfeed.outreach.templates import usable_season
    today = dt.date(2026, 8, 29)
    assert usable_season("Fall 2026", today) == ""
    assert usable_season("Summer 2027", today) == "Summer 2027"
    assert usable_season("internship", today) == ""
    assert usable_season(None, today) == ""


def test_a_draft_without_a_season_still_reads_as_a_sentence():
    """Dropping the season must not leave a double space or a dangling "for"."""
    subject, body, _ = templates.render(
        {"id": 1, "first_name": "Dana", "full_name": "Dana Reed", "title": "x"},
        {"company": "Acme", "role": "SWE Intern", "season": "Fall 2020"})
    for line in (subject, *body.splitlines()):
        if line.startswith("  - "):        # the bullet list is indented on purpose
            continue
        assert "  " not in line and " ," not in line, repr(line)
    assert "for and" not in body and "'s  " not in body


def test_only_the_first_word_of_a_first_name_is_used():
    """LinkedIn firstName is free text: "Jeevan Lobo S." arrives with first
    name "Jeevan Lobo", and "Hi Jeevan Lobo," is the tell that a script
    wrote it."""
    _, body, _ = templates.render(
        {"id": 1, "first_name": "Jeevan Lobo", "full_name": "Jeevan Lobo S."},
        {"company": "Acme", "role": "SWE Intern", "season": "Summer 2027"})
    assert body.startswith("Hi Jeevan,"), body[:40]


# ---- reading the mailbox --------------------------------------------------

def test_header_names_are_sent_as_repeated_parameters(monkeypatch):
    """Gmail wants metadataHeaders=From&metadataHeaders=Subject. Collapsed
    into one value it answers 200 with the message and no headers at all --
    and a message with no headers classifies as a human reply, so bounces are
    recorded as interest and the bad address is never suppressed. Nothing
    upstream errors; the reply rate just looks unusually good.
    """
    from jobfeed.outreach import gmail
    seen = {}

    class _R:
        def read(self): return b'{"payload":{"headers":[]}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout=0):
        seen["url"] = req.full_url
        return _R()

    monkeypatch.setattr(gmail.urllib.request, "urlopen", fake)
    gmail._get("messages/1", "tok", format="metadata",
               metadataHeaders=["From", "Subject"])
    assert seen["url"].count("metadataHeaders=") == 2, seen["url"]
    assert "%5B" not in seen["url"], seen["url"]


def test_a_bounce_is_not_read_as_a_reply():
    """The whole suppression path hangs off this: misread once, the pipeline
    keeps writing to a dead address and the bounce rate it uses to protect
    the sending domain never rises."""
    from jobfeed.outreach.gmail import classify
    hard = classify({"from": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                     "subject": "Delivery Status Notification (Failure)",
                     "return-path": "<>"},
                    "550 5.1.1 The email account that you tried to reach "
                    "does not exist.")
    assert hard == ("bounce", "hard")
    soft = classify({"from": "postmaster@example.com",
                     "subject": "Undeliverable: your message"},
                    "451 4.4.1 temporary failure, will retry")
    assert soft == ("bounce", "soft")
    human, _ = classify(
        {"from": "Laura Resio <laura.resio@amazon.com>", "subject": "Re: your note"},
        "Thanks for reaching out -- happy to take a look at your application.")
    assert human == "human"


def test_an_out_of_office_stops_the_sequence_without_counting_as_interest():
    """A follow-up landing after any answer, even an automatic one, is what
    turns a polite note into a complaint."""
    from jobfeed.outreach.gmail import classify
    kind, _ = classify({"from": "laura.resio@amazon.com",
                        "subject": "Automatic reply: your note",
                        "auto-submitted": "auto-replied"}, "I am out of office")
    assert kind == "auto"


def test_a_reply_is_matched_by_its_threading_headers(con):
    """Not by subject: a recruiter who forwards internally or rewrites the
    subject still carries References, and matching on subject would attach
    the reply to the wrong application."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _draft(con, cid, "dana@acme.com")
    con.execute("UPDATE outreach SET status='sent', sent_at=?, message_id=?, "
                "thread_id='T1'", (time.time(), "<abc@mail.gmail.com>"))
    con.commit()

    row = _run._match(con, {"headers": {"references": "<abc@mail.gmail.com>"},
                            "thread_id": "other", "from": "someone@else.com"})
    assert row and row["email"] == "dana@acme.com"

    # No headers at all: the sender is the last resort, and must still work.
    row = _run._match(con, {"headers": {}, "thread_id": None,
                            "from": "dana@acme.com"})
    assert row and row["email"] == "dana@acme.com"

    assert _run._match(con, {"headers": {}, "thread_id": None,
                             "from": "stranger@nowhere.com"}) is None


def test_a_followup_reuses_the_subject_that_was_actually_sent(con):
    """Re-rendering reads live job and profile rows. A posting retitled
    between the first note and the follow-up would change the subject, and a
    changed subject is a new thread rather than a nudge on the old one."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _draft(con, cid, "dana@acme.com", key="https://x/1")
    con.execute("INSERT INTO job(company_id,title,season,canonical_url,"
                "first_seen_at,last_seen_at,status) "
                "VALUES(?,'SWE Intern','Summer 2027','https://x/1',?,?,'open')",
                (cid, time.time(), time.time()))
    con.execute("UPDATE outreach SET status='sent', sent_at=?, subject=?, "
                "message_id='<m1@x>', thread_id='T1'",
                (time.time() - 5 * 86400, "the subject they saw"))
    con.commit()

    assert _run.followups(con)["queued"] == 1
    row = con.execute("SELECT subject, thread_id, message_id FROM outreach "
                      "WHERE step=1").fetchone()
    assert row["subject"] == "the subject they saw"
    assert row["thread_id"] == "T1" and row["message_id"] == "<m1@x>"


# ---- the subject line -----------------------------------------------------

def test_a_subject_is_never_cut_mid_phrase():
    """Blunt truncation removed exactly the load-bearing words, leaving
    "Summer 2027 Software Development Engineer" with no "Intern" and no
    "application" -- a subject that no longer says what the mail is."""
    long_role = "Software Development Engineer Intern - Annapurna Labs"
    for cid in range(3):
        subject, _, _ = templates.render(
            {"id": cid, "first_name": "Dana"},
            {"company": "Amazon", "role": long_role, "season": "Summer 2027"})
        assert len(subject) <= templates.SUBJECT_MAX, (len(subject), subject)
        assert "Intern" in subject, subject
        assert not subject.endswith(("-", ",", " ")), subject


def test_the_team_suffix_is_dropped_from_the_subject_but_kept_in_the_body():
    """The team is worth reading once the recruiter is already in the mail,
    and worth losing where it pushes the rest of the line past the cut."""
    subject, body, _ = templates.render(
        {"id": 0, "first_name": "Dana"},
        {"company": "Amazon", "season": "Summer 2027",
         "role": "Software Development Engineer Intern - Annapurna Labs"})
    assert "Annapurna" not in subject
    assert "Annapurna" in body


def test_the_ladder_only_drops_what_it_has_to():
    """Otherwise every subject collapses to the barest rung. A short role
    keeps the closing phrase; a long one gives it up and keeps the role."""
    short, _, _ = templates.render(
        {"id": 0, "first_name": "Dana"},
        {"company": "Acme", "role": "SWE Intern", "season": "Summer 2027"})
    assert "applied, would love to connect" in short, short

    long, _, _ = templates.render(
        {"id": 0, "first_name": "Dana"},
        {"company": "Amazon", "season": "Summer 2027",
         "role": "Software Development Engineer Intern - Annapurna Labs"})
    assert "Software Development Engineer Intern" in long, long


def test_an_empty_bracket_leaves_no_empty_brackets(monkeypatch):
    """Clearing it must drop the brackets with it, not send every subject out
    starting "[] "."""
    from jobfeed.outreach import profile, templates as t
    monkeypatch.setattr(profile, "BRACKET", "")
    monkeypatch.setattr(t, "bracket", lambda: "")
    subject, _, _ = t.render({"id": 0, "first_name": "Dana"},
                             {"company": "Acme", "role": "SWE Intern"})
    assert not subject.startswith("["), subject
    assert "[]" not in subject


def test_the_bracket_is_one_setting(monkeypatch):
    """Wording it is a judgement call about how you want to be read, so it is
    a string to set rather than parts to compose."""
    from jobfeed.outreach import profile, templates as t
    monkeypatch.setattr(t, "bracket", lambda: "Prev Google/Zon")
    subject, _, _ = t.render({"id": 0, "first_name": "Dana"},
                             {"company": "Acme", "role": "SWE Intern"})
    assert subject.startswith("[Prev Google/Zon] "), subject


# ---- several applications at one company ----------------------------------

def _job(con, cid, key, title):
    con.execute("INSERT INTO job(company_id,title,season,canonical_url,"
                "first_seen_at,last_seen_at,status) "
                "VALUES(?,?,'Summer 2027',?,?,?,'open')",
                (cid, title, key, time.time(), time.time()))
    con.execute("INSERT INTO application(job_key,stage,updated_at) "
                "VALUES(?,'applied',?)", (key, time.time()))
    con.commit()


def _roster(monkeypatch, size=99):
    """A company with `size` findable recruiters, named Rec 1, Rec 2, ...

    Returns as many as asked for, like the real search: the top-up path in
    _recruiters asks for more than it needs, and a stub with a fixed list
    would make that look like it worked when it had nothing new to give.
    """
    def find(company, n=3):
        return [{"full_name": f"Rec {i}", "first_name": f"R{i}",
                 "title": "University Recruiter", "linkedin_url": "",
                 "email": f"rec{i}@acme.com", "email_status": "verified"}
                for i in range(1, min(n, size) + 1)]
    monkeypatch.setattr(apify, "find_recruiters", find)


def _sent(con, oid):
    """Mark one outreach row sent, as dispatch would."""
    con.execute("UPDATE outreach SET status='sent', sent_at=?, message_id='<m@x>' "
                "WHERE id=?", (time.time(), oid))
    con.commit()


def test_three_roles_at_one_company_become_one_note_each_to_three_people(con, monkeypatch):
    """Three recruiters, three notes -- but each note names all three roles.
    Drafting per posting instead produced nine near-identical emails to the
    same three people, and the cooldown then held eight of them forever."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _roster(monkeypatch)
    titles = ["SWE Intern", "SWE Intern - Networking", "Data Scientist Intern"]
    for i, title in enumerate(titles, 1):
        _job(con, cid, f"https://acme/{i}", title)

    stats = _run.prepare(con, limit=10, per_company=3)
    assert stats["jobs"] == 3 and stats["drafts"] == 3, stats

    rows = con.execute("SELECT * FROM outreach").fetchall()
    assert len({r["contact_id"] for r in rows}) == 3
    assert len({r["campaign"] for r in rows}) == 1, "one batch, one campaign"
    for row in rows:
        for title in titles:
            assert title in row["body"], (row["id"], title)
    # Every application is recorded against every note, or the roles a note
    # did not name as primary look undrafted and get written again tomorrow.
    assert con.execute("SELECT COUNT(*) c FROM outreach_job").fetchone()["c"] == 9
    assert _run.prepare(con, limit=10)["drafts"] == 0


def test_a_later_application_reaches_different_recruiters(con, monkeypatch):
    """A fortnight later, a fresh batch goes to people who have not heard from
    you. Without the top-up in _recruiters the first application spends the
    whole cached roster and every later one finds nobody new."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _roster(monkeypatch)

    batches = []
    for i in range(1, 4):
        _job(con, cid, f"https://acme/{i}", f"SWE Intern {i}")
        assert _run.prepare(con, limit=10, per_company=3)["drafts"] == 3, i
        rows = con.execute("SELECT o.id, c.email FROM outreach o "
                           "JOIN contact c ON c.id=o.contact_id "
                           "WHERE o.status='draft'").fetchall()
        batches.append({r["email"] for r in rows})
        for row in rows:
            _sent(con, row["id"])
        # a fortnight passes before the next application
        con.execute("UPDATE outreach SET sent_at=sent_at-? WHERE sent_at IS NOT NULL",
                    (14 * 86400,))
        con.commit()

    assert all(len(b) == 3 for b in batches), batches
    assert not (batches[0] & batches[1]) and not (batches[1] & batches[2]), batches
    assert len(batches[0] | batches[1] | batches[2]) == 9


def test_a_second_application_too_soon_waits_rather_than_writing(con, monkeypatch):
    """Three days after a batch, the cooldown has not expired. The drafts are
    not written now and thrown away -- they are simply not written yet, and a
    later run picks the application up once the company is free."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _roster(monkeypatch)
    _job(con, cid, "https://acme/1", "SWE Intern 1")
    _run.prepare(con, limit=10, per_company=3)
    for row in con.execute("SELECT id FROM outreach").fetchall():
        _sent(con, row["id"])
    con.execute("UPDATE outreach SET sent_at=sent_at-?", (3 * 86400,))
    con.commit()

    _job(con, cid, "https://acme/2", "SWE Intern 2")
    stats = _run.prepare(con, limit=10, per_company=3)
    assert stats["drafts"] == 0
    assert "contacted 3d ago" in " ".join(stats["skipped"]), stats["skipped"]

    # ... and once the cooldown lapses, the same application goes out.
    con.execute("UPDATE outreach SET sent_at=sent_at-? WHERE sent_at IS NOT NULL",
                (6 * 86400,))
    con.commit()
    assert _run.prepare(con, limit=10, per_company=3)["drafts"] == 3


def test_a_batch_does_not_block_itself(con, monkeypatch):
    """The three notes in one batch are days apart, so the first one sent
    would otherwise put the company in cooldown and hold the other two --
    which is exactly what happened before campaigns existed."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _roster(monkeypatch)
    _job(con, cid, "https://acme/1", "SWE Intern 1")
    _run.prepare(con, limit=10, per_company=3)
    _run.schedule(con)

    sent = []

    def fake(to, subject, body, **kw):
        sent.append(to)
        return {"message_id": f"<{len(sent)}@x>", "thread_id": f"T{len(sent)}"}

    monkeypatch.setattr(_run, "gmail_send", fake)
    # Eight, not three: the batch starts on the next weekday and skips a
    # weekend, so three sends can span more calendar days than there are notes.
    for _ in range(8):
        con.execute("UPDATE outreach SET send_after=send_after-86400 "
                    "WHERE status='queued'")
        con.commit()
        out = _run.dispatch(con, dry_run=False, limit=10)
        assert not out.get("held"), out["held"]
    assert len(sent) == 3, sent


def test_the_roster_running_out_stops_rather_than_repeats(con, monkeypatch):
    """A company with only three findable recruiters has nobody new after the
    first batch. Writing to the same people again that soon is the same note
    twice, so prepare stops."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _roster(monkeypatch, size=3)
    _job(con, cid, "https://acme/1", "SWE Intern 1")
    assert _run.prepare(con, limit=10, per_company=3)["drafts"] == 3
    for row in con.execute("SELECT id FROM outreach").fetchall():
        _sent(con, row["id"])
    con.execute("UPDATE outreach SET sent_at=sent_at-? WHERE sent_at IS NOT NULL",
                (30 * 86400,))
    con.commit()

    _job(con, cid, "https://acme/2", "SWE Intern 2")
    stats = _run.prepare(con, limit=10, per_company=3)
    assert stats["drafts"] == 0
    assert "contacted within" in " ".join(stats["skipped"]), stats["skipped"]


def test_roles_are_named_individually_in_a_grouped_note():
    """The team suffix is the only thing telling two postings apart, so
    shortening the list turns three roles into the same role written twice."""
    _, body, _ = templates.render(
        {"id": 1, "first_name": "Dana"},
        {"company": "Acme", "season": "Summer 2027",
         "roles": ["SWE Intern", "SWE Intern - Networking"]})
    assert "SWE Intern and SWE Intern - Networking" in body, body
    assert "two roles" in body


# ---- real posting titles --------------------------------------------------
#
# Every title below is one the feed actually stored. Invented titles are tidy
# in ways real ones are not, and each of these broke something.

def test_a_hyphenated_word_is_not_cut_in_half():
    """"ASIC Package Engineer Intern Co-op" went out as "... Intern Co":
    the team-suffix rule matched the hyphen inside Co-op."""
    assert templates.short_role("ASIC Package Engineer Intern Co-op") == \
        "ASIC Package Engineer Intern Co-op"
    assert templates.short_role("Software Development Engineer Intern - "
                                "Annapurna Labs") == \
        "Software Development Engineer Intern"


def test_a_season_in_the_title_is_not_said_twice():
    """Employers put it there themselves, so the sentence read "Summer 2027
    Software Engineer Intern - Vehicle Software - Summer 2027"."""
    _, body, _ = templates.render(
        {"id": 1, "first_name": "Dana"},
        {"company": "Tesla", "season": "Summer 2027",
         "role": "Software Engineer Intern - Vehicle Software - Summer 2027"})
    assert body.count("Summer 2027") == 1, body


def test_roles_from_different_seasons_do_not_claim_one_season():
    """Tesla listed a Summer 2027 and a Spring 2027 posting side by side. A
    note asserting one and then contradicting itself in the list below is
    worse than one that names neither."""
    _, body, _ = templates.render(
        {"id": 1, "first_name": "Dana"},
        {"company": "Tesla", "season": "Summer 2027", "roles": [
            "Software Engineer Intern - Vehicle Software - Summer 2027",
            "Software Engineer Intern - Information Security - Spring 2027"]})
    assert "Summer 2027" not in body and "Spring 2027" not in body, body


def test_the_count_matches_the_list():
    """The closing phrase said "three notes" whatever the number was, so four
    applications read "four roles ... rather than send you three notes"."""
    for n, word in ((2, "two"), (3, "three"), (4, "four"), (5, "five")):
        _, body, _ = templates.render(
            {"id": 2, "first_name": "Noor"},
            {"company": "American Express", "season": "Summer 2027",
             "roles": [f"Software Engineer Intern - Team {i}" for i in range(n)]})
        opener = body.split("I am a B.S.")[0]
        assert word in opener, (n, opener)
        for other in ("two", "three", "four", "five"):
            if other != word:
                assert other not in opener, (n, other, opener)


def test_more_than_two_roles_are_listed_rather_than_run_together():
    """Four real posting titles in one sentence is unreadable -- one of them
    is "Data Analytics Intern - Global Servicing - Financial Crimes Risk &
    Controls"."""
    titles = ["Product Development Intern - Global Servicing",
              "Data Analytics Intern - Global Servicing - Financial Crimes "
              "Risk & Controls",
              "Software Engineer Intern - Enterprise Technology Services"]
    _, body, _ = templates.render({"id": 1, "first_name": "Noor"},
                                  {"company": "American Express", "roles": titles})
    for t in titles:
        assert f"  - {t}" in body, t

    # Scoped to the opening paragraph: the résumé wins below it are a bulleted
    # list too, so checking the whole body proves nothing about the roles.
    _, pair, _ = templates.render({"id": 1, "first_name": "Noor"},
                                  {"company": "Amex", "roles": titles[:2]})
    opener = pair.split("I am a B.S.")[0]
    assert "  - " not in opener, opener
    assert f"{titles[0]} and {titles[1]}" in opener


def test_the_same_title_twice_is_listed_once():
    """Two distinct Amex postings share a title. Listing it twice reads as a
    bug in the sender, not as two applications."""
    dupe = "Software Engineer Intern - Enterprise Technology Services"
    _, body, _ = templates.render(
        {"id": 1, "first_name": "Noor"},
        {"company": "American Express", "roles": [dupe, dupe, "AI Engineer Intern"]})
    assert body.count(dupe) == 1 and "two roles" in body, body
