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

from ..clicking import click as _click
from ..mapper import match_rule
from ..models import Field
from .base import WizardWorker, query_first

FORM_FIELD = "div[data-automation-id^='formField-']"

# The prompt sitting at the top of a dropdown, which is not one of its choices.
PLACEHOLDER_OPTION = re.compile(r"^(select|choose)\b|^-{2,}|^please\s+select", re.I)


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
            # Workday's other picklist: a search box that commits a choice as a
            # removable chip. Typing into it only sets the search text, so the
            # value shows on screen, reads back fine, and the form still says
            # the field is required -- which is exactly what "How Did You Hear
            # About Us?" did on every run. It needs click, type, pick.
            multiselect = box.query_selector(
                "[data-automation-id='multiSelectContainer']")
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
            elif multiselect is not None:
                kind = "combobox"
                selector = (f"{FORM_FIELD}[data-automation-id='{automation_id}'] "
                            "input")
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
            elif kind == "combobox" and (required or match_rule(label) is None):
                # Read these even when a rule answers them, because the rule's
                # answer is often not on the menu: the profile says the source
                # was a "Company website" and this tenant offers only Job Board,
                # Talent Acquisition Team and University/College. Without the
                # list nothing can tell that the answer is unusable, so the
                # write silently fails and the step never validates.
                options = self._multiselect_options(box)
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

    # Workday prompts nest. "How Did You Hear About Us?" offers Job Board,
    # Talent Acquisition Team and University/College, and none of those is an
    # answer: clicking one drills into the boards underneath it. Reading the top
    # level and calling those the options meant the model was choosing between
    # category names, so even a sensible pick could never satisfy the field.
    # Options are therefore collected as "Category > Leaf" and written by
    # walking the same path.
    PATH_SEP = " > "
    MAX_MENU_DEPTH = 3

    def _visible_options(self, sel: str, exclude: set[str] | None = None) -> list:
        """Visible options, minus any that were already on screen.

        Scoping by exclusion rather than by container: Workday keeps several
        prompt menus mounted at once, and the Country Phone Code picker sits
        directly beside this one with its selection rendered as a promptOption
        of its own. Reading them all produced a list that began "United States
        of America (+1) > Job Board" and then drilled into every country on
        earth. Whatever was on screen before this widget opened is not one of
        its choices.
        """
        exclude = exclude or set()
        out = []
        for opt in self.page.query_selector_all(f"[data-automation-id='{sel}']"):
            try:
                if not opt.is_visible():
                    continue
            except Exception:
                continue
            text = (opt.inner_text() or "").strip()
            if text and text not in exclude and not PLACEHOLDER_OPTION.match(text):
                out.append((opt, text))
        return out

    def _options_on_screen(self) -> set[str]:
        return {t for _, t in self._visible_options("promptOption")}

    def _multiselect_options(self, box) -> list[str]:
        """Every selectable answer, including ones a level down.

        A leaf reachable only by opening a category is still an answer to this
        question, and the model cannot pick what it was never shown.
        """
        el = box.query_selector("input")
        if el is None:
            return []
        try:
            # Anything already showing belongs to a neighbouring picker.
            outside = self._options_on_screen()
            if not _click(el):
                return []
            self.page.wait_for_timeout(700)

            top = [t for _, t in self._visible_options("promptOption", outside)]
            leaves = {t for _, t in self._visible_options("promptLeafNode", outside)}
            paths: list[str] = []

            for text in top:
                if text in leaves:
                    paths.append(text)          # selectable where it stands
                    continue
                # A category: open it, take what is underneath, come back.
                match = [o for o, t in self._visible_options("promptOption", outside)
                         if t == text]
                if not match or not _click(match[0]):
                    continue
                self.page.wait_for_timeout(900)
                for _, child in self._visible_options("promptLeafNode",
                                                      outside | {text}):
                    paths.append(f"{text}{self.PATH_SEP}{child}")
                # Reopen at the top for the next category.
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(250)
                if not _click(el):
                    break
                self.page.wait_for_timeout(700)

            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)
            return paths or top
        except Exception:
            return []

    # Workday's own search-and-select, reached by calling the React handler the
    # input already has. Adopted from berellevy/job_app_filler, which the
    # selector conventions in this file were already cross-checked against --
    # its DropdownSearchable does this and notes "the dropdown doesn't need to
    # be opened".
    #
    # It is better than clicking through the menu in every way that has cost
    # this project time: Workday resolves a nested answer itself, so the
    # category/leaf walk is unnecessary; nothing is clicked, so the click_filter
    # overlay is irrelevant; and no menu is opened, so there is no neighbouring
    # popup to accidentally read.
    REACT_PICK_JS = r"""
    ([el, value]) => {
      let props = null;
      for (const k in el) { if (k.startsWith('__reactProps')) { props = el[k]; break; } }
      if (!props || typeof props.onKeyDown !== 'function') return 'no-handler';
      props.onKeyDown({key: 'Tab', target: {value: value}});
      return 'dispatched';
    }
    """

    def _react_pick(self, f: Field, value: str) -> Optional[str]:
        """Ask Workday to select `value` itself. None if it did not take."""
        box = self.page.query_selector(
            f"{FORM_FIELD}[data-automation-id='formField-{f.id}']")
        if box is None:
            return None
        el = box.query_selector("input")
        if el is None:
            return None
        try:
            if self.page.evaluate(self.REACT_PICK_JS, [el, value]) != "dispatched":
                return None
        except Exception:
            return None
        self.page.wait_for_timeout(700)
        # The chosen item appears in the widget's own selected list. Reading
        # that rather than the input is the point -- the input holds the search
        # text, which is what made a typed value look filled when it was not.
        chip = box.query_selector("[data-automation-id='selectedItem'], "
                                  "ul[data-automation-id='selectedItemList'] li")
        got = (chip.inner_text() or "").strip() if chip is not None else ""
        if not got:
            return None

        # Workday's search is fuzzy and will confidently select the wrong
        # entry: asked for "United States of America (+1)" it chose "American
        # Samoa (+1)". Handing that back would put a wrong answer on the form
        # that the form is perfectly happy to accept. Asked for "Job Board" it
        # chose "University Job Board", which is the nested leaf and correct --
        # so the test is whether one contains the other, not equality.
        want, low = value.strip().lower(), got.lower()
        if want and low and (want in low or low in want):
            return got

        # Wrong pick: clear it, so the fallback does not stack a second
        # selection on top of a bad one.
        remove = box.query_selector("[data-automation-id='DELETE_charm'], "
                                    "[data-automation-id='selectedItem'] button")
        if remove is not None:
            _click(remove)
            self.page.wait_for_timeout(400)
        return None

    def _write_multiselect(self, f: Field, value: str) -> Optional[str]:
        """Select a value that may sit behind one or more categories."""
        el = self.page.query_selector(f.selector)
        if el is None:
            return None
        outside = self._options_on_screen()
        if not _click(el):
            return None
        self.page.wait_for_timeout(700)

        from ..mapper import resolve_option

        # Let Workday do it first; fall back to walking the menu by hand.
        if committed := self._react_pick(f, value):
            return committed

        def chip() -> str:
            """The committed selection, which is the only proof of success."""
            el = self.page.query_selector(
                f"{FORM_FIELD}[data-automation-id='formField-{f.id}'] "
                "[data-automation-id='selectedItem']")
            return (el.inner_text() or "").strip() if el is not None else ""

        # The markup does not say which options select and which descend --
        # promptLeafNode marks all three of these as leaves, and Job Board
        # descends anyway. So click and look: a chip means done, fresh options
        # mean we went a level deeper and should carry on.
        wanted = [s.strip() for s in value.split(self.PATH_SEP) if s.strip()]
        seen = set(outside)
        walked: list[str] = []

        for depth in range(self.MAX_MENU_DEPTH):
            available = self._visible_options("promptOption", seen)
            if not available:
                break
            target = wanted[depth] if depth < len(wanted) else (
                wanted[-1] if wanted else "")
            chosen = resolve_option(target, [t for _, t in available]) if target else None
            # Nothing matched by name: at the top that is a real failure, but a
            # level down it just means the menu is narrower than the answer --
            # a single remaining choice is the answer.
            match = [o for o, t in available if t == chosen] if chosen else (
                [available[0][0]] if depth and len(available) == 1 else [])
            if not match:
                break
            seen |= {t for _, t in available}
            walked.append(next(t for o, t in available if o == match[0]))
            if not _click(match[0]):
                break
            self.page.wait_for_timeout(900)
            if committed := chip():
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(200)
                # Two different answers are wanted here, and returning one for
                # both marked a correctly filled field as missing:
                #
                #   verify FAIL How Did You Hear About Us?*
                #               want=Job Board got=1 item selected, Handshake
                #
                # Clicking the category Job Board leaves a chip reading
                # Handshake, so verification has to compare against the chip.
                # Teaching needs the opposite -- the route, since Handshake is
                # not on the top-level menu and replaying it would find
                # nothing. So the route is stashed and the chip is returned.
                if self.pending_paths is None:
                    self.pending_paths = {}
                self.pending_paths[f.id] = self.PATH_SEP.join(walked)
                return committed

        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(200)
        return chip() or None

    def _listbox_options(self, box) -> list[str]:
        """Open a Workday dropdown far enough to read it, then close it.

        The options are rendered only while it is open, so there is no way to
        know them at discovery time without opening it.
        """
        button = box.query_selector("button[aria-haspopup='listbox']")
        if button is None:
            return []
        try:
            if not _click(button):
                return []
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
                # "Select One" is the prompt, not a choice. Offered as one, the
                # model picked it for Phone Device Type and the form answered
                # "The entered value is not one of the options provided" -- and
                # it would have been taught as the answer for every later run.
                if text and not PLACEHOLDER_OPTION.match(text) and text not in texts:
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
        # The automation id usually carries the meaning --
        # "formField-legalNameSection_firstName" is perfectly readable. But a
        # tenant's own questions are keyed by uuid, and "802e7dac252910014cf42"
        # tells the mapper nothing: the audit's verdict on one was "field label
        # is not meaningful", which was exactly right. Read the question off the
        # page instead.
        derived = re.sub(r"[-_]+", " ", automation_id.replace("formField-", "")).strip()
        if not re.fullmatch(r"[0-9a-f]{16,}", derived.replace(" ", "")):
            return derived
        try:
            nearby = box.evaluate(
                r"""b => {
                     const clean = s => (s || '').replace(/\s+/g, ' ').trim();
                     let n = b, hops = 0;
                     while (n && hops++ < 4) {
                       const t = clean(n.innerText);
                       if (t && t.length > 3) return t.split('\n')[0].slice(0, 160);
                       n = n.parentElement;
                     }
                     return '';
                   }""") or ""
        except Exception:
            nearby = ""
        return nearby.strip() or derived

    def _write(self, f: Field, value: str) -> Optional[str]:
        """Workday's dropdowns and radios are not native controls."""
        if f.kind == "select":
            button = self.page.query_selector(f.selector)
            if button is None:
                return None
            # Workday paints a click_filter div over this button; a plain
            # click waits 30s for an obstruction that never clears.
            if not _click(button):
                return None
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
                        _click(el)
                        self.page.wait_for_timeout(300)
                        return text
            _click(button)          # close the listbox we opened
            return None

        if f.kind == "combobox":
            return self._write_multiselect(f, value)

        # Radio groups fall through to the base worker. It matches the answer
        # against the members' labels with the same resolver the options were
        # read with, so "No" still finds "No" and a long-form option still
        # matches a short profile answer -- this override only did exact
        # case-insensitive equality and returned None for everything else.
        return super()._write(f, value)
