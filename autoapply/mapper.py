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
    # Before the degree rule on purpose. "Please select date that you will
    # complete your current degree" is a date question, and matching "degree"
    # inside it filled a graduation-date picker with "Bachelor's Degree".
    (r"date .*(complete|completion|graduat|finish)"
     r"|(complete|completion|graduat|finish).*date"
     r"|expected graduation|anticipated graduation",  "education.end_date"),
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


# Questions a rule will match and always answer wrongly. Workday's My
# Information asks four phone-shaped questions -- Phone Device Type, Country
# Phone Code, Phone Number, Phone Extension -- and the phone rule claimed all
# four, filling every one with the phone number. Rather than making the phone
# pattern ever more baroque, these are held out of rule matching entirely and
# handed to the model tier, which can read what the question is asking and pick
# from the options actually offered. Whatever it settles on is then taught, so
# the deterministic pass answers them directly from the next run on.
RULE_EXEMPT = (
    r"phone\s*(device\s*)?type|device\s*type",
    r"phone\s*ext(ension)?\b|\bext(ension)?\s*(number)?$",
)

# Country Phone Code is answered from the profile's country, not held out. It
# was exempt, which sent it to the model, which answered "1" -- and a bare "1"
# resolved to American Samoa, a real +1 country the form was happy to accept.
# "United States" matches "United States of America (+1)" outright.
COUNTRY_CODE_RULE = (r"country\s*(phone\s*)?code|phone\s*country",
                     "location.country")


def match_rule(label: str) -> Optional[str]:
    norm = normalize_label(label)
    if not norm:
        return None
    if any(re.search(p, norm) for p in RULE_EXEMPT):
        return None
    if re.search(COUNTRY_CODE_RULE[0], norm):
        return COUNTRY_CODE_RULE[1]
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
    # A bare number or a two-letter token matches incidentally. "1" is a whole
    # word inside "American Samoa (+1)", so a country phone code answered as
    # "1" resolved to American Samoa -- which Workday accepted, because it is a
    # real +1 country, and which was then taught as the answer for every later
    # run. Short tokens have to match exactly or not at all; the state
    # abbreviations below are handled by name.
    if want.isdigit() or len(want) <= 2:
        if len(want) == 2 and (full := US_STATES.get(want.upper())):
            for opt in options:
                if opt.strip().lower() == full.lower():
                    return opt
        return None

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
    # A profile carries "NJ" and the dropdown lists "New Jersey". Neither
    # containment test can bridge that, so the state came back "Select One" and
    # the step would not validate.
    if len(want) == 2 and (full := US_STATES.get(want.upper())):
        for opt in options:
            if opt.strip().lower() == full.lower():
                return opt
    return None


US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "PR": "Puerto Rico", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


# Below this the model is telling us the profile does not back the answer.
MIN_ANSWER_CONFIDENCE = 0.25

_FREE_TEXT_PROMPT = (
    "Answer this job-application question in 2-4 sentences, in the first "
    "person, using only facts from the profile.\n\n"
    "Question: {question}\n\n"
    "Do not invent employers, projects, dates or credentials. Do not claim "
    "specific knowledge of the company's products that the profile does not "
    "support -- write about what the candidate has actually done and what they "
    "want to work on. No salutation, no sign-off, no placeholders."
)


def _is_free_text(f: Field) -> bool:
    """A prompt wanting prose, not a value to look up."""
    if f.kind == "textarea":
        return True
    label = (f.label or "").lower()
    return (len(label) > 60 and label.rstrip("* ").endswith("?")) or any(
        p in label for p in ("why are you", "why do you", "tell us", "describe",
                             "cover letter", "in your own words"))


