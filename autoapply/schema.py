"""The form's own schema, from the ATS that serves it.

Discovery reads a rendered page and infers what each control is for. That is
necessary where nothing better exists, and it is where most of this project's
bugs have lived: a question named after its own first option, a required flag
that was never in the DOM, an option list that only exists once a menu is
open, an upload widget that turned out to autofill the form.

Some ATSs publish the answer. Greenhouse serves every board's questions as
JSON, unauthenticated -- label, type, required, the option list, and the
`name` attribute the input carries in the page, which is what lets a schema
entry be matched to a control with no guessing at all.

So: ask the ATS first, believe it over the DOM, and fall back to reading the
page where no such source exists. Lever's public endpoint returns posting
metadata but not the application's questions, and Ashby's form is not exposed
this way, so both stay on discovery.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

from . import log as _log

TIMEOUT = float(os.getenv("SCHEMA_TIMEOUT", "12"))

# board slug and posting id out of a Greenhouse application URL.
_GREENHOUSE = re.compile(
    r"^(?:job-boards|boards)\.greenhouse\.io$", re.I)

# Greenhouse's own type names -> the kinds discovery uses.
_KINDS = {
    "input_text": "text",
    "textarea": "textarea",
    "input_file": "file",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "checkbox",
    "boolean": "checkbox",
}


def _get(url: str) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "autoapply"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except Exception as exc:
        _log.get("schema").info("%s: %s", type(exc).__name__, exc)
        return None


def _greenhouse(host: str, path: str) -> dict[str, dict]:
    m = re.match(r"/([^/]+)/jobs/(\d+)", path)
    if not m:
        return {}
    board, job = m.group(1), m.group(2)
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{board}"
                f"/jobs/{job}?questions=true")
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for q in data.get("questions") or []:
        label = (q.get("label") or "").strip()
        required = bool(q.get("required"))
        for f in q.get("fields") or []:
            name = (f.get("name") or "").strip()
            if not name or not label:
                continue
            values = [str(v.get("label") or v.get("value") or "").strip()
                      for v in (f.get("values") or [])]
            out[name] = {
                "label": label,
                "required": required,
                "kind": _KINDS.get(f.get("type") or "", ""),
                "options": [v for v in values if v],
            }
    return out


def for_job(url: str) -> dict[str, dict]:
    """name attribute -> {label, required, kind, options}, or {} if unknown."""
    from urllib.parse import urlparse

    parts = urlparse(url or "")
    host = (parts.hostname or "").lower()
    if _GREENHOUSE.match(host):
        found = _greenhouse(host, parts.path or "")
        if found:
            _log.get("schema").info(
                "the board published %d question(s) for this posting", len(found))
        return found
    return {}


def apply_to(fields, schema: dict[str, dict]) -> int:
    """Correct discovered fields from the schema. Returns how many changed.

    Only what the schema actually states is taken: a question's real text, the
    fact that it is required, and the options it offers. Everything else --
    the selector, the frame, how to drive the control -- stays with discovery,
    which is the half that has to touch the page.
    """
    if not schema:
        return 0
    changed = 0
    for f in fields:
        entry = schema.get((getattr(f, "name", "") or "").strip())
        if entry is None:
            entry = schema.get((f.id or "").strip())
        if not entry:
            continue
        before = (f.label, f.required, tuple(f.options or ()))
        if entry["label"]:
            f.label = entry["label"]
        if entry["required"]:
            f.required = True
        if entry["options"] and not f.options:
            f.options = list(entry["options"])
        if before != (f.label, f.required, tuple(f.options or ())):
            changed += 1
    return changed
