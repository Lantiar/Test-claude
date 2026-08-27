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
    # Weakly keyed on the worker itself. An address is not an identity: it is
    # reused as soon as the object at it is collected, so a run that ended
    # without a verdict left state under a number the next job could be given.
    _AUDITED.pop(worker, None)
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


def test_a_field_we_could_not_write_is_repaired_without_the_form_complaining():
    """The form's objection is one way to learn a field is wrong. Our own
    failure to write it is another -- known earlier, and for anything the form
    does not validate, the only one.

    "Have you ever served in the military?" was answered 'No', the radio would
    not take it, and nothing said so: the write returned a bare None, the form
    raised no error against a field it does not require, and the run ended
    reporting it missing with no note that anything had been attempted.
    """
    from autoapply.models import Mapping
    from autoapply.repair import repair_step

    field = Field(id="mil", selector="#mil", label="Have you ever served?",
                  kind="radio", options=["Yes", "No"])
    mapping = Mapping(field_id="mil", action="fill", value="No",
                      label="Have you ever served?", source="rules")

    seen = {}

    class Provider:
        name = "openai"

        def _chat(self, system, user):
            seen["user"] = user
            return '{"fixes":[{"label":"Have you ever served?","value":"No",'\
                   '"confidence":0.9}]}'

    class Worker:
        def frames(self):
            return []

        def _write(self, f, value):
            return value            # the second attempt sticks

    repaired, notes = repair_step(Worker(), [field], [mapping], PROFILE,
                                  provider=Provider(), store=None,
                                  ats="workday", unwritten=["mil"])
    assert repaired == 1, notes
    assert "could not enter" in seen["user"], "the model is told what happened"


def test_no_errors_and_nothing_unwritten_costs_no_model_call():
    from autoapply.repair import repair_step

    class Provider:
        name = "openai"

        def _chat(self, system, user):
            raise AssertionError("a clean step must not reach the model")

    class Worker:
        def frames(self):
            return []

    assert repair_step(Worker(), [], [], PROFILE, provider=Provider(),
                       store=None, ats="workday") == (0, [])


def test_the_audit_leaves_the_accounts_own_answers_alone(tmp_path):
    """An answer the account already held is not ours to second-guess.

    Workday keeps the candidate profile between applications, so a repeatable
    section arrives populated and the filler correctly leaves it alone. Once
    the mapping records what is actually there, the audit sees
    https://www.nideesh.ai where the profile says https://nideesh.ai, calls it
    wrong over the www, and rewrites a field nobody should be touching -- which
    is the "You can't add duplicate website URLs" rejection that the prefilled
    protection exists to prevent, reached from the other direction. The step
    then re-renders and the run loops on it.
    """
    from autoapply.models import Mapping
    from autoapply.repair import audit_step

    class Provider:
        name = "openai"

        def _chat(self, system, user):
            raise AssertionError("the account's own answer was sent to the audit")

    field = Field(id="url", selector="#url", label="URL")
    mapping = Mapping(field_id="url", action="fill",
                      value="https://www.nideesh.ai", label="URL",
                      source="account")

    assert audit_step(object(), [field], [mapping], PROFILE,
                      provider=Provider(), store=_store(tmp_path),
                      ats="workday") == (0, [])


def test_the_model_is_told_what_day_it_is():
    """Workday's Self Identify page has a required "Date" beside the signature.

    Nothing in a candidate profile says what day it is, so every tier correctly
    declined to answer it -- and because the step could then never validate,
    the answers that same step *had* worked out were discarded on every retry,
    since a rejected step teaches nothing. The run stalled one page short of
    Review on a field whose answer is simply today.

    The date of signing is a fact about the world, not an invented fact about
    the candidate. Which dates it may be used for has to be spelled out, since
    answering a date of birth with today would be an invention.
    """
    from datetime import date
    from autoapply.llm import today_note

    note = today_note()
    assert f"{date.today():%Y-%m-%d}" in note
    for permitted in ("signed", "acknowledgement"):
        assert permitted in note
    for forbidden in ("date of birth", "graduation date", "available to start"):
        assert forbidden in note, f"{forbidden} must be ruled out"


def test_every_tier_that_answers_a_question_knows_the_date():
    """The mapper's answering pass and both repair-side prompts. A tier that
    does not know the date cannot fix the field that stalls the run."""
    from datetime import date
    from autoapply.llm import _prompt

    stamp = f"{date.today():%Y-%m-%d}"
    assert stamp in _prompt([{"label": "Date"}], PROFILE)

    import inspect
    from autoapply import repair

    source = inspect.getsource(repair)
    assert source.count("today_note()") >= 2, "audit and repair both need it"


