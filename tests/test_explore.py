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
        # What the control held each time it was asked. The last entry is what
        # the model was looking at when it said the field was done.
        self.saw: list[str] = []

    def _chat(self, system, user):
        payload = json.loads(user)
        items = payload["on_screen"]
        self.asked.append(items)
        self.saw.append(payload.get("currently_shows"))
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


# --- captchas ---------------------------------------------------------------

def _worker_on(fixture):
    from playwright.sync_api import sync_playwright
    from autoapply.workers.generic import GenericWorker

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    p = sync_playwright().start()
    browser = p.chromium.launch(**launch)
    pg = browser.new_page()
    pg.goto((FIXTURES / fixture).as_uri())
    return GenericWorker(pg), browser, p


def test_talking_about_captchas_is_not_having_one():
    """AMD's iCIMS page embeds a JSON translation bundle reading
    "hcaptcha":{"protected":"protected by hcaptcha."} for a widget it never
    renders, and the run reported CAPTCHA present and refused to proceed on a
    page with no challenge anywhere on it.

    Workday's noCaptchaWrapper had already forced one exception to be
    hard-coded into the marker list, which should have been the clue that the
    test itself was wrong rather than the list incomplete. A challenge is a
    thing on the screen, not a word in the source.
    """
    worker, browser, p = _worker_on("captcha_talk.html")
    try:
        assert not worker.saw_captcha()
    finally:
        browser.close(); p.stop()


def test_a_rendered_challenge_is_still_caught():
    """The check must not have been loosened into uselessness: a widget of the
    size a person actually clicks still stops the run."""
    worker, browser, p = _worker_on("captcha_real.html")
    try:
        assert worker.saw_captcha()
    finally:
        browser.close(); p.stop()


# --- getting from the posting to the application ----------------------------

def test_it_clicks_through_from_a_job_posting_to_the_application():
    """Only the Workday worker knew a posting is not an application.

    On BNY's Oracle site the run stayed on the posting and filled the careers
    page's own furniture: a "City, state, country" location search and a
    dropzone reading "Upload or drag and drop your PDF resume file here to get
    AI recommended jobs" -- a job-recommendation widget, not the application's
    resume field. It reported five fields discovered and one filled, on a page
    with no application on it.
    """
    from autoapply.models import Job
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        worker = GenericWorker(pg)
        worker.open(Job(url=(FIXTURES / "job_posting.html").as_uri(),
                        ats="generic"))

        assert pg.url.endswith("apply.html"), f"still on {pg.url}"
        labels = {(f.label or "").lower() for f in worker.discover()}
        assert any("first name" in x for x in labels), labels
        browser.close()


def test_it_does_not_click_apply_when_already_on_the_form():
    """An "Apply" further down an application page is a different job, and
    "Apply filters" on a search page is not an application at all."""
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "apply.html").as_uri())
        assert GenericWorker(pg).start_application() == ""
        browser.close()


def test_a_url_that_is_already_the_application_is_not_left():
    """Notion's link is an Ashby /application URL that renders the form itself.

    open() went looking for a way in anyway, found the posting's own apply
    link sitting on the application it leads to, followed it, and ended on
    www.ashbyhq.com -- where the gate refused to fill a marketing page,
    correctly and one navigation too late. The run then read that as a closed
    posting, because a closed Ashby posting redirects to exactly that page. It
    was not closed: the posting is live and serves the form at the URL the run
    started from. We navigated off it ourselves and diagnosed the wreckage as
    the site's doing.

    START_JS has its own "am I already on an application" test, and it misses
    this: it scans document.querySelectorAll('label, legend'), while Ashby is
    a React app whose captions are divs and whose accessible names live on the
    inputs. That test sees an empty page. Discovery does not, so the guard
    that matters asks discovery.
    """
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "ashby_application.html").as_uri())
        worker = GenericWorker(pg)

        # Without the guard this is what open() would have followed.
        assert worker.start_application(
            click=False,
            job_url="https://jobs.ashbyhq.com/notion/"
                    "3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c/application"
        ).startswith("vendor_home.html"), (
            "the fixture no longer reproduces the navigation that broke Notion")

        assert worker._already_on_the_application()
        assert pg.url.endswith("ashby_application.html"), f"left for {pg.url}"
        browser.close()


