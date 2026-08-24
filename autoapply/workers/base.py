"""Shared worker behaviour: discover fields, fill them, screenshot, submit.

Greenhouse and Lever differ only in their container/submit selectors and a few
quirks, so the DOM work lives here and the subclasses stay small.
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional

from ..models import Field, FillOutcome, Job, Mapping

SKIP_TYPES = {"submit", "button", "reset", "image"}
CAPTCHA_MARKERS = ("recaptcha", "hcaptcha", "cf-turnstile", "captcha")

# Reads the label for a control the way a person would: explicit <label for>,
# ARIA, an ancestor label, the nearest preceding text, then placeholder/name.
LABEL_JS = """
(el) => {
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  if (el.id) {
    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (l && clean(l.innerText)) return clean(l.innerText);
  }
  const aria = el.getAttribute('aria-label');
  if (clean(aria)) return clean(aria);
  const labelledby = el.getAttribute('aria-labelledby');
  if (labelledby) {
    const t = labelledby.split(/\\s+/).map(id => {
      const n = document.getElementById(id); return n ? n.innerText : '';
    }).join(' ');
    if (clean(t)) return clean(t);
  }
  const anc = el.closest('label');
  if (anc && clean(anc.innerText)) return clean(anc.innerText);
  let node = el.parentElement, hops = 0;
  while (node && hops++ < 4) {
    const lbl = node.querySelector('label, .label, legend, [class*="label"]');
    if (lbl && clean(lbl.innerText)) return clean(lbl.innerText);
    node = node.parentElement;
  }
  return clean(el.getAttribute('placeholder')) || clean(el.getAttribute('name'));
}
"""

# React and similar frameworks track value on the DOM node; assigning .value
# directly is invisible to them, so go through the native setter and fire events.
SET_VALUE_JS = """
([el, value]) => {
  const proto = el.tagName === 'TEXTAREA'
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input',  { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new Event('blur',   { bubbles: true }));
}
"""


class Worker:
    ats = "generic"
    form_selector = "form"
    submit_selector = "button[type=submit]"
    # Text that means the application landed. Checked after submit; without a
    # match we do not record an application as applied.
    confirm_patterns = (r"thank you", r"application (was )?(received|submitted)",
                        r"we('| ha)ve received", r"successfully submitted")

    def __init__(self, page):
        self.page = page

    # ---- discovery -------------------------------------------------------
    def open(self, job: Job) -> None:
        self.page.goto(job.url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(1500)

    def saw_captcha(self) -> bool:
        html = self.page.content().lower()
        return any(m in html for m in CAPTCHA_MARKERS)

    # Selectors that mean "you must sign in or create an account to continue".
    auth_selectors: tuple[str, ...] = ()

    def needs_auth(self) -> bool:
        return query_first(self.page, self.auth_selectors) is not None

    def discover(self) -> list[Field]:
        fields: list[Field] = []
        root = self.page.query_selector(self.form_selector) or self.page
        for idx, el in enumerate(root.query_selector_all("input, textarea, select")):
            try:
                if not el.is_visible():
                    continue
            except Exception:
                continue
            tag = (el.evaluate("e => e.tagName") or "").lower()
            itype = (el.get_attribute("type") or "text").lower()
            if tag == "input" and itype in SKIP_TYPES:
                continue
            if (el.get_attribute("aria-hidden") or "") == "true":
                continue

            kind = ("textarea" if tag == "textarea"
                    else "select" if tag == "select"
                    else itype)
            label = (self.page.evaluate(LABEL_JS, el) or "").strip()
            name = el.get_attribute("name") or ""
            fid = el.get_attribute("id") or name or f"field-{idx}"

            required = bool(el.get_attribute("required")) \
                or (el.get_attribute("aria-required") == "true") \
                or "*" in label

            options: list[str] = []
            if kind == "select":
                options = [
                    (o.inner_text() or o.get_attribute("value") or "").strip()
                    for o in el.query_selector_all("option")
                ]
                options = [o for o in options
                           if o and not re.match(r"^(select|choose|--)", o.lower())]

            # A unique selector we can find again during verification.
            if el.get_attribute("id"):
                selector = f'#{css_escape(el.get_attribute("id"))}'
            elif name:
                selector = f'{tag}[name="{name}"]'
            else:
                selector = f"{self.form_selector} {tag}:nth-of-type({idx + 1})"

            fields.append(Field(id=fid, selector=selector, label=label or name,
                                kind=kind, required=required, options=options))
        return fields

    # ---- lifecycle -------------------------------------------------------
    # Single-page workers let the pipeline verify afterwards. Wizard workers
    # verify each step before advancing, because earlier steps' DOM is gone by
    # the time the pipeline looks.
    verifies_internally = False

    def run(self, job: Job, profile: dict, store, provider,
            screenshot_dir: str) -> FillOutcome:
        from .. import mapper

        self.open(job)
        fields = self.discover()
        mappings = mapper.map_fields(fields, profile, job.ats,
                                     store=store, provider=provider)
        return self.fill(job, fields, mappings, screenshot_dir)

    # ---- filling ---------------------------------------------------------
    def fill(self, job: Job, fields: list[Field], mappings: list[Mapping],
             screenshot_dir: str) -> FillOutcome:
        outcome = FillOutcome(job=job, fields=fields, mappings=mappings)
        by_id = {f.id: f for f in fields}

        for m in mappings:
            if m.action not in ("fill", "generate") or not m.value:
                continue
            f = by_id.get(m.field_id)
            if f is None:
                continue
            try:
                written = self._write(f, m.value)
                if written is not None:
                    # A custom dropdown may render the option differently to how
                    # the profile spells it; record what is actually on the form.
                    m.value = written
                    outcome.filled_ids.append(f.id)
            except Exception as exc:                       # one field never kills the run
                outcome.errors.append(f"{f.label or f.id}: {exc}")

        outcome.saw_captcha = self.saw_captcha()
        outcome.needs_auth = self.needs_auth()
        outcome.filled_ok = not outcome.missing_required
        outcome.screenshot_path = self.screenshot(job, screenshot_dir)
        return outcome

    def _write(self, f: Field, value: str) -> Optional[str]:
        """Write one field. Returns the value actually written, or None."""
        el = self.page.query_selector(f.selector)
        if el is None:
            return None
        if f.kind == "file":
            path = os.path.expanduser(value)
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            el.set_input_files(path)
            return value
        if f.kind == "select":
            try:
                el.select_option(label=value)
            except Exception:
                el.select_option(value=value)
            return value
        if f.kind in ("checkbox", "radio"):
            if str(value).strip().lower() in ("yes", "true", "1", "on"):
                el.check()
            return value
        self.page.evaluate(SET_VALUE_JS, [el, value])
        return value

    def screenshot(self, job: Job, directory: str) -> str:
        # Wizard steps fill with no directory; only the final page is captured.
        if not directory:
            return ""
        os.makedirs(directory, exist_ok=True)
        safe = re.sub(r"[^a-z0-9]+", "-", job.key.lower())[-80:]
        path = os.path.join(directory, f"{int(time.time())}{safe}.png")
        try:
            self.page.screenshot(path=path, full_page=True)
        except Exception:
            return ""
        return path

    # ---- submission ------------------------------------------------------
    def submit(self) -> tuple[bool, str]:
        """Click submit and confirm the application actually landed."""
        btn = query_first(self.page, (self.submit_selector,))
        if btn is None:
            return False, "submit button not visible"
        before = self.page.url
        btn.click()
        try:
            self.page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        self.page.wait_for_timeout(2000)
        return self.confirmed(before)

    def confirmed(self, url_before: str) -> tuple[bool, str]:
        body = ""
        try:
            body = (self.page.inner_text("body") or "").lower()
        except Exception:
            pass
        for pattern in self.confirm_patterns:
            if re.search(pattern, body):
                return True, f"confirmed: matched /{pattern}/"
        if self.page.url != url_before and "confirmation" in self.page.url.lower():
            return True, f"confirmed: redirected to {self.page.url}"
        return False, "no post-submit confirmation found"


def css_escape(value: str) -> str:
    return re.sub(r"([^a-zA-Z0-9_-])", r"\\\1", value)


def get_worker(ats: str, page) -> Optional[Worker]:
    """DOM workers only. iCIMS, Ashby, Oracle/Taleo and unknown hosts are driven
    by the browser-use agent instead — see workers/agent.py and the playbooks."""
    from .greenhouse import GreenhouseWorker
    from .lever import LeverWorker
    from .workday import WorkdayWorker

    registry = {
        "greenhouse": GreenhouseWorker,
        "lever": LeverWorker,
        "workday": WorkdayWorker,
    }
    cls = registry.get(ats)
    return cls(page) if cls else None


def query_first(scope, selectors: tuple[str, ...] | list[str]):
    """First selector in the list matching a *visible* element.

    Visibility is the point, not a nicety: a Workday wizard keeps the Submit
    button in the DOM and hidden until the review step, so a presence-only check
    would report "we're at review" on page one.
    """
    for sel in selectors:
        try:
            el = scope.query_selector(sel)
            if el is not None and el.is_visible():
                return el
        except Exception:
            continue
    return None


class WizardWorker(Worker):
    """Multi-page application flows (Workday, iCIMS, Oracle).

    Each step is discovered, mapped and filled on its own, then verified before
    we advance — once the next step renders, the previous step's DOM is gone.
    """

    verifies_internally = True
    max_steps = 15
    next_selectors: tuple[str, ...] = ()
    review_selectors: tuple[str, ...] = ()

    def at_review(self) -> bool:
        return query_first(self.page, self.review_selectors) is not None

    def advance(self) -> bool:
        btn = query_first(self.page, self.next_selectors)
        if btn is None:
            return False
        try:
            btn.click()
        except Exception:
            return False
        try:
            self.page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        self.page.wait_for_timeout(1200)
        return True

    def run(self, job: Job, profile: dict, store, provider,
            screenshot_dir: str) -> FillOutcome:
        from .. import mapper
        from ..verify import verify_fields

        self.open(job)
        outcome = FillOutcome(job=job)
        all_ok = True

        for _ in range(self.max_steps):
            if self.saw_captcha():
                outcome.saw_captcha = True
                break
            if self.needs_auth():
                outcome.needs_auth = True
                break

            fields = self.discover()
            mappings = mapper.map_fields(fields, profile, job.ats,
                                         store=store, provider=provider)
            step = self.fill(job, fields, mappings, screenshot_dir="")
            outcome.fields.extend(fields)
            outcome.mappings.extend(mappings)
            outcome.filled_ids.extend(step.filled_ids)
            outcome.errors.extend(step.errors)

            ok, detail = verify_fields(self.page, fields, mappings,
                                       outcome.filled_ids)
            outcome.verify_detail.update(detail)
            all_ok = all_ok and ok

            if self.at_review() or not self.advance():
                break

        outcome.saw_captcha = outcome.saw_captcha or self.saw_captcha()
        outcome.needs_auth = outcome.needs_auth or self.needs_auth()
        outcome.verified = all_ok and not outcome.missing_required
        outcome.filled_ok = not outcome.missing_required
        outcome.screenshot_path = self.screenshot(job, screenshot_dir)
        return outcome
