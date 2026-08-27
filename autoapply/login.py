"""Sign in to an ATS, including the emailed one-time code.

Every ATS builds this page from the same parts -- an email field, a password
field, a submit button, and sometimes a second page asking for a code that was
just mailed -- so this is written against those parts rather than per tenant.
Three shapes it has to survive:

  * both fields on one page (Workday, most Greenhouse-hosted sign-ins)
  * email first, password on the next page (iCIMS)
  * either of those, then a one-time code (Workday when it does not recognise
    the device)

Registration is a separate, deliberate path. sign_in() will never click a
Create Account control -- Workday's generic clickable id is the create button
on that page, so a submit-button search finds it by accident, and registering
on an employer's system under someone's name is not a thing to do by accident.
create_account() does it on purpose, only when the account list opts in with
allow_account_creation.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

from .clicking import click as _click
from .workers.base import LABEL_JS, _TRAP

ACCOUNTS_PATH = os.getenv("ACCOUNTS_PATH", "config/accounts.json")

EMAIL_SELECTORS = (
    "input[data-automation-id='email']",
    "input[type=email]",
    "input[name*='email' i]", "input[id*='email' i]",
    "input[name*='user' i]", "input[id*='username' i]",
)
PASSWORD_SELECTORS = (
    "input[data-automation-id='password']",
    "input[type=password]",
    "input[name*='password' i]", "input[id*='password' i]",
)
SUBMIT_SELECTORS = (
    "button[data-automation-id='signInSubmitButton']",
    "button[type=submit]", "input[type=submit]",
    "button:has-text('Sign In')", "button:has-text('Sign in')",
    "button:has-text('Log In')", "button:has-text('Log in')",
    "button:has-text('Continue')", "button:has-text('Next')",
    "a:has-text('Sign In')",
)
# The one-time code box. Deliberately not input[type=text] in general -- that
# would match the email field again and retype the address into it.
CODE_SELECTORS = (
    "input[data-automation-id='verificationCode']",
    "input[name*='code' i]", "input[id*='code' i]",
    "input[autocomplete='one-time-code']",
    "input[name*='otp' i]", "input[id*='otp' i]",
)
CODE_PROMPTS = ("verification code", "security code", "one-time", "we sent",
                "check your email", "enter the code", "passcode")

# Workday and iCIMS open on Create Account, with Sign In as a link beside it.
SIGNIN_SWITCH_SELECTORS = (
    "[data-automation-id='signInLink']",
    "button:has-text('Sign In')", "a:has-text('Sign In')",
    "button:has-text('Sign in')", "a:has-text('Sign in')",
    "a:has-text('Already have an account')",
)

# Never click these. Workday's generic clickable is data-automation-id=
# "click_filter", and on the Create Account page that generic id IS the create
# button -- a submit-button list matching it starts registering an account on an
# employer's system under the candidate's name. Signing in is recoverable;
# that is not, and it is not a run's decision to make.
# Boxes a form genuinely will not proceed without: the terms, the privacy
# notice, the age attestation. Deliberately narrow -- anything about updates,
# offers, partners, newsletters or sharing a profile is somebody's marketing
# and is not ours to accept on the candidate's behalf.
# "disclaimer" earns its place from BNY, whose required agreement is a hidden
# native checkbox with no readable label and the id "legal-disclaimer-checkbox".
# Skipping it left the door shut and the run reporting that the page was not an
# application -- true, and not the reason.
_REQUIRED_CONSENT = re.compile(
    r"\b(terms|conditions|privacy|policy|agree|acknowledg|consent to the|"
    r"disclaimer|read and understand|certify|i am at least|18 years)\b", re.I)

FORBIDDEN_CLICK_TEXT = ("create account", "create an account", "sign up",
                        "signup", "register", "join now", "new user")


def _is_account_creation(el) -> bool:
    """Would clicking this register a new account?"""
    try:
        parts = " ".join(filter(None, [
            el.inner_text() or "",
            el.get_attribute("aria-label") or "",
            el.get_attribute("data-automation-id") or "",
            el.get_attribute("id") or "",
        ])).lower()
    except Exception:
        return True                 # cannot tell -> treat as unsafe
    return any(bad in parts for bad in FORBIDDEN_CLICK_TEXT)


VERIFY_PASSWORD_SELECTORS = (
    "input[data-automation-id='verifyPassword']",
    "input[name*='verify' i]", "input[id*='verify' i]",
    "input[name*='confirm' i]", "input[id*='confirm' i]",
)
CREATE_SUBMIT_SELECTORS = (
    "[data-automation-id='createAccountSubmitButton']",
    "button:has-text('Create Account')",
    "div[role=button][aria-label='Create Account']",
    # Not every registration button says "account". TikTok's create-account
    # page offers an email box, a password box and a button reading just
    # "Create", so the run filled the form and then reported "no
    # create-account button" on a page consisting of little else.
    # TikTok: <button class="atsx-btn signUp-submit"><span>Create</span></button>
    "button.signUp-submit", "button:has(span:text-is('Create'))",
    "button:text-is('Create')", "button:text-is('Sign Up')",
    "button:text-is('Sign up')", "button:text-is('Register')",
    "button:text-is('Join')", "button:text-is('Continue')",
)
SIGNIN_SUBMIT_SELECTORS = (
    "button[data-automation-id='signInSubmitButton']",
    "button:has-text('Sign In')", "button:has-text('Sign in')",
)
CREATE_SWITCH_SELECTORS = (
    "[data-automation-id='createAccountLink']",
    "a:has-text('Create Account')", "button:has-text('Create Account')",
    # TikTok renders it as a plain link beside the sign-in form.
    "a:has-text('Create account')", "a:text-is('Sign up')",
    "a:has-text('Need an email account')",
)
# "already registered" rather than "wrong password" -- the two are worth telling
# apart, because only one of them means registering would help.
EXISTS_MARKERS = ("already exists", "already registered", "already in use",
                  "account with this email", "email is taken")


class LoginUnavailable(RuntimeError):
    """No usable credentials, or the sign-in did not go through."""


def load_accounts(path: str | None = None) -> dict:
    path = path or ACCOUNTS_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def credentials_for(url: str, accounts: dict | None = None) -> dict | None:
    """The most specific entry whose key matches this URL's host."""
    accounts = load_accounts() if accounts is None else accounts
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    best, best_len = None, -1
    for key, value in accounts.items():
        if key == "default":
            continue
        k = key.lower()
        if (host == k or host.endswith("." + k) or k in host) and len(k) > best_len:
            best, best_len = value, len(k)
    return best or accounts.get("default")


