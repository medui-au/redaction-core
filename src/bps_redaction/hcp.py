"""HCPRedactionService — HCP name extraction and redaction.

Practice-wide HCP map is cached in the environment's Cache (key: hcp_practice_map_cache)
for up to 24 hours to avoid hammering BPSDB on every pipeline run. The cache
is invalidated by fingerprint: if the CONTACTS table changes (row count or max ID)
the cached map is rebuilt immediately.

Patient-specific contacts (from correspondence in/out) are always fetched live.
"""

import json
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Any
import re

from . import _env
from ._env import log
from .escapes import _TO_MARK as _ESCAPE_MARKS
from .vault import TokenVault
from .words import word_list_path
from .redaction import (NON_NAME_KEYS, _COLLOCATION_GUARD,
                        _safe_sub, _TOKEN_RE)


_CACHE_KEY = 'hcp_practice_map_cache'
_CACHE_TTL_HOURS = 24

# Process-level caches — survive across requests, reset on worker restart.
# Avoids BPSDB round-trips on every pipeline run when data is stable.
# (Kept per environment, under the names below, via _env.state.)
_practice_lock = threading.Lock()
_PRACTICE_MEM_TTL = 3600  # seconds before re-checking the slow cache + fingerprint


# The practice's own doctors, from BP's user list (not the address book).
_DOCTORS_TTL = 3600

# The practice's other staff — nurses, receptionists — from the same list.

# Common first names, from BP's patients (see _common_first_names).
_FIRST_NAMES_TTL = 86400
# After a surname that is also a common first name: another capitalised word
# means it is being used as a first name ("Dr David Smith"), not as the
# contact's surname. Case-sensitive on purpose — the combined pattern ignores
# case, and "Dr James said" is still Dr James.
_FOLLOWED_BY_NAME = r"(?![ \t]+(?-i:[A-Z])[a-zA-Z'-])"
_PATIENT_CONTACTS_TTL = 300

# Titles that mark what follows as a person. An ambiguous surname — one that is
# also an ordinary English word — is only redacted in this company.
_TITLE_PREFIX = (r'(?:Dr|Dr\.|Doctor|Prof|Prof\.|Professor|A/Prof|Assoc\.? Prof\.?|'
                 r'Mr|Mr\.|Mrs|Mrs\.|Ms|Ms\.|Miss|Sr|Sister|Nurse)\s+')

# Surnames that are also ordinary words: about one contact in seven in a
# practice's address book. Matching them bare turned radiology reports into nonsense —
# "Small myometrial cysts" became "[Redacted HCP: …] myometrial cysts".
_WORD_LIST_PATHS = (word_list_path(),)

# Fallback when no system word list is installed. Not exhaustive; it covers the
# surnames most likely to appear as ordinary words in clinical text.
_FALLBACK_WORDS = frozenset("""
small large long short white black brown green grey gray young old best
little major minor rose may day bell cross church hunt ford wood stone price
chance fields french english walker baker butcher archer carpenter cook
mason miller porter turner tailor taylor parsons sharp swift strong power
moody noble rich poor savage wild free bright dark light early late north
south east west winter summer spring frost snow rain storm river brook lake
hill dale field ford glen banks bush heath marsh moor park springs
bird finch crane swan drake fox wolf lamb bull colt hart doe buck
head foot hand arm back heart bone blood skin nail rib
case chase bond bill mark will grant hope faith joy love peace
stern hardy sound sharpe bigger stout stokes gates rivers waters
""".split())

# Typing slips that are also address-book surnames: "let me know if teh
# above…" matched a practitioner with that surname, in generic-chat and
# patient runs, never as a name (ADR-053). Like an ordinary word, such a
# surname now needs a title before it.
_TYPO_WORDS = frozenset({'teh'})

_word_set: frozenset | None = None
_lower_word_set: frozenset | None = None

# A practitioner's surname followed on the same line by a lone initial and a
# capitalised word: "Dr Rose J Quaverley" (invented) is one person, and taking
# "Rose" alone left the initial and surname beside the token (ADR-122).
# Case-sensitive inside the case-blind pattern.
_NAME_TAIL = r"(?P<tail>(?-i:[ \t]+[A-Z]\.?[ \t]+(?P<tailname>[A-Z][a-zA-Z'-]+)))?"