def test_a_field_the_repair_tier_answered_counts_as_answered():
    """The run reached Review reporting "no answer for required:
    disabilityStatus" three log lines under the repair that answered it and
    the step being accepted.

    fill() records what it writes; the repair tier writes through a different
    path and recorded nothing, so a field it rescued stayed absent from
    filled_ids and the gate blocked on it. The same bookkeeping drives
    missing_required, so the one field the loop had just fixed was the one
    thing standing between the run and a clean pass.
    """
    from autoapply.models import FillOutcome, Job, Mapping

    outcome = FillOutcome(job=Job(url="https://x", ats="workday"))
    outcome.filled_ids = ["name"]
    mappings = [
        Mapping(field_id="name", action="fill", value="Nideesh", source="rules"),
        Mapping(field_id="disabilityStatus", action="fill",
                value="No, I do not have a disability", source="form-repair"),
        Mapping(field_id="skipped", action="unknown", value="", source=""),
    ]
    for m in mappings:
        if (m.source in ("form-repair", "audit") and m.value
                and m.field_id not in outcome.filled_ids):
            outcome.filled_ids.append(m.field_id)

    assert "disabilityStatus" in outcome.filled_ids
    assert "skipped" not in outcome.filled_ids


def test_a_successful_repair_clears_the_failure_that_prompted_it():
    """Verification ran before the repair and its result was ANDed with the
    one after, so a repair could never clear the failure it was made for: the
    step's verdict stayed False no matter what the page ended up holding. The
    state after the repair is the state of the step."""
    ok_before, ok_after = False, True
    step_ok = ok_before
    repaired = 1
    if repaired:
        step_ok = ok_after          # replaces, does not AND
    assert step_ok is True


def test_a_redirect_to_a_marketing_page_is_not_an_application():
    """A closed Ashby posting redirects to Ashby's own marketing homepage.

    The run landed there, discovery found the "Get in Touch" lead-capture box,
    the filler typed the candidate's email address into it, verification
    confirmed the address was indeed in the box, and the outcome came back
    verified=True. The only thing between that and auto mode mailing his
    address to an ATS vendor's sales team was an unrelated CAPTCHA check
    happening to fire.

    Listings close; this is ordinary, not exceptional.
    """
    from autoapply.workers.base import _same_posting

    ashby = "https://jobs.ashbyhq.com/notion/3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c/application"
    assert not _same_posting(ashby, "https://www.ashbyhq.com/")
    assert not _same_posting(ashby, "https://jobs.ashbyhq.com/notion/9999zzzz/application")

    # A company's careers site handing off to its ATS tenant is how a large
    # share of real links work: same job, different domain. Comparing hosts
    # rejected the normal case -- this is the posting AMD actually serves.
    assert _same_posting("https://careers.amd.com/careers-home/jobs/91176",
                         "https://campus-amd.icims.com/jobs/91176/login")
    assert _same_posting("https://careers.bny.com/jobs/81251",
                         "https://eofe.fa.us2.oraclecloud.com/hcmUI/"
                         "CandidateExperience/en/sites/BNY-Careers/job/81251")

    # An ATS rewriting its own URL is normal and must still match.
    wd = ("https://mastercard.wd1.myworkdayjobs.com/Campus/job/OFallon-Missouri/"
          "Software-Engineer-Intern--Summer-2027---United-States_R-287618-1")
    assert _same_posting(wd, wd.replace("/Campus", "/en-US/Campus") + "/apply/applyManually")
    gh = "https://boards.greenhouse.io/cloudflare/jobs/1234567"
    assert _same_posting(gh, "https://job-boards.greenhouse.io/cloudflare/jobs/1234567?gh_src=x")


def test_the_gate_refuses_a_page_that_is_not_an_application():
    """The second line of defence, for a page reached without a redirect."""
    from autoapply.gate import looks_like_an_application
    from autoapply.models import FillOutcome, Job

    job = Job(url="https://x", ats="ashby")

    lead_capture = FillOutcome(job=job)
    lead_capture.fields = [Field(id="email", selector="#e", label="Email")]
    lead_capture.filled_ids = ["email"]
    assert not looks_like_an_application(lead_capture)

    real = FillOutcome(job=job)
    real.fields = [Field(id="n", selector="#n", label="First Name"),
                   Field(id="e", selector="#e", label="Email"),
                   Field(id="r", selector="#r", label="Resume", kind="file")]
    real.filled_ids = ["n", "e", "r"]
    assert looks_like_an_application(real)

    # What is asked decides it, not how much: one recognisable question is
    # enough, and a marketing page's lone "Email" is not one.
    one_real = FillOutcome(job=job)
    one_real.fields = [Field(id="n", selector="#n", label="First name")]
    one_real.filled_ids = ["n"]
    assert looks_like_an_application(one_real)


