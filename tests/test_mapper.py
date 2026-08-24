"""Mapper rules: coverage, and the substring traps that bit us once already."""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autoapply.mapper import map_fields, normalize_label, resolve_option  # noqa: E402
from autoapply.models import Field                                        # noqa: E402

def _profile() -> dict:
    """Example profile, with the resume pointed at the test fixture."""
    p = json.load(open("config/profile.example.json"))
    p["files"]["resume"] = str(pathlib.Path(__file__).parent / "fixtures" / "resume.pdf")
    return p


PROFILE = _profile()


def mapping_for(label, kind="text", options=None, required=False):
    f = Field(id="x", selector="#x", label=label, kind=kind,
              options=options or [], required=required)
    return map_fields([f], PROFILE, "greenhouse")[0]


def test_normalize_strips_required_markers():
    assert normalize_label("First Name *") == "first name"
    assert normalize_label("Email (required)") == "email"


def test_core_identity_fields():
    assert mapping_for("First Name *").value == "Jane"
    assert mapping_for("Last Name *").value == "Doe"
    assert mapping_for("Full name").value == "Jane Doe"
    assert mapping_for("Email").value == "jane.doe@example.com"


def test_sensitive_fields_fill_from_profile_not_queue():
    """The whole point of the current design: these answer, they don't block."""
    auth = mapping_for("Are you legally authorized to work in the United States?",
                       "select", ["Yes", "No"])
    assert auth.action == "fill" and auth.value == "Yes"

    spon = mapping_for("Will you now or in the future require sponsorship?",
                       "select", ["Yes", "No"])
    assert spon.action == "fill" and spon.value == "No"

    assert mapping_for("Desired Salary").action == "fill"
    assert mapping_for("Gender", "select",
                       ["Male", "Female", "Decline To Self Identify"]).action == "fill"


def test_substring_traps():
    """Short patterns must not match inside longer words."""
    # "ethnicity" contains "city"
    assert mapping_for("Race / Ethnicity", "select",
                       ["Asian", "Decline To Self Identify"]).value == "Decline To Self Identify"
    # "excellent" contains "cell"
    assert mapping_for("Tell us about an excellent project").action == "unknown"
    # "United States" contains "state"
    assert mapping_for("Are you authorized to work in the United States?",
                       "select", ["Yes", "No"]).value == "Yes"


def test_unanswerable_required_field_is_unknown():
    m = mapping_for("Describe your favourite compiler optimization",
                    "textarea", required=True)
    assert m.action == "unknown"


def test_optional_unanswerable_field_is_skipped():
    thin = json.loads(json.dumps(PROFILE))
    thin["files"]["cover_letter"] = ""
    f = Field(id="cl", selector="#cl", label="Cover Letter", kind="file")
    assert map_fields([f], thin, "greenhouse")[0].action == "skip"


def test_resolve_option_handles_decline_variants():
    assert resolve_option("Decline to self-identify",
                          ["Asian", "Decline To Self Identify"]) == "Decline To Self Identify"
    assert resolve_option("No", ["Yes", "No"]) == "No"
    assert resolve_option("Yes", ["Yes, I have a disability",
                                  "No, I don't"]) == "Yes, I have a disability"
    assert resolve_option("Nonsense", ["Yes", "No"]) is None


def test_human_correction_is_reused_next_time(tmp_path):
    """A typed answer for a field the profile can't answer should stick."""
    from autoapply.mapper import signature
    from autoapply.store import Store

    store = Store(str(tmp_path / "learn.sqlite"))
    f = Field(id="ai", selector="#ai", label="Additional information", kind="textarea")

    first = map_fields([f], PROFILE, "lever", store=store)[0]
    assert first.action == "unknown"

    store.record_correction(signature("lever", f.label), f.label, "Excited about this role.")

    second = map_fields([f], PROFILE, "lever", store=store)[0]
    assert second.action == "fill"
    assert second.value == "Excited about this role."
    assert second.source == "learned"
