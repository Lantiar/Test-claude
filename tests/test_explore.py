"""The tier that adapts to a widget nobody wrote code for.

This is the one that is supposed to make hand-written per-widget support
unnecessary: when the filler cannot set a field, look at the page, ask the
model what to click, click it, look again. Its output is the click path, stored
like any other correction, so the next run replays it with no model at all.

It was failing silently. The chosen element was looked back up by matching
innerText, which an <input> does not have -- its label in the candidate list
came from aria-label, placeholder or value -- so for any input-backed control
the lookup found nothing and the loop hit a bare `break`. Five of its eight
exits logged nothing, so a tier that never once succeeded looked exactly like a
tier that had nothing to do, and the missing capability got worked around
upstream instead of fixed.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autoapply.browser import find_chromium   # noqa: E402
from autoapply.explore import solve_field     # noqa: E402
from autoapply.models import Field            # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

PROFILE = {"identity": {"first_name": "Nideesh"}}


class Chooser:
    """Stands in for the model: picks the first item whose label matches."""

    name = "openai"

    def __init__(self, *wanted: str):
        self.wanted = list(wanted)
        self.asked: list[list[dict]] = []

    def _chat(self, system, user):
        items = json.loads(user)["on_screen"]
        self.asked.append(items)
        if self.wanted:
            target = self.wanted.pop(0)
            for item in items:
                if target.lower() in item["label"].lower():
                    return json.dumps({"click": item["i"], "done": False})
        return json.dumps({"done": True})


class _Worker:
    def __init__(self, page):
        self.page = page

    def frame_for(self, field):
        return self.page


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "explore_widget.html").as_uri())
        yield pg
        browser.close()


def test_it_can_click_an_input_backed_control(page):
    """The bug, exactly: the control you must open first is an <input>.

    Matching on innerText could never find it again, so the loop broke on its
    first click every time -- on the dropdown this tier exists to drive.
    """
    field = Field(id="veteranStatus", selector="#vet", label="Have you ever "
                  "served in the military?", kind="select")
    worker = _Worker(page)

    def read_value():
        return (page.eval_on_selector("#vet", "e => e.value") or "").strip()

    value, path = solve_field(worker, field, PROFILE,
                              Chooser("served in the military",
                                      "not a protected veteran"),
                              read_value)

    assert value == "I am not a protected veteran", f"got {value!r} via {path}"
    # The path is the point: stored, it replays next run with no model.
    assert path == ["Have you ever served in the military?",
                    "I am not a protected veteran"]


def test_the_model_is_shown_the_options_once_the_menu_opens(page):
    """A closed Workday dropdown has no options in the DOM, which is why every
    tier upstream was answering this question blind. Opening it is what puts
    the real choices in front of the model."""
    field = Field(id="veteranStatus", selector="#vet", label="Have you ever "
                  "served in the military?", kind="select")
    chooser = Chooser("served in the military", "not a protected veteran")

    solve_field(_Worker(page), field, PROFILE, chooser,
                lambda: (page.eval_on_selector("#vet", "e => e.value") or "").strip())

    labels = {i["label"] for i in chooser.asked[-1]}
    assert "I am not a protected veteran" in labels
    assert "I don't wish to answer" in labels


def test_it_refuses_to_advance_the_form(page):
    """Clicking Save and Continue would leave the step half-filled."""
    from autoapply.explore import _is_navigation

    for label in ("Save and Continue", "Continue", "Submit", "Back", "Next"):
        assert _is_navigation(label), label
    for label in ("I don't wish to answer", "Nebraska", "Backend Engineer"):
        assert not _is_navigation(label), label


# --- consent banners --------------------------------------------------------

def test_a_cookie_banner_is_dismissed_so_the_form_can_be_seen():
    """AMD's cookie modal covers the whole iCIMS form.

    Discovery found zero fields on a page carrying a complete application, and
    the run reported "no form fields discovered" about a form it had never
    seen. Nearly every EU-facing careers site has one of these, so it is worth
    handling once rather than rediscovering per ATS.
    """
    from playwright.sync_api import sync_playwright
    from autoapply.workers.generic import GenericWorker

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "consent_banner.html").as_uri())
        worker = GenericWorker(pg)

        assert pg.query_selector("#cookie-notice") is not None
        clicked = worker.dismiss_consent()
        assert clicked == "accept cookies", f"clicked {clicked!r}"
        assert pg.query_selector("#cookie-notice") is None

        # And the form behind it is now discoverable.
        labels = {(f.label or "").lower() for f in worker.discover()}
        assert any("first name" in x for x in labels), labels
        browser.close()


def test_it_does_not_click_the_forms_own_agree_or_submit():
    """"I agree" on an application page is a terms checkbox or the submit
    button. Only a control inside something actually talking about cookies is
    a consent banner, which is why the search is scoped rather than a sweep for
    accept-shaped text."""
    from playwright.sync_api import sync_playwright
    from autoapply.workers.generic import GenericWorker

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "consent_banner.html").as_uri())
        worker = GenericWorker(pg)
        worker.dismiss_consent()          # takes the banner

        # Nothing left to dismiss: the form's own "I agree" is not a target.
        assert worker.dismiss_consent() == ""
        assert not pg.eval_on_selector("#terms", "e => e.checked")
        browser.close()