def test_a_login_url_serving_the_applications_first_page_is_not_a_wall():
    """AMD's iCIMS tenant serves the application's own first step from
    /jobs/91176/login: "Please enter your email to begin the application
    process", an email box, a privacy acceptance and a Next button, no password
    anywhere. Read as a sign-in wall, the run tried to authenticate, found
    nothing to authenticate with, and stopped on the first page of an
    application it had in fact already filled in."""
    class Frame:
        def __init__(self, text, password=False):
            self.text, self.password = text, password

        def query_selector(self, sel):
            return object() if (self.password and "password" in sel) else None

        def inner_text(self, sel):
            return self.text

    class W:
        from autoapply.workers.base import Worker as _W
        ENTRY_TEXT = _W.ENTRY_TEXT
        looks_like_an_entry_step = _W.looks_like_an_entry_step

        def __init__(self, frames):
            self._frames = frames

        def frames(self):
            return self._frames

    entry = W([Frame("Please enter your email to begin the application process")])
    assert entry.looks_like_an_entry_step()

    real_login = W([Frame("Sign in to your account", password=True)])
    assert not real_login.looks_like_an_entry_step()

    # A password field settles it even on a page that also mentions applying.
    both = W([Frame("Sign in to begin the application process", password=True)])
    assert not both.looks_like_an_entry_step()


def test_a_click_path_that_repeats_itself_is_not_an_answer(tmp_path):
    """Oracle's "City, state, country" made the explore tier click "Search by
    Location" six times in a row. It reported the field solved with a recipe
    reading "Search by Location > Search by Location > ..." and, because the
    form raised no complaint, that got committed as a lesson.

    Learning it is worse than learning nothing: a taught answer outranks the
    rules beneath it, so every later run would replay the loop in preference
    to anything that might work.
    """
    from autoapply.repair import _teach, commit_lessons, usable

    assert usable("Search by Location > Search by Location > Search by Location") == ""
    assert usable("A > A") == ""
    # A genuine nested route keeps its distinct levels.
    assert usable("Job Board > Handshake") == "Job Board > Handshake"
    assert usable("Mobile") == "Mobile"

    store = _store(tmp_path)
    _teach(store, "oracle", "City, state, country", "X > X > X")
    assert commit_lessons(store, "oracle") == []


def test_explore_stops_when_the_model_picks_the_same_thing_twice():
    """Clicking the same control again is not progress, and six of them in a
    row is how the loop above got built."""
    import inspect
    from autoapply import explore

    source = inspect.getsource(explore.solve_field)
    assert "clicked" in source
    assert 'choice["label"] in clicked' in source


def test_a_lesson_the_reviewer_objects_to_is_retracted(tmp_path):
    """A wrong lesson was permanent. It outranks every other source, is held
    out of re-auditing, and there was no way to remove one -- so the store had
    accumulated "Last Name = Kumar" against a profile reading "Bharath Kumar",
    and three answers learned off a careers *search* page. The presubmit
    reviewer could see the damage and had no way to undo it.
    """
    from autoapply.gate import forget_flagged
    from autoapply.models import FillOutcome, Job, Mapping

    store = _store(tmp_path)
    store.record_correction(signature("workday", "Last Name*"), "Last Name*", "Kumar")
    store.record_correction(signature("workday", "State"), "State", "New Jersey")

    job = Job(url="https://x", ats="workday")
    outcome = FillOutcome(job=job, mappings=[
        Mapping(field_id="ln", action="fill", value="Kumar",
                label="Last Name*", source="learned"),
        Mapping(field_id="st", action="fill", value="New Jersey",
                label="State", source="learned"),
        Mapping(field_id="c", action="fill", value="Monroe Township",
                label="City", source="rules"),
    ])

    dropped = forget_flagged(outcome, [
        "Last Name answer 'Kumar' contradicts profile last name 'Bharath Kumar'"],
        store)
    assert dropped == ["Last Name*"]
    assert store.literal_for(signature("workday", "Last Name*")) is None
    # Everything it did not object to survives.
    assert store.literal_for(signature("workday", "State")) == "New Jersey"


def test_only_taught_answers_are_retracted(tmp_path):
    """A rule or a model answer is re-derived next run anyway; a lesson is the
    only thing that persists unchallenged, so it is the only thing to drop."""
    from autoapply.gate import forget_flagged
    from autoapply.models import FillOutcome, Job, Mapping

    store = _store(tmp_path)
    store.record_correction(signature("workday", "City"), "City", "Monroe Township")
    outcome = FillOutcome(job=Job(url="https://x", ats="workday"), mappings=[
        Mapping(field_id="c", action="fill", value="Monroe Township",
                label="City", source="rules")])
    assert forget_flagged(outcome, ["City answer is wrong"], store) == []
    assert store.literal_for(signature("workday", "City")) == "Monroe Township"


