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

        Reaching the end dominates: an application that is 60% filled and
        submitted beats one that is 95% filled and stuck, because only the
        first is an application. Coverage breaks ties, and time is a small
        tiebreak after that -- a filler twice as slow is worth having if it
        actually finishes.

        Coverage is deliberately the small term. On a multi-step form it is
        not comparable between contenders: the incumbent can be scored field
        by field as it fills each step, and an agent that walks the same steps
        on its own cannot be, because by the time anyone can look the earlier
        steps no longer exist. Reaching the end and how far it got are
        measurable identically for everyone, so the ranking rests on those and
        coverage only breaks ties. On a single-page form every contender is
        read back the same way and it means what it says.
        """
        return (100.0 * self.reached_review
                + 10.0 * self.steps_advanced
                + 20.0 * self.coverage
                - min(self.seconds, 600) / 120.0)


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
