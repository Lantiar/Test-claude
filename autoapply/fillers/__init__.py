"""Interchangeable form fillers, so they can be compared instead of argued about.

The premise of this branch. Filling a job application is not a novel problem
and several projects already do it; hand-writing another widget driver every
time a tenant renders a dropdown differently is a losing race, and the last
branch lost it -- Workday reached the review page only after a per-widget fix
for the button+listbox, then the multiselect, then the fact that the
multiselect nests two levels deep, then segmented date spinners, then checkbox
groups.

What is actually solved here, and worth keeping, is everything around the
filling: getting through a sign-in, creating an account, reading a verification
code out of Gmail, dismissing a consent banner, clicking through from a posting
to the application, and refusing to type into a page that is not one. That work
was hard-won and none of it is what a form-filling agent does.

So: the auth layer hands over a browser page already signed in and already
standing on the application, and a filler's whole job is to answer what is on
it. Every contender gets the identical page, the identical profile and the
identical scoring, which is the only way "which does best" has an answer.

A filler may be a local DOM script, a browser-use agent, a computer-use loop,
Skyvern, or anything else that can be wrapped in fill(). The learner sits on
top of whichever wins and covers what it leaves behind, rather than being the
mechanism.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..models import Field, Job


@dataclass
class FillReport:
    """What one filler managed on one application, in comparable terms."""

    filler: str
    job: Job
    # label -> value, as the filler claims to have entered them.
    answers: dict[str, str] = field(default_factory=dict)
    # Read back off the page afterwards by the harness, not by the filler:
    # a filler grading its own work is the thing this is meant to avoid.
    verified: dict[str, str] = field(default_factory=dict)
    fields_found: int = 0
    steps_advanced: int = 0
    reached_review: bool = False
    seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    screenshot: str = ""
    # How `verified` was arrived at. A single-page form can be read back by the
    # harness after the fact, which is the fairest possible check. A wizard
    # cannot: advancing destroys the previous step's DOM, so the review page
    # holds nothing and an end-of-run readback scores every contender zero.
    # There the count comes from each step being verified as it was filled --
    # still a DOM readback, just taken at the only moment it exists.
    scored_by: str = "harness readback"

    @property
    def filled(self) -> int:
        return len(self.verified)

    @property
    def coverage(self) -> float:
        """Fraction of what was on the page that ended up answered."""
        return (self.filled / self.fields_found) if self.fields_found else 0.0

    def score(self) -> float:
        """One number, so a bake-off has an ordering.

        Reaching the end dominates: an application 60% filled and finished
        beats one 95% filled and stuck, because only the first is an
        application. Then the count of fields actually holding a value, then
        time.

        steps_advanced is deliberately NOT in here. It cannot mean the same
        thing for every contender -- the incumbent reports wizard steps
        accepted, a browser-use agent reports how many actions it took, a
        vision loop reports iterations -- and scoring on it ranked an agent
        that never reached the review step at 415 against 159 for the one
        contender that finished the application, on the strength of 40 agent
        actions counted as 40 steps. It stays as a column because it is worth
        seeing; it is not a measure of progress.

        The count is absolute rather than a ratio for a related reason: a
        contender that stops on step two is read back against the two fields
        in front of it and scores 12/12, while one that walked to the end is
        read back against 41. The denominators are not the same question.
        """
        return (100.0 * self.reached_review
                + 2.0 * self.filled
                - min(self.seconds, 900) / 120.0)


class Filler(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """(usable here, why not). Checked before a run so a missing
        dependency is reported as a skip rather than a zero -- a filler that
        could not start is not a filler that did badly."""

    def fill(self, page, job: Job, profile: dict,
             on_step: Callable[[int], None] | None = None) -> FillReport:
        """Answer the application on `page`, which is already signed in and
        already standing on the form. Advance through its steps, stop at the
        review page, and never submit."""


_REGISTRY: dict[str, Callable[[], Filler]] = {}


def register(name: str, build: Callable[[], Filler]) -> None:
    _REGISTRY[name] = build


def get(name: str) -> Filler:
    if name not in _REGISTRY:
        raise KeyError(f"unknown filler {name!r}; have {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def names() -> list[str]:
    return sorted(_REGISTRY)


def load_all() -> None:
    """Import the adapters so they register themselves. Each import is
    optional: a contender whose dependency is missing must not stop the rest
    of the bake-off from running."""
    for module in ("dom", "browseruse", "computeruse", "skyvern_filler"):
        try:
            __import__(f"{__name__}.{module}")
        except Exception:
            continue
