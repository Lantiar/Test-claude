"""Stories from one account, and the links in them.

Stories expire after 24 hours. That single fact drives everything here: this is
the only source with a deadline, a missed window loses those links permanently,
and there is no way to go back for them. Simplify can be re-read any time and
will say the same thing; a story cannot be re-read at all.

Where the links actually are is the awkward part. instaloader has no support
for link stickers -- StoryItem exposes caption, media URLs and timestamps, and
nothing else -- so the tapped-through URL is not available on the documented
API at all. It is available in the response Instagram's own mobile client gets,
which instaloader will fetch and hand over as StoryItem._iphone_struct, and
which needs a logged-in session. So there are two extraction paths and they
find different things:

  * link stickers, out of the mobile struct: the URL the story links to,
    exactly, with nothing to parse.
  * text: URLs typed into the caption, which are ordinary text.

A URL burned into the image as part of the graphic is a third case and is not
covered -- that needs OCR, and this machine has no OCR available. It is
reported rather than skipped quietly, which is the difference between a gap you
know about and a source that silently under-reports forever.

On the account: use a throwaway. Reading another account's stories through the
private API is against Instagram's terms, the session can be checkpointed or
banned, and none of that should ever touch an account that matters.
"""
from __future__ import annotations

import os
import re
import time

from .. import db as _db
from .. import log_note
from ..models import RawListing
from . import register

NAME = "instagram"

# The account whose stories we read.
TARGET = os.getenv("IG_TARGET", "zero2sudo")

# A URL sitting in ordinary text. Deliberately tolerant about the scheme, since
# people type "acme.com/careers" as often as they paste a full link.
_URL = re.compile(
    r"\b(?:https?://|www\.)[^\s<>\"'\)\]]+|\b[a-z0-9][\w-]*\.(?:com|io|co|org|net|"
    r"ai|dev|jobs|so|gg|app|xyz|me)(?:/[^\s<>\"'\)\]]*)?", re.I)


def _session(username: str | None = None, password: str | None = None):
    """A logged-in instaloader context, from a saved session where possible."""
    import instaloader

    user = username or os.getenv("IG_USER", "")
    if not user:
        raise RuntimeError(
            "no Instagram account configured: set IG_USER and either IG_PASS "
            "or a saved session file. Stories are not readable anonymously.")
    loader = instaloader.Instaloader(
        quiet=True, download_pictures=False, download_videos=False,
        download_video_thumbnails=False, save_metadata=False,
        iphone_support=True)          # the link stickers live behind this
    session_dir = os.getenv("IG_SESSION_DIR", "data/ig")
    os.makedirs(session_dir, exist_ok=True)
    path = os.path.join(session_dir, f"session-{user}")
    if os.path.exists(path):
        loader.load_session_from_file(user, path)
        return loader, user
    pw = password or os.getenv("IG_PASS", "")
    if not pw:
        raise RuntimeError(
            f"no saved session at {path} and IG_PASS is unset: the first login "
            f"needs a password, after which the session is reused.")
    loader.login(user, pw)
    loader.save_session_to_file(path)
    return loader, user


def _sticker_urls(item) -> tuple[list[str], str]:
    """(URLs from link stickers, a note when the shape was not what we expect).

    Reaching through a private attribute of a third-party library into an
    undocumented response, so the note matters as much as the URLs: if
    Instagram renames this field, every future poll finds nothing, and nothing
    finding anything is indistinguishable from an account that posted no links.
    The note is what makes the difference visible in the run record.
    """
    try:
        struct = item._iphone_struct
    except Exception as exc:
        return [], f"mobile struct unavailable ({type(exc).__name__})"
    if not isinstance(struct, dict) or not struct:
        return [], "mobile struct was empty"

    urls, seen_any_sticker_field = [], False
    for field in ("story_link_stickers", "story_cta", "link_stickers"):
        if field not in struct:
            continue
        seen_any_sticker_field = True
        for sticker in struct.get(field) or []:
            if not isinstance(sticker, dict):
                continue
            link = sticker.get("story_link") or sticker.get("webUri") or sticker
            url = (link.get("url") if isinstance(link, dict) else None) \
                or sticker.get("url") or sticker.get("web_uri")
            if isinstance(url, str) and url.strip():
                urls.append(url.strip())
    if urls:
        return urls, ""
    if seen_any_sticker_field:
        return [], ""          # the field is there and this story has no link
    return [], ("no link-sticker field in the mobile struct -- Instagram may "
                "have renamed it; links from stickers cannot be read")


def _text_urls(item) -> list[str]:
    text = " ".join(str(x) for x in (getattr(item, "caption", "") or "",))
    out = []
    for m in _URL.finditer(text):
        u = m.group(0).rstrip(".,;:")
        out.append(u if u.lower().startswith("http") else f"https://{u}")
    return out


def stories(con=None, target: str | None = None, **kwargs):
    """RawListings for every link found in the target's live stories."""
    import instaloader

    target = target or TARGET
    loader, user = _session()
    profile = instaloader.Profile.from_username(loader.context, target)

    notes: set[str] = set()
    count = 0
    for story in loader.get_stories(userids=[profile.userid]):
        for item in story.get_items():
            count += 1
            urls, note = _sticker_urls(item)
            if note:
                notes.add(note)
            urls += _text_urls(item)
            posted = getattr(item, "date_utc", None)
            posted = posted.timestamp() if posted else None
            ref = f"{target}/{getattr(item, 'mediaid', '')}"
            for url in dict.fromkeys(urls):          # order-preserving dedupe
                yield RawListing(
                    source=NAME,
                    source_record_id=f"{ref}:{url}"[:200],
                    url=url,
                    # A story says nothing about the employer or the title. The
                    # link resolver and the ATS page fill those in later; what
                    # a story is authoritative about is the date, because we
                    # watched it appear.
                    posted_at=posted,
                    active=True,
                    story_ref=ref,
                    raw={"story_ref": ref, "caption": getattr(item, "caption", "")},
                )
    if count == 0:
        notes.add(f"{target} has no live stories right now")
    # A story image with the URL drawn into the graphic needs OCR, and this
    # machine has none. Said once per run rather than never: a source that
    # cannot see part of its input should say so.
    if not _ocr_available():
        notes.add("no OCR available: URLs drawn into a story image are not read")
    stories.notes = sorted(notes)
    if con is not None and notes:
        for n in notes:
            log_note(con, NAME, n)


def _ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
    except Exception:
        return False
    import shutil

    return shutil.which("tesseract") is not None


register(NAME, stories)
