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

from .base import Worker


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
