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

PROFILE = {"identity": {"first_name": "Nideesh", "phone": "224-333-1045"}}


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "teach.sqlite"))


def test_a_rule_gets_phone_extension_wrong():
    """The premise. 'Phone Extension' matches the phone rule, so the
    deterministic pass fills it with the entire phone number."""
    assert match_rule("Phone Extension") == "identity.phone"


def test_a_taught_answer_outranks_a_rule_that_matches(tmp_path):
    """The fix. Without it the teaching store is inert for every field a rule
    already claims -- which is exactly the set of fields that need teaching."""
    store = _store(tmp_path)
    field = Field(id="ext", selector="#ext", label="Phone Extension")

    before = map_fields([field], PROFILE, "workday", store=store)[0]
    assert before.value == "224-333-1045", "expected the rule to misfire first"

    store.record_correction(signature("workday", "Phone Extension"),
                            "Phone Extension", "")
    store.record_correction(signature("workday", "Phone Extension"),
                            "Phone Extension", "N/A")

    after = map_fields([field], PROFILE, "workday", store=store)[0]
    assert after.source == "learned"
    assert after.value == "N/A"


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
