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
from ..clicking import click as _click
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
  // The options are whatever shares this control's name -- that is what makes
  // a radio group a group in HTML, with or without a fieldset -- falling back
  // to the ones inside the scope. Lever wraps neither its radios in <label>
  // nor its groups in <fieldset>, so labelOf() found nothing, the subtraction
  // below removed nothing, and every one of its questions discovered as "Yes":
  // three characters, longer than the two the loop asks for, returned as the
  // question. Four required questions per posting, unanswerable, on every
  // Lever board.
  const name = el.getAttribute('name');
  const peers = name
    ? Array.from(document.getElementsByName(name))
    : Array.from(scope.querySelectorAll('input[type="radio"], input[type="checkbox"]'));
  const opts = [];
  for (const peer of peers) {
    const own = labelOf(peer);
    if (own) { opts.push(own); continue; }
    // No label element: the option's text is whatever sits in its own row.
    const row = peer.parentElement;
    const t = row ? clean(row.innerText) : '';
    if (t && t.length < 60) opts.push(t);
  }
  const isAnOption = t =>
    opts.some(o => o && (o === t || (t.length < 40 && o.includes(t))));

  let n = scope, hops = 0;
  while (n && hops++ < 5) {
    let text = clean(n.innerText);
    opts.forEach(o => { text = text.split(o).join(' '); });
    text = clean(text);
    // A question is not one of its own answers. Without this the loop stops on
    // the first container whose text survives subtraction, which on a form
    // whose options it could not read is the option itself.
    if (text.length > 2 && !isAnOption(text)) return text;
    n = n.parentElement;
  }
  return '';
}
"""

# React and similar frameworks track value on the DOM node; assigning .value
# directly is invisible to them, so go through the native setter and fire events.
SET_VALUE_JS = r"""
([el, value]) => {
  // The prototype has to match the element. Calling HTMLInputElement's value
  // setter on anything else throws "Illegal invocation", which is what
  // TikTok's School name and YYYY fields did -- they are not inputs, and
  // discovery had called them text.
  const tag = el.tagName;
  const proto = tag === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype
              : tag === 'SELECT'   ? window.HTMLSelectElement.prototype
              : tag === 'INPUT'    ? window.HTMLInputElement.prototype
              : null;
  if (proto) {
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, value);
  } else if (el.isContentEditable) {
    // A rich-text box: its content is its value.
    el.focus();
    el.textContent = value;
  } else if ('value' in el) {
    el.value = value;            // a custom element with its own accessor
  } else {
    return false;
  }
  el.dispatchEvent(new Event('input',  { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new Event('blur',   { bubbles: true }));
  return true;
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
    # field id -> the click path that filled it. Kept apart from the value
    # because they answer different questions: verification needs what the form
    # now shows ("Handshake"), and teaching needs the route that got there
    # ("Job Board > Handshake"), since the leaf is not on the top-level menu.
    pending_paths: dict[str, str] | None = None
    # Set once a sign-in succeeds: the session is now worth protecting.
    _signed_in: bool = False
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
        self.dismiss_consent()
        self.settle_landing()
        if self._already_on_the_application():
            return
        target = self.start_application(click=False, job_url=job.url)
        if target.startswith("http") and _offsite(job.url, target):
            # Go there, do not click there. AMD's Apply control points at
            # campus-amd.icims.com, which serves the applicant form to a clean
            # visitor -- but clicking it carries the careers site's cookies and
            # tracking parameters along, and iCIMS then routes the session into
            # amdsso.okta.com, an employee SSO app with no application on it.
            # Trying to recover afterwards raced a redirect that fires later
            # than any check for it. Not clicking is simpler and always
            # equivalent: the href is the destination.
            _log.get(f"worker.{self.ats}").info(
                "following the application link directly: %s",
                _log.brief(target, 60))
            try:
                # Only before we hold a session. Clearing was added so AMD's
                # careers cookies could not route the ATS hop into employee
                # SSO; called again after a successful sign-in it throws that
                # sign-in away, which is exactly what happened on TikTok --
                # signed in, landed on /position/application, re-opened, and
                # was back at /login with the session gone.
                if not self._signed_in:
                    self.page.context.clear_cookies()
                self.page.goto(target.split("?")[0], wait_until="domcontentloaded")
            except Exception:
                pass
            self.settle_step(timeout_ms=15000, reloads=0)
            self.dismiss_consent()
            return
        target = self.start_application(job_url=job.url)
        if target:
            # Wait for the application to render, do not guess at it. AMD's
            # click-through lands on an iCIMS page whose form arrives in an
            # iframe after about four seconds: at a fixed 2.5s wait discovery
            # found zero fields and the run reported "no form fields
            # discovered" about a page that was still loading.
            self.settle_step(timeout_ms=15000, reloads=0)
            self.dismiss_consent()
            self._escape_sso(target)

    def settle_landing(self, timeout_ms: int = 12000) -> None:
        """Wait until the landing page shows a form, or a way into one.

        open() looked after a fixed 1500ms. BNY's Oracle posting renders its
        two APPLY NOW buttons later than that, so the run found no fields and
        no apply control, did nothing, and fell through to the agent lane on
        every single attempt -- a link whose form is three clicks away and
        which a probe reaches in four seconds.

        Neither half of that wait can be a fixed number, because the two pages
        this has to serve are opposites: an application is ready when it has
        fields, a posting when it has the button that leads to them. So wait
        for whichever arrives, and stop as soon as it does.
        """
        deadline = time.time() + timeout_ms / 1000
        settled = 0
        last = -1
        while time.time() < deadline:
            try:
                if self.discover():
                    return
                if self.start_application(click=False, job_url=""):
                    return
            except Exception:
                pass
            # A page that has stopped changing has nothing more to give, and
            # waiting out the full window on it is pure cost: the two
            # empty-discovery tests took thirty seconds each, for a page that
            # was never going to render anything. Growth in the DOM is what
            # tells a still-rendering SPA apart from a finished empty page.
            try:
                size = self.page.evaluate("() => document.getElementsByTagName('*').length")
            except Exception:
                size = last
            settled = settled + 1 if size == last else 0
            last = size
            if settled >= 2:
                return
            self.page.wait_for_timeout(600)

    def _already_on_the_application(self) -> bool:
        """Is the page we were handed the form itself?

        Then there is nothing to click through to, and looking for something to
        click is a way to leave. Notion's link is an Ashby /application URL
        that renders the form directly; open() went hunting for an Apply
        control anyway, found the posting's own apply link sitting on the
        application it leads to, followed it, and ended on www.ashbyhq.com --
        where the gate saw a marketing page and refused to fill anything.

        The run read that as a closed posting, because a closed Ashby posting
        does redirect to exactly that page. It was not closed: the posting is
        live and serves the form at the URL we started from. We navigated off
        it ourselves and then diagnosed the wreckage as the site's doing.

        START_JS has a test of its own for this and it misses this case: it
        scans document.querySelectorAll('label, legend'), while Ashby is a
        React app whose captions are divs and whose accessible names live on
        the inputs. That test sees an empty page. Discovery does not, so ask
        discovery.
        """
        from ..gate import looks_like_an_application

        try:
            fields = self.discover()
        except Exception:
            return False
        probe = FillOutcome(job=Job(url=self.page.url, ats=self.ats),
                            fields=fields)
        if not looks_like_an_application(probe):
            return False
        _log.get(f"worker.{self.ats}").info(
            "already on the application (%d field(s)); not looking for a way in",
            len(fields))
        # Hand them on rather than making run() find them again. Discovery
        # opens every combobox to read its options, so on TikTok's 66-field
        # form it is minutes, not milliseconds -- and this check would
        # otherwise have added a second pass to every application reached
        # directly by URL.
        self._landing = (self.page.url, fields)
        return True

    _landing: tuple[str, list] | None = None

    def take_landing_fields(self) -> list:
        """Discovery from open(), if the page has not moved since. One shot."""
        landing, self._landing = self._landing, None
        if not landing:
            return []
        url, fields = landing
        try:
            return fields if self.page.url == url else []
        except Exception:
            return []

    # A corporate identity provider. Landing on one means the Apply control led
    # into an employee login rather than the candidate flow.
    SSO_HOSTS = ("okta.com", "onelogin.com", "pingidentity.com", "ping-eng.com",
                 "microsoftonline.com", "auth0.com", "duosecurity.com")

    def _escape_sso(self, target: str) -> None:
        """Back out of a corporate SSO detour onto the applicant form.

        AMD's careers page carries an Apply link that routes through
        amdsso.okta.com into an employee SSO app, while the plain iCIMS URL --
        campus-amd.icims.com/jobs/<id>/login -- still serves the candidate
        flow, an email box and a privacy acceptance. Following the button
        therefore lands somewhere no applicant can sign in to, and the run
        reports zero fields on a form that exists and is reachable.

        The href the Apply control pointed at is the applicant URL; it is
        arriving there via the click, with its tracking parameters, that
        triggers the redirect. So go there directly instead.
        """
        try:
            landed = (self.page.url or "").lower()
        except Exception:
            return
        if not target.startswith("http"):
            return
        # Two ways to know the click went somewhere useless: it landed on an
        # identity provider, or it produced no form at all. The second is the
        # general test and the one that actually caught AMD -- following its
        # Apply control reaches login.icims.com/authorize for *internal*-amd,
        # which is not an IdP hostname and is still not an application. Fresh,
        # the same URL serves the candidate flow, so it is the click's
        # accumulated state that redirects.
        if not any(host in landed for host in self.SSO_HOSTS):
            try:
                if self.discover():
                    return
            except Exception:
                return
            # Nothing discovered is not by itself a detour. SmartRecruiters'
            # Apply moves to its own oneclick-ui on the same host, which needs
            # a moment to render: this read the empty page as an SSO hop, threw
            # away the cookies and navigated to the URL it was already on, and
            # the run then refused to fill "a page that is not the posting".
            # Going somewhere is what makes a detour; being early is not.
            if not _offsite(landed, target) and \
                    landed.split("?")[0].rstrip("/") == target.split("?")[0].rstrip("/"):
                _log.get(f"worker.{self.ats}").info(
                    "already at the apply URL; waiting for it rather than "
                    "starting over")
                self.settle_landing()
                return
        direct = target.split("?")[0]
        log = _log.get(f"worker.{self.ats}")
        log.info("Apply led to corporate SSO (%s); going straight to %s",
                 _log.brief(landed, 40), _log.brief(direct, 60))
        try:
            # Clear first. The redirect is driven by state the careers site
            # set, so arriving at the same URL in the same session lands in the
            # same place -- AMD sent us to amdsso.okta.com either way. From a
            # clean session that exact URL serves the applicant form. Nothing
            # worth keeping has been established yet: this runs before sign-in.
            self.page.context.clear_cookies()
            self.page.goto(direct, wait_until="domcontentloaded")
            self.settle_step(timeout_ms=15000, reloads=0)
            self.dismiss_consent()
            log.info("reached %s with %d field(s)", _log.brief(self.page.url, 50),
                     len(self.discover()))
        except Exception as exc:
            log.info("could not reach %s: %s", _log.brief(direct, 50), exc)

    # A job posting is not an application, and only the Workday worker knew to
    # click through from one to the other. On BNY's Oracle site the run stayed
    # on the posting and filled the careers site's own furniture -- a "City,
    # state, country" location search and a dropzone reading "Upload or drag
    # and drop your PDF resume file here to get AI recommended jobs", which is
    # a job-recommendation widget and not the application's resume field at
    # all. It then reported five fields discovered and one filled, on a page
    # with no application on it.
    START_JS = r"""
    (wanted) => {
      const clean = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
      // Already looking at an application? Then there is nothing to click
      // through to, and an "Apply" further down the page is a different job.
      //
      // Asking for the candidate's name is the signal. Matching "resume" is
      // not: BNY's posting carries "Upload or drag and drop your PDF resume
      // file here to get AI recommended jobs", so a page whose only resume
      // mention is a recommendation widget reads as an application and the
      // Apply button never gets clicked -- which is the exact bug this is
      // meant to fix.
      const signs = /first name|last name|full name|legal name|cover letter|work authoriz|sponsorship/;
      for (const el of document.querySelectorAll('label, legend')) {
        if (signs.test(clean(el.innerText))) return '';
      }
      // Whole-label match first: "Apply filters" on a search page is not this,
      // and neither is "Apply" inside a sentence.
      const want = /^(apply|apply now|apply for this job|apply to this job|apply manually|apply online|apply for job|start application|start your application|i'?m interested)$/;
      // ...but a real one is often labelled with the job. AMD's is
      // aria-label="Apply : 2027 Masters Software Engineer Intern/Co-op in
      // Multiple Locations", which a whole-label test skips -- so the link
      // straight to the application was passed over for being descriptive,
      // and the run sat on the posting finding no fields at all. Apply
      // followed by a separator is still Apply; "Apply filters" is not.
      const prefixed = /^(apply|start application)\s*[:\u2013\u2014-]|^apply\s+(to|for)\s+(this|the)\b/;
      const isApply = (label) => label && (want.test(label) ||
                                (prefixed.test(label) && !/filter/.test(label)));
      // An ATS host in the href is stronger than any wording: a link off this
      // page and into icims/workday/greenhouse/lever/ashby/oracle is the
      // application by definition, whatever it calls itself.
      const ATS = /(icims|myworkdayjobs|workday|greenhouse|lever\.co|ashbyhq|oraclecloud|taleo|smartrecruiters|jobvite)\./i;

      const nodes = [...document.querySelectorAll(
          'a, button, [role=button], input[type=button], input[type=submit]')];
      const namesThisJob = (href) =>
        (wanted || []).some(id => id && href.toLowerCase().includes(id));
      // This posting's own apply link first, whatever it is called.
      for (const n of nodes) {
        const href = n.getAttribute('href') || '';
        if (!href || !namesThisJob(href)) continue;
        const label = clean(n.getAttribute('aria-label') || n.innerText || n.value);
        if (!/apply|application/i.test(label + ' ' + href)) continue;
        const r = n.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        n.setAttribute('data-autoapply-start', '1');
        return href;
      }
      for (const pass of ['ats', 'label']) {
        for (const n of nodes) {
          const label = clean(n.getAttribute('aria-label') || n.innerText || n.value);
          const href = n.getAttribute('href') || '';
          const hit = pass === 'ats'
            ? (ATS.test(href) && /apply|application/i.test(label + ' ' + href))
            : isApply(label);
          if (!hit) continue;
          const r = n.getBoundingClientRect();
          if (!r.width || !r.height) continue;
          n.setAttribute('data-autoapply-start', '1');
          // The href, when there is one: _escape_sso needs somewhere to go.
          return href || label;
        }
      }
      return '';
    }
    """

    def start_application(self, click: bool = True, job_url: str = "") -> str:
        """Find the way through from a job posting to the application.

        With click=False the control is located and its href returned without
        being activated, so the caller can navigate to it instead.
        """
        # The ids naming this posting, so an Apply link that mentions it wins
        # over one that does not. TikTok's job page carries both: "Apply" ->
        # careers.tiktok.com/position/application, which is the generic form
        # and comes first in the DOM, and "Apply to this job" ->
        # careers.tiktok.com/resume/<job id>/apply, which is this job. Taking
        # the first match landed on a page with two unlabelled dropdowns and
        # no application on it.
        wanted = _posting_ids(job_url)
        for frame in self.frames():
            try:
                label = frame.evaluate(self.START_JS, list(wanted))
            except Exception:
                continue
            if not label:
                continue
            try:
                button = frame.query_selector("[data-autoapply-start]")
                if button is None:
                    continue
                if not click:
                    return label
                if _click(button):
                    _log.get(f"worker.{self.ats}").info(
                        "clicked through to the application (%r)", label)
                    return label
            except Exception:
                continue
        return ""

    # A cookie banner is not a hard problem, it is just always there. AMD's
    # covers the whole iCIMS form with a modal, so discovery found zero fields
    # on a page that had a complete application on it -- the run reported "no
    # form fields discovered" about a form it never saw. Most careers sites in
    # any EU-facing company have one, so this is worth doing once here rather
    # than being rediscovered per ATS.
    CONSENT_JS = r"""
    () => {
      const clean = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
      // Only inside something that is talking about cookies or consent: a bare
      // "Accept" or "I agree" elsewhere on an application page is a terms
      // checkbox or a submit, and clicking those is the opposite of harmless.
      const banners = [];
      document.querySelectorAll(
        '[id*="cookie" i], [class*="cookie" i], [id*="consent" i],' +
        ' [class*="consent" i], [aria-label*="cookie" i], [role="dialog"]'
      ).forEach(n => {
        const t = clean(n.innerText);
        if (t && t.length < 3000 && /cookie|consent|privacy|gdpr/.test(t)) {
          banners.push(n);
        }
      });
      // Accept-shaped, and never a settings/reject/manage control -- those open
      // a second dialog and leave the page just as blocked.
      const yes = /^(accept|accept all|accept cookies|accept all cookies|allow|allow all|allow all cookies|i accept|i agree|agree|ok|got it|continue|understood|close)$/;
      for (const banner of banners) {
        for (const b of banner.querySelectorAll(
                'button, a[role=button], [role=button], input[type=button], input[type=submit]')) {
          const label = clean(b.getAttribute('aria-label') || b.innerText || b.value);
          if (!yes.test(label)) continue;
          const r = b.getBoundingClientRect();
          if (!r.width || !r.height) continue;
          b.setAttribute('data-autoapply-consent', '1');
          return label;
        }
      }
      return '';
    }
    """

    def dismiss_consent(self) -> str:
        """Click a cookie/consent banner away. Returns what was clicked."""
        for frame in self.frames():
            try:
                label = frame.evaluate(self.CONSENT_JS)
            except Exception:
                continue
            if not label:
                continue
            try:
                button = frame.query_selector("[data-autoapply-consent]")
                if button is not None and _click(button):
                    _log.get(f"worker.{self.ats}").info(
                        "dismissed a consent banner (%r)", label)
                    self.page.wait_for_timeout(600)
                    return label
            except Exception:
                continue
        return ""

    # Single-page apps fail to load a step and say so. Workday renders
    # "Something went wrong -- Please refresh the page" while still showing the
    # progress bar and the signed-in header, so the run reads it as a page with
    # no fields and gives up on an application that only needed reloading.
    TRANSIENT_ERROR_MARKERS = ("something went wrong", "please refresh",
                               "try again later", "unexpected error")

    # A site telling us to come back later. Distinct from a transient error --
    # reloading does not clear it, and on a door that counts attempts, another
    # try is what deepens the hole.
    RATE_LIMIT_MARKERS = ("too many attempts", "try again later",
                          "try again in", "maximum number of attempts",
                          "too many requests", "rate limit",
                          "temporarily locked", "please wait a few minutes")

    def rate_limited(self) -> str:
        """What the site said about coming back later, or "".

        BNY's door counts attempts, and six rounds of submitting it earned
        "Too Many Attempts. Try Again Later. You reached the maximum number of
        attempts. Try again in 30 minutes." The run reported that page as "does
        not look like a job application", which is true of what was on screen
        and says nothing about why -- so the next run repeats the attempt, and
        the count goes up again.
        """
        for frame in self.frames():
            try:
                text = (frame.inner_text("body") or "")
            except Exception:
                continue
            low = text.lower()
            for marker in self.RATE_LIMIT_MARKERS:
                if marker in low:
                    at = low.find(marker)
                    return " ".join(text[at:at + 120].split())
        return ""

    def showing_transient_error(self) -> bool:
        for frame in self.frames():
            try:
                text = (frame.inner_text("body") or "").lower()
            except Exception:
                continue
            if any(m in text for m in self.TRANSIENT_ERROR_MARKERS):
                return True
        return False

    def settle_step(self, timeout_ms: int = 30000, reloads: int = 2) -> bool:
        """Wait for a step to finish rendering, reloading if it failed to.

        Used after passing a sign-in wall and whenever a step comes back empty:
        an SPA step that fails to load looks exactly like a step with nothing on
        it, and the run would otherwise conclude the application is over.

        Waits for the field count to hold steady rather than returning on the
        first non-empty read. A half-rendered Workday step reported one field
        where it had thirteen, and the run filled that one and moved on.
        """
        log = _log.get(f"worker.{self.ats}")
        for attempt in range(reloads + 1):
            deadline = time.time() + timeout_ms / 1000
            last = -1
            while time.time() < deadline:
                try:
                    count = len(self.discover())
                except Exception:
                    count = -1
                if count > 0 and count == last:
                    return True
                last = count
                if count <= 0 and self.showing_transient_error():
                    break
                self.page.wait_for_timeout(700)

            if attempt >= reloads:
                break
            log.info("page did not render (%s); reloading",
                     "error shown" if self.showing_transient_error()
                     else "timed out")
            try:
                self.page.reload(wait_until="domcontentloaded")
                self.page.wait_for_timeout(3000)
            except Exception:
                break
        return False

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

    # A challenge is a thing on the screen, not a word in the source.
    #
    # This used to scan the page HTML for "captcha" and friends. Everything
    # that merely talks about captchas matched: AMD's iCIMS page embeds a JSON
    # translation bundle containing
    # "hcaptcha":{"privacy":"privacy","protected":"protected by hcaptcha."} for
    # a widget it never renders, and the run reported CAPTCHA present and
    # refused to proceed on a page with no challenge anywhere on it. Workday's
    # noCaptchaWrapper had already forced one exception to be hard-coded, which
    # should have been the clue that the test itself was wrong rather than the
    # marker list incomplete.
    #
    # Only an interactive challenge stops us, and an interactive challenge has
    # to be visible to be solved. An invisible reCAPTCHA v3 scores silently and
    # asks nothing of anyone, so blocking on one means blocking on a great many
    # forms nobody is being challenged by.
    CAPTCHA_JS = r"""
    () => {
      // Widget containers and challenge iframes. grecaptcha-badge is
      // deliberately absent: the v2 badge sits in the corner of a great many
      // forms and challenges nobody.
      const sel = 'iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i],' +
                  ' iframe[src*="turnstile" i], .g-recaptcha, #g-recaptcha,' +
                  ' .h-captcha, .cf-turnstile';
      // The anchor frame is the widget itself, and on the enterprise/invisible
      // setup that Greenhouse boards run by default it never asks anything: it
      // scores the session in the background and the form submits normally.
      // The challenge -- the "select all the buses" grid -- is a *separate*
      // bframe, and it is the one worth stopping for. Blocking on the anchor
      // stopped Axon's application at 29 of 32 fields filled, and would stop
      // most of the Greenhouse boards on a Summer-2027 list the same way.
      const ANCHOR = /\/(anchor|webworker)\b/i;
      const CHALLENGE = /\/bframe\b/i;
      for (const n of document.querySelectorAll(sel)) {
        if (n.classList.contains('grecaptcha-badge')) continue;
        const src = n.getAttribute('src') || '';
        if (ANCHOR.test(src) && !CHALLENGE.test(src)) {
          // ...unless it is showing a tick box a person has to click, which
          // is an anchor that has been given a real size on screen.
          const r = n.getBoundingClientRect();
          if (r.width < 100 || r.height < 40) continue;
        }
        const cs = getComputedStyle(n);
        // Being hidden on purpose is what separates an invisible v3 -- which
        // scores silently and asks nothing of anyone -- from a challenge. Size
        // is not: a container that has not rendered yet measures zero and will
        // still put a checkbox in front of someone a moment later.
        if (cs.display === 'none' || cs.visibility === 'hidden'
            || cs.opacity === '0') continue;
        return (n.getAttribute('src') || n.className || n.id || 'captcha')
               .toString().slice(0, 80);
      }
      return '';
    }
    """

    def saw_captcha(self) -> bool:
        for frame in self.frames():
            try:
                found = frame.evaluate(self.CAPTCHA_JS)
            except Exception:
                continue
            if found:
                _log.get(f"worker.{self.ats}").info(
                    "captcha challenge on screen: %s", _log.brief(found, 60))
                return True
        return False

    # Selectors that mean "you must sign in or create an account to continue".
    auth_selectors: tuple[str, ...] = ()

    # A sign-in step announces itself in the URL. Worth checking on its own:
    # an email-first login (iCIMS asks for the address before the password)
    # has no password field on the page yet, so selector matching alone reads
    # it as an ordinary form and the run tries to fill it as an application.
    AUTH_URL_MARKERS = ("/login", "/signin", "/sign-in", "/sign_in",
                        "/register", "/createaccount", "/create-account")

    # An ATS serves the application's own first step from a /login URL. AMD's
    # iCIMS tenant says "Please enter your email to begin the application
    # process" at /jobs/91176/login: an email box, a privacy acceptance and a
    # Next button, no password anywhere. Read as a wall, the run tried to sign
    # in, found nothing to sign in with, and stopped on the first page of an
    # application it could have filled -- which it had in fact already filled.
    #
    # BNY's Oracle site does the same thing in different words. Clicking APPLY
    # NOW lands on /apply/email: "You don't need to have an account. Get
    # started right away by simply using your email", an email box, "I agree
    # with the terms and conditions", and NEXT. Not one of the phrases below
    # matched it, so the door was not recognised and the run went no further.
    ENTRY_TEXT = ("begin the application", "start the application",
                  "start your application", "begin your application",
                  "to apply for this", "application process",
                  "need to have an account", "get started right away",
                  "simply using your email")

    def looks_like_an_entry_step(self) -> bool:
        """A /login URL that is really the application's own first page."""
        for frame in self.frames():
            try:
                if frame.query_selector("input[type=password]") is not None:
                    return False
            except Exception:
                continue
        for frame in self.frames():
            try:
                text = (frame.inner_text("body") or "").lower()
            except Exception:
                continue
            if any(phrase in text for phrase in self.ENTRY_TEXT):
                return True
        return False

    def needs_auth(self) -> bool:
        if any(query_first(fr, self.auth_selectors) is not None
               for fr in self.frames()):
            return True
        # A password box means an account, whatever the URL says. TikTok serves
        # its wall from lifeattiktok.com/search/<id> -- no /login, no marker,
        # and the generic worker has no auth_selectors -- so the run read
        # "Enter your email / Set your password" as an ordinary form, never
        # tried to sign in or register, and refused to fill it. Which was the
        # right refusal and the wrong reason: there was an account to make.
        for frame in self.frames():
            try:
                if frame.query_selector("input[type=password]") is not None:
                    return True
            except Exception:
                continue
        try:
            path = (self.page.url or "").lower().split("?")[0]
        except Exception:
            return False
        if not any(marker in path for marker in self.AUTH_URL_MARKERS):
            return False
        return not self.looks_like_an_entry_step()

    def _mail_waiter(self, since: float | None = None):
        """A function that waits for the emailed one-time code, or None.

        Shared, because the door needs it as much as the sign-in does: behind
        BNY's is a six-digit PIN, and pass_entry_step had no way to ask for one.

        `since` is the moment the button that asks for the code was pressed.
        Without it the mailbox's high-water line is drawn when the wait starts,
        which is up to fifteen seconds later -- and BNY's mail lands inside
        that gap, so four "Confirm Your Identity" messages sat unread while the
        waiter timed out having ignored every one as pre-existing.
        """
        try:
            from ..mailcode import MailUnavailable, wait_for_code
        except Exception:
            return None

        def waiter(needles, _w=wait_for_code, _since=since):
            try:
                return _w(needles, timeout=int(
                    os.getenv("MAIL_CODE_TIMEOUT", "180")), since=_since)
            except MailUnavailable:
                return None
        return waiter

    def pass_entry_step(self, job) -> bool:
        """Walk through the application's own first page. True if it moved.

        Recognising the entry step was only half the job. AMD's iCIMS tenant
        serves /jobs/91176/login with an email box, a privacy acceptance and a
        Next button -- no password anywhere -- and needs_auth() correctly says
        that is not a wall. Nothing then walked through it: discovery found
        Email plus a consent tick, looks_like_an_application() correctly said
        two such fields are not an application, and the run stopped on the
        first page of a form it could have filled, reporting "no form fields
        discovered" about an application it never reached.

        The consent here is required and specific -- it is what the form will
        not proceed without -- and is ticked by the same narrow test the
        sign-in path uses, which never touches a marketing opt-in.
        """
        from ..login import (EMAIL_SELECTORS, SUBMIT_SELECTORS,
                             _accept_required_consent, _first_in, _set)

        log = _log.get(f"worker.{self.ats}")
        creds = None
        try:
            from ..login import credentials_for
            creds = credentials_for(job.url)
        except Exception:
            pass
        email = (creds or {}).get("email") or os.getenv("AUTOAPPLY_EMAIL", "")
        if not email:
            log.info("entry step needs an email address and none is configured")
            return False

        el, frame = _first_in(list(self.frames()), EMAIL_SELECTORS)
        if el is None:
            return False
        # BNY's door carries a field literally named "honeypot", there to catch
        # something that fills in every box it finds. _first already skips it,
        # since it is not visible -- but a trap is worth refusing by name as
        # well as by accident, and the cost of tripping one is being taken for
        # a bot on a real person's application.
        if _is_a_trap(el):
            log.info("entry step: refusing to fill a honeypot field")
            return False
        _set(frame, el, email)
        _accept_required_consent(self, lambda msg: log.info("  %s", msg))

        before = self.page.url
        pressed_at = time.time()
        button, _ = _first_in(list(self.frames()), SUBMIT_SELECTORS)
        if button is None:
            log.info("entry step has no submit control")
            return False
        try:
            button.click()
        except Exception as exc:
            log.info("entry step would not submit: %s", exc)
            return False
        self.settle_step(timeout_ms=15000, reloads=0)

        # Behind BNY's door is a six-digit PIN it has just emailed -- six boxes,
        # pin-code-1 through pin-code-6. The sign-in path has waited for a
        # mailed code since it was written; this one had no way to ask for it,
        # so the run reached the form's own verification step and stopped
        # there with "no answer for required: pin-code-1 ... pin-code-5".
        from ..login import _clear_code

        ok, detail = _clear_code(self, creds or {}, self._mail_waiter(pressed_at),
                                 lambda msg: log.info("  %s", msg))
        if not ok:
            log.info("entry step: %s", detail)
            return False
        if detail != "no code requested":
            self.settle_step(timeout_ms=15000, reloads=0)

        # Through means the door is behind us, not that the URL twitched.
        #
        # This asked whether the URL had changed or discovery returned
        # anything, and AMD's answers yes to both while standing still: the
        # click appends ?mobile=false to the same /login, and discovery finds
        # the same Email box it found before. So the run logged "entry step ->
        # through" and then, one line later, refused to fill the page it was
        # still on. A step that reports success while nothing moved is worse
        # than one that fails, because the failure it hides is the diagnosis.
        from ..gate import looks_like_an_application as _is_app

        fields = self.discover()
        if held := self.rate_limited():
            log.warning("entry step: the site is holding us off -- %s",
                        _log.brief(held, 90))
            self._held_off = held
            return False
        through = bool(fields) and (
            _is_app(FillOutcome(job=job, fields=fields))
            or not self.looks_like_an_entry_step())
        log.info("entry step -> %s (%s, %d field(s)%s)",
                 "through" if through else "still on the door",
                 _log.brief(self.page.url, 50), len(fields),
                 "" if self.page.url != before else ", url unchanged")
        return through

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

        # From now, not from whenever the wait happens to begin: the code is
        # asked for partway through sign_in(), and mail that arrives before the
        # waiter starts is mail the waiter would otherwise refuse to look at.
        waiter = self._mail_waiter(time.time())

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

    def _form_root(self, frame):
        """The narrowest container form_selector names, not the earliest one.

        form_selector is written as a preference list -- "form, main, body" on
        generic, "#application-form, #app_body, form#application_form, form" on
        Greenhouse -- narrowest first, so discovery scopes to the application
        and not to the site around it. Handed to querySelector, that list is
        not a preference at all: it returns whichever match comes first in the
        *document*, and <body> precedes <main> in every document there is. So
        the fallback of last resort won on every page, and discovery has been
        reading whole pages.

        TikTok's is 66 fields wide partly because of it. Among them was the
        footer's <select class="language-selection-form">, English or 日本語 --
        an unlabelled control the mapper had no answer for, that the run
        counted as a field it could not fill, and that the reviewer read as
        evidence the page might not be an application at all.

        Written order alone would be too eager the other way. A careers site
        often carries a <form> that is a search box, and "form" leads three of
        the four lists here: taking it would scope discovery to two controls
        and lose the application entirely -- a worse failure than reading the
        footer, and on Workday it would break the one link that works today.
        So a candidate has to hold enough controls to be a form worth having;
        below that it is furniture too, and the search continues.
        """
        best, best_n = None, 0
        for sel in _selector_list(self.form_selector):
            try:
                el = frame.query_selector(sel)
                n = len(el.query_selector_all("input, textarea, select")) if el else 0
            except Exception:
                continue
            if n >= self.MIN_FORM_CONTROLS:
                return el
            if n > best_n:
                best, best_n = el, n
        return best if best is not None else frame

    # What separates an application from a search box. Deliberately low: the
    # question is only whether a candidate is furniture, and the narrowest
    # candidate that is not wins.
    MIN_FORM_CONTROLS = 3

    def _discover_in(self, frame) -> list[Field]:
        fields: list[Field] = []
        groups: dict[str, Field] = {}
        frame_url = "" if frame is self.page.main_frame else (frame.url or "")
        root = self._form_root(frame)
        for idx, el in enumerate(root.query_selector_all("input, textarea, select")):
            # A honeypot is a field put there to catch something that fills in
            # every box it finds. Invisibility is how they are usually hidden,
            # so the check below already skips most of them -- but that is a
            # side effect, and BNY's is named "honeypot" outright. Refusing by
            # name too costs nothing, and what it protects is a real person's
            # application not being taken for a bot's.
            if _is_a_trap(el):
                continue
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
            if itype == "file" and _fills_the_form_in_for_you(el):
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
                # Stamp it, because there is nothing else to go on and the
                # thing that used to go here did not work.
                #
                # It built f"{self.form_selector} {tag}:nth-of-type({idx+1})",
                # and form_selector is a selector *list* -- "form, main, body"
                # on generic, "form, div[data-automation-id=applyFlowPage],
                # body" on Workday. Concatenated, that parses as several
                # selectors, of which only the last carries the tag, so
                # querySelector returned whatever came first in the document:
                # on TikTok, the <main> element. Writing the field then set a
                # `value` property on <main>, and verification read the same
                # property straight back and passed. A field nobody could see
                # was reported filled and correct.
                #
                # The index was wrong too, independently: "body input:nth-of-
                # type(5)" does not mean the fifth input on the page, it means
                # any input that is the fifth of its type among its own
                # parent's children. Neither half of the expression referred to
                # the element it was built for.
                #
                # An attribute does, and start_application already marks its
                # target the same way. A re-render can drop it -- so can any
                # selector go stale -- and unlike the old expression, when this
                # one misses it finds nothing rather than something else.
                try:
                    el.evaluate("(e, v) => e.setAttribute('data-autoapply-fid', v)",
                                fid)
                    selector = f'[data-autoapply-fid="{fid}"]'
                except Exception:
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
                self._signed_in = True
                # Signing in usually lands on the form itself -- TikTok's
                # redirect_path carries you straight to /position/application.
                # Re-opening from the job URL throws that away and can walk
                # back into the wall, so only do it when the sign-in left us
                # somewhere without a form on it.
                self.settle_step(timeout_ms=10000, reloads=0)
                if not self.discover():
                    self.open(job)
        fields = self.take_landing_fields() or self.discover()

        # An ATS that puts its own front door before the form. Walk through it
        # rather than reporting the door as an empty application: AMD's run
        # said "no form fields discovered" about a form it had never reached,
        # because two fields called Email and "you must indicate that you have
        # read the privacy notice" are correctly not an application, and
        # nothing went any further.
        from ..gate import looks_like_an_application as _is_app

        if (fields and not _is_app(FillOutcome(job=job, fields=fields))
                and self.looks_like_an_entry_step()
                and self.pass_entry_step(job)):
            self._signed_in = True
            fields = self.discover()

        # Do not type into a page we were redirected to. A closed Ashby posting
        # redirects to Ashby's marketing homepage, where discovery found the
        # "Get in Touch" lead-capture box and the run put the candidate's email
        # address in it. The gate refuses to submit such a page, but by then
        # the address has already been typed into a stranger's form, and a
        # listing closing between runs is ordinary rather than exceptional.
        # Whether this is an application is asked before anything is typed, not
        # after. BNY's posting had expired and Oracle swapped in the careers
        # home page client-side, without changing the URL -- so the redirect
        # check below saw nothing wrong, and the run typed the job title into
        # the site's search box, a location into its location filter, and the
        # candidate's portfolio URL into a customer-service chat widget. The
        # gate refused to submit it, correctly and far too late: refusing to
        # submit does not un-type anything.
        from ..gate import looks_like_an_application

        probe = FillOutcome(job=job, fields=fields)
        if fields and not looks_like_an_application(probe):
            log = _log.get(f"worker.{self.ats}")
            log.warning("%s does not look like an application (%s) -- not "
                        "filling anything", _log.brief(self.page.url, 60),
                        ", ".join((f.label or f.id or "?")[:28]
                                  for f in fields[:4]))
            outcome = FillOutcome(job=job, fields=fields)
            outcome.errors.append(
                "this page is not a job application: "
                + ", ".join((f.label or f.id or "?")[:40] for f in fields[:4]))
            outcome.reached_end = False
            self._record_page_state(outcome)
            return outcome

        if not _same_posting(job.url, self.page.url) and len(fields) < 3:
            log = _log.get(f"worker.{self.ats}")
            log.warning("landed on %s, which is not %s -- not filling anything",
                        _log.brief(self.page.url, 70), _log.brief(job.url, 70))
            outcome = FillOutcome(job=job, fields=fields)
            outcome.errors.append(
                f"redirected to {self.page.url} -- the posting is probably "
                "closed; nothing was filled")
            outcome.reached_end = False
            self._record_page_state(outcome)
            return outcome

        mappings = mapper.map_fields(fields, profile, job.ats,
                                     store=store, provider=provider)
        outcome = self.fill(job, fields, mappings, screenshot_dir, profile)
        self._review_and_repair(job, fields, mappings, outcome, profile,
                                store, provider)
        if not fields and self.needs_auth():
            outcome.needs_auth = True
        if self._signed_in:
            # This form was reached by signing in, so it exists only inside
            # this browser's session. The agent fallback launches a fresh
            # profile at the job URL: it gets the sign-in wall, reports "no
            # form fields discovered; sign-in required", and the pipeline
            # merges that into the queue row on top of the real findings. On
            # TikTok that buried a run that had filled 49 of 66 fields under a
            # verdict describing a page it never reached. The wizard lane has
            # said so since it was written; the single-page lane never did.
            outcome.session_bound = True
        return outcome

    _held_off: str = ""

    def _record_page_state(self, outcome) -> None:
        """What this page is, on the paths that give up before filling.

        Both early returns built a bare outcome and left saw_captcha and
        needs_auth False, so a run that stopped in front of a CAPTCHA reported
        no CAPTCHA. The gate then had nothing to block on beyond "nothing was
        filled", and _needs_agent_fallback -- which refuses to retry a page
        held by a CAPTCHA, precisely because the agent is held by it too --
        saw no reason to refuse. So AMD's hCaptcha door was handed to the
        agent lane on every run, to spend a full budget of steps discovering
        it could not get through either.
        """
        try:
            outcome.verify_detail["_landed_url"] = self.page.url
            outcome.verify_detail["_page_title"] = self.page.title() or ""
        except Exception:
            pass
        try:
            outcome.saw_captcha = self.saw_captcha()
        except Exception:
            pass
        try:
            outcome.needs_auth = self.needs_auth()
        except Exception:
            pass
        held = self._held_off or self.rate_limited()
        if held:
            outcome.errors.append(f"the site is holding us off: {held}")

    def _review_and_repair(self, job, fields, mappings, outcome, profile,
                           store, provider) -> None:
        """The audit / form-verdict / explore tiers, for a single-page form.

        These lived only in WizardWorker.run, so every single-page ATS --
        Greenhouse, Lever, Ashby, iCIMS, generic -- had the deterministic pass
        and nothing else: no reader checking that an answer answers the
        question asked, no second attempt at a field the form rejected, no
        exploring a widget the filler cannot drive, and nothing taught to the
        next run. Cloudflare stalling at 17 of 24 fields and Notion at 15 of 23
        was that absence, not a shortage of information.

        The form's own validation is the acceptance signal here. A wizard gets
        one for free -- the step either advances or comes back -- and a
        single-page form gives the same evidence by whether it still objects.
        """
        from ..repair import (audit_step, commit_lessons, drop_lessons,
                              read_errors, repair_step, teach_paths)

        log = _log.get(f"worker.{self.ats}")
        if not fields:
            return

        audited, notes = audit_step(self, fields, mappings, profile,
                                    provider=provider, store=store,
                                    ats=job.ats)
        if notes:
            log.info("audit corrected %d", audited)
            for note in notes:
                log.debug("  audit: %s", _log.brief(note, 120))
            outcome.errors.extend(notes)

        teach_paths(store, self, fields, job.ats)

        repaired, notes = repair_step(self, fields, mappings, profile,
                                      provider=provider, store=store,
                                      ats=job.ats, unwritten=outcome.unwritten)
        if notes:
            log.info("repaired %d", repaired)
            for note in notes:
                log.debug("  repair: %s", _log.brief(note, 120))
            outcome.errors.extend(notes)

        if repaired:
            for m in mappings:
                if (m.source in ("form-repair", "audit") and m.value
                        and m.field_id not in outcome.filled_ids):
                    outcome.filled_ids.append(m.field_id)

        # Nothing left flagged means the form is satisfied with what it holds,
        # which is the only evidence available that an answer was right. A form
        # still objecting is not evidence of anything, so what was staged for
        # it is discarded rather than replayed confidently on every later run.
        if read_errors(self):
            if dropped := drop_lessons(store):
                log.info("the form still objects; discarded %d unproven "
                         "answer(s)", dropped)
        elif learned := commit_lessons(store, job.ats):
            log.info("the form accepts it; learned %d answer(s)", len(learned))
            for lesson in learned:
                log.debug("  learned: %s", _log.brief(lesson, 100))

    # ---- filling ---------------------------------------------------------
    def fill(self, job: Job, fields: list[Field], mappings: list[Mapping],
             screenshot_dir: str, profile: dict | None = None) -> FillOutcome:
        from .. import mapper

        profile = profile if profile is not None else {}

        outcome = FillOutcome(job=job, fields=fields, mappings=mappings)
        by_id = {f.id: f for f in fields}

        # Workday keeps the candidate profile between applications, so a
        # repeatable section arrives already populated -- websites, work
        # history, uploads. Writing into an entry that already holds an answer
        # is what produced "You can't add duplicate website URLs" even after
        # the two entries on the page were given different links: the clash was
        # with what the account had stored from an earlier run, not with each
        # other. Read what is already there and leave it alone.
        repeated_labels = {}
        for f in fields:
            key = (f.label or "").strip().lower()
            if key:
                repeated_labels[key] = repeated_labels.get(key, 0) + 1
        prefilled: dict[str, str] = {}
        for f in fields:
            if repeated_labels.get((f.label or "").strip().lower(), 0) < 2:
                continue
            try:
                el = self.frame_for(f).query_selector(f.selector)
                current = (el.evaluate("e => e.value || ''") or "").strip() if el else ""
            except Exception:
                current = ""
            if not current:
                continue
            # ...unless it is one of our own links that an earlier run left in
            # a field that never asked for one. That is the one stored value
            # this tool can prove it put there, so it is the one it may take
            # back. Everything else the account holds stays: on TikTok it is
            # the resume, parsed by TikTok, and richer than the profile.
            if mapper.is_a_stray_link(f.label or "", current, profile):
                _log.get(f"worker.{self.ats}").info(
                    "%s holds %s, left by an earlier run -- overwriting",
                    f.label or f.id, _log.brief(current, 60))
                continue
            prefilled[f.id] = current
            outcome.filled_ids.append(f.id)

        for m in mappings:
            if m.action not in ("fill", "generate") or not m.value:
                continue
            f = by_id.get(m.field_id)
            if f is None:
                continue
            if f.id in prefilled:
                # What is on the form is now the answer to this field. Leaving
                # the mapping holding the value we chose not to write makes
                # verify compare against a value we deliberately did not send
                # and report a mismatch -- want=https://nideesh.ai
                # got=https://www.nideesh.ai, on a field that is correct and
                # that we were right not to touch. That false failure is enough
                # to mark the whole run unverified.
                m.value = prefilled[f.id]
                m.source = "account"
                outcome.errors.append(
                    f"{f.label or f.id}: already answered on the account "
                    f"as '{_log.brief(prefilled[f.id], 40)}', left as is")
                continue
            try:
                written = self._write(f, m.value)
                if written is not None:
                    # A custom dropdown may render the option differently to how
                    # the profile spells it; record what is actually on the form.
                    m.value = written
                    outcome.filled_ids.append(f.id)
                else:
                    # We know the answer and the control would not take it. Say
                    # so, and hand the field to the repair tier -- waiting for
                    # the form to object means never, for anything it does not
                    # validate.
                    outcome.unwritten.append(f.id)
                    outcome.errors.append(
                        f"{f.label or f.id}: could not write '{_log.brief(m.value, 40)}'")
            except Exception as exc:                       # one field never kills the run
                outcome.unwritten.append(f.id)
                outcome.errors.append(f"{f.label or f.id}: {exc}")

        for m in mappings:
            f = by_id.get(m.field_id)
            if f is not None and f.id in outcome.filled_ids:
                self.form_frame_url = f.frame_url
                break

        # What the run actually ended up looking at, for the tier that judges
        # whether any of this was plausible.
        try:
            outcome.verify_detail["_landed_url"] = self.page.url
            outcome.verify_detail["_page_title"] = self.page.title() or ""
        except Exception:
            pass
        outcome.saw_captcha = self.saw_captcha()
        outcome.needs_auth = self.needs_auth()
        outcome.filled_ok = not outcome.missing_required
        outcome.screenshot_path = self.screenshot(job, screenshot_dir)
        return outcome

    # A learned recipe for a control that needed clicking through. Kept in the
    # same store as any other taught answer; the separator is what tells them
    # apart, so nothing else needed a new concept.
    PATH_SEP = " > "

    def _write(self, f: Field, value: str) -> Optional[str]:
        """Write one field. Returns the value actually written, or None."""
        if self.PATH_SEP in (value or "") and f.kind in (
                "combobox", "select", "radio", "checkbox", "text"):
            if written := self._replay_path(f, value):
                return written
        ctx = self.frame_for(f)
        el = ctx.query_selector(f.selector)
        if el is None:
            return None
        if f.kind == "file":
            # Workday's upload is a list, and every retry of a rejected step
            # appended another copy -- the run left three identical resumes on
            # My Experience. Uploading is only ever done once per field.
            already = ctx.query_selector(
                f"{f.selector} ~ [data-automation-id='file-upload-item'], "
                "[data-automation-id='file-upload-item']")
            if already is not None and os.path.basename(value) in (
                    (already.inner_text() or "")):
                return value
            path = os.path.expanduser(value)
            if not os.path.exists(path):
                # A mislabelled upload zone can be mapped to a name or an email
                # by a rule matching its label. Uploading is not possible and
                # the run should not die over it -- leave it for the gate.
                return None
            el.set_input_files(path)
            return value
        if f.kind == "select":
            # Only a real <select> answers select_option. Discovery calls a
            # custom dropdown "select" too, and the error it raises --
            # "Element is not a <select> element" -- says nothing about what to
            # do instead. Drive that like the combobox it is.
            tag = ""
            try:
                tag = (el.evaluate("e => e.tagName") or "").upper()
            except Exception:
                pass
            if tag != "SELECT":
                return self._write_combobox(el, value, ctx)
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

        if not _click(el):
            return None
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
                    _click(opt)
                    self.page.wait_for_timeout(250)
                    return text
        self.page.keyboard.press("Escape")
        return None

    def _replay_path(self, f: Field, value: str) -> Optional[str]:
        """Re-walk a click path that was worked out once and remembered.

        This is what stops an awkward control costing a model call on every
        future application: the first run figures out that Job Board has to be
        opened before Handshake exists, and every run after it just clicks.
        """
        ctx = self.frame_for(f)
        el = ctx.query_selector(f.selector)
        if el is None or not _click(el):
            return None
        self.page.wait_for_timeout(600)

        steps = [s.strip() for s in value.split(self.PATH_SEP) if s.strip()]
        for step in steps:
            found = None
            for node in ctx.query_selector_all(
                    "[role=option], [data-automation-id='promptOption'], "
                    "li, button, [role=button]"):
                try:
                    if not node.is_visible():
                        continue
                except Exception:
                    continue
                if (node.inner_text() or "").strip() == step:
                    found = node
                    break
            if found is None or not _click(found):
                return None
            self.page.wait_for_timeout(700)

        try:
            self.page.keyboard.press("Escape")
        except Exception:
            pass
        return value

    def _write_choice(self, f: Field, el, value: str) -> Optional[str]:
        """Tick the member of a radio/checkbox group whose label is the answer.

        A lone checkbox has no options and is a plain yes/no consent tick; a
        group has to match the answer against the members' own labels, because
        checking whichever one happens to be first answers a different question
        than the one that was asked.
        """
        from ..mapper import resolve_option

        if not f.options:
            text = str(value).strip().lower()
            # A tick box holds yes or no and nothing else. Auto-Owners asks
            # "I have a preferred name" as a lone checkbox, the preferred-name
            # rule matched the label and handed it "Nideesh", and this read
            # that as not-yes, unticked the box, and returned the name as the
            # value written. The run counted the field filled and verification
            # then found it empty. Refusing a value that is neither answers the
            # question honestly: nothing was written.
            if text not in ("yes", "true", "1", "on", "no", "false", "0", "off"):
                _log.get(f"write.{self.ats}").debug(
                    "choice %s: %r is not a yes or a no for a tick box",
                    _log.brief(f.label or f.id, 40), value)
                return None
            want = text in ("yes", "true", "1", "on")
            # Look before touching it. TikTok serves "I have read and agreed to
            # the Privacy Policy" already ticked, and clicking a ticked box
            # unticks it: the run then reported "could not write 'Yes'" and
            # "'true' would not stick", explore spent six steps on it, and
            # verification failed -- over a field that had been correct from
            # the moment the page loaded, which the run itself broke and could
            # not repair, because every repair attempt was another click.
            #
            # A control already saying what we mean is written. On a consent
            # box that is also the only safe reading: the alternative is a run
            # whose way of agreeing to a privacy policy is to toggle it and
            # hope.
            try:
                now = el.is_checked()
            except Exception:
                now = None
            if now is want:
                return value
            if not _click(el):
                return None
            try:
                if el.is_checked() is not want:
                    el.check(timeout=4000) if want else el.uncheck(timeout=4000)
            except Exception:
                pass
            return value

        # "'No' would not stick" names the symptom and not one of the four
        # different causes behind it, which is a diagnosis that costs a whole
        # run to make. Each branch says which one it was.
        log = _log.get(f"write.{self.ats}")
        ctx = self.frame_for(f)
        members = ctx.query_selector_all(f.selector)
        labelled = []
        for m in members:
            text = (ctx.evaluate(LABEL_JS, m) or "").strip()
            if text:
                labelled.append((m, text))
        if not labelled:
            log.debug("choice %s: %d member(s), none with a readable label",
                      _log.brief(f.label or f.id, 40), len(members))
            return None

        chosen = resolve_option(value, [text for _, text in labelled])
        if chosen is None:
            log.debug("choice %s: %r matches none of %s",
                      _log.brief(f.label or f.id, 40), value,
                      [t for _, t in labelled][:8])
            return None
        for m, text in labelled:
            if text == chosen:
                # Already the answer? Then it is written. Clicking a ticked
                # checkbox unticks it, which is how TikTok's privacy-policy
                # consent went from correct to broken and then unrepairable,
                # every repair attempt being another click.
                try:
                    if m.is_checked():
                        return text
                except Exception:
                    pass
                # check() runs its own actionability wait and never reaches the
                # overlay-aware click, so a radio painted over by a custom
                # control times out after 30s exactly like the buttons did.
                if not _click(m):
                    log.debug("choice %s: '%s' found but not clickable",
                              _log.brief(f.label or f.id, 40), text)
                    return None
                try:
                    if not m.is_checked():
                        m.check(timeout=4000)
                except Exception:
                    pass
                # A radio that reports unchecked after both a click and a
                # check() is one the widget is driving itself, and reporting it
                # written would teach the answer as good on a field that is
                # still empty.
                try:
                    if not m.is_checked():
                        log.debug("choice %s: clicked '%s', still unchecked",
                                  _log.brief(f.label or f.id, 40), text)
                        return None
                except Exception:
                    pass
                return text
        return None

    # Kinds whose choices are a closed list, so answering off-list is a
    # guaranteed rejection and asking the model blind is a guaranteed guess.
    PICKLIST_KINDS = ("select", "combobox", "radio", "checkbox")

    def probe_options(self, f: Field) -> list[str]:
        """Read a control's real choices from the live page.

        Workday renders a dropdown's options only while it is open, so
        discovery records the field with options=[] and every tier downstream
        answers blind. That is how "Have you ever served in the military?" got
        'No': a sensible answer to the question as written, and not one of the
        choices, because the real ones are VEVRAA wordings -- "I am not a
        protected veteran", "I don't wish to answer". There is no "No" to pick,
        so the write failed, the repair tier asked the model again with the
        same empty option list, and it answered 'No' again.

        Opening the control costs a second and turns a guess into a choice.
        """
        if f.kind not in self.PICKLIST_KINDS:
            return []
        ctx = self.frame_for(f)
        el = ctx.query_selector(f.selector)
        if el is None:
            return []
        try:
            return self._probe_options(el, ctx)
        except Exception:
            return []

    def _probe_options(self, el, frame=None) -> list[str]:
        """Open a combobox just long enough to read its choices, then close it."""
        try:
            if not _click(el):
                return []
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
        if (n.querySelector && n.querySelector(
              '[role=option], [data-automation-id="promptOption"]')) return n;
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
            candidates.append(scope.query_selector_all(
                "[role=option], [data-automation-id='promptOption']"))
        # A menu rendered into a portal is not an ancestor of the input, so fall
        # back to the listbox this input names, then to any open one.
        owns = None
        try:
            owns = el.get_attribute("aria-controls") or el.get_attribute("aria-owns")
        except Exception:
            pass
        if owns:
            candidates.append(ctx.query_selector_all(f'[id="{owns}"] [role=option]'))
        candidates.append(ctx.query_selector_all(
            "[role=listbox] [role=option], [data-automation-id='promptOption']"))

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
        """Did the application actually land?

        This searched the whole page body, so a footer reading "Thank you for
        visiting", a testimonial, or a chat widget's greeting confirmed a
        submission that never happened -- and a false confirmation is the one
        mistake here with no way back: the job is recorded applied, and
        already_applied() skips it forever after.

        So: only text that appeared *because* we submitted. The form is gone
        or the URL changed, and the confirming words are near the top of what
        replaced it rather than anywhere on the page.
        """
        body = ""
        try:
            body = (self.page.inner_text("body") or "").lower()
        except Exception:
            pass

        moved = self.page.url != url_before
        # The submit control, not "a form element": form_selector falls back to
        # body on Workday ("form, div[data-automation-id='applyFlowPage'], body")
        # so it matches on every page ever rendered and could never be gone.
        # The button we just clicked is specific to the form we just sent, and
        # a page that accepted a submission does not still offer it.
        submit_gone = True
        try:
            submit_gone = query_first(self.page, (self.submit_selector,)) is None
        except Exception:
            pass
        if not (moved or submit_gone):
            return False, ("the submit button is still on screen and the URL "
                           "did not change; not treating this as submitted")

        # Near the top of what replaced the form, not buried in a footer.
        head = body[:1200]
        for pattern in self.confirm_patterns:
            if re.search(pattern, head):
                return True, f"confirmed: matched /{pattern}/"
        if self.page.url != url_before and "confirmation" in self.page.url.lower():
            return True, f"confirmed: redirected to {self.page.url}"
        return False, "no post-submit confirmation found"


def css_escape(value: str) -> str:
    return re.sub(r"([^a-zA-Z0-9_-])", r"\\\1", value)


# BNY spells it "honey-pot", with a hyphen, and its trap is *visible* -- so
# neither the original pattern nor the visibility check would have kept the
# run out of it. Match the separator rather than one spelling of it.
_TRAP = re.compile(
    r"honey[-_ ]?pot|bot[-_ ]?trap|leave[-_ ]?(this|it)[-_ ]?(blank|empty)", re.I)

# An upload that offers to fill the form in for you, rather than one the
# application is asking for.
_AUTOFILL_UPLOAD = re.compile(
    r"autofill|auto-fill|automatically fill|fill (in )?(the |your )?"
    r"(application|form|fields)|recommended jobs|job recommendations", re.I)


def _fills_the_form_in_for_you(el) -> bool:
    """Is this upload a convenience widget rather than an application field?

    Notion's Ashby form opens with "Autofill from resume -- Upload your resume
    here to autofill key application fields", a 1x1 file input with no id and
    no name, sitting above the real Resume field. Discovery took it for an
    application field and, having nothing else nearby to read, labelled it
    "Full Name" -- the caption of the input after it. The reviewer then
    reported duplicate entries for the same question, which was true of what
    it was shown.

    Uploading there is worse than useless: Ashby's parser would rewrite the
    fields the run had just filled. BNY's careers page has the same shape in
    different words -- "upload or drag and drop your PDF resume file here to
    get AI recommended jobs" -- and that one cost a whole run, because it made
    a search page look like an application.

    The real upload says what it is for; these say what they will do for you.
    """
    try:
        text = el.evaluate(
            """e => {
                 const box = e.closest('div,section,fieldset,form') || e;
                 return ((box.innerText || '') + ' ' +
                         (box.className || '')).replace(/\\s+/g, ' ');
               }""") or ""
    except Exception:
        return False
    return bool(_AUTOFILL_UPLOAD.search(text))


def _is_a_trap(el) -> bool:
    """A field that exists to catch something that fills in everything."""
    try:
        for attr in ("name", "id", "class", "aria-label", "placeholder"):
            if _TRAP.search(el.get_attribute(attr) or ""):
                return True
    except Exception:
        return False
    return False


def _selector_list(selector: str) -> list[str]:
    """Split a CSS selector list on its top-level commas, in written order.

    Only the top-level ones: "input[name='a,b'], form" is two selectors, not
    three, and :is(a, b) is one.
    """
    out, depth, current = [], 0, ""
    quote = ""
    for ch in selector:
        if quote:
            current += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            if part := current.strip():
                out.append(part)
            current = ""
            continue
        current += ch
    if part := current.strip():
        out.append(part)
    return out


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
        self.open(job)
        return self.walk(job, profile, store, provider, screenshot_dir)

    def walk(self, job: Job, profile: dict, store, provider,
             screenshot_dir: str) -> FillOutcome:
        """The step loop alone, on a browser already standing on the form.

        Split out from run() so that whoever got us here does not have their
        work thrown away. The bake-off harness signs in and navigates to the
        application, and a filler calling run() re-opened the job URL on top
        of that -- the harness reported 13 fields and the filler reported 0 a
        moment later, on the same page.
        """
        from .. import mapper
        from ..repair import (audit_step, commit_lessons, drop_lessons,
                              repair_step, teach_paths)
        from ..verify import verify_fields

        log = _log.get(f"wizard.{self.ats}")
        outcome = FillOutcome(job=job)
        all_ok = True
        seen_steps: list[str] = []

        for step_no in range(1, self.max_steps + 1):
            if self.saw_captcha():
                log.info("step %d: captcha present, stopping", step_no)
                outcome.saw_captcha = True
                outcome.reached_end = False
                break
            if self.needs_auth():
                log.info("step %d: sign-in wall at %s", step_no, self.page.url)
                ok, detail = self.try_sign_in(job)
                log.info("step %d: sign-in -> %s (%s)", step_no, ok, detail)
                outcome.errors.append(f"sign-in: {detail}")
                if ok:
                    # Everything past this point lives in this browser's session.
                    outcome.session_bound = True
                if not ok:
                    outcome.needs_auth = True
                    outcome.reached_end = False
                    break
                # Signed in, but the wizard behind the wall has not rendered
                # yet. sign_in returns as soon as the sign-in page is gone, and
                # discovery ran 28ms later against an empty document -- the run
                # then found nothing to advance with and stopped, having lost
                # the whole application to a race. Wait for the form.
                self.settle_step()
                continue

            fields = self.discover()
            if not fields and not self.at_review():
                # Empty is ambiguous: a step that failed to load is
                # indistinguishable from one with nothing on it, and treating
                # the first as the second abandons the application.
                self.settle_step()
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
                outcome.reached_end = False
                break

            mappings = mapper.map_fields(fields, profile, job.ats,
                                         store=store, provider=provider)
            for m in mappings:
                log.debug("  map %-34s %-9s %-12s %s",
                          _log.brief(m.label or m.field_id, 34), m.action,
                          m.source or "-", _log.brief(m.value, 40))

            step = self.fill(job, fields, mappings, screenshot_dir="",
                             profile=profile)
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

            # Routes worked out while filling are staged with everything else,
            # so they are learned only if this step is accepted.
            teach_paths(store, self, fields, job.ats)

            ok, detail = verify_fields(self.page, fields, mappings,
                                       outcome.filled_ids)
            for fid, d in detail.items():
                if isinstance(d, dict) and d.get("ok") is False:
                    log.debug("  verify FAIL %-28s want=%s got=%s",
                              _log.brief(d.get("label", fid), 28),
                              _log.brief(d.get("expected"), 30),
                              _log.brief(d.get("actual"), 30))
            outcome.verify_detail.update(detail)
            step_ok = ok

            # A wizard validates on Save and Continue: if it rejects the step it
            # re-renders it with the offending fields marked, and we are still
            # standing on the same step. That rejection names exactly what is
            # wrong, which is a better critic than anything we could ask, so
            # repair from it and try to move on again. Whatever works is written
            # to the corrections store, so the next application answers it right
            # the first time instead of relearning it here.
            if not self.at_review() and self.advance():
                # Advancing does not yet mean accepted: a rejected step
                # re-renders itself with the offending fields marked, and we
                # are still standing on it. repair_step reads that verdict.
                repaired, notes = repair_step(
                    self, fields, mappings, profile, provider=provider,
                    store=store, ats=job.ats, unwritten=step.unwritten)
                if notes:
                    log.info("step %d: form rejected it, repaired %d",
                             step_no, repaired)
                    for note in notes:
                        log.debug("  repair: %s", _log.brief(note, 120))
                outcome.errors.extend(notes)
                if repaired:
                    # A field the repair tier answered is answered. It was
                    # never written by fill(), so its id is not in filled_ids,
                    # and the gate then blocks on a required field that is
                    # sitting there correctly filled in -- the run reached
                    # Review with "no answer for required: disabilityStatus"
                    # three lines under the log saying what it had answered it
                    # with, and the step being accepted.
                    for m in mappings:
                        if (m.source in ("form-repair", "audit") and m.value
                                and m.field_id not in outcome.filled_ids):
                            outcome.filled_ids.append(m.field_id)

                    ok2, detail2 = verify_fields(self.page, fields, mappings,
                                                 outcome.filled_ids)
                    outcome.verify_detail.update(detail2)
                    # The state after the repair is the state of the step. The
                    # pre-repair reading is what the repair was for, so keeping
                    # it in the verdict makes a successful repair unable to
                    # clear the failure that prompted it.
                    step_ok = ok2
                    self.advance()

                # Whether anything was learned here rests on the form, not on
                # our own reading of the page. A step we are no longer standing
                # on was accepted; one that came back is not evidence of
                # anything, so what was staged for it is discarded.
                moved = "|".join(sorted(f.id for f in self.discover()))
                if moved != fingerprint:
                    outcome.steps_done += 1
                    # Past step one, the page we are on is reachable only by
                    # having walked here. A fresh browser on the job URL gets
                    # step one, so the agent lane can no longer help.
                    outcome.session_bound = True
                    if learned := commit_lessons(store, job.ats):
                        log.info("step %d accepted; learned %d answer(s)",
                                 step_no, len(learned))
                        for lesson in learned:
                            log.debug("  learned: %s", _log.brief(lesson, 100))
                elif dropped := drop_lessons(store):
                    log.info("step %d rejected; discarded %d unproven answer(s)",
                             step_no, dropped)
                all_ok = all_ok and step_ok
                continue

            all_ok = all_ok and step_ok
            if self.at_review():
                log.info("step %d: reached the review step", step_no)
                break
            if not self.advance():
                log.info("step %d: nothing to advance with, stopping", step_no)
                outcome.reached_end = False
                break
        else:
            log.warning("ran out of steps (max %d) before reaching review",
                        self.max_steps)
            outcome.reached_end = False

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


def _same_posting(wanted: str, landed: str) -> bool:
    """Are we still on the job we asked for?

    The posting's own identifier decides it, not the host. A company's careers
    site handing off to its ATS tenant is how a large share of real links work
    -- careers.amd.com/careers-home/jobs/91176 becomes
    campus-amd.icims.com/jobs/91176/login, a different domain and the same job
    -- so comparing hosts rejects the normal case. The id survives that handoff.
    What it does not survive is a redirect to a marketing homepage, which is
    the thing worth catching.

    Host is the fallback for a URL carrying nothing identifiable, where the
    only question left is whether we are still on the same site at all.
    """
    from urllib.parse import urlparse

    def marks(url: str) -> tuple[str, set[str]]:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower().removeprefix("www.")
        # Registrable-ish suffix, so jobs.ashbyhq.com and www.ashbyhq.com match.
        host = ".".join(host.split(".")[-2:])
        # A segment with a digit in it: a job number, a slug ending in one, a
        # UUID. Four characters is enough -- AMD's is 91176, and requiring six
        # dropped it and took the host comparison down with it.
        ids = {seg.lower() for seg in re.split(r"[/?&=]", parsed.path or "")
               if len(seg) >= 4 and re.search(r"\d", seg)}
        return host, ids

    want_host, want_ids = marks(wanted)
    got_host, got_ids = marks(landed)
    if want_ids and got_ids:
        return bool(want_ids & got_ids)
    if want_ids and not got_ids:
        # We asked for an identifiable posting and ended up somewhere that
        # names no posting at all. That is the homepage case.
        return False
    return want_host == got_host


def _offsite(from_url: str, to_url: str) -> bool:
    """Does this link leave the current site? An ATS handoff always does."""
    from urllib.parse import urlparse

    def host(u):
        h = (urlparse(u or "").hostname or "").lower().removeprefix("www.")
        return ".".join(h.split(".")[-2:])

    a, b = host(from_url), host(to_url)
    return bool(a and b and a != b)


def _posting_ids(url: str) -> set[str]:
    """The identifying parts of a job URL: the number or slug naming it."""
    from urllib.parse import urlparse

    return {seg.lower() for seg in re.split(r"[/?&=]", urlparse(url or "").path or "")
            if len(seg) >= 6 and re.search(r"\d", seg)}
