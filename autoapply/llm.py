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


class LLMProvider(Protocol):
    name: str

    def map_fields(self, fields: list[dict], profile: dict) -> dict[str, dict]:
        """{field_id: {"profile_path": str|None, "confidence": float}}"""

    def generate(self, prompt: str, profile: dict) -> str:
        """Free text for an open question (cover letter, 'why this company')."""


def _prompt(fields: list[dict], profile: dict) -> str:
    return (
        "Candidate profile (dotted paths are what you may reference):\n"
        + json.dumps(profile, indent=2)
        + "\n\nForm fields needing a mapping:\n"
        + json.dumps(fields, indent=2)
    )


class RulesProvider:
    """No-op provider. Unmatched fields stay unknown, which blocks auto-submit."""

    name = "rules"

    def map_fields(self, fields, profile):
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
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]

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

    def generate(self, prompt, profile):
        return self._chat(
            "Write a concise, truthful answer using only the profile facts given.",
            prompt + "\n\n" + json.dumps(profile),
        ).strip()


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
