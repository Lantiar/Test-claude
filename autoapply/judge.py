"""Independent verification pass for agent-driven fills.

The deterministic readback in verify.py is the first tier and covers the DOM
workers. It cannot cover the agent path: the agent drives its own browser, and
an agent reporting on its own work is not verification. So this opens the page
fresh and asks a model one question — does what is on this form match what we
intended to submit — with no access to the filling agent's reasoning.
"""
from __future__ import annotations

import asyncio
import json
import os

from .models import FillOutcome
from .workers.agent import AgentUnavailable, build_browser, build_llm

JUDGE_TASK = """You are checking a job application form that another process filled in.
Do not change anything. Read the page and answer.

URL: {url}

These are the values that should be on the form now:
{expected}

Report:
- matches: true only if every value above is actually present on the form
- mismatched: labels whose on-page value differs from the expected value
- empty_required: labels of required fields that are still empty
- submitted_already: true if the page shows the application was already submitted
- evidence: one sentence on what you saw

Be strict. If you cannot see a field, treat it as mismatched rather than assuming.
"""


def _model():
    from pydantic import BaseModel

    class Verdict(BaseModel):
        matches: bool
        mismatched: list[str]
        empty_required: list[str]
        submitted_already: bool
        evidence: str

    return Verdict


def judge(outcome: FillOutcome) -> FillOutcome:
    """Set outcome.verified from an independent read of the page."""
    expected = {m.label or m.field_id: m.value
                for m in outcome.mappings if m.action in ("fill", "generate") and m.value}
    if not expected:
        outcome.verified = False
        outcome.verify_detail["judge"] = "nothing was filled"
        return outcome

    try:
        verdict = asyncio.run(_ask(outcome.job.url, expected))
    except AgentUnavailable as exc:
        outcome.verified = False
        outcome.verify_detail["judge"] = f"unavailable: {exc}"
        return outcome
    except Exception as exc:
        outcome.verified = False
        outcome.verify_detail["judge"] = f"failed: {type(exc).__name__}: {exc}"
        return outcome

    return apply_verdict(outcome, verdict)


def apply_verdict(outcome: FillOutcome, verdict) -> FillOutcome:
    """Turn a judge verdict into a verified flag. Separate from the network call
    so the decision logic is testable without a model."""
    if verdict is None:
        outcome.verified = False
        outcome.verify_detail["judge"] = "no structured verdict"
        return outcome

    outcome.verified = bool(verdict.matches) and not verdict.empty_required
    outcome.verify_detail["judge"] = {
        "matches": verdict.matches,
        "mismatched": verdict.mismatched,
        "empty_required": verdict.empty_required,
        "submitted_already": verdict.submitted_already,
        "evidence": verdict.evidence,
    }
    # A form the judge says is already submitted must not be submitted twice.
    if verdict.submitted_already:
        outcome.verified = False
        outcome.errors.append("judge reports the application was already submitted")
    return outcome


async def _ask(url: str, expected: dict[str, str]):
    from browser_use import Agent

    agent = Agent(
        task=JUDGE_TASK.format(url=url, expected=json.dumps(expected, indent=2)),
        llm=build_llm(),
        browser=build_browser(),
        output_model_schema=_model(),
    )
    history = await agent.run(max_steps=int(os.getenv("JUDGE_MAX_STEPS", "10")))
    return history.structured_output
