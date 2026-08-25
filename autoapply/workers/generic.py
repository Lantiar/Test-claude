"""A DOM worker for ATSs with no dedicated one.

The base worker already reads labels the way a person would, groups radio and
checkbox sets, drives comboboxes and uploads files. That covers a lot of forms
that are not Greenhouse, Lever or Workday -- Ashby's application form discovers
cleanly with no Ashby-specific code at all.

So this is tried before the agent: it is deterministic, free, and verifiable,
where the agent lane needs a model and grades its own work. When it discovers
nothing the pipeline still falls through to the agent, which is what that
fallback is for.
"""
from __future__ import annotations

from .base import Worker, query_first


class GenericWorker(Worker):
    ats = "generic"
    # Fall back to the whole document: plenty of application forms are not
    # wrapped in a <form> at all.
    form_selector = "form, main, body"
    submit_selector = ("button[type=submit], input[type=submit], "
                       "button:has-text('Submit'), button:has-text('Submit Application')")
    auth_selectors = (
        "input[type=password]",
        "a:has-text('Sign in')", "button:has-text('Sign in')",
        "a:has-text('Create account')", "button:has-text('Create account')",
    )

    def open(self, job):
        super().open(job)
        # A company careers page is often a shell around a real ATS: AMD's
        # careers.amd.com posting is an AMD-branded wrapper whose Apply button
        # goes to campus-amd.icims.com. Discovery on the wrapper finds nothing
        # useful, so follow the link through to the ATS that actually holds the
        # form and let the normal machinery take it from there.
        target = self._ats_apply_link()
        if target:
            try:
                self.page.goto(target, wait_until="domcontentloaded")
                self.page.wait_for_timeout(2500)
            except Exception:
                pass

    def _ats_apply_link(self) -> str | None:
        """An Apply link pointing at a different host we recognise as an ATS."""
        from urllib.parse import urljoin, urlparse

        from ..router import detect

        here = urlparse(self.page.url).hostname or ""
        best = None
        for a in self.page.query_selector_all("a[href]"):
            href = (a.get_attribute("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            url = urljoin(self.page.url, href)
            host = urlparse(url).hostname or ""
            if not host or host == here:
                continue
            if detect(url) == "unknown":
                continue
            text = ((a.inner_text() or "") + " " + href).lower()
            # Prefer an actual Apply control; a careers page also links to the
            # ATS from "Returning User Login", which lands on a sign-in wall.
            if "apply" in text:
                return url
            best = best or url
        return best