def test_a_tick_box_that_already_says_yes_is_not_clicked_off():
    """TikTok's privacy-policy consent arrives ticked.

    _write_choice clicked it anyway, which unticked it. The run then reported
    "could not write 'Yes'" and "'true' would not stick", explore spent six
    steps failing to work the control out, and the whole application failed
    verification -- over a field that was correct when the page loaded, that
    the run itself broke, and that no repair could fix, because every repair
    attempt was another click.

    A control already saying what we mean is written. On a consent box that is
    also the only safe reading: the alternative is a run whose way of agreeing
    to a privacy policy is to toggle it and hope.
    """
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "prechecked_consent.html").as_uri())
        worker = GenericWorker(pg)

        consent = next(f for f in worker.discover()
                       if "privacy policy" in (f.label or "").lower())
        assert consent.kind == "checkbox", consent.kind
        assert worker._write(consent, "Yes") == "Yes", "a ticked box is written"
        assert pg.eval_on_selector("#pp", "e => e.checked"), "it was clicked off"
        browser.close()


def test_a_field_with_no_id_and_no_name_is_still_findable():
    """The fallback selector did not refer to the element it was built for.

    It was f"{form_selector} {tag}:nth-of-type({idx+1})", and form_selector is
    a selector *list* -- "form, main, body". Concatenated, that parses as
    several selectors, only the last of which carries the tag, so querySelector
    returned whatever came first in the document. On TikTok that was <main>:
    writing the field set a `value` property on <main>, and verification read
    the same property straight back and passed. A field nobody could see was
    reported filled and correct.

    The index was wrong independently. "body input:nth-of-type(5)" does not
    mean the fifth input on the page; it means any input that is the fifth of
    its type among its own parent's children.
    """
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "no_id_no_name.html").as_uri())
        worker = GenericWorker(pg)

        intro = next(f for f in worker.discover()
                     if "self-introduction" in (f.label or "").lower())
        assert pg.eval_on_selector(intro.selector, "e => e.tagName") == "TEXTAREA", (
            f"{intro.selector!r} does not resolve to the field it names")

        assert worker._write(intro, "Hello") == "Hello"
        assert pg.eval_on_selector("textarea", "e => e.value") == "Hello"
        # ...and not onto whatever the old expression happened to hit first.
        assert not pg.eval_on_selector("main", "e => e.value"), \
            "the value landed on the container, not the control"
        browser.close()


def test_discovery_scopes_to_the_form_and_not_the_site_around_it():
    """form_selector is a preference list; querySelector does not read it as one.

    "form, main, body" is written narrowest-first so discovery scopes to the
    application. Handed to querySelector, the list is not a preference at all:
    it returns whichever match comes first in the *document*, and <body>
    precedes <main> in every document there is. The fallback of last resort
    therefore won on every page, and discovery has been reading whole pages.

    TikTok's 66 fields are partly that. Among them was the footer's
    <select class="language-selection-form">, English or 日本語 -- an
    unlabelled control the mapper had no answer for, that the run counted as a
    field it could not fill, and that the reviewer read as evidence the page
    might not be an application at all.
    """
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "no_id_no_name.html").as_uri())
        worker = GenericWorker(pg)

        assert pg.eval_on_selector(
            "body", "e => !!e.querySelector('.language-selection-form')"), \
            "the fixture no longer carries the site furniture this is about"
        assert worker._form_root(pg.main_frame).evaluate("e => e.tagName") == "MAIN"

        fields = worker.discover()
        assert not [f for f in fields if f.kind == "select"], \
            f"the site's language picker was discovered: {[f.label for f in fields]}"
        assert {(f.label or "").lower() for f in fields} == {
            "first name", "last name", "self-introduction"}
        browser.close()