# Letters after a practitioner's name. With one, or on a line of its own, an
# initial and an address-book surname that is also an ordinary word are a person:
# "J Small FRANZCR" (ADR-123).
# A literal \\n in a data block, as masked while the rules run (ADR-116).
_ESCAPED_BREAK = r'\\[' + ''.join(_ESCAPE_MARKS.values()) + ']'
_POST_NOMINALS = ('MBBS', 'FRANZCR', 'FRACR', 'FRACP', 'FRACS', 'FRACGP', 'FANZCA',
                  'FRANZCOG', 'FRCPA', 'MBChB', 'BMBS', 'FACEM', 'FRANZCP', 'FRANZCO',
                  'MBBCh', 'DDU', 'FACRRM', 'FAFRM')


def _ordinary_words() -> frozenset:
    """Words that also occur as surnames, from the system list where present."""
    global _word_set
    if _word_set is not None:
        return _word_set

    from pathlib import Path
    for path in _WORD_LIST_PATHS:
        candidate = Path(path)
        if candidate.exists():
            try:
                _word_set = frozenset(
                    w.strip().lower() for w in
                    candidate.read_text(errors='ignore').splitlines()
                    if w.strip() and "'" not in w
                )
                return _word_set
            except OSError:
                break

    # Visible rather than silent: without a word list, over-redaction returns.
    try:
        log.warning(
            'no system word list found (%s); falling back to the built-in list, '
            'so some surnames that are ordinary words may be over-redacted',
            ', '.join(_WORD_LIST_PATHS))
    except RuntimeError:
        pass
    _word_set = _FALLBACK_WORDS
    return _word_set  # seconds


def _lower_words() -> frozenset:
    """The word list's lower-case entries only: words, not names or places.

    The guard on a following surname (ADR-122) uses this narrower set, so a
    common surname the list holds with a capital still counts as a name.
    """
    global _lower_word_set
    if _lower_word_set is not None:
        return _lower_word_set
    from pathlib import Path
    for path in _WORD_LIST_PATHS:
        candidate = Path(path)
        if candidate.exists():
            try:
                _lower_word_set = frozenset(
                    w.strip() for w in candidate.read_text(errors='ignore').splitlines()
                    if w.strip() and "'" not in w and w.strip() == w.strip().lower())
                return _lower_word_set
            except OSError:
                break
    _lower_word_set = _FALLBACK_WORDS
    return _lower_word_set