def test_a_resume_dropzone_does_not_make_a_search_page_an_application():
    """BNY's careers page carries "Upload or drag and drop your PDF resume file
    here to get AI recommended jobs" -- a job-recommendation widget. The word
    "resume" and the file input both matched, so the search page passed as an
    application and the filler typed the job title into the site's own search
    box before the gate could object.

    A file input is what the search page and the real form have in common. An
    application asks who you are.
    """
    from autoapply.gate import looks_like_an_application
    from autoapply.models import FillOutcome, Job

    def page(*labels, files=()):
        o = FillOutcome(job=Job(url="https://x", ats="oracle"))
        o.fields = [Field(id=str(i), selector=f"#{i}", label=l,
                          kind="file" if l in files else "text")
                    for i, l in enumerate(labels)]
        return o

    dropzone = ("Upload or drag and drop your PDF resume file here to get AI "
                "recommended jobs based on your skills and experience.")
    assert not looks_like_an_application(page(
        "Job title, skill, keyword", "City, state, country", dropzone,
        "Accept the terms and conditions", files=(dropzone,)))

    assert looks_like_an_application(page(
        "First Name", "Last Name", "Email", "Resume", files=("Resume",)))
    assert looks_like_an_application(page("First name"))
    assert not looks_like_an_application(page("Email"))


def test_a_question_we_cannot_read_is_left_unanswered():
    """TikTok's form carries fields labelled "", "field-90", and the box's own
    placeholder "Please enter". Asked to answer them, the model obliged: a
    self-introduction went into one labelled "Please enter" and a portfolio URL
    into two unlabelled boxes. The presubmit reviewer objected to exactly
    those, and it was right -- each is a guess wearing an answer's clothes.

    Same rule the rest of the design runs on, a null beats a guess, applied to
    the question instead of the value.
    """
    from autoapply.mapper import _readable_question

    for unreadable in ("", "field-0", "field-90", "Please enter", "(select)",
                       "select", "--", "Please select", "Enter"):
        assert not _readable_question(
            Field(id="x", selector="#x", label=unreadable)), unreadable

    for real in ("First Name", "Self-introduction", "YYYY", "GPA",
                 "Why do you want this role?", "School name"):
        assert _readable_question(
            Field(id="x", selector="#x", label=real)), real


def test_an_unreadable_field_is_reported_not_silently_skipped(tmp_path):
    """It has to reach the gate as unanswered, so a required one still blocks
    and a person can see what was left alone."""
    store = _store(tmp_path)
    fields = [Field(id="a", selector="#a", label="First Name"),
              Field(id="b", selector="#b", label="field-90", required=True)]
    out = {m.field_id: m for m in map_fields(fields, PROFILE, "unknown",
                                             store=store, provider=None)}
    assert out["a"].action == "fill"
    assert out["b"].action == "unknown"
    assert out["b"].source == "unreadable-label"


def test_a_spare_link_only_answers_a_question_about_a_link():
    """_spread_repeated_values hands a spare link to any repeated field that is
    unanswered or duplicated. That was harmless while the only repeated fields
    in sight were Workday's two URL boxes.

    TikTok's form repeats whole records -- Title, Company name, Description,
    Project URL, once per project -- so those labels repeat too, and the
    spread put a GitHub URL into "Title" and a personal site into "Company
    name". The presubmit reviewer caught both.
    """
    from autoapply.mapper import _spread_repeated_values
    from autoapply.models import Mapping

    profile = {"links": {"portfolio": "https://nideesh.ai",
                         "github": "https://github.com/nb923"}}
    mappings = [
        Mapping(field_id="t1", action="unknown", label="Title"),
        Mapping(field_id="c1", action="unknown", label="Company name"),
        Mapping(field_id="d1", action="unknown", label="Description"),
        Mapping(field_id="u1", action="unknown", label="Project URL"),
        Mapping(field_id="u2", action="unknown", label="URL"),
    ]
    _spread_repeated_values(mappings, {m.field_id for m in mappings}, profile)
    got = {m.field_id: (m.action, m.value) for m in mappings}

    assert got["t1"][0] == "unknown", f"Title got {got['t1'][1]!r}"
    assert got["c1"][0] == "unknown", f"Company name got {got['c1'][1]!r}"
    assert got["d1"][0] == "unknown", f"Description got {got['d1'][1]!r}"
    # The link-shaped ones still get distinct links, which is what it is for.
    assert got["u1"][0] == "fill" and got["u2"][0] == "fill"
    assert got["u1"][1] != got["u2"][1]


def test_only_a_link_this_tool_left_behind_is_taken_back():
    """The account's memory is only worth deferring to while it is right --
    but proving it wrong needs evidence, not taste.

    TikTok saves the application profile between runs, so a value an earlier
    run of this tool got wrong comes back on the next one as an answer
    "already on the account". Title held https://github.com/nb923/NutriCart
    for six iterations: the filler left it alone as the account's own, the
    presubmit reviewer objected to it every run, and nothing could act on the
    objection because the value had never been ours to retract.

    The counterweight is that most of what TikTok holds is the *resume*, parsed
    by TikTok, and richer than config/profile.json -- which carries no project
    or award records at all. A rule that overwrote whatever looked wrong for a
    label would replace parsed resume entries with generic profile answers.
    So the test is only ever: is this one of our own profile links, in a field
    that never asked for a link.
    """
    from autoapply.mapper import is_a_stray_link

    profile = {"links": {"github": "https://github.com/nb923",
                         "portfolio": "https://nideesh.ai"}}

    assert is_a_stray_link("Title", "https://github.com/nb923", profile)
    # Spelling is not the question -- the same link is the same link.
    assert is_a_stray_link("Company name", "www.nideesh.ai/", profile)

    # A link the resume parser found. Not ours, not touched.
    assert not is_a_stray_link("Title", "https://acme.example.com", profile)
    # A field that is asking for a link is answered by one.
    assert not is_a_stray_link("Project URL", "https://github.com/nb923", profile)
    # Prose that cites a repo is prose, and a parsed record stays put.
    assert not is_a_stray_link(
        "Description", "Built the parser; see https://github.com/nb923", profile)
    assert not is_a_stray_link("Title", "Data Analyst Intern", profile)
    assert not is_a_stray_link("Title", "", profile)


