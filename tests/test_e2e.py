"""End-to-end: drive the real pipeline against local ATS-shaped fixtures."""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autoapply.pipeline import apply_to          # noqa: E402
from autoapply.store import Store                # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
def _profile() -> dict:
    """Example profile, with the resume pointed at the test fixture."""
    p = json.load(open("config/profile.example.json"))
    p["files"]["resume"] = str(pathlib.Path(__file__).parent / "fixtures" / "resume.pdf")
    return p


PROFILE = _profile()


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "test.sqlite"))


def _url(name: str) -> str:
    return (FIXTURES / name).as_uri()


def test_greenhouse_auto_submits_and_confirms(tmp_path):
    os.environ["SCREENSHOT_DIR"] = str(tmp_path / "shots")
    r = apply_to(_url("greenhouse.html"), mode="auto", store=_store(tmp_path),
                 profile=PROFILE, ats_override="greenhouse")
    assert r.status == "applied", f"{r.status}: {r.gate.reasons} {r.detail}"
    assert r.outcome.verified
    assert not r.outcome.missing_required


def test_lever_auto_submits_and_confirms(tmp_path):
    os.environ["SCREENSHOT_DIR"] = str(tmp_path / "shots")
    r = apply_to(_url("lever.html"), mode="auto", store=_store(tmp_path),
                 profile=PROFILE, ats_override="lever")
    assert r.status == "applied", f"{r.status}: {r.gate.reasons} {r.detail}"


def test_approve_mode_never_submits(tmp_path):
    os.environ["SCREENSHOT_DIR"] = str(tmp_path / "shots")
    store = _store(tmp_path)
    r = apply_to(_url("greenhouse.html"), mode="approve", store=store,
                 profile=PROFILE, ats_override="greenhouse")
    assert r.status == "queued"
    assert r.gate.reasons == ["approve mode"]
    assert len(store.queue_list()) == 1
    assert store.stats()["applied"] == 0


def test_missing_required_answer_blocks_auto(tmp_path):
    """Strip the salary answer: a required field nothing can answer must queue."""
    os.environ["SCREENSHOT_DIR"] = str(tmp_path / "shots")
    thin = json.loads(json.dumps(PROFILE))
    thin["compensation"]["expected_salary"] = ""
    r = apply_to(_url("greenhouse.html"), mode="auto", store=_store(tmp_path),
                 profile=thin, ats_override="greenhouse")
    assert r.status == "queued"
    assert any("no answer for required" in x for x in r.gate.reasons)


def test_dedupe_skips_second_application(tmp_path):
    os.environ["SCREENSHOT_DIR"] = str(tmp_path / "shots")
    store = _store(tmp_path)
    first = apply_to(_url("lever.html"), mode="auto", store=store,
                     profile=PROFILE, ats_override="lever")
    assert first.status == "applied"
    second = apply_to(_url("lever.html"), mode="auto", store=store,
                      profile=PROFILE, ats_override="lever")
    assert second.status == "skipped"


def test_unsupported_ats_never_guesses(tmp_path):
    r = apply_to("https://amat.wd1.myworkdayjobs.com/External/job/X/SWE_R1",
                 mode="auto", store=_store(tmp_path), profile=PROFILE)
    assert r.status == "skipped"
    assert r.job.ats == "workday"
