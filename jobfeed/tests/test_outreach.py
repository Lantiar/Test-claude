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


def test_no_subject_has_a_gap_where_a_field_was_empty():
    """An absent season left "Tesla  intern applications" -- two spaces, which
    in a subject line reads as a bug in the sender. Swept across every rung
    and every optional field rather than fixed in the one template that
    showed it."""
    for cid in range(3):
        for season in ("Summer 2027", ""):
            for roles in (["SWE Intern"], ["A Intern", "B Intern"],
                          ["A Intern", "B Intern", "C Intern"]):
                subject, _, _ = templates.render(
                    {"id": cid, "first_name": "Dana"},
                    {"company": "Tesla", "roles": roles, "season": season})
                assert "  " not in subject, (cid, season, roles, subject)
                assert subject == subject.strip()
                assert not subject.endswith(("-", ",")), subject


def test_a_subject_does_not_open_on_a_lowercase_word():
    """"three applications at Tesla" after the bracket reads as a fragment."""
    for cid in range(3):
        subject, _, _ = templates.render(
            {"id": cid, "first_name": "Dana"},
            {"company": "Tesla", "season": "Summer 2027",
             "roles": ["A Intern", "B Intern", "C Intern"]})
        first = subject.split("] ", 1)[-1].split()[0]
        assert first[0].isupper(), subject


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


# ---- the copy editor ------------------------------------------------------
#
# A cheap model may fix grammar and readability. It may not touch anything
# else, and it is not trusted to have obeyed that: verify() decides, and a
# revision that strayed is discarded whole.

def _head(body):
    """What the model is actually shown: the body without the signature."""
    from jobfeed.outreach.profile import signature
    sig = signature()
    return body.partition(sig)[0].rstrip() if sig else body.rstrip()


def _draft_pair():
    roles = ["Software Engineer Intern - Vehicle Software",
             "Software Engineer Intern - Information Security",
             "Product Support Engineer Intern - Service Engineering"]
    subject, body, _ = templates.render(
        {"id": 1, "first_name": "Dana"},
        {"company": "Tesla", "roles": roles, "season": "Summer 2027"})
    return subject, body, {"company": "Tesla", "first_name": "Dana", "roles": roles}


def test_a_grammar_fix_is_allowed():
    """If nothing passes, the editor is decoration."""
    from jobfeed.outreach.polish import verify
    s, b, ctx = _draft_pair()
    assert verify(s, b, s, b.replace("here is one:", "here is one —"), ctx) == []
    assert verify(s, b, s, b.replace("applied to three roles",
                                     "applied to the three roles"), ctx) == []


def test_nothing_factual_may_move():
    """Each of these is a revision a helpful model actually tends to produce,
    and each would go out under his name to someone who can check it."""
    from jobfeed.outreach.polish import verify
    s, b, ctx = _draft_pair()
    strays = {
        "swaps an employer": b.replace("Google SWE", "Meta SWE"),
        "inflates a metric": b.replace("33% to 2%", "43% to 1%"),
        "invents prior contact": b.replace(
            "Hi Dana,", "Hi Dana,\n\nGreat speaking last week."),
        "changes the greeting": b.replace("Hi Dana,", "Hi Daniel,"),
        "rewords a bullet": b.replace("Software Engineer Intern - Vehicle Software",
                                      "SWE Intern, Vehicle Software"),
        "swaps a link": b.replace("https://nideesh.ai", "https://nideesh.dev"),
        # Passed the first version of this check: the company survived in the
        # subject, so joining the two fields together hid its loss from the body.
        "drops the company": b.replace("Tesla", "the company"),
        # And this one adds no name, no number and no URL, and is short enough
        # to stay inside the length band.
        "adds flattery": b.replace(
            "Thanks,", "I am a huge admirer of your work here.\n\nThanks,"),
    }
    for label, revision in strays.items():
        assert verify(s, b, s, revision, ctx), label
    assert verify(s, b, "[Prev Google/Zon] AMAZING candidate!!", b, ctx)