def map_fields(fields: list[Field], profile: dict, ats: str,
               store=None, provider=None) -> list[Mapping]:
    """Rules -> cache -> LLM. Anything still unanswered is `unknown`."""
    mappings: list[Mapping] = []
    unresolved: list[Field] = []

    # Repeatable sections put several identically-labelled fields on one step:
    # Workday's My Experience has a Websites list where every entry is "URL*".
    # Filling them all from the same rule gave both entries the portfolio, and
    # the form answered "You can't add duplicate website URLs". Only the first
    # of a repeated label is answered from rules; the rest go to the model,
    # which is shown what its neighbours already hold and can pick a different
    # link the profile actually has.
    seen_labels: dict[str, int] = {}
    repeated: set[str] = set()
    for f in fields:
        key = normalize_label(f.label)
        if not key:
            continue
        seen_labels[key] = seen_labels.get(key, 0) + 1
        if seen_labels[key] > 1:
            repeated.add(f.id)

    for f in fields:
        if f.id in repeated:
            unresolved.append(f)
            continue
        # A taught answer outranks everything, including a rule that matches.
        # It has to: the fields most in need of teaching are the ones a rule
        # gets confidently wrong. "Phone Extension" matches the phone rule and
        # gets filled with the whole phone number, and while the rule won that
        # race no correction -- typed by a human in the dashboard or learned
        # from the form rejecting the step -- could ever take effect. Consulted
        # last, the teaching store was inert for exactly the cases it exists for.
        if store is not None:
            learned = store.literal_for(signature(ats, f.label))
            if learned:
                mappings.append(Mapping(field_id=f.id, action="fill", value=learned,
                                        confidence=1.0, source="learned",
                                        label=f.label))
                continue

        path = match_rule(f.label) or match_rule(f.id)
        source = "rules"

        if path is None and store is not None:
            row = store.cache_get(signature(ats, f.label))
            if row and row["profile_path"]:
                path, source = row["profile_path"], "cache"

        if path is None:
            unresolved.append(f)
            continue

        built = _build(f, path, profile, source, RULE_CONFIDENCE)
        if built.action == "unknown" and f.options:
            # A rule matched the label but its value is not one of the choices
            # actually on the form: "are you currently enrolled?" pattern-matches
            # the education rules, but the form wants Yes/No, not a school name.
            # The model can pick from the real list, so let it try before this
            # gives up.
            unresolved.append(f)
            continue
        mappings.append(built)

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
            if m.action == "unknown" and f.options:
                # Same trap as the rules branch: a plausible profile path whose
                # value is not one of the form's choices. Hand it to the
                # answering pass, which sees the options themselves.
                still.append(f)
                continue
            mappings.append(m)
            if store is not None and m.action == "fill":
                # Unconfirmed until a human sees it once — same bar as any other
                # non-deterministic source.
                store.cache_put(signature(ats, f.label), ats, f.label, "fill",
                                path, m.confidence, unconfirmed=True)
        unresolved = still

    # Second model pass: questions no profile *path* answers, but the profile's
    # facts still do -- "do you have permission to work in the UK?" follows from
    # a US-only authorization, and "why this role?" from the education and
    # experience already on file. Path-mapping structurally cannot express
    # either, so without this every such field stays unknown and every
    # application with one is blocked.
    if unresolved and provider is not None and provider.name != "rules":
        payload = [{"field_id": f.id, "label": f.label, "kind": f.kind,
                    "options": f.options, "required": f.required}
                   for f in unresolved]
        # What the deterministic pass already put on this form. Without it the
        # model sees each unanswered field alone and cannot tell "Phone
        # Extension" from the "Phone Number" sitting above it.
        answered = [{"label": m.label or m.field_id, "value": m.value}
                    for m in mappings
                    if m.action in ("fill", "generate") and m.value]
        try:
            answers = provider.answer_fields(payload, profile, answered)
        except TypeError:
            answers = provider.answer_fields(payload, profile)
        except Exception:
            answers = {}
        still = []
        for f in unresolved:
            hint = answers.get(f.id) or {}
            value, confidence = hint.get("value"), float(hint.get("confidence", 0.5))
            # The model reports how well the profile backs each answer. Filling
            # one it scored at ~zero is exactly the invented answer the whole
            # design is trying to avoid, so treat it as unanswered and let the
            # gate block instead.
            if value and confidence < MIN_ANSWER_CONFIDENCE:
                value = None
            if not value:
                still.append(f)
                continue
            if f.kind in ("select", "radio", "checkbox", "combobox") and f.options:
                # The model must land on a real option, not paraphrase one.
                value = resolve_option(value, f.options) or ""
                if not value:
                    still.append(f)
                    continue
            mappings.append(Mapping(field_id=f.id, action="fill", value=value,
                                    confidence=confidence,
                                    source="llm-answer", label=f.label))
        unresolved = still

        # Free-text prompts ("why this company?", "describe a project") get
        # their own call each. Asked as one of eight in a batch the model
        # answers the easy ones and returns null for these; asked on its own it
        # writes the answer. This is what provider.generate() is for.
        rest = []
        for f in unresolved:
            if not _is_free_text(f):
                rest.append(f)
                continue
            try:
                text = (provider.generate(_FREE_TEXT_PROMPT.format(question=f.label),
                                          profile) or "").strip()
            except Exception:
                text = ""
            if not text:
                rest.append(f)
                continue
            mappings.append(Mapping(field_id=f.id, action="generate", value=text,
                                    confidence=0.5, source="llm-generate",
                                    label=f.label))
        unresolved = rest

    for f in unresolved:
        mappings.append(Mapping(field_id=f.id, action="unknown", label=f.label))

    if repeated:
        _spread_repeated_values(mappings, repeated, profile)
    return mappings


