"""Shared worker behaviour: discover fields, fill them, screenshot, submit.

Greenhouse and Lever differ only in their container/submit selectors and a few
quirks, so the DOM work lives here and the subclasses stay small.
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional

from .. import log as _log
from ..models import Field, FillOutcome, Job, Mapping

SKIP_TYPES = {"submit", "button", "reset", "image"}
CAPTCHA_MARKERS = ("recaptcha", "hcaptcha", "cf-turnstile", "captcha")
# Markers that contain "captcha" but mean the opposite. Workday renders a
# placeholder div[data-automation-id="noCaptchaWrapper"] on pages with no
# challenge at all, so a bare substring test reports a CAPTCHA on every
# Workday page -- and the gate then blocks a submit that nothing was wrong with.
NON_CAPTCHA_MARKERS = ("nocaptchawrapper", "nocaptcha", "no-captcha")

# Reads the label for a control the way a person would: explicit <label for>,
# ARIA, an ancestor label, the nearest preceding text, then placeholder/name.
LABEL_JS = r"""
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

# The question a radio/checkbox group is asking, as opposed to the label on any
# one of its options. Without this a "Pronouns" group discovers as five separate
# fields called He/Him, She/Her, They/Them ... and nothing can answer any of them.
# A stable key for the group a choice input belongs to. Radios share a name, but
# checkbox sets often do not -- each option gets its own name -- so fall back to
# the identity of the fieldset/group that contains them.
GROUP_KEY_JS = r"""
(el) => {
  // Only an explicit fieldset / group role is trusted to delimit a choice set.
  // Walking up to "the nearest ancestor holding more than one checkbox" looks
  // reasonable and is not: on a real form that ancestor is the whole section,
  // and a dozen unrelated standalone checkboxes collapse into one bogus group.
  const g = el.closest('fieldset, [role="group"], [role="radiogroup"]');
  if (!g) return '';
  if (!g.getAttribute('data-aa-group')) {
    window.__aaGroupN = (window.__aaGroupN || 0) + 1;
    g.setAttribute('data-aa-group', 'grp' + window.__aaGroupN);
  }
  return g.getAttribute('data-aa-group');
}
"""

GROUP_LABEL_JS = r"""
(el) => {
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

  const group = el.closest('fieldset, [role="group"], [role="radiogroup"]');
  if (group) {
    const lg = group.querySelector('legend');
    if (lg && clean(lg.innerText)) return clean(lg.innerText);
    const by = group.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map(id => {
        const n = document.getElementById(id); return n ? n.innerText : '';
      }).join(' ');
      if (clean(t)) return clean(t);
    }
    const lab = group.getAttribute('aria-label');
    if (clean(lab)) return clean(lab);
  }

  // No legend: the question sits outside the group. Walk outwards and subtract
  // the options' own labels from the container's text -- whatever is left is
  // the question. Without this a group is named after its first option, and
  // "Which offices are you interested in?" discovers as "New York, NY".
  const scope = group || el.parentElement;
  const opts = Array.from(
    scope.querySelectorAll('input[type="radio"], input[type="checkbox"]')
  ).map(labelOf).filter(Boolean);

  let n = scope, hops = 0;
  while (n && hops++ < 4) {
    let text = clean(n.innerText);
    opts.forEach(o => { text = text.split(o).join(' '); });
    text = clean(text);
    if (text.length > 2) return text;
    n = n.parentElement;
  }
  return '';
}
"""