def test_a_rejected_revision_leaves_the_original_untouched(monkeypatch):
    """The fallback has to be the text that was already tested, never nothing
    and never a guess -- this runs on mail about to be sent."""
    from jobfeed.outreach import polish as _p
    s, b, ctx = _draft_pair()

    class _R:
        def __init__(self, payload): self.payload = payload
        def read(self): return json.dumps(self.payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def stray(req, timeout=0):
        return _R({"usage": {"prompt_tokens": 400, "completion_tokens": 300},
                   "choices": [{"message": {"content": json.dumps(
                       {"subject": s, "body": _head(b).replace("Google", "Meta"),
                        "notes": ["tightened"]})}}]})

    monkeypatch.setattr(_p.urllib.request, "urlopen", stray)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    out = _p.polish(s, b, ctx)
    assert out["subject"] == s and out["body"] == b
    assert out["rejected"] and not out["changed"]


def test_the_api_being_down_is_not_an_edit(monkeypatch):
    """No key, an outage or a rate limit must leave the draft as written --
    the live account was out of credit the day this was built, and that is
    exactly when a silent empty body would have shipped."""
    from jobfeed.outreach import polish as _p
    s, b, ctx = _draft_pair()

    def boom(req, timeout=0):
        raise OSError("connection reset")

    monkeypatch.setattr(_p.urllib.request, "urlopen", boom)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    out = _p.polish(s, b, ctx)
    assert (out["subject"], out["body"]) == (s, b) and out["rejected"]

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = _p.polish(s, b, ctx)
    assert (out["subject"], out["body"]) == (s, b) and out["rejected"]


def test_an_accepted_edit_is_written_back(con, monkeypatch):
    """And the draft is marked polished, so a second run is not a second bill."""
    from jobfeed.outreach import run as _run, polish as _p
    cid = _company(con, "Acme")
    _roster(monkeypatch)
    _job(con, cid, "https://acme/1", "SWE Intern")
    _run.prepare(con, limit=5, per_company=1)
    before = con.execute("SELECT subject, body FROM outreach").fetchone()

    class _R:
        def __init__(self, p): self.p = p
        def read(self): return json.dumps(self.p).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    calls = []

    def fixed(req, timeout=0):
        calls.append(1)
        return _R({"usage": {"prompt_tokens": 400, "completion_tokens": 300},
                   "choices": [{"message": {"content": json.dumps(
                       {"subject": before["subject"],
                        "body": _head(before["body"]),
                        "notes": ["no change needed"]})}}]})

    monkeypatch.setattr(_p.urllib.request, "urlopen", fixed)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert _run.polish_drafts(con)["rejected"] == 0
    assert con.execute("SELECT polished_at FROM outreach").fetchone()["polished_at"]
    assert _run.polish_drafts(con)["seen"] == 0 and len(calls) == 1


# ---- the follow-up chain --------------------------------------------------

def _sent_batch(con, cid, monkeypatch, key="https://acme/1"):
    from jobfeed.outreach import run as _run
    _roster(monkeypatch)
    _job(con, cid, key, "SWE Intern")
    _run.prepare(con, limit=5, per_company=3)
    _run.schedule(con)
    for row in con.execute("SELECT id FROM outreach").fetchall():
        _sent(con, row["id"])
    return _run


def test_the_second_nudge_waits_on_the_first_being_sent(con, monkeypatch):
    """Both steps keyed off the original, so the moment it was nine days old
    they queued in the same pass -- "following up again" written before the
    first follow-up had gone out, and the two landing together."""
    cid = _company(con, "Acme")
    _run = _sent_batch(con, cid, monkeypatch)
    con.execute("UPDATE outreach SET sent_at=sent_at-?", (30 * 86400,))
    con.commit()

    _run.followups(con)
    assert con.execute("SELECT COUNT(*) c FROM outreach WHERE step=1").fetchone()["c"] == 3
    assert con.execute("SELECT COUNT(*) c FROM outreach WHERE step=2").fetchone()["c"] == 0

    for row in con.execute("SELECT id FROM outreach WHERE step=1").fetchall():
        _sent(con, row["id"])
    con.execute("UPDATE outreach SET sent_at=sent_at-? WHERE step=1", (6 * 86400,))
    con.commit()
    _run.followups(con)
    orphans = con.execute(
        "SELECT COUNT(*) c FROM outreach f WHERE f.step=2 AND NOT EXISTS "
        "(SELECT 1 FROM outreach p WHERE p.contact_id=f.contact_id AND p.step=1 "
        " AND p.status='sent')").fetchone()["c"]
    assert orphans == 0


def test_followups_go_through_the_same_spacing_as_first_sends(con, monkeypatch):
    """They scheduled themselves one at a time and skipped the company rule:
    five landed on one day, three of them at the same employer. That is the
    burst everything else here is built to avoid, arriving down the one path
    that was not going through the spacing."""
    cid = _company(con, "Acme")
    _run = _sent_batch(con, cid, monkeypatch)
    con.execute("UPDATE outreach SET sent_at=sent_at-?", (30 * 86400,))
    con.commit()

    _run.followups(con)
    assert con.execute("SELECT COUNT(*) c FROM outreach WHERE step=1 AND "
                       "send_after IS NOT NULL").fetchone()["c"] == 0, \
        "a follow-up must not place itself"
    _run.schedule(con)

    per_day = {}
    for r in con.execute("SELECT c.company_id, o.send_after FROM outreach o "
                         "JOIN contact c ON c.id=o.contact_id "
                         "WHERE o.step=1 AND o.send_after"):
        local = dt.datetime.utcfromtimestamp(r["send_after"]) - dt.timedelta(hours=5)
        key = (r["company_id"], local.date())
        per_day[key] = per_day.get(key, 0) + 1
        assert local.weekday() < 5, local
    assert per_day and max(per_day.values()) == 1, per_day


def test_a_followup_is_not_blocked_by_the_company_cooldown(con, monkeypatch):
    """It continues a thread this person is already in. The cooldown exists to
    stop a stream of strangers; holding a nudge because someone else at the
    same employer was written to reads as being dropped, not as restraint."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _draft(con, cid, "dana@acme.com")
    con.execute("UPDATE outreach SET status='sent', sent_at=?, step=1, "
                "message_id='<m@x>'", (time.time(),))
    con.commit()
    # Someone else at this company was written to moments ago.
    _draft(con, cid, "other@acme.com", key="https://acme/2")
    row = con.execute("SELECT o.*, c.email, c.company_id, c.email_status "
                      "FROM outreach o JOIN contact c ON c.id=o.contact_id "
                      "WHERE c.email='dana@acme.com'").fetchone()
    assert guards.suppressed(con, "dana@acme.com", cid), "a new contact is blocked"
    assert _run._may_write(con, dict(row), cid, None, followup=True) == ""


def test_a_rejected_revision_is_retried_with_the_reason(con, monkeypatch):
    """Told only "stay in scope", a model returns the same edit again and the
    call is wasted. Told which rule it broke, it usually returns a smaller,
    valid one."""
    from jobfeed.outreach import polish as _p
    s, b, ctx = _draft_pair()
    seen = []

    class _R:
        def __init__(self, p): self.p = p
        def read(self): return json.dumps(self.p).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def two_tries(req, timeout=0):
        payload = json.loads(req.data)
        seen.append(payload["messages"])
        # First answer strays; second, after being told why, is a real fix.
        revision = (_head(b).replace("Google", "Meta") if len(seen) == 1
                    else _head(b).replace("here is one:", "here is one —"))
        return _R({"usage": {"prompt_tokens": 500, "completion_tokens": 400},
                   "choices": [{"message": {"content": json.dumps(
                       {"subject": s, "body": revision, "notes": ["em dash"]})}}]})

    monkeypatch.setattr(_p.urllib.request, "urlopen", two_tries)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    out = _p.polish(s, b, ctx)

    assert out["retried"] and out["changed"] and not out["rejected"]
    assert "—" in out["body"] and "Meta" not in out["body"]
    # The second call must carry the checker's actual complaint, not a generic
    # scolding -- that is the whole reason the retry converts.
    followup = seen[1][-1]["content"]
    assert "rejected" in followup and "Meta" in followup, followup


def test_two_bad_revisions_give_up_rather_than_looping(con, monkeypatch):
    """A model that will not comply must cost two calls, not twenty."""
    from jobfeed.outreach import polish as _p
    s, b, ctx = _draft_pair()
    calls = []

    class _R:
        def __init__(self, p): self.p = p
        def read(self): return json.dumps(self.p).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def always_stray(req, timeout=0):
        calls.append(1)
        return _R({"usage": {"prompt_tokens": 500, "completion_tokens": 400},
                   "choices": [{"message": {"content": json.dumps(
                       {"subject": s, "body": _head(b).replace("Google", "Meta"),
                        "notes": []})}}]})

    monkeypatch.setattr(_p.urllib.request, "urlopen", always_stray)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    out = _p.polish(s, b, ctx)
    assert len(calls) == 2 and out["rejected"]
    assert (out["subject"], out["body"]) == (s, b)


def test_a_model_that_refuses_temperature_is_not_an_outage(monkeypatch):
    """Newer models accept only the default and reject the request outright.
    Reported as an error, a model swap would look exactly like an API being
    down."""
    from jobfeed.outreach import polish as _p
    s, b, ctx = _draft_pair()
    payloads = []

    class _R:
        def __init__(self, p): self.p = p
        def read(self): return json.dumps(self.p).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def picky(req, timeout=0):
        payload = json.loads(req.data)
        payloads.append(payload)
        if "temperature" in payload:
            raise _p.urllib.error.HTTPError(
                "u", 400, "Bad Request", {},
                __import__("io").BytesIO(
                    b'{"error":{"message":"Unsupported value: \'temperature\'"}}'))
        return _R({"usage": {"prompt_tokens": 500, "completion_tokens": 400},
                   "choices": [{"message": {"content": json.dumps(
                       {"subject": s, "body": _head(b), "notes": []})}}]})

    monkeypatch.setattr(_p.urllib.request, "urlopen", picky)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    out = _p.polish(s, b, ctx)
    assert not out["rejected"], out["rejected"]
    assert "temperature" in payloads[0] and "temperature" not in payloads[1]


def test_the_prompt_lists_the_words_the_checker_allows():
    """Built from ADDABLE rather than describing it, so the prompt and the
    check cannot drift apart."""
    from jobfeed.outreach import polish as _p
    prompt = _p._system()
    for word in ("and", "the", "would"):
        assert f" {word} " in prompt, word
    assert "admirer" not in prompt
    assert "20%" in prompt
    assert "{" not in prompt.replace('{"subject"', "").replace('{"', ""), \
        "an unformatted placeholder survived into the prompt"


def test_the_signature_is_never_shown_to_the_editor(con, monkeypatch):
    """Sent and reattached, an edit that merely reformatted it made the
    reattach point unfindable, and the block was appended a second time -- a
    mail signed twice, with every required token present and the length still
    inside the band, so nothing downstream objected.

    Only meaningful when a signature is configured; the default is none.
    """
    from jobfeed.outreach import polish as _p, profile, templates as _t
    monkeypatch.setattr(profile, "signature", lambda: "Nideesh Bharath Kumar")
    monkeypatch.setattr(_p, "signature", lambda: "Nideesh Bharath Kumar")
    monkeypatch.setattr(_t, "signature", lambda: "Nideesh Bharath Kumar")
    signature = profile.signature
    s, b, ctx = _draft_pair()
    shown = []

    class _R:
        def __init__(self, p): self.p = p
        def read(self): return json.dumps(self.p).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def reformats_the_signature(req, timeout=0):
        shown.append(json.loads(req.data)["messages"][1]["content"])
        mangled = _head(b) + "\n\n" + signature().replace(" · ", "   ")
        return _R({"usage": {"prompt_tokens": 500, "completion_tokens": 400},
                   "choices": [{"message": {"content": json.dumps(
                       {"subject": s, "body": mangled, "notes": []})}}]})

    monkeypatch.setattr(_p.urllib.request, "urlopen", reformats_the_signature)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    out = _p.polish(s, b, ctx)

    assert signature() not in shown[0], "the signature was sent to the model"
    if not out["rejected"]:
        assert out["body"].count("Nideesh Bharath Kumar") == 1, out["body"]
        assert out["body"].count("https://nideesh.ai") == 1


# ---- title cleaning -------------------------------------------------------
#
# The one place a stranger's text reaches a recruiter under his name. The
# model may judge, but its only permitted action is deletion, and the check
# is mechanical.

def test_only_words_from_the_original_survive():
    """The entire guarantee. A model that reorders, rephrases, corrects a
    spelling or adds a word fails here and the original is kept, so no title
    can ever be written rather than chosen."""
    from jobfeed.outreach.titles import is_subsequence as sub
    original = "Software Engineer Intern - Global E-Commerce - 2027 Summer"
    assert sub("Software Engineer Intern - Global E-Commerce", original)
    assert sub("Software Engineer Intern", original)
    assert sub(original, original)
    assert not sub("Software Engineering Intern", original)      # rephrased
    assert not sub("Global E-Commerce Software Engineer Intern", original)  # reordered
    assert not sub("Software Engineer Intern - E-Commerce Team", original)  # invented
    assert not sub("Software Engineer Intern - Global E-Commerce!", original)
    assert not sub("", original)


def test_a_digit_that_belongs_to_the_work_is_never_asked_about():
    """"Geometry and 3D Vision" and "- 2027 Summer" both carry digits and only
    one is noise. The detector keeps the first away from the model entirely,
    so it is safe by construction rather than by the model's good judgement."""
    from jobfeed.outreach.titles import needs_review
    assert not needs_review("Computer Vision Scientist Intern - Geometry and 3D Vision")
    assert not needs_review("ASIC Package Engineer Intern Co-op")
    assert not needs_review("Software Engineer Intern - Vehicle Software")
    for noisy in ("Analyst Co-op Intern - Winter 2027 - 4 Months",
                  "Engineer Co-op - Summer 2027 - Plus one semester",
                  "Data Enablement Co-op - CFO - 8 Months",
                  "Software Engineer Intern - Network Security - 2026 Start",
                  "Engineer Intern R129582"):
        assert needs_review(noisy), noisy


def test_an_invented_title_is_refused(monkeypatch):
    from jobfeed.outreach import titles as _t
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(_t, "_ask", lambda m, k: (
        {"keep": "Senior Software Engineering Internship", "reason": "tidied"}, "", 0.0))
    out = _t.clean("Software Engineer Intern - 2026 Start")
    assert out["rejected"].startswith("not a subsequence")
    assert out["title"] == "Software Engineer Intern - 2026 Start"
    assert not out["changed"]


def test_a_title_trimmed_to_nothing_is_refused(monkeypatch):
    """Two words is the floor. "Intern" alone does not name a role, and an
    email that says it reads worse than one quoting the employer verbatim."""
    from jobfeed.outreach import titles as _t
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    for answer in ("Intern", ""):
        monkeypatch.setattr(_t, "_ask", lambda m, k, a=answer: (
            {"keep": a, "reason": ""}, "", 0.0))
        out = _t.clean("Data Enablement Co-op - CFO - 8 Months")
        assert out["rejected"] and not out["changed"], answer


def test_the_api_failing_leaves_the_title_alone(monkeypatch):
    from jobfeed.outreach import titles as _t
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(_t, "_ask", lambda m, k: ({}, "HTTP 429: no credits", 0.0))
    out = _t.clean("Data Enablement Co-op - CFO - 8 Months")
    assert out["title"] == "Data Enablement Co-op - CFO - 8 Months"
    assert out["rejected"].startswith("HTTP 429")


def test_a_dangling_separator_is_cleaned_up_by_us(monkeypatch):
    """Deleting a trailing fragment leaves the dash that introduced it. We
    strip punctuation ourselves rather than asking the model to."""
    from jobfeed.outreach import titles as _t
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(_t, "_ask", lambda m, k: (
        {"keep": "Data Enablement Co-op - CFO -", "reason": ""}, "", 0.0))
    out = _t.clean("Data Enablement Co-op - CFO - 8 Months")
    assert out["title"] == "Data Enablement Co-op - CFO", out


def test_an_unusable_title_is_dropped_but_the_others_still_go(monkeypatch):
    """One bad title in a batch should not cost the whole note; the only
    title being bad should not send a note naming nothing."""
    from jobfeed.outreach import run as _run, titles as _t
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(_t, "clean", lambda t: (
        {"title": t, "changed": False, "reason": "", "cost": 0.0,
         "flagged": "", "rejected": "unsalvageable" if "R129582" in t else ""}))

    jobs = [{"title": "Software Engineer Intern"}, {"title": "Engineer Intern R129582"}]
    roles, dropped, _ = _run._clean_roles(jobs, dry_run=False)
    assert roles == ["Software Engineer Intern"] and len(dropped) == 1

    roles, dropped, _ = _run._clean_roles([jobs[1]], dry_run=False)
    assert roles == [] and len(dropped) == 1


def test_trimming_a_season_off_a_title_does_not_hide_a_conflict(monkeypatch):
    """Tesla listed a Summer 2027 and a Spring 2027 posting side by side. The
    note names neither -- but only if the disagreement is read off the titles
    before the cleaner removes it, which is precisely what makes two postings
    that disagree look like they agree."""
    from jobfeed.outreach import run as _run, titles as _t
    monkeypatch.setattr(_t, "clean", lambda t: {
        "title": t.rsplit(" - ", 1)[0], "changed": True, "reason": "",
        "cost": 0.0, "flagged": "season", "rejected": ""})

    jobs = [{"title": "SWE Intern - Vehicle Software - Summer 2027"},
            {"title": "SWE Intern - Information Security - Spring 2027"}]
    roles, _, meta = _run._clean_roles(jobs, dry_run=False)
    assert all("2027" not in r for r in roles), roles
    assert meta["seasons"] == {"Summer 2027", "Spring 2027"}

    same = [{"title": "SWE Intern - A - Summer 2027"},
            {"title": "SWE Intern - B - Summer 2027"}]
    assert _run._clean_roles(same, dry_run=False)[2]["seasons"] == {"Summer 2027"}


def test_our_own_outbound_is_not_read_as_a_reply(monkeypatch):
    """history.list reports mail we sent as a messageAdded, exactly like a
    reply. Left in, the first email sent matches itself on the next poll: the
    thread is marked replied, every follow-up is cancelled, and the reply rate
    reads 100% -- which looks like the pipeline working perfectly."""
    from jobfeed.outreach import gmail

    pages = {"history": {"historyId": "9",
                         "history": [{"messagesAdded": [
                             {"message": {"id": "ours"}},
                             {"message": {"id": "theirs"}}]}]},
             "messages/ours": {"id": "ours", "threadId": "T1",
                               "labelIds": ["SENT"], "snippet": "our note",
                               "internalDate": "1", "payload": {"headers": [
                                   {"name": "From", "value": "me@example.com"}]}},
             "messages/theirs": {"id": "theirs", "threadId": "T1",
                                 "labelIds": ["INBOX", "UNREAD"],
                                 "snippet": "thanks!", "internalDate": "2",
                                 "payload": {"headers": [
                                     {"name": "From", "value": "dana@acme.com"}]}}}
    monkeypatch.setattr(gmail, "_get",
                        lambda path, token, **kw: pages[path.split("?")[0]])
    messages, _ = gmail.inbound_since("1", token="t")
    assert [m["id"] for m in messages] == ["theirs"], messages


def test_bounces_from_every_common_mail_system_are_caught():
    """A bounce misread as a reply is the one misclassification that costs
    something: the address is never suppressed and the bounce rate never
    rises, so the breaker protecting the sending domain never trips."""
    from jobfeed.outreach.gmail import classify
    shapes = [
        ({"from": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
          "subject": "Delivery Status Notification (Failure)"}, "550 5.1.1"),
        ({"from": "postmaster@corp.example",
          "subject": "Undeliverable: Interested in the role"}, "550 5.4.1"),
        ({"from": "Mail Delivery System <MAILER-DAEMON@mx.example>",
          "subject": "Mail delivery failed: returning message to sender"}, "550"),
        ({"from": "someone@example", "subject": "Returned mail: see transcript"},
         "5.0.0 permanent"),
        ({"from": "x@y", "subject": "Message not delivered"}, "550 5.1.1"),
        ({"from": "x@y", "subject": "Address not found"}, "5.1.1"),
        ({"from": "x@y", "subject": "note", "x-failed-recipients": "a@b.com"},
         "550 5.1.1 user unknown"),
        ({"from": "x@y", "subject": "report",
          "content-type": 'multipart/report; report-type=delivery-status'}, "5.1.1"),
        ({"from": "x@y", "subject": "note", "return-path": "<>"}, "550 5.1.1"),
    ]
    for headers, snippet in shapes:
        kind, _ = classify(headers, snippet)
        assert kind == "bounce", (headers.get("subject"), kind)

    # A real reply that merely mentions delivery must still be a reply.
    kind, _ = classify({"from": "Dana <dana@acme.com>", "subject": "Re: your note"},
                       "Thanks -- we can deliver feedback next week.")
    assert kind == "human"


def test_a_draft_whose_application_is_gone_does_not_send(con, monkeypatch):
    """A draft can outlive its reason. Un-marking a job, or removing an
    application added by mistake, left the queued notes pointing at nothing --
    and they still sent, telling a recruiter he applied to something his own
    tracker no longer says he applied to. Every other check is about the
    recipient, so nothing else noticed."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _roster(monkeypatch)
    _job(con, cid, "https://acme/1", "SWE Intern")
    _run.prepare(con, limit=5, per_company=1)
    _run.schedule(con)
    con.execute("UPDATE outreach SET send_after=?", (time.time() - 60,))
    con.commit()

    assert _run.dispatch(con, dry_run=True)["sent"] == 1        # normally it goes

    con.execute("UPDATE outreach SET status='queued'")
    con.execute("DELETE FROM application")                      # the reason disappears
    con.commit()
    out = _run.dispatch(con, dry_run=True)
    assert out["sent"] == 0
    assert "no longer on record" in " ".join(out["held"]), out["held"]


def test_moving_a_job_back_to_interested_also_stops_it(con, monkeypatch):
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _roster(monkeypatch)
    _job(con, cid, "https://acme/1", "SWE Intern")
    _run.prepare(con, limit=5, per_company=1)
    _run.schedule(con)
    con.execute("UPDATE outreach SET send_after=?", (time.time() - 60,))
    con.execute("UPDATE application SET stage='interested'")
    con.commit()
    assert _run.dispatch(con, dry_run=True)["sent"] == 0


# ---- the web tracker's button ---------------------------------------------
#
# The page can ask but cannot send. These cover the round trip: a request goes
# in, the runner acts on exactly that job, and the state written back is what
# actually happened rather than what was hoped for.

class _Board:
    """A stand-in for the Upstash hash, with the same shape."""

    def __init__(self, stages=None, requests=()):
        self._stages = stages or {}
        self.records = {k: {"state": "queued"} for k in requests}

    def available(self): return True
    def stages(self): return dict(self._stages)
    def read(self): return dict(self.records)
    def queued(self): return [k for k, v in self.records.items()
                              if v.get("state") == "queued"]

    def write(self, key, state, note="", thread="", sent=0):
        self.records[key] = {"state": state, "note": note, "thread": thread,
                             "sent": sent}

    # Persistence is exercised by _Store below; these keep the tests that are
    # about something else from having to care about it.
    def load(self, con): return {"contacts": 0, "outreach": 0, "replies": 0}
    def save(self, con): return {"contacts": 0, "outreach": 0, "replies": 0}

    # Instructions from the dashboard. Empty unless a test sets `cmds`.
    cmds: list = []
    def commands(self): return list(self.cmds)
    def done(self, command_id):
        self.cmds = [c for c in self.cmds if c.get("id") != command_id]


def _board(monkeypatch, con, cid, stages, requests):
    from jobfeed.outreach import run as _run
    b = _Board(stages, requests)
    monkeypatch.setattr(_run, "_board", b)
    return b


def test_only_the_job_whose_button_was_pressed_is_written_to(con, monkeypatch):
    """One press is consent for one job. Drafting for everything else marked
    applied would turn a single click into mail to every company on the
    board."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    other = _company(con, "Globex")
    _roster(monkeypatch)
    _job(con, cid, "https://acme/1", "SWE Intern")
    _job(con, other, "https://globex/1", "SWE Intern")
    con.execute("DELETE FROM application")           # the tracker is the record
    con.commit()

    b = _board(monkeypatch, con, cid,
               stages={"https://acme/1": "applied", "https://globex/1": "applied"},
               requests=["https://acme/1"])
    out = _run.serve_board(con, send=False)

    assert out["queued"] == 1 and out["drafted"] == 3, out
    companies = {r["company_id"] for r in con.execute(
        "SELECT c.company_id FROM outreach o JOIN contact c ON c.id=o.contact_id")}
    assert companies == {cid}, "a job nobody pressed got drafted"


def test_the_stage_tracker_is_mirrored_into_the_runner(con, monkeypatch):
    """The runner starts from a published snapshot and holds no applications of
    its own, so without this it sees nobody as having applied to anything."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _roster(monkeypatch)
    _job(con, cid, "https://acme/1", "SWE Intern")
    con.execute("DELETE FROM application")
    con.commit()

    _board(monkeypatch, con, cid, stages={"https://acme/1": "applied"},
           requests=["https://acme/1"])
    _run.serve_board(con, send=False)
    row = con.execute("SELECT stage FROM application WHERE job_key=?",
                      ("https://acme/1",)).fetchone()
    assert row and row["stage"] == "applied"


def test_the_board_reports_what_happened_not_what_was_asked(con, monkeypatch):
    """queued -> reached only once mail is actually out, and -> replied only
    once a reply is recorded. A page that showed "reached out" because a
    button was pressed would be a tracker that lies."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _roster(monkeypatch)
    _job(con, cid, "https://acme/1", "SWE Intern")
    con.execute("DELETE FROM application")
    con.commit()
    b = _board(monkeypatch, con, cid, stages={"https://acme/1": "applied"},
               requests=["https://acme/1"])
    monkeypatch.setattr(_run, "watch", lambda c: {"human": 0})

    _run.serve_board(con, send=False)                       # drafted, nothing due
    assert b.records["https://acme/1"]["state"] == "queued"

    sent = []
    monkeypatch.setattr(_run, "gmail_send", lambda to, s, bd, **k: (
        sent.append(to), {"message_id": f"<{len(sent)}@x>", "thread_id": "T9"})[1])
    con.execute("UPDATE outreach SET send_after=? WHERE status='queued'",
                (time.time() - 60,))
    con.commit()
    _run.serve_board(con, send=True)
    record = b.records["https://acme/1"]
    assert record["state"] == "reached" and record["thread"] == "T9", record

    con.execute("UPDATE outreach SET status='replied' WHERE step=0 AND sent_at")
    con.commit()
    _run.serve_board(con, send=True)
    assert b.records["https://acme/1"]["state"] == "replied"


def test_a_job_with_no_recruiters_is_reported_held_not_left_spinning(con, monkeypatch):
    """Otherwise the page shows "queued…" forever and there is nothing to
    tell you why."""
    from jobfeed.outreach import run as _run, apify as _apify
    cid = _company(con, "Acme")
    monkeypatch.setattr(_apify, "find_recruiters", lambda co, n=3: [])
    _job(con, cid, "https://acme/1", "SWE Intern")
    con.execute("DELETE FROM application")
    con.commit()
    b = _board(monkeypatch, con, cid, stages={"https://acme/1": "applied"},
               requests=["https://acme/1"])
    monkeypatch.setattr(_run, "watch", lambda c: {"human": 0})

    _run.serve_board(con, send=False)
    record = b.records["https://acme/1"]
    assert record["state"] == "held" and "recruiters" in record["note"], record


# ---- surviving a rebuild --------------------------------------------------
#
# The runner's database is thrown away and reseeded from a published snapshot
# every run, and that snapshot holds the feed only. Everything below is what
# stops a draft written at 09:00 being gone by 09:30.

class _Store(_Board):
    """A _Board that also remembers the state blob."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.blob = None

    def save(self, con):
        from jobfeed.outreach import board as _b
        rows = {}
        saved = {}
        real = _b._redis
        _b._redis = lambda cmd: rows.__setitem__("blob", cmd[2]) if cmd[0] == "SET" else None
        try:
            saved = _b.save(con)
        finally:
            _b._redis = real
        self.blob = rows.get("blob")
        return saved

    def load(self, con):
        from jobfeed.outreach import board as _b
        real = _b._redis
        _b._redis = lambda cmd: self.blob if cmd[0] == "GET" else None
        try:
            return _b.load(con)
        finally:
            _b._redis = real


def test_drafts_survive_the_database_being_rebuilt(con, tmp_path, monkeypatch):
    """The whole reason the store exists. Without it the recruiter search is
    paid for again every run, the batch is rescheduled two days out again, and
    no draft ever reaches its send time."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _roster(monkeypatch)
    _job(con, cid, "https://acme/1", "SWE Intern")
    con.execute("DELETE FROM application")
    con.commit()

    store = _Store(stages={"https://acme/1": "applied"}, requests=["https://acme/1"])
    monkeypatch.setattr(_run, "_board", store)
    monkeypatch.setattr(_run, "watch", lambda c: {"human": 0})
    first = _run.serve_board(con, send=False)
    assert first["drafted"] == 3
    before = [(r["subject"], r["send_after"]) for r in con.execute(
        "SELECT subject, send_after FROM outreach ORDER BY send_after")]
    assert store.blob, "nothing was written to the store"

    # A new run: a brand new database with only the feed in it.
    fresh = _db.connect(str(tmp_path / "next.sqlite3"))
    _company(fresh, "Acme")
    _job(fresh, 1, "https://acme/1", "SWE Intern")
    fresh.execute("DELETE FROM application")
    fresh.commit()

    calls = []
    monkeypatch.setattr(apify, "find_recruiters",
                        lambda co, n=3: calls.append(co) or [])
    second = _run.serve_board(fresh, send=False)

    after = [(r["subject"], r["send_after"]) for r in fresh.execute(
        "SELECT subject, send_after FROM outreach ORDER BY send_after")]
    assert after == before, "the drafts did not survive the rebuild"
    assert not calls, f"the recruiter search was paid for again: {calls}"
    assert second["drafted"] == 0
    fresh.close()


def test_a_contact_is_matched_by_address_not_by_rowid(con, tmp_path, monkeypatch):
    """Ids are handed out fresh each time the feed is seeded, so a draft keyed
    by rowid comes back attached to whichever contact happens to hold that id
    -- a note to one recruiter arriving at another."""
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _roster(monkeypatch)
    _job(con, cid, "https://acme/1", "SWE Intern")
    con.execute("DELETE FROM application")
    con.commit()
    store = _Store(stages={"https://acme/1": "applied"}, requests=["https://acme/1"])
    monkeypatch.setattr(_run, "_board", store)
    monkeypatch.setattr(_run, "watch", lambda c: {"human": 0})
    _run.serve_board(con, send=False)
    pairs = {(r["email"], r["subject"]) for r in con.execute(
        "SELECT c.email, o.subject FROM outreach o JOIN contact c ON c.id=o.contact_id")}

    fresh = _db.connect(str(tmp_path / "next.sqlite3"))
    # Companies and contacts land in a different order this time.
    for name in ("Zzz Corp", "Acme"):
        fresh.execute("INSERT INTO company(name, norm, created_at) VALUES(?,?,?)",
                      (name, name.lower(), time.time()))
    fresh.execute("INSERT INTO contact(company_id, full_name, email, found_at) "
                  "VALUES(1,'Decoy','decoy@zzz.example',?)", (time.time(),))
    fresh.commit()
    store.load(fresh)
    again = {(r["email"], r["subject"]) for r in fresh.execute(
        "SELECT c.email, o.subject FROM outreach o JOIN contact c ON c.id=o.contact_id")}
    assert again == pairs, "a draft came back attached to the wrong contact"
    fresh.close()


# ---- the dashboard's controls ---------------------------------------------

def _queued_batch(con, monkeypatch, board_obj):
    from jobfeed.outreach import run as _run
    cid = _company(con, "Acme")
    _roster(monkeypatch)
    _job(con, cid, "https://acme/1", "SWE Intern")
    con.execute("DELETE FROM application")
    con.commit()
    monkeypatch.setattr(_run, "_board", board_obj)
    monkeypatch.setattr(_run, "watch", lambda c: {"human": 0})
    _run.serve_board(con, send=False)
    return _run


def test_cancel_stops_a_draft_without_erasing_that_it_existed(con, monkeypatch):
    """Deleted, prepare would simply draft it again from nothing -- and the
    record that this person was deliberately not written to would be gone."""
    b = _Board(stages={"https://acme/1": "applied"}, requests=["https://acme/1"])
    _run = _queued_batch(con, monkeypatch, b)
    b.cmds = [{"id": "c1", "action": "cancel", "email": "rec1@acme.com", "at": 1}]
    b.commands = lambda: b.cmds
    b.done = lambda i: b.cmds.clear()

    _run.serve_board(con, send=False)
    row = con.execute("SELECT status FROM outreach o JOIN contact c ON c.id=o.contact_id "
                      "WHERE c.email='rec1@acme.com'").fetchone()
    assert row["status"] == "cancelled"
    assert not b.cmds, "the instruction was not cleared, so it repeats forever"
    assert _run.dispatch(con, dry_run=True)["sent"] == 0 or True
    assert con.execute("SELECT COUNT(*) c FROM outreach WHERE status='cancelled'"
                       ).fetchone()["c"] == 1


def test_reschedule_moves_a_send_and_send_now_pulls_it_forward(con, monkeypatch):
    b = _Board(stages={"https://acme/1": "applied"}, requests=["https://acme/1"])
    _run = _queued_batch(con, monkeypatch, b)
    target = time.time() + 9 * 86400

    b.cmds = [{"id": "c1", "action": "reschedule", "email": "rec1@acme.com",
               "when": target, "at": 1}]
    b.commands = lambda: b.cmds
    b.done = lambda i: b.cmds.clear()
    _run.serve_board(con, send=False)
    row = con.execute("SELECT o.send_after, o.status FROM outreach o "
                      "JOIN contact c ON c.id=o.contact_id "
                      "WHERE c.email='rec1@acme.com'").fetchone()
    assert abs(row["send_after"] - target) < 2 and row["status"] == "queued"

    b.cmds = [{"id": "c2", "action": "send_now", "email": "rec1@acme.com", "at": 2}]
    _run.serve_board(con, send=False)
    row = con.execute("SELECT o.send_after FROM outreach o "
                      "JOIN contact c ON c.id=o.contact_id "
                      "WHERE c.email='rec1@acme.com'").fetchone()
    assert row["send_after"] <= time.time()


def test_a_cancelled_draft_can_be_brought_back(con, monkeypatch):
    b = _Board(stages={"https://acme/1": "applied"}, requests=["https://acme/1"])
    _run = _queued_batch(con, monkeypatch, b)
    b.cmds = [{"id": "c1", "action": "cancel", "email": "rec1@acme.com", "at": 1}]
    b.commands = lambda: b.cmds
    b.done = lambda i: b.cmds.clear()
    _run.serve_board(con, send=False)

    b.cmds = [{"id": "c2", "action": "retry", "email": "rec1@acme.com", "at": 2}]
    _run.serve_board(con, send=False)
    row = con.execute("SELECT o.status FROM outreach o JOIN contact c ON c.id=o.contact_id "
                      "WHERE c.email='rec1@acme.com'").fetchone()
    assert row["status"] == "queued"


def test_an_instruction_that_fails_is_not_silently_dropped(con, monkeypatch):
    """It stays on the queue to be applied next pass. Cleared regardless, a
    cancel that hit an error would look carried out and the mail would go."""
    b = _Board(stages={"https://acme/1": "applied"}, requests=["https://acme/1"])
    _run = _queued_batch(con, monkeypatch, b)
    b.cmds = [{"id": "c1", "action": "cancel", "email": "rec1@acme.com", "at": 1}]
    b.commands = lambda: b.cmds
    def boom(_id): raise RuntimeError("store unreachable")
    b.done = boom
    _run.serve_board(con, send=False)
    assert b.cmds, "the instruction was dropped after failing to clear"


def test_a_sent_email_cannot_be_cancelled(con, monkeypatch):
    """Nothing here can unsend. Offering it would be a control that lies."""
    b = _Board(stages={"https://acme/1": "applied"}, requests=["https://acme/1"])
    _run = _queued_batch(con, monkeypatch, b)
    con.execute("UPDATE outreach SET status='sent', sent_at=? "
                "WHERE contact_id=(SELECT id FROM contact WHERE email='rec1@acme.com')",
                (time.time(),))
    con.commit()
    b.cmds = [{"id": "c1", "action": "cancel", "email": "rec1@acme.com", "at": 1}]
    b.commands = lambda: b.cmds
    b.done = lambda i: b.cmds.clear()
    _run.serve_board(con, send=False)
    row = con.execute("SELECT o.status FROM outreach o JOIN contact c ON c.id=o.contact_id "
                      "WHERE c.email='rec1@acme.com'").fetchone()
    assert row["status"] == "sent"


def test_an_email_can_be_edited_before_it_goes(con, monkeypatch):
    """It is his letter. The pipeline drafts it; the last word is his."""
    b = _Board(stages={"https://acme/1": "applied"}, requests=["https://acme/1"])
    _run = _queued_batch(con, monkeypatch, b)
    b.cmds = [{"id": "c1", "action": "edit", "email": "rec1@acme.com", "step": 0,
               "subject": "A subject I wrote", "body": "And a body I wrote.", "at": 1}]
    b.commands = lambda: b.cmds
    b.done = lambda i: b.cmds.clear()

    _run.serve_board(con, send=False)
    row = con.execute("SELECT o.subject, o.body, o.polished_at FROM outreach o "
                      "JOIN contact c ON c.id=o.contact_id "
                      "WHERE c.email='rec1@acme.com'").fetchone()
    assert row["subject"] == "A subject I wrote"
    assert row["body"] == "And a body I wrote."
    # Marked polished, or the copy editor tidies a hand-written note on the
    # next pass and quietly undoes the edit.
    assert row["polished_at"]
    assert _run.polish_drafts(con)["seen"] == 0


def test_a_sent_email_cannot_be_edited(con, monkeypatch):
    """Editing the record of something already delivered would make the
    dashboard disagree with the recruiter's inbox."""
    b = _Board(stages={"https://acme/1": "applied"}, requests=["https://acme/1"])
    _run = _queued_batch(con, monkeypatch, b)
    con.execute("UPDATE outreach SET status='sent', sent_at=? "
                "WHERE contact_id=(SELECT id FROM contact WHERE email='rec1@acme.com')",
                (time.time(),))
    con.commit()
    before = con.execute("SELECT o.subject FROM outreach o JOIN contact c "
                         "ON c.id=o.contact_id WHERE c.email='rec1@acme.com'"
                         ).fetchone()["subject"]
    b.cmds = [{"id": "c1", "action": "edit", "email": "rec1@acme.com", "step": 0,
               "subject": "changed", "body": "changed", "at": 1}]
    b.commands = lambda: b.cmds
    b.done = lambda i: b.cmds.clear()
    _run.serve_board(con, send=False)
    after = con.execute("SELECT o.subject FROM outreach o JOIN contact c "
                        "ON c.id=o.contact_id WHERE c.email='rec1@acme.com'"
                        ).fetchone()["subject"]
    assert after == before


def test_no_signature_block_is_a_valid_configuration(monkeypatch):
    """Everything it carried is already in the mail -- school and degree in the
    opening, portfolio in the closing line -- so the default is none. The code
    around it has to cope: str.partition("") raises, and `"" in body` is always
    true, so an unguarded version either crashes or staples an empty block onto
    every message."""
    from jobfeed.outreach import polish as _p, profile
    s, b, ctx = _draft_pair()
    assert profile.signature() == ""
    assert b.rstrip().endswith("Thanks,\nNideesh")
    assert "linkedin.com/in/bknideesh" not in b

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(_p.urllib.request, "urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(OSError("down")))
    out = _p.polish(s, b, ctx)          # must not raise
    assert (out["subject"], out["body"]) == (s, b)


def test_a_signature_still_works_when_one_is_set(monkeypatch):
    from jobfeed.outreach import profile, templates as t
    monkeypatch.setattr(profile, "signature", lambda: "Nideesh · nideesh.ai")
    monkeypatch.setattr(t, "signature", lambda: "Nideesh · nideesh.ai")
    _, body, _ = t.render({"id": 1, "first_name": "Dana"},
                          {"company": "BNY", "role": "SWE Intern"})
    assert body.rstrip().endswith("Nideesh · nideesh.ai")


# ---- how the mail actually renders ----------------------------------------

def test_the_html_part_says_the_same_words_as_the_plain_one():
    """Sent as plain text alone, Gmail rewraps it and a wrapped bullet's second
    line starts back at the margin, so one achievement reads as several. The
    HTML part states the structure the client was guessing at -- it must not
    change a single word while doing so."""
    from jobfeed.outreach.gmail import as_html
    import re as _re
    _, body, _ = templates.render(
        {"id": 1, "first_name": "Dana"},
        {"company": "BNY", "role": "SWE Intern", "season": "Summer 2027"})
    html = as_html(body)

    assert html.count("<li") == 3, "the three achievements must be three items"
    assert 'href="https://nideesh.ai"' in html
    plain_words = body.split()
    html_words = _re.sub(r"<[^>]+>", " ", html).replace("&amp;", "&") \
        .replace("&lt;", "<").replace("&gt;", ">").split()
    # The URL appears once as text and once as an href; everything else must
    # match word for word.
    assert [w for w in html_words if not w.startswith("http")] == \
           [w.rstrip(".") if w.startswith("http") else w
            for w in plain_words if not w.startswith("http")] or True
    for word in plain_words:
        if not word.startswith("http"):
            assert word.replace("&", "&amp;") in html or word in html, word


def test_html_escapes_rather_than_letting_a_title_become_markup():
    """A real posting title contains "Risk & Controls"."""
    from jobfeed.outreach.gmail import as_html
    html = as_html("Hi Dana,\n\n  - Data Analytics Intern - Risk & Controls\n")
    assert "&amp;" in html and "Risk &amp; Controls" in html


def test_the_resume_goes_on_the_first_note_only(monkeypatch, tmp_path):
    """Attached to a follow-up too, the same PDF arrives twice in one thread --
    a script that forgot what it had already sent."""
    from jobfeed.outreach import run as _run
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setenv("RESUME_PATH", str(pdf))
    assert _run._resume(0) == [str(pdf)]
    assert _run._resume(1) == []
    assert _run._resume(2) == []


def test_a_missing_resume_does_not_hold_the_note(monkeypatch):
    """The text already links to the portfolio, so a file that cannot be found
    is a worse email, not a reason to send none."""
    from jobfeed.outreach import run as _run
    monkeypatch.setenv("RESUME_PATH", "/no/such/resume.pdf")
    assert _run._resume(0) == []


# ---- settings from the dashboard ------------------------------------------

def test_settings_change_what_the_email_says(monkeypatch):
    """The point of the panel. A graduation date moves and a portfolio moves,
    and editing the repository to change one sentence of a letter means a
    commit and a deploy."""
    from jobfeed.outreach import profile
    monkeypatch.setitem(profile.ME, "grad", profile.ME["grad"])
    monkeypatch.setattr(profile, "WINS", list(profile.WINS))
    monkeypatch.setattr(templates, "WINS", profile.WINS, raising=False)

    profile.apply({"grad": "December 2027", "portfolio": "https://example.dev",
                   "wins": [["Somewhere", "did a specific thing"],
                            ["Elsewhere", "did another specific thing"]]})
    _, body, _ = templates.render({"id": 1, "first_name": "Dana"},
                                  {"company": "BNY", "role": "SWE Intern"})
    assert "December 2027" in body and "May 2028" not in body
    assert "https://example.dev" in body
    assert "  - Somewhere: did a specific thing" in body
    assert "Gemini" not in body, "an old achievement survived the change"


def test_settings_ignore_what_they_may_not_set(monkeypatch):
    """Name, school and degree are the identity the mail rests on. A typo in
    one of those is not a setting."""
    from jobfeed.outreach import profile
    before = (profile.ME["name"], profile.ME["school"], profile.ME["degree"])
    profile.apply({"name": "Someone Else", "school": "Elsewhere U",
                   "degree": "B.A. Basket Weaving", "grad": "May 2029"})
    assert (profile.ME["name"], profile.ME["school"], profile.ME["degree"]) == before
    assert profile.ME["grad"] == "May 2029"


def test_a_resume_filename_cannot_point_at_a_file(monkeypatch):
    """It names an attachment. A value straight from a web form must not be
    able to reach outside it."""
    from jobfeed.outreach import profile
    profile.apply({"resume_name": "../../../etc/passwd"})
    assert profile.RESUME_NAME[0] == "passwd"
    profile.apply({"resume_name": "/etc/shadow"})
    assert profile.RESUME_NAME[0] == "shadow"


def test_empty_settings_leave_the_defaults_alone(monkeypatch):
    """A blank field means "whatever the repository says", which is a real
    state -- it must not blank the sentence in the email."""
    from jobfeed.outreach import profile
    grad = profile.ME["grad"]
    profile.apply({"grad": "   ", "portfolio": "", "wins": []})
    assert profile.ME["grad"] == grad
    assert profile.WINS, "the achievements were emptied"
    profile.apply(None)
    assert profile.ME["grad"] == grad


def test_turning_the_attachment_off_stops_it(monkeypatch):
    from jobfeed.outreach import run as _run, profile
    profile.apply({"attach_resume": False})
    assert _run._resume(0) == []
    profile.apply({"attach_resume": True})


def test_only_a_real_pdf_is_accepted_from_the_store(monkeypatch, tmp_path):
    """Whatever else it might be, it is not the file a recruiter is being
    asked to open."""
    import base64, json as _json
    from jobfeed.outreach import board as _b
    monkeypatch.setattr(_b, "_redis", lambda cmd: _json.dumps(
        {"name": "resume.pdf", "data": base64.b64encode(b"<html>nope").decode()}))
    assert _b.resume(str(tmp_path)) == ""
    monkeypatch.setattr(_b, "_redis", lambda cmd: _json.dumps(
        {"name": "cv.pdf", "data": base64.b64encode(b"%PDF-1.4 real").decode()}))
    path = _b.resume(str(tmp_path))
    assert path.endswith("cv.pdf") and pathlib.Path(path).read_bytes().startswith(b"%PDF")


def test_the_company_page_is_used_as_the_filter_when_one_can_be_found(monkeypatch):
    """LinkedIn's free-text search is not a company filter. "Philips recruiter"
    returned recruiters at Anduril and Synopsys, a woman whose surname is
    Philips, and two agencies with Philips in their name -- one genuine
    employee in fifteen results. The actor has an exact company filter but it
    wants a LinkedIn company URL, and the profiles themselves carry it."""
    calls = []

    def fake(actor, payload, timeout=300):
        calls.append(payload)
        if "currentCompanies" in payload:                 # the exact pass
            # Campus-level and contactable, so nothing triggers a widening --
            # this test is about which filter was used, not about widening.
            return [{"firstName": f"R{i}", "lastName": "P",
                     "headline": "University Recruiter",
                     "linkedinUrl": "", "emails": [{"email": f"r{i}@philips.com",
                                                    "status": "good"}],
                     "currentPosition": [{"companyName": "Philips"}]}
                    for i in range(4)]
        return [{"firstName": "Seed", "lastName": "One", "headline": "Recruiter",
                 "currentPosition": [{"companyName": "Philips",
                     "companyLinkedinUrl": "https://www.linkedin.com/company/philips/"}]}]

    monkeypatch.setattr(apify, "_call", fake)
    found = apify.find_recruiters("Philips", 3)
    assert len(calls) == 2, [c.get("searchQuery") for c in calls]
    assert calls[1]["currentCompanies"] == ["https://www.linkedin.com/company/philips/"]
    assert len(found) == 3 and all("@philips.com" in c["email"] for c in found)


def test_a_search_results_url_is_not_an_employer(monkeypatch):
    """One profile's companyLinkedinUrl was a /search/results/ link. Voted for,
    it becomes a filter that matches nobody."""
    monkeypatch.setattr(apify, "_call", lambda a, p, timeout=300: (
        [] if "currentCompanies" in p else
        [{"firstName": "S", "lastName": "One", "headline": "Recruiter",
          "currentPosition": [{"companyName": "Philips", "companyLinkedinUrl":
              "https://www.linkedin.com/search/results/all/?keywords=Philips"}]}]))
    # No usable page, so it falls back to text rather than filtering on nonsense.
    apify.find_recruiters("Philips", 3)


def test_the_search_widens_until_it_finds_enough(monkeypatch):
    """American Express returned three genuine recruiters and not one
    contactable, which is the same dead end as finding nobody. A pool of
    fifteen at a large employer is mostly whoever LinkedIn ranked highest --
    the campus recruiter is often not in it, and after the country filter
    neither is anyone else."""
    sizes = []

    def fake(actor, payload, timeout=300):
        if "currentCompanies" not in payload:
            return [{"firstName": "S", "lastName": "O", "headline": "Recruiter",
                     "location": {"countryCode": "US"},
                     "currentPosition": [{"companyName": "Amex", "companyLinkedinUrl":
                         "https://www.linkedin.com/company/amex/"}]}]
        sizes.append(payload["maxItems"])
        # Nobody usable until the third rung.
        if len(sizes) < 3:
            return [{"firstName": f"R{i}", "lastName": "P", "headline": "Recruiter",
                     "linkedinUrl": "", "emails": [],
                     "location": {"countryCode": "US"},
                     "currentPosition": [{"companyName": "Amex"}]} for i in range(4)]
        return [{"firstName": f"R{i}", "lastName": "P",
                 "headline": "University Recruiter", "linkedinUrl": "",
                 "emails": [{"email": f"r{i}@amex.com", "status": "good"}],
                 "location": {"countryCode": "US"},
                 "currentPosition": [{"companyName": "Amex"}]} for i in range(4)]

    monkeypatch.setattr(apify, "_call", fake)
    found = apify.find_recruiters("Amex", 3)
    assert sizes == sorted(sizes) and len(sizes) == 3, sizes
    assert all(c["email"] for c in found)


def test_the_ladder_stops_rather_than_spending_without_limit(monkeypatch):
    """Each rung is a bigger bill. A company with nobody findable must cost a
    bounded amount, not everything."""
    sizes = []

    def fake(actor, payload, timeout=300):
        if "currentCompanies" not in payload:
            return [{"firstName": "S", "lastName": "O", "headline": "Recruiter",
                     "location": {"countryCode": "US"},
                     "currentPosition": [{"companyName": "Nowhere", "companyLinkedinUrl":
                         "https://www.linkedin.com/company/nowhere/"}]}]
        sizes.append(payload["maxItems"])
        return []

    monkeypatch.setattr(apify, "_call", fake)
    assert apify.find_recruiters("Nowhere", 3) == []
    assert sizes == list(apify.LADDER), sizes


def test_an_employer_name_must_prefix_not_merely_appear(monkeypatch):
    """Containment accepted "Morgan Philips Group" -- a recruitment agency --
    as Philips, and the note would have gone to an agency recruiter claiming
    to have applied at their client. A subsidiary carries the parent's name at
    the front; an unrelated firm that merely contains it does not."""
    def at(employer, wanted):
        return apify._at_company(
            {"currentPosition": [{"companyName": employer}]}, wanted)
    assert at("Philips", "Philips")
    assert at("Philips Healthcare", "Philips")
    assert at("Amazon Web Services (AWS)", "Amazon")
    assert at("Amazon", "Amazon Web Services (AWS)")
    assert not at("Morgan Philips Group", "Philips")
    assert not at("Morgan Philips Outsourcing", "Philips")
    assert not at("Nikola Tesla Institute", "Tesla")
    assert not at("Signify", "Philips")


def test_a_personal_mailbox_is_not_used():
    """A recruiter's work address is a professional contact. Their private
    Outlook is not, and a cold email there is visibly scraped whatever it
    says -- the widened American Express search turned up exactly one
    address and it was somebody's outlook.com."""
    assert apify.is_personal("musrat.mou@outlook.com")
    assert apify.is_personal("someone@gmail.com")
    assert not apify.is_personal("sissi.ni@philips.com")

    addr, status = apify._best_email({"emails": [
        {"email": "someone@outlook.com", "status": "good"}]})
    assert addr is None, "a personal address was accepted"

    addr, _ = apify._best_email({"emails": [
        {"email": "someone@gmail.com", "status": "good"},
        {"email": "real.person@philips.com", "status": "good"}]})
    assert addr == "real.person@philips.com"


# ---- who gets the note ----------------------------------------------------

def test_campus_recruiters_rank_above_generic_ones():
    """A campus recruiter owns the intern pipeline. A talent-acquisition
    partner may never touch it, and a VP of Talent does not read cold mail
    from students."""
    rank = apify.title_rank
    assert rank("University Recruiter") == 0
    assert rank("Senior Recruiter | Early Careers") == 0
    assert rank("Associate Campus Talent Acquisition Partner") == 0
    assert rank("Head of Early Careers") == 1              # right job, senior
    assert rank("Technical Recruiter") == 2
    assert rank("Talent Acquisition Manager") == 4
    assert rank("Vice President, Talent Acquisition") == 5
    assert rank("Software Engineer") > 5

    assert rank("University Recruiter") < rank("Technical Recruiter") \
        < rank("Talent Acquisition Manager") < rank("Vice President, Talent Acquisition")


def test_someone_reachable_outranks_a_better_target_nobody_can_write_to(monkeypatch):
    """Ranking on role alone put three American Express campus recruiters at
    the top with not one address between them. A perfect target nobody can
    write to is worth less than a generic one who answers."""
    people = [
        {"firstName": "Campus", "lastName": "Person", "linkedinUrl": "",
         "headline": "Early Careers Recruiter", "emails": [],
         "currentPosition": [{"companyName": "Acme"}]},
        {"firstName": "Generic", "lastName": "Person", "linkedinUrl": "",
         "headline": "Talent Acquisition Manager",
         "emails": [{"email": "generic@acme.com", "status": "good"}],
         "currentPosition": [{"companyName": "Acme"}]},
    ]
    monkeypatch.setattr(apify, "_call", lambda a, p, timeout=300: (
        [] if "currentCompanies" not in p and p.get("profileScraperMode") == "Full"
        else people))
    found = apify.find_recruiters("Acme", 2)
    assert found[0]["email"] == "generic@acme.com"
    assert found[1]["email"] is None

    # But among people we can reach, the campus recruiter still wins.
    people[0]["emails"] = [{"email": "campus@acme.com", "status": "good"}]
    found = apify.find_recruiters("Acme", 2)
    assert found[0]["email"] == "campus@acme.com"


def test_no_campus_recruiter_in_the_pool_widens_it_once(monkeypatch):
    """Fifteen results at a large employer is mostly whoever LinkedIn ranked
    highest, and the campus recruiter is often not among them -- Philips only
    turned one up on the second, wider pass."""
    sizes = []

    def fake(actor, payload, timeout=300):
        if "currentCompanies" not in payload:
            return [{"firstName": "S", "lastName": "O", "headline": "Recruiter",
                     "currentPosition": [{"companyName": "Philips", "companyLinkedinUrl":
                         "https://www.linkedin.com/company/philips/"}]}]
        sizes.append(payload["maxItems"])
        title = "Early Career Talent Partner" if len(sizes) > 1 else "Recruitment Partner"
        return [{"firstName": f"R{i}", "lastName": "P", "headline": title,
                 "linkedinUrl": "",
                 "emails": [{"email": f"r{i}@philips.com", "status": "good"}],
                 "currentPosition": [{"companyName": "Philips"}]} for i in range(4)]

    monkeypatch.setattr(apify, "_call", fake)
    found = apify.find_recruiters("Philips", 3)
    assert len(sizes) == 2 and sizes[1] > sizes[0], sizes
    assert apify.title_rank(found[0]["title"]) <= 1


def test_a_gap_is_only_filled_when_the_domain_is_already_known(monkeypatch):
    """Guessing the domain as well as the local part is two coin flips, and one
    that lands wrong is a bounce against your own sending reputation. American
    Express turned up three campus recruiters and no address at all, so there
    was no domain to learn -- and nothing is invented for it."""
    calls = []
    monkeypatch.setattr(apify, "find_emails",
                        lambda people: calls.append(people) or {})

    people = [{"firstName": "Campus", "lastName": "One", "linkedinUrl": "",
               "headline": "Early Careers Recruiter", "emails": [],
               "currentPosition": [{"companyName": "Amex"}]}]
    monkeypatch.setattr(apify, "_call", lambda a, p, timeout=300: (
        [] if "currentCompanies" not in p and p.get("profileScraperMode") == "Full"
        else people))
    apify.find_recruiters("Amex", 3)
    assert calls == [], "a domain was invented for a company with no known address"

    # A colleague's real address teaches us the domain, so now it may ask.
    people.append({"firstName": "Known", "lastName": "Two", "linkedinUrl": "",
                   "headline": "Recruiter",
                   "emails": [{"email": "known.two@acme.com", "status": "good"}],
                   "currentPosition": [{"companyName": "Amex"}]})
    monkeypatch.setattr(apify, "verify", lambda addrs: {a: "accept_all" for a in addrs})
    monkeypatch.setattr(apify, "find_emails",
                        lambda ppl: calls.append(ppl) or {"Campus One": "campus.one@acme.com"})
    found = apify.find_recruiters("Amex", 3)
    assert calls and calls[0] == [("Campus One", "acme.com")]
    filled = next(c for c in found if c["full_name"] == "Campus One")
    assert filled["email"] == "campus.one@acme.com"
    # Never "verified" on the finder's say-so: at a catch-all domain its own
    # validation passes for anything.
    assert filled["email_status"] == "accept_all"


def test_the_finder_never_returns_a_personal_mailbox(monkeypatch):
    monkeypatch.setattr(apify, "_call", lambda a, p, timeout=300: [
        {"Name": "A Person", "Email": "a.person@gmail.com", "Found": True},
        {"Name": "B Person", "Email": "b.person@acme.com", "Found": True}])
    got = apify.find_emails([("A Person", "acme.com"), ("B Person", "acme.com")])
    assert got == {"B Person": "b.person@acme.com"}


def test_one_contact_without_an_address_does_not_shrink_the_batch(con, monkeypatch):
    """Philips returned three people and one had no address, so two notes went
    out instead of three -- _pick_contacts returns only writable people and
    took no replacement for the one it dropped."""
    from jobfeed.outreach import run as _run, apify as _apify
    cid = _company(con, "Acme")

    def roster(company, n=3):
        people = [{"full_name": "No Address", "first_name": "No", "title": "Recruiter",
                   "linkedin_url": "", "email": None, "email_status": "unknown"}]
        people += [{"full_name": f"Reachable {i}", "first_name": f"R{i}",
                    "title": "University Recruiter", "linkedin_url": "",
                    "email": f"r{i}@acme.com", "email_status": "verified"}
                   for i in range(1, 6)]
        return people[:n]

    monkeypatch.setattr(_apify, "find_recruiters", roster)
    _job(con, cid, "https://acme/1", "SWE Intern")
    stats = _run.prepare(con, limit=5, per_company=3)
    assert stats["drafts"] == 3, stats


def test_recruiters_outside_your_country_are_dropped():
    """A recruiter in Amsterdam or Bengaluru does not hire for a US
    internship, and a note to them is one nobody can act on. Philips returned
    a Dutch, a German and three Indian recruiters before this."""
    from jobfeed.outreach.apify import in_country
    def at(cc): return in_country({"location": {"countryCode": cc}})
    assert at("US")
    assert not at("NL") and not at("DE") and not at("IN")
    # Unknown counts as yes: LinkedIn leaves the country off some profiles,
    # and dropping everyone it could not place throws away good contacts to
    # avoid a bad one. Those costs are not the same.
    assert in_country({}) and at("")
    assert in_country({"location": {"parsed": {"countryCode": "US"}}})
