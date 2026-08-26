"""Skyvern, embedded, driving the browser the harness prepared.

Ruled out too early once already: the class exposed by `skyvern` is a client
that wants an API key, so it looked like it needed a server and a Postgres
stack. It does not -- Skyvern.local(use_in_memory_db=True) starts the whole
thing in-process over an ASGI transport against SQLite in memory. Reading the
constructor signature was not the same as reading the package.

Worth having in the comparison because it is the one contender built for this
exact problem rather than adapted to it: Skyvern exists to fill web forms, and
if buying beats building anywhere it should be here.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading

from .. import log as _log
from ..models import Job
from . import FillReport, register

log = _log.get("filler.skyvern")

TASK = """Fill in this job application from the candidate profile below.

Answer every question you can from the profile. Never invent a fact about the
candidate -- no employer, date, degree, salary or authorization that is not
below. Where a question is not answerable from the profile and the field
offers a decline-to-answer option, choose that.

Advance through the form's steps as you complete them.

STOP when you reach the review or summary step. Do NOT click Submit or Apply:
this is a rehearsal and the application must not be sent.

Candidate profile:
{profile}
"""


class SkyvernFiller:
    name = "skyvern"

    def available(self):
        try:
            from skyvern.library.skyvern import Skyvern  # noqa: F401
        except Exception as exc:
            return False, f"skyvern not importable: {str(exc)[:80]}"
        try:
            import skyvern.library.embedded_server_factory  # noqa: F401
        except Exception:
            return False, 'needs `pip install "skyvern[local]"`'
        if not os.getenv("OPENAI_API_KEY"):
            return False, "needs OPENAI_API_KEY"
        return True, ""

    def fill(self, page, job: Job, profile: dict, on_step=None) -> FillReport:
        from skyvern.library.skyvern import Skyvern
        from skyvern.schemas.llm import LLMConfig

        report = FillReport(filler=self.name, job=job)
        cdp = os.getenv("AUTOAPPLY_CDP_URL", "")
        if not cdp:
            report.errors.append(
                "no CDP endpoint: set AUTOAPPLY_CDP_PORT so this contender "
                "can drive the page the harness signed in on")
            return report

        model = os.getenv("SKYVERN_MODEL", os.getenv("VISION_MODEL", "gpt-4o"))

        async def go():
            sky = await _maybe_await(Skyvern.local(
                use_in_memory_db=True,
                llm_config=LLMConfig(model_name=model,
                                     required_env_vars=["OPENAI_API_KEY"],
                                     supports_vision=True,
                                     add_assistant_prefix=False),
                # Attach, do not launch. Left to itself Skyvern starts its own
                # browser and dies in under a second on
                # "Executable doesn't exist at .../chromium-1234/", because its
                # pinned playwright wants a build this container does not have
                # -- the same version pin that cost a run on our own launches.
                # cdp-connect also happens to be the whole point: the browser it
                # attaches to is the one already signed in and standing on the
                # form.
                settings={
                    "BROWSER_TYPE": "cdp-connect",
                    "BROWSER_REMOTE_DEBUGGING_URL": cdp,
                    "CHROME_EXECUTABLE_PATH": _chromium(),
                    "MAX_STEPS_PER_RUN": int(os.getenv("SKYVERN_MAX_STEPS", "40")),
                },
            ))
            try:
                return await sky.run_task(
                    prompt=TASK.format(profile=json.dumps(profile, indent=2)),
                    url=page.url,
                    max_steps=int(os.getenv("SKYVERN_MAX_STEPS", "30")),
                    wait_for_completion=True,
                )
            finally:
                await sky.aclose()

        # Its own thread and loop, for the same reason browser-use needs one:
        # the harness holds this page through Playwright's sync API, which is
        # itself driven by a running event loop.
        box: dict = {}

        def runner():
            try:
                box["result"] = asyncio.run(go())
            except Exception as exc:
                box["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join(timeout=float(os.getenv("AGENT_TIMEOUT", "900")))
        if thread.is_alive():
            report.errors.append("skyvern did not finish within AGENT_TIMEOUT")
            return report
        if "error" in box:
            report.errors.append(box["error"])
            return report

        result = box.get("result")
        status = str(getattr(result, "status", "") or "")
        log.info("skyvern finished: %s", status)
        report.steps_advanced = len(getattr(result, "steps", []) or [])
        if "complete" in status.lower():
            report.reached_review = True
        return report


def _chromium() -> str:
    """The browser this machine actually has, for any path Skyvern still
    launches through."""
    from ..browser import find_chromium

    return find_chromium()


async def _maybe_await(value):
    """Skyvern.local() is sync in some versions and a coroutine in others."""
    if asyncio.iscoroutine(value):
        return await value
    return value


register("skyvern", SkyvernFiller)
