"""The one shape every source is reduced to before anything else looks at it."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawListing:
    """What one source said about one thing, once.

    Adapters produce these and nothing else. Everything downstream -- identity,
    matching, storage, dates -- reads this and never the source's own format,
    which is what keeps a fourth source from touching more than one file.
    """
    source: str                       # simplify | instagram | ...
    source_record_id: str             # that source's own id, for idempotency
    url: str
    title: str = ""
    company: str = ""
    locations: list[str] = field(default_factory=list)
    season: str = ""
    sponsorship: str = ""
    category: str = ""
    company_slug: str = ""
    # What the source claims about when this was posted. None when the source
    # does not say -- which is not the same as "posted now", and is the reason
    # this is nullable rather than defaulted to the time of the poll.
    posted_at: float | None = None
    updated_at: float | None = None
    # False for a listing the source says is closed, so a run can retire it
    # rather than quietly leaving it open forever.
    active: bool = True
    raw: dict = field(default_factory=dict)
    # Stories only: where in the story this came from, for tracing back.
    story_ref: str = ""
