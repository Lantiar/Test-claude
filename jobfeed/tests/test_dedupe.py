"""The rules that stop two jobs becoming one. These are the load-bearing ones:
a missed match shows a duplicate, a false match deletes a job."""
import time

import pytest

from jobfeed import db as _db
from jobfeed import dedupe
from jobfeed.models import RawListing


@pytest.fixture
def con(tmp_path):
    c = _db.connect(str(tmp_path / "t.sqlite3"))
    yield c
    c.close()


def listing(**kw):
    base = dict(source="simplify", source_record_id="x", url="", title="",
                company="Acme")
    base.update(kw)
    return RawListing(**base)


def test_the_same_listing_twice_is_one_job(con):
    a = listing(source_record_id="1", url="https://jobs.lever.co/acme/2f1c261d-9b65-412b-9f17-34b8968bdd78",
                title="Software Engineer Intern")
    first, _ = dedupe.record(con, a)
    second, how = dedupe.record(con, listing(
        source_record_id="2", url=a.url + "/apply?utm_source=x",
        title="Software Engineer Intern"))
    assert first == second and how == "ats"


def test_different_requisitions_never_merge_on_text(con):
    # AMD 90925 (San Jose) and 90926 (Secaucus): the titles differ by one
    # letter and they are two jobs. This merged 55 pairs before the rule.
    one, _ = dedupe.record(con, listing(
        source_record_id="a", url="https://careers.amd.com/jobs/90925?icims=1",
        title="Research Engineer Intern/Co-op - AMD Research", company="AMD"))
    two, how = dedupe.record(con, listing(
        source_record_id="b", url="https://careers.amd.com/jobs/90926?icims=1",
        title="Research Engineering Intern/Co-op - AMD Research", company="AMD"))
    assert one != two, "two live requisitions were merged into one"


def test_a_phd_variant_is_a_different_job(con):
    one, _ = dedupe.record(con, listing(
        source_record_id="a", url="https://jobs.apple.com/en-us/details/200664221",
        title="Machine Learning and Artificial Intelligence Intern", company="Apple"))
    two, _ = dedupe.record(con, listing(
        source_record_id="b", url="https://jobs.apple.com/en-us/details/200664223",
        title="Machine Learning and Artificial Intelligence PhD Intern", company="Apple"))
    assert one != two


def test_a_listing_with_no_link_can_still_join_one_that_has_one(con):
    # This is what the text tier is for: a story names a job and gives a
    # shortened link, Simplify has the same job with the employer's own URL.
    known, _ = dedupe.record(con, listing(
        source_record_id="a", url="https://jobs.ashbyhq.com/acme/3f1c261d-9b65-412b-9f17-34b8968bdd78",
        title="Backend Engineer Intern", season="Summer 2027"))
    same, how = dedupe.record(con, listing(
        source="instagram", source_record_id="b", url="",
        title="Backend Engineer Intern", season="Summer 2027"))
    assert same == known and how == "text"


def test_seasons_that_disagree_are_different_postings(con):
    one, _ = dedupe.record(con, listing(source_record_id="a", url="",
                                        title="Data Intern", season="Summer 2027"))
    two, _ = dedupe.record(con, listing(source_record_id="b", url="",
                                        title="Data Intern", season="Spring 2027"))
    assert one != two


def test_a_real_posted_date_replaces_an_estimate(con):
    when = time.time() - 86400 * 30
    jid, _ = dedupe.record(con, listing(source_record_id="a", url="",
                                        title="Ops Intern", posted_at=None))
    row = con.execute("SELECT * FROM job WHERE id=?", (jid,)).fetchone()
    assert row["posted_at_is_estimate"] == 1

    dedupe.record(con, listing(source_record_id="b", url="", title="Ops Intern",
                               posted_at=when))
    row = con.execute("SELECT * FROM job WHERE id=?", (jid,)).fetchone()
    assert row["posted_at_is_estimate"] == 0
    assert abs(row["posted_at"] - when) < 1


def test_every_sighting_is_kept(con):
    for i in range(3):
        dedupe.record(con, listing(
            source_record_id=str(i),
            url="https://jobs.lever.co/acme/4f1c261d-9b65-412b-9f17-34b8968bdd78",
            title="SWE Intern"), now=time.time() + i)
    assert con.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM sighting").fetchone()[0] == 3


def test_the_answer_does_not_depend_on_which_source_is_polled_first(con):
    """A story link creates a row with no title; Simplify must fill it in.

    Polled the other way round this always worked, which is why it survived:
    five jobs present in both sources stayed permanently blank whenever the
    story feed happened to run first.
    """
    url = "https://jobs.ashbyhq.com/acme/5f1c261d-9b65-412b-9f17-34b8968bdd78"
    bare, _ = dedupe.record(con, listing(
        source="instagram", source_record_id="s1", url=url, title="", company=""))
    full, how = dedupe.record(con, listing(
        source_record_id="s2", url=url, title="Backend Engineer Intern",
        company="Acme", locations=["NYC"]))
    assert full == bare and how == "ats"
    row = con.execute("SELECT * FROM job WHERE id=?", (bare,)).fetchone()
    assert row["title"] == "Backend Engineer Intern"
    assert row["company_id"] is not None


def test_a_snapshot_round_trip_keeps_every_job_and_its_first_seen_date(con, tmp_path):
    """A fresh runner restores from the published snapshot, not from a cache.

    Two things must survive: story-only jobs, which cannot be refetched from
    anywhere once the story expires, and first_seen_at, which cannot be
    reconstructed at all. Losing either looks exactly like a healthy run.
    """
    import json

    from jobfeed import publish as _publish
    from jobfeed import seed as _seed

    old = time.time() - 86400 * 9
    story, _ = dedupe.record(con, listing(
        source="instagram", source_record_id="s1",
        url="https://jobs.ashbyhq.com/acme/6f1c261d-9b65-412b-9f17-34b8968bdd78",
        title="Robotics Intern", company="Acme", posted_at=old,
        posted_at_is_real=False), now=old)

    out = tmp_path / "site"
    _publish.publish(con, str(out))

    fresh = _db.connect(str(tmp_path / "fresh.sqlite3"))
    try:
        report = _seed.seed(fresh, str(out / "jobs.json"))
        assert report["restored"] == 1
        row = fresh.execute("SELECT * FROM job").fetchone()
        assert row["title"] == "Robotics Intern"
        assert row["ats_key"] == "ashby::6f1c261d-9b65-412b-9f17-34b8968bdd78"
        assert abs(row["first_seen_at"] - old) < 1, "first_seen_at was redated"
        assert row["posted_at_is_estimate"] == 1, "an estimate came back as real"
        # And seeding twice must not duplicate it.
        assert _seed.seed(fresh, str(out / "jobs.json"))["restored"] == 0
    finally:
        fresh.close()
