"""The practice's own nickname table (ADR-048).

Learned from BP: for each name, the preferred names recorded for patients who
carry it — William -> Bill, Robert -> Bob. A patient with no preferred name
recorded is still often called by the form most of their namesakes go by, and
that shortened name is on no identifier list. A pairing counts only when 3 or
more patients share it, so the table is aggregate data, never a record of one
person. Built from the practice's own entries, so it holds only forms staff
have actually typed — no generated nonsense — and the Australian forms a
generic list lacks.

The table changes slowly: kept in the environment's Cache, rebuilt weekly.
"""
import json
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import _env
from ._env import log

MIN_PATIENTS = 3        # a pairing shared by fewer could point at one person
TOP_FIRST = 3           # forms used for a patient's first name
TOP_MIDDLE = 1          # and for a middle name — people often go by it
MIN_FORM_LENGTH = 3     # 'Al', 'Ed' collide with abbreviations

_CACHE_KEY = 'practice_nickname_table'
_CACHE_TTL = timedelta(days=7)
_MEM_TTL = 3600
_lock = threading.Lock()

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]+")


def build_table(rows) -> dict[str, list[str]]:
    """{name: [forms, most common first]} from BP preferred-name rows.

    A preferred name equal to a recorded name, or containing the surname, is
    not a nickname. A two-word preferred name contributes its first word
    ("Bob Smith" -> Bob). A form that shares its first two letters with a
    middle name — and not with the first name — is that middle name's.
    """
    pairs: Counter = Counter()
    for r in rows:
        first_words = [w.lower() for w in re.split(r'[\s-]+', (r.get('FirstName') or '').strip()) if w]
        first = first_words[0] if first_words else ''
        middles = [w.lower() for w in first_words[1:] + re.split(r'[\s-]+', (r.get('MiddleName') or '').strip())
                   if len(w) > 1]
        surname = (r.get('Surname') or '').strip().lower()
        words = _WORD_RE.findall(r.get('PreferredName') or '')
        if not first or not words:
            continue
        form = words[0].lower()
        if form == first or form in middles or (surname and form in surname):
            continue
        owner = next((m for m in middles if form[:2] == m[:2] and form[:2] != first[:2]), first)
        pairs[(owner, form)] += 1

    table: dict[str, list[str]] = defaultdict(list)
    for (name, form), n in pairs.most_common():
        if n >= MIN_PATIENTS and len(form) >= MIN_FORM_LENGTH:
            table[name].append(form)
    return dict(table)


def get_table() -> dict[str, list[str]]:
    """The cached table; {} if BPSDB can't be reached (no forms, no harm)."""
    with _lock:
        mem = _env.state('nickname_mem')
        if mem and (time.monotonic() - mem[1]) < _MEM_TTL:
            return mem[0]

    table = None
    raw = _env.env().cache.get(_CACHE_KEY)
    if raw:
        try:
            cached = json.loads(raw) if isinstance(raw, str) else raw
            built = datetime.fromisoformat(cached['built_at'])
            if datetime.now(timezone.utc) - built.replace(tzinfo=timezone.utc) < _CACHE_TTL:
                table = cached['table']
        except (KeyError, ValueError, TypeError):
            table = None
    if table is None:
        table = _rebuild()
    with _lock:
        _env.set_state('nickname_mem', (table, time.monotonic()))
    return table


def _rebuild() -> dict[str, list[str]]:
    if not _env.has_source():
        return {}
    try:
        rows = _env.query('practice_preferred_names')
    except Exception as exc:  # source boundary: no table this time, retry on next expiry
        log.warning('practice_nickname_service: rebuild failed: %s', exc)
        return {}
    table = build_table(rows)
    _env.env().cache.set(_CACHE_KEY, json.dumps({
        'built_at': datetime.now(timezone.utc).isoformat(), 'table': table,
    }), description='Practice nickname table, from BP preferred names (ADR-048)')
    log.info('practice_nickname_service: rebuilt, %d names, %d forms',
                            len(table), sum(len(v) for v in table.values()))
    return table


