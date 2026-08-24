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
]

# Only these have a dedicated single-page worker today. Everything else needs
# the agent path (not built yet), so it queues rather than guessing.
SUPPORTED: set[ATS] = {"greenhouse", "lever"}


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
