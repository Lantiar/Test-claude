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
)
CREATE_SWITCH_SELECTORS = (
    "[data-automation-id='createAccountLink']",
    "a:has-text('Create Account')", "button:has-text('Create Account')",
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


def _click(el) -> bool:
    """Click a control that something else may be painted on top of.

    Workday renders its real <button> and then covers it with
    div[data-automation-id="click_filter"][role=button], which carries the same
    aria-label and the actual handler. A plain click on the button underneath
    never lands -- Playwright waits for it to stop being obscured and times out
    after 30s, which is what both the sign-in and create-account clicks were
    doing. So: scroll it into view, and if something stands on top of it that
    represents the same control, click that instead.
    """
    try:
        el.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    try:
        handle = el.evaluate_handle(
            """el => {
                 const r = el.getBoundingClientRect();
                 const top = document.elementFromPoint(r.left + r.width / 2,
                                                       r.top + r.height / 2);
                 if (!top || top === el || el.contains(top)) return null;
                 const norm = n => ((n.getAttribute('aria-label') || n.innerText || '')
                                    .trim().toLowerCase());
                 const a = norm(top), b = norm(el);
                 if (!a || !b) return null;
                 return (a === b || a.includes(b) || b.includes(a)) ? top : null;
               }""")
        proxy = handle.as_element()
        if proxy is not None:
            proxy.click(timeout=8000)
            return True
    except Exception:
        pass

    try:
        el.click(timeout=8000)
        return True
    except Exception:
        pass

    # Last resort: dispatch the click directly, which ignores pointer-event
    # interception entirely.
    try:
        el.evaluate("e => e.click()")
        return True
    except Exception:
        return False


def _on_registration_form(frames) -> bool:
    """Is this the Create Account form rather than the sign-in one?

    Keyed on what only registration has -- a confirm-password field, or a
    create-account submit button. A password field is not a signal: both forms
    have one, and the create form has two.
    """
    for fr in frames:
        if _first(fr, VERIFY_PASSWORD_SELECTORS) is not None:
            return True
        if _first(fr, CREATE_SUBMIT_SELECTORS, allow_account_creation=True) is not None:
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

    if worker.needs_auth():
        return False, "still on a sign-in page after submitting"
    return True, "signed in"


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

    # The terms checkbox, where there is one -- the form will not submit without it.
    for fr in worker.frames():
        try:
            for box in fr.query_selector_all("input[type=checkbox]"):
                if box.is_visible() and not box.is_checked():
                    box.check()
        except Exception:
            continue

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