def _first(scope, selectors, allow_account_creation: bool = False):
    for sel in selectors:
        try:
            el = scope.query_selector(sel)
            if el is None or not el.is_visible() or not el.is_enabled():
                continue
            if not allow_account_creation and _is_account_creation(el):
                continue
            return el
        except Exception:
            continue
    return None


def _on_registration_form(frames) -> bool:
    """Is this the Create Account form rather than the sign-in one?

    Keyed on what only registration has -- a confirm-password field, or a
    create-account submit button. A password field is not a signal: both forms
    have one, and the create form has two.
    """
    for fr in frames:
        # The confirm-password field is the one control only registration has.
        if _first(fr, VERIFY_PASSWORD_SELECTORS) is not None:
            return True
    for fr in frames:
        # A create-account button counts only when nothing offers to sign you
        # in beside it. The sign-in form keeps a Create Account link for people
        # without an account, and matching that read every sign-in page as a
        # registration page -- which left the run insisting it was still on the
        # create form after it had successfully switched away from it.
        if _first(fr, CREATE_SUBMIT_SELECTORS, allow_account_creation=True) is None:
            continue
        if _first(fr, SIGNIN_SUBMIT_SELECTORS) is None:
            return True
    return False


def _find_switch(frames):
    """The Sign In control on a page that opened on Create Account."""
    for fr in frames:
        for sel in SIGNIN_SWITCH_SELECTORS:
            try:
                el = fr.query_selector(sel)
                if el is None or not el.is_visible():
                    continue
                if _is_account_creation(el):
                    continue
                return el, fr
            except Exception:
                continue
    return None, None


