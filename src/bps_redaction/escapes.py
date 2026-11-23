"""Literal escapes as word boundaries (MedUI ADR-116).

Data blocks reach a prompt written out as Python or JSON text, where a line break
is the two characters backslash and n. To the rules, "\\nGoldie" was then the one
word "nGoldie": a name right after it was neither redacted nor found by the
checker. Here the escape's letter is swapped for a private-use character of the
same length before the rules run, and swapped back after. Every boundary the
rules test then sees a non-letter, and positions (the checker's offsets) are
unchanged. The vault restores the letters in what it stores, so a token's
original never carries a placeholder.

A string that already holds one of the placeholders is left as it is.
"""

from __future__ import annotations

import re

_TO_MARK = {'n': '', 't': '', 'r': ''}
_FROM_MARK = {v: k for k, v in _TO_MARK.items()}
_ESCAPE = re.compile(r'(?<!\\)((?:\\\\)*\\)([ntr])')
_MARKS = re.compile('[]')


def _mask_str(text: str) -> str:
    if '\\' not in text or _MARKS.search(text):
        return text
    return _ESCAPE.sub(lambda m: m.group(1) + _TO_MARK[m.group(2)], text)


def _unmask_str(text: str) -> str:
    return _MARKS.sub(lambda m: _FROM_MARK[m.group(0)], text) if _MARKS.search(text) else text


def _walk(value, fn):
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_walk(v, fn) for v in value]
    if isinstance(value, tuple):
        return tuple(_walk(v, fn) for v in value)
    if isinstance(value, dict):
        return {k: _walk(v, fn) for k, v in value.items()}
    return value


def mask(value):
    """Text (or nested data) with each literal \\n, \\t, \\r letter swapped for a placeholder."""
    return _walk(value, _mask_str)


def unmask(value):
    """The placeholders back to their letters."""
    return _walk(value, _unmask_str)
