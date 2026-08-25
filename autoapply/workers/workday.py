"""Workday: multi-page wizard keyed off data-automation-id.

Selector conventions are Workday's own and are stable across tenants; they were
cross-checked against berellevy/job_app_filler, which drives the same markup.
Each field lives in a div[data-automation-id^="formField-"], so discovery walks
those containers rather than raw inputs — that is what gives us the label and the
control type together.
"""
from __future__ import annotations

import os
import re

from typing import Optional

from ..mapper import match_rule
from ..models import Field
from .base import WizardWorker, query_first

FORM_FIELD = "div[data-automation-id^='formField-']"


class WorkdayWorker(WizardWorker):
    ats = "workday"
    form_selector = "form, div[data-automation-id='applyFlowPage'], body"

    next_selectors = (
        "button[data-automation-id='bottom-navigation-next-button']",
        "button[data-automation-id='pageFooterNextButton']",
        "button[data-automation-id='wd-CommandButton_uic_okButton']",
        "button:has-text('Save and Continue')",
        "button:has-text('Continue')",
        "button:has-text('Next')",
    )
    review_selectors = (
        "button[data-automation-id='bottom-navigation-submit-button']",
        "button:has-text('Submit')",
    )
    submit_selector = ("button[data-automation-id='bottom-navigation-submit-button'], "
                       "button:has-text('Submit')")
    # Tag-agnostic on purpose: tenants render these as either <a> or <button>,
    # and the Blackstone tenant ships none of the *createAccountLink*/
    # *createAccountPage* ids the older list relied on -- it gates the wizard
    # behind a Create Account step whose tells are the password pair and the
    # account submit button.
    auth_selectors = (
        "[data-automation-id='createAccountLink']",
        "[data-automation-id='signInLink']",
        "[data-automation-id='createAccountPage']",
        "[data-automation-id='createAccountSubmitButton']",
        "input[data-automation-id='verifyPassword']",
        "input[data-automation-id='password']",
    )
    confirm_patterns = (
        r"thank you for applying",
        r"your application (has been|was) submitted",
        r"application (was )?(received|submitted)",
        r"we('| ha)ve received your application",
    )

    # Each hop below waits for the state it expects instead of sleeping a fixed
    # span. Workday is a single-page app whose controls render well after
    # domcontentloaded -- on a live tenant the old fixed 1.5s/2s waits expired
    # while the job page was still blank, so the worker never left the job
    # description, discovered zero fields, and the run fell through to the agent.
    APPLY_SELECTORS = ("a[data-automation-id='adventureButton']",
                       "button[data-automation-id='adventureButton']",
                       "a:has-text('Apply')", "button:has-text('Apply')")
    MANUAL_SELECTORS = ("a[data-automation-id='applyManually']",
                        "button[data-automation-id='applyManually']",
                        "a:has-text('Apply Manually')",
                        "button:has-text('Apply Manually')")
    # Any of these means a hop landed: the wizard, or the account wall that
    # guards it. Both are real destinations -- needs_auth() tells them apart.
    # Deliberately *not* div[data-automation-id='applyFlowPage']: that is the
    # outer shell and it mounts before the step content, so waiting on it
    # returns while the page is still empty and discovery sees nothing.
    LANDED_SELECTORS = (FORM_FIELD,
                        "[data-automation-id='signInContent']",
                        "[data-automation-id='createAccountSubmitButton']",
                        "input[data-automation-id='password']")

    # Live tenants can take many seconds to render; fixtures and direct apply
    # links render at once. Overridable so a slow tenant can be given more.
    nav_timeout = int(os.getenv("AUTOAPPLY_NAV_TIMEOUT_MS", "30000"))

    def _settle(self, selectors: tuple[str, ...]) -> bool:
        """Wait until any of these is visible. False if none arrives in time."""
        joined = ", ".join(selectors)
        try:
            self.page.wait_for_selector(joined, state="visible",
                                        timeout=self.nav_timeout)
            return True
        except Exception:
            return False

    def open(self, job):
        self.page.goto(job.url, wait_until="domcontentloaded")
        # The job page has an Apply button before the wizard exists; "Apply
        # Manually" is preferred over Workday's own resume parser, which fills
        # badly and then has to be corrected field by field.
        # Wait for the Apply control *or* the form itself: a direct apply link
        # (and the test fixture) has no Apply button, and waiting on one that
        # will never appear would burn the whole timeout before discovery.
        if self._settle(self.APPLY_SELECTORS + self.LANDED_SELECTORS):
            btn = query_first(self.page, self.APPLY_SELECTORS)
            if btn is not None:
                try:
                    btn.click()
                except Exception:
                    pass
                # Apply either opens the manual/autofill choice or goes straight
                # into the flow, so wait for whichever of those turns up.
                self._settle(self.MANUAL_SELECTORS + self.LANDED_SELECTORS)

        manual = query_first(self.page, self.MANUAL_SELECTORS)
        if manual is not None:
            try:
                manual.click()
            except Exception:
                pass
            self._settle(self.LANDED_SELECTORS)

    def discover(self) -> list[Field]:
        """Walk formField containers; each yields exactly one logical field."""
        fields: list[Field] = []
        for idx, box in enumerate(self.page.query_selector_all(FORM_FIELD)):
            try:
                if not box.is_visible():
                    continue
            except Exception:
                continue

            automation_id = box.get_attribute("data-automation-id") or f"formField-{idx}"
            fid = automation_id.replace("formField-", "") or f"field-{idx}"
            label = self._label_for(box, automation_id)
            required = "*" in label or bool(box.query_selector("[aria-required='true']"))

            listbox = box.query_selector("button[aria-haspopup='listbox']")
            file_zone = box.query_selector("div[data-automation-id='file-upload-drop-zone']")
            textarea = box.query_selector("textarea")
            radios = box.query_selector_all("input[type=radio]")
            checkboxes = box.query_selector_all("input[type=checkbox]")

            if file_zone is not None or box.query_selector("input[type=file]"):
                kind, selector = "file", f"{FORM_FIELD}[data-automation-id='{automation_id}'] input[type=file]"
            elif listbox is not None:
                kind = "select"
                selector = (f"{FORM_FIELD}[data-automation-id='{automation_id}'] "
                            "button[aria-haspopup='listbox']")
            elif textarea is not None:
                kind, selector = "textarea", f"{FORM_FIELD}[data-automation-id='{automation_id}'] textarea"
            elif len(radios) >= 2:
                # The inputs, not the container: the group readback checks each
                # member's .checked, and handed a div it finds none and falls
                # back to the container's text -- which is the question. That is
                # why a correctly selected "No" verified as the question itself.
                kind = "radio"
                selector = (f"{FORM_FIELD}[data-automation-id='{automation_id}'] "
                            "input[type=radio]")
            elif len(checkboxes) == 1:
                kind, selector = "checkbox", f"{FORM_FIELD}[data-automation-id='{automation_id}'] input[type=checkbox]"
            else:
                kind, selector = "text", f"{FORM_FIELD}[data-automation-id='{automation_id}'] input"

            # Without the real choices the model cannot answer a question the
            # profile does not cover -- "Phone Device Type" stayed unknown every
            # run because nothing ever told anyone that Mobile was on offer.
            options: list[str] = []
            if kind == "radio":
                # Read from the labels already in the DOM: no interaction, so
                # no risk of disturbing the control.
                options = self._radio_labels(box)
            elif kind == "select" and match_rule(label) is None:
                # Opening a dropdown is the only way to see its choices, and
                # doing it to every dropdown on the page disturbs them -- the
                # ones a rule can already answer came back unfillable. So pay
                # that cost only where the answer is otherwise unknown, which
                # is exactly the case the options are needed for.
                options = self._listbox_options(box)

            fields.append(Field(id=fid, selector=selector, label=label,
                                kind=kind, required=required, options=options))
        return fields

    def _radio_labels(self, box) -> list[str]:
        """The choices in a radio group, read without touching the page."""
        labels: list[str] = []
        for radio in box.query_selector_all("input[type=radio]"):
            text = ""
            rid = radio.get_attribute("id") or ""
            if rid:
                lab = box.query_selector(f"label[for='{rid}']")
                if lab is not None:
                    text = (lab.inner_text() or "").strip()
            if not text:
                text = (radio.get_attribute("value") or "").strip()
            if text and text not in labels:
                labels.append(text)
        return labels

    def _listbox_options(self, box) -> list[str]:
        """Open a Workday dropdown far enough to read it, then close it.

        The options are rendered only while it is open, so there is no way to
        know them at discovery time without opening it.
        """
        button = box.query_selector("button[aria-haspopup='listbox']")
        if button is None:
            return []
        try:
            button.click()
            self.page.wait_for_timeout(450)
            texts = []
            for el in self.page.query_selector_all(
                    "li[role=option], div[data-automation-id='promptOption'], [role=option]"):
                try:
                    if not el.is_visible():
                        continue
                except Exception:
                    continue
                text = (el.inner_text() or "").strip()
                if text and text not in texts:
                    texts.append(text)
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(150)
            return texts
        except Exception:
            return []

    def _label_for(self, box, automation_id: str) -> str:
        for sel in ("label", "legend", "[id$='-label']"):
            el = box.query_selector(sel)
            if el is not None:
                text = (el.inner_text() or "").strip()
                if text:
                    return re.sub(r"\s+", " ", text)
        # Fall back to the automation id itself: "formField-legalNameSection_firstName"
        # still carries the field's meaning for the mapper to match on.
        return re.sub(r"[-_]+", " ", automation_id.replace("formField-", "")).strip()

    def _write(self, f: Field, value: str) -> Optional[str]:
        """Workday's dropdowns and radios are not native controls."""
        if f.kind == "select":
            button = self.page.query_selector(f.selector)
            if button is None:
                return None
            button.click()
            self.page.wait_for_timeout(600)

            # Options only exist once the listbox is open, so they can't be known
            # at discovery time. Resolve against the live texts using the same
            # matcher the mapper uses, so "Decline to self-identify" still finds
            # "Decline To Self Identify".
            from ..mapper import resolve_option

            elements = self.page.query_selector_all(
                "li[role=option], div[data-automation-id='promptOption'], [role=option]")
            options = []
            for el in elements:
                try:
                    if not el.is_visible():
                        continue
                except Exception:
                    continue
                text = (el.inner_text() or "").strip()
                if text:
                    options.append((el, text))

            chosen = resolve_option(value, [t for _, t in options])
            if chosen is not None:
                for el, text in options:
                    if text == chosen:
                        el.click()
                        self.page.wait_for_timeout(300)
                        return text
            button.click()          # close the listbox we opened
            return None

        # Radio groups fall through to the base worker. It matches the answer
        # against the members' labels with the same resolver the options were
        # read with, so "No" still finds "No" and a long-form option still
        # matches a short profile answer -- this override only did exact
        # case-insensitive equality and returned None for everything else.
        return super()._write(f, value)
