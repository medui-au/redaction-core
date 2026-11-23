"""Tier 0 and tier 1 detection — identifiers of people we can name.

Tier 0 searches the checked text for **that patient's own** identifiers. A hit
is a confirmed leak, not an inference.

Tier 1 does the same for the practice's contacts (health practitioners) and
the practice's own sites.

The search terms are every string the redaction builder makes
(redaction.identifier_entries) plus renderings only the checker looks for:
the point is to look for *more renderings than redaction removes*. Each term's
coverage is worked out, not written down: the term on its own is put through
the core's own prompt redaction, and

    coverage = 'missed_by_rules'   redaction removes this string, and it still
                                   got through — a bug worth chasing
    coverage = 'outside_rules'     redaction leaves this string alone — a rule
                                   worth adding

So when a new rule makes redaction catch a rendering, its findings change
label by themselves.

Identifiers read here stay on this host: they are compared locally and never
sent anywhere.
"""
import functools
import itertools
import re
import threading
import time
from datetime import datetime

from .. import _env
from .._env import log
from ..months import month_in_date
from ..words import word_list_path
from . import name_forms as nickname_service

# Per-patient identifier cache for the life of a scan. Demographics don't
# change mid-audit, and a scan revisits the same patients constantly.
_IDENT_TTL = 1800

_PRACTICE_TTL = 3600

_contacts_lock = threading.Lock()
_CONTACTS_TTL = 3600

# Names shorter than this match too much ordinary text to be useful.
_MIN_NAME_CHARS = 3

# A surname followed by one of these is a clinical term, not a person:
# "White Cell Count", "white matter", "white coat". The same list redaction
# uses (ADR-024), so findings and redaction agree about what counts as a name.
CLINICAL_COLLOCATIONS = (
    'cell', 'cells', 'count', 'counts', 'matter', 'coat', 'blood',
    'noise', 'spot', 'spots', 'reflex', 'sign', 'stain',
    'body', 'bodies', 'syndrome', 'disease',
)
_COLLOCATION_RE = re.compile(r'^\s+(?:%s)\b' % '|'.join(CLINICAL_COLLOCATIONS),
                             re.IGNORECASE)   # case-insensitive: "Cell" and "cell" alike


def _is_clinical_collocation(text: str, end: int) -> bool:
    """True when what follows the match makes it a clinical term."""
    return bool(_COLLOCATION_RE.match(text[end:end + 24]))

# Renderings the redaction builder doesn't make; its own come with its entries.
_EXTRA_DOB_FORMATS = ('%d-%m-%y', '%Y-%m-%d', '%b %d, %Y', '%B %-d, %Y')


# Street-type abbreviations, both directions. Measured on real prompts: 100 of
# 338 street-line occurrences matched only after such a swap.
_STREET_TYPES = {
    'street': 'st', 'road': 'rd', 'avenue': 'ave', 'drive': 'dr', 'court': 'ct',
    'crescent': 'cres', 'parade': 'pde', 'terrace': 'tce', 'highway': 'hwy',
    'place': 'pl', 'close': 'cl', 'lane': 'ln', 'boulevard': 'blvd',
    'circuit': 'cct', 'esplanade': 'esp', 'way': 'way', 'grove': 'gr',
}

# Rarer abbreviations of the same types, matched both ways (v21, MedUI ADR-077):
# a letter writes "Av" where BP holds "Avenue".
_STREET_TYPE_EXTRA = {
    'avenue': ('av',), 'parade': ('pd',), 'crescent': ('cr', 'cresc'),
    'terrace': ('ter', 'terr'), 'court': ('crt',), 'circuit': ('cir', 'crct'),
    'highway': ('hway',), 'esplanade': ('espl',), 'close': ('cls',),
}
_STREET_TYPE_FORMS = {full: (abbr,) + _STREET_TYPE_EXTRA.get(full, ())
                      for full, abbr in _STREET_TYPES.items()}
_ALL_STREET_TYPE_WORDS = frozenset(_STREET_TYPES) | {a for forms in _STREET_TYPE_FORMS.values() for a in forms}

_STATE_WORDS = r'(?i:QLD|NSW|VIC|SA|WA|TAS|NT|ACT|Queensland)'

# BP splits an address across ADDRESS1 (street) and ADDRESS2 (locality), and the
# two are stored as one line here. A report writes the street followed by the
# *suburb* from CITY instead, so the combined line never matches. Truncating at
# the street type gives a core that does, and the span rule then extends over
# whatever locality or suburb follows.
_STREET_CORE_RE = re.compile(
    r'^\s*(?:unit|u|flat|apt|lot|shop)?\s*\d+[A-Za-z]?(?:[-/,]\s*\d+[A-Za-z]?)?\s+.*?\b(?:%s)\b' %
    '|'.join(list(_STREET_TYPES) + list(_STREET_TYPES.values())),
    re.IGNORECASE,
)


def _street_core(street: str) -> str:
    """'732 Example Road Some Locality' -> '732 Example Road'. '' if no type found."""
    match = _STREET_CORE_RE.match(street or '')
    return match.group(0).strip() if match else ''

# How far past the street line a suburb (or, reaching past it, a postcode) may
# sit and still belong to the same address.
_TAIL_GAP_CITY = 12
_TAIL_GAP_POSTCODE = 25


def _hyphen_forms(text: str) -> set:
    """Two words of the street name, stored apart, written with a hyphen between
    them: 'Wombat Hollow Rd' as 'Wombat-Hollow Road' (v21, MedUI ADR-077). One
    pair at a time, never joined to the street type."""
    forms, words = set(), text.split(' ')
    for i in range(len(words) - 1):
        a, b = words[i], words[i + 1]
        if (re.fullmatch(r"[A-Za-z][A-Za-z'’]+", a) and re.fullmatch(r"[A-Za-z][A-Za-z'’]+", b)
                and b.lower().rstrip('.') not in _ALL_STREET_TYPE_WORDS):
            forms.add(' '.join(words[:i] + [a + '-' + b] + words[i + 2:]))
    return forms


