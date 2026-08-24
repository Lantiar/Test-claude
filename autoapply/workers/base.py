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
                if self._write(f, m.value):
                    outcome.filled_ids.append(f.id)
            except Exception as exc:                       # one field never kills the run
                outcome.errors.append(f"{f.label or f.id}: {exc}")

        outcome.saw_captcha = self.saw_captcha()
        outcome.filled_ok = not outcome.missing_required
        outcome.screenshot_path = self.screenshot(job, screenshot_dir)
        return outcome

    def _write(self, f: Field, value: str) -> bool:
        el = self.page.query_selector(f.selector)
        if el is None:
            return False
        if f.kind == "file":
            path = os.path.expanduser(value)
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            el.set_input_files(path)
            return True
        if f.kind == "select":
            try:
                el.select_option(label=value)
            except Exception:
                el.select_option(value=value)
            return True
        if f.kind in ("checkbox", "radio"):
            if str(value).strip().lower() in ("yes", "true", "1", "on"):
                el.check()
            return True
        self.page.evaluate(SET_VALUE_JS, [el, value])
        return True

    def screenshot(self, job: Job, directory: str) -> str:
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
        btn = self.page.query_selector(self.submit_selector)
        if btn is None:
            return False, "submit button not found"
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
    from .greenhouse import GreenhouseWorker
    from .lever import LeverWorker

    return {"greenhouse": GreenhouseWorker, "lever": LeverWorker}.get(ats, lambda p: None)(page)
