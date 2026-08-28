"""Story links from one account, through Apify's stories actor.

Chosen over driving Instagram ourselves, and the reason is not convenience.
instaloader has no link-sticker support at all -- StoryItem exposes captions,
media URLs and timestamps and nothing else -- so the tapped-through URL is only
reachable through a private mobile-API struct behind a logged-in session. That
means an account, an account means a phone number, and a burner tied to a real
number is a burner in name only. This actor reads public stories with no login,
so none of that applies.

Stories expire after 24 hours, which makes this the only source with a
deadline. Simplify can be re-read at any time and will say the same thing; a
story that is missed is missed permanently. Poll at least every few hours.

The shape below is what the endpoint actually returns, which is not what its
documentation describes. The docs promise story_link_stickers objects carrying
story_link.url; the response has a flat `links` list of plain strings, and each
one is an l.instagram.com redirect with the real destination url-encoded in
?u=. Coded against the observed response, with a note raised when it stops
looking like this -- an actor quietly changing its output is indistinguishable
from an account that posted no links.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .. import log_note
from ..models import RawListing
from . import register

NAME = "instagram"
TARGET = os.getenv("IG_TARGET", "zero2sudo")
ACTOR = os.getenv("APIFY_STORIES_ACTOR", "npXRkev4Qrrq989Pz")
ENDPOINT = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


def fetch(target: str, token: str, timeout: int = 280) -> list[dict]:
    url = f"{ENDPOINT.format(actor=ACTOR)}?token={token}"
    body = json.dumps({"usernames": [target]}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def stories(con=None, target: str | None = None, **kwargs):
    """One RawListing per link found in the target's live stories."""
    target = target or TARGET
    token = os.getenv("APIFY_TOKEN", "")
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set; story links cannot be read")

    payload = fetch(target, token)
    notes: list[str] = []
    stories_seen = links_seen = 0

    for account in payload:
        for story in account.get("stories") or []:
            stories_seen += 1
            links = story.get("links")
            if links is None and "story_link_stickers" in story:
                # The documented shape. Handled because if the actor ever
                # switches to it, silently returning nothing is the worst
                # outcome available.
                links = [(s.get("story_link") or {}).get("url")
                         for s in story["story_link_stickers"] or []]
            for link in links or []:
                if not isinstance(link, str) or not link.startswith("http"):
                    continue
                links_seen += 1
                yield RawListing(
                    source=NAME,
                    source_record_id=f"{story.get('storyId','')}:{link}"[:200],
                    # Left wrapped on purpose: identity.unwrap peels
                    # l.instagram.com off, and keeping the original here means
                    # the sighting records exactly what the story contained.
                    url=link,
                    posted_at=story.get("timestamp"),
                    # When the account shared it, not when the employer posted
                    # it. Simplify's real date supersedes this wherever the two
                    # sources meet on the same job.
                    posted_at_is_real=False,
                    story_ref=str(story.get("storyId") or ""),
                    raw={k: v for k, v in story.items() if k != "mediaUrl"},
                )

    if stories_seen and not links_seen:
        notes.append(f"{stories_seen} stories and no links in any of them -- "
                     f"check the actor's output shape")
    if not stories_seen:
        notes.append(f"{target} has no live stories right now")
    stories.notes = notes
    if con is not None:
        for n in notes:
            log_note(con, NAME, n)


register(NAME, stories)
