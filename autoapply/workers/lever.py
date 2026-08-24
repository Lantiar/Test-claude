"""Lever: single-page form at jobs.lever.co/<company>/<id>/apply."""
from __future__ import annotations

from .base import Worker


class LeverWorker(Worker):
    ats = "lever"
    form_selector = "form.application-form, form[action*='apply'], form"
    submit_selector = ("button[type=submit], .postings-btn[type=submit], "
                       "button:has-text('Submit application')")
    confirm_patterns = (
        r"thank you for applying",
        r"application (was )?(received|submitted)",
        r"we('| ha)ve received your application",
        r"your application has been submitted",
    )

    def open(self, job):
        super().open(job)
        try:
            self.page.wait_for_selector("form", timeout=15000)
        except Exception:
            pass