# React and similar frameworks track value on the DOM node; assigning .value
# directly is invisible to them, so go through the native setter and fire events.
SET_VALUE_JS = r"""
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
    # Set during fill: the frame the filled fields live in. Submitting has to
    # happen there. An outer page can carry a button that looks like the form's
    # submit and is not -- the iframed fixture has exactly that, and clicking it
    # produces a convincing "Thank you for applying" while the real form in the
    # frame was never sent.
    form_frame_url: str = ""
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

    def frames(self) -> list:
        """Main frame first, then any child frame with real content.

        Application forms are routinely embedded: an iCIMS login, a Greenhouse
        board dropped into a company careers page. Searching only the main frame
        finds nothing on those and reports the page as empty markup.
        """
        out = [self.page.main_frame]
        for fr in self.page.frames:
            if fr is self.page.main_frame:
                continue
            url = (fr.url or "")
            if not url or url == "about:blank":
                continue
            out.append(fr)
        return out

    def frame_for(self, f: Field):
        """The frame a discovered field belongs to, falling back to the page."""
        if not f.frame_url:
            return self.page
        for fr in self.page.frames:
            if fr.url == f.frame_url:
                return fr
        return self.page

    def saw_captcha(self) -> bool:
        html = ""
        for fr in self.frames():
            try:
                html += (fr.content() or "").lower()
            except Exception:
                continue
        for negative in NON_CAPTCHA_MARKERS:
            html = html.replace(negative, "")
        return any(m in html for m in CAPTCHA_MARKERS)

    # Selectors that mean "you must sign in or create an account to continue".
    auth_selectors: tuple[str, ...] = ()

    # A sign-in step announces itself in the URL. Worth checking on its own:
    # an email-first login (iCIMS asks for the address before the password)
    # has no password field on the page yet, so selector matching alone reads
    # it as an ordinary form and the run tries to fill it as an application.
    AUTH_URL_MARKERS = ("/login", "/signin", "/sign-in", "/sign_in",
                        "/register", "/createaccount", "/create-account")

    def needs_auth(self) -> bool:
        if any(query_first(fr, self.auth_selectors) is not None
               for fr in self.frames()):
            return True
        try:
            path = (self.page.url or "").lower().split("?")[0]
        except Exception:
            return False
        return any(marker in path for marker in self.AUTH_URL_MARKERS)

    # Signing in is attempted at most once per run. A second try after a
    # refusal is how an account gets locked, and the credentials would be the
    # same ones that just failed.
    _tried_sign_in = False

    def try_sign_in(self, job) -> tuple[bool, str]:
        """Attempt the sign-in wall this run is stuck behind.

        Returns (signed_in, detail). A failure is not an error: the caller
        queues with the reason, which is what it did before this existed.
        """
        if self._tried_sign_in:
            return False, "already attempted"
        self._tried_sign_in = True

        from ..login import LoginUnavailable, credentials_for, sign_in

        creds = credentials_for(self.page.url) or credentials_for(job.url)
        if not creds:
            return False, "no credentials configured for this host"

        waiter = None
        try:
            from ..mailcode import MailUnavailable, wait_for_code

            def waiter(needles, _w=wait_for_code):           # noqa: E731
                try:
                    return _w(needles, timeout=int(
                        os.getenv("MAIL_CODE_TIMEOUT", "180")))
                except MailUnavailable:
                    return None
        except Exception:
            waiter = None

        log = _log.get("login")
        log.info("attempting sign-in as %s at %s",
                 creds.get("email"), _log.brief(self.page.url, 70))
        try:
            ok, detail = sign_in(self, creds, wait_for_code=waiter,
                                 log=lambda m: log.debug("  %s", m))
            if ok:
                log.info("signed in")
                return True, detail
            log.info("sign-in did not go through: %s", detail)
        except LoginUnavailable as exc:
            return False, str(exc)
        except Exception as exc:                    # never kill the run over it
            detail = f"{type(exc).__name__}: {exc}"

        # Sign-in did not get through, so there may be no account yet.
        # Registering is per-host opt-in because it is the one step here that
        # cannot be undone: it puts a new account on an employer's system under
        # the candidate's name. Same address and password, so the next run
        # signs in to what this created instead of registering again.
        if not creds.get("allow_account_creation"):
            return False, f"{detail}; account creation not enabled for this host"

        from ..login import create_account

        log.info("no account reached; registering (allow_account_creation on)")
        try:
            self.page.reload(wait_until="domcontentloaded")
            self.page.wait_for_timeout(2500)
            made, made_detail = create_account(self, creds, wait_for_code=waiter,
                                               log=lambda m: log.debug("  %s", m))
            log.info("registration -> %s (%s)", made, made_detail)
        except Exception as exc:
            log.warning("registration raised: %s", exc)
            return False, f"{detail}; create failed: {type(exc).__name__}: {exc}"
        return made, f"sign-in: {detail}; create: {made_detail}"

    def discover(self) -> list[Field]:
        """Every frame, not just the top one -- embedded forms are the norm."""
        fields: list[Field] = []
        seen_ids: set[str] = set()
        for frame in self.frames():
            try:
                found = self._discover_in(frame)
            except Exception:
                continue
            for f in found:
                # The same form often appears both standalone and re-embedded
                # in a child frame; keep the first sighting of each field.
                key = f"{f.id}|{f.label}"
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                fields.append(f)
        return fields

    def _discover_in(self, frame) -> list[Field]:
        fields: list[Field] = []
        groups: dict[str, Field] = {}
        frame_url = "" if frame is self.page.main_frame else (frame.url or "")
        root = frame.query_selector(self.form_selector) or frame
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
            # react-select and friends render a listbox behind a plain text
            # input. Typing into it sets no value -- the widget only commits
            # when an option is chosen -- so it needs the click/type/pick path,
            # not the native setter.
            if kind not in ("select", "textarea") and (
                    (el.get_attribute("role") or "") == "combobox"
                    or (el.get_attribute("aria-autocomplete") or "") == "list"):
                kind = "combobox"
            label = (frame.evaluate(LABEL_JS, el) or "").strip()
            name = el.get_attribute("name") or ""
            fid = el.get_attribute("id") or name or f"field-{idx}"

            required = bool(el.get_attribute("required")) \
                or (el.get_attribute("aria-required") == "true") \
                or "*" in label

            # One logical field per radio/checkbox group, carrying the group's
            # question as its label and the members' labels as its options.
            if itype in ("radio", "checkbox"):
                gname = el.get_attribute("name") or ""
                gkey = (frame.evaluate(GROUP_KEY_JS, el) or "").strip()
                # A fieldset outranks the name attribute: Ashby names each
                # checkbox after its own label, so name-keying splits a real
                # group into one field per option. Radios without a fieldset
                # still group by name, which is what name is for.
                if gkey:
                    key = f"{itype}:{gkey}"
                elif itype == "radio" and gname:
                    key = f"radio:{gname}"
                else:
                    key = f"{itype}:{fid}"          # standalone consent box
                if key in groups:
                    if label and label not in groups[key].options:
                        groups[key].options.append(label)
                    groups[key].required = groups[key].required or required
                    continue
                gl = (frame.evaluate(GROUP_LABEL_JS, el) or "").strip()
                field = Field(
                    id=gkey or gname or fid,
                    selector=(f'[data-aa-group="{gkey}"] input[type={itype}]' if gkey
                              else f'{tag}[name="{gname}"]' if (itype == "radio" and gname)
                              else f'[id="{el.get_attribute("id") or fid}"]'),
                    label=gl or label, kind=itype, required=required,
                    options=[label] if label else [], frame_url=frame_url,
                )
                groups[key] = field
                fields.append(field)
                continue

            options: list[str] = []
            if kind == "combobox":
                # The choices only exist while the menu is open, so read them
                # now: the mapper can only pick a real option if it can see the
                # real list, and "How did you hear about us?" never contains the
                # profile's wording verbatim.
                options = self._probe_options(el, frame)
            if kind == "select":
                options = [
                    (o.inner_text() or o.get_attribute("value") or "").strip()
                    for o in el.query_selector_all("option")
                ]
                options = [o for o in options
                           if o and not re.match(r"^(select|choose|--)", o.lower())]

            # A unique selector we can find again during verification.
            if el.get_attribute("id"):
                # [id="..."] rather than #id: a CSS id selector may not begin
                # with a digit, and UUID ids that start with one are everywhere
                # (Ashby names every custom field that way). #0d09... raises a
                # SyntaxError and the field silently never gets written.
                selector = f'[id="{el.get_attribute("id")}"]'
            elif name:
                selector = f'{tag}[name="{name}"]'
            else:
                selector = f"{self.form_selector} {tag}:nth-of-type({idx + 1})"

            fields.append(Field(id=fid, selector=selector, label=label or name,
                                kind=kind, required=required, options=options,
                                frame_url=frame_url))

        # A lone checkbox is a consent box, not a choice between options: keep
        # its own label so "I agree to the terms" is still answerable.
        for field in fields:
            if field.kind == "checkbox" and len(field.options) <= 1:
                field.options = []
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
        if self.needs_auth():
            ok, detail = self.try_sign_in(job)
            if ok:
                # The form only exists past the wall; re-open so discovery sees
                # the application rather than the sign-in page it replaced.
                self.open(job)
        fields = self.discover()
        mappings = mapper.map_fields(fields, profile, job.ats,
                                     store=store, provider=provider)
        outcome = self.fill(job, fields, mappings, screenshot_dir)
        if not fields and self.needs_auth():
            outcome.needs_auth = True
        return outcome

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

        for m in mappings:
            f = by_id.get(m.field_id)
            if f is not None and f.id in outcome.filled_ids:
                self.form_frame_url = f.frame_url
                break

        outcome.saw_captcha = self.saw_captcha()
        outcome.needs_auth = self.needs_auth()
        outcome.filled_ok = not outcome.missing_required
        outcome.screenshot_path = self.screenshot(job, screenshot_dir)
        return outcome

    def _write(self, f: Field, value: str) -> Optional[str]:
        """Write one field. Returns the value actually written, or None."""
        ctx = self.frame_for(f)
        el = ctx.query_selector(f.selector)
        if el is None:
            return None
        if f.kind == "file":
            path = os.path.expanduser(value)
            if not os.path.exists(path):
                # A mislabelled upload zone can be mapped to a name or an email
                # by a rule matching its label. Uploading is not possible and
                # the run should not die over it -- leave it for the gate.
                return None
            el.set_input_files(path)
            return value
        if f.kind == "select":
            try:
                el.select_option(label=value)
            except Exception:
                el.select_option(value=value)
            return value
        if f.kind in ("checkbox", "radio"):
            return self._write_choice(f, el, value)
        if f.kind == "combobox":
            return self._write_combobox(el, value, ctx)
        ctx.evaluate(SET_VALUE_JS, [el, value])
        return value

    def _write_combobox(self, el, value: str, ctx=None) -> Optional[str]:
        """Open the listbox, filter to the value, take the best real option.

        The options only exist once it is open, so they cannot be read at
        discovery time; and the listbox has to be scoped to this widget --
        several are usually mounted at once (an international phone input keeps
        a 200-entry country list in the DOM permanently), so an unscoped
        [role=option] sweep picks up somebody else's menu.
        """
        from ..mapper import resolve_option

        el.click()
        self.page.wait_for_timeout(250)
        try:
            el.type(value, delay=20)
        except Exception:
            self.page.keyboard.type(value, delay=20)
        self.page.wait_for_timeout(600)

        options = self._combobox_options(el, ctx)

        if options:
            chosen = resolve_option(value, [text for _, text in options])
            if chosen is None:
                # No real option means this answer. Taking the first one would
                # put a fabricated answer on the form under the candidate's
                # name, so leave it unset and let the gate block on it.
                self.page.keyboard.press("Escape")
                return None
            for opt, text in options:
                if text == chosen:
                    opt.click()
                    self.page.wait_for_timeout(250)
                    return text
        self.page.keyboard.press("Escape")
        return None

    def _write_choice(self, f: Field, el, value: str) -> Optional[str]:
        """Tick the member of a radio/checkbox group whose label is the answer.

        A lone checkbox has no options and is a plain yes/no consent tick; a
        group has to match the answer against the members' own labels, because
        checking whichever one happens to be first answers a different question
        than the one that was asked.
        """
        from ..mapper import resolve_option

        if not f.options:
            if str(value).strip().lower() in ("yes", "true", "1", "on"):
                el.check()
                return value
            return None

        ctx = self.frame_for(f)
        members = ctx.query_selector_all(f.selector)
        labelled = []
        for m in members:
            text = (ctx.evaluate(LABEL_JS, m) or "").strip()
            if text:
                labelled.append((m, text))
        if not labelled:
            return None

        chosen = resolve_option(value, [text for _, text in labelled])
        if chosen is None:
            return None
        for m, text in labelled:
            if text == chosen:
                m.check()
                return text
        return None

    def _probe_options(self, el, frame=None) -> list[str]:
        """Open a combobox just long enough to read its choices, then close it."""
        try:
            el.click()
            self.page.wait_for_timeout(350)
            texts = [text for _, text in self._combobox_options(el, frame)]
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(120)
            return texts
        except Exception:
            return []

    # Walk up from the input to the nearest ancestor that actually contains
    # options. Scoping by ancestry rather than a page-wide [role=option] sweep
    # is what keeps one widget from reading another's menu -- an international
    # phone input keeps a 200-entry country listbox mounted at all times, and an
    # unscoped query hands you Afghanistan for every question on the page.
    OPTION_SCOPE_JS = r"""
    (el) => {
      let n = el, hops = 0;
      while (n && hops++ < 8) {
        if (n.querySelector && n.querySelector('[role=option]')) return n;
        n = n.parentElement;
      }
      return null;
    }
    """

    def _combobox_options(self, el, frame=None) -> list:
        """Visible [role=option] belonging to this combobox, nearest scope first."""
        ctx = frame if frame is not None else self.page
        options: list = []
        scope = None
        try:
            handle = ctx.evaluate_handle(self.OPTION_SCOPE_JS, el)
            scope = handle.as_element()
        except Exception:
            scope = None

        candidates = []
        if scope is not None:
            candidates.append(scope.query_selector_all("[role=option]"))
        # A menu rendered into a portal is not an ancestor of the input, so fall
        # back to the listbox this input names, then to any open one.
        owns = None
        try:
            owns = el.get_attribute("aria-controls") or el.get_attribute("aria-owns")
        except Exception:
            pass
        if owns:
            candidates.append(ctx.query_selector_all(f'[id="{owns}"] [role=option]'))
        candidates.append(ctx.query_selector_all("[role=listbox] [role=option]"))

        for group in candidates:
            for opt in group:
                try:
                    if not opt.is_visible():
                        continue
                except Exception:
                    continue
                text = (opt.inner_text() or "").strip()
                if text:
                    options.append((opt, text))
            if options:
                break
        return options

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
        """Click submit, in the frame the form is in, and confirm it landed."""
        scopes = []
        if self.form_frame_url:
            for fr in self.page.frames:
                if fr.url == self.form_frame_url:
                    scopes.append(fr)
                    break
        scopes.append(self.page)

        btn = None
        for scope in scopes:
            btn = query_first(scope, (self.submit_selector,))
            if btn is not None:
                break
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
    """The DOM worker for an ATS, or the generic one when none is dedicated."""
    from .generic import GenericWorker
    from .greenhouse import GreenhouseWorker
    from .lever import LeverWorker
    from .workday import WorkdayWorker

    registry = {
        "greenhouse": GreenhouseWorker,
        "lever": LeverWorker,
        "workday": WorkdayWorker,
    }
    # Anything without a dedicated worker still gets a deterministic first pass
    # rather than going straight to the agent; empty discovery falls through to
    # the agent lane exactly as before.
    return registry.get(ats, GenericWorker)(page)


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
        from ..repair import audit_step, repair_step
        from ..verify import verify_fields

        log = _log.get(f"wizard.{self.ats}")
        self.open(job)
        outcome = FillOutcome(job=job)
        all_ok = True
        seen_steps: list[str] = []

        for step_no in range(1, self.max_steps + 1):
            if self.saw_captcha():
                log.info("step %d: captcha present, stopping", step_no)
                outcome.saw_captcha = True
                break
            if self.needs_auth():
                log.info("step %d: sign-in wall at %s", step_no, self.page.url)
                ok, detail = self.try_sign_in(job)
                log.info("step %d: sign-in -> %s (%s)", step_no, ok, detail)
                outcome.errors.append(f"sign-in: {detail}")
                if not ok:
                    outcome.needs_auth = True
                    break
                # Signed in: the wizard is on the other side of that wall, so
                # re-read the page rather than counting the step as done.
                continue

            fields = self.discover()
            # A wizard that cannot satisfy a step re-renders the same one, and
            # without this the run spends every remaining iteration rediscovering
            # it -- 13 passes over one page reported as 169 fields.
            fingerprint = "|".join(sorted(f.id for f in fields))
            repeats = seen_steps.count(fingerprint)
            seen_steps.append(fingerprint)
            log.info("step %d: %s -- %d field(s)%s", step_no,
                     _log.brief(self.page.url, 80), len(fields),
                     f" (seen {repeats + 1}x)" if repeats else "")
            if repeats >= 2:
                log.warning("step %d: same step %d times, stopping rather than "
                            "looping", step_no, repeats + 1)
                outcome.errors.append(
                    f"stuck on the same step after {repeats + 1} attempts")
                break

            mappings = mapper.map_fields(fields, profile, job.ats,
                                         store=store, provider=provider)
            for m in mappings:
                log.debug("  map %-34s %-9s %-12s %s",
                          _log.brief(m.label or m.field_id, 34), m.action,
                          m.source or "-", _log.brief(m.value, 40))

            step = self.fill(job, fields, mappings, screenshot_dir="")
            log.info("step %d: filled %d/%d", step_no, len(step.filled_ids),
                     len([m for m in mappings if m.action in ("fill", "generate")]))
            for err in step.errors:
                log.debug("  fill error: %s", _log.brief(err, 120))
            outcome.fields.extend(fields)
            outcome.mappings.extend(mappings)
            outcome.filled_ids.extend(step.filled_ids)
            outcome.errors.extend(step.errors)

            # Tier two: the script filled from rules, so audit what it produced
            # before the form ever sees it. The form will accept a phone
            # extension containing the whole phone number -- it is a valid
            # string -- so only a reader who knows what the question meant
            # catches that one. Corrections are taught, so the deterministic
            # pass gets it right next time and this tier stops being consulted
            # for that field.
            audited, audit_notes = audit_step(
                self, fields, mappings, profile, provider=provider,
                store=store, ats=job.ats)
            if audit_notes:
                log.info("step %d: audit corrected %d", step_no, audited)
                for note in audit_notes:
                    log.debug("  audit: %s", _log.brief(note, 120))
                outcome.errors.extend(audit_notes)

            ok, detail = verify_fields(self.page, fields, mappings,
                                       outcome.filled_ids)
            for fid, d in detail.items():
                if isinstance(d, dict) and d.get("ok") is False:
                    log.debug("  verify FAIL %-28s want=%s got=%s",
                              _log.brief(d.get("label", fid), 28),
                              _log.brief(d.get("expected"), 30),
                              _log.brief(d.get("actual"), 30))
            outcome.verify_detail.update(detail)
            all_ok = all_ok and ok

            # A wizard validates on Save and Continue: if it rejects the step it
            # re-renders it with the offending fields marked, and we are still
            # standing on the same step. That rejection names exactly what is
            # wrong, which is a better critic than anything we could ask, so
            # repair from it and try to move on again. Whatever works is written
            # to the corrections store, so the next application answers it right
            # the first time instead of relearning it here.
            if not self.at_review() and self.advance():
                repaired, notes = repair_step(
                    self, fields, mappings, profile, provider=provider,
                    store=store, ats=job.ats)
                if notes:
                    log.info("step %d: form rejected it, repaired %d",
                             step_no, repaired)
                    for note in notes:
                        log.debug("  repair: %s", _log.brief(note, 120))
                outcome.errors.extend(notes)
                if repaired:
                    ok2, detail2 = verify_fields(self.page, fields, mappings,
                                                 outcome.filled_ids)
                    outcome.verify_detail.update(detail2)
                    all_ok = all_ok and ok2
                    self.advance()
                continue

            if self.at_review() or not self.advance():
                break

        outcome.saw_captcha = outcome.saw_captcha or self.saw_captcha()
        outcome.needs_auth = outcome.needs_auth or self.needs_auth()
        # bool(outcome.fields) matters: a wizard that breaks out on step one
        # (auth wall, CAPTCHA) has discovered nothing, so all_ok is still True
        # and missing_required is still empty -- "verified" would be true of a
        # form we never even read.
        outcome.verified = (all_ok and not outcome.missing_required
                            and bool(outcome.fields))
        outcome.filled_ok = not outcome.missing_required
        outcome.screenshot_path = self.screenshot(job, screenshot_dir)
        return outcome