def test_a_form_reached_by_signing_in_is_not_retried_from_a_fresh_browser():
    """The single-page lane must mark the session too, not just the wizard lane.

    _needs_agent_fallback has guarded against retrying a session-bound form
    since the wizard lane was written, and only WizardWorker.walk ever set the
    flag. TikTok is a single-page form behind a login, so a run that signed in
    and filled 49 of 66 fields was handed to the agent fallback, which launched
    a fresh browser at the job URL, met the sign-in wall, and reported "no form
    fields discovered; sign-in or account creation required". The pipeline
    merged that into the queue row, and the verdict on the run described a page
    it had never reached.
    """
    from autoapply.models import FillOutcome, Job
    from autoapply.pipeline import _needs_agent_fallback
    from autoapply.workers.base import Worker

    field = Field(id="fn", selector="#fn", label="First name")

    class Page:
        url = "https://lifeattiktok.com/resume/123/apply"

        def title(self):
            return "Apply"

    class SignsIn(Worker):
        def __init__(self):
            super().__init__(Page())
            self.walls = 1

        def open(self, job):
            pass

        def needs_auth(self):
            hit, self.walls = self.walls > 0, 0
            return hit

        def try_sign_in(self, job):
            return True, "signed in"

        def settle_step(self, **kw):
            pass

        def discover(self):
            return [field]

        def saw_captcha(self):
            return False

        def screenshot(self, job, where):
            return ""

        def fill(self, job, fields, mappings, screenshot_dir, profile=None):
            out = FillOutcome(job=job, fields=fields, mappings=mappings)
            out.filled_ids = ["fn"]
            return out

        def _review_and_repair(self, *a, **kw):
            pass

    job = Job(url="https://lifeattiktok.com/search/123", ats="unknown")
    outcome = SignsIn().run(job, PROFILE, _store_in_memory(), None, "")

    assert outcome.session_bound, (
        "the form was reached by signing in; a fresh browser cannot see it")
    outcome.verified = False
    assert not _needs_agent_fallback(outcome), (
        "the fallback would start over logged out and overwrite the findings")


def _store_in_memory():
    return Store(":memory:")


def test_every_provider_implements_what_every_tier_calls():
    """A provider missing _chat switches off the whole review architecture.

    The three mapping methods were the entire interface AnthropicProvider
    implemented, and every tier built since -- audit, repair, explore,
    presubmit, run plausibility -- calls provider._chat(). On Anthropic each
    one raised AttributeError into an `except Exception` that reports the tier
    as unavailable, so choosing that provider left a run in which nothing was
    ever found wrong and no log said why.
    """
    import inspect
    from autoapply import llm

    required = {"map_fields", "answer_fields", "generate", "_chat"}
    providers = [obj for name, obj in vars(llm).items()
                 if inspect.isclass(obj) and name.endswith("Provider")
                 and name not in ("LLMProvider", "RulesProvider")]
    # RulesProvider is excluded by design rather than by omission: it is the
    # no-model path, and every tier checks name == "rules" and returns before
    # calling anything. OpenAICompatProvider names itself in __init__, so the
    # exclusion has to be by class, not by reading `name` off the class.
    assert len(providers) >= 2, f"only found {[p.__name__ for p in providers]}"
    for provider in providers:
        missing = required - set(dir(provider))
        assert not missing, f"{provider.__name__} is missing {sorted(missing)}"


