"""Playwright bootstrap.

Kept in one place because the browser binary and proxy differ between a
developer box (Playwright's own download, direct egress) and a locked-down
container (preinstalled Chromium behind an HTTPS proxy).
"""
from __future__ import annotations

import contextlib
import os

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


@contextlib.contextmanager
def browser_page(storage_state: str | None = None):
    """Yield a page in a fresh context. One context per run, per the rate rules."""
    launch: dict = {"headless": os.getenv("HEADLESS", "1") != "0"}
    if exe := os.getenv("AUTOAPPLY_CHROMIUM"):
        launch["executable_path"] = exe
    if proxy := os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"):
        launch["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        ctx_args: dict = {"user_agent": UA, "viewport": {"width": 1440, "height": 1000}}
        if storage_state and os.path.exists(storage_state):
            ctx_args["storage_state"] = storage_state
        context = browser.new_context(**ctx_args)
        context.set_default_timeout(30000)
        page = context.new_page()
        try:
            yield page
        finally:
            if storage_state:
                with contextlib.suppress(Exception):
                    context.storage_state(path=storage_state)
            with contextlib.suppress(Exception):
                context.close()
            with contextlib.suppress(Exception):
                browser.close()
