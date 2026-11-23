"""Common first names, swept from text last (ADR-058).

Carers and relatives — of disabled patients especially — are named in the notes
by first name, and are rarely the BP next of kin, so no record-based pass knows
them. This pass removes the first names most common among the practice's own
patients wherever they still stand in the text, with the capitalised word that
follows on the same line (a surname, more often than not; a street or an
organisation otherwise, of little value to the model). One reversible NM token
per person.

The list: first names held by at least MIN_PATIENTS patient records, less
ordinary English words and months — rebuilt weekly from BP — plus the
administrators' `first_names` list: `include` true adds a name, false takes one
out. Edits apply within a minute. A host may also add the practice's own users'
first names (ADR-134, `Redactor(practice_first_names=True)`): see practice_names().
"""
import json
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import _env
from ._env import log

MIN_PATIENTS = 10
_MONTHS = frozenset({'jan', 'feb', 'mar', 'apr', 'april', 'may', 'jun', 'june', 'jul', 'july',
                     'aug', 'august', 'sep', 'sept', 'oct', 'nov', 'dec'})

_CACHE_KEY = 'common_first_name_list'
_CACHE_TTL = timedelta(days=7)
_MEM_TTL = 3600
_LIST_TTL = 60
_lock = threading.Lock()

LIST_TABLE = 'first_names'
_SWITCHED_OFF = frozenset({'false', 'f', 'no', 'n', '0', 'off'})

# A capitalised word next to the name that is not a surname: after it ("Mary
# Has"), or before it with a comma ("Thanks, Mary"; "Daughter, Mary").
NOT_A_SURNAME = frozenset({
    'the', 'and', 'or', 'but', 'is', 'was', 'has', 'had', 'have', 'will', 'would', 'can', 'could',
    'should', 'may', 'might', 'to', 'for', 'from', 'with', 'at', 'in', 'on', 'of', 'by', 'as', 'if',
    'when', 'then', 'he', 'she', 'they', 'we', 'i', 'it', 'his', 'her', 'their', 'our', 'my', 're',
    'dr', 'mr', 'mrs', 'ms', 'miss', 'and/or', 'also', 'not', 'no', 'yes', 'ok', 'said', 'says',
    # Note verbs: "Mary Reviewed", "Susan Called".
    'reviewed', 'seen', 'called', 'rang', 'phoned', 'noted', 'discussed', 'advised', 'attended',
    'visited', 'reports', 'reported', 'states', 'stated',
    # Greetings and sign-offs before a comma (ADR-059).
    'hi', 'hello', 'dear', 'thanks', 'thank', 'regards', 'cheers', 'sincerely', 'cc', 'attn',
    # Spoken fillers before a comma, in transcripts: "Yeah, Sam" (ADR-133).
    'yeah', 'yep', 'okay', 'oh', 'ah', 'um', 'uh', 'hey', 'right', 'alright', "g'day", 'good',
    'exactly', "i'm", "i've", "i'll", "i'd",
    'attention', 'please', 'sorry', 'well', 'so', 'now', 'today', 'yesterday', 'here', 'there',
    # Relationships: worth keeping for the model (ADR-059).
    'mum', 'mother', 'dad', 'father', 'daughter', 'son', 'wife', 'husband', 'partner', 'carer',
    'sister', 'brother', 'friend', 'aunt', 'uncle', 'niece', 'nephew', 'grandmother',
    'grandfather', 'grandson', 'granddaughter', 'nan', 'nana', 'pop', 'guardian', 'neighbour',
    'stepmother', 'stepfather', 'support', 'worker', 'coordinator', 'nurse', 'doctor', 'patient',
})

# Names never matched in capitals, because the capitals are a clinical
# abbreviation: AVA, aortic valve area in echo reports (ADR-059).
_NOT_IN_CAPITALS = frozenset({'ava'})

# Lower case only for names this long or longer: "sam", "ian" are too short to
# trust in lower case (ADR-059). The practice's own users' first names, which are
# never ordinary words, from 4 letters ("paul", ADR-135).
_LOWER_MIN = 5
_PRACTICE_LOWER_MIN = 4