# Links the profile offers, in the order a second or third entry should reach
# for them.
_LINK_PATHS = ("links.portfolio", "links.linkedin", "links.github",
               "links.other_website")


def _spread_repeated_values(mappings: list[Mapping], repeated: set[str],
                            profile: dict) -> None:
    """Give repeated fields distinct values.

    Routing them to the model was not enough: asked separately, it answered
    both "URL*" entries with the same GitHub link, and Workday rejects the step
    with "You can't add duplicate website URLs". The value has to be made
    distinct where it is assigned, not argued about afterwards -- by the time
    the audit noticed, both were already written.
    """
    available = [v for v in (get_value(profile, p) for p in _LINK_PATHS) if v]
    used = {m.value for m in mappings
            if m.field_id not in repeated and m.action == "fill" and m.value}

    for m in mappings:
        if m.field_id not in repeated:
            continue
        if m.action == "fill" and m.value and m.value not in used:
            used.add(m.value)
            continue
        # Either a duplicate of a value already on the form, or a repeated
        # field nothing answered. Both want the next link not yet used.
        spare = next((v for v in available if v not in used), "")
        if spare:
            m.action, m.value, m.source = "fill", spare, "spread"
            used.add(spare)
        elif m.action == "fill":
            # Nothing distinct left to say. Leaving it empty is honest; the
            # gate blocks on it if the form requires it.
            m.action, m.value = "unknown", ""


# A question phrased as one. Workday's application questions are nearly all of
# this shape, and a rule matching a word inside one answers it with a fact:
# "In your current role, do you engage with Mastercard employees..." took the
# current-role rule and was answered "Software Engineer Intern", and "Are you
# currently engaged in any outside employment... please state..." took the
# state rule and was answered "NJ".
# The question phrase can sit anywhere: "In your current role, do you engage
# with Mastercard employees..." is a yes/no question that does not begin like
# one. A wh-question is excluded -- "What is your current role?" wants the role,
# and "How did you hear about us?" wants the source.
YES_NO_QUESTION = re.compile(
    r"\b(do|does|did|are|were|have|has|had|will|would|can|could|should)\s+you(r)?\b")
WH_QUESTION = re.compile(r"^(what|which|when|where|who|whom|whose|how|why)\b")
YES_NO_ANSWERS = {"yes", "no", "y", "n", "true", "false"}


def _looks_like_yes_no(label: str, value: str) -> bool:
    """A yes/no question about to be answered with something that is neither."""
    norm = normalize_label(label)
    if WH_QUESTION.match(norm) or not YES_NO_QUESTION.search(norm):
        return False
    low = value.strip().lower()
    return low not in YES_NO_ANSWERS and not any(h in low for h in DECLINE_HINTS)


def _build(f: Field, path: str, profile: dict, source: str, confidence: float) -> Mapping:
    value = get_value(profile, path)
    if not value:
        # Optional and unanswerable is fine; required and unanswerable blocks auto.
        return Mapping(field_id=f.id, action="unknown" if f.required else "skip",
                       source=source, label=f.label)
    # A fact is not an answer to a yes/no question. Leave it unknown so the
    # model answers it -- which is what happened to every question on this step
    # that a rule did not claim first.
    if _looks_like_yes_no(f.label, value):
        return Mapping(field_id=f.id, action="unknown", source=source, label=f.label)

    if f.kind in ("select", "radio", "combobox") and f.options:
        chosen = resolve_option(value, f.options)
        if chosen is None:
            return Mapping(field_id=f.id, action="unknown", source=source, label=f.label)
        value = chosen
    return Mapping(field_id=f.id, action="fill", value=value,
                   confidence=confidence, source=source, label=f.label)