def test_a_search_box_is_not_mistaken_for_the_application_form():
    """Written order alone would be too eager the other way.

    A careers site usually carries a <form> that is a search box, and "form"
    leads three of the four form_selector lists. Taking the first match in
    written order would scope discovery to the search box and lose the
    application entirely -- a worse failure than reading the footer, and on
    Workday it would break the one link that works today.
    """
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "search_form_then_application.html").as_uri())
        worker = GenericWorker(pg)

        assert worker._form_root(pg.main_frame).evaluate("e => e.tagName") == "MAIN"
        labels = {(f.label or "").lower() for f in worker.discover()}
        assert "first name" in labels and "cover letter" in labels, labels
        assert "search jobs" not in labels, labels
        browser.close()


def test_the_application_s_own_front_door_is_walked_through():
    """AMD's iCIMS tenant puts an email box in front of the form.

    /jobs/91176/login asks for an email and a privacy acceptance, with no
    password anywhere, and needs_auth() correctly says that is not a wall.
    Recognising it was only half the job: nothing walked through it. Discovery
    found Email plus a consent tick, looks_like_an_application() correctly said
    two such fields are not an application, and the run stopped on the first
    page of a form it could have filled -- reporting "no form fields
    discovered" about an application it never reached.
    """
    import os
    from autoapply.models import Job
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    os.environ["AUTOAPPLY_EMAIL"] = "someone@example.com"
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "icims_entry.html").as_uri())
        worker = GenericWorker(pg)

        assert worker.looks_like_an_entry_step()
        assert not worker.needs_auth(), "no password: this is a door, not a wall"
        assert worker.pass_entry_step(Job(url=pg.url, ats="generic"))

        labels = {(f.label or "").lower() for f in worker.discover()}
        assert any("first name" in x for x in labels), labels
        # The required consent was ticked -- the form does not proceed without
        # it -- and that is the only kind of box this may touch.
        browser.close()


def test_oracles_email_door_is_walked_through_without_tripping_its_honeypot():
    """BNY's posting reaches its form through the same kind of door as AMD's.

    Clicking APPLY NOW lands on /apply/email: "You don't need to have an
    account. Get started right away by simply using your email", an email box,
    "I agree with the terms and conditions", and NEXT. None of the phrases that
    recognise an entry step matched Oracle's wording, so the door was not
    recognised and the run went no further -- it discovered one field on the
    whole page and fell through to the agent lane every time.

    The page also carries a field named "honeypot". Invisibility is how those
    are usually hidden, and the visibility check already skips most of them --
    but that is a side effect, and what it protects is a real person's
    application not being taken for a bot's. So it is refused by name.
    """
    import os
    from autoapply.models import Job
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    os.environ["AUTOAPPLY_EMAIL"] = "someone@example.com"
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "oracle_entry.html").as_uri())
        worker = GenericWorker(pg)

        assert worker.looks_like_an_entry_step(), "Oracle's wording must count"
        found = worker.discover()
        assert not [f for f in found
                    if pg.eval_on_selector(f.selector, "e => e.id") == "hp"], (
            f"the trap was discovered: {[(f.id, f.label) for f in found]}")
        assert [f.label for f in found if f.kind == "email"] == ["Email Address"]

        assert worker.pass_entry_step(Job(url=pg.url, ats="generic"))
        assert not pg.query_selector("#hp"), "still on the door"
        # What the door saw when it was submitted. Anything in the honeypot is
        # what gets a real person's application flagged as a bot's; BNY spells
        # it "honey-pot", with a hyphen, and renders it *visible*, so neither
        # the original pattern nor the visibility check would have kept the run
        # out of it.
        assert pg.evaluate("() => window.__honeypot") == "", "the trap was filled"
        # And the required agreement is a hidden native checkbox behind a
        # styled label -- id "legal-disclaimer-checkbox", no readable label of
        # its own. Skipped as invisible, the door never opened and the run
        # reported that the page was not an application: true, and not why.
        assert pg.evaluate("() => window.__agreed") is True, \
            "the door will not open until its own agreement is accepted"

        labels = {(f.label or "").lower() for f in worker.discover()}
        assert any("first name" in x for x in labels), labels
        browser.close()


