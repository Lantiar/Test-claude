"""Test-wide safety rails.

The suite is offline and must stay that way: it runs on every commit, and a
test whose result depends on an API key in the environment is a test that
passes on one machine and fails on another for reasons that have nothing to
do with the code.
"""
import pytest

from jobfeed.outreach import apify


@pytest.fixture(autouse=True)
def _no_live_balance_check(monkeypatch):
    """`find_recruiters` asks Apify what is left before it spends anything.

    That is one real HTTP call, and with APIFY_TOKEN set in the environment --
    a .env sourced into the shell, say -- the whole suite would start
    depending on the balance of a real billing account. Unreadable is the
    honest default here, and it is the one that lets a search proceed; the
    tests that are about the floor patch this themselves.
    """
    monkeypatch.setattr(apify, "remaining_credit", lambda: None)