def _find(frames, selectors):
    for fr in frames:
        if (el := _first(fr, selectors)) is not None:
            return el, fr
    return None, None


def _asks_for_code(frames) -> bool:
    for fr in frames:
        try:
            text = (fr.inner_text("body") or "").lower()
        except Exception:
            continue
        if any(p in text for p in CODE_PROMPTS):
            return True
    return False


def _set(frame, el, value: str) -> None:
    """Type rather than assign: these forms are React-controlled too."""
    try:
        el.click()
        el.fill("")
        el.type(value, delay=25)
    except Exception:
        frame.evaluate(
            """([e, v]) => {
                 const p = window.HTMLInputElement.prototype;
                 Object.getOwnPropertyDescriptor(p, 'value').set.call(e, v);
                 e.dispatchEvent(new Event('input',  {bubbles: true}));
                 e.dispatchEvent(new Event('change', {bubbles: true}));
               }""", [el, value])


def _code_boxes(frame, first) -> list:
    """The one-box-per-digit inputs a split code field is made of, in order.

    Keyed on the shape they share: same size, same parent, all short. Anything
    that does not look like a set of single-character boxes returns just the
    one box that was found, and the caller types the whole code into it.
    """
    try:
        return frame.query_selector_all(frame.evaluate(
            """e => {
                 const p = e.parentElement;
                 if (!p) return '';
                 const kin = [...p.querySelectorAll('input')].filter(
                   x => x.type !== 'hidden' && (x.maxLength === 1 ||
                        (x.id && /(^|[-_])(pin|code)[-_]?\\d+$/i.test(x.id))));
                 if (kin.length < 2 || !kin.includes(e)) return '';
                 p.setAttribute('data-autoapply-code', '1');
                 return '[data-autoapply-code] input:not([type=hidden])';
               }""", first)) if frame else [first]
    except Exception:
        return [first]


def _clear_code(worker, creds: dict, wait_for_code, say) -> tuple[bool, str]:
    """Fill the emailed one-time code, if the page is asking for one."""
    page = worker.page
    code_el, code_frame = _find(worker.frames(), CODE_SELECTORS)
    if code_el is None and not _asks_for_code(worker.frames()):
        return True, "no code requested"
    if wait_for_code is None:
        return False, "a one-time code was requested and no mailbox is configured"
    if code_el is None:
        return False, "a one-time code was requested but its field was not found"

    needles = creds.get("code_filter") or ["verification"]
    say(f"waiting for a code matching {needles}")
    code = wait_for_code(needles)
    if not code:
        return False, "no verification code arrived in time"
    # One box per digit, on the sites that do it that way. BNY's Oracle door
    # asks for a six-digit PIN as pin-code-1 .. pin-code-6, and typing the
    # whole code into the first one leaves five empty boxes and one digit.
    boxes = _code_boxes(code_frame, code_el)
    if len(boxes) > 1 and len(boxes) == len(code):
        for box, digit in zip(boxes, code):
            _set(code_frame, box, digit)
        say(f"filled the code across {len(boxes)} boxes")
    else:
        _set(code_frame, code_el, code)
        say("filled the code")
    btn, _ = _find(worker.frames(), SUBMIT_SELECTORS)
    if btn is not None:
        _click(btn)
        try:
            page.wait_for_load_state("networkidle", timeout=25000)
        except Exception:
            pass
        page.wait_for_timeout(2500)
    return True, "code accepted"


