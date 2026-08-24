"""Workday: multi-page wizard keyed off data-automation-id.

Selector conventions are Workday's own and are stable across tenants; they were
cross-checked against berellevy/job_app_filler, which drives the same markup.
Each field lives in a div[data-automation-id^="formField-"], so discovery walks
those containers rather than raw inputs — that is what gives us the label and the
control type together.
"""
from __future__ import annotations

import re

from typing import Optional

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
    auth_selectors = (
        "button[data-automation-id='createAccountLink']",
        "button[data-automation-id='signInLink']",
        "div[data-automation-id='createAccountPage']",
        "input[data-automation-id='password']",
    )
    confirm_patterns = (
        r"thank you for applying",
        r"your application (has been|was) submitted",
        r"application (was )?(received|submitted)",
        r"we('| ha)ve received your application",
    )

    def open(self, job):
        super().open(job)
        # The job page has an Apply button before the wizard exists; "Apply
        # Manually" is preferred over Workday's own resume parser, which fills
        # badly and then has to be corrected field by field.
        for sel in ("a[data-automation-id='adventureButton']",
                    "button[data-automation-id='adventureButton']",
                    "a:has-text('Apply')", "button:has-text('Apply')"):
            btn = query_first(self.page, (sel,))
            if btn is not None:
                try:
                    btn.click()
                    self.page.wait_for_timeout(2000)
                except Exception:
                    pass
                break
        manual = query_first(self.page, ("a[data-automation-id='applyManually']",
                                         "button:has-text('Apply Manually')"))
        if manual is not None:
            try:
                manual.click()
                self.page.wait_for_timeout(2000)
            except Exception:
                pass

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
                kind, selector = "radio", f"{FORM_FIELD}[data-automation-id='{automation_id}']"
            elif len(checkboxes) == 1:
                kind, selector = "checkbox", f"{FORM_FIELD}[data-automation-id='{automation_id}'] input[type=checkbox]"
            else:
                kind, selector = "text", f"{FORM_FIELD}[data-automation-id='{automation_id}'] input"

            fields.append(Field(id=fid, selector=selector, label=label,
                                kind=kind, required=required))
        return fields

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

        if f.kind == "radio":
            box = self.page.query_selector(f.selector)
            if box is None:
                return None
            for radio in box.query_selector_all("input[type=radio]"):
                rid = radio.get_attribute("id") or ""
                label = box.query_selector(f"label[for='{rid}']") if rid else None
                text = (label.inner_text() if label else "") or radio.get_attribute("value") or ""
                if text.strip().lower() == value.strip().lower():
                    radio.check()
                    return text.strip()
            return None

        return super()._write(f, value)
