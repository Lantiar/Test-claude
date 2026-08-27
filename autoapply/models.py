"""Typed values passed between pipeline stages.

Every stage takes and returns one of these so each can be tested in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

# What the mapper decided to do with one form field.
#   fill     -> we have a concrete answer (profile, cache, or LLM)
#   generate -> the answer is LLM-written free text
#   skip     -> optional field we deliberately leave blank
#   unknown  -> nothing could answer it; blocks auto-submit if the field is required
Action = Literal["fill", "generate", "skip", "unknown"]

ATS = Literal["greenhouse", "lever", "workday", "ashby", "icims", "oracle",
             "smartrecruiters", "workable", "rippling", "tiktok", "unknown"]


@dataclass
class Job:
    url: str
    ats: ATS
    company: str = ""
    title: str = ""

    @property
    def key(self) -> str:
        """Canonical identity used for dedupe. Query strings are tracking noise."""
        return self.url.split("?")[0].rstrip("/").lower()


@dataclass
class Field:
    """One input discovered on the application form."""
    id: str                      # stable handle we use as a dict key
    selector: str                # how to find it again in the DOM
    label: str = ""
    kind: str = "text"           # text|email|tel|textarea|select|file|checkbox|radio
    required: bool = False
    options: list[str] = field(default_factory=list)   # for select/radio
    # Which frame the field lives in. Embedded boards (an iCIMS login, a
    # Greenhouse form dropped into a company careers page) render inside an
    # iframe, and a selector only resolves against the frame that holds it.
    frame_url: str = ""


@dataclass
class Mapping:
    field_id: str
    action: Action
    value: str = ""
    confidence: float = 0.0
    source: str = ""             # rules|cache|llm — for debugging and the dashboard
    label: str = ""


@dataclass
class FillOutcome:
    job: Job
    fields: list[Field] = field(default_factory=list)
    mappings: list[Mapping] = field(default_factory=list)
    filled_ids: list[str] = field(default_factory=list)
    screenshot_path: str = ""
    saw_captcha: bool = False
    needs_auth: bool = False
    filled_ok: bool = False
    verified: bool = False
    # Did a multi-step flow actually get to the end? A wizard that stalls on
    # step one still fills and verifies everything on that step, so verified
    # says True and the gate sees nothing wrong -- while five later steps were
    # never even opened. Single-page workers leave this True; only the wizard
    # clears it, and only when it stopped somewhere other than the review page.
    reached_end: bool = True
    # How many wizard steps were accepted. A multi-step form cannot be scored
    # by reading it back at the end -- advancing destroys the previous step's
    # DOM, so the review page holds nothing at all -- and this is what an
    # external observer can honestly count instead.
    steps_done: int = 0
    # Does the state this run reached depend on a session? A signed-in Workday
    # wizard five steps deep exists only inside this browser's cookies. The
    # agent fallback launches a *fresh* browser on a fresh profile and navigates
    # to the job URL, so it lands on the logged-out posting and cannot see any
    # of it -- it reports the whole form missing, burns the token budget the
    # audit tier needs, and its failures get merged into the queue reasons on
    # top of the real ones. Fall back for markup we could not read, never for a
    # session we could not hand over.
    session_bound: bool = False
    # Fields we had an answer for and could not write. Distinct from an
    # unanswered field: the answer exists and the control defeated us, which is
    # a filler problem, not a knowledge one. Left silent these just show up as
    # "missing" at the end of the run with no note saying anything was even
    # attempted -- and they only ever got a second chance if the form happened
    # to mark them invalid, which it does not do for an optional one.
    unwritten: list[str] = field(default_factory=list)
    verify_detail: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def missing_required(self) -> list[str]:
        """Required fields we could not answer — the one thing that must block auto."""
        done = set(self.filled_ids)
        return [f.id for f in self.fields if f.required and f.id not in done]


@dataclass
class GateResult:
    decision: Literal["submit", "queue", "skip"]
    reasons: list[str] = field(default_factory=list)


@dataclass
class ApplyResult:
    job: Job
    outcome: Optional[FillOutcome]
    gate: GateResult
    status: str                  # applied|queued|skipped|errored
    detail: str = ""