def test_an_upload_that_offers_to_fill_the_form_in_is_not_a_field():
    """Notion's Ashby form opens with an autofill widget above the real upload.

    "Autofill from resume -- Upload your resume here to autofill key
    application fields": a 1x1 file input with no id and no name. Discovery
    took it for an application field and, with nothing else nearby to read,
    labelled it "Full Name" -- the caption of the input after it. The reviewer
    then reported duplicate entries for the same question, which was true of
    what it had been shown.

    Uploading there is worse than useless: Ashby's parser would rewrite the
    fields the run had just filled. BNY's careers page has the same shape in
    other words -- "upload or drag and drop your PDF resume file here to get AI
    recommended jobs" -- and that one cost a whole run, by making a search page
    look like an application. The real upload says what it is for; these say
    what they will do for you.
    """
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "ashby_autofill.html").as_uri())

        fields = GenericWorker(pg).discover()
        uploads = [f for f in fields if f.kind == "file"]
        assert len(uploads) == 1, [(f.id, f.label) for f in uploads]
        assert uploads[0].label == "Resume", uploads[0].label

        names = [f for f in fields if (f.label or "") == "Full Name"]
        assert len(names) == 1, "the autofill widget still doubles as a name field"
        assert names[0].kind != "file"
        browser.close()


def test_the_value_explore_returns_was_looked_at_before_being_accepted(page):
    """This tier's answer is taught, and a wrong lesson is worse than none.

    It returned as soon as the control held anything different from what it
    held before -- so any non-empty change was the answer, and that answer is
    stored, replayed with confidence 1.0 on every future run, and outranks
    every rule beneath it. Nobody ever looked at it.

    Going round once more costs one model call on a tier that only fires for
    controls no other tier can drive, and it turns "done" into a statement
    about the value produced rather than about nothing at all.
    """
    field = Field(id="veteranStatus", selector="#vet", label="Have you ever "
                  "served in the military?", kind="select")
    chooser = Chooser("served in the military", "not a protected veteran")

    value, path = solve_field(
        _Worker(page), field, PROFILE, chooser,
        lambda: (page.eval_on_selector("#vet", "e => e.value") or "").strip())

    assert value == "I am not a protected veteran", f"{value!r} via {path}"
    # The reading it was asked to accept is the one the clicks produced -- not
    # the empty control it started on.
    assert chooser.saw[-1] == "I am not a protected veteran", chooser.saw
    # ...and not the unanswered control it started on.
    assert chooser.saw[0] == "Select One", chooser.saw
    assert len(chooser.saw) == len(path) + 1, (
        f"one reading per click plus the confirming one: {chooser.saw}")


def test_a_page_we_refuse_to_fill_still_reports_what_it_is():
    """AMD's hCaptcha door was handed to the agent lane on every run.

    Both give-up paths built a bare outcome and left saw_captcha and
    needs_auth False, so a run that stopped in front of a CAPTCHA reported no
    CAPTCHA. The gate had nothing to block on beyond "nothing was filled", and
    _needs_agent_fallback -- which refuses to retry a page held by a CAPTCHA,
    precisely because the agent is held by it too -- saw no reason to refuse.
    So the fallback spent a full budget of steps discovering it could not get
    through either.
    """
    from autoapply.models import Job
    from autoapply.pipeline import _needs_agent_fallback
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "captcha_real.html").as_uri())
        worker = GenericWorker(pg)
        assert worker.saw_captcha(), "the fixture no longer shows a captcha"

        outcome = worker.run(Job(url=pg.url, ats="generic"), PROFILE, None,
                             None, "")
        assert outcome.saw_captcha, "the run stopped at a captcha and did not say so"
        assert not _needs_agent_fallback(outcome), (
            "the agent is held by a captcha too; retrying only spends a budget")
        browser.close()


