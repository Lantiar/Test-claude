"""Postings pinned by hand.

The bug this exists for: Lyft's Summer 2027 software internships are on Lyft's
own Greenhouse board and in neither Simplify nor the story feed, so an
application to one had no row to attach to. The outreach that was queued
attached to Lyft's full-time Software Engineer role instead -- a different job
with a different letter -- and two drafts naming the wrong role reached the
send queue.
"""
from __future__ import annotations

import json

import pytest

from jobfeed import db as _db
from jobfeed.ingest import poll
from jobfeed.sources import load_all, manual


@pytest.fixture
def con(tmp_path):
    c = _db.connect(str(tmp_path / "t.sqlite3"))
    yield c
    c.close()


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    path = tmp_path / "manual.json"
    path.write_text(json.dumps([{
        "url": "https://app.careerpuck.com/job-board/lyft/job/8797837002?gh_jid=8797837002",
        "title": "Software Engineer Intern, Fullstack (Summer 2027)",
        "company": "Lyft",
        "locations": ["New York, NY"],
        "season": "Summer 2027",
    }]))
    monkeypatch.setattr(manual, "PATH", path)
    return path


def _jobs(con):
    return [dict(r) for r in con.execute(
        "SELECT j.title, j.ats_key, c.name company FROM job j "
        "LEFT JOIN company c ON c.id=j.company_id")]


def test_a_pinned_posting_becomes_a_job(con, pinned):
    load_all()
    counts = poll(con, "manual")
    assert counts["new"] == 1, counts
    job = _jobs(con)[0]
    assert job["company"] == "Lyft"
    assert job["title"] == "Software Engineer Intern, Fullstack (Summer 2027)"
    # The identity that matters: outreach is keyed by it, and it is what makes
    # a second poll a match rather than a duplicate.
    assert job["ats_key"] == "greenhouse::8797837002", job["ats_key"]


def test_polling_twice_does_not_make_two_jobs(con, pinned):
    """The file is re-yielded on every poll -- that is the whole point of it
    being a file -- so the run after the one that added it must match rather
    than insert. A pinned posting that duplicated itself hourly would be worse
    than the missing row it replaced."""
    load_all()
    poll(con, "manual")
    second = poll(con, "manual")
    assert second["new"] == 0, second
    assert len(_jobs(con)) == 1


def test_the_posting_survives_the_rebuild_that_lost_it(con, pinned, tmp_path):
    """The runner is stateless: the published snapshot is seeded into a fresh
    database every run. A job added once to a database is gone the moment that
    database is thrown away, which is why this is a file and not an insert."""
    load_all()
    poll(con, "manual")
    con.commit()

    fresh = _db.connect(str(tmp_path / "rebuilt.sqlite3"))
    try:
        assert _jobs(fresh) == [], "a new database should start empty"
        poll(fresh, "manual")
        assert [j["title"] for j in _jobs(fresh)] == [
            "Software Engineer Intern, Fullstack (Summer 2027)"]
    finally:
        fresh.close()


def test_an_absent_file_is_not_an_error(con, tmp_path, monkeypatch):
    """The normal state for anyone who has never pinned anything."""
    monkeypatch.setattr(manual, "PATH", tmp_path / "nothing-here.json")
    load_all()
    assert poll(con, "manual")["seen"] == 0
    assert _jobs(con) == []


def test_a_malformed_file_says_so_rather_than_emptying_the_feed(
        con, tmp_path, monkeypatch, capsys):
    """Yielding nothing on a syntax error looks exactly like an empty file,
    and the pinned jobs would leave the feed without anything being said."""
    path = tmp_path / "manual.json"
    path.write_text("[{not json")
    monkeypatch.setattr(manual, "PATH", path)
    load_all()
    assert poll(con, "manual")["seen"] == 0
    assert "not readable JSON" in capsys.readouterr().out


def test_an_entry_without_a_url_is_dropped(tmp_path, monkeypatch):
    """A URL is the record id and the only thing identity can be built from.
    An entry without one would be a job that matches every other job with no
    URL, by normalized text."""
    path = tmp_path / "manual.json"
    path.write_text(json.dumps([{"title": "Software Engineer Intern",
                                 "company": "Lyft"},
                                {"url": "https://jobs.lever.co/acme/1"}]))
    monkeypatch.setattr(manual, "PATH", path)
    assert [e["url"] for e in manual.entries()] == [
        "https://jobs.lever.co/acme/1"]


def test_the_pinned_file_in_this_repo_is_readable(tmp_path):
    """The real file, not a fixture. A typo in it is a silently empty source."""
    for entry in manual.entries():
        assert entry.get("url"), entry
        assert entry.get("title"), entry
        assert entry.get("company"), entry