def _first_in(frames, selectors, allow_account_creation: bool = False):
    for fr in frames:
        el = _first(fr, selectors, allow_account_creation=allow_account_creation)
        if el is not None:
            return el, fr
    return None, None


def sign_in(worker, creds: dict, wait_for_code=None, log=None) -> tuple[bool, str]:
    """Drive the sign-in. Returns (signed_in, detail).

    `worker` supplies the page and its frames; `wait_for_code` is injected so
    the mail path can be swapped or left out entirely.
    """
    page = worker.page
    say = log or (lambda _m: None)
    email = (creds or {}).get("email")
    password = (creds or {}).get("password")
    if not email or not password:
        raise LoginUnavailable("no credentials for this host")

    # --- get onto the Sign In form, not the Create Account one beside it ---
    # Detect registration by what is unique to it. Asking "is there a password
    # field?" does not work: the Create Account form has two, so that test says
    # "already on the sign-in form", the run fills the registration form
    # believing it is signing in, and then finds no button it is allowed to
    # press -- the only one there is Create Account, which the guard rightly
    # refuses. Mastercard's tenant opens on exactly that page.
    if _on_registration_form(worker.frames()):
        switch, _ = _find_switch(worker.frames())
        if switch is None:
            return False, "on a create-account form with no sign-in link"
        if not _click(switch):
            return False, "could not click the sign-in link"
        page.wait_for_timeout(3500)
        say("switched from create-account to the sign-in form")
        if _on_registration_form(worker.frames()):
            return False, "still on the create-account form after clicking sign-in"

    # --- email, then password, which may be on this page or the next ---
    el, frame = _find(worker.frames(), EMAIL_SELECTORS)
    if el is None:
        return False, "no email field on the sign-in page"
    _set(frame, el, email)
    say("filled email")

    pw, pw_frame = _find(worker.frames(), PASSWORD_SELECTORS)
    if pw is None:
        # Email-first flow (iCIMS): advance, then look again.
        btn, _ = _find(worker.frames(), SUBMIT_SELECTORS)
        if btn is None:
            return False, "no password field and nothing to advance with"
        _click(btn)
        page.wait_for_timeout(3500)
        pw, pw_frame = _find(worker.frames(), PASSWORD_SELECTORS)
        if pw is None:
            return False, "password field never appeared"
    _set(pw_frame, pw, password)
    say("filled password")
    _accept_required_consent(worker, say)

    btn, _ = _find(worker.frames(), SUBMIT_SELECTORS)
    if btn is None:
        return False, "no submit button on the sign-in page"
    if not _click(btn):
        return False, "could not click the sign-in button"
    try:
        page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        pass
    page.wait_for_timeout(2500)

    # --- the emailed code, if this device is not recognised ---
    ok, detail = _clear_code(worker, creds, wait_for_code, say)
    if not ok:
        return False, detail

    # Wait for the wall to go, rather than deciding on one early reading.
    #
    # This asked once, about 2.5 seconds after the click, and Workday's SPA had
    # not swapped the sign-in form out yet -- so a sign-in that had in fact
    # worked was reported as "still on a sign-in page after submitting". The
    # run then went off to register an account it already had, found the
    # create-account page equally unrendered, and gave up on the whole
    # application. The screenshot it saved shows the candidate signed in, on
    # Workday's My Information step, with the step rail all the way to Review.
    #
    # networkidle does not settle this: a Workday page keeps connections open,
    # so the wait returns immediately and the fixed pause after it is the only
    # thing standing between the click and the verdict.
    for _ in range(int(os.getenv("SIGNIN_SETTLE_SECONDS", "20"))):
        if not worker.needs_auth():
            return True, "signed in"
        page.wait_for_timeout(1000)
    return False, "still on a sign-in page after submitting"



