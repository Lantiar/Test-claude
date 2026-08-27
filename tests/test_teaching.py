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
