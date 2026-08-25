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


def test_unknown_ats_queues_rather_than_guessing(tmp_path):
    """No dedicated worker and no LLM configured: queue with a clear reason.

    The agent lane handles unknown hosts, but it needs a model. Without one it
    must not fall back to guessing at selectors.
    """
    os.environ["LLM_PROVIDER"] = "rules"
    r = apply_to("https://acme.example.com/careers/apply/123",
                 mode="auto", store=_store(tmp_path), profile=PROFILE)
    assert r.job.ats == "unknown"
    assert r.status == "queued"
    assert any("agent unavailable" in x for x in r.gate.reasons)


def test_oracle_routes_to_agent_lane(tmp_path):
    """JPMorgan-style Oracle Cloud HCM links reach the agent lane, not a skip."""
    from autoapply import router

    job = router.parse_job(
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/210123")
    assert job.ats == "oracle"
    assert job.ats in router.AGENT_ATS


def test_icims_and_ashby_route_to_agent_lane():
    from autoapply import router

    assert router.parse_job("https://careers-gdms.icims.com/jobs/74471/job").ats == "icims"
    assert router.parse_job("https://jobs.ashbyhq.com/Crusoe/abc/application").ats == "ashby"
    assert {"icims", "ashby"} <= router.AGENT_ATS


def test_empty_discovery_never_submits(tmp_path):
    """A page whose fields we cannot see must not be submitted.

    This page has a working submit button and no fillable field anywhere.
    Nothing was filled, so nothing may be sent — even though verification passes
    vacuously and there is a live submit button one click away.
    """
    os.environ["SCREENSHOT_DIR"] = str(tmp_path / "shots")
    os.environ["LLM_PROVIDER"] = "rules"          # no agent available to fall back to
    store = _store(tmp_path)
    r = apply_to(_url("nofields.html"), mode="auto", store=store,
                 profile=PROFILE, ats_override="greenhouse")

    assert r.status != "applied", "submitted a form where nothing was filled"
    assert store.stats()["applied"] == 0
    reasons = " ".join(r.gate.reasons)
    assert "no form fields discovered" in reasons or "agent unavailable" in reasons


def test_empty_discovery_reaches_for_the_agent(tmp_path):
    """Empty discovery is exactly what the agent lane is for, so it must try."""
    os.environ["SCREENSHOT_DIR"] = str(tmp_path / "shots")
    os.environ["LLM_PROVIDER"] = "rules"
    r = apply_to(_url("nofields.html"), mode="auto", store=_store(tmp_path),
                 profile=PROFILE, ats_override="greenhouse")
    # With no model configured the fallback can't run, but it must have been
    # attempted — that is what surfaces as "agent unavailable".
    assert "agent unavailable" in " ".join(r.gate.reasons)


def test_iframed_form_is_discovered_and_filled(tmp_path):
    """A form inside an iframe is the normal case, not an unreadable one.

    iCIMS serves its form in a frame, and a company careers page routinely
    embeds someone else's board. Discovery that only reads the top document
    reports these as empty markup and hands them to the agent lane, which is
    slower, costs a model call, and is not needed.
    """
    os.environ["SCREENSHOT_DIR"] = str(tmp_path / "shots")
    os.environ["LLM_PROVIDER"] = "rules"
    r = apply_to(_url("iframed.html"), mode="auto", store=_store(tmp_path),
                 profile=PROFILE, ats_override="greenhouse")

    assert r.outcome is not None and r.outcome.fields, "did not look inside the frame"
    assert r.outcome.filled_ids, "found the frame's fields but filled none"
    labels = {(m.label or "").lower() for m in r.outcome.mappings if m.action == "fill"}
    assert any("first name" in x for x in labels)


def test_submit_prefers_the_forms_own_frame(tmp_path):
    """The button on the outer page is not this form's submit button.

    iframed.html carries a decoy: an outer submit that swaps in a convincing
    "Thank you for applying" while the real form in the frame was never sent.
    Recording that as applied is the worst outcome the gate has -- a job marked
    done that was never actually submitted.
    """
    from autoapply.browser import browser_page
    from autoapply.workers.greenhouse import GreenhouseWorker

    with browser_page() as page:
        page.goto(_url("iframed.html"))
        page.wait_for_timeout(600)
        worker = GreenhouseWorker(page)
        inner = [f.url for f in page.frames if f.url.endswith("greenhouse.html")]
        assert inner, "fixture no longer has the inner form frame"

        worker.form_frame_url = inner[0]
        worker.submit()
        # The decoy rewrites the OUTER document; if it still has its heading the
        # click went to the frame, which is where the form is.
        outer = page.main_frame.inner_text("body").lower()
        assert "apply for software engineer" in outer, "clicked the outer decoy"
