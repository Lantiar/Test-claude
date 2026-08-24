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

ATS = Literal["greenhouse", "lever", "workday", "ashby", "icims", "unknown"]


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
