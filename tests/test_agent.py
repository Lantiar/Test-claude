"""Agent lane: playbooks, and safe degradation when no model is configured."""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autoapply.judge import judge                                  # noqa: E402
from autoapply.models import FillOutcome, Job, Mapping             # noqa: E402
from autoapply.workers.agent import (AgentUnavailable, build_llm,  # noqa: E402
                                     playbook_for)


def test_every_agent_ats_has_a_playbook():
    from autoapply import router

    for ats in router.AGENT_ATS:
        text = playbook_for(ats)
        assert text.strip(), f"{ats} playbook is empty"


def test_unknown_ats_falls_back_to_generic_playbook():
    assert playbook_for("nosuchats").startswith("# Unknown ATS")


def test_playbooks_tell_the_agent_not_to_invent_answers():
    generic = playbook_for("generic").lower()
    assert "invent" in generic


def test_agent_refuses_to_run_without_a_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "rules")
    with pytest.raises(AgentUnavailable):
        build_llm()


def test_agent_provider_requires_its_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AgentUnavailable):
        build_llm()


def test_judge_leaves_outcome_unverified_when_unavailable(monkeypatch):
    """No model means no verification — which must mean queued, never submitted."""
    monkeypatch.setenv("LLM_PROVIDER", "rules")
    outcome = FillOutcome(job=Job(url="https://jobs.ashbyhq.com/x/y", ats="ashby"))
    outcome.mappings = [Mapping(field_id="a", action="fill", value="Jane",
                                label="First Name")]
    result = judge(outcome)
    assert result.verified is False
    assert "unavailable" in str(result.verify_detail["judge"])


def test_judge_rejects_an_already_submitted_page():
    """Guard against double-submitting: verified must stay False."""
    from dataclasses import dataclass, field as dc_field

    from autoapply.judge import apply_verdict

    @dataclass
    class Verdict:
        matches: bool = True
        mismatched: list = dc_field(default_factory=list)
        empty_required: list = dc_field(default_factory=list)
        submitted_already: bool = True
        evidence: str = "confirmation page is showing"

    outcome = FillOutcome(job=Job(url="https://jobs.ashbyhq.com/x/y", ats="ashby"))
    result = apply_verdict(outcome, Verdict())
    assert result.verified is False
    assert any("already submitted" in e for e in result.errors)


def test_judge_verifies_a_clean_verdict():
    from dataclasses import dataclass, field as dc_field

    from autoapply.judge import apply_verdict

    @dataclass
    class Verdict:
        matches: bool = True
        mismatched: list = dc_field(default_factory=list)
        empty_required: list = dc_field(default_factory=list)
        submitted_already: bool = False
        evidence: str = "all values present"

    outcome = FillOutcome(job=Job(url="https://jobs.ashbyhq.com/x/y", ats="ashby"))
    assert apply_verdict(outcome, Verdict()).verified is True


def test_judge_fails_on_empty_required_even_if_matches():
    from dataclasses import dataclass, field as dc_field

    from autoapply.judge import apply_verdict

    @dataclass
    class Verdict:
        matches: bool = True
        mismatched: list = dc_field(default_factory=list)
        empty_required: list = dc_field(default_factory=lambda: ["Cover Letter"])
        submitted_already: bool = False
        evidence: str = "one required field is blank"

    outcome = FillOutcome(job=Job(url="https://jobs.ashbyhq.com/x/y", ats="ashby"))
    assert apply_verdict(outcome, Verdict()).verified is False
