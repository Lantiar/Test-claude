"""The shared board between the web page and the runner.

The page can ask for outreach but cannot perform it -- sourcing is Apify,
sending is Gmail, and both are Python somewhere else. So a click writes an
intent to Upstash and this reads it back, does the work, and writes what
actually happened. The state on the page is therefore a report, never a hope.

Same store as the stage tracker, reached over HTTP with the standard library.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

KEY = "jobfeed:outreach"
STAGE_KEY = "jobfeed:stages"


def _creds() -> tuple[str, str] | None:
    """Found by suffix, matching what api/outreach.js accepts.

    Vercel's Upstash integration names these differently depending on how the
    store was added, and the runner's secrets are copied from there by hand --
    so a correctly-set variable under a slightly different name must not read
    as "no store".
    """
    env = os.environ

    def find(suffix: str) -> str | None:
        if env.get(suffix):
            return env[suffix]
        for name, value in env.items():
            if name.endswith(suffix) and value:
                return value
        return None

    url = find("KV_REST_API_URL") or find("UPSTASH_REDIS_REST_URL") \
        or find("REDIS_REST_URL")
    token = find("KV_REST_API_TOKEN") or find("UPSTASH_REDIS_REST_TOKEN") \
        or find("REDIS_REST_TOKEN")
    return (url, token) if url and token else None


def available() -> bool:
    return _creds() is not None


def _redis(command: list):
    creds = _creds()
    if not creds:
        raise RuntimeError("no Upstash credentials in the environment")
    url, token = creds
    req = urllib.request.Request(
        url, data=json.dumps(command).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read())
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(body["error"])
    return body.get("result") if isinstance(body, dict) else body


def read() -> dict[str, dict]:
    """job_key -> record."""
    flat = _redis(["HGETALL", KEY]) or []
    out: dict[str, dict] = {}
    for i in range(0, len(flat) - 1, 2):
        try:
            out[flat[i]] = json.loads(flat[i + 1])
        except Exception:
            out[flat[i]] = {"state": str(flat[i + 1])}
    return out


def queued() -> list[str]:
    """The job keys someone has pressed the button on."""
    return [k for k, v in read().items() if v.get("state") == "queued"]


def write(job_key: str, state: str, note: str = "", thread: str = "",
          sent: int = 0) -> None:
    record = {"state": state, "at": int(time.time())}
    if note:
        record["note"] = note[:300]
    if thread:
        record["thread"] = thread[:120]
    if sent:
        record["sent"] = int(sent)
    _redis(["HSET", KEY, job_key, json.dumps(record)])


def stages() -> dict[str, str]:
    """job_key -> stage, as the web tracker holds it.

    The runner starts from a published snapshot every time and has no
    application table of its own, so without this it sees nobody as having
    applied to anything and outreach finds nothing to do. The tracker is the
    record; this is how the runner reads it.
    """
    flat = _redis(["HGETALL", STAGE_KEY]) or []
    return {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}