def _tick_through_label(frame, box) -> bool:
    """Tick a styled checkbox the way a person does: by its label.

    A styled control is a hidden native input with a label painted over it, so
    check() -- which clicks -- fails with "Element is outside of the viewport"
    however much force it is given. The site's own handler is listening on the
    label, so click that. Setting the property is the fallback, and it is only
    worth anything because these controls read the property back.
    """
    try:
        if frame.evaluate(
                """e => {
                     const l = e.closest('label') ||
                       (e.id && document.querySelector(
                          'label[for="' + CSS.escape(e.id) + '"]'));
                     if (!l) return false;
                     l.click();
                     return e.checked;
                   }""", box):
            return True
    except Exception:
        pass
    try:
        return bool(frame.evaluate(
            """e => {
                 e.checked = true;
                 e.dispatchEvent(new Event('input',  {bubbles: true}));
                 e.dispatchEvent(new Event('change', {bubbles: true}));
                 return e.checked;
               }""", box))
    except Exception:
        return False


def _accept_required_consent(worker, say) -> None:
    """Tick the agreement a sign-in or registration form will not proceed without.

    TikTok's sign-in carries "I have read and agree to the User Agreement and
    Applicant Privacy Policy" beside the password box, and submitting without
    it leaves you on the same page -- which the run reported as "still on a
    sign-in page after submitting", a true statement about the wrong cause.

    Same narrow test as registration uses: what the form requires, never what
    it would merely like. A marketing opt-in is not ours to accept.
    """
    for fr in worker.frames():
        try:
            boxes = fr.query_selector_all("input[type=checkbox]")
        except Exception:
            continue
        # A lone unchecked checkbox on a form whose own text is an agreement is
        # that agreement. TikTok's carries no label, no aria-label and no
        # required attribute -- LABEL_JS returns "" and so does its parent --
        # while the words "I have read and agree to the User Agreement and
        # Applicant Privacy Policy" sit elsewhere in the form. Judging it by
        # its own label was therefore never going to work, and submitting
        # without it silently leaves you on the sign-in page.
        unlabelled_consent = False
        try:
            visible = [b for b in boxes if b.is_visible() and not b.is_checked()]
            if len(visible) == 1:
                form_text = (fr.inner_text("body") or "").lower()[:4000]
                unlabelled_consent = bool(_REQUIRED_CONSENT.search(form_text))
        except Exception:
            visible = []

        for box in boxes:
            try:
                if box.is_checked():
                    continue
                label = (fr.evaluate(LABEL_JS, box) or "").strip().lower()
                required = (box.get_attribute("required") is not None
                            or box.get_attribute("aria-required") == "true")
                if not box.is_visible():
                    # Hidden is usually a reason to leave a box alone -- that is
                    # how honeypots are built. But a styled checkbox is a hidden
                    # native input with a label painted over it, and BNY's
                    # agreement is exactly that: input-row__hidden-control,
                    # required, id "legal-disclaimer-checkbox". Skipping it
                    # meant the door never opened, and the run reported the
                    # page was not an application -- true, and not the reason.
                    #
                    # So a hidden box may be ticked only when it is required
                    # and says what it is: an agreement, by its label or its
                    # own id. A trap is neither.
                    identity = f"{label} {box.get_attribute('id') or ''} " \
                               f"{box.get_attribute('name') or ''}"
                    if not (required and _REQUIRED_CONSENT.search(identity)):
                        continue
                    if _TRAP.search(identity):
                        say(f"leaving a honeypot alone: {identity.strip()[:60]}")
                        continue
                    # check() clicks, and a zero-size positioned input is not
                    # clickable even with force -- "Element is outside of the
                    # viewport". What a person clicks is the label painted over
                    # it, and the site's own handler is listening there, so
                    # click that; fall back to setting the property and saying
                    # so, which is what a styled control ultimately reads.
                    if not _tick_through_label(fr, box):
                        continue
                    say(f"agreed to: {label[:60] or identity.strip()[:60]}")
                    continue
                if required or _REQUIRED_CONSENT.search(label) or (
                        unlabelled_consent and not label):
                    box.check()
                    say(f"agreed to: {label[:60] or '(the form-level agreement)'}")
            except Exception as exc:
                # Was `continue`. LABEL_JS was never imported here, so both
                # consent ticks raised NameError into this handler and did
                # nothing at all -- silently, for every run since they were
                # written. A checkbox that cannot be read is worth a line.
                say(f"could not read a checkbox: {type(exc).__name__}: {exc}")

