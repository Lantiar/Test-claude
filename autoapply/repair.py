"""Read the form's own complaints, fix what they point at, and remember it.

The form is a better critic than any model we could ask. When Workday rejects a
step it says exactly which field is wrong and why -- "The field How Did You Hear
About Us? is required and must have a value" -- and marks the input
aria-invalid. That is ground truth, free, and available before anything is
submitted. Nothing was reading it.

So each step now goes: fill -> ask the form what it objects to -> ask the model
for a better answer to those specific fields, with the complaint as context ->
write it back -> and persist whatever worked, so the next run starts with the
right answer instead of rediscovering it.

That last part is the point. A field this gets wrong once should be wrong once,
not once per application. The corrections table already existed for answers a
human fixed in the dashboard; this writes to the same place, so a fix learned
from the form and a fix typed by a person are reused identically.

What it cannot repair is a value that is right but would not stick -- a widget
the filler drives incorrectly needs the filler fixed. It retries such a field
and reports it still failing rather than quietly claiming success.
"""
from __future__ import annotations

import json
import re

from .models import Field, Mapping

# Fields a form has marked invalid, and the text explaining why. aria-invalid
# and aria-errormessage are standards rather than Workday conventions, so this
# reads iCIMS and Greenhouse too.
READ_ERRORS_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];

  const errorTextFor = (el) => {
    const ids = ((el.getAttribute('aria-errormessage') || '') + ' ' +
                 (el.getAttribute('aria-describedby') || '')).split(/\s+/);
    for (const id of ids) {
      if (!id) continue;
      const n = document.getElementById(id);
      const t = clean(n && n.innerText);
      if (t && /required|invalid|must|error|select|enter/i.test(t)) return t;
    }
    // Fall back to error-ish text next to the field.
    let p = el.parentElement, hops = 0;
    while (p && hops++ < 3) {
      const n = p.querySelector('[class*="error" i], [role="alert"]');
      const t = clean(n && n.innerText);
      if (t) return t;
      p = p.parentElement;
    }
    return '';
  };

  const labelFor = (el) => {
    const id = el.getAttribute('id');
    if (id) {
      const l = document.querySelector('label[for="' + CSS.escape(id) + '"]');
      if (l && clean(l.innerText)) return clean(l.innerText);
    }
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map(i => {
        const n = document.getElementById(i); return n ? n.innerText : '';
      }).join(' ');
      if (clean(t)) return clean(t);
    }
    return clean(el.getAttribute('aria-label')) || clean(el.getAttribute('name'));
  };

  document.querySelectorAll('[aria-invalid="true"]').forEach(el => {
    out.push({label: labelFor(el), id: el.getAttribute('id') || '',
              name: el.getAttribute('name') || '', error: errorTextFor(el)});
  });
  return out;
}
"""

AUDIT_SYSTEM = (
    "A job application was filled in automatically by pattern-matching field "
    "labels against a candidate's profile. Audit what it produced.\n"
    "You are given each question and the answer that was entered. Flag only "
    "answers that are WRONG -- not ones that are merely terse or unremarkable.\n"
    "Wrong means: it contradicts the profile; it answers a different question "
    "than the one asked (a phone EXTENSION field holding the full phone number, "
    "a device TYPE field holding a number, a yes/no question holding a school "
    "name); it is a placeholder or a fragment; or it asserts something the "
    "profile does not support.\n"
    "For each wrong answer give the value that should be there instead, using "
    "only the profile plus ordinary sense about what the question asks. If the "
    "field lists options, return exactly one of them, verbatim. If nothing "
    "defensible can be given, return null and it will be left for a human.\n"
    'Reply with JSON only: {"wrong":[{"label":..,"value":..|null,'
    '"why":"a few words","confidence":0.0-1.0}]}'
)

REPAIR_SYSTEM = (
    "A job application form rejected some answers. For each one you are given "
    "the question, the answer that was sent, the form's own complaint, and the "
    "options if it is a picklist.\n"
    "Return a better answer for each, using only facts from the candidate's "
    "profile plus ordinary common sense about what the question is asking.\n"
    "Rules:\n"
    "- If the field lists options, return exactly one of them, verbatim.\n"
    "- Never invent a fact about the candidate. If the profile does not support "
    "an answer and no option is obviously right, return null.\n"
    "- Read the question carefully: a field asking for a phone EXTENSION is not "
    "asking for the phone number, and a field asking for a device TYPE wants "
    "something like Mobile, not a number.\n"
    'Reply with JSON only: {"fixes":[{"label":..,"value":..|null,'
    '"confidence":0.0-1.0}]}'
)


def audit_step(worker, fields: list[Field], mappings: list[Mapping],
               profile: dict, provider=None, store=None,
               ats: str = "") -> tuple[int, list[str]]:
    """Check what the deterministic fill produced, and correct what is wrong.

    The tier between the script and the form's own verdict. The form only
    objects to what is missing or malformed -- it will happily accept a phone
    extension field containing the full phone number, because that is a valid
    string. Only a reader who knows what the question meant catches that.

    Every correction is taught, so the deterministic pass gets it right next
    time and this tier stops being consulted for that field at all. The point
    is for the model to be needed less over time, not the same amount forever.
    """
    notes: list[str] = []
    if provider is None or getattr(provider, "name", "rules") == "rules":
        return 0, notes

    by_id = {f.id: f for f in fields}
    filled = [m for m in mappings
              if m.action in ("fill", "generate") and m.value
              and m.field_id in by_id]
    # A taught answer was already confirmed once; re-auditing it every run costs
    # a model call to relearn what is on file.
    filled = [m for m in filled if m.source != "learned"]
    if not filled:
        return 0, notes

    payload = [{"label": m.label or m.field_id,
                "entered": m.value,
                "options": by_id[m.field_id].options,
                "kind": by_id[m.field_id].kind} for m in filled]
    try:
        raw = provider._chat(
            AUDIT_SYSTEM,
            "Candidate profile:\n" + json.dumps(profile, indent=2)
            + "\n\nWhat was entered:\n" + json.dumps(payload, indent=2))
    except Exception as exc:
        notes.append(f"audit unavailable: {type(exc).__name__}")
        return 0, notes

    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return 0, notes
    try:
        wrong = json.loads(raw[start:end + 1]).get("wrong") or []
    except json.JSONDecodeError:
        return 0, notes

    by_label = {_norm(m.label or m.field_id): m for m in filled}
    fixed = 0
    for item in wrong:
        mapping = by_label.get(_norm(item.get("label", "")))
        if mapping is None:
            continue
        field = by_id.get(mapping.field_id)
        value = item.get("value")
        why = item.get("why") or "audited as wrong"
        if not value:
            notes.append(f"{mapping.label}: flagged ({why}), no replacement offered")
            continue
        if field.options and value not in field.options:
            from .mapper import resolve_option
            value = resolve_option(str(value), field.options) or ""
            if not value:
                notes.append(f"{mapping.label}: flagged ({why}), no matching option")
                continue
        try:
            written = worker._write(field, str(value))
        except Exception as exc:
            notes.append(f"{mapping.label}: rewrite failed ({exc})")
            continue
        if written is None:
            notes.append(f"{mapping.label}: '{value}' would not stick")
            continue

        mapping.value = written
        mapping.source = "audit"
        mapping.confidence = float(item.get("confidence", 0.6))
        fixed += 1
        notes.append(f"{mapping.label}: {why} -> '{written}'")
        _teach(store, ats, mapping.label, written)

    return fixed, notes


# A dropdown prompt is not an answer. Teaching one would make every later run
# fill the field with it and fail the same validation, which is worse than not
# learning at all -- a wrong taught answer outranks the rules that follow it.
_NOT_AN_ANSWER = re.compile(r"^(select|choose)\b|^-{2,}|^please\s+select$", re.I)


def _teach(store, ats: str, label: str, value: str) -> None:
    """Record an answer so the deterministic pass produces it next time."""
    if store is None or not label or not value:
        return
    if _NOT_AN_ANSWER.match(value.strip()):
        return
    from .mapper import signature
    try:
        store.record_correction(signature(ats, label), label, value)
    except Exception:
        pass


def read_errors(worker) -> list[dict]:
    """What the form says is wrong, across every frame."""
    found: list[dict] = []
    for frame in worker.frames():
        try:
            found.extend(frame.evaluate(READ_ERRORS_JS) or [])
        except Exception:
            continue
    # Only entries that actually name something.
    return [e for e in found if (e.get("label") or e.get("id"))]


def _match_field(entry: dict, fields: list[Field]) -> Field | None:
    """The discovered field an error entry refers to."""
    eid, name = entry.get("id") or "", entry.get("name") or ""
    for f in fields:
        if eid and (f.id == eid or eid in f.selector):
            return f
        if name and f.id == name:
            return f
    label = _norm(entry.get("label") or "")
    if not label:
        return None
    for f in fields:
        fl = _norm(f.label)
        if fl and (fl == label or fl in label or label in fl):
            return f
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def repair_step(worker, fields: list[Field], mappings: list[Mapping],
                profile: dict, provider=None, store=None,
                ats: str = "") -> tuple[int, list[str]]:
    """Fix what the form objected to. Returns (repaired count, notes).

    Anything that works is written to the corrections store, so the next
    application answers it correctly the first time.
    """
    notes: list[str] = []
    entries = read_errors(worker)
    if not entries:
        return 0, notes

    by_id = {m.field_id: m for m in mappings}
    targets: list[tuple[dict, Field, Mapping | None]] = []
    for entry in entries:
        field = _match_field(entry, fields)
        if field is not None:
            targets.append((entry, field, by_id.get(field.id)))

    if not targets:
        notes.append(f"form reported {len(entries)} error(s) on fields we did "
                     "not discover")
        return 0, notes

    if provider is None or getattr(provider, "name", "rules") == "rules":
        notes.append(f"{len(targets)} field(s) rejected; no model to repair them")
        return 0, notes

    payload = [{
        "label": field.label or field.id,
        "sent": (mapping.value if mapping else ""),
        "complaint": entry.get("error") or "rejected",
        "options": field.options,
        "kind": field.kind,
    } for entry, field, mapping in targets]

    try:
        raw = provider._chat(
            REPAIR_SYSTEM,
            "Candidate profile:\n" + json.dumps(profile, indent=2)
            + "\n\nRejected answers:\n" + json.dumps(payload, indent=2))
    except Exception as exc:
        notes.append(f"repair pass unavailable: {type(exc).__name__}")
        return 0, notes

    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        notes.append("repair pass returned nothing usable")
        return 0, notes
    try:
        fixes = json.loads(raw[start:end + 1]).get("fixes") or []
    except json.JSONDecodeError:
        notes.append("repair pass returned nothing usable")
        return 0, notes

    suggested = {_norm(f.get("label", "")): f for f in fixes if f.get("label")}
    repaired = 0

    for entry, field, mapping in targets:
        fix = suggested.get(_norm(field.label or field.id))
        value = (fix or {}).get("value")
        if not value:
            notes.append(f"{field.label or field.id}: no better answer available")
            continue
        try:
            written = worker._write(field, str(value))
        except Exception as exc:
            notes.append(f"{field.label or field.id}: rewrite failed ({exc})")
            continue
        if written is None:
            # The answer may be right and the widget simply not driveable by
            # the current filler. Say so rather than counting it as fixed.
            notes.append(f"{field.label or field.id}: '{value}' would not stick")
            continue

        if mapping is not None:
            mapping.value = written
            mapping.action = "fill"
            mapping.source = "form-repair"
            mapping.confidence = float((fix or {}).get("confidence", 0.6))
        else:
            mappings.append(Mapping(field_id=field.id, action="fill",
                                    value=written, source="form-repair",
                                    confidence=0.6, label=field.label))
        repaired += 1
        notes.append(f"{field.label or field.id}: -> '{written}'")

        # Remember it. This is the part that makes the next run start correct
        # rather than making the same mistake and repairing it again.
        _teach(store, ats, field.label, written)

    return repaired, notes
