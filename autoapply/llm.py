"""Pluggable LLM layer.

The default provider is `rules`, which makes no network call at all: the
deterministic mapper in mapper.py already covers the standardized Greenhouse /
Lever field set. The API providers only ever see fields the rules could not
answer, which is what keeps cost proportional to novelty rather than volume.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol

from . import budget as _budget
from . import log as _log


def _retry_after(detail: str, headers=None) -> float:
    """How long the API asked us to wait, in seconds."""
    for key in ("retry-after-ms", "x-ratelimit-reset-tokens", "retry-after"):
        raw = (headers or {}).get(key) if headers else None
        if raw:
            try:
                value = float(str(raw).rstrip("ms").rstrip("s"))
                return min(max(value / 1000 if "ms" in key else value, 0.5), 30)
            except ValueError:
                pass
    if m := re.search(r"try again in ([\d.]+)(ms|s)", detail or "", re.I):
        seconds = float(m.group(1)) / (1000 if m.group(2).lower() == "ms" else 1)
        return min(max(seconds, 0.5), 30)
    return 2.0

def today_note() -> str:
    """The one fact about the world every self-identification form needs.

    Workday's Self Identify page carries a required "Date" beside the
    signature, and nothing in a candidate profile says what day it is -- so
    every tier correctly declined to answer it, the step could never validate,
    and the answers the same step *had* worked out were discarded on each retry
    because a rejected step teaches nothing. The date of signing is a fact
    about the world, not an invented fact about the candidate, so supplying it
    is not a guess. Which dates it may be used for is spelled out, because
    answering a date of birth with today would be.
    """
    from datetime import date

    today = date.today()
    return (f"Today's date is {today:%Y-%m-%d} ({today:%B %-d, %Y}). Use it "
            "only for a field asking when the form was signed or completed -- "
            "a signature date, an acknowledgement date, \"today's date\". "
            "Never for a date of birth, a graduation date, an employment date "
            "or a date available to start: those come from the profile or "
            "nowhere.")


SYSTEM = (
    "You map job-application form fields onto a candidate profile. "
    "For each field return the dotted profile path whose value answers it, or "
    "null when nothing in the profile does. Never invent facts."
)


ANSWER_SYSTEM = (
    "You answer job-application questions on a candidate's behalf, using ONLY "
    "the facts in the profile you are given.\n"
    "Rules:\n"
    "- Never invent facts. No employer, date, credential, degree, salary or "
    "authorization that is not in the profile.\n"
    "- If the profile does not support an answer, return null for that field. "
    "A null is always better than a guess.\n"
    "- Reason from the profile where the answer follows from it. If the profile "
    "says the candidate is authorized to work in the United States and needs no "
    "sponsorship, then a question about work authorization in a DIFFERENT "
    "country is answered from that fact, not assumed to be the same.\n"
    "- If the field lists options, return exactly one of them, verbatim.\n"
    "- If the profile's answer is not among the options, pick the offered "
    "option closest to the truth of it. A field can only be answered with what "
    "it offers, and leaving a required one empty stops the application. Return "
    "null only when every option would be false.\n"
    "- For yes/no questions return exactly \"Yes\" or \"No\".\n"
    "- Voluntary self-identification questions -- race, ethnicity, gender, "
    "veteran status, disability -- are answered from the profile when it "
    "states the fact. When it does not, and the field offers a decline-to-"
    "answer option (\"I do not wish to answer\", \"Decline to self-identify\", "
    "\"Prefer not to say\"), choose that option. It is the truthful answer to a "
    "question about a fact you were not given, the form offers it precisely "
    "for that case, and it is not a guess about the candidate. Returning null "
    "instead leaves a required field empty and stops the application.\n"
    "- For free-text questions write 1-3 truthful sentences grounded in the "
    "profile. No placeholders, no invented enthusiasm about specifics you were "
    "not told.\n"
    "- You are also shown the fields already answered on this same form, as "
    "context. Use them to tell neighbouring questions apart: a form asking both "
    "\"Phone Number\" and \"Phone Extension\" is asking two different things, "
    "and a second \"URL\" beside one already holding a portfolio wants a "
    "different link. Do not repeat a value already given to another field "
    "unless the question genuinely calls for the same answer.\n"
    + _budget.SAMPLED_NOTE + "\n"
    "- confidence is 0.0-1.0: how well the profile supports the answer."
)


class LLMProvider(Protocol):
    name: str

    def map_fields(self, fields: list[dict], profile: dict) -> dict[str, dict]:
        """{field_id: {"profile_path": str|None, "confidence": float}}"""

    def answer_fields(self, fields: list[dict], profile: dict) -> dict[str, dict]:
        """Answer questions no profile path covers.

        {field_id: {"value": str|None, "confidence": float}}. Returning None is
        the correct result whenever the profile does not support an answer --
        an unanswered required field blocks auto-submit, an invented one does not.
        """

    def generate(self, prompt: str, profile: dict) -> str:
        """Free text for an open question (cover letter, 'why this company')."""


def _prompt(fields: list[dict], profile: dict,
            answered: list[dict] | None = None) -> str:
    """The profile, what still needs answering, and what is already on the form.

    The last part matters more than it looks. Rules match a label in isolation,
    which is how "Phone Extension" got the phone number and how two fields both
    labelled "URL" both got the portfolio. Showing the model what its
    neighbours already hold is what lets it tell them apart.
    """
    parts = [
        today_note(),
        "",
        "Candidate profile (dotted paths are what you may reference):",
        json.dumps(profile, indent=2),
    ]
    if answered:
        parts += ["", "Already answered on this same form (context, do not "
                      "re-answer these):", json.dumps(answered, indent=2)]
    parts += ["", "Form fields needing a mapping:", json.dumps(fields, indent=2)]
    return "\n".join(parts)


class RulesProvider:
    """No-op provider. Unmatched fields stay unknown, which blocks auto-submit."""

    name = "rules"

    def map_fields(self, fields, profile):
        return {}

    def answer_fields(self, fields, profile):
        return {}

    def generate(self, prompt, profile):
        return ""


class AnthropicProvider:
    name = "anthropic"

    def __init__(self):
        import anthropic  # imported lazily so the dep stays optional
        self.client = anthropic.Anthropic()
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")

    def map_fields(self, fields, profile):
        from pydantic import BaseModel

        class FieldMapping(BaseModel):
            field_id: str
            profile_path: str | None
            confidence: float

        class Result(BaseModel):
            mappings: list[FieldMapping]

        resp = self.client.messages.parse(
            model=self.model,
            max_tokens=4096,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": _prompt(fields, profile)}],
            output_format=Result,
        )
        return {
            m.field_id: {"profile_path": m.profile_path, "confidence": m.confidence}
            for m in resp.parsed_output.mappings
        }

    def answer_fields(self, fields, profile):
        from pydantic import BaseModel

        class Answer(BaseModel):
            field_id: str
            value: str | None
            confidence: float

        class Answers(BaseModel):
            answers: list[Answer]

        resp = self.client.messages.parse(
            model=self.model,
            max_tokens=4096,
            system=ANSWER_SYSTEM,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": _prompt(fields, profile)}],
            output_format=Answers,
        )
        return {a.field_id: {"value": a.value, "confidence": a.confidence}
                for a in resp.parsed_output.answers}

    def generate(self, prompt, profile):
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system="Write a concise, truthful answer using only the profile facts given.",
            messages=[{"role": "user", "content": prompt + "\n\n" + json.dumps(profile)}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()


class OpenAICompatProvider:
    """OpenAI and Ollama both speak this shape; only the base URL differs."""

    def __init__(self, name: str, base_url: str, model: str, api_key: str = "none"):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def _chat(self, system: str, user: str) -> str:
        import urllib.error
        import urllib.request

        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0,
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        return self._send(req)

    # A rate limit is not a failure, it is a queue. The audit and repair tiers
    # were being silently switched off by 429s for whole runs -- "audit
    # corrected 0" meant the audit never ran, not that the form was clean -- and
    # the answer they would have given was sitting one retry away. The window
    # resets in well under a second.
    RETRY_STATUSES = {429, 500, 502, 503, 504}
    MAX_ATTEMPTS = int(os.getenv("LLM_MAX_ATTEMPTS", "5"))

    def _send(self, req) -> str:
        import time
        import urllib.error
        import urllib.request

        last = ""
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read())["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    body = json.loads(exc.read().decode("utf-8", "replace"))
                    detail = (body.get("error") or {}).get("message", "")
                except Exception:
                    pass
                last = f"{exc.code} {exc.reason}: {detail[:200]}"
                if exc.code not in self.RETRY_STATUSES:
                    raise RuntimeError(last) from exc
                # OpenAI says how long to wait; believe it, with a floor so a
                # "try again in 261ms" does not turn into a spin.
                wait = _retry_after(detail, exc.headers)
                _log.get("llm").info(
                    "%s -- retrying in %.1fs (attempt %d/%d)",
                    last[:80], wait, attempt + 1, self.MAX_ATTEMPTS)
                time.sleep(wait)
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                if attempt + 1 >= self.MAX_ATTEMPTS:
                    break
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"gave up after {self.MAX_ATTEMPTS} attempts: {last}")

    def map_fields(self, fields, profile):
        raw = self._chat(
            SYSTEM + ' Reply with JSON only: {"mappings":[{"field_id":..,'
                     '"profile_path":..,"confidence":0.0-1.0}]}',
            _prompt(fields, profile),
        )
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < 0:
            return {}
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return {}
        return {
            m["field_id"]: {"profile_path": m.get("profile_path"),
                            "confidence": float(m.get("confidence", 0))}
            for m in data.get("mappings", []) if m.get("field_id")
        }

    def answer_fields(self, fields, profile, answered=None):
        raw = self._chat(
            ANSWER_SYSTEM + ' Reply with JSON only: {"answers":[{"field_id":..,'
                            '"value":..,"confidence":0.0-1.0}]}',
            _prompt(fields, profile, answered),
        )
        return _parse_answers(raw)

    def generate(self, prompt, profile):
        return self._chat(
            "Write a concise, truthful answer using only the profile facts given.",
            prompt + "\n\n" + json.dumps(profile),
        ).strip()


def _parse_answers(raw: str) -> dict[str, dict]:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out: dict[str, dict] = {}
    for a in data.get("answers", []):
        fid = a.get("field_id")
        if not fid:
            continue
        value = a.get("value")
        out[fid] = {"value": None if value in (None, "", "null") else str(value),
                    "confidence": float(a.get("confidence", 0) or 0)}
    return out


def get_provider(name: str | None = None) -> LLMProvider:
    name = (name or os.getenv("LLM_PROVIDER", "rules")).lower()
    if name == "anthropic":
        return AnthropicProvider()
    if name == "openai":
        return OpenAICompatProvider(
            "openai",
            os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            os.getenv("OPENAI_API_KEY", ""),
        )
    if name == "ollama":
        return OpenAICompatProvider(
            "ollama",
            os.getenv("OLLAMA_HOST", "http://localhost:11434") + "/v1",
            os.getenv("OLLAMA_MODEL", "llama3.1"),
        )
    return RulesProvider()