def test_the_reviewer_is_not_asked_to_judge_what_the_run_did_not_do():
    """Two of Mastercard's three blockers described the payload, not the run.

    "Repeated 'URL*' question with the same answer": Workday keeps the
    candidate profile between applications, so both URL entries arrived holding
    https://www.nideesh.ai and the filler correctly left them alone -- writing
    into a populated entry is what produced "You can't add duplicate website
    URLs". The run had not touched either field and was right not to.

    "Page title is empty": Workday sets the title asynchronously, so reading it
    a moment early returns "". Sent as an empty string, absent metadata became
    a finding about the application.
    """
    from autoapply.models import FillOutcome, Job, Mapping
    from autoapply import sanity

    job = Job(url="https://mastercard.wd1.myworkdayjobs.com/x", ats="workday")
    outcome = FillOutcome(job=job)
    for i in (1, 2):
        outcome.fields.append(Field(id=f"u{i}", selector=f"#u{i}", label="URL*"))
        outcome.mappings.append(
            Mapping(field_id=f"u{i}", action="fill", label="URL*",
                    value="https://www.nideesh.ai", source="account"))
    outcome.fields.append(Field(id="fn", selector="#fn", label="First Name"))
    outcome.mappings.append(Mapping(field_id="fn", action="fill",
                                    label="First Name", value="Nideesh",
                                    source="rules"))

    digest = sanity._form_digest(outcome)
    account = [p for p in digest if p.get("already_on_the_account")]
    assert [p["question"] for p in account] == ["URL*", "URL*"]
    ours = [p for p in digest if not p.get("already_on_the_account")]
    assert [p["question"] for p in ours] == ["First Name"], ours

    sent = {}

    class Provider:
        name = "openai"

        def _chat(self, system, user):
            sent["system"], sent["user"] = system, user
            return '{"plausible": true, "problems": []}'

    sanity.review_run(outcome, "https://mastercard.wd1.myworkdayjobs.com/x",
                      "", Provider())
    assert "page_title" not in sent["user"], (
        "an empty title is absent metadata, not a finding about the run")
    assert "already_on_the_account" in sent["system"], (
        "the reviewer has to be told which answers the run did not enter")


def test_a_finding_about_a_question_that_is_not_on_the_form_is_dropped():
    """The reviewer can only add blockers, so an invented one has no counterweight.

    Mastercard's run reached review with 40 of 47 fields filled and verified,
    and was blocked by three findings, all false. One was "Answer for 'Have you
    ever worked for Mastercard?' contradicts previous answer". The form asks
    that twice, in two wordings, and the run answered "No" to both.

    A finding about an answer now has to say which question it is about, and
    the question has to exist. That does not let the reviewer approve anything
    -- it still cannot clear a block -- it holds it to the evidence it was
    given. A finding about the page carries no question and stands: those are
    the ones this tier exists for.
    """
    from autoapply.sanity import _cited

    pairs = [{"question": "First Name", "answer": "Nideesh"},
             {"question": "Have you ever worked for Mastercard?", "answer": "No"}]

    kept = _cited([
        {"question": "Have you ever worked for Mastercard?", "answer": "No",
         "problem": "contradicts a previous answer"},
        {"question": "Salary expectation", "problem": "left blank"},
        {"question": None, "problem": "ended on a job search page"},
        {"problem": "the page is not an application"},
    ], pairs)

    assert any("contradicts" in k for k in kept), kept
    assert any("search page" in k for k in kept), kept
    assert any("not an application" in k for k in kept), kept
    assert not any("Salary expectation" in k for k in kept), (
        "a finding about a question this form never asked must not block")

    # Plain strings still work: the reviewer is asked for objects, not held to
    # them, and a blocker is too important to lose to a formatting slip.
    assert _cited(["ended on a job search page"], pairs) == [
        "ended on a job search page"]

    # The same discipline one level down. Citing a real question was not
    # enough: Mastercard was blocked twice more by findings correctly
    # addressed to real questions and wrong about them -- "Race/Ethnicity:
    # answer entered into a field that plainly asks something else", where the
    # answer was "Asian", and "worked for Mastercard: duplicate question with
    # conflicting answers", where both said "No".
    assert _cited([{"question": "First Name", "answer": "Bharath",
                    "problem": "wrong given name"}], pairs) == [], \
        "an objection to an answer nobody entered must not block"
    assert _cited([{"question": "First Name", "answer": "Nideesh",
                    "problem": "is a surname"}], pairs) == [
        "First Name: is a surname"], "a real objection must survive"