def _held(rows) -> Counter:
    held: Counter = Counter()
    for r in rows:
        first = (str(r.get('first_name') or '').split() or [''])[0].lower()
        if first.isalpha() and len(first) > 2:
            held[first] += int(r.get('n') or 0)
    return held


def build_names(rows) -> frozenset:
    """Lower-case first names held by MIN_PATIENTS or more records, as a set."""
    from .redaction import _lowercase_words
    words = _lowercase_words()
    return frozenset(n for n, c in _held(rows).items()
                     if c >= MIN_PATIENTS and n not in words and n not in _MONTHS)


def _learned() -> tuple[frozenset, frozenset]:
    """(the weekly sweep list, every first name held by at least one patient record)."""
    with _lock:
        mem = _env.state('sweep_names_mem')
        if mem and (time.monotonic() - mem[1]) < _MEM_TTL:
            return mem[0]
    result = None
    raw = _env.env().cache.get(_CACHE_KEY)
    if raw:
        try:
            cached = json.loads(raw) if isinstance(raw, str) else raw
            built = datetime.fromisoformat(cached['built_at'])
            if datetime.now(timezone.utc) - built.replace(tzinfo=timezone.utc) < _CACHE_TTL:
                result = (frozenset(cached['names']), frozenset(cached['held']))
        except (KeyError, ValueError, TypeError):
            result = None    # also a cache from before 0.2.9, which has no 'held'
    if result is None:
        result = _rebuild()
    with _lock:
        _env.set_state('sweep_names_mem', (result, time.monotonic()))
    return result


def learned_names() -> frozenset:
    """The weekly list from BP; empty if BP can't be reached (no sweep, no harm)."""
    return _learned()[0]


def held_names() -> frozenset:
    """First names held by at least one patient record; empty if BP can't be reached."""
    return _learned()[1]


def _rebuild() -> tuple[frozenset, frozenset]:
    if not _env.has_source():
        return frozenset(), frozenset()
    try:
        rows = _env.query('patient_first_name_counts')
    except Exception as exc:  # source boundary: no list this time, retry on next expiry
        log.warning('first_names: rebuild failed: %s', exc)
        return frozenset(), frozenset()
    names = build_names(rows)
    held = frozenset(n for n, c in _held(rows).items() if c > 0)
    _env.env().cache.set(_CACHE_KEY, json.dumps({
        'built_at': datetime.now(timezone.utc).isoformat(), 'names': sorted(names),
        'held': sorted(held),
    }), description='Common first names swept from AI text, from BP patients (ADR-058)')
    log.info('first_names: rebuilt, %d names', len(names))
    return names, held


def listed() -> tuple[frozenset, frozenset]:
    """(added, taken out) from the administrators' list."""
    mem = _env.state('sweep_list_mem')
    if mem and (time.monotonic() - mem[1]) < _LIST_TTL:
        return mem[0]
    added, removed = set(), set()
    try:
        rows = _env.env().lists.rows(LIST_TABLE)
    except Exception as exc:                # list store boundary
        log.error('first-name list unreadable: %s', exc)
        rows = []
    for row in rows:
        name = str(row.get('name') or '').strip().lower()
        if len(name) < 2:
            continue
        (removed if str(row.get('include')).strip().lower() in _SWITCHED_OFF else added).add(name)
    result = (frozenset(added), frozenset(removed))
    _env.set_state('sweep_list_mem', (result, time.monotonic()))
    return result


# ── The practice's own users' first names (ADR-134) ──────────────────────────
#
# A practice user's first name on its own ("Dr GERARD [Redacted HCP: …]", "Thanks,
# Zuzana") is left by the HCP pass, which takes full names only, and is missed by
# the sweep when fewer than MIN_PATIENTS patients share it. With the option on,
# every doctor's and staff member's first name is swept too, current or former, as
# BP holds them. Left out, as for the weekly list: ordinary words and months
# (an administrator adds those by hand, as for any name), everyday-word names,
# names of one or two letters and initials. Also left out: accounts named for a
# service rather than a person ("Telehealth Bookings", "Flu Clinic Northvale"),
# told by a first name no patient has and a surname that is an ordinary word or
# one of the practice's own towns.