def _glued_forms(text: str) -> set:
    """The street line with the space after the house number, or before the type,
    dropped by text extraction: '1234Wombat Hollow Rd', '1234 Wombat-HollowRd'
    (v21, MedUI ADR-079)."""
    forms = set()
    number = re.match(r'^(\d{1,5}[A-Za-z]?)\s+(?=[A-Za-z])', text)
    words = text.split(' ')
    typed = (len(words) >= 3 and words[-1].lower().rstrip('.') in _ALL_STREET_TYPE_WORDS
             and re.fullmatch(r"[A-Za-z][A-Za-z'’-]*", words[-2]))
    if number:
        forms.add(number.group(1) + text[number.end():])
    if typed:
        joined = ' '.join(words[:-2] + [words[-2] + words[-1]])
        forms.add(joined)
        if number:
            forms.add(number.group(1) + joined[number.end():])
    return forms


@functools.lru_cache(maxsize=4096)
def _compiled(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


def _street_renderings(street: str) -> list[str]:
    """The street line as stored, plus abbreviation and unit-number variants."""
    if not street:
        return []
    out = {street}
    core = _street_core(street)
    if core and core.lower() != street.lower():
        out.add(core)
        out.update(_street_renderings(core))
    lowered = street.lower()
    for full, abbrs in _STREET_TYPE_FORMS.items():
        for abbr in abbrs:
            if re.search(r'\b' + full + r'\b', lowered):
                out.add(re.sub(r'\b' + full + r'\b', abbr, street, flags=re.I))
                out.add(re.sub(r'\b' + full + r'\b', abbr + '.', street, flags=re.I))
            if re.search(r'\b' + abbr + r'\.?\b', lowered):
                out.add(re.sub(r'\b' + abbr + r'\.?\b', full, street, flags=re.I))
    unit = re.match(r'^(?:unit|u|flat|apt)\s*(\d+[a-z]?)[,/ ]+(\d+.*)$', street, re.I)
    if unit:
        out.add('{}/{}'.format(unit.group(1), unit.group(2)))
        out.add('U{}/{}'.format(unit.group(1), unit.group(2)))
    out |= {h for rendering in out for h in _hyphen_forms(rendering)}
    out |= {g for rendering in out for g in _glued_forms(rendering)}
    # The line as stored first: callers label index 0 'street_line_only', and a
    # set's order would otherwise decide which rendering that is.
    return [street] * (len(street) > 5) + sorted(s for s in out if len(s) > 5 and s != street)


_WORD_LIST_PATHS = (word_list_path(),)
_word_set: frozenset | None = None


def ordinary_words() -> frozenset:
    """Ordinary English words — about one contact surname in seven is one.

    A one-word identifier that is an ordinary word is searched for only when
    capitalised, so "small bowel" isn't reported as a leak for a patient named
    Small. Redaction's rule too (ADR-025), so findings and redaction agree.
    """
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
                    if w.strip() and "'" not in w)
                return _word_set
            except OSError:
                break
    log.warning('no system word list; ordinary-word names will be '
                               'searched case-insensitively and may over-report')
    _word_set = frozenset()
    return _word_set


# ── Spelling variants: same identifier, different spacing or punctuation ──────
# A report writing "10 Buonavista Ave" for a stored "10 Buona Vista Avenue"
# matched nothing: neither redaction nor the checker collapsed the internal
# space, so the checker shared redaction's blind spot and a clean scan proved
# nothing.

_STREET_NUMBER_RE = re.compile(
    r'^(?:unit|u|flat|apt|lot|shop|\d+[a-z]?(?:[-/]\d+[a-z]?)?)$', re.IGNORECASE)


def _spelling_variants(value: str) -> list[tuple[str, str]]:
    """(value, variant_form) pairs differing only in spacing or punctuation.

    Matching is case-insensitive, so 'VanDyke' also covers 'Vandyke'.
    """
    text = (value or '').strip()
    if not text:
        return []
    out: list[tuple[str, str]] = []
    if ' ' in text:
        out.append((re.sub(r'\s+', '', text), 'spacing_collapsed'))
    if "'" in text or '-' in text:
        out.append((text.replace("'", '').replace('-', ''), 'punctuation_stripped'))
        out.append((text.replace("'", ' ').replace('-', ' '), 'punctuation_spaced'))
    return out


def _given_name_words(value: str) -> list[str]:
    """Each word of a multi-word given name, as redaction builds them (ADR-042).

    BP often holds first and middle names together in FirstName, so the name a
    patient is called by never appeared on its own. Single letters dropped.
    """
    parts = [w for w in re.split(r'[\s-]+', (value or '').strip()) if w]
    if len(parts) < 2:
        return []
    return [w for w in parts if len(w) > 1]


def _collapse_street_name(street: str) -> str:
    """'10 Buona Vista Avenue' -> '10 BuonaVista Avenue'.

    Only the words between the street number and the street type are joined.
    The number (including a unit prefix) keeps its spacing, and anything after
    the street type — a suburb carried on the same line — is left alone.
    Returns '' when there is nothing to collapse.
    """
    tokens = (street or '').split()
    if len(tokens) < 3:
        return ''

    start = 0
    while start < len(tokens) and _STREET_NUMBER_RE.match(tokens[start]):
        start += 1

    # The *last* street-type token, not the first: 'St Marys Close' has 'St'
    # inside the name, and the real type is 'Close'. A wrong guess only ever
    # produces a variant that matches nothing.
    end = len(tokens)
    for index in range(start, len(tokens)):
        token = tokens[index].lower().rstrip('.')
        if token in _ALL_STREET_TYPE_WORDS:
            end = index

    middle = tokens[start:end]
    if len(middle) < 2:
        return ''
    return ' '.join(tokens[:start] + [''.join(middle)] + tokens[end:])