def test_the_vendors_own_marketing_site_is_not_a_hop_to_the_ats():
    """Two reasons the Notion run left a form it had already found.

    GenericWorker.open() hunts for a company careers page's link through to the
    real ATS -- AMD's careers.amd.com wrapper points at campus-amd.icims.com --
    and it ran even when the base worker had already stopped on the
    application. So on Notion it went looking anyway.

    And what it found was Ashby's own footer link. The offsite test compared
    hostnames exactly, so on jobs.ashbyhq.com a link to www.ashbyhq.com read as
    a hop to a different site; the domain is a real ATS domain, so detect()
    agreed it was one. It navigated away from a page holding 23 discovered
    fields, and the run reported the posting closed.
    """
    from autoapply.workers.generic import GenericWorker

    class Anchor:
        def __init__(self, href, text):
            self._href, self._text = href, text

        def get_attribute(self, _):
            return self._href

        def inner_text(self):
            return self._text

    class Page:
        def __init__(self, url, anchors):
            self.url, self._anchors = url, anchors

        def query_selector_all(self, _):
            return self._anchors

    ashby = GenericWorker(Page(
        "https://jobs.ashbyhq.com/notion/3fba1c39/application",
        [Anchor("https://www.ashbyhq.com/", "Powered by Ashby"),
         Anchor("https://www.ashbyhq.com/apply-with-ashby", "Apply with Ashby")]))
    assert ashby._ats_apply_link() is None, "the vendor's own site is not the form"

    # The case the hop exists for still works.
    amd = GenericWorker(Page(
        "https://careers.amd.com/careers-home/jobs/91176",
        [Anchor("https://campus-amd.icims.com/jobs/91176/login", "Apply")]))
    assert amd._ats_apply_link() == "https://campus-amd.icims.com/jobs/91176/login"

    # And once the base worker has stopped on the application, open() does not
    # go looking at all -- not even for a hop that would otherwise qualify.
    from autoapply.models import Job
    from autoapply.workers.base import Worker

    went = []

    class Page2(Page):
        def goto(self, url, **kw):
            went.append(url)

        def wait_for_timeout(self, _):
            pass

    class Stopped(GenericWorker):
        def open(self, job):
            # Stand in for the base worker finding the form and returning.
            self._landing = (self.page.url, [object()])
            # Put the real method back, rather than deleting the attribute.
            # `del` removed Worker.open from the class outright, so every test
            # that ran afterwards in the same process failed with "'super'
            # object has no attribute 'open'" -- six of them, in a file this
            # test has nothing to do with.
            original = Worker.open
            Worker.open = lambda *a, **k: None
            try:
                GenericWorker.open(self, job)
            finally:
                Worker.open = original

    worker = Stopped(Page2(
        "https://careers.amd.com/careers-home/jobs/91176",
        [Anchor("https://campus-amd.icims.com/jobs/91176/login", "Apply")]))
    worker.open(Job(url=worker.page.url, ats="generic"))
    assert went == [], f"navigated away from the application to {went}"


def test_a_sign_in_is_not_condemned_by_one_early_reading():
    """Mastercard's run signed in, then reported that it had not.

    The check asked needs_auth() once, about 2.5 seconds after clicking, and
    Workday's SPA had not swapped the sign-in form out yet. So a sign-in that
    worked was reported as "still on a sign-in page after submitting"; the run
    went off to register an account it already had, found the create-account
    page equally unrendered, and abandoned the application. The screenshot it
    saved shows the candidate signed in, on Workday's My Information step,
    with the step rail all the way to Review.

    networkidle does not settle this -- a Workday page keeps connections open,
    so the wait returns immediately and the fixed pause after it is the only
    thing between the click and the verdict.
    """
    import os
    from autoapply import login

    os.environ["SIGNIN_SETTLE_SECONDS"] = "5"

    class Frame:
        def query_selector(self, _):
            return None

    class Page:
        url = "https://x.wd1.myworkdayjobs.com/en-US/Campus/job/y/apply/applyManually"

        def __init__(self):
            self.waited = 0

        def wait_for_timeout(self, ms):
            self.waited += ms

        def wait_for_load_state(self, *a, **k):
            pass

    class Worker:
        """Still walled for the first two readings, then through."""

        def __init__(self):
            self.page = Page()
            self.reads = 0

        def frames(self):
            return [Frame()]

        def needs_auth(self):
            self.reads += 1
            return self.reads <= 2

    worker = Worker()
    stubs = {"_on_registration_form": lambda *a: False,
             "_find": lambda *a: (object(), Frame()),
             "_set": lambda *a: None,
             "_accept_required_consent": lambda *a: None,
             "_clear_code": lambda *a: (True, ""),
             "_click": lambda *a: True}
    # Restored afterwards. Left in place, these leak into every test that runs
    # after this one in the same process -- which is how a green suite came to
    # report a failure in a test that passes on its own.
    original = {name: getattr(login, name) for name in stubs}
    try:
        for name, stub in stubs.items():
            setattr(login, name, stub)
        ok, detail = login.sign_in(worker, {"email": "a@b.c", "password": "x"})
    finally:
        for name, value in original.items():
            setattr(login, name, value)

    assert ok, detail
    assert worker.reads > 1, "it gave up on the first reading again"


def test_the_agent_does_not_start_over_on_a_form_that_is_nearly_filled():
    """Unverified on its own is not a reason to begin again.

    Verification fails over one field in twenty-two, and a fresh browser does
    not fix one field: it re-reads the same page from scratch, spends the
    tokens the audit and repair tiers want, and files a second opinion on top
    of the real findings. On Notion that meant a DOM pass which had filled 20
    of 22 and taught itself three answers being followed by an agent clicking
    around the live form it had just completed.

    What the fallback is for is a lane that got nowhere -- markup it cannot
    read at all, an iframe, a shadow root, a tenant that renders differently.
    """
    from autoapply.models import FillOutcome, Job
    from autoapply.pipeline import _needs_agent_fallback

    job = Job(url="https://jobs.ashbyhq.com/notion/x/application", ats="ashby")

    def outcome(found, filled, verified=False):
        o = FillOutcome(job=job, verified=verified)
        o.fields = [Field(id=f"f{i}", selector=f"#f{i}", label=f"Q{i}")
                    for i in range(found)]
        o.filled_ids = [f"f{i}" for i in range(filled)]
        return o

    assert not _needs_agent_fallback(outcome(22, 20)), \
        "a nearly-complete form is not a failure to start over from"
    assert _needs_agent_fallback(outcome(22, 0)), "nothing filled is"
    assert _needs_agent_fallback(FillOutcome(job=job)), "empty discovery is"
    assert _needs_agent_fallback(outcome(22, 2)), "barely anything filled is"
    # And a verified run never needed the fallback in the first place.
    assert not _needs_agent_fallback(outcome(22, 22, verified=True))


