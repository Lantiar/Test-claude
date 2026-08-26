"""Playwright bootstrap.

Kept in one place because the browser binary and proxy differ between a
developer box (Playwright's own download, direct egress) and a locked-down
container (preinstalled Chromium behind an HTTPS proxy).
"""
from __future__ import annotations

import contextlib
import glob
import os

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def find_chromium() -> str:
    """The Chromium actually on this machine, whichever build it is.

    pip's playwright pins a build number and refuses anything else: this
    container ships 1194 and the installed wheel wants 1234, so every launch
    dies with "Executable doesn't exist at .../chromium_headless_shell-1234/"
    -- a whole run lost, before a single page, to a version pin. The binary is
    right there under a neighbouring directory name.

    AUTOAPPLY_CHROMIUM still wins when set. This is the fallback for when it is
    not, which is otherwise a footgun: the variable has to be exported for
    every invocation and forgetting it looks like a broken browser rather than
    a missing setting.
    """
    if exe := os.getenv("AUTOAPPLY_CHROMIUM"):
        return exe
    root = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    # Full Chromium before headless_shell: the shell cannot render the
    # extensions and PDF paths some ATSs use for resume upload.
    for pattern in ("chromium-*/chrome-linux/chrome",
                    "chromium/chrome-linux/chrome",
                    "chromium_headless_shell-*/chrome-headless-shell-linux64/"
                    "chrome-headless-shell"):
        for path in sorted(glob.glob(os.path.join(root, pattern)), reverse=True):
            if os.access(path, os.X_OK):
                return path
    return ""          # let Playwright use its own resolution and report it


@contextlib.contextmanager
def browser_page(storage_state: str | None = None):
    """Yield a page in a fresh context. One context per run, per the rate rules."""
    launch: dict = {"headless": os.getenv("HEADLESS", "1") != "0"}
    if exe := find_chromium():
        launch["executable_path"] = exe
    if proxy := os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"):
        launch["proxy"] = {"server": proxy}
    # Some inspecting proxies reset the connection on Chromium's TLS 1.3
    # ClientHello (it is ~1.7kB with GREASE and split across segments, where
    # curl's fits one small segment) -- the tunnel opens, then dies mid-handshake
    # for every host. Capping the offer at TLS 1.2 shrinks the hello enough to
    # get through. Opt-in only: this is a sandbox workaround, not a default,
    # and it never disables certificate verification.
    if tls_max := os.getenv("AUTOAPPLY_TLS_MAX"):
        launch.setdefault("args", []).append(f"--ssl-version-max={tls_max}")

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