# ── Street name without its house number ──────────────────────────────────────
# A patient at '<n> Mt <Name> Road' had 'Mt <Name> Road' — no number —
# survive in a document summary. Every rendering above keeps the number, so
# the name alone, or the name split from its number by RTF codes ('\fs20'),
# never matched.

_MOUNT_FORMS = ('Mt', 'Mt.', 'Mount')


def _street_name_renderings(street: str) -> list[str]:
    """'12 Mt Example Road Locality' -> 'Mt Example Road', 'Mount Example Rd', ...

    Only when at least two words precede the street type: 'Mt Example Road',
    not 'Main Road' — a one-word name is too common a road to belong to one
    patient. The street type is the *last* one on the line, as in
    _collapse_street_name. Returns [] when there is no street type.
    """
    tokens = (street or '').split()
    start = 0
    while start < len(tokens) and _STREET_NUMBER_RE.match(tokens[start].rstrip(',')):
        start += 1

    end = None
    for index in range(start, len(tokens)):
        token = tokens[index].lower().rstrip('.')
        if token in _ALL_STREET_TYPE_WORDS:
            end = index
    if end is None or end - start < 2:
        return []

    words = [t.rstrip(',') for t in tokens[start:end]]
    street_type = tokens[end].lower().rstrip('.')
    full = next(f for f, forms in _STREET_TYPE_FORMS.items() if street_type == f or street_type in forms)
    type_forms = {full.title()} | {a.title() + dot for a in _STREET_TYPE_FORMS[full] for dot in ('', '.')}

    word_forms = [[w] for w in words]
    for index, word in enumerate(words):
        if word.lower().rstrip('.') in ('mt', 'mount'):
            word_forms[index] = list(_MOUNT_FORMS)

    out: dict[str, str] = {}
    for combination in itertools.product(*word_forms, sorted(type_forms)):
        rendering = ' '.join(combination)
        out.setdefault(rendering.lower(), rendering)
    return [s for s in out.values() if len(s) > 5]


def _digits(value: str) -> str:
    return re.sub(r'\D', '', value or '')


_AREA_CODES = ('07', '02', '03', '08')


def _national_forms(digits: str) -> list[str]:
    """Every 10-digit national number a stored value could be written as.

    Many patients have an 8-digit home number stored with no area code, while
    documents write it as '07xxxxxxxx'. Word boundaries stop the
    8-digit identifier matching inside that longer run — correctly — so the
    area-code forms have to be generated instead.
    """
    if len(digits) == 10 and digits.startswith('0'):
        return [digits]
    if len(digits) == 9 and digits.startswith('4'):        # mobile missing its 0
        return ['0' + digits]
    if len(digits) == 11 and digits.startswith('61'):      # stored international
        return ['0' + digits[2:]]
    if len(digits) == 8:                                   # landline, no area code
        return [code + digits for code in _AREA_CODES]
    return []


def _phone_renderings(digits: str) -> list[str]:
    """Spacings and prefixes an Australian number is written in."""
    out: list[str] = []
    if len(digits) == 8:
        out.append(f'{digits[:4]} {digits[4:]}')
    for national in _national_forms(digits):
        out.append(national)
        if national.startswith('04'):
            out.append(f'{national[:4]} {national[4:7]} {national[7:]}')
            out.append(f'{national[:4]}-{national[4:7]}-{national[7:]}')
            out.append(f'+61 {national[1:4]} {national[4:7]} {national[7:]}')
            out.append(f'+61{national[1:]}')
        else:
            out.append(f'({national[:2]}) {national[2:6]} {national[6:]}')
            out.append(f'{national[:2]} {national[2:6]} {national[6:]}')
            out.append(f'({national[:2]}) {national[2:]}')
            out.append(f'+61 {national[1:2]} {national[2:6]} {national[6:]}')
    return out


def _phone_partial_renderings(digits: str) -> list[str]:
    """A half-spaced or dotted number: 'xxxx xxxxxx', 'xxxx.xxx.xxx'.

    A patient's mobile was found in free text as 'xxxx xxxxxx' — four digits,
    one space, then the remaining six — when only the fully spaced, closed and
    hyphenated forms were searched for, so only the shaped pattern pass saw it.
    Redaction has since learnt these forms; coverage labels say so.
    """
    out: list[str] = []
    for national in _national_forms(digits):
        if national.startswith('04'):
            out.append(f'{national[:4]} {national[4:]}')
            out.append(f'{national[:4]}.{national[4:7]}.{national[7:]}')
        else:
            out.append(f'{national[:2]} {national[2:]}')
            out.append(f'{national[:2]}.{national[2:6]}.{national[6:]}')
    return out


def _checker_cache(name: str) -> dict:
    """A cache dict for the checker, one per environment (Redactor)."""
    caches = _env.state('checker_caches')
    if caches is None:
        caches = {}
        _env.set_state('checker_caches', caches)
    return caches.setdefault(name, {})


# ── Coverage: does redaction remove this string? ──────────────────────────────

_PROBE_KEY = 'coverage-probe'
_PROBE_SEPARATOR = '\n\n'


def _covered(values: list[str], patient_id: int | None) -> list[bool]:
    """For each string: does the core's prompt redaction change it, on its own?

    One redaction over all of them, a blank line apart (no rule reaches across
    one), instead of one call each: the nickname pass reads BP on every call.
    """
    if not values:
        return []
    from ..profiles import redact_prompt
    from ..vault import TokenVault
    out = redact_prompt(_PROBE_SEPARATOR.join(values), patient_id, TokenVault(_PROBE_KEY))
    parts = out.split(_PROBE_SEPARATOR)
    if len(parts) == len(values):
        return [after != before for before, after in zip(values, parts)]
    log.warning('coverage probe: batch split mismatch, probing one at a time')
    return [redact_prompt(v, patient_id, TokenVault(_PROBE_KEY)) != v for v in values]


