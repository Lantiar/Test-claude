"""Adapters. Each one turns a source into RawListings and knows nothing else."""
from __future__ import annotations

from typing import Callable, Iterable

from ..models import RawListing

_SOURCES: dict[str, Callable[..., Iterable[RawListing]]] = {}


def register(name: str, fn) -> None:
    _SOURCES[name] = fn


def get(name: str):
    return _SOURCES[name]


def names() -> list[str]:
    return sorted(_SOURCES)


def load_all() -> None:
    from . import instagram, simplify         # noqa: F401