# ── The administrators' list (ADR-049) ──────────────────────────────────────
# The name_variants list: name, form, active (`bps-redact lists import
# name_variants`). Each entry is a shortened name a reviewer has seen in real
# text. A person has vouched for each, so there is no 3-patient
# minimum and no top-N cap: every active form of the patient's first or
# middle name is used. Unreadable or missing table: no listed forms, logged.
LIST_TABLE = 'name_variants'
LIST_COLUMNS = (('name', 'TEXT'), ('form', 'TEXT'), ('active', 'BOOLEAN'),
                ('added_by', 'TEXT'), ('added_at', 'TIMESTAMP'),
                ('source_run', 'INTEGER'), ('note', 'TEXT'))
_LIST_TTL = 60          # a grid edit applies within a minute
_SWITCHED_OFF = frozenset({'false', 'f', 'no', 'n', '0', 'off'})


def listed_forms() -> dict[str, list[str]]:
    """{name: [forms]} from the administrators' list, active rows only."""
    list_mem = _env.state('list_mem')
    if list_mem and (time.monotonic() - list_mem[1]) < _LIST_TTL:
        return list_mem[0]
    out: dict[str, list[str]] = defaultdict(list)
    try:
        rows = _env.env().lists.rows(LIST_TABLE)
    except Exception as exc:                # list store boundary
        log.error('name-variant list unreadable: %s', exc)
        rows = []
    for row in rows:
        name = str(row.get('name') or '').strip().lower()
        form = str(row.get('form') or '').strip().lower()
        if (not name or len(form) < MIN_FORM_LENGTH or form == name
                or str(row.get('active')).strip().lower() in _SWITCHED_OFF):
            continue
        if form not in out[name]:
            out[name].append(form)
    listed = dict(out)
    _env.set_state('list_mem', (listed, time.monotonic()))
    return listed


def forget_listed_forms() -> None:
    """Drop the cached list so a new entry applies to the next run."""
    _env.set_state('list_mem', None)


# ── The curated list (0.2.14, MedUI ADR-148) ────────────────────────────────
# data/name_forms.csv: name, form. Common Australian short forms and spelling
# pairs, from general knowledge, never from a practice's records: Michael ->
# Mick, Elizabeth -> Libby, Sarah <-> Sara. Kept to forms in common use, so a
# pairing can't point at one person. No form here is an everyday-word name
# (firstnames.EVERYDAY_WORD_NAMES: Will, May, Bill, Rose…); those come only
# from the practice's own table or its list. Shipped with the package, so it
# needs nothing from the host. Not cached by TTL: it never changes at run time.
_curated: dict[str, list[str]] | None = None


def curated_forms() -> dict[str, list[str]]:
    """{name: [forms]} from the curated list shipped with the core."""
    global _curated
    if _curated is not None:
        return _curated
    import csv
    from importlib import resources
    out: dict[str, list[str]] = defaultdict(list)
    with resources.files(__package__).joinpath('data/name_forms.csv').open(newline='') as fh:
        for row in csv.DictReader(fh):
            name = (row.get('name') or '').strip().lower()
            form = (row.get('form') or '').strip().lower()
            if name and len(form) >= MIN_FORM_LENGTH and form != name and form not in out[name]:
                out[name].append(form)
    _curated = dict(out)
    return _curated


def forms_for(first_name: str, middle_name: str = '', recorded: set[str] | None = None) -> list[str]:
    """The forms to look for in this patient's text, lower-case.

    Every listed form of each first- or middle-name word (ADR-049), every
    curated form (ADR-148), then the learned table's top 3 forms of the first
    name and top 1 of each middle-name word (ADR-048), less any name the
    patient already has on record.
    """
    table = get_table()
    listed = listed_forms()
    curated = curated_forms()
    recorded = {r.lower() for r in (recorded or set())}
    first_words = [w.lower() for w in re.split(r'[\s-]+', (first_name or '').strip()) if len(w) > 1]
    middle_words = [w.lower() for w in re.split(r'[\s-]+', (middle_name or '').strip()) if len(w) > 1]
    out: list[str] = []
    for i, w in enumerate(first_words + middle_words):
        top = TOP_FIRST if i == 0 else TOP_MIDDLE
        for form in listed.get(w, []) + curated.get(w, []) + table.get(w, [])[:top]:
            if form not in recorded and form not in out:
                out.append(form)
    return out