def _label(variants: list[dict], patient_id: int | None) -> list[dict]:
    """Fill in each variant's coverage. A form searched for only when capitalised
    is probed capitalised, as it would be found."""
    probes = [v['value'][:1].upper() + v['value'][1:] if v.get('match_mode') == 'capitalised'
              else v['value'] for v in variants]
    for variant, covered in zip(variants, _covered(probes, patient_id)):
        variant['coverage'] = 'missed_by_rules' if covered else 'outside_rules'
    return variants


# ── Tier 0: the run's own patient ─────────────────────────────────────────────

def _checker_fields(row: dict) -> dict:
    """The redaction builder's demographics row, under the names the renderers use."""
    fields = dict(row)
    for mine, theirs in (('ADDRESS1', 'ADDRESS1_PART'), ('ADDRESS2', 'ADDRESS2_PART'),
                         ('CITY', 'CITY_PART'), ('POSTCODE', 'POSTCODE_PART'),
                         ('MEDICARENO', 'MEDICARENO_CARD')):
        fields[mine] = row.get(theirs)
    # Shown with a display prefix: '(H) 0491570158'.
    for field in ('PHONE_HOME', 'PHONE_MOBILE', 'PHONE_WORK'):
        fields[field] = re.sub(r'^\([HMW]\)\s*', '', row.get(field) or '')
    return fields


def get_patient_identifiers(patient_id: int) -> list[dict]:
    """Identifier variants for one patient: {value, kind, variant_form, coverage, ...}."""
    cached = _checker_cache('ident').get(patient_id)
    if cached and (time.monotonic() - cached[1]) < _IDENT_TTL:
        return cached[0]

    from ..redaction import RedactionService
    row = RedactionService._get_demographics(patient_id)
    variants = _label(_build_variants(row), patient_id) if row else []
    _checker_cache('ident')[patient_id] = (variants, time.monotonic())
    return variants


def _build_variants(row: dict) -> list[dict]:
    """The checker's own renderings first, then every string redaction builds.

    Where both make a string, the checker's kind and form label it; redaction's
    entries add what the checker never generated. Coverage is filled in later.
    """
    from ..nicknames import forms_for
    from ..redaction import identifier_entries
    demographics = _checker_fields(row)
    out: list[dict] = []
    seen: set[str] = set()

    def add(value, kind: str, variant_form: str, score: float = 1.0,
            match_mode: str = 'any', extend_to: list | None = None,
            pattern: str | None = None) -> None:
        text = str(value).strip() if value else ''
        if not text or len(text) < _MIN_NAME_CHARS:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        # A one-word name that is also an ordinary English word is only a name
        # when capitalised.
        if (match_mode == 'any' and kind in ('name', 'name_variant', 'nok_name')
                and text.isalpha() and text.lower() in ordinary_words()):
            match_mode = 'capitalised'
        entry = {'value': text, 'kind': kind, 'variant_form': variant_form,
                 'coverage': None, 'score': score, 'match_mode': match_mode,
                 'extend_to': extend_to or []}
        if pattern:
            entry['pattern'] = pattern   # matched as a pattern, not as text (v21)
        out.append(entry)

    first = (demographics.get('FIRSTNAME') or '').strip()
    middle = (demographics.get('MIDDLENAME') or '').strip()
    surname = (demographics.get('SURNAME') or '').strip()
    preferred = (demographics.get('PREFERREDNAME') or '').strip()
    full = f'{first} {surname}'.strip()

    for value, form in ((first, 'first'), (middle, 'middle'), (surname, 'surname'),
                        (full, 'full')):
        add(value, 'name', form)
    if first and surname:
        add(f'{first} {surname}', 'name', 'first_surname')
    if first and middle and surname:
        add(f'{first} {middle} {surname}', 'name', 'first_middle_surname')
    if preferred:
        add(preferred, 'name', 'preferred')
        if surname:
            add(f'{preferred} {surname}', 'name', 'preferred_surname')
    if first and surname:
        add(f'{surname}, {first}', 'name', 'surname_comma_first')
        add(f'{surname} {first}', 'name', 'surname_first')
    # Spacing and punctuation variants, and each word of a multi-word given
    # name on its own ('Mary Jane' -> 'Mary').
    for value in (first, middle, surname, preferred):
        for variant, form in _spelling_variants(value):
            add(variant, 'name', form)
    given_words = [w for v in (first, middle, preferred) for w in _given_name_words(v)]
    for word in given_words:
        add(word, 'name', 'given_word')

    # Given-name variants: BP holds no record of them, so exact matching can't
    # find them. Scored below an exact match — evidence, not proof — and
    # searched for only when capitalised where the form is an ordinary word.
    # Each given word too: 'Mary Jane' has no nicknames, 'Mary' has several.
    for source_name in dict.fromkeys((first, preferred, *given_words)):
        for variant in nickname_service.variants(source_name):
            weight = 0.6 if variant['source'] == 'nickname' else 0.5
            add(variant['value'], 'name_variant',
                f"{variant['source']}:{variant['value']}",
                score=weight, match_mode=variant['match_mode'])

    # The forms redaction's nickname pass uses (ADR-048, ADR-049): the
    # practice's learned table and the administrators' list. Title case only
    # when the form is an ordinary word, any case otherwise (ADR-056).
    recorded = {w for r in (first, middle, surname, preferred) for w in re.split(r'[\s-]+', r) if w}
    for form in forms_for(first, middle, recorded=recorded):
        add(form, 'name_variant', f'practice:{form}', score=0.7)

    _add_dob_variants(demographics.get('DOB'), add)
    _add_contact_variants(demographics, add)
    _add_government_ids(demographics, add)
    _add_nok_variants(demographics, add)

    # Everything else redaction builds: composite strings, NOK first names, ...
    for value, kind, form in identifier_entries(row):
        add(value, kind, f'redaction:{form}')
    return out


