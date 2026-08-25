"""Run logging: what the worker saw, chose, typed, and what came back.

A failed application run is hard to diagnose after the fact because the page is
gone. The queue row says "required field unanswered", the screenshot shows one
moment, and the reason it went wrong -- which tier chose the value, whether it
was written, whether it stuck -- is nowhere. This makes that trail explicit.

Levels are used with intent rather than by feel:
  * info  -- the shape of the run: step boundaries, sign-in outcome, counts
  * debug -- per-field decisions and results, which is what you actually need
             when one field out of a hundred and sixty is wrong

Set AUTOAPPLY_LOG=debug for the second. Every run also writes a full debug log
to data/logs/ regardless of console level, so a run that fails once can be read
afterwards without having to reproduce it with the flag turned on.
"""
from __future__ import annotations

import logging
import os
import sys
import time

_CONFIGURED = False
LOG_DIR = os.getenv("LOG_DIR", "data/logs")


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    root = logging.getLogger("autoapply")
    root.setLevel(logging.DEBUG)
    root.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, os.getenv("AUTOAPPLY_LOG", "info").upper(),
                             logging.INFO))
    console.setFormatter(logging.Formatter("%(levelname)-5s %(name)-22s %(message)s"))
    root.addHandler(console)

    # The file handler is always debug: the whole point is that a run which
    # fails once can be read afterwards rather than reproduced.
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"run-{int(time.time())}.log")
        handler = logging.FileHandler(path)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)-22s %(message)s"))
        root.addHandler(handler)
        root.debug("log file: %s", path)
    except OSError:
        pass


def get(name: str) -> logging.Logger:
    """A logger for one component, e.g. get('worker.workday')."""
    _configure()
    return logging.getLogger(f"autoapply.{name}")


def brief(value, limit: int = 60) -> str:
    """Shorten a value for a log line without hiding that it was truncated."""
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
