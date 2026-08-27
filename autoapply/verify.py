"""Read the form back and confirm what we believe we set is actually there.

This is deterministic DOM readback, not an LLM pass: it costs nothing, runs on
every application, and catches the failure it exists to catch (a field the
worker thought it filled but didn't). The LLM/agent verifier belongs above this
as an escalation for unknown ATSs, not as the first tier.
"""
from __future__ import annotations

import os
import re

from . import log as _log
from .models import FillOutcome

READ_JS = r"""
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
  if (el.type === 'file') {
    if (el.files && el.files.length) return el.files[0].name;
    // Most ATSs swap the input for a "resume.pdf  Remove" chip once the upload
    // lands, so the input itself reads back empty even though the file is
    // attached. Look for a filename in the surrounding container before
    // concluding nothing was uploaded.
    let n = el.parentElement, hops = 0;
    while (n && hops++ < 4) {
      const m = (n.innerText || '').match(/[^\s\/\\]+\.(pdf|docx?|txt|rtf)\b/i);
      if (m) return m[0];
      n = n.parentElement;
    }
    return '';
  }
  if ('value' in el) return el.value || '';
  return (el.innerText || '').trim();
}
"""


# A combobox commits its choice to the widget's display, clearing the input it
# was typed into, so el.value reads empty on a field that is correctly set.
READ_COMBO_JS = r"""
(el) => {
  // react-select and friends show the committed choice in a *sibling* of the
  // input (single-value / multi-value), while the input's own container holds
  // only the input and therefore reads empty.
  let n = el, hops = 0;
  while (n && hops++ < 6) {
    if (n.querySelector) {
      const sv = n.querySelector('[class*="single-value"], [class*="singleValue"],'
                               + ' [class*="multi-value"], [class*="multiValue"]');
      if (sv && sv.innerText && sv.innerText.trim()) return sv.innerText.trim();
    }
    n = n.parentElement;
  }
  const box = el.closest('[class*="control"], [class*="select"]') || el.parentElement;
  const text = box ? (box.innerText || '') : '';
  // Screen-reader state text is not a value. An open Workday multiselect
  // renders "Expanded" into its container, so the readback returned Expanded
  // as the field's answer -- verification compared it against what we meant to
  // write, called the mismatch a failure, and the real problem (the write
  // never landed) was reported as the wrong thing entirely.
  const STATE = /^(expanded|collapsed|selected|required|optional|open|closed|clear|remove|search)$/i;
  const first = text.split('\n').map(s => s.trim())
                    .filter(Boolean).filter(s => !STATE.test(s))[0];
  return first || el.value || '';
}
"""


# A radio/checkbox group is many elements; reading the first one answers
# "is the first option checked", which is not the question. Read the labels of
# whichever members are actually checked.
READ_CHOICE_JS = r"""
(els) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const labelOf = (n) => {
    const id = n.getAttribute('id');
    if (id) {
      const l = document.querySelector('label[for="' + CSS.escape(id) + '"]');
      if (l && clean(l.innerText)) return clean(l.innerText);
    }
    const anc = n.closest('label');
    return anc ? clean(anc.innerText) : '';
  };
  return els.filter(e => e.checked).map(labelOf).filter(Boolean).join(', ');
}
"""


def _ctx(page, f):
    """The frame a field lives in: a selector only resolves against its own."""
    url = getattr(f, "frame_url", "")
    if not url:
        return page
    for fr in page.frames:
        if fr.url == url:
            return fr
    return page


def _read(page, el, kind: str) -> str:
    return page.evaluate(READ_COMBO_JS if kind == "combobox" else READ_JS, el)


def _read_field(page, f, el) -> tuple[str, str]:
    """(value read back, kind to compare it as)."""
    ctx = _ctx(page, f)
    if f.kind in ("radio", "checkbox") and f.options:
        try:
            return ctx.eval_on_selector_all(f.selector, READ_CHOICE_JS), "text"
        except Exception:
            return "", "text"
    return _read(ctx, el, f.kind), f.kind


def _squash(s: str) -> str:
    """Strip everything a form widget is free to reformat."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _matches(expected: str, actual: str, kind: str) -> bool:
    e, a = expected.strip().lower(), actual.strip().lower()
    if not a:
        return False
    if kind == "file":
        # Compare basenames: we hold a path, the DOM reports a filename.
        e = os.path.basename(e)
        return a in e or e.endswith(a) or e in a
    if kind in ("checkbox", "radio"):
        return a == "on"
    if e == a or e in a or a in e:              # selects normalize whitespace/case
        return True
    # Widgets rewrite what they are given -- an intl phone input turns
    # "224-333-1045" into "2243331045", a date picker re-punctuates, a currency
    # field adds separators. Byte-equality would call every one of those a
    # verification failure, so fall back to comparing the characters that carry
    # the meaning.
    se, sa = _squash(e), _squash(a)
    return bool(se) and bool(sa) and (se == sa or se in sa or sa in se)


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
            el = _ctx(page, f).query_selector(f.selector)
            actual, cmp_kind = _read_field(page, f, el) if el else ("", f.kind)
        except Exception as exc:
            detail[m.field_id] = {"label": f.label, "error": str(exc)}
            ok = False
            continue
        good = _matches(m.value, actual, cmp_kind)
        detail[m.field_id] = {"label": f.label, "expected": m.value,
                              "actual": actual, "ok": good}
        if not good:
            ok = False
            _log.get("verify").warning(
                "%s: wrote %r, form holds %r",
                _log.brief(f.label or f.id, 50), _log.brief(str(m.value), 60),
                _log.brief(str(actual), 60))
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
            el = _ctx(page, f).query_selector(f.selector)
            actual, cmp_kind = _read_field(page, f, el) if el else ("", f.kind)
        except Exception as exc:
            detail[m.field_id] = {"label": f.label, "error": str(exc)}
            ok = False
            continue
        good = _matches(m.value, actual, cmp_kind)
        detail[m.field_id] = {"label": f.label, "expected": m.value,
                              "actual": actual, "ok": good}
        if not good:
            ok = False
            # Say which field and what it holds. "verification failed" on its
            # own is the whole report a run gives for this, and it names
            # neither -- so every investigation of it starts by re-running the
            # application to find out what the sentence meant. The detail was
            # being recorded and thrown away.
            _log.get("verify").warning(
                "%s: wrote %r, form holds %r",
                _log.brief(f.label or f.id, 50), _log.brief(str(m.value), 60),
                _log.brief(str(actual), 60))
            # It isn't really filled if it didn't stick — let the gate see that.
            if f.id in outcome.filled_ids:
                outcome.filled_ids.remove(f.id)

    # A required field left empty is a verification failure too.
    missing = outcome.missing_required
    if missing:
        ok = False
        detail["_missing_required"] = {"fields": missing}

    # Nothing discovered means nothing was checked, and a pass over an empty
    # list succeeds vacuously. The wizard path already refuses to call that
    # verified; the single-page path has to agree, or a page whose markup
    # defeated discovery reports verified=True having read no form at all.
    if not outcome.fields:
        ok = False
        detail["_no_fields"] = {"note": "discovery found no fields to verify"}

    outcome.verified = ok
    # update, not replace. Assigning a fresh dict here wiped _landed_url and
    # _page_title, which fill() had just recorded -- so the sanity reviewer was
    # handed the URL we asked for instead of the one we ended up on, and BNY's
    # posting being swapped for the careers homepage was invisible to the one
    # tier that could have named it.
    outcome.verify_detail.update(detail)
    return outcome
