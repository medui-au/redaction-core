"""Check text for what survived redaction.

The text is cut into
overlapping chunks, tokens are masked (so nothing matches across or inside a
token), and four scanners run over each chunk:

  tier 0  the patient's own identifiers and their variants
  tier 1  named practitioners (address book, practice users) and the
          practice's own details
  tier 2  shapes that may be anyone's: phones, emails, Medicare numbers,
          addresses, titled names, links

Findings carry offsets into the text and types, never the matched text.
"""

from __future__ import annotations

import time
from collections import Counter

from . import identifiers as identifier_service
from . import patterns as pattern_detector
from .chunking import chunk_text
from ..escapes import mask

DETECTOR_VERSION = pattern_detector.DETECTOR_VERSION


def scan(text: str, patient_id: int | None) -> list[dict]:
    """Every finding, with offsets made absolute; overlapping chunks may repeat one."""
    # A literal \\n, \\t or \\r is a word boundary; positions are unchanged (ADR-116).
    text = mask(text or '')
    identifiers = identifier_service.get_patient_identifiers(patient_id) if patient_id else []
    contacts = identifier_service.get_contacts_matcher()
    practice = identifier_service.get_practice_identifiers()
    practice_name = identifier_service.get_practice_name_patterns()

    out: list[dict] = []
    for index, (start, end) in enumerate(chunk_text(text)):
        masked = pattern_detector.mask_tokens(text[start:end])
        findings = (identifier_service.scan_chunk_patient(masked, 0, identifiers)
                    + identifier_service.scan_chunk_contacts(masked, 0, contacts)
                    + identifier_service.scan_chunk_practice(masked, 0, practice)
                    + identifier_service.scan_chunk_practice_name(masked, 0, practice_name)
                    + pattern_detector.scan_chunk(masked, 0))
        for finding in findings:
            out.append({**finding, 'chunk_index': index,
                        'tier': finding.get('tier', 2),
                        'match_start': start + finding['match_start'],
                        'match_end': start + finding['match_end']})
    return out


def hits(findings: list[dict]) -> list[dict]:
    """One hit per span, by position then tier."""
    out, seen = [], set()
    for f in sorted(findings, key=lambda f: (f['match_start'], f['tier'])):
        span = (f['match_start'], f['match_end'])
        if span in seen:
            continue
        seen.add(span)
        out.append({'tier': f['tier'], 'type': f['finding_type'],
                    'kind': (f.get('variant_form') or '').split(':', 1)[0] or None,
                    'coverage': f.get('coverage'),
                    'start': f['match_start'], 'end': f['match_end']})
    return out


# A verdict pauses a request for the person to review when the patient's own or a
# practitioner's identifier is found (MedUI ADR-090).
BLOCKING_TIERS = (0, 1)


def verdict(text: str, patient_id: int | None) -> dict:
    """The verdict on text about to be sent (0.2.0, MedUI's stored format):
    {detector_version, lists, scanned_ms, top_tier, hits, patient_identifiers}.
    hits: one per span, by position: {tier, type, kind, coverage, start, end}.
    patient_identifiers: 'loaded', 'unavailable' (tier 0 not searched, so not clean)
    or 'none' (no patient)."""
    from ..fingerprint import lists_fingerprint
    started = time.perf_counter()
    if patient_id:
        state = 'loaded' if identifier_service.get_patient_identifiers(patient_id) else 'unavailable'
    else:
        state = 'none'
    found = hits(scan(text or '', patient_id))
    return {
        'detector_version': DETECTOR_VERSION,
        'lists': lists_fingerprint(),
        'scanned_ms': round((time.perf_counter() - started) * 1000),
        'top_tier': min((h['tier'] for h in found), default=None),
        'hits': found,
        'patient_identifiers': state,
    }


def is_blocking(result: dict | None) -> bool:
    """True when the request should stop for review (a tier 0 or 1 hit)."""
    return bool(result) and 'error' not in result and result.get('top_tier') in BLOCKING_TIERS


def summarise(result: dict | None) -> dict | None:
    """For a privacy card: the top tier, hits (offsets) and findings counted by tier,
    type and coverage. None when the check didn't run or failed."""
    if not isinstance(result, dict) or 'error' in result or 'hits' not in result:
        return None
    found = [h for h in result.get('hits') or [] if isinstance(h, dict)]
    counts = Counter((h.get('tier'), h.get('type'), h.get('coverage')) for h in found)
    findings = [{'tier': t, 'type': ty or 'unknown', 'coverage': c, 'count': n}
                for (t, ty, c), n in sorted(counts.items(),
                                            key=lambda kv: (kv[0][0] is None, kv[0][0] or 0, str(kv[0][1])))]
    return {'top_tier': result.get('top_tier'), 'hits': found, 'findings': findings,
            'detector_version': result.get('detector_version'), 'lists': result.get('lists'),
            'scanned_ms': result.get('scanned_ms'), 'patient_identifiers': result.get('patient_identifiers')}
