"""Greenhouse: single-page React form on boards/job-boards.greenhouse.io."""
from __future__ import annotations

from .base import Worker


class GreenhouseWorker(Worker):
    ats = "greenhouse"
    # job-boards.* uses #application-form; the older boards.* uses #app_body.
    form_selector = "#application-form, #app_body, form#application_form, form"
    submit_selector = ("#submit_app, button[type=submit], "
                       "input[type=submit], button:has-text('Submit application')")
    confirm_patterns = (
        r"thank you for applying",
        r"your application (has been|was) submitted",
        r"application (was )?(received|submitted)",
        r"we('| ha)ve received your application",
    )

    def open(self, job):
        super().open(job)
        # The form is lazily mounted; give React a beat before scanning.
        try:
            self.page.wait_for_selector("input, textarea", timeout=15000)
        except Exception:
            pass