def test_a_code_split_across_one_box_per_digit_is_filled_across_them():
    """Behind BNY's door is a six-digit PIN in six boxes.

    The code path was written for a single field, so it would have typed all
    six digits into pin-code-1 and left five boxes empty and one digit in the
    first. The run reached the door's own verification step and stopped there
    with "no answer for required: pin-code-1 ... pin-code-5".
    """
    from autoapply.login import _clear_code
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "pin_code.html").as_uri())

        class Worker:
            page = pg

            def frames(self):
                return [pg]

        said = []
        ok, detail = _clear_code(Worker(), {"code_filter": ["verify"]},
                                 lambda _n: "483920", said.append)
        assert ok, detail
        # What the form was submitted with -- _clear_code presses Verify, and
        # a real page navigates away from the boxes it just read.
        assert pg.evaluate("() => window.__pin") == "483920", \
            f"submitted {pg.evaluate('() => window.__pin')!r}"
        browser.close()


def test_a_site_holding_us_off_says_so():
    """BNY's door counts attempts, and six rounds of submitting it earned
    "Too Many Attempts. Try Again Later. You reached the maximum number of
    attempts. Try again in 30 minutes."

    The run reported that page as "does not look like a job application" --
    true of what was on screen, and silent about why. So the next run repeats
    the attempt and the count goes up again. A site asking us to come back
    later is a distinct, recoverable state and worth naming: reloading does
    not clear it, and another try is what deepens the hole.
    """
    from autoapply.models import FillOutcome, Job
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.goto((FIXTURES / "rate_limited.html").as_uri())
        worker = GenericWorker(pg)

        held = worker.rate_limited()
        assert "too many attempts" in held.lower(), held

        outcome = FillOutcome(job=Job(url=pg.url, ats="generic"))
        worker._record_page_state(outcome)
        assert any("holding us off" in e for e in outcome.errors), outcome.errors

        # An ordinary application is not mistaken for one.
        pg.goto((FIXTURES / "apply.html").as_uri())
        assert GenericWorker(pg).rate_limited() == ""
        browser.close()


def test_a_tick_box_takes_a_yes_or_a_no_and_nothing_else():
    """Auto-Owners asks "I have a preferred name" as a lone checkbox.

    The preferred-name rule matched the label and handed it "Nideesh". The
    write read that as not-yes, unticked the box, and returned the name as the
    value written -- so the run counted the field filled, and verification
    found it empty. Refusing a value that is neither a yes nor a no answers
    the question honestly: nothing was written, and the gate can see it.
    """
    from autoapply.models import Field
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()
        pg.set_content('<label><input id="pn" type="checkbox"> '
                       'I have a preferred name</label>')
        worker = GenericWorker(pg)
        box = Field(id="pn", selector="#pn", label="I have a preferred name",
                    kind="checkbox")

        assert worker._write(box, "Nideesh") is None, "a name is not an answer"
        assert not pg.eval_on_selector("#pn", "e => e.checked")

        assert worker._write(box, "Yes") == "Yes"
        assert pg.eval_on_selector("#pn", "e => e.checked")
        assert worker._write(box, "No") == "No"
        assert not pg.eval_on_selector("#pn", "e => e.checked")
        browser.close()


def test_an_invisible_recaptcha_is_not_a_challenge():
    """Axon's application stopped at 29 of 32 fields filled over a reCAPTCHA
    that asks nobody anything.

    The enterprise/invisible setup Greenhouse boards run by default scores the
    session in the background and the form submits normally; its anchor frame
    is the widget, not a challenge. The challenge -- the "select all the
    buses" grid -- is a separate bframe. Blocking on the anchor would stop most
    of the Greenhouse and Lever boards on a Summer-2027 list, which between
    them are hundreds of live postings.
    """
    from autoapply.workers.generic import GenericWorker
    from playwright.sync_api import sync_playwright

    launch = {"headless": True}
    if exe := find_chromium():
        launch["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        pg = browser.new_page()

        pg.goto((FIXTURES / "recaptcha_anchor.html").as_uri())
        assert not GenericWorker(pg).saw_captcha(), \
            "an invisible reCAPTCHA challenges nobody"

        pg.goto((FIXTURES / "recaptcha_challenge.html").as_uri())
        assert GenericWorker(pg).saw_captcha(), \
            "the grid a person has to solve must still stop the run"
        browser.close()
