"""Pluggable LLM layer.

The default provider is `rules`, which makes no network call at all: the
deterministic mapper in mapper.py already covers the standardized Greenhouse /
Lever field set. The API providers only ever see fields the rules could not
answer, which is what keeps cost proportional to novelty rather than volume.
"""
from __future__ import annotations

import json
import os
from typing import Any, Protocol

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
    "- For free-text questions write 1-3 truthful sentences grounded in the "
    "profile. No placeholders, no invented enthusiasm about specifics you were "
    "not told.\n"
    "- You are also shown the fields already answered on this same form, as "
    "context. Use them to tell neighbouring questions apart: a form asking both "
    "\"Phone Number\" and \"Phone Extension\" is asking two different things, "
    "and a second \"URL\" beside one already holding a portfolio wants a "
    "different link. Do not repeat a value already given to another field "
    "unless the question genuinely calls for the same answer.\n"
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
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            # "HTTPError" alone says nothing: a 429 means slow down, a 400 means
            # the request is malformed or too long, and they need opposite
            # responses. Carry the code and the body's message.
            detail = ""
            try:
                body = json.loads(exc.read().decode("utf-8", "replace"))
                detail = (body.get("error") or {}).get("message", "")
            except Exception:
                pass
            raise RuntimeError(
                f"{exc.code} {exc.reason}: {detail[:200]}") from exc

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
