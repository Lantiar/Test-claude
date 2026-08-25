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
    from autoapply.repair import _teach

    store = _store(tmp_path)
    field = Field(id="phoneType", selector="#phoneType", label="Phone Device Type",
                  kind="combobox", options=["Mobile", "Home", "Work"],
                  required=True)

    first = map_fields([field], PROFILE, "workday", store=store)[0]
    assert first.action == "unknown", "profile has no answer for this"

    _teach(store, "workday", "Phone Device Type", "Mobile")

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

    for label in ("Phone Device Type*", "Phone Extension", "Country Phone Code*"):
        assert match_rule(label) is None, f"{label} should not resolve to a rule"


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
    from autoapply.repair import _teach

    store = _store(tmp_path)
    field = Field(id="phoneType", selector="#phoneType", label="Phone Device Type",
                  kind="select", options=["Mobile", "Home"], required=True)

    for prompt in ("Select One", "Select...", "Choose an option", "--", ""):
        _teach(store, "workday", "Phone Device Type", prompt)
        assert map_fields([field], PROFILE, "workday", store=store)[0].source \
            != "learned", f"taught the placeholder {prompt!r}"

    _teach(store, "workday", "Phone Device Type", "Mobile")
    assert map_fields([field], PROFILE, "workday", store=store)[0].value == "Mobile"


def test_workday_dropdown_options_exclude_the_prompt():
    from autoapply.workers.workday import PLACEHOLDER_OPTION

    for prompt in ("Select One", "Select...", "Choose an option", "--"):
        assert PLACEHOLDER_OPTION.match(prompt), prompt
    for real in ("Mobile", "Home", "Selected Applicant"):
        assert not PLACEHOLDER_OPTION.match(real), real