def _add_dob_variants(dob_raw, add) -> None:
    if not dob_raw:
        return
    add(str(dob_raw), 'dob', 'raw')
    try:
        dob = datetime.strptime(str(dob_raw)[:10], '%Y-%m-%d')
    except (ValueError, TypeError):
        return
    for fmt in _EXTRA_DOB_FORMATS:
        try:
            rendered = dob.strftime(fmt)
        except ValueError:
            continue
        add(rendered, 'dob', f'dob:{fmt}')


def _add_contact_variants(demographics: dict, add) -> None:
    add(demographics.get('EMAIL'), 'email', 'as_stored')

    street = ' '.join(part for part in (demographics.get('ADDRESS1'),
                                        demographics.get('ADDRESS2')) if part).strip()
    city = (demographics.get('CITY') or '').strip()
    postcode = (demographics.get('POSTCODE') or '').strip()
    if street and city:
        add(f'{street}, {city} {postcode}'.strip(), 'address', 'as_stored')

    # The street line does the finding; the suburb/postcode anchors say how far
    # the address extends. Both ends are this patient's own values, so the span
    # is anchored at both sides and cannot run away.
    anchors = [a for a in (postcode, city) if len(a) >= 3]
    for index, rendering in enumerate(_street_renderings(street)):
        form = 'street_line_only' if index == 0 else 'street_line_variant'
        add(rendering, 'address', form, extend_to=anchors)
        # Same line written with the street name closed up, or without its
        # hyphen/apostrophe. Applied to every rendering, so the abbreviated
        # street type is covered too ('10 Buonavista Ave').
        collapsed = _collapse_street_name(rendering)
        if collapsed:
            add(collapsed, 'address', 'street_spacing_collapsed', extend_to=anchors)
        for variant, variant_form in _spelling_variants(rendering):
            if variant_form == 'spacing_collapsed':
                continue  # the whole line with no spaces at all is never written
            add(variant, 'address', f'street_{variant_form}', extend_to=anchors)
    for rendering in _street_name_renderings(street):
        add(rendering, 'address', 'street_name_no_number', extend_to=anchors)

    # The patient's suburb with their postcode, without a street line (v21,
    # MedUI ADR-060/074/075): CITY, and ADDRESS2 when it is a place name — a
    # rural address keeps its locality there and the postal town in CITY. The
    # end of a multi-word suburb counts too; a state may sit between.
    if re.fullmatch(r'\d{4}', postcode):
        line2 = (demographics.get('ADDRESS2') or '').strip()
        localities = [(city, 'locality_postcode')]
        if re.fullmatch(r"[A-Za-z][A-Za-z '’.-]+", line2) and line2.lower() != city.lower():
            localities.append((line2, 'line2_locality_postcode'))
        for locality, form in localities:
            words = locality.split()
            if len(locality) < 3:
                continue
            forms = [' '.join(words[i:]) for i in range(len(words)) if len(words[-1]) >= 3]
            alternation = '|'.join(re.escape(f).replace(r'\ ', r'\s+') for f in forms)
            add(f'{locality} {postcode}', 'address', form,
                pattern=r'(?<!\w)(?:' + alternation + r')[ \t,]{1,3}(?:' + _STATE_WORDS
                        + r'\.?[ \t,]{1,3})?' + postcode + r'(?!\d)')

    for field in ('PHONE_HOME', 'PHONE_MOBILE', 'PHONE_WORK'):
        digits = _digits(demographics.get(field) or '')
        # Any spacing: up to three spaces, dots or hyphens between any two digits
        # ('04 02470481', '0-418-791.445'; v21, MedUI ADR-068), or a run of 4 to 12
        # spaces or tabs, as column-aligned text writes it (MedUI ADR-152). An 8-digit
        # number is also looked for without its area code, as the redactor takes it.
        gap = r'(?:[ \t.\-]{0,3}|[ \t]{4,12})'
        forms = _national_forms(digits) if len(digits) >= 8 else []
        if forms and not forms[0].startswith('04'):
            # The local 8 digits alone, each area digit without its zero before
            # them ('75566 7788'; MedUI ADR-153), and the zero without its area
            # digit ('05566 7788'; MedUI ADR-154).
            forms = forms + [forms[0][2:]] + [f[1:] for f in forms] + ['0' + forms[0][2:]]
        for national in forms:
            add(f'{national} (any spacing)', 'phone', 'any_spacing',
                pattern=r'(?<![\d])' + gap.join(national) + r'(?!\d)')

    for field in ('PHONE_HOME', 'PHONE_MOBILE', 'PHONE_WORK'):
        raw = (demographics.get(field) or '').strip()
        if not raw:
            continue
        add(raw, 'phone', 'as_stored')
        digits = _digits(raw)
        if len(digits) >= 8:
            add(digits, 'phone', 'digits_only')
            form = 'with_area_code' if len(digits) == 8 else 'spaced_or_prefixed'
            for rendering in _phone_renderings(digits):
                add(rendering, 'phone', form)
            for rendering in _phone_partial_renderings(digits):
                add(rendering, 'phone', 'partial_spacing')


def _add_government_ids(demographics: dict, add) -> None:
    for field in ('PENSIONNO', 'DVANO', 'IHI'):
        add(demographics.get(field), field.lower(), 'as_stored')

    medicare = _digits(demographics.get('MEDICARENO') or '')
    if len(medicare) == 10:
        add(medicare, 'medicareno', 'digits_only')
        for irn in '123456789':
            add(medicare + irn, 'medicareno', 'with_irn')
        add(f'{medicare[:4]} {medicare[4:9]} {medicare[9:]}', 'medicareno', 'grouped')
        # Any grouping, or cut short to its first 7 to 9 digits (MedUI ADR-153).
        from ..redaction import _medicare_own_regex
        add(f'{medicare[:7]} (any grouping or cut short)', 'medicareno', 'any_grouping',
            pattern=_medicare_own_regex(medicare))


