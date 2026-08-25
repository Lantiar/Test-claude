"""The last check before an application is sent, and the guard on registering."""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autoapply.gate import blockers                       # noqa: E402
from autoapply.models import Field, FillOutcome, Job, Mapping   # noqa: E402
from autoapply.presubmit import presubmit_review          # noqa: E402

PROFILE = {"education": {"school": "Rutgers University"}}


def _outcome() -> FillOutcome:
    job = Job(url="https://jobs.lever.co/acme/1", ats="lever")
    return FillOutcome(
        job=job,
        fields=[Field(id="a", selector="#a", label="First name", required=True)],
        mappings=[Mapping(field_id="a", action="fill", value="Nideesh",
                          label="First name")],
        filled_ids=["a"], verified=True,
    )


class _Provider:
    """Stands in for a model. name != 'rules' so the review actually runs."""
    name = "stub"

    def __init__(self, reply): self.reply, self.seen = reply, None

    def _chat(self, system, user):
        self.seen = user
        return self.reply


def test_review_blocks_when_the_model_objects():
    p = _Provider('{"safe_to_submit": false, "blocking": ["school is wrong"],'
                  ' "notes": "mismatch"}')
    safe, blocking, _ = presubmit_review(_outcome(), PROFILE, p)
    assert safe is False and "school is wrong" in blocking


def test_review_passes_a_clean_form():
    p = _Provider('{"safe_to_submit": true, "blocking": [], "notes": "ok"}')
    safe, blocking, _ = presubmit_review(_outcome(), PROFILE, p)
    assert safe is True and blocking == []


def test_review_declines_rather_than_passing_when_it_cannot_run():
    """Unreachable, unparseable or unconfigured must not read as approval.

    Declining costs a queued application. Passing by default costs a wrong one
    sent under someone's name, so the failure has to fall the safe way.
    """
    class Boom:
        name = "stub"
        def _chat(self, *_a): raise RuntimeError("network down")

    safe, blocking, _ = presubmit_review(_outcome(), PROFILE, Boom())
    assert safe is False and blocking

    safe, blocking, _ = presubmit_review(_outcome(), PROFILE,
                                         _Provider("not json at all"))
    assert safe is False and blocking

    class Rules:
        name = "rules"
    safe, blocking, _ = presubmit_review(_outcome(), PROFILE, Rules())
    assert safe is False and blocking


def test_review_sees_the_answers_and_the_profile():
    p = _Provider('{"safe_to_submit": true, "blocking": []}')
    presubmit_review(_outcome(), PROFILE, p)
    assert "Nideesh" in p.seen and "Rutgers" in p.seen


def test_review_cannot_clear_a_deterministic_block():
    """It may only add reasons. A form where nothing was filled stays blocked
    however enthusiastic the model is about it."""
    job = Job(url="https://jobs.lever.co/acme/1", ats="lever")
    empty = FillOutcome(job=job, verified=True)
    p = _Provider('{"safe_to_submit": true, "blocking": []}')
    reasons = blockers(empty, None, profile=PROFILE, provider=p)
    assert "no form fields discovered" in reasons


def test_review_runs_in_approve_mode_too(monkeypatch):
    """It is not an auto-mode-only check: approve mode shows a human a list of
    values, not the reasoning about whether those values answer the questions."""
    p = _Provider('{"safe_to_submit": false, "blocking": ["wrong degree"]}')
    reasons = blockers(_outcome(), None, profile=PROFILE, provider=p)
    assert "wrong degree" in reasons


def test_review_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("PRESUBMIT_REVIEW", "0")
    p = _Provider('{"safe_to_submit": false, "blocking": ["would block"]}')
    safe, blocking, _ = presubmit_review(_outcome(), PROFILE, p)
    assert safe is True and blocking == []


# --- the registration guard -------------------------------------------------

def test_sign_in_never_clicks_a_create_account_control():
    """Workday's generic clickable id is data-automation-id="click_filter", and
    on the Create Account page that generic id IS the create button. Listing it
    among the submit selectors walks straight into registering an account."""
    from autoapply.login import SUBMIT_SELECTORS, _is_account_creation

    assert not any("click_filter" in s for s in SUBMIT_SELECTORS)

    class El:
        def __init__(self, **kw): self.kw = kw
        def inner_text(self): return self.kw.get("text", "")
        def get_attribute(self, n): return self.kw.get(n)

    assert _is_account_creation(El(**{"aria-label": "Create Account",
                                      "data-automation-id": "click_filter"}))
    assert _is_account_creation(El(text="Register"))
    assert _is_account_creation(El(text="Sign up now"))
    assert not _is_account_creation(El(text="Sign In",
                                       **{"data-automation-id":
                                          "signInSubmitButton"}))


def test_an_unreadable_control_is_treated_as_unsafe():
    from autoapply.login import _is_account_creation

    class Hostile:
        def inner_text(self): raise RuntimeError("detached")
        def get_attribute(self, _n): return None

    assert _is_account_creation(Hostile()) is True


def test_registration_is_off_unless_the_host_opts_in():
    from autoapply.login import credentials_for

    accounts = {"example.com": {"email": "a@b.c", "password": "x"}}
    creds = credentials_for("https://example.com/apply", accounts)
    assert not creds.get("allow_account_creation")


def test_a_wizard_that_never_reached_review_cannot_submit():
    """A stalled wizard looks perfect from the gate's side.

    It fills and verifies everything on the step it is stuck on, so verified is
    True, nothing is missing, and the run reported "auto mode would have
    submitted" while it had in fact never left step one of eight. Submitting
    there sends a part-filled application under the candidate's name.
    """
    job = Job(url="https://x.wd1.myworkdayjobs.com/j", ats="workday")
    outcome = FillOutcome(
        job=job,
        fields=[Field(id="a", selector="#a", label="First name", required=True)],
        mappings=[Mapping(field_id="a", action="fill", value="Nideesh",
                          label="First name")],
        filled_ids=["a"], verified=True, reached_end=False,
    )
    reasons = blockers(outcome, None)
    assert any("review step" in r for r in reasons), reasons

    outcome.reached_end = True
    assert not any("review step" in r for r in blockers(outcome, None))
