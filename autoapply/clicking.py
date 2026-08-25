"""Clicking a control that something else is painted on top of.

Workday renders its real <button> and then covers it with
div[data-automation-id="click_filter"][role=button], carrying the same
aria-label and the actual handler. A plain click on the button underneath never
lands: Playwright waits for it to stop being obscured and gives up after 30
seconds, so the failure reads as a mysterious timeout rather than as an
interception.

That pattern cost this project the same half hour twice -- once on the sign-in
and create-account buttons, then again on the dropdowns, because the fix lived
in the sign-in module and the workers could not reach it. It belongs somewhere
both can see.

Not Workday-specific: overlays, custom focus rings and sticky footers do the
same thing on plenty of forms.
"""
from __future__ import annotations

# Does the element on top stand for the same control as the one underneath?
# Compared on aria-label or text so an unrelated overlay -- a cookie banner, a
# modal backdrop -- is not clicked in its place.
SAME_CONTROL_JS = r"""
el => {
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  if (!top || top === el || el.contains(top)) return null;
  const norm = n => ((n.getAttribute('aria-label') || n.innerText || '')
                     .trim().toLowerCase());
  const a = norm(top), b = norm(el);
  if (!a || !b) return null;
  return (a === b || a.includes(b) || b.includes(a)) ? top : null;
}
"""


def click(el, timeout: int = 8000) -> bool:
    """Click `el`, or whatever is standing in front of it. False if none worked.

    The timeout is deliberately short. The default 30s is spent waiting for an
    obstruction that is never going away, and a run with a dozen such fields
    spends minutes discovering that.
    """
    try:
        el.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    try:
        handle = el.evaluate_handle(SAME_CONTROL_JS)
        proxy = handle.as_element()
        if proxy is not None:
            proxy.click(timeout=timeout)
            return True
    except Exception:
        pass

    try:
        el.click(timeout=timeout)
        return True
    except Exception:
        pass

    # Last resort: dispatch the click directly, which ignores pointer-event
    # interception entirely. It skips the browser's own actionability checks,
    # so it is tried only after the honest attempts.
    try:
        el.evaluate("e => e.click()")
        return True
    except Exception:
        return False
