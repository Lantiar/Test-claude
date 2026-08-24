"""Field -> value mapping: rules, then cache, then LLM.

Cache keys are (ats, normalized label) and store the *profile path*, never the
resolved value: keying on host would never hit (every company gets its own
Greenhouse/Lever subdomain) and caching values would refill stale data after a
profile edit.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .models import Field, Mapping

# (label pattern, dotted profile path). First match wins, so order is specific -> general.
RULES: list[tuple[str, str]] = [
    (r"first\s*name|given\s*name",                       "identity.first_name"),
    (r"last\s*name|surname|family\s*name",               "identity.last_name"),
    (r"preferred\s*name|nickname|preferred first",       "identity.preferred_name"),
    (r"pronoun",                                         "identity.pronouns"),
    (r"full\s*name|^name$|^your name$|^legal name$",     "identity.full_name"),
    (r"e-?mail",                                         "identity.email"),
    (r"phone|mobile|telephone|\bcell\b",                     "identity.phone"),

    (r"cover\s*letter",                                  "files.cover_letter"),
    (r"resum|curriculum vitae|\bcv\b",                   "files.resume"),
    (r"transcript",                                      "files.transcript"),

    (r"linked\s*in",                                     "links.linkedin"),
    (r"git\s*hub",                                       "links.github"),
    (r"portfolio|personal\s*(web)?site|website|\burl\b", "links.portfolio"),

    (r"school|university|college|institution",           "education.school"),
    (r"degree",                                          "education.degree"),
    (r"discipline|\bmajor\b|field of study",                 "education.discipline"),
    (r"\bgpa\b",                                         "education.gpa"),
    (r"graduat",                                         "education.graduation_year"),

    (r"current (company|employer)",                      "experience.current_company"),
    (r"current (job )?title|current role",               "experience.current_title"),
    (r"years of .*experience|experience.*years",         "experience.years_of_experience"),
    (r"start date|available to start|earliest start|when can you start",
                                                         "experience.earliest_start_date"),
    (r"notice period",                                   "experience.notice_period"),
    (r"sponsor|require .*visa|visa sponsorship",         "work_authorization.requires_sponsorship"),
    (r"authoriz(ed|ation) to work|legally authorized|eligible to work|work authorization",
                                                         "work_authorization.authorized_to_work"),
    (r"visa|immigration status",                         "work_authorization.visa_status"),
    (r"clearance",                                       "work_authorization.security_clearance"),

    (r"relocat",                                         "location.willing_to_relocate"),

    (r"zip|postal",                                      "location.postal_code"),
    # Bare "city" also matches "ethnicity"; bare "cell" matches "excellent".
    (r"\bcity\b",                                            "location.city"),
    # \bstate\b alone also matches "United States", which is why the work
    # authorization rules above must be tried first.
    (r"(?<!united )\bstate\b|province",                 "location.state"),
    (r"country",                                         "location.country"),
    (r"address|street",                                  "location.address_line1"),
    (r"location|where are you based",                    "location.city"),

    (r"salary|compensation|pay expectation|desired pay", "compensation.expected_salary"),

    (r"hispanic|\blatin",                                  "demographics.hispanic_latino"),
    (r"gender|\bsex\b",                                  "demographics.gender"),
    (r"\brace\b|ethnic",                                     "demographics.race_ethnicity"),
    (r"veteran|military",                                "demographics.veteran_status"),
    (r"disabilit",                                       "demographics.disability_status"),

    (r"how did you hear|hear about (us|this)|referral source",  "misc.how_did_you_hear"),
    (r"refer(red|ral)",                                  "misc.referral_name"),
    (r"previously (worked|employed|applied)",            "misc.previously_employed_here"),
    (r"18 years|over 18|age requirement",                "misc.over_18"),
]

RULE_CONFIDENCE = 0.95
DECLINE_HINTS = ("decline", "prefer not", "do not wish", "don't wish", "not wish to answer")


def normalize_label(label: str) -> str:
    s = label.lower().strip()
    s = re.sub(r"\(required\)|\(optional\)|required|optional", " ", s)
    s = re.sub(r"[\*∗]", " ", s)
    s = re.sub(r"[^a-z0-9\s\-']", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def signature(ats: str, label: str) -> str:
    return f"{ats}|{normalize_label(label)}"


def get_value(profile: dict, path: str) -> str:
    """Resolve a dotted path, including the computed `identity.full_name`."""
    if path == "identity.full_name":
        ident = profile.get("identity", {})
        return f"{ident.get('first_name','')} {ident.get('last_name','')}".strip()
    node: Any = profile
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return ""
        node = node[part]
    return "" if node is None else str(node)


def match_rule(label: str) -> Optional[str]:
    norm = normalize_label(label)
    if not norm:
        return None
    for pattern, path in RULES:
        if re.search(pattern, norm):
            return path
    return None


def resolve_option(value: str, options: list[str]) -> Optional[str]:
    """Pick the option a free-text profile value corresponds to."""
    if not options:
        return value or None
    want = value.strip().lower()
    if not want:
        return None
    for opt in options:                                    # exact
        if opt.strip().lower() == want:
            return opt
    for opt in options:                                    # whole-word either way
        low = opt.strip().lower()
        if not low:
            continue
        # Word-bounded, not bare substring: plain containment lets the option
        # "No" match a value like "Nonsense".
        if (re.search(rf"\b{re.escape(low)}\b", want)
                or re.search(rf"\b{re.escape(want)}\b", low)):
            return opt
    if want in ("yes", "no"):                              # yes/no phrased long-form
        for opt in options:
            low = opt.strip().lower()
            if low.startswith(want) or re.match(rf"^{want}\b", low):
                return opt
    if any(h in want for h in DECLINE_HINTS):              # any decline-shaped option
        for opt in options:
            if any(h in opt.lower() for h in DECLINE_HINTS):
                return opt
    return None


def map_fields(fields: list[Field], profile: dict, ats: str,
               store=None, provider=None) -> list[Mapping]:
    """Rules -> cache -> LLM. Anything still unanswered is `unknown`."""
    mappings: list[Mapping] = []
    unresolved: list[Field] = []

    for f in fields:
        path = match_rule(f.label) or match_rule(f.id)
        source = "rules"

        if path is None and store is not None:
            row = store.cache_get(signature(ats, f.label))
            if row and row["profile_path"]:
                path, source = row["profile_path"], "cache"

        if path is None:
            if store is not None:
                learned = store.literal_for(signature(ats, f.label))
                if learned:
                    mappings.append(Mapping(field_id=f.id, action="fill", value=learned,
                                            confidence=1.0, source="learned",
                                            label=f.label))
                    continue
            unresolved.append(f)
            continue

        mappings.append(_build(f, path, profile, source, RULE_CONFIDENCE))

    # Only genuinely novel fields reach the model.
    if unresolved and provider is not None and provider.name != "rules":
        payload = [{"field_id": f.id, "label": f.label, "kind": f.kind,
                    "options": f.options, "required": f.required} for f in unresolved]
        try:
            suggested = provider.map_fields(payload, profile)
        except Exception:
            suggested = {}
        still: list[Field] = []
        for f in unresolved:
            hint = suggested.get(f.id) or {}
            path = hint.get("profile_path")
            if not path or not get_value(profile, path):
                still.append(f)
                continue
            m = _build(f, path, profile, "llm", float(hint.get("confidence", 0.5)))
            mappings.append(m)
            if store is not None and m.action == "fill":
                # Unconfirmed until a human sees it once — same bar as any other
                # non-deterministic source.
                store.cache_put(signature(ats, f.label), ats, f.label, "fill",
                                path, m.confidence, unconfirmed=True)
        unresolved = still

    for f in unresolved:
        mappings.append(Mapping(field_id=f.id, action="unknown", label=f.label))

    return mappings


def _build(f: Field, path: str, profile: dict, source: str, confidence: float) -> Mapping:
    value = get_value(profile, path)
    if not value:
        # Optional and unanswerable is fine; required and unanswerable blocks auto.
        return Mapping(field_id=f.id, action="unknown" if f.required else "skip",
                       source=source, label=f.label)
    if f.kind in ("select", "radio"):
        chosen = resolve_option(value, f.options)
        if chosen is None:
            return Mapping(field_id=f.id, action="unknown", source=source, label=f.label)
        value = chosen
    return Mapping(field_id=f.id, action="fill", value=value,
                   confidence=confidence, source=source, label=f.label)