_PRACTICE_TTL = 3600
_TITLE = re.compile(r'^(?:dr|mr|mrs|ms|miss|mx|prof)\.?$', re.I)


def user_first_names(first_name, surname) -> tuple[list[str], str]:
    """(first names, surname) of one BP user row. Some practices hold a doctor's
    whole name in the surname field ("Dr. Sam Pellow", first name empty); BP's
    bracketed suffix ("Pellow (K)") is dropped. Initials are not names."""
    surname_words = [w for w in re.sub(r'\s*\([^)]*\)\s*$', '', str(surname or '')).split()
                     if not _TITLE.match(w)]
    if str(first_name or '').strip():
        words = str(first_name).split()
    else:
        words, surname_words = surname_words[:-1], surname_words[-1:]
    firsts = [w for w in words if len(w) > 2 and re.fullmatch(r'[A-Za-z]+(?:-[A-Za-z]+)*', w)]
    return firsts, (surname_words[-1] if surname_words else '')


def practice_names() -> frozenset:
    """The practice users' first names, lower case; empty unless the host opted in."""
    e = _env.env()
    if not e.practice_first_names or not _env.has_source():
        return frozenset()
    mem = _env.state('sweep_practice_mem')
    if mem and (time.monotonic() - mem[1]) < _PRACTICE_TTL:
        return mem[0]
    from .redaction import _lowercase_words
    words = _lowercase_words()
    rows: list[dict] = []
    towns: set[str] = set()
    try:
        for q in ('practice_doctors', 'practice_staff'):
            rows += _env.query(q)
        for loc in _env.query('practice_locations'):
            towns.update(w.lower() for w in re.findall(r'[A-Za-z]+', str(loc.get('CITY') or '')))
    except Exception as exc:  # source boundary: no practice names this time, retry next hour
        log.warning('first_names: practice users unavailable: %s', exc)
        return frozenset()
    held = held_names()
    found: set[str] = set()
    for r in rows:
        firsts, surname = user_first_names(r.get('first_name'), r.get('surname'))
        for f in firsts:
            n = f.lower()
            if n in words or n in _MONTHS or n in EVERYDAY_WORD_NAMES:
                continue
            if n not in held and (surname.lower() in words or surname.lower() in towns):
                continue                                    # an account named for a service
            found.add(n)
    result = frozenset(found)
    _env.set_state('sweep_practice_mem', (result, time.monotonic()))
    return result


def names() -> frozenset:
    added, removed = listed()
    return (learned_names() | added | practice_names()) - removed


def pattern() -> re.Pattern | None:
    """The sweep's pattern: an optional "SURNAME, " before the name, the name in
    Title case, capitals or (5+ letters; a practice user's, 4+) lower case, and an
    optional following capitalised word. Case-sensitive by design."""
    current = names()
    practice = practice_names()
    mem = _env.state('sweep_pattern')
    if mem and mem[0] == (current, practice):
        return mem[1]
    compiled = None
    if current:
        forms = set()
        for n in current:
            forms.add(n.capitalize())
            if n not in _NOT_IN_CAPITALS:
                forms.add(n.upper())
            if len(n) >= _LOWER_MIN or (len(n) >= _PRACTICE_LOWER_MIN and n in practice):
                forms.add(n)
        word = r"[A-Z][a-zA-Z'’-]+"
        compiled = re.compile(
            r"(?<![\w'’-])(?:(?P<pre>" + word + r"),[ \t]+)?"
            r"(?P<name>" + '|'.join(re.escape(f) for f in sorted(forms, key=len, reverse=True)) + r")"
            r"(?![\w'’-])(?:[ \t]+(?P<post>" + word + r")(?![\w'’-]))?")
    _env.set_state('sweep_pattern', ((current, practice), compiled))
    return compiled


