"""Workday wizard: multi-step navigation, custom dropdowns, per-step verification."""
from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autoapply.pipeline import apply_to        # noqa: E402
from autoapply.store import Store              # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

@pytest.fixture(autouse=True)
def _submit_mechanics_only(monkeypatch):
    """These tests are about the submit path, not the presubmit reviewer.

    That reviewer declines when no model is configured -- deliberately, since
    passing by default costs a wrong application sent under someone's name --
    and the fixture tests run with LLM_PROVIDER=rules. They passed until now
    only because the reviewer was never reachable from the pipeline at all;
    with that fixed it correctly blocks every one of them. Switch it off here
    explicitly rather than weaken the rule it enforces: it has its own tests
    in test_presubmit.py.
    """
    monkeypatch.setenv("PRESUBMIT_REVIEW", "0")
    monkeypatch.setenv("SANITY_REVIEW", "0")



def _profile() -> dict:
    p = json.load(open("config/profile.example.json"))
    p["files"]["resume"] = str(FIXTURES / "resume.pdf")
    return p


def test_workday_wizard_walks_every_step_and_submits(tmp_path):
    os.environ["SCREENSHOT_DIR"] = str(tmp_path / "shots")
    r = apply_to((FIXTURES / "workday.html").as_uri(), mode="auto",
                 store=Store(str(tmp_path / "wd.sqlite")), profile=_profile(),
                 ats_override="workday")
    assert r.status == "applied", f"{r.status}: {r.gate.reasons} {r.detail}"

    labels = {(m.label or "").lower() for m in r.outcome.mappings if m.action == "fill"}
    # Fields from three different wizard steps must all appear.
    assert any("first name" in x for x in labels)          # step 1
    assert any("resume" in x for x in labels)              # step 2
    assert any("authorized" in x for x in labels)          # step 3


def test_workday_custom_dropdown_is_selected(tmp_path):
    """The button+listbox dropdown is not a <select>; it needs the click path."""
    os.environ["SCREENSHOT_DIR"] = str(tmp_path / "shots")
    r = apply_to((FIXTURES / "workday.html").as_uri(), mode="auto",
                 store=Store(str(tmp_path / "wd2.sqlite")), profile=_profile(),
                 ats_override="workday")
    auth = [m for m in r.outcome.mappings if "authorized" in (m.label or "").lower()]
    assert auth and auth[0].value == "Yes"
    gender = [m for m in r.outcome.mappings if "gender" in (m.label or "").lower()]
    assert gender and gender[0].value == "Decline To Self Identify"


def test_workday_verifies_each_step_internally(tmp_path):
    from autoapply.workers.workday import WorkdayWorker

    assert WorkdayWorker.verifies_internally is True


# --- regressions found against the live Blackstone tenant ---------------------

import contextlib                                          # noqa: E402

from autoapply.browser import browser_page                 # noqa: E402


@contextlib.contextmanager
def _page_with(html: str):
    with browser_page() as page:
        page.set_content(html)
        yield page


def test_workday_no_captcha_wrapper_is_not_a_captcha():
    """Workday ships div[data-automation-id="noCaptchaWrapper"] on pages with no
    challenge at all, so a bare "captcha" substring test fires on every Workday
    page -- and the gate then blocks a submit that nothing was wrong with."""
    from autoapply.workers.workday import WorkdayWorker
    with _page_with("<div data-automation-id='noCaptchaWrapper'></div>"
                    "<div data-automation-id='applyFlowPage'></div>") as page:
        assert WorkdayWorker(page).saw_captcha() is False


def test_workday_real_captcha_is_still_detected():
    from autoapply.workers.workday import WorkdayWorker
    with _page_with("<div data-automation-id='noCaptchaWrapper'></div>"
                    "<div class='g-recaptcha' data-sitekey='x'></div>") as page:
        assert WorkdayWorker(page).saw_captcha() is True


def test_workday_account_wall_is_reported_as_auth():
    """The Blackstone tenant gates the wizard behind Create Account and ships
    none of the createAccountLink/createAccountPage ids the first selector list
    relied on. Missing it sends the run to the agent lane, which the same wall
    blocks -- so the queue reason has to name the wall, not empty markup."""
    from autoapply.workers.workday import WorkdayWorker
    with _page_with(
        "<div data-automation-id='signInContent'>"
        "<div data-automation-id='formField-email'><label>Email Address*</label>"
        "<input data-automation-id='email'></div>"
        "<div data-automation-id='formField-password'><label>Password*</label>"
        "<input type='password' data-automation-id='password'></div>"
        "<button data-automation-id='createAccountSubmitButton'>Create Account</button>"
        "</div>") as page:
        assert WorkdayWorker(page).needs_auth() is True


def test_a_checkbox_group_is_a_choice_not_a_text_box(tmp_path):
    """Race/Ethnicity is eight checkboxes, and discovery only recognised a
    group of exactly one.

    Eight fell through to the text branch, whose selector matches the first
    checkbox and whose write sets .value on it. Setting .value on a checkbox
    does nothing whatsoever and raises nothing, so the fill reported success
    and verification read the field back empty -- want=Asian got= -- on every
    run, and the step could never validate because the field really was still
    empty. A lone checkbox stays a consent tick with no choices to offer.
    """
    from playwright.sync_api import sync_playwright
    from autoapply.browser import find_chromium
    from autoapply.workers.workday import WorkdayWorker

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        page = browser.new_page()
        page.goto((FIXTURES / "workday_selfid.html").as_uri())
        fields = {f.label: f for f in WorkdayWorker(page).discover()}
        browser.close()

    race = next(f for k, f in fields.items() if "Race" in k)
    assert race.kind == "checkbox", f"discovered as {race.kind}"
    assert "type=checkbox" in race.selector
    assert any(o.startswith("Asian") for o in race.options), race.options
    assert len(race.options) == 8

    consent = next(f for k, f in fields.items() if "consent" in k.lower())
    assert consent.kind == "checkbox"
    assert consent.options == [], "a lone checkbox is a tick, not a question"