def test_the_run_reviewer_can_retract_a_lesson_too(tmp_path):
    """Only the presubmit reviewer could, so a finding from the other one
    blocked the run and changed nothing.

    Mastercard's "How Did You Hear About Us?" held "University Job Board",
    learned, against a profile that says "Company website" -- and would have
    held it on every future run. A lesson outranks the rules beneath it and
    replays with confidence 1.0, so whichever tier notices it is wrong has to
    be able to take it back.
    """
    from autoapply.models import FillOutcome, Job, Mapping
    from autoapply.gate import forget_flagged
    from autoapply.mapper import signature

    store = _store(tmp_path)
    label = "How Did You Hear About Us?*"
    store.record_correction(signature("workday", label), label,
                            "University Job Board")
    assert store.literal_for(signature("workday", label)) == "University Job Board"

    job = Job(url="https://x.wd1.myworkdayjobs.com/y", ats="workday")
    outcome = FillOutcome(job=job)
    outcome.mappings = [Mapping(field_id="h", action="fill", label=label,
                                value="University Job Board", source="learned")]

    dropped = forget_flagged(outcome, [
        "Answer for 'How Did You Hear About Us?' is a contradiction to the "
        "profile's source of 'Company website'."], store)

    assert dropped == [label], dropped
    assert store.literal_for(signature("workday", label)) is None, \
        "the lesson survived the objection"


def test_the_mailbox_line_is_drawn_when_the_code_was_asked_for():
    """Four codes arrived and the waiter ignored all four.

    _wait_gmail hides everything already in the mailbox, which is right -- an
    unfiltered wait would hand back a code out of unrelated mail, and an old
    code is worse than none. But the line was drawn when the wait began, and
    the wait begins after the click that asks for the code, once the page has
    been given up to fifteen seconds to settle. BNY's mail lands inside that
    gap: four "BNY Careers - Confirm Your Identity" messages sat in the
    mailbox while the waiter timed out at 180 seconds having skipped every one
    of them as pre-existing.
    """
    import inspect
    import time
    from autoapply import mailcode
    from autoapply.workers.base import Worker

    assert "since" in inspect.signature(mailcode.wait_for_code).parameters
    assert "since" in inspect.signature(mailcode._wait_gmail).parameters

    asked = {}

    def fake_wait_gmail(needles, timeout, poll, since=None):
        asked["since"] = since
        return "123456"

    original = mailcode._wait_gmail
    configured = mailcode._gmail_configured
    try:
        mailcode._wait_gmail = fake_wait_gmail
        mailcode._gmail_configured = lambda: True
        pressed = time.time() - 30
        waiter = Worker(object())._mail_waiter(pressed)
        assert waiter(["bny"]) == "123456"
        assert asked["since"] == pressed, (
            "the waiter must look back to the click, not to its own start")
    finally:
        mailcode._wait_gmail = original
        mailcode._gmail_configured = configured


def test_a_date_the_widget_reformatted_is_the_same_date():
    """Notion's whole run failed verification over a correct graduation date.

    A date picker does more than re-punctuate: it reorders, and it fills in
    what you left out. "2028-05" came back as "05/01/2028" -- the same month
    of the same year, with a day the widget chose. Squashing gives 202805
    against 05012028, which share no useful substring, so the comparison that
    rescues a reformatted phone number could not rescue this.

    The numbers a date is made of are what to compare: everything supplied has
    to still be there, and the widget may add its own.
    """
    from autoapply.verify import _matches

    assert _matches("2028-05", "05/01/2028", "text"), "the case that failed"
    assert _matches("2028-05-01", "05/01/2028", "text"), "reordered"
    assert _matches("05/2028", "2028-05", "text"), "reordered, either way round"

    # A different date is still a different date.
    assert not _matches("2028-05", "05/01/2027", "text"), "wrong year"
    assert not _matches("2028-05", "06/01/2028", "text"), "wrong month"
    assert not _matches("2028-05", "", "text"), "empty is never a match"

    # And the reformatting this already handled keeps working.
    assert _matches("224-333-1045", "2243331045", "text")
    assert not _matches("224-333-1045", "224-333-9999", "text")