def _add_nok_variants(demographics: dict, add) -> None:
    """Next of kin and emergency contact — a third party in the patient's record."""
    for index in (1, 2):
        first = (demographics.get(f'NOK{index}_FIRSTNAME') or '').strip()
        surname = (demographics.get(f'NOK{index}_SURNAME') or '').strip()
        if first and surname:
            add(f'{first} {surname}', 'nok_name', 'full_name')
        add(surname, 'nok_name', 'surname')
        for value in (first, surname):
            for variant, form in _spelling_variants(value):
                add(variant, 'nok_name', form)
        for phone_field in (f'NOK{index}_PHONE1', f'NOK{index}_PHONE2'):
            digits = _digits(demographics.get(phone_field) or '')
            if len(digits) >= 8:
                add(digits, 'nok_phone', 'digits_only')
                for rendering in _phone_renderings(digits):
                    add(rendering, 'nok_phone', 'spaced_or_prefixed')
                for rendering in _phone_partial_renderings(digits):
                    add(rendering, 'nok_phone', 'partial_spacing')


def scan_chunk_patient(masked_chunk: str, offset: int, identifiers: list[dict]) -> list[dict]:
    """Tier 0 — occurrences of this patient's own identifiers in the sent text."""
    findings: list[dict] = []
    lowered = masked_chunk.lower()
    for identifier in identifiers:
        if identifier.get('pattern'):
            continue                      # after the exact forms, below
        value = identifier['value']
        needle = value.lower()
        end_offset = len(value)
        start = lowered.find(needle)
        while start != -1:
            matched = masked_chunk[start:start + end_offset]
            # Forms that are also ordinary words ('will', 'may') are only
            # names when written as a name — Title case. All-caps is excluded
            # deliberately: the dataset lists "Dob" as a diminutive of Robert,
            # which otherwise matches the literal label "DOB:" in every prompt.
            # The patient's own names (not nicknames) also count in capitals —
            # "RE: MARY ROSE" — as redaction matches them (ADR-045).
            # A name with capitals inside it counts as stored too — "McDonald",
            # "VanDyke" (ADR-055); Title case alone missed it.
            capital_ok = (identifier.get('match_mode') != 'capitalised'
                          or (matched[:1].isupper() and matched[1:].islower())
                          or (matched.isupper() and identifier['kind'] in ('name', 'nok_name'))
                          or (matched == value and value[1:] != value[1:].lower()))
            after_hyphen = (identifier['kind'] == 'name_variant' and start > 0
                            and masked_chunk[start - 1] == '-')
            verb_opening = (identifier['kind'] == 'name_variant'
                            and needle in _AUXILIARY_FORMS
                            and _opens_sentence(masked_chunk, start))
            # A name that is also a month, in a date, is the date (ADR-118):
            # redaction keeps it, so it is not reported.
            in_date = (identifier['kind'] in ('name', 'name_variant', 'nok_name')
                       and month_in_date(masked_chunk, start, start + end_offset))
            # A name with an s straight after it, as redaction takes it (ADR-141):
            # "Marigolds' daughter", "Marigolds daughter".
            name_end = start + end_offset
            if identifier['kind'] in ('name', 'name_variant', 'nok_name'):
                name_end += _name_s_suffix(masked_chunk, name_end, value,
                                           identifier.get('match_mode') == 'capitalised')
            if (capital_ok and not after_hyphen and not verb_opening and not in_date
                    and _is_word_bounded(masked_chunk, start, name_end, value)
                    and not _is_contraction(masked_chunk, name_end)
                    and not (identifier['kind'] in ('name', 'name_variant', 'nok_name')
                             and _is_clinical_collocation(masked_chunk, name_end))):
                match_end = _extend_to_anchor(masked_chunk, name_end,
                                              identifier.get('extend_to'))
                findings.append({
                    'finding_type': f"patient_{identifier['kind']}",
                    'match_start': offset + start,
                    'match_end': offset + match_end,
                    'score': identifier.get('score', 1.0),
                    'tier': 0,
                    'variant_form': identifier['variant_form'],
                    'coverage': identifier['coverage'],
                })
            start = lowered.find(needle, start + 1)
    # Pattern identifiers (v21): a span an exact form already found is not
    # reported twice.
    taken = [(f['match_start'] - offset, f['match_end'] - offset, f['finding_type']) for f in findings]
    for identifier in identifiers:
        if not identifier.get('pattern'):
            continue
        finding_type = f"patient_{identifier['kind']}"
        for match in _compiled(identifier['pattern']).finditer(masked_chunk):
            if any(t == finding_type and s < match.end() and match.start() < e for s, e, t in taken):
                continue
            taken.append((match.start(), match.end(), finding_type))
            findings.append({
                'finding_type': finding_type,
                'match_start': offset + match.start(),
                'match_end': offset + match.end(),
                'score': identifier.get('score', 1.0),
                'tier': 0,
                'variant_form': identifier['variant_form'],
                'coverage': identifier['coverage'],
            })
    findings += _glued_names(masked_chunk, lowered, offset, identifiers, findings)
    return findings


# The patient's own names searched for glued to a letter or digit (v29, MedUI ADR-141).
_GLUED_FORMS = ('first', 'surname', 'preferred')
_GLUED_MIN_CHARS = 5


