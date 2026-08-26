"""Skip browser-dependent tests with a clear message when Chromium is missing."""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autoapply.browser import find_chromium  # noqa: E402


def _chromium_available() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "playwright is not installed"
    launch = {"headless": True}
    # Ask the same resolver the app uses. Reading AUTOAPPLY_CHROMIUM directly
    # meant every browser test skipped unless the variable happened to be
    # exported -- 20 of them, quietly, reported as "20 skipped" on a line most
    # runs never look at. A test that does not run is not a passing test.
    if exe := find_chromium():
        launch["executable_path"] = exe
    try:
        with sync_playwright() as p:
            p.chromium.launch(**launch).close()
        return True, ""
    except Exception as exc:
        return False, f"chromium unavailable ({str(exc).splitlines()[0]}). "\
                      "Run `playwright install chromium`, or set AUTOAPPLY_CHROMIUM."


def pytest_collection_modifyitems(config, items):
    ok, why = _chromium_available()
    if ok:
        return
    skip = pytest.mark.skip(reason=why)
    for item in items:
        if "test_e2e" in item.nodeid or "test_workday" in item.nodeid:
            item.add_marker(skip)
