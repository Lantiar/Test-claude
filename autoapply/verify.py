"""Read the form back and confirm what we believe we set is actually there.

This is deterministic DOM readback, not an LLM pass: it costs nothing, runs on
every application, and catches the failure it exists to catch (a field the
worker thought it filled but didn't). The LLM/agent verifier belongs above this
as an escalation for unknown ATSs, not as the first tier.
"""
from __future__ import annotations

from .models import FillOutcome

READ_JS = """
(el) => {
  if (!el) return '';
  if (el.tagName === 'SELECT') {
    const o = el.selectedOptions[0];
    return o ? (o.innerText || o.value || '') : '';
  }
  // Workday-style dropdowns are buttons that display the chosen option as text;
  // they have no .value to read.
  if (el.tagName === 'BUTTON') return (el.innerText || '').trim();
  if (el.type === 'checkbox' || el.type === 'radio') return el.checked ? 'on' : '';
  if (el.type === 'file') return (el.files && el.files.length) ? el.files[0].name : '';
  if ('value' in el) return el.value || '';
  return (el.innerText || '').trim();
}
"""


def _matches(expected: str, actual: str, kind: str) -> bool:
    e, a = expected.strip().lower(), actual.strip().lower()
    if not a:
        return False
    if kind == "file":
        return a in e or e.endswith(a)          # browsers report basename only
    if kind in ("checkbox", "radio"):
        return a == "on"
    return e == a or e in a or a in e           # selects normalize whitespace/case


def verify_fields(page, fields, mappings, filled_ids: list[str]) -> tuple[bool, dict]:
    """Check one page's worth of fields. Wizard steps call this before advancing."""
    by_id = {f.id: f for f in fields}
    detail: dict[str, dict] = {}
    ok = True

    for m in mappings:
        if m.action not in ("fill", "generate") or not m.value:
            continue
        f = by_id.get(m.field_id)
        if f is None:
            continue
        try:
            el = page.query_selector(f.selector)
            actual = page.evaluate(READ_JS, el) if el else ""
        except Exception as exc:
            detail[m.field_id] = {"label": f.label, "error": str(exc)}
            ok = False
            continue
        good = _matches(m.value, actual, f.kind)
        detail[m.field_id] = {"label": f.label, "expected": m.value,
                              "actual": actual, "ok": good}
        if not good:
            ok = False
            if f.id in filled_ids:
                filled_ids.remove(f.id)
    return ok, detail


def verify(page, outcome: FillOutcome) -> FillOutcome:
    by_id = {f.id: f for f in outcome.fields}
    detail: dict[str, dict] = {}
    ok = True

    for m in outcome.mappings:
        if m.action not in ("fill", "generate") or not m.value:
            continue
        f = by_id.get(m.field_id)
        if f is None:
            continue
        try:
            el = page.query_selector(f.selector)
            actual = page.evaluate(READ_JS, el) if el else ""
        except Exception as exc:
            actual, exc_note = "", str(exc)
            detail[m.field_id] = {"label": f.label, "error": exc_note}
            ok = False
            continue
        good = _matches(m.value, actual, f.kind)
        detail[m.field_id] = {"label": f.label, "expected": m.value,
                              "actual": actual, "ok": good}
        if not good:
            ok = False
            # It isn't really filled if it didn't stick — let the gate see that.
            if f.id in outcome.filled_ids:
                outcome.filled_ids.remove(f.id)

    # A required field left empty is a verification failure too.
    missing = outcome.missing_required
    if missing:
        ok = False
        detail["_missing_required"] = {"fields": missing}

    outcome.verified = ok
    outcome.verify_detail = detail
    return outcome
