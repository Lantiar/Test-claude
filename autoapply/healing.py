"""Self-healing selectors, via autoheal-locator, driven by Playwright.

The architecture asked for is: the script finds the field and fills it; when
the script cannot find it, something looks at the page, works out which element
was meant, and writes that back so the script finds it unaided next time.

That is a solved problem with a name -- self-healing locators -- and
autoheal-locator implements it in Python with the exact tier order wanted:

    ORIGINAL_SELECTOR -> CACHED -> DOM_ANALYSIS -> VISUAL_ANALYSIS

with a persistent cache, so the model is consulted once per broken selector
and never again. What it ships without is a Playwright adapter; the interface
for one is abstract and five methods wide, and AutomationFramework.PLAYWRIGHT
already exists in its enum. So this is the gap filled rather than the wheel
rebuilt.

The division of labour with the rest of the project is worth stating, because
the two halves look similar and are not. autoheal remembers WHERE a field is.
The teaching store remembers WHAT to put in it. A dropdown that moved needs the
first; "Have you ever served in the military?" needs the second, and no amount
of selector healing would ever produce "I AM NOT A VETERAN".
"""
from __future__ import annotations

import os
from typing import Any

from . import log as _log

log = _log.get("healing")


class PlaywrightAdapter:
    """autoheal's WebAutomationAdapter, over a Playwright page.

    Implemented rather than imported because the library ships Selenium only.
    Its own enum already names PLAYWRIGHT, so this is the seam it left open.
    """

    def __init__(self, page):
        self.page = page

    # -- the five abstract methods ----------------------------------------
    async def find_elements(self, selector) -> list:
        try:
            return self.page.query_selector_all(str(selector))
        except Exception:
            # A malformed selector is a miss, not a crash: healing exists
            # precisely for selectors that have stopped being valid.
            return []

    async def get_page_source(self, include_shadow_dom: bool = True) -> str:
        if not include_shadow_dom:
            return self.page.content()
        try:
            return self.page.evaluate(SHADOW_DOM_JS)
        except Exception:
            return self.page.content()

    async def take_screenshot(self) -> bytes:
        # CSS scale, not device scale: a retina full-page shot costs several
        # times more to send and tells a model nothing extra.
        return self.page.screenshot(type="png", scale="css")

    async def get_element_context(self, element):
        from autoheal.models.element_context import ElementContext

        try:
            data = self.page.evaluate(CONTEXT_JS, element)
        except Exception:
            data = {}
        return ElementContext(
            parent_container=data.get("parent", ""),
            relative_position=data.get("position", ""),
            sibling_elements=data.get("siblings", []),
            attributes=data.get("attributes", {}),
            text_content=data.get("text", ""),
            fingerprint=data.get("fingerprint", ""),
        )

    def get_framework_type(self):
        from autoheal.models.enums import AutomationFramework

        return AutomationFramework.PLAYWRIGHT


SHADOW_DOM_JS = r"""
() => {
  // Shadow roots are invisible to outerHTML, and an ATS that renders its
  // controls inside one looks like an empty page to anything reading markup.
  const walk = (root) => {
    let html = '';
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) html += walk(el.shadowRoot);
    }
    return (root.innerHTML || '') + html;
  };
  return walk(document);
}
"""

CONTEXT_JS = r"""
(el) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const attrs = {};
  for (const a of el.attributes || []) attrs[a.name] = a.value;
  const parent = el.parentElement;
  const siblings = parent
    ? [...parent.children].filter(n => n !== el).slice(0, 6)
        .map(n => clean(n.innerText).slice(0, 40)).filter(Boolean)
    : [];
  const r = el.getBoundingClientRect();
  return {
    parent: parent ? parent.tagName.toLowerCase() : '',
    position: `${Math.round(r.x)},${Math.round(r.y)}`,
    siblings,
    attributes: attrs,
    text: clean(el.innerText).slice(0, 120),
    fingerprint: [el.tagName, attrs.type || '', attrs.name || '',
                  clean(el.getAttribute('aria-label'))].join('|'),
  };
}
"""


def build_locator(page, provider_name: str | None = None):
    """An AutoHealLocator over this page, or None when healing is switched off.

    Returns None rather than raising: healing is an improvement on the
    deterministic path, and a run must not die because it is unavailable.
    """
    if os.getenv("AUTOAPPLY_HEALING", "1") == "0":
        return None
    try:
        from autoheal import (AIProvider, AutoHealConfiguration,
                              AutoHealLocator, ExecutionStrategy)
    except Exception as exc:
        log.info("autoheal not installed (%s); selectors will not self-heal",
                 type(exc).__name__)
        return None

    name = (provider_name or os.getenv("HEALING_PROVIDER", "openai")).lower()
    provider = {
        "openai": AIProvider.OPENAI,
        "anthropic": AIProvider.ANTHROPIC_CLAUDE,
        "local": AIProvider.LOCAL_MODEL,
        "mock": AIProvider.MOCK,
    }.get(name, AIProvider.MOCK)

    try:
        config = AutoHealConfiguration(
            ai_provider=provider,
            # Try the selector, then what worked last time, then read the DOM,
            # then look at it. Cheapest first, and the model only on a genuine
            # miss.
            execution_strategy=ExecutionStrategy.SMART_SEQUENTIAL,
        )
        return (AutoHealLocator.builder()
                .with_web_adapter(PlaywrightAdapter(page))
                .with_configuration(config)
                .build())
    except Exception as exc:
        log.info("could not build the healing locator: %s: %s",
                 type(exc).__name__, str(exc)[:120])
        return None
