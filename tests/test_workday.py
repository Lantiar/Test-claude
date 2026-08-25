"""Workday wizard: multi-step navigation, custom dropdowns, per-step verification."""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autoapply.pipeline import apply_to        # noqa: E402
from autoapply.store import Store              # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


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