def create_account(worker, creds: dict, wait_for_code=None,
                   log=None) -> tuple[bool, str]:
    """Register an account, then clear the emailed verification if there is one.

    Only reached when the account list sets allow_account_creation for the host.
    Uses the same address and password as sign-in, so a second run signs in to
    what this created rather than registering again.
    """
    page = worker.page
    say = log or (lambda _m: None)
    email = (creds or {}).get("email")
    password = (creds or {}).get("password")
    if not email or not password:
        raise LoginUnavailable("no credentials for this host")

    # Get onto the create form; the page may be showing sign-in.
    if _first_in(worker.frames(), CREATE_SUBMIT_SELECTORS,
                 allow_account_creation=True)[0] is None:
        switch, _ = _first_in(worker.frames(), CREATE_SWITCH_SELECTORS,
                              allow_account_creation=True)
        if switch is not None:
            _click(switch)
            page.wait_for_timeout(2500)
            say("switched to the create-account form")

    el, frame = _find(worker.frames(), EMAIL_SELECTORS)
    if el is None:
        return False, "no email field on the create-account page"
    _set(frame, el, email)

    pw, pw_frame = _find(worker.frames(), PASSWORD_SELECTORS)
    if pw is None:
        return False, "no password field on the create-account page"
    _set(pw_frame, pw, password)

    verify, v_frame = _find(worker.frames(), VERIFY_PASSWORD_SELECTORS)
    if verify is not None:
        _set(v_frame, verify, password)
    say("filled the registration fields")

    # The terms checkbox, where there is one -- the form will not submit
    # without it. ONLY that one: this ticked every visible unchecked box on
    # the page, which on a real create-account form means the marketing
    # opt-in and "share my profile with partner employers" as well, agreed to
    # under the candidate's name with no record of what was agreed to. A
    # required box the form will not submit without is a different thing from
    # a box someone would like you to tick.
    for fr in worker.frames():
        try:
            boxes = fr.query_selector_all("input[type=checkbox]")
        except Exception:
            continue
        for box in boxes:
            try:
                if not box.is_visible() or box.is_checked():
                    continue
                label = (fr.evaluate(LABEL_JS, box) or "").strip().lower()
                required = (box.get_attribute("required") is not None
                            or box.get_attribute("aria-required") == "true")
                if required or _REQUIRED_CONSENT.search(label):
                    box.check()
                    say(f"ticked the required consent: {label[:60] or '(unlabelled)'}")
                elif label:
                    say(f"left unticked: {label[:60]}")
            except Exception as exc:
                say(f"could not read a checkbox: {type(exc).__name__}: {exc}")

    btn, _ = _first_in(worker.frames(), CREATE_SUBMIT_SELECTORS,
                       allow_account_creation=True)
    if btn is None:
        return False, "no create-account button"
    if not _click(btn):
        return False, "could not click the create-account button"
    try:
        page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        pass
    page.wait_for_timeout(3000)

    body = ""
    for fr in worker.frames():
        try:
            body += (fr.inner_text("body") or "").lower()
        except Exception:
            continue
    if any(m in body for m in EXISTS_MARKERS):
        return False, "an account already exists for this address"

    ok, detail = _clear_code(worker, creds, wait_for_code, say)
    if not ok:
        return False, detail
    if worker.needs_auth():
        return False, "still on a sign-in page after creating the account"
    return True, "created an account and signed in"
