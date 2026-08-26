"""The incumbent: read the DOM, map each field, write each value.

Kept as a contender rather than deleted, because "which does best" has no
answer without the thing being replaced in the comparison. It is also the only
contender that is free -- rules answer most of a form with no model call at all
-- so if it loses narrowly on coverage it may still win on cost.
"""
from __future__ import annotations

import os

from .. import log as _log
from ..models import Job
from . import FillReport, register

log = _log.get("filler.dom")


class DomFiller:
    name = "dom"

    def available(self):
        return True, ""

    def fill(self, page, job: Job, profile: dict, on_step=None) -> FillReport:
        from .. import mapper
        from ..llm import get_provider
        from ..store import Store
        from ..workers import get_worker

        report = FillReport(filler=self.name, job=job)
        provider = get_provider()
        store = Store(os.getenv("DB_PATH", "data/autoapply.sqlite"))
        worker = get_worker(job.ats, page) or get_worker("generic", page)

        # A wizard walks itself; a single-page form is one pass.
        if hasattr(worker, "max_steps"):
            outcome = worker.run(job, profile, store, provider, "")
            report.fields_found = len(outcome.fields)
            report.reached_review = outcome.reached_end
            report.steps_advanced = max(0, len(
                {f.frame_url for f in outcome.fields}) - 1)
            report.answers = {m.label or m.field_id: str(m.value)
                              for m in outcome.mappings
                              if m.action in ("fill", "generate") and m.value}
            report.errors.extend(outcome.errors[:5])
            return report

        fields = worker.discover()
        report.fields_found = len(fields)
        mappings = mapper.map_fields(fields, profile, job.ats, store=store,
                                     provider=provider)
        outcome = worker.fill(job, fields, mappings, "")
        worker._review_and_repair(job, fields, mappings, outcome, profile,
                                  store, provider)
        report.answers = {m.label or m.field_id: str(m.value) for m in mappings
                          if m.action in ("fill", "generate") and m.value}
        report.errors.extend(outcome.errors[:5])
        return report


register("dom", DomFiller)
