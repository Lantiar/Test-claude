"""browser-use, attached to the page the harness already prepared.

Attached rather than launched: browser-use opening its own browser is what made
it useless on Workday last branch. It landed on a logged-out job posting, could
not see the five signed-in steps the DOM lane had walked, reported the whole
form missing, and spent the token budget the other tiers needed doing it. Over
CDP it inherits the session, the consent dismissal and the wizard position.
"""
from __future__ import annotations

import asyncio
import os

from .. import log as _log
from ..models import Job
from . import FillReport, register

log = _log.get("filler.browseruse")

TASK = """Fill in this job application from the candidate profile below.

Rules:
- Answer every question you can from the profile. Never invent a fact about the
  candidate: no employer, date, degree, salary or authorization that is not
  below. If a question is not answerable from the profile and offers a
  decline-to-answer option, choose that.
- Work through the whole form from the top, scrolling down as you go. Most
  applications are several screens long, and the section in front of you when
  you start is not the only one.
- Advance through the form's steps (Save and Continue / Next) as you finish
  each one.
- STOP only when every question you can answer is answered. A visible Submit
  or Apply button does not mean you are finished: a single-page application
  shows Submit from the moment it loads, below an empty form.
- Do NOT click Submit, Apply, or anything that sends the application. This is
  a rehearsal.

Candidate profile:
{profile}
"""


class BrowserUseFiller:
    name = "browseruse"

    def available(self):
        try:
            import browser_use  # noqa: F401
        except Exception as exc:
            return False, f"browser-use not importable: {exc}"
        if os.getenv("LLM_PROVIDER", "rules") == "rules":
            return False, "needs a model (LLM_PROVIDER=rules)"
        return True, ""

    def fill(self, page, job: Job, profile: dict, on_step=None) -> FillReport:
        import json

        from browser_use import Agent, Browser

        from ..workers.agent import build_llm

        report = FillReport(filler=self.name, job=job)
        cdp = os.getenv("AUTOAPPLY_CDP_URL", "")
        if not cdp:
            report.errors.append(
                "no CDP endpoint: the harness must launch the browser with "
                "--remote-debugging-port for this contender to share its page")
            return report

        async def go():
            browser = Browser(cdp_url=cdp)
            agent = Agent(
                task=TASK.format(profile=json.dumps(profile, indent=2)),
                llm=build_llm(), browser=browser)
            # A wizard is seven steps and each one takes the agent several
            # actions: reading the page, opening a dropdown, picking, saving.
            # At 40 it ran out on Voluntary Disclosures -- "final response
            # because the step budget is exhausted" -- six steps in and still
            # working, and was scored as though it had failed.
            return await agent.run(
                max_steps=int(os.getenv("AGENT_MAX_STEPS", "150")))

        # In its own thread with its own loop. The harness holds the page
        # through Playwright's *sync* API, which is itself driven by a running
        # event loop, so asyncio.run() here raises "cannot be called from a
        # running event loop" and the contender scores zero having never
        # started. browser-use reaches the browser over CDP rather than through
        # that page object, so a separate thread is free to drive it.
        import threading

        box: dict = {}

        def runner():
            try:
                box["history"] = asyncio.run(go())
            except Exception as exc:            # carried back across the thread
                box["error"] = f"{type(exc).__name__}: {str(exc)[:140]}"

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join(timeout=float(os.getenv("AGENT_TIMEOUT", "1800")))
        if thread.is_alive():
            report.errors.append("agent did not finish within AGENT_TIMEOUT")
            return report
        if "error" in box:
            report.errors.append(box["error"])
            return report
        history = box.get("history")

        try:
            report.steps_advanced = len(getattr(history, "history", []) or [])
        except Exception:
            pass
        return report


register("browseruse", BrowserUseFiller)
