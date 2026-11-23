"""De-redaction: restore original text in a reply, from a token map.

Three-pass strategy:
  1. Exact match: replace tokens verbatim using the token map
  2. Fuzzy hex: if any [Redacted *: XX-<hex>] tokens remain, log a warning
     (partial hex IDs can appear in LLM paraphrases; full hex still matches in pass 1)
  3. Placeholder sweep: replace any remaining [Redacted *: *] patterns with a
     safe '(redacted)' marker so no token leaks through to the user

A reply that is a JSON object or array (a chatbot's {"Title", "Response"}
envelope) is restored inside its parsed strings and written back as JSON, so
a restored value containing a quote or a backslash cannot break it (MedUI
ADR-073).
"""

import json
import re
from dataclasses import dataclass

from ._env import log

_TOKEN_RE = re.compile(r'\[Redacted [A-Za-z]+: [A-Z]+-[0-9a-f]+\]')
_ANY_REDACTED_RE = re.compile(r'\[Redacted [^\]]+\]')
_FENCE_RE = re.compile(r'^```(?:json)?\s*\n([\s\S]*?)\n?```$', re.IGNORECASE)


def _json_reply(text: str):
    """The reply parsed as a JSON object or array (a ```json fence allowed), or None."""
    body = text.strip()
    fence = _FENCE_RE.match(body)
    if fence:
        body = fence.group(1).strip()
    if not body.startswith(('{', '[')):
        return None
    try:
        value = json.loads(body)
    except ValueError:
        return None
    return value if isinstance(value, (dict, list)) else None


def _restore(text: str, token_map: dict[str, str]) -> tuple[str, int]:
    replacements = 0
    for token, original in token_map.items():
        if token in text:
            text = text.replace(token, original)
            replacements += 1
    return text, replacements


@dataclass
class Restored:
    text: str
    restored: int       # distinct tokens replaced
    unresolved: int     # well-formed tokens not in the map
    swept: int          # [Redacted …] patterns replaced with '(redacted)'


def deredact(text: str, token_map: dict[str, str]) -> Restored:
    """Restore text with token_map (token → original). Never returns a raw token.

    An empty map still gets the sweep: a library caller must never be handed
    a token.
    """
    if not text:
        return Restored(text, 0, 0, 0)
    token_map = token_map or {}

    # Pass 1: exact token replacement. A JSON reply is restored inside its
    # strings: pasting a name like 'First "Preferred" Last' into the raw text
    # left an unescaped quote, and the reply no longer parsed (ADR-073).
    reply = _json_reply(text)
    if reply is not None:
        replacements = 0

        def _walk(value):
            nonlocal replacements
            if isinstance(value, str):
                value, n = _restore(value, token_map)
                replacements += n
                return value
            if isinstance(value, dict):
                return {_walk(k): _walk(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_walk(v) for v in value]
            return value

        restored = json.dumps(_walk(reply), ensure_ascii=False)
    else:
        restored, replacements = _restore(text, token_map)

    # Pass 2: fuzzy — warn about any remaining well-formed tokens not in map
    remaining = _TOKEN_RE.findall(restored)
    if remaining:
        log.warning('deredact: %d token(s) not in reverse map', len(remaining))

    # Pass 3: sweep any remaining [Redacted ...] patterns → safe placeholder
    swept, sweep_count = _ANY_REDACTED_RE.subn('(redacted)', restored)
    if sweep_count:
        log.warning('deredact: %d malformed/unknown token(s) replaced with placeholder', sweep_count)
        restored = swept

    return Restored(restored, replacements, len(remaining), sweep_count)