def token_span(m: re.Match) -> tuple[int, int]:
    """The part of a match that becomes the token: the name, with the surname
    before it or the word after it unless that is plainly not a surname."""
    start, end = m.start('name'), m.end('name')
    if m.group('pre') and m.group('pre').lower() not in NOT_A_SURNAME:
        start = m.start('pre')
    if m.group('post') and m.group('post').lower() not in NOT_A_SURNAME:
        end = m.end('post')
    return start, end


# ── The recording clinician's own first names, in a transcript (ADR-133) ──────
#
# In a consultation transcript a first name on its own is most often the
# clinician being spoken to ("thanks, Sam"), and speech-to-text often writes it
# in lower case. The host passes the recording clinician's names; they are
# matched in Title case, capitals and lower case. Lower case is not used for a
# name that is an everyday word, where the word is far more common than the
# name ("we will see", "a mark on the skin"), nor for names of one or two
# letters ("et al"). Those match in Title case and capitals only.

EVERYDAY_WORD_NAMES = frozenset({
    'will', 'may', 'mark', 'pat', 'sue', 'frank', 'bill', 'rose', 'grace', 'hope', 'joy', 'dawn',
    'jack', 'ray', 'don', 'rob', 'art', 'guy', 'iris', 'max', 'kit', 'drew', 'gene', 'grant', 'norm',
    'ward', 'dean', 'chase', 'chance', 'rich', 'sandy', 'penny', 'carol', 'holly', 'ivy', 'heather',
    'olive', 'ruby', 'amber', 'faith', 'summer', 'june', 'april', 'august', 'autumn', 'pearl', 'hazel',
    'ginger', 'crystal', 'sky', 'ash', 'cliff', 'glen', 'dale', 'rocky', 'rusty', 'dusty', 'misty',
    'sunny', 'candy', 'brandy', 'rod', 'nick', 'earl', 'eve', 'page', 'reed', 'hunter', 'brook',
    'sage', 'angel', 'bud', 'buck', 'dot', 'fern', 'lily', 'mercy', 'major', 'king', 'noble', 'jean',
    'jay', 'wren', 'robin', 'cherry', 'daisy', 'poppy', 'violet', 'jade', 'opal', 'coral', 'star',
    'storm', 'river', 'rain', 'lane', 'gale', 'bell', 'lance', 'bob', 'chip', 'chuck', 'mike', 'pip',
    'terry', 'duke', 'jewel', 'cash', 'gay', 'dick', 'sterling', 'forest', 'rowan', 'clay', 'flint',
})
_CLINICIAN_LOWER_MIN = 3


def clinician_pattern(names) -> re.Pattern | None:
    """The pattern for the clinician's own names: shaped as the sweep's, the name
    in Title case, capitals and (unless guarded) lower case. A possessive 's
    after the name is a boundary ("Sam's"), as for nicknames (ADR-117)."""
    forms: set[str] = set()
    for raw in names or ():
        for word in str(raw or '').split():
            word = word.strip(".,;:()[]\"'’")
            if len(word) < 2 or not re.fullmatch(r"[A-Za-z]+(?:-[A-Za-z]+)*", word):
                continue
            n = word.lower()
            forms.add('-'.join(p.capitalize() for p in n.split('-')))
            if n not in _NOT_IN_CAPITALS:
                forms.add(n.upper())
            if (len(n) >= _CLINICIAN_LOWER_MIN and n not in EVERYDAY_WORD_NAMES
                    and n not in _MONTHS):
                forms.add(n)
    if not forms:
        return None
    word = r"[A-Z][a-zA-Z'’-]+"
    return re.compile(
        r"(?<![\w'’-])(?:(?P<pre>" + word + r"),[ \t]+)?"
        r"(?P<name>" + '|'.join(re.escape(f) for f in sorted(forms, key=len, reverse=True)) + r")"
        r"(?![\w-])(?!['’](?!s\b)\w)(?:[ \t]+(?P<post>" + word + r")(?![\w'’-]))?")