def _glued_names(masked_chunk: str, lowered: str, offset: int, identifiers: list[dict],
                 found: list[dict]) -> list[dict]:
    """The patient's first name, surname or preferred name glued to a letter or digit.

    "xQuillfeather", "Quillfeather2", "MARIGOLDQUILLFEATHER" hid the name from
    redaction and from the whole-word search above. Redaction does not take these
    (coverage outside_rules): a tier 0 finding, so the request pauses for the user
    to remove or allow it. Only names of 5+ letters that are not ordinary words,
    so "Ann" is not found in "announce", "Hill" in "Hillside" or a short name in a
    longer one; never when the word it sits in is an ordinary word.
    """
    words = ordinary_words()
    out: list[dict] = []
    taken = [(f['match_start'] - offset, f['match_end'] - offset) for f in found]
    for identifier in identifiers:
        value = identifier['value']
        if (identifier['kind'] != 'name' or identifier['variant_form'] not in _GLUED_FORMS
                or identifier.get('match_mode') == 'capitalised'
                or not value.isalpha() or len(value) < _GLUED_MIN_CHARS):
            continue
        needle = value.lower()
        start = lowered.find(needle)
        while start != -1:
            end = start + len(needle)
            before = lowered[start - 1:start]
            after = lowered[end:end + 1]
            if ((before.isalnum() or after.isalnum())
                    and not any(s < end and start < e for s, e in taken)):
                run_start, run_end = start, end
                while run_start > 0 and lowered[run_start - 1].isalpha():
                    run_start -= 1
                while run_end < len(lowered) and lowered[run_end].isalpha():
                    run_end += 1
                run = lowered[run_start:run_end]
                if run not in words and not (run.endswith('s') and run[:-1] in words):
                    taken.append((start, end))
                    out.append({
                        'finding_type': 'patient_name',
                        'match_start': offset + start,
                        'match_end': offset + end,
                        'score': identifier.get('score', 1.0),
                        'tier': 0,
                        'variant_form': identifier['variant_form'] + '_glued',
                        'coverage': 'outside_rules',
                    })
            start = lowered.find(needle, start + 1)
    return out


def _extend_to_anchor(text: str, end: int, anchors: list | None) -> int:
    """Extend a match to a trailing suburb or postcode, if one is close by.

    Without this, flagging the street line leaves the suburb and postcode
    sitting there, which was common in real prompts. The postcode is
    tried first because it sits after the suburb: anchoring on the suburb would
    stop short. Never crosses a line break.
    """
    if not anchors:
        return end
    for anchor, gap in ((anchors[0], _TAIL_GAP_POSTCODE),
                        *[(a, _TAIL_GAP_CITY) for a in anchors[1:]]):
        window = text[end:end + gap + len(anchor)]
        if '\n' in window:
            window = window.split('\n')[0]
        position = window.lower().find(anchor.lower())
        if position != -1:
            return end + position + len(anchor)
    return end


def _is_word_bounded(text: str, start: int, end: int, value: str) -> bool:
    """Require word boundaries for alphanumeric identifiers.

    Without this, a surname like 'Long' matches inside 'belongs'.
    """
    if not value[:1].isalnum():
        return True
    before = text[start - 1] if start > 0 else ' '
    after = text[end] if end < len(text) else ' '
    return not (before.isalnum() or after.isalnum())


_CONTRACTION_RE = re.compile(r"['’]t\b")


def _name_s_suffix(text: str, end: int, value: str, capitalised: bool) -> int:
    """How many characters of an s straight after a name belong to it (ADR-141).

    2 for a possessive written s' ("Marigolds'"), 1 for a bare s ("Marigolds
    daughter"), else 0. A name matched in any case does not take a bare s that
    makes it an ordinary word, as redaction does not.
    """
    if not value[-1:].isalpha() or text[end:end + 1] not in ('s', 'S'):
        return 0
    if text[end + 1:end + 2] in ("'", '’') and not text[end + 2:end + 3].isalnum():
        return 2
    if text[end + 1:end + 2].isalnum():
        return 0
    if not capitalised and (value + 's').lower() in ordinary_words():
        return 0
    return 1


def _is_contraction(text: str, end: int) -> bool:
    """True when the match is the front of a contraction: "Don" in "Don't".

    The apostrophe counts as a word boundary, so "Don't" matched as a
    diminutive of Donald. A possessive ("Don's") is still a name and still matches.
    """
    return bool(_CONTRACTION_RE.match(text, end))


# Name variants that are also auxiliary verbs. Title case can't tell them
# apart at the start of a sentence: "...at the clinic. Will continue with
# exercises" was flagged as William's nickname. Only the patient's variants:
# a recorded name ("Will" in BP) is still flagged anywhere.
_AUXILIARY_FORMS = frozenset({'will', 'may', 'can', 'did', 'must', 'shall'})
# What can come just before a sentence: its end, a line break (real or the
# escaped '\n' of a rendered dict), a label or speaker colon, a list bullet, or
# the quote that opens a field value.
_SENTENCE_LEAD_RE = re.compile(r"(?:^|[.!?:;\n•*'\"(]|\\[n\ue000]|^\s*-|\n\s*-)\s*$")


def _opens_sentence(text: str, start: int) -> bool:
    """True when the word at start is the first word of a sentence."""
    return bool(_SENTENCE_LEAD_RE.search(text[max(0, start - 40):start]))


def get_patient_names(patient_ids: list[int]) -> dict[int, str]:
    """Display names for several patients in one query.

    Used by the review screens. One round trip for a page of rows, rather
    than the per-patient identifier query, which is far heavier.
    """
    ids = sorted({int(pid) for pid in patient_ids if pid})
    if not ids:
        return {}
    try:
        rows = _env.query('patient_names', ids)
    except Exception as exc:  # pyodbc raises several unrelated error types
        log.error('patient name lookup failed: %s', exc)
        return {}

    names: dict[int, str] = {}
    for row in rows:
        first = (row.get('FIRSTNAME') or '').strip()
        surname = (row.get('SURNAME') or '').strip()
        preferred = (row.get('PREFERREDNAME') or '').strip()
        label = f'{first} {surname}'.strip()
        if preferred and preferred.lower() != first.lower():
            label = f'{first} "{preferred}" {surname}'.strip()
        names[row['INTERNALID']] = label or str(row['INTERNALID'])
    return names


