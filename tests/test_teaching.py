"""The learning loop: a field got wrong once should be wrong once.

The tiers are script -> audit -> form's own verdict, and every tier that
corrects something writes it back so the tier below produces it next time. What
these check is that the writeback is actually reachable, which it was not: the
taught answer was consulted only when no rule matched, so the fields most in
need of teaching -- the ones a rule gets confidently wrong -- could never
receive it.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autoapply.mapper import map_fields, match_rule, signature   # noqa: E402
from autoapply.models import Field                               # noqa: E402
from autoapply.store import Store                                # noqa: E402

PROFILE = {"identity": {"first_name": "Nideesh", "phone": "224-333-1045"},
           "location": {"city": "Monroe Township", "state": "NJ"}}


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "teach.sqlite"))


def test_a_taught_answer_outranks_a_rule_that_matches(tmp_path):
    """The precedence fix, on a field whose rule is right in general.

    Teaching has to be able to override a matching rule, not merely fill the
    gaps between rules. A rule is a guess from a label pattern; a taught answer
    was confirmed once, by a person or by the form accepting it. Consulted
    after the rules, the store was inert for every field a rule already
    claimed -- which is exactly where a rule goes wrong.
    """
    store = _store(tmp_path)
    field = Field(id="city", selector="#city", label="City")

    before = map_fields([field], PROFILE, "workday", store=store)[0]
    assert before.source == "rules" and before.value == "Monroe Township"

    store.record_correction(signature("workday", "City"), "City", "Jersey City")

    after = map_fields([field], PROFILE, "workday", store=store)[0]
    assert after.source == "learned"
    assert after.value == "Jersey City"


def test_teaching_is_scoped_to_the_ats(tmp_path):
    """A correction learned on one ATS must not leak onto another whose form
    asks a similar-looking question in a different context."""
    store = _store(tmp_path)
    store.record_correction(signature("workday", "Phone Extension"),
                            "Phone Extension", "N/A")
    field = Field(id="ext", selector="#ext", label="Phone Extension")

    other = map_fields([field], PROFILE, "greenhouse", store=store)[0]
    assert other.source != "learned"


def test_a_repair_is_reused_by_the_next_run(tmp_path):
    """End to end: what the audit teaches, the deterministic pass produces --
    with no model involved the second time."""
    from autoapply.repair import _teach, commit_lessons

    store = _store(tmp_path)
    field = Field(id="phoneType", selector="#phoneType", label="Phone Device Type",
                  kind="combobox", options=["Mobile", "Home", "Work"],
                  required=True)

    first = map_fields([field], PROFILE, "workday", store=store)[0]
    assert first.action == "unknown", "profile has no answer for this"

    _teach(store, "workday", "Phone Device Type", "Mobile")
    commit_lessons(store, "workday")          # the form accepted the step

    second = map_fields([field], PROFILE, "workday", store=store,
                        provider=None)[0]
    assert second.action == "fill"
    assert second.value == "Mobile"
    assert second.source == "learned"


# --- what the live Mastercard form exposed ----------------------------------

def test_phone_shaped_questions_are_not_all_the_phone_number():
    """Workday's My Information asks four phone-shaped questions and the phone
    rule claimed all four, filling every one with the phone number. Only the
    one actually asking for the number should match a rule; the rest go to the
    model, which can read the question and pick from the real options."""
    assert match_rule("Phone Number*") == "identity.phone"
    assert match_rule("Mobile Phone") == "identity.phone"

    # Nothing in the profile answers these, so they go to the model.
    for label in ("Phone Device Type*", "Phone Extension"):
        assert match_rule(label) is None, f"{label} should not resolve to a rule"

    # This one the profile does answer. Sending it to the model instead got "1"
    # back, which resolved to American Samoa -- a real +1 country, so the form
    # accepted it and the wrong answer was learned.
    assert match_rule("Country Phone Code*") == "location.country"


def test_a_country_name_beats_a_bare_dialling_code():
    from autoapply.mapper import resolve_option

    options = ["American Samoa (+1)", "Anguilla (+1)", "United States of America (+1)"]
    assert resolve_option("United States", options) == "United States of America (+1)"
    # A bare number is a whole word inside half these options and must not
    # select one by being the first that contains it.
    assert resolve_option("1", options) is None
    assert resolve_option("+1", options) is None


def test_a_state_abbreviation_finds_the_full_name_in_a_dropdown():
    """The profile carries NJ and the form lists New Jersey. Neither
    containment test bridges that, so State came back 'Select One' and the
    step would not validate."""
    from autoapply.mapper import resolve_option

    options = ["Alabama", "Missouri", "New Jersey", "New York"]
    assert resolve_option("NJ", options) == "New Jersey"
    assert resolve_option("New Jersey", options) == "New Jersey"
    assert resolve_option("ZZ", options) is None


def test_a_dropdown_prompt_is_never_taught_as_an_answer(tmp_path):
    """"Select One" is the prompt at the top of a dropdown, not a choice.

    Offered as an option the model picked it for Phone Device Type and the form
    answered "The entered value is not one of the options provided". Teaching it
    would be worse than not learning at all: a taught answer outranks the rules
    below it, so every later run would fill the field with the prompt and fail
    the same validation.
    """
    from autoapply.repair import _teach, commit_lessons

    store = _store(tmp_path)
    field = Field(id="phoneType", selector="#phoneType", label="Phone Device Type",
                  kind="select", options=["Mobile", "Home"], required=True)

    for prompt in ("Select One", "Select...", "Choose an option", "--", ""):
        _teach(store, "workday", "Phone Device Type", prompt)
        commit_lessons(store, "workday")
        assert map_fields([field], PROFILE, "workday", store=store)[0].source \
            != "learned", f"taught the placeholder {prompt!r}"

    _teach(store, "workday", "Phone Device Type", "Mobile")
    commit_lessons(store, "workday")
    assert map_fields([field], PROFILE, "workday", store=store)[0].value == "Mobile"


def test_workday_dropdown_options_exclude_the_prompt():
    from autoapply.workers.workday import PLACEHOLDER_OPTION

    for prompt in ("Select One", "Select...", "Choose an option", "--"):
        assert PLACEHOLDER_OPTION.match(prompt), prompt
    for real in ("Mobile", "Home", "Selected Applicant"):
        assert not PLACEHOLDER_OPTION.match(real), real


def test_nothing_is_learned_from_a_step_the_form_rejected(tmp_path):
    """The form accepting the step is the only evidence a fill was right.

    Everything the code can observe about a field is a proxy -- a chip
    appeared, the displayed value changed, no fresh options came back -- and
    each of those has already been wrong once on a real form. A lesson drawn
    from a wrong one is worse than no lesson: a taught answer outranks the
    rules beneath it and gets replayed confidently on every later run.
    """
    from autoapply.repair import _teach, commit_lessons, drop_lessons

    store = _store(tmp_path)
    field = Field(id="src", selector="#src", label="How Did You Hear About Us?",
                  kind="combobox", options=["Job Board"], required=True)

    # A plausible-looking fill that the form then refused.
    _teach(store, "workday", "How Did You Hear About Us?", "Job Board")
    assert drop_lessons(store) == 1
    assert map_fields([field], PROFILE, "workday", store=store)[0].source != "learned"

    # The same answer, on a step that was accepted.
    _teach(store, "workday", "How Did You Hear About Us?", "Job Board")
    assert commit_lessons(store, "workday") == [
        "How Did You Hear About Us? = Job Board"]
    assert map_fields([field], PROFILE, "workday", store=store)[0].source == "learned"


# --- keeping the tiers affordable enough to actually run --------------------

def test_a_long_picklist_is_sampled_but_keeps_the_likely_answer():
    """A 429 does not look like a failure, it looks like a clean form.

    "audit corrected 0" meant the audit never ran. The tier was switched off by
    a rate limit for whole runs, and the bulk of every request was two ~250-entry
    country dropdowns that any model already knows by heart. Sampling them is
    what keeps the tier inside the budget -- but the sample is worthless if it
    drops the entry the candidate actually needs.
    """
    from autoapply.budget import digest_options

    options = ([f"Country{i} (+{i})" for i in range(2, 250)]
               + ["American Samoa (+1)", "United States of America (+1)"])
    shown, total = digest_options(options, hints=("Country Phone Code",
                                                  "United States", "NJ"))
    assert total == 250, "the model is told the real size of the list"
    assert len(shown) <= 40
    assert "United States of America (+1)" in shown


def test_a_bespoke_picklist_is_sent_whole():
    """Sampling a "How did you hear about us?" would destroy the question --
    there the options *are* the information, and none of them resembles
    anything in the profile."""
    from autoapply.budget import digest_options

    options = ["Job Board", "University Career Fair", "Employee Referral",
               "LinkedIn", "Other"]
    assert digest_options(options, hints=()) == (options, None)


def test_a_sampled_field_is_labelled_as_sampled():
    """Without the marker the model treats the sample as exhaustive and returns
    null for an answer that is in the full list but was not shown."""
    from autoapply.budget import describe

    small = describe(["Mobile", "Home"], hints=())
    assert "options_sampled" not in small and "option_count" not in small

    big = describe([f"Option {i}" for i in range(200)], hints=())
    assert big["options_sampled"] is True
    assert big["option_count"] == 200


def test_a_rate_limit_is_waited_out_not_treated_as_a_failure():
    """OpenAI says how long to wait; the retry believes it, with a floor so a
    "try again in 261ms" does not turn into a spin."""
    from autoapply.llm import _retry_after

    assert _retry_after("", {"retry-after-ms": "261"}) == 0.5
    assert _retry_after("Please try again in 3s", None) == 3.0
    assert _retry_after("", None) == 2.0
    # Never sleep for minutes on end.
    assert _retry_after("", {"retry-after": "600"}) == 30


def test_a_signed_in_run_does_not_restart_in_a_fresh_browser():
    """The agent fallback is for markup we could not read, not a session we
    cannot hand over.

    Mastercard filled 29 fields across five steps behind a sign-in, failed to
    verify, and the fallback launched a fresh browser on a fresh profile at the
    job URL. That browser sees the logged-out posting -- step one -- so it
    reported the entire form missing (agent-missing-0, agent-missing-1), merged
    that into the queue reasons on top of the real ones, and spent the token
    budget the audit tier needed on it.
    """
    from autoapply.models import FillOutcome, Job
    from autoapply.pipeline import _needs_agent_fallback

    job = Job(url="https://example.wd1.myworkdayjobs.com/x", ats="workday")

    stuck = FillOutcome(job=job, verified=False)
    stuck.fields = [Field(id="a", selector="#a", label="A")]
    stuck.filled_ids = ["a"]
    assert _needs_agent_fallback(stuck), "unverified and no session: agent may help"

    stuck.session_bound = True
    assert not _needs_agent_fallback(stuck)


def test_empty_discovery_still_falls_back():
    """The case the fallback exists for must survive the guard."""
    from autoapply.models import FillOutcome, Job
    from autoapply.pipeline import _needs_agent_fallback

    job = Job(url="https://jobs.example.com/x", ats="icims")
    assert _needs_agent_fallback(FillOutcome(job=job))


def test_an_unchanged_step_is_not_audited_twice(tmp_path):
    """A wizard that will not accept a step re-renders it, and the loop fills
    and audits it again -- three times, same questions, same answers, a full
    set of model calls each. The verdict on identical content is identical;
    only the tokens are new, and they are the ones the repair tier then cannot
    get. A repair that changes a value must still be re-audited."""
    from autoapply.repair import _AUDITED, audit_step
    from autoapply.models import Mapping

    calls = []

    class Provider:
        name = "openai"

        def _chat(self, system, user):
            calls.append(user)
            return '{"wrong":[]}'

    class Worker:
        pass

    worker = Worker()
    _AUDITED.pop(id(worker), None)
    fields = [Field(id="a", selector="#a", label="Phone Extension")]
    mapping = Mapping(field_id="a", action="fill", value="224-333-1045",
                      label="Phone Extension", source="rules")

    audit_step(worker, fields, [mapping], PROFILE, provider=Provider(),
               store=_store(tmp_path), ats="workday")
    audit_step(worker, fields, [mapping], PROFILE, provider=Provider(),
               store=_store(tmp_path), ats="workday")
    assert len(calls) == 1, "the same step was audited twice"

    mapping.value = "N/A"
    audit_step(worker, fields, [mapping], PROFILE, provider=Provider(),
               store=_store(tmp_path), ats="workday")
    assert len(calls) == 2, "a changed answer must be audited again"


def test_the_tls_cap_follows_the_proxy_not_an_exported_variable(monkeypatch):
    """A workaround nobody remembers to switch on is not a workaround.

    Chromium's TLS 1.3 ClientHello is reset by the inspecting proxy, so every
    navigation fails with ERR_CONNECTION_RESET -- which reads as "the site is
    down", not "a setting is missing". A run was lost to exactly that. The
    condition the cap is for is one we can test for.
    """
    from autoapply.browser import tls_ceiling

    monkeypatch.delenv("AUTOAPPLY_TLS_MAX", raising=False)
    assert tls_ceiling("http://proxy:8080") == "tls1.2"
    assert tls_ceiling(None) == "", "no proxy, no reason to cap"

    monkeypatch.setenv("AUTOAPPLY_TLS_MAX", "tls1.3")
    assert tls_ceiling("http://proxy:8080") == "tls1.3", "explicit setting wins"
    monkeypatch.setenv("AUTOAPPLY_TLS_MAX", "none")
    assert tls_ceiling("http://proxy:8080") == "", "and can force it off"
