"""The environment the rules run in: where data comes from, and per-instance state.

The rule modules were ported from a Flask app, where they reached the database
and config through Flask's `current_app`. Here a Redactor sets the environment
for the duration of each call (a context variable, so threads and async tasks
each see their own), and the rules read it through `env()`.
"""

from __future__ import annotations

import contextlib
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import SimpleNamespace

from .interfaces import Cache, Lists, MemoryCache, RowSource, StaticLists

log = logging.getLogger('bps_redaction')


@dataclass
class Env:
    source: RowSource | None = None
    lists: Lists = field(default_factory=StaticLists)
    cache: Cache = field(default_factory=MemoryCache)
    # Process-level caches the rules keep (address book, doctors, staff, first
    # names, identifiers…): one set per environment, so two Redactors on
    # different sources never share them.
    state: SimpleNamespace = field(default_factory=SimpleNamespace)
    # Sweep the practice's own users' first names too (ADR-134); off unless the host asks.
    practice_first_names: bool = False


_current: ContextVar[Env | None] = ContextVar('bps_redaction_env', default=None)


def env() -> Env:
    current = _current.get()
    if current is None:
        raise RuntimeError('bps_redaction: no environment; call through a Redactor')
    return current


@contextlib.contextmanager
def using(e: Env):
    token = _current.set(e)
    try:
        yield e
    finally:
        _current.reset(token)


def has_source() -> bool:
    return env().source is not None


def query(name: str, params=None) -> list[dict]:
    """Rows from the identity source. Raises if there is none or it fails."""
    source = env().source
    if source is None:
        raise RuntimeError('no identity source configured')
    return source.query(name, list(params) if params else None)


def state(name: str, default=None):
    return getattr(env().state, name, default)


def set_state(name: str, value) -> None:
    setattr(env().state, name, value)