# ── Tier 1: named practitioners from the practice contacts ────────────────────

def get_contacts_matcher() -> re.Pattern | None:
    """Regex matching practitioner names, in precise forms only.

    Surname alone is deliberately not matched: with thousands of surnames in a
    practice's contacts, many are ordinary words and every transcript would light
    up. Full name, or a title with a surname, is specific enough to mean
    something.

    The practice's own BP users are added as "First Surname" and "F. Surname":
    nurses and receptionists are rarely in the address book, and their names
    in progress notes went unreported — as they went unredacted (ADR-040).
    """
    matcher, fetched_at = _env.state('contacts_matcher') or (None, 0.0)
    if matcher is not None and (time.monotonic() - fetched_at) < _CONTACTS_TTL:
        return matcher

    with _contacts_lock:
        try:
            rows = _env.query('hcp_practice_contacts')
            users = _env.query('practice_users')
        except Exception as exc:  # pyodbc raises several unrelated error types
            log.error('practice contacts lookup failed: %s', exc)
            return None

        surnames: set[str] = set()
        full_names: set[str] = set()
        for row in rows:
            surname = (row.get('SURNAME') or '').strip()
            first = (row.get('FIRSTNAME') or '').strip()
            if len(surname) < _MIN_NAME_CHARS:
                continue
            surnames.add(surname)
            if len(first) >= _MIN_NAME_CHARS:
                full_names.add(f'{first} {surname}')
        for row in users:
            first = (row.get('first_name') or '').strip()
            # BP marks some users with a bracketed suffix ("Smith (K)") that
            # notes do not carry.
            surname = re.sub(r'\s*\([^)]*\)\s*$', '', row.get('surname') or '').strip()
            if len(first) >= 2 and len(surname) >= _MIN_NAME_CHARS:
                full_names.update((f'{first} {surname}', f'{first[0].upper()}. {surname}'))

        if not surnames:
            return None

        titled = r'(?:Dr\.?|Doctor|Prof\.?|Professor)\s+(?:%s)' % '|'.join(
            sorted((re.escape(s) for s in surnames), key=len, reverse=True))
        parts = [titled]
        if full_names:
            parts.append('|'.join(sorted((re.escape(n) for n in full_names),
                                         key=len, reverse=True)))
        pattern = re.compile(r'\b(?:%s)\b' % '|'.join(parts), re.IGNORECASE)
        _env.set_state('contacts_matcher', (pattern, time.monotonic()))
        return pattern


# ── Tier 1: the practice's own sites ─────────────────────────────────────────

def get_practice_identifiers() -> list[dict]:
    """Address, phone, fax and email strings for every practice site.

    Exactly the strings redaction builds for the practice pass, each with a
    form read from its shape and its coverage worked out. Cached for the
    process.
    """
    practice_cache = _env.state('checker_practice_cache')
    if practice_cache and (time.monotonic() - practice_cache[1]) < _PRACTICE_TTL:
        return practice_cache[0]

    from ..redaction import RedactionService
    out = []
    for value in RedactionService.get_practice_identifiers():
        if '@' in value:
            form = 'site_email'
        elif re.fullmatch(r'[\d\s()+.-]+', value):
            form = 'site_phone'
        else:
            form = 'site_address_full' if ',' in value else 'site_address'
        out.append({'value': value, 'form': form, 'coverage': None})
    _label(out, None)

    _env.set_state('checker_practice_cache', (out, time.monotonic()))
    return out


def scan_chunk_practice(masked_chunk: str, offset: int,
                        practice: list[dict]) -> list[dict]:
    """Tier 1 — the practice's own contact details in the sent text."""
    findings: list[dict] = []
    lowered = masked_chunk.lower()
    for entry in practice:
        value = entry['value']
        needle = value.lower()
        start = lowered.find(needle)
        while start != -1:
            if _is_word_bounded(masked_chunk, start, start + len(value), value):
                findings.append({
                    'finding_type': 'practice_contact_detail',
                    'match_start': offset + start,
                    'match_end': offset + start + len(value),
                    'score': 1.0,
                    'tier': 1,
                    'variant_form': entry['form'],
                    'coverage': entry['coverage'],
                })
            start = lowered.find(needle, start + 1)
    return findings


def get_practice_name_patterns() -> list:
    """The practice's own name in every form redaction takes (ADR-121)."""
    from ..redaction import RedactionService
    return [pattern for pattern, _clean in RedactionService.get_practice_name_patterns()]


def scan_chunk_practice_name(masked_chunk: str, offset: int, patterns: list) -> list[dict]:
    """Tier 1 — a form of the practice's own name left in the sent text."""
    return [{
        'finding_type': 'practice_contact_detail',
        'match_start': offset + match.start(),
        'match_end': offset + match.end(),
        'score': 1.0,
        'tier': 1,
        'variant_form': 'site_name',
        'coverage': 'missed_by_rules',
    } for pattern in patterns for match in pattern.finditer(masked_chunk)]


def scan_chunk_contacts(masked_chunk: str, offset: int,
                        matcher: re.Pattern | None) -> list[dict]:
    """Tier 1 — a named practice contact appearing in the sent text."""
    if matcher is None:
        return []
    return [{
        'finding_type': 'practice_contact_name',
        'match_start': offset + match.start(),
        'match_end': offset + match.end(),
        'score': 1.0,
        'tier': 1,
        'variant_form': 'contacts_match',
        'coverage': 'missed_by_rules',
    } for match in matcher.finditer(masked_chunk)
      if not _is_clinical_collocation(masked_chunk, match.end())]
