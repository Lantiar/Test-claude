"""A tier badge for companies worth noticing in a list of two thousand.

Matched on the normalised company name -- the same function the deduplicator
uses -- so that "Citadel Securities, LLC" and "Citadel Securities" land on the
same entry, while "Citadel" stays a different firm, which it is.

Exact matches only, with aliases written out. Substring matching would be
shorter and wrong: "Meta" appears inside "Metabolon" and "Metagenomi", "Block"
inside "Blockdaemon", "Apple" inside "Applied Materials". A badge on the wrong
company is worse than no badge, because the whole point is to be able to trust
it at a glance.
"""
from __future__ import annotations

import re

from . import normalize

# The lists as given, plus the spellings these companies actually post under.
_TIERS: dict[str, dict[str, list[str]]] = {
    "S": {
        "frontier AI": [
            "OpenAI", "Anthropic", "Google DeepMind", "DeepMind", "xAI",
            "Mistral", "Mistral AI", "Safe Superintelligence", "SSI",
        ],
        "quant / HFT": [
            "Jane Street", "Jane Street Capital", "Citadel",
            "Citadel Securities", "Hudson River Trading", "HRT",
            "Two Sigma", "Jump Trading", "DE Shaw", "D. E. Shaw",
            "The D. E. Shaw Group", "Optiver", "IMC", "IMC Trading",
            "SIG", "Susquehanna", "Susquehanna International Group",
            "Akuna", "Akuna Capital", "Radix", "Radix Trading",
        ],
        "big tech": ["Google", "Meta", "Meta Platforms", "Apple"],
    },
    "A": {
        "big tech": ["Amazon", "Microsoft", "Nvidia", "NVIDIA", "Netflix"],
        "AI / infra": [
            "Perplexity", "Perplexity AI", "Cursor", "Anysphere",
            "Character.AI", "Character AI", "Together.ai", "Together AI",
            "Fireworks.ai", "Fireworks AI", "Scale AI", "Databricks",
            "Snowflake", "Waymo", "Figure", "Figure AI", "Cohere",
        ],
        "fintech / crypto": [
            "Stripe", "Coinbase", "Ramp", "Plaid", "Robinhood", "Brex",
        ],
        "product": [
            "Airbnb", "Uber", "DoorDash", "Pinterest", "Reddit", "Palantir",
            "Palantir Technologies", "Snap", "Snap Inc", "Roblox", "Lyft",
            "Instacart", "Maplebear",
        ],
    },
    "B": {
        "": [
            "Salesforce", "Atlassian", "Adobe", "LinkedIn", "Dropbox",
            "Block", "Square", "TikTok", "ByteDance", "Oracle", "Twilio",
            "Datadog", "Cloudflare", "Splunk", "Workday", "ServiceNow",
            "Notion", "Figma", "Discord", "GitHub", "HubSpot", "Spotify",
            "Shopify",
        ],
    },
}

# normalised name -> (tier, the group it came from)
_INDEX: dict[str, tuple[str, str]] = {}
for _tier, _groups in _TIERS.items():
    for _group, _names in _groups.items():
        for _name in _names:
            _key = normalize.company(_name)
            # First writer wins, so a company listed in two tiers keeps the
            # higher one -- S is declared before A before B.
            if _key and _key not in _INDEX:
                _INDEX[_key] = (_tier, _group)

ORDER = {"S": 0, "A": 1, "B": 2}


# A trailing "(SIG)", "(Square)", "(Anysphere)" -- companies post under their
# own name with the familiar one bracketed after it, and normalize keeps the
# bracketed part because it strips punctuation rather than what is inside it.
# Susquehanna appears in the corpus both ways.
_PAREN = re.compile(r"\s*\([^)]*\)")


def _look_up(company: str):
    name = company or ""
    hit = _INDEX.get(normalize.company(name))
    if hit is None and "(" in name:
        # Both halves, since either can be the one that is listed: the
        # bracketed part is the familiar name about as often as it is a
        # disambiguator.
        hit = (_INDEX.get(normalize.company(_PAREN.sub("", name)))
               or _INDEX.get(normalize.company(
                   " ".join(re.findall(r"\(([^)]*)\)", name)))))
    return hit


def tier(company: str) -> str | None:
    """'S', 'A', 'B', or None."""
    hit = _look_up(company)
    return hit[0] if hit else None


def group(company: str) -> str:
    hit = _look_up(company)
    return hit[1] if hit else ""


def known() -> dict[str, tuple[str, str]]:
    return dict(_INDEX)
