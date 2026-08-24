"""URL -> ATS detection. A plain data table so a new ATS is a one-line change."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .models import ATS, Job

# (host regex, ats). First match wins.
REGISTRY: list[tuple[str, ATS]] = [
    (r"(^|\.)(job-boards|boards)\.greenhouse\.io$", "greenhouse"),
    (r"(^|\.)greenhouse\.io$",                      "greenhouse"),
    (r"(^|\.)lever\.co$",                           "lever"),
    (r"(^|\.)ashbyhq\.com$",                        "ashby"),
    (r"\.myworkdayjobs\.com$",                      "workday"),
    (r"(^|\.)icims\.com$",                          "icims"),
    # JPMorgan and most large banks run Oracle Cloud HCM; Taleo is its older sibling.
    (r"\.fa\.oraclecloud\.com$|\.ocs\.oraclecloud\.com$", "oracle"),
    (r"(^|\.)oraclecloud\.com$",                    "oracle"),
    (r"(^|\.)taleo\.net$",                          "oracle"),
]

# ATSs with a dedicated DOM worker: cheap, deterministic, no model needed.
DOM_WORKERS: set[ATS] = {"greenhouse", "lever", "workday"}

# Everything else is driven by the browser-use agent with a per-ATS playbook.
# That path needs an LLM; without one configured it queues with a clear reason
# rather than guessing at selectors.
AGENT_ATS: set[ATS] = {"icims", "ashby", "oracle", "unknown"}

SUPPORTED: set[ATS] = DOM_WORKERS | AGENT_ATS


def detect(url: str) -> ATS:
    host = (urlparse(url).hostname or "").lower()
    for pattern, ats in REGISTRY:
        if re.search(pattern, host):
            return ats
    return "unknown"


def normalize(url: str, ats: ATS) -> str:
    """Point at the application form rather than the job description."""
    base = url.split("?")[0].rstrip("/")
    if ats == "lever" and not base.endswith("/apply"):
        return base + "/apply"
    return base


def parse_job(url: str) -> Job:
    ats = detect(url)
    parts = [p for p in urlparse(url).path.split("/") if p]
    company = ""
    if ats == "lever" and parts:
        company = parts[0]
    elif ats == "greenhouse" and parts:
        company = parts[0]
    elif ats == "workday":
        company = (urlparse(url).hostname or "").split(".")[0]
    return Job(url=normalize(url, ats), ats=ats, company=company)
