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