class HCPRedactionService:

    @staticmethod
    def build_hcp_map(patient_id: int | None) -> list[dict]:
        """Return combined list of HCP contact dicts for this patient.

        Each dict has: {surname, firstname, full_name, category, contact_id}

        Sources:
          1. Practice-wide CONTACTS table (cached ≤24h + fingerprint validated)
          2. Patient-specific contacts from CORRESPONDENCEIN
          3. Patient-specific contacts from CORRESPONDENCEOUT
          4. The practice's own doctors and other staff, from BP's user list
        """
        practice = HCPRedactionService._get_practice_map()
        # No patient (the generic chatbot, ADR-053): the practice-wide lists only.
        patient  = HCPRedactionService._get_patient_contacts(patient_id) if patient_id else []

        # Merge by contact_id to deduplicate
        merged: dict[int, dict] = {c['contact_id']: c for c in practice}
        for c in patient:
            merged.setdefault(c['contact_id'], c)
        for c in HCPRedactionService._get_practice_doctors():
            merged.setdefault(c['contact_id'], c)
        for c in HCPRedactionService._get_practice_staff():
            merged.setdefault(c['contact_id'], c)

        return list(merged.values())

    @staticmethod
    def redact_hcp(data: Any, hcp_map: list[dict], vault: TokenVault,
                   organisations_as_written: bool = False) -> Any:
        """Recursively replace HCP names in data with vault tokens.

        Uses a single combined alternation regex (one scan per string) rather than
        one compile+search per entry. With thousands of HCP contacts this is orders
        of magnitude faster than the naive per-entry approach.

        organisations_as_written (the generic chatbot, ADR-053): an entry with no
        first name — an organisation — matches only in its own capitals or in
        all capitals. The address book holds services named after ordinary
        phrases ("Domestic Violence", "Mental Health Services"), and a general
        question about domestic violence reached the model as a placeholder.
        """
        if not hcp_map:
            return data

        words = _ordinary_words() | _TYPO_WORDS
        first_names = HCPRedactionService._common_first_names()
        entries: list[tuple[str, str]] = []      # matched anywhere
        titled_only: list[tuple[str, str]] = []  # matched only after a title
        # Surnames that are also common first names: matched as above, but not
        # when another name follows ("Dr David Smith" is Dr Smith, not the
        # contact surnamed David). A few dozen contacts in a large address book.
        entries_first: list[tuple[str, str]] = []
        titled_first: list[tuple[str, str]] = []
        as_written: list[tuple[str, str]] = []   # organisations, in their capitals
        for c in hcp_map:
            cat     = c.get('category', '')
            surname = (c.get('surname') or '').strip()
            full    = (c.get('full_name') or '').strip()
            if organisations_as_written and not (c.get('firstname') or '').strip():
                name = full if len(full) > 2 else surname
                # A one-word name that is an ordinary word still needs a title.
                if len(name) > 2 and not (' ' not in name and name.lower() in words):
                    as_written.append((name, cat))
                    continue
            if full and len(full) > 2 and full.lower() != surname.lower():
                entries.append((full, cat))
            # Staff are matched by full name only (ADR-040).
            if c.get('full_name_only'):
                continue
            if surname and len(surname) > 2:
                is_first = surname.lower() in first_names
                # A surname that is also an ordinary word needs a title to be a
                # person. 'Small myometrial cysts' is a finding, not Dr Small.
                if surname.lower() in words:
                    (titled_first if is_first else titled_only).append((surname, cat))
                else:
                    (entries_first if is_first else entries).append((surname, cat))

        if not (entries or titled_only or entries_first or titled_first or as_written):
            return data

        # Longest-first prevents shorter surnames shadowing longer full names
        entries.sort(key=lambda e: len(e[0]), reverse=True)

        # Pre-mint all tokens; build lowercase lookup for the replacement callback.
        # vault.mint_hcp is idempotent within a run so pre-minting is safe.
        token_lookup: dict[str, str] = {}
        for name, specialty in entries + titled_only + entries_first + titled_first + as_written:
            token_lookup.setdefault(name.lower(), vault.mint_hcp(name, specialty))

        # One combined pattern — compiled once, used for every string in the data.
        # \b word boundaries prevent short surnames (e.g. "Ali", "Test") from
        # matching inside longer medical words ("Malignant", "testosterone").
        alternatives = []
        if entries:
            alternatives.append(
                '(?:' + '|'.join(r'\b' + re.escape(name) + r'\b'
                                 for name, _ in entries) + ')')
        if titled_only:
            # The title is part of the match but stays in the text, outside the
            # token: 'Dr [Redacted HCP: …]', as for any other surname (ADR-054).
            alternatives.append(
                '(?:' + _TITLE_PREFIX + '(?:' +
                '|'.join(re.escape(name) for name, _ in titled_only) + r')\b)')
        if entries_first:
            alternatives.append(
                '(?:' + '|'.join(r'\b' + re.escape(name) + r'\b'
                                 for name, _ in entries_first) + ')' + _FOLLOWED_BY_NAME)
        if titled_first:
            alternatives.append(
                '(?:' + _TITLE_PREFIX + '(?:' +
                '|'.join(re.escape(name) for name, _ in titled_first) + r')\b' +
                _FOLLOWED_BY_NAME + ')')
        combined = (re.compile('(?:' + '|'.join(alternatives) + ')' + _COLLOCATION_GUARD
                               + _NAME_TAIL, re.IGNORECASE) if alternatives else None)
        # Every address-book surname, and the word list's lower-case words: what
        # a surname after an initial is checked against (ADR-122).
        book = {(c.get('surname') or '').strip().lower() for c in hcp_map} - {''}
        lower_words = _lower_words()

        def _tail_is_name(word: str) -> bool:
            # In capitals, only an address-book surname: in MedUI's prompts a
            # capitalised word after "A" was a dose or report heading, not a name.
            if word.isupper():
                return word.lower() in book
            return word.lower() in book or word.lower() not in lower_words

        # An initial and an ordinary-word address-book surname, with no title,
        # where a post-nominal follows or the two are the whole line (ADR-123).
        ordinary_book = sorted({n for n, _ in titled_only + titled_first}, key=len, reverse=True)
        initialled = None
        if ordinary_book:
            names = '|'.join(re.escape(f) for n in ordinary_book
                             for f in (n[:1].upper() + n[1:].lower(), n.upper()))
            initialled = re.compile(
                r"(?<![\w'-])[A-Z]\.?[ \t]+(?:" + names + r")(?![\w'-])"
                r"(?=,?[ \t]+(?:" + '|'.join(_POST_NOMINALS) + r")\b"
                r"|[ \t]*(?:\n|$|" + _ESCAPED_BREAK + r"))", re.MULTILINE)
            # The whole line: nothing but spaces before the initial either.
            line_start = re.compile(r'(?:^|\n|' + _ESCAPED_BREAK + r')[ \t]*$')
        written = None
        if as_written:
            forms = sorted({f for name, _ in as_written for f in (name, name.upper())},
                           key=len, reverse=True)
            written = re.compile('(?:' + '|'.join(r'\b' + re.escape(f) + r'\b' for f in forms)
                                 + ')' + _COLLOCATION_GUARD)

        def _sub_names(segment: str) -> str:
            if combined:
                segment = combined.sub(_replace, segment)
            if written:
                segment = _redact_written(segment)
            if initialled:
                segment = _skip_tokens(initialled, _replace_initialled, segment)
            return segment

        def _skip_tokens(pattern, repl, segment: str) -> str:
            parts, last = [], 0
            for tm in _TOKEN_RE.finditer(segment):
                parts.append(pattern.sub(repl, segment[last:tm.start()]))
                parts.append(tm.group())
                last = tm.end()
            parts.append(pattern.sub(repl, segment[last:]))
            return ''.join(parts)

        def _replace_initialled(m: re.Match) -> str:
            after_postnominal = not re.match(r'[ \t]*(?:\n|$|' + _ESCAPED_BREAK + ')',
                                             m.string[m.end():])
            if not after_postnominal and not line_start.search(m.string[:m.start()]):
                return m.group(0)
            return vault.mint_hcp(m.group(0))

        def _redact_written(segment: str) -> str:
            # Runs after the main pass, so skip the tokens it just placed.
            parts, last = [], 0
            for tm in _TOKEN_RE.finditer(segment):
                parts.append(written.sub(_replace, segment[last:tm.start()]))
                parts.append(tm.group())
                last = tm.end()
            parts.append(written.sub(_replace, segment[last:]))
            return ''.join(parts)

        def _replace(m: re.Match) -> str:
            tail = m.groupdict().get('tail')
            if tail:
                # The surname, then an initial and a name: one person, one token
                # that restores all of it; the title stays in front (ADR-122).
                core = m.group(0)[:len(m.group(0)) - len(tail)]
                if not _tail_is_name(m.group('tailname')):
                    return _replace_core(core) + tail
                title = re.match(_TITLE_PREFIX, core, flags=re.IGNORECASE)
                cut = title.end() if title else 0
                return core[:cut] + vault.mint_hcp(core[cut:] + tail)
            return _replace_core(m.group(0))

        def _replace_core(matched: str) -> str:
            token = token_lookup.get(matched.lower())
            if token:
                return token
            # A titled match: the token is the name's, so the title stays in
            # front of it. Inside the token it was lost on de-redaction, which
            # restores the name alone: 'Dr Small' came back as 'Small' (ADR-054).
            title = re.match(_TITLE_PREFIX, matched, flags=re.IGNORECASE)
            if not title:
                return matched
            token = token_lookup.get(matched[title.end():].lower())
            return matched[:title.end()] + token if token else matched

        def _redact_str(text: str) -> str:
            # Skip regions already occupied by tokens from the patient pass.
            parts: list[str] = []
            last = 0
            for tm in _TOKEN_RE.finditer(text):
                parts.append(_sub_names(text[last:tm.start()]))
                parts.append(tm.group())
                last = tm.end()
            parts.append(_sub_names(text[last:]))
            return ''.join(parts)

        def _walk(obj, key=None):
            # Analyte names, units and codes never hold a practitioner.
            if isinstance(obj, str):
                if key and str(key).strip().lower() in NON_NAME_KEYS:
                    return obj
                return _redact_str(obj)
            if isinstance(obj, dict):
                return {k: _walk(v, k) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(item, key) for item in obj]
            if isinstance(obj, tuple):
                return tuple(_walk(item, key) for item in obj)
            return obj

        return _walk(data)

    # ── Internal helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _get_practice_map() -> list[dict]:
        """Return practice-wide HCP map.

        Hot path (within _PRACTICE_MEM_TTL): returns process-level in-memory cache
        with no BPSDB or PostgreSQL access.
        Warm path (TTL expired): re-checks the slow cache + source fingerprint, then
        refreshes the in-memory cache.
        """
        with _practice_lock:
            practice_mem = _env.state('practice_mem')
            if practice_mem is not None:
                contacts, fetched_at = practice_mem
                if (time.monotonic() - fetched_at) < _PRACTICE_MEM_TTL:
                    return contacts

        # In-memory stale — check the slow cache
        contacts = None
        cached_raw = _env.env().cache.get(_CACHE_KEY)
        if cached_raw:
            try:
                cached = json.loads(cached_raw) if isinstance(cached_raw, str) else cached_raw
                cached_at = datetime.fromisoformat(cached['cached_at'])
                age = datetime.now(timezone.utc) - cached_at.replace(tzinfo=timezone.utc)
                if age < timedelta(hours=_CACHE_TTL_HOURS):
                    if HCPRedactionService._fingerprint_matches(cached.get('fingerprint')):
                        contacts = cached['contacts']
            except (KeyError, ValueError, TypeError):
                pass

        if contacts is None:
            contacts = HCPRedactionService._rebuild_practice_map()

        with _practice_lock:
            _env.set_state('practice_mem', (contacts, time.monotonic()))

        return contacts

    @staticmethod
    def _common_first_names() -> frozenset:
        """First names shared by 20+ of the practice's patients, cached a day.

        Empty if BPSDB is unavailable — the surname rules then apply as before.
        """
        first_names_mem = _env.state('first_names_mem')
        if first_names_mem and (time.monotonic() - first_names_mem[1]) < _FIRST_NAMES_TTL:
            return first_names_mem[0]
        names: frozenset = frozenset()
        if _env.has_source():
            try:
                names = frozenset(r['name'] for r in _env.query('common_first_names')
                                  if r.get('name'))
            except Exception as exc:  # source boundary: fall back to the plain surname rules
                log.warning('HCPRedactionService: first names unavailable: %s', exc)
        _env.set_state('first_names_mem', (names, time.monotonic()))
        return names

    @staticmethod
    def _get_practice_doctors() -> list[dict]:
        """The practice's own doctors, in the forms their letters are signed.

        The address book held some doctors under a different first name and
        others not at all, so "Dr J. Smith" went out whole, or as "Dr J." and
        a token. Each doctor contributes "First Surname", "F. Surname" and
        "F Surname", bare and after "Dr"/"Dr."; the longest match wins, so a
        whole signature becomes one token, and two doctors sharing a surname
        keep different tokens. Cached for an hour; [] if BPSDB is unavailable.
        """
        cached = _env.state('doctors_mem')
        if cached and (time.monotonic() - cached[1]) < _DOCTORS_TTL:
            return cached[0]
        if not _env.has_source():
            return []
        try:
            rows = _env.query('practice_doctors')
        except Exception as exc:  # source boundary: logged; the address book still applies
            log.warning('HCPRedactionService: practice doctors unavailable: %s', exc)
            return []

        entries: list[dict] = []
        for r in rows:
            first = (r.get('first_name') or '').strip()
            surname = (r.get('surname') or '').strip()
            forms = []
            if first:
                initial = first[0].upper()
                bare = [f'{first} {surname}', f'{initial}. {surname}', f'{initial} {surname}']
                forms = bare + [f'{t} {b}' for t in ('Dr', 'Dr.') for b in bare]
            for index, form in enumerate(forms or [surname]):
                entries.append({'surname': surname, 'firstname': first, 'full_name': form,
                                'category': 'Practice doctor',
                                'contact_id': f"user:{r.get('user_id')}:{index}"})
        _env.set_state('doctors_mem', (entries, time.monotonic()))
        return entries

    @staticmethod
    def _get_practice_staff() -> list[dict]:
        """The practice's staff other than doctors, as "First Surname" and "F. Surname".

        Nurses and receptionists sign progress notes with their name on its own
        line, and only those who happened to be in the address book were
        redacted. Full names only, never the surname alone (ADR-040): a staff
        surname shared with a patient or a place would otherwise be redacted
        everywhere it appears. Labelled by BP user group ("Practice nurse").
        Cached for an hour; [] if BPSDB is unavailable.
        """
        cached = _env.state('staff_mem')
        if cached and (time.monotonic() - cached[1]) < _DOCTORS_TTL:
            return cached[0]
        if not _env.has_source():
            return []
        try:
            rows = _env.query('practice_staff')
        except Exception as exc:  # source boundary: logged; the address book still applies
            log.warning('HCPRedactionService: practice staff unavailable: %s', exc)
            return []

        entries: list[dict] = []
        for r in rows:
            first = (r.get('first_name') or '').strip()
            # BP marks some users with a bracketed suffix — "Smith (K)" — that
            # notes do not carry; left on, the name could never match.
            surname = re.sub(r'\s*\([^)]*\)\s*$', '', r.get('surname') or '').strip()
            if not first or not surname:
                continue
            forms = [f'{first} {surname}', f'{first[0].upper()}. {surname}']
            for index, form in enumerate(forms):
                entries.append({'surname': surname, 'firstname': first, 'full_name': form,
                                'category': (r.get('role') or 'Practice staff').strip(),
                                'full_name_only': True,
                                'contact_id': f"staff:{r.get('user_id')}:{index}"})
        _env.set_state('staff_mem', (entries, time.monotonic()))
        return entries

    @staticmethod
    def _rebuild_practice_map() -> list[dict]:
        if not _env.has_source():
            log.warning('HCPRedactionService: no identity source; returning empty HCP map')
            return []

        try:
            rows = _env.query('hcp_practice_contacts')
            fp_rows = _env.query('hcp_practice_contacts_fingerprint')
        except Exception as exc:
            log.error('HCPRedactionService._rebuild_practice_map error: %s', exc)
            return []

        contacts = [_normalise_contact(r) for r in rows]
        fingerprint = fp_rows[0] if fp_rows else {}

        payload = {
            'cached_at': datetime.now(timezone.utc).isoformat(),
            'fingerprint': fingerprint,
            'contacts': contacts,
        }
        _env.env().cache.set(_CACHE_KEY, json.dumps(payload),
                          description='HCP practice contact map cache (rebuilt by HCPRedactionService)')

        return contacts

    @staticmethod
    def _fingerprint_matches(stored_fp: dict | None) -> bool:
        """Return True when the CONTACTS table fingerprint matches stored values."""
        if not stored_fp:
            return False
        if not _env.has_source():
            return True  # can't verify; assume valid

        try:
            live = _env.query('hcp_practice_contacts_fingerprint')
            if not live:
                return False
            return live[0].get('cnt') == stored_fp.get('cnt') and \
                   live[0].get('max_id') == stored_fp.get('max_id')
        except Exception:
            return False

    @staticmethod
    def _get_patient_contacts(patient_id: int) -> list[dict]:
        patient_contacts_cache = _env.state('patient_contacts_cache')
        if patient_contacts_cache is None:
            patient_contacts_cache = {}
            _env.set_state('patient_contacts_cache', patient_contacts_cache)
        cached = patient_contacts_cache.get(patient_id)
        if cached and (time.monotonic() - cached[1]) < _PATIENT_CONTACTS_TTL:
            return cached[0]

        if not _env.has_source():
            return []

        try:
            corrin  = _env.query('hcp_patient_contacts_corrin', [patient_id])
            corrout = _env.query('hcp_patient_contacts_corrout', [patient_id])
        except Exception as exc:
            log.error('HCPRedactionService._get_patient_contacts error: %s', exc)
            return []

        seen: set[int] = set()
        results: list[dict] = []
        for row in (corrin or []) + (corrout or []):
            c = _normalise_contact(row)
            if c['contact_id'] not in seen:
                seen.add(c['contact_id'])
                results.append(c)

        patient_contacts_cache[patient_id] = (results, time.monotonic())
        return results


def _normalise_contact(row: dict) -> dict:
    surname  = (row.get('SURNAME') or '').strip()
    firstname = (row.get('FIRSTNAME') or '').strip()
    full = f'{firstname} {surname}'.strip() if firstname else surname
    return {
        'contact_id': row.get('CONTACTID', 0),
        'surname': surname,
        'firstname': firstname,
        'full_name': full,
        'category': (row.get('CATEGORYNAME') or '').strip(),
    }
