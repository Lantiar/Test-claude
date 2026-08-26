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
