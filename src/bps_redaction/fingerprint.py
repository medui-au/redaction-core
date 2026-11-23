"""A short fingerprint of the practice's own lists (0.2.0).

Recorded beside the ruleset in every verdict, so a change to a practice's lists is as
visible as a version bump: two results with the same ruleset and fingerprint were
produced by the same rules and the same lists.
"""
from __future__ import annotations

import hashlib
import json
import time

from . import _env

LIST_TABLES = ('always_redact', 'name_variants', 'identifier_labels', 'first_names')
_TTL = 60
# Bookkeeping columns that don't change what is redacted.
_IGNORED = frozenset({'id', 'created_at', 'updated_at', 'created', 'updated', 'note'})


def lists_fingerprint() -> str:
    """8 hex characters over the practice's rows in LIST_TABLES ('none' without lists)."""
    cached = _env.state('lists_fingerprint')
    if cached and time.monotonic() - cached[1] < _TTL:
        return cached[0]
    lists = _env.env().lists
    if lists is None:
        return 'none'
    content = []
    for table in LIST_TABLES:
        try:
            rows = lists.rows(table)
        except Exception:                 # list store boundary: an unreadable list counts as empty
            rows = []
        clean = sorted(json.dumps({k: str(v) for k, v in r.items() if k not in _IGNORED}, sort_keys=True)
                       for r in rows)
        content.append([table, clean])
    if _env.env().practice_first_names:   # ADR-134; absent when off, so older fingerprints hold
        content.append(['option', 'practice_first_names'])
    digest = hashlib.sha256(json.dumps(content).encode()).hexdigest()[:8]
    _env.set_state('lists_fingerprint', (digest, time.monotonic()))
    return digest
