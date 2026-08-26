"""browser-use agent worker: the path for ATSs with no dedicated worker.

Used three ways:
  * primary driver for iCIMS, Ashby, Oracle/Taleo and unknown hosts
  * fallback when a dedicated worker fills but fails verification
  * the LLM judge in judge.py shares this module's model wiring

The agent drives its own browser (browser-use speaks CDP directly), so it does
not share the Playwright page the DOM workers use. It fills and stops: whether
anything is submitted is still the gate's decision, and submission is a separate
second task.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile
from typing import Optional

from ..models import Field, FillOutcome, Job, Mapping

PLAYBOOKS = pathlib.Path(__file__).resolve().parent.parent / "playbooks"


class AgentUnavailable(RuntimeError):
    """No usable LLM configured. The caller queues rather than guessing."""


def playbook_for(ats: str) -> str:
    path = PLAYBOOKS / f"{ats}.md"
    if not path.exists():
        path = PLAYBOOKS / "generic.md"
    return path.read_text()


def build_llm():
    """Map LLM_PROVIDER onto a browser-use chat model."""
    provider = os.getenv("LLM_PROVIDER", "rules").lower()
    if provider == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise AgentUnavailable("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset")
        from browser_use.llm import ChatAnthropic
        return ChatAnthropic(model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5"))
    if provider == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            raise AgentUnavailable("LLM_PROVIDER=openai but OPENAI_API_KEY is unset")
        from browser_use.llm import ChatOpenAI
        return ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    if provider == "ollama":
        from browser_use.llm import ChatOllama
        return ChatOllama(model=os.getenv("OLLAMA_MODEL", "llama3.1"),
                          host=os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    raise AgentUnavailable(
        "LLM_PROVIDER=rules has no agent. Unknown ATSs need a model: set "
        "LLM_PROVIDER to anthropic, openai or ollama."
    )


def build_browser():
    from browser_use import Browser

    from ..browser import find_chromium

    kwargs: dict = {"headless": os.getenv("HEADLESS", "1") != "0"}
    if exe := find_chromium():
        kwargs["executable_path"] = exe
        # An explicit binary and a browser channel are mutually exclusive;
        # leaving a channel set makes the launch look for a browser that is
        # not installed and time out.
        kwargs["channel"] = None
    if state := os.getenv("STORAGE_STATE"):
        kwargs["storage_state"] = state
    # The agent drives its own browser, so the egress settings the DOM lane
    # gets from browser.py have to be handed to it explicitly -- without them
    # it launches a browser that cannot reach anything and the run dies at
    # startup rather than on the page.
    if proxy := os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"):
        kwargs["proxy"] = {"server": proxy}
    # A dedicated profile directory per run. Sharing the default one makes the
    # launch hang until CDP times out in a container -- browser-use issues #2941,
    # #3613 -- and the failure looks like "BrowserStartEvent timed out after
    # 30.0s" with no mention of the profile.
    kwargs["user_data_dir"] = tempfile.mkdtemp(prefix="autoapply-bu-")
    args = ["--no-sandbox", "--disable-dev-shm-usage"]
    if tls_max := os.getenv("AUTOAPPLY_TLS_MAX"):
        args.append(f"--ssl-version-max={tls_max}")
    kwargs["args"] = args
    return Browser(**kwargs)


def _report_model():
    from pydantic import BaseModel

    class FilledField(BaseModel):
        label: str
        value: str

    class AgentReport(BaseModel):
        filled: list[FilledField]
        unanswered_required: list[str]
        saw_captcha: bool
        needs_auth: bool
        reached_review: bool
        notes: str

    return AgentReport


FILL_TASK = """Fill in this job application, then STOP without submitting it.

Application URL: {url}

Candidate profile — the only facts you may use:
{profile}

Site notes:
{playbook}

Rules:
- Fill every field the profile answers, including work authorization, demographics
  and salary. Those have explicit answers in the profile; use them.
