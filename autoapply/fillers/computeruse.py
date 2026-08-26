"""A vision loop: screenshot, decide, click or type, look again.

The other contenders read the page's markup. This one only looks at it, which
is the whole question worth testing -- every failure on the last branch was a
markup-reading failure that a person looking at the screen would not have made.
A checkbox group discovered as a text box, a dropdown whose options do not
exist until it is open, a translation string mistaken for a CAPTCHA: none of
those are visible to someone with a screenshot, because none of them are real.

The cost is the other side of it. A screenshot is thousands of tokens per step
where a DOM diff is dozens, so this is expected to win on capability and lose
on price. That trade is exactly what the bake-off is for.
"""
from __future__ import annotations

import base64
import json
import os

from .. import log as _log
from ..models import Job
from . import FillReport, register

log = _log.get("filler.computeruse")

SYSTEM = (
    "You are filling in a job application by looking at screenshots of it.\n"
    "Each turn you get the screenshot and the candidate's profile, and you "
    "reply with ONE action as JSON:\n"
    '  {"action":"click","x":<px>,"y":<px>,"why":"..."}\n'
    '  {"action":"type","text":"...","why":"..."}     (into whatever is focused)\n'
    '  {"action":"key","key":"Tab|Enter|Escape","why":"..."}\n'
    '  {"action":"done","why":"reached the review step"}\n'
    "Rules:\n"
    "- Coordinates are pixels from the top-left of the image you were given.\n"
    "- Never invent a fact about the candidate. Where the profile does not "
    "answer a question and a decline-to-answer option exists, choose it.\n"
    "- Advance a step with Save and Continue / Next when the step is complete.\n"
    "- STOP with done when you reach a review or summary step. Never click "
    "Submit or Apply: this is a rehearsal, not a real application."
)


class ComputerUseFiller:
    name = "computeruse"

    def available(self):
        if os.getenv("LLM_PROVIDER", "rules") == "rules":
            return False, "needs a model (LLM_PROVIDER=rules)"
        model = os.getenv("OPENAI_MODEL", "")
        if model and "mini" in model:
            # Worth saying plainly rather than scoring it badly: a small model
            # given only pixels misplaces clicks, and that is a fact about the
            # model rather than about looking-instead-of-reading.
            log.info("%s is a small model; vision clicking will be weak", model)
        return True, ""

    def fill(self, page, job: Job, profile: dict, on_step=None) -> FillReport:
        from ..llm import get_provider

        report = FillReport(filler=self.name, job=job)
        provider = get_provider()
        max_steps = int(os.getenv("VISION_MAX_STEPS", "25"))

        for step in range(max_steps):
            shot = page.screenshot(type="png")
            b64 = base64.b64encode(shot).decode()
            try:
                raw = provider._chat_vision(
                    SYSTEM,
                    "Candidate profile:\n" + json.dumps(profile, indent=2),
                    b64)
            except AttributeError:
                report.errors.append(
                    "the configured provider has no vision call; add "
                    "_chat_vision to use this contender")
                return report
            except Exception as exc:
                report.errors.append(f"{type(exc).__name__}: {str(exc)[:110]}")
                return report

            start, end = raw.find("{"), raw.rfind("}")
            if start < 0:
                report.errors.append("model replied without JSON")
                break
            try:
                act = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                report.errors.append("model replied with bad JSON")
                break

            kind = act.get("action")
            log.debug("step %d: %s (%s)", step + 1, kind,
                      _log.brief(act.get("why"), 60))
            if kind == "done":
                report.reached_review = True
                break
            try:
                if kind == "click":
                    page.mouse.click(float(act["x"]), float(act["y"]))
                elif kind == "type":
                    page.keyboard.type(str(act.get("text", "")), delay=15)
                elif kind == "key":
                    page.keyboard.press(str(act.get("key", "Tab")))
                else:
                    report.errors.append(f"unknown action {kind!r}")
                    break
            except Exception as exc:
                report.errors.append(f"{kind} failed: {str(exc)[:80]}")
                break
            page.wait_for_timeout(700)
            report.steps_advanced = step + 1

        return report


register("computeruse", ComputerUseFiller)