- If a required field has NO answer in the profile, leave it blank and list its
  label in unanswered_required. Never invent an answer.
- Do NOT submit the application. Stop when the form is filled or you reach a
  review/summary step.
- Stop immediately and set the matching flag if you hit a CAPTCHA (saw_captcha) or
  a sign-in / create-account screen (needs_auth). Do not try to solve or bypass either.
- Report every field you filled, with the value you entered.
"""

SUBMIT_TASK = """The application at {url} is already filled in and has been approved
for submission. Click the final submit button, wait for the result, and report whether
a confirmation actually appeared (confirmation text, a thank-you page, or a
confirmation redirect). Do not re-fill any fields. If no confirmation appears, say so
plainly — do not assume success."""


class AgentWorker:
    """Same run() contract as the DOM workers, different machinery."""

    ats = "agent"
    verifies_internally = True

    def __init__(self, page=None):
        self.page = page          # unused; kept so get_worker() stays uniform
        self.last_report = None

    def run(self, job: Job, profile: dict, store, provider,
            screenshot_dir: str) -> FillOutcome:
        report = asyncio.run(self._fill(job, profile))
        self.last_report = report
        return self._to_outcome(job, report)

    async def _fill(self, job: Job, profile: dict):
        from browser_use import Agent

        llm = build_llm()
        browser = build_browser()
        task = FILL_TASK.format(url=job.url, profile=json.dumps(profile, indent=2),
                                playbook=playbook_for(job.ats))
        agent = Agent(task=task, llm=llm, browser=browser,
                      output_model_schema=_report_model())
        history = await agent.run(max_steps=int(os.getenv("AGENT_MAX_STEPS", "40")))
        return history.structured_output

    def submit(self) -> tuple[bool, str]:
        """Second agent pass, run only after the gate says submit."""
        if self.last_job is None:
            return False, "no job in context"
        confirmed, detail = asyncio.run(self._submit(self.last_job))
        return confirmed, detail

    async def _submit(self, job: Job) -> tuple[bool, str]:
        from browser_use import Agent
        from pydantic import BaseModel

        class SubmitReport(BaseModel):
            confirmed: bool
            evidence: str

        agent = Agent(task=SUBMIT_TASK.format(url=job.url), llm=build_llm(),
                      browser=build_browser(), output_model_schema=SubmitReport)
        history = await agent.run(max_steps=int(os.getenv("AGENT_SUBMIT_STEPS", "12")))
        out = history.structured_output
        if out is None:
            return False, "agent returned no structured result"
        return bool(out.confirmed), out.evidence

    last_job: Optional[Job] = None

    def _to_outcome(self, job: Job, report) -> FillOutcome:
        self.last_job = job
        outcome = FillOutcome(job=job)
        if report is None:
            outcome.errors.append("agent returned no structured result")
            outcome.verified = False
            return outcome

        for idx, item in enumerate(report.filled):
            fid = f"agent-{idx}"
            outcome.fields.append(Field(id=fid, selector="", label=item.label))
            outcome.mappings.append(Mapping(field_id=fid, action="fill", value=item.value,
                                            confidence=0.7, source="agent", label=item.label))
            outcome.filled_ids.append(fid)

        for idx, label in enumerate(report.unanswered_required):
            fid = f"agent-missing-{idx}"
            outcome.fields.append(Field(id=fid, selector="", label=label, required=True))
            outcome.mappings.append(Mapping(field_id=fid, action="unknown", label=label))

        outcome.saw_captcha = report.saw_captcha
        outcome.needs_auth = report.needs_auth
        outcome.filled_ok = not report.unanswered_required
        # The agent grading its own work is not verification. judge.py runs an
        # independent pass; until then this stays False so the gate queues.
        outcome.verified = False
        outcome.verify_detail = {"agent_notes": report.notes,
                                 "reached_review": report.reached_review}
        return outcome
