"""RedactionService — patient PII extraction and recursive text redaction.

get_identifiers() pulls name/DOB/address/contact/ID strings from BPSDB.
redact() replaces all occurrences (longest-first, case-insensitive) in any
nested data structure, minting TokenVault tokens for each unique identifier.
"""

import itertools
import re
import time
from datetime import datetime

from . import _env
from ._env import log
from .escapes import _TO_MARK as _ESCAPE_MARKS
from .months import month_in_date
from .vault import TokenVault
from .words import word_list_path

# Per-patient identifier cache: {patient_id: (identifiers, fetched_at)}
# 5-minute TTL — patient demographics change rarely within a session.
# (Kept per environment: see _cache().)
_IDENTIFIER_TTL = 300  # seconds

# Regex identifiers (currently the address span) keyed the same way.

# Street name without its house number (ADR-033), keyed the same way.

# Practice locations change about never; one fetch an hour is plenty.
_PRACTICE_TTL = 3600


def _cache(name: str) -> dict:
    """A cache dict that belongs to the current environment (one per Redactor)."""
    caches = _env.state('caches')
    if caches is None:
        caches = {}
        _env.set_state('caches', caches)
    return caches.setdefault(name, {})

# Matches any token previously placed by either redaction pass.
# Segments that match are skipped during replacement to prevent double-redaction.
_TOKEN_RE = re.compile(r'\[Redacted [A-Za-z]+: [A-Z]+-[0-9a-f]+\]')

# Words that turn a surname into a clinical term. "White Cell Count" is an
# analyte, not Dr White; "white matter" and "white coat" likewise. A match
# followed by one of these is left alone, in both the patient and HCP passes.
# Without it, atomised pathology results were corrupted.
# Kept deliberately narrow: each word must be one that turns a surname into a
# recognised clinical term. 'test', 'line', 'score' and 'scale' were dropped —
# they follow a real surname too easily ("Dr White test results").
CLINICAL_COLLOCATIONS = (
    'cell', 'cells', 'count', 'counts', 'matter', 'coat', 'blood',
    'noise', 'spot', 'spots', 'reflex', 'sign', 'stain',
    'body', 'bodies', 'syndrome', 'disease',
)
# Inline (?i:) rather than relying on the pattern's flags: the capitalised
# matcher below is deliberately case-sensitive, and without this the guard
# would miss "White Cell Count" while catching "white cell count".
# Same line only (MedUI ADR-083): with \s+ the guard looked past trailing spaces and
# blank lines, so a surname ending one line was kept whenever a later line
# began with "Count" or "Blood".
_COLLOCATION_GUARD = r'(?![^\S\n]+(?i:%s)\b)' % '|'.join(CLINICAL_COLLOCATIONS)


# An s straight after a name (ADR-141): a possessive written s' ("Marigolds'"), or
# no apostrophe at all ("Marigolds daughter"). Either case: "MARIGOLDS'".
_NAME_S_SUFFIX = r"(?P<s>[sS](?:['’](?![^\W_]))?)"


def _plain_s_word(name: str, tail: str, words) -> bool:
    """True when a bare s makes the name an ordinary word: "Deans", "Jeans"."""
    return len(tail) == 1 and (name + tail).lower() in words


def _safe_sub(pattern: re.Pattern, replacement, text: str) -> str:
    """Apply pattern.sub only to non-token segments of text.

    A match that is only a month word in a date is left as it is (ADR-118).
    """
    def _repl(m: re.Match) -> str:
        if month_in_date(m.string, m.start(), m.end()):
            return m.group(0)
        return replacement(m) if callable(replacement) else m.expand(replacement)

    parts: list[str] = []
    last = 0
    for m in _TOKEN_RE.finditer(text):
        parts.append(pattern.sub(_repl, text[last:m.start()]))
        parts.append(m.group())
        last = m.end()
    parts.append(pattern.sub(_repl, text[last:]))
    return ''.join(parts)


# Street-type abbreviations, both directions. Measured on real prompts, about
# three in ten street-line occurrences matched only after such a swap — a report writes
# "14 Example St" where BP holds "14 Example Street".
_STREET_TYPES = {
    'street': 'st', 'road': 'rd', 'avenue': 'ave', 'drive': 'dr', 'court': 'ct',
    'crescent': 'cres', 'parade': 'pde', 'terrace': 'tce', 'highway': 'hwy',
    'place': 'pl', 'close': 'cl', 'lane': 'ln', 'boulevard': 'blvd',
    'circuit': 'cct', 'esplanade': 'esp', 'way': 'way', 'grove': 'gr',
}
# Less common abbreviations of the same types, matched as well as the one above
# but never written in its place (ADR-077): a letter wrote "Av" where BP holds
# "Avenue".
_STREET_TYPE_EXTRA = {
    'avenue': ('av',), 'parade': ('pd',), 'crescent': ('cr', 'cresc'),
    'terrace': ('ter', 'terr'), 'court': ('crt',), 'circuit': ('cir', 'crct'),
    'highway': ('hway',), 'esplanade': ('espl',), 'close': ('cls',),
}
_STREET_TYPE_FORMS = {full: (abbr,) + _STREET_TYPE_EXTRA.get(full, ())
                      for full, abbr in _STREET_TYPES.items()}
_ALL_STREET_TYPE_WORDS = frozenset(_STREET_TYPES) | {a for forms in _STREET_TYPE_FORMS.values() for a in forms}
# Not when a street type follows (ADR-142): "12 Quillfeathers Road" is a street named for
# the surname. Left whole for the street-name and address passes, which take the
# whole street; taking "Quillfeather" first left the number and the type behind.
_NOT_A_STREET = (r'(?![^\S\n]+(?i:%s)\b)'
                 % '|'.join(sorted(_ALL_STREET_TYPE_WORDS, key=len, reverse=True)))
# Nor when "Rd" or "Road" follows one or two words later (ADR-143): "Quillfeathers
# Gully Creek Rd". Road only: "St" can be Saint ("Quillfeathers wife at St Vincent's").
_NOT_A_ROAD = r"(?![^\S\n]+(?:[A-Za-z'’-]+[^\S\n]+){1,2}(?i:rd|road)\b)"
# Nor straight after a house number (ADR-143): "123 Quillfeathers Gully Creek Rd" is an
# address, whatever follows; the address passes take it whole.
_HOUSE_NUMBER_BEFORE = re.compile(r'\d[A-Za-z]?[^\S\n]+$')


def _after_house_number(m: re.Match) -> bool:
    return bool(_HOUSE_NUMBER_BEFORE.search(m.string, max(0, m.start() - 8), m.start()))

# The street line is truncated here before matching. BP's ADDRESS1 often carries
# a locality after the street type — '732 Example Road Some Locality' — while a
# report writes the street followed by the *suburb* from the CITY field instead.
# Matching the stored line verbatim therefore finds nothing; matching the core
# (number + name + type) finds it, and the span rule then extends over whatever
# locality or suburb follows.
_STREET_CORE_RE = re.compile(
    r'^\s*(?:unit|u|flat|apt|lot|shop)?\s*\d+[A-Za-z]?(?:[-/,]\s*\d+[A-Za-z]?)?\s+.*?\b(?:%s)\b' %
    '|'.join(sorted(_ALL_STREET_TYPE_WORDS, key=len, reverse=True)),
    re.IGNORECASE,
)


def _street_core(street: str) -> str:
    """'732 Example Road Some Locality' -> '732 Example Road'.

    Returns '' when no street type is recognised, in which case the caller
    keeps the whole line.
    """
    match = _STREET_CORE_RE.match(street or '')
    return match.group(0).strip() if match else ''

# How far past the street line a suburb or postcode may sit and still count as
# part of the same address. Spans measured on real prompts ran 20-39 characters
# in total, so the gap itself is small; widening this to 100 changed nothing.
_ADDRESS_TAIL_GAP = 12

# Reaching a postcode means crossing the suburb, so this one is wider. Real
# spans measured 20-39 characters end to end, street line included.
_ADDRESS_SPAN_GAP = 25


def _punctuation_forms(text: str) -> set:
    """A street written with its apostrophes dropped or spaced, and its hyphens
    with or without spaces around them (ADR-063).

    "O'Reilly Road" is typed "OReilly Road" or "O Reilly Road"; a road stored as
    "Smith- Jones Road" appears as "Smith-Jones Road". Every rendering is matched
    exactly, so a punctuation difference let the whole street line through.
    """
    forms = {text}
    if "'" in text or '’' in text:
        forms.add(re.sub(r"['’]", '', text))
        forms.add(re.sub(r"\s+", ' ', re.sub(r"['’]", ' ', text)).strip())
    if '-' in text:
        for form in list(forms):
            forms.add(re.sub(r'\s*-\s*', '-', form))
            forms.add(re.sub(r'\s*-\s*', ' - ', form))
            forms.add(re.sub(r'\s+', ' ', re.sub(r'\s*-\s*', ' ', form)).strip())
    # The reverse: two words of the name, stored apart, written with a hyphen
    # between them ("Word Word Rd" as "Word-Word Road"; ADR-077). One pair at a
    # time, never into the street type.
    for form in list(forms):
        words = form.split(' ')
        for i in range(len(words) - 1):
            a, b = words[i], words[i + 1]
            if (re.fullmatch(r"[A-Za-z][A-Za-z'’]+", a) and re.fullmatch(r"[A-Za-z][A-Za-z'’]+", b)
                    and b.lower().rstrip('.') not in _ALL_STREET_TYPE_WORDS):
                forms.add(' '.join(words[:i] + [a + '-' + b] + words[i + 2:]))
    return forms


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

    # Unit forms: '2/14 Example St' where BP holds 'Unit 2 14 Example St'
    unit = re.match(r'^(?:unit|u|flat|apt)\s*(\d+[a-z]?)[,/ ]+(\d+.*)$', street, re.I)
    if unit:
        out.add('{}/{}'.format(unit.group(1), unit.group(2)))
        out.add('U{}/{}'.format(unit.group(1), unit.group(2)))
    out = {f for rendering in out for f in _punctuation_forms(rendering)}   # ADR-063
    out |= {g for rendering in out for g in _glued_forms(rendering)}        # ADR-079
    return [s for s in out if len(s) > 5]


def _glued_forms(text: str) -> set:
    """The street line with the space after the house number, or before the
    street type, gone (ADR-079).

    Text extracted from a PDF or a scan can drop either space: "1234Wombat
    Road", "1234 Wombat-HollowDR". The patient's own street only, so the exact
    match keeps it specific.
    """
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


# The patient's street name without its house number (ADR-033). Every rendering
# above keeps the number, so 'Mt Example Road' on its own — or split from its
# number by RTF codes — was never redacted.
_STREET_NUMBER_TOKEN_RE = re.compile(
    r'^(?:unit|u|flat|apt|lot|shop|\d+[a-z]?(?:[-/]\d+[a-z]?)?)$', re.IGNORECASE)
_MOUNT_FORMS = ('Mt', 'Mt.', 'Mount')

# Written in place of the street name. Fixed text, not a vault token: nothing
# downstream needs the name restored.
STREET_NAME_PLACEHOLDER = '[Redacted Address]'

# Anyone else's date of birth (ADR-047): a full date straight after a DOB or
# date-of-birth label, once the patient's own has become a token — a relative
# in an escort line ("daughter, DOB 01/02/1980"), a client named in an outside
# report. The label is the proof it is a date of birth. Fixed text, as for
# addresses (ADR-035): nothing downstream needs it restored. A token between
# label and date — the patient's own DOB already redacted — ends the match, so
# a later, unrelated date is never taken for one.
DOB_PLACEHOLDER = '[Redacted DOB]'
_OTHER_DOB_RE = re.compile(
    r'(?i)(\b(?:d\.?\s?o\.?\s?b\.?|date\s+of\s+birth|born)\b[^\w\[\]\n]{0,6})'
    r'(\d{1,2}[/.-]\d{1,2}[/.-](?:\d{4}|\d{2})(?![\d/.-]\d)'
    r'|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+\d{4})')

# Anyone else's address (ADR-035): a specialist's rooms, a pathology or imaging
# site, a hospital — and the patient's *previous* addresses, which BP does not
# keep, so nothing record-based can catch them. A house number, one to three
# capitalised words and a street type, with any unit/suite/level prefix and a
# following suburb + state + postcode; or a PO Box / Locked Bag. Case-sensitive
# on purpose: "2 tablets daily" has no capitalised street type. Replaced with
# the same fixed text as the patient's street name — nothing needs restoring,
# and in practice about half could not be told apart from a clinician's.
_STREET_TYPE_WORDS = ('Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Crescent|Cres|Court|Ct|Parade|Pde|'
                      'Place|Pl|Lane|Ln|Terrace|Tce|Highway|Hwy|Close|Cl|Boulevard|Blvd|'
                      'Circuit|Cct|Esplanade|Esp|Way|Grove|Gr|Square|Sq')
_ADDRESS_TAIL = (r'(?:,?\s+(?:[A-Z][A-Za-z\'-]+\s+){0,3}'
                 r'(?:QLD|NSW|VIC|SA|WA|TAS|NT|ACT|Qld|Vic|Tas)\.?,?\s+\d{4}(?!\d))?')
_OTHER_STREET_RE = re.compile(
    r'(?<![\w/.:$])'
    r'(?:(?:Unit|Suite|Shop|Level|Lvl|Ste|U|UNIT|SUITE|LEVEL|SHOP)\s*\d+[A-Za-z]?\s*[,/]?\s*)?'
    # Number, name and street type on one line (ADR-071): a year ending one
    # line and "Referring Officer Dr" opening the next is not a street.
    r'\d{1,5}[A-Za-z]?(?:[-/]\d{1,5}[A-Za-z]?)?[^\S\r\n]+'
    r'((?:[A-Z][A-Za-z\'-]+[^\S\r\n]+){1,3})'
    # "Dr" followed by an initial is a title, not Drive: "… HILLVIEW Dr J. Smith"
    # had its title taken as the street type and left the name (ADR-064).
    r'(?!(?:Dr|DR)\.?[ \t]+[A-Z]\.?[ \t])'
    r'(?:' + _STREET_TYPE_WORDS + '|' + _STREET_TYPE_WORDS.upper() + r')\.?(?!\w)'
    + _ADDRESS_TAIL)
_OTHER_POBOX_RE = re.compile(
    r'\b(?:P\.?\s?O\.?\s?Box|GPO Box|PO BOX|Locked Bag|LOCKED BAG)\s+\d+' + _ADDRESS_TAIL)
# A locality after the patient's street that is not their own suburb (ADR-064):
# a former address, or the same road in another town. Words and a state, words
# and a postcode, or words in capitals — never a field label ("PHONE", "DOB").
_STATE_WORDS = r'(?:QLD|NSW|VIC|SA|WA|TAS|NT|ACT|Qld|Vic|Tas)'
_LOCALITY_LABELS = (r'(?:PH|PHONE|MOB|MOBILE|TEL|FAX|EMAIL|HOME|WORK|DOB|DATE|AGE|SEX|NOK|'
                    r'CONTACT|MEDICARE|PO|BOX|POSTAL|AND|THE|FOR|WITH|FROM|TO|OF|ON|AT|IN)')
_NOT_LABEL = r'(?!(?i:' + _LOCALITY_LABELS + r')\b)'
_LOCALITY_WORD = _NOT_LABEL + r'[A-Z][A-Za-z\'-]+(?!:)[ \t]+'
_OTHER_LOCALITY_TAIL = (
    r'(?:,?[ \t]+(?:' + _LOCALITY_WORD + r'){1,3}' + _STATE_WORDS + r'\.?(?:,?[ \t]+\d{4}(?!\d))?'
    r'|,?[ \t]+(?:' + _LOCALITY_WORD + r'){1,3}\d{4}(?!\d)'
    r'|,?[ \t]+' + _NOT_LABEL + r'[A-Z]{3,}'
    r'(?:[ \t]+' + _NOT_LABEL + r'[A-Z]{3,}){0,2}(?![\w:]))')


def _own_locality_tail(postcode: str) -> str:
    """_OTHER_LOCALITY_TAIL after the patient's own street (ADR-081): the four
    digits after the locality words must be the patient's own postcode, or follow
    a gap of at most three spaces. A document that prints the address twice on one
    line, in two columns, puts the second copy's house number a long gap after the
    suburb; any four digits there took it as a postcode, and the second copy's
    street words were left in view.
    """
    tight_word = _NOT_LABEL + r"[A-Z][A-Za-z'-]+(?!:)[ \t]{1,3}"
    own = (r'|,?[ \t]+(?:' + _LOCALITY_WORD + r'){1,3}' + re.escape(postcode) + r'(?!\d)'
           if re.fullmatch(r'\d{4}', postcode or '') else '')
    return (r'(?:,?[ \t]+(?:' + _LOCALITY_WORD + r'){1,3}' + _STATE_WORDS + r'\.?(?:,?[ \t]+\d{4}(?!\d))?'
            + own
            + r'|,?[ \t]+(?:' + tight_word + r'){1,3}\d{4}(?!\d)'
            r'|,?[ \t]+' + _NOT_LABEL + r'[A-Z]{3,}'
            r'(?:[ \t]+' + _NOT_LABEL + r'[A-Z]{3,}){0,2}(?![\w:]))')
# A house, lot or rural number written in front of the street name (ADR-064).
# Any space but a line break after it: documents converted from RTF or HTML put
# a no-break space there, and the number stayed in front of the token (ADR-069).
_STREET_NUMBER_LEAD = (r'(?<![\w/.:$])(?i:(?:RMB|R\.M\.B\.|Lot|Unit|U|Shop|Lvl|Level|Flat|Apt)\.?[^\S\r\n]*)?'
                       r'\d{1,5}[A-Za-z]?(?:[-/]\d{1,5}[A-Za-z]?)?,?[^\S\r\n]+')
# The street between a house number and the patient's own suburb with their
# postcode: up to four words, any case, then a street type, on one line
# (ADR-075). Only ever tried in front of that pair, which is the patient's.
_PAIR_STREET = (r"(?:[^\W\d_][\w'’.-]*[^\S\r\n]+){1,4}?(?:" + _STREET_TYPE_WORDS
                + r")\.?,?[^\S\r\n]+")
# The house number in front of the pair may also follow a literal "\n" (or its
# mask while the rules run, ADR-116): the
# prompt pass reads data blocks written out as Python text, where a line break
# is those two characters and the number would otherwise look glued to an "n".
_PAIR_LEAD = r'(?:(?<=\\n)|(?<=\\\ue000)|(?<![\w/.:$]))' + _STREET_NUMBER_LEAD[len(r'(?<![\w/.:$])'):]
# "Review in 2 Weeks Dr Jones": a count and a unit, then a title, not a street.
_NOT_A_STREET_NAME = frozenset({
    'weeks', 'week', 'days', 'day', 'months', 'month', 'years', 'year', 'hours', 'hour',
    'hrs', 'mins', 'minutes', 'tablets', 'tablet', 'tabs', 'caps', 'capsules', 'times',
    'doses', 'units', 'mg', 'ml', 'mcg', 'am', 'pm', 'x',
})


def _street_name_renderings(street: str) -> list[str]:
    """'12 Mt Example Road Locality' -> 'Mt Example Road', 'Mount Example Rd', ...

    The street type is the *last* one on the line: 'St Marys Close' has 'St'
    inside the name. Returns [] when no street type is found.
    """
    tokens = (street or '').split()
    start = 0
    while start < len(tokens) and _STREET_NUMBER_TOKEN_RE.match(tokens[start].rstrip(',')):
        start += 1

    end = None
    for index in range(start, len(tokens)):
        token = tokens[index].lower().rstrip('.')
        if token in _ALL_STREET_TYPE_WORDS:
            end = index
    if end is None or end == start:
        return []

    words = [t.rstrip(',') for t in tokens[start:end]]
    street_type = tokens[end].lower().rstrip('.')
    full = next(f for f, forms in _STREET_TYPE_FORMS.items() if street_type == f or street_type in forms)
    type_forms = sorted({full.title()} | {a.title() + dot for a in _STREET_TYPE_FORMS[full] for dot in ('', '.')})

    word_forms = [[w] for w in words]
    for index, word in enumerate(words):
        if word.lower().rstrip('.') in ('mt', 'mount'):
            word_forms[index] = list(_MOUNT_FORMS)

    out: dict[str, str] = {}
    for combination in itertools.product(*word_forms, type_forms):
        rendering = ' '.join(combination)
        out.setdefault(rendering.lower(), rendering)
    for rendering in list(out.values()):                                     # ADR-063
        for form in _punctuation_forms(rendering):
            out.setdefault(form.lower(), form)
    return [s for s in out.values() if len(s) > 5]


# Area codes tried when a stored number has none. Local first; the rest cost
# nothing, since each produces one exact 10-digit string.
_AREA_CODES = ('07', '02', '03', '08')


def _national_forms(digits: str) -> list[str]:
    """Every 10-digit national number a stored value could be written as.

    Many patients have an 8-digit home number stored with no area code (about
    four in ten in one practice's sample), while documents write it as
    '07xxxxxxxx'. The 8-digit
    identifier cannot match inside that longer run — word boundaries prevent
    matching a fragment, which is correct — so the area-code forms have to be
    generated instead.
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
    """Spacings and prefixes an Australian number is written in.

    BP writes the eScript SMS destination into progress notes as
    '04xx xxx xxx', and radiology reports write a home number as
    '07xxxxxxxx' where the database holds eight digits.
    """
    out: list[str] = []

    # The stored form itself, spaced as an 8-digit local number often is
    if len(digits) == 8:
        out.append(f'{digits[:4]} {digits[4:]}')

    for national in _national_forms(digits):
        out.append(national)
        if national.startswith('04'):
            out.append(f'{national[:4]} {national[4:7]} {national[7:]}')
            out.append(f'{national[:4]}-{national[4:7]}-{national[7:]}')
            out.append(f'+61 {national[1:4]} {national[4:7]} {national[7:]}')
            out.append(f'+61{national[1:]}')
            # Half-spaced and dotted groupings. An audit found the patient's
            # own mobile written these ways, unredacted: only a generic
            # phone-shape pass had been seeing them.
            out.append(f'{national[:4]} {national[4:]}')
            out.append(f'{national[:4]}.{national[4:7]}.{national[7:]}')
        else:
            out.append(f'({national[:2]}) {national[2:6]} {national[6:]}')
            out.append(f'{national[:2]} {national[2:6]} {national[6:]}')
            out.append(f'({national[:2]}) {national[2:]}')
            out.append(f'+61 {national[1:2]} {national[2:6]} {national[6:]}')
            out.append(f'{national[:2]} {national[2:]}')
            out.append(f'{national[:2]}.{national[2:6]}.{national[6:]}')
    return out


# Block fields that hold analyte names, units and codes — never a person.
# Skipping them keeps "White Cell Count" intact for a patient named White.
NON_NAME_KEYS = frozenset({
    'resultname', 'mostrecentresultname', 'units', 'unit', 'range',
    'referencerange', 'loinccode', 'mostrecentloinccode', 'abnormalflag',
    'testname', 'itemcode', 'atccode',
})


# Titles that mark what follows as a person's name. Used for the catch-all
# pass: anyone the contacts book doesn't know — a soundalike misspelling, or a
# practitioner from another practice — still gets redacted.
_TITLED_NAME_RE = re.compile(
    r'\b(?:Dr|Dr\.|Doctor|Prof|Prof\.|Professor|A/Prof|Assoc\.? Prof\.?|'
    r'Mr|Mr\.|Mrs|Mrs\.|Ms|Ms\.|Miss|Sr|Sister)'
    # Up to three initials before the name — "Dr H. A. Smith", "Dr J Smith".
    # Without them the pattern needed a whole word after the title, and an
    # outside doctor signing with initials was not redacted at all.
    # Never across a line break (ADR-053): "Dr" closing one line and a heading
    # opening the next ("Dr\n\nFax Number", "Dr J Smith\n\nTests Completed")
    # were taken as one name. [^\S\r\n] still allows a non-breaking space.
    r'(?:[^\S\r\n]+[A-Z]\.?(?=[\s.]))?(?:[ \t]*\.?[ \t]*[A-Z]\.?(?=[\s.])){0,2}'
    r'[^\S\r\n]+[A-Z][a-zA-Z\'-]{2,}'
    # BP writes a preferred name in brackets after the first name — "Dr
    # Katherine (Kate) Smith". Stopping at the bracket left the surname.
    r'(?:[ \t]+\([A-Z][a-zA-Z\'-]+\)(?=[ \t]+[A-Z]))?'
    # Middle initials between the first name and the surname — "Dr Mary J.
    # Smith". Without them the match stopped at the first name and left the
    # initial and surname in plain view (ADR-065).
    r'(?:(?:[^\S\r\n]+[A-Z]\.?(?=[\s.])){0,2}[^\S\r\n]+[A-Z][a-zA-Z\'-]{2,})?'
    # A third name word, on the same line: "Dr. Mary Anne Smith" used to lose
    # only "Dr. Mary Anne", leaving the surname in plain view (found by a
    # local-model PII screening pass).
    r'(?:[ \t]+[A-Z][a-zA-Z\'-]{2,})?'
)

# 'Dr' is also the street type Drive. Never seen in this corpus, but guarded:
# a title preceded by a street number and name is an address.
# On one line only (ADR-071): a year ending the line above is not a house number.
# The number is not part of a date, a time or a decimal: "<date> Name Name Dr"
# is a titled name after a date, not "<number> Street Drive" (ADR-078).
_ADDRESS_BEFORE_RE = re.compile(r'(?<![\d/.:\-])\d{1,4}[A-Za-z]?[^\S\r\n]+(?:[A-Z][a-z]+[^\S\r\n]+){1,3}$')
# 'Dr' that can be Drive: not followed by an initial (ADR-064's rule, ADR-071).
_DRIVE_LIKE_TITLE_RE = re.compile(r'D[rR]\.?(?![^\S\r\n]+[A-Z]\.?(?=[\s.]))')

# Words that follow a title without naming anyone.
_NOT_A_PERSON = frozenset({
    'google', 'monologue', 'dialogue', 'patient', 'doctor', 'clinic',
    'practice', 'surgery', 'hospital', 'unknown', 'name', 'redacted',
})

# ── Contact details that belong to nobody we know ────────────────────────────
# The patient's own email and phone numbers are redacted from BPSDB, and the
# practice's from its locations. Everyone else's went through: an audit found
# email addresses in about a fifth of cloud-bound prompts, and phone numbers
# in over a tenth, that were neither — relatives, carers, other clinicians'
# rooms.
#
# A pattern, not a list: there is nothing to look these up against. Tokens are
# reversible per value, so de-redaction restores them where the output needs
# them ("ring the daughter on …") and two numbers stay distinguishable.

# The same shape the checker's pattern pass uses (checker/patterns.py).
_EMAIL_RE = re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b')

# An address without its ending (ADR-050): "ruby9@gmr" — a transcription that
# lost ".com", or a typed slip. Letters first on the left, a word that isn't
# an ordinary word on the right — unless it is a mail provider. Clinical
# shorthand keeps its '@': "1g@noon", "6.2@2hrs", "2@bed" all start with a
# number or end in an ordinary word, and spaced " @ " is never touched.
_BARE_AT_RE = re.compile(r"(?<![\w.+@-])([A-Za-z][\w.+-]{1,40})@([A-Za-z][\w-]{1,30})(?![\w@-]|\.\w)")
_MAIL_PROVIDERS = ('gmail', 'hotmail', 'outlook', 'yahoo', 'bigpond', 'icloud', 'live',
                   'optusnet', 'iinet', 'internode', 'westnet', 'ozemail', 'tpg', 'aol',
                   'msn', 'protonmail', 'proton')

# A spoken address in a transcript (ADR-050): "ruby nine at gmail dot com".
# Rare but real in dictated text. The whole phrase becomes one EM token. A
# pronoun or filler before "at" ("email them at gmail") is not an address
# name; spelled-out digits take in up to two words before them, so the name
# that starts the address goes too.
_SPOKEN_EMAIL_RE = re.compile(
    r"(?i)(?<![\w'’])((?:[a-z0-9][\w.]*\s+){0,2})([a-z0-9][\w.]*)\s+at\s+(" + '|'.join(_MAIL_PROVIDERS) + r")\b"
    r"((?:\s+dot\s+(?:com|net|org|au))*)")
_NOT_AN_ADDRESS_NAME = frozenset({
    'them', 'me', 'him', 'her', 'us', 'you', 'it', 'to', 'email', 'emailed', 'mail',
    'address', 'is', 'was', 'and', 'or', 'the', 'a', 'an', 'on', 'my', 'your',
    'their', 'his', 'its', 'it’s', "it's", 'that', 'this',
})
_NUMBER_WORDS = frozenset({
    'zero', 'oh', 'one', 'two', 'three', 'four', 'for', 'five', 'six', 'seven', 'eight',
    'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen',
    'seventeen', 'eighteen', 'nineteen', 'twenty', 'thirty', 'forty', 'fifty',
    'sixty', 'seventy', 'eighty', 'ninety', 'hundred', 'double', 'triple',
})


def _is_numberish(word: str) -> bool:
    return word.isdigit() or word.lower() in _NUMBER_WORDS


# Between digit groups: a hyphen, or up to three spaces or tabs — '0412  345  678'
# was typed with two (ADR-068). The bare 8-digit number below keeps one, so two
# 4-digit values in a results table are not joined into a number.
_GROUP_GAP = r'(?:[ \t]{1,3}|[ \t]*-[ \t]*)?'

# A phone label typed straight onto the number, with no space or colon: 'ph0412 345 678',
# 'mob0412345678' (MedUI ADR-131). Only these words may touch the number; any other
# letter or digit in front still means it is part of a code or reference. One fixed-width
# lookbehind per word, since Python's lookbehind takes no alternation of mixed widths.
_GLUED_LABELS = ('telephone', 'mobile', 'phone', 'cell', 'home', 'work', 'fax', 'mob', 'tel',
                 'ph', 'h', 'w', 'm', 'p', 'f', 't')
_AFTER_GLUED_LABEL = '|'.join(r'(?<=\b(?i:%s))' % w for w in _GLUED_LABELS)

# Mobiles: 04xx xxx xxx in any spacing, and +61 / 61 forms.
_MOBILE_RE = re.compile(r'(?:(?<![\w+])|' + _AFTER_GLUED_LABEL + r')(?:\+61' + _GROUP_GAP + r'|61' + _GROUP_GAP + r'|0)4\d{2}'
                        + _GROUP_GAP + r'\d{3}' + _GROUP_GAP + r'\d{3}(?!\d)')

# Landlines with an area code: 07 xxxx xxxx, (07) xxxx xxxx, +61 7 xxxx xxxx,
# and a whole number in brackets, (07 xxxx xxxx), which a first draft that
# refused an opening bracket before the area code let through.
_LANDLINE_RE = re.compile(
    r'(?:(?<![\w+])|' + _AFTER_GLUED_LABEL + r')(?:\(0[2-8]\)' + _GROUP_GAP + r'|0[2-8]' + _GROUP_GAP
    + r'|\+61' + _GROUP_GAP + r'\(?0?[2-8]\)?' + _GROUP_GAP + r')'
    r'\d{4}' + _GROUP_GAP + r'\d{4}(?!\d)')

# A bare local number, 8 digits with no area code. Common: in one practice's
# prompts these outnumbered the area-coded ones. Subscriber numbers
# never start with 0 or 1. The lookarounds refuse a fragment of something
# longer — a digit group before or after, a decimal, a date or a code.
_BARE_EIGHT_RE = re.compile(
    r'(?:(?<![\w/.+-])|' + _AFTER_GLUED_LABEL + r')(?<!\d[ -])([2-9]\d{3})([ -]?)(\d{4})(?![\w/:]|\.\d|[ -]\d|-\w)')

# The word just before a bare number, when it says what the number is.
_PHONE_LABEL_RE = re.compile(
    r'\b(?:ph|phone|tel|telephone|fax|mob|mobile|cell|home|work|h|w|m|p|f|t)\b\W*$',
    re.IGNORECASE)

# Medicare numbers that belong to nobody on file (ADR-034): a relative's, or a
# report pasted into the wrong chart. Card number = 8 digits, a check digit,
# then the card issue number; the IRN may follow. Only taken when the word
# "Medicare" sits just before it, or when it is printed in the card's 4-5-1
# grouping *and* its check digit is right — so a lab value or reference number
# of the same shape is left alone. The same two shapes the checker looks for.
# The card's expiry, when it directly follows the number (ADR-051): BP stores
# the field as card + IRN + "04/2027", and letters print "… 1 Exp 04/27".
# Same line only, month/year only, never the start of a full date ("12/04/2027").
_MEDICARE_EXPIRY = (r'(?:[ \t]{1,3}(?:(?i:exp(?:iry|ires)?|valid(?:[ \t]+(?:to|until))?)\.?:?[ \t]{0,3})?'
                    r'(?:0[1-9]|1[0-2])/(?:\d{4}|\d{2})(?![\d/]))?')
_MEDICARE_NUMBER = r'[2-6]\d{3}[ -]?\d{5}[ -]?\d(?:[ /-]?[1-9])?(?!\d)' + _MEDICARE_EXPIRY
_MEDICARE_LABELLED_RE = re.compile(
    r'(\bmedicare\b[^\d\n\[]{0,25})(?<!\d)(' + _MEDICARE_NUMBER + ')', re.IGNORECASE)
_MEDICARE_GROUPED_RE = re.compile(
    r'(?<![\d-])([2-6]\d{3} \d{5} \d(?:[ /-]?[1-9])?(?!\d)' + _MEDICARE_EXPIRY + ')')


# ── One word of a multi-word surname (MedUI ADR-154) ─────────────────────────
# Particles are never the distinctive word of a surname.
_SURNAME_PARTICLES = frozenset({
    'van', 'von', 'de', 'der', 'den', 'del', 'la', 'le', 'di', 'da', 'dos', 'du', 'st',
    'mac', 'mc', 'bin', 'binti', 'ap', 'ter', 'ten'})
_SURNAME_WORD_MIN = 5
# A person's name already taken: the patient's own, or a name nobody could attribute.
_PERSON_TOKEN_RE = re.compile(r'\[Redacted (?:Patient|Name): (?:PT|NM)-[0-9a-f]{12}\]')
_SURNAME_WORD_REACH = 3          # words between the surname word and the token, at most


def surname_words(surname: str) -> list[str]:
    """The distinctive words of a surname of two or more words, spaced or hyphenated.

    Five or more letters, not an ordinary word and not a particle: 'Quillfeather' and
    'Ashgrove' from 'Quillfeather-Ashgrove', 'Thistlewood' from 'van der Thistlewood',
    nothing from 'Small-Rivers'. A one-word surname has none: it is an identifier whole.
    """
    parts = [p for p in re.split(r'[\s\-]+', (surname or '').strip()) if p]
    if len(parts) < 2:
        return []
    from .hcp import _ordinary_words
    words = _ordinary_words()
    return [p for p in dict.fromkeys(parts)
            if p.isalpha() and len(p) >= _SURNAME_WORD_MIN
            and p.lower() not in words and p.lower() not in _SURNAME_PARTICLES]


def _beside_person_token(text: str, start: int, end: int) -> bool:
    """A person token within three words of text[start:end], on the same line."""
    line_start = text.rfind('\n', 0, start) + 1
    line_end = text.find('\n', end)
    line_end = len(text) if line_end < 0 else line_end
    before = _PERSON_TOKEN_RE.sub('\x00', text[line_start:start]).split()
    after = _PERSON_TOKEN_RE.sub('\x00', text[end:line_end]).split()
    return (any('\x00' in w for w in before[-_SURNAME_WORD_REACH:])
            or any('\x00' in w for w in after[:_SURNAME_WORD_REACH]))


# The patient's own name fields in a demographics row (ADR-051).
_OWN_NAME_FIELDS = frozenset({'FIRSTNAME', 'MIDDLENAME', 'SURNAME', 'PREFERREDNAME'})


def _medicare_check_ok(number: str) -> bool:
    """The 9th digit is the weighted sum of the first 8, mod 10."""
    digits = re.sub(r'\D', '', number)
    if len(digits) < 10:
        return False
    return sum(int(d) * w for d, w in zip(digits[:8], (1, 3, 7, 9, 1, 3, 7, 9))) % 10 == int(digits[8])


# Between the digits of a known number: up to three spaces, tabs, dots or
# hyphens, never a line break (ADR-068).
_DIGIT_GAP = r'[ \t.\-]{0,3}'
# Or a run of 4 to 12 spaces or tabs: column-aligned text spreads a number's groups across
# columns ('5566        7788', '07    5566    7788'; ADR-152). Only the person's own digits
# in order match, so a wide gap is still safe; never a line break.
_OWN_DIGIT_GAP = r'(?:[ \t.\-]{0,3}|[ \t]{4,12})'
# A regex identifier starting with this comment runs after the literal ones.
_LATE_PATTERN = '(?#late)'


def _own_phone_pattern(digits: str) -> re.Pattern | None:
    """A stored phone number in any spacing, with or without its prefix (ADR-068).

    The literal renderings cover the usual groupings; a number typed with two
    spaces between its groups ('5566  7788') matched none of them. Only this
    person's own digits match, so any spacing between them is safe.
    """
    forms = _national_forms(digits)
    if not forms:
        return None
    national = forms[0]
    if national.startswith('04'):
        prefix = r'(?:(?:\+?61)' + _OWN_DIGIT_GAP + r'|0)?'
        body = national[1:]                              # 4xx xxx xxx
    else:
        codes = sorted({f[1] for f in forms})            # area digit(s): one, or each tried
        area = '[%s]' % ''.join(codes)
        # Or the area digit without its zero ('75566 7788'; MedUI ADR-153), or the
        # zero without its area digit ('05566 7788'; MedUI ADR-154): the lookbehind
        # below still refuses a digit in front of either.
        prefix = (r'(?:(?:\+?61' + _DIGIT_GAP + r'\(?0?' + area + r'\)?'
                  r'|\(?0' + area + r'\)?|' + area + r'|0)' + _OWN_DIGIT_GAP + r')?')
        body = national[2:]                              # the 8-digit local number
    # Marked to run after the literal identifiers (see redact): BP's composite
    # strings — the NOK line, '(M) 04…' — must match whole first.
    # Any letters may sit straight in front (ADR-131: 'ph0412…', 'call0412…'): these are
    # this person's own digits, so only a digit or '+' before them means a longer number.
    return re.compile(_LATE_PATTERN + r'(?<![\d+])' + prefix + _OWN_DIGIT_GAP.join(body) + r'(?!\d)')


def _medicare_card_pattern(card: str) -> re.Pattern | None:
    """The patient's card with any issue number and any IRN, in any grouping.

    The 10th digit is the card *issue* number, which changes when a card is
    reissued. A report printed from an older or newer card carries a different
    one, so the exact number on file never matched it (ADR-034). The first
    nine digits identify the person on their own.
    """
    if len(card) != 10:
        return None
    # In any grouping, one space or hyphen at most between two digits ('2953 7015 61';
    # MedUI ADR-153), or cut short: its first 7 to 9 digits standing alone. Seven of
    # one person's own digits in order do not turn up by chance.
    return re.compile(_medicare_own_regex(card))


def _medicare_own_regex(card: str) -> str:
    """The patient's 10-digit card in any grouping, or its first 7 to 9 digits alone."""
    g = '[ -]?'
    full = g.join(card[:9]) + g + r'\d(?:[ /-]?[1-9])?(?!\d)' + _MEDICARE_EXPIRY
    short = g.join(card[:7]) + '(?:' + g + card[7] + '(?:' + g + card[8] + r')?)?(?![\d-])'
    return r'(?<![\d-])(?:' + full + '|' + short + ')'


# ── Medicare provider numbers (ADR-061) ──────────────────────────────────────
# A practitioner's provider number: a six-digit stem (often written without its
# leading zero), a practice-location character and a check letter. Taken only
# when the check letter is right, so a lab code or reference of the same shape
# is left alone. In one practice's prompts they appeared in about a quarter
# of them, unredacted; very few same-shape strings failed the check.
_PROVIDER_LOCATION = '0123456789ABCDEFGHJKLMNPQRTUVWXY'
_PROVIDER_RE = re.compile(r'(?<![\w/])(\d{5,6}[0-9A-HJ-NP-RT-Y][ABFHJKLTWXY])(?![\w/])')


def _provider_number_ok(number: str) -> bool:
    stem, location, check = number[:-2].zfill(6), number[-2], number[-1]
    total = sum(int(d) * w for d, w in zip(stem, (3, 5, 8, 4, 2, 1))) + _PROVIDER_LOCATION.index(location) * 6
    return 'YXWTLKJHFBA'[total % 11] == check


# ── The practice's own name (ADR-121) ────────────────────────────────────────
# BP's registered name ("Avonleigh and Brackenridge Medical Centres", invented)
# and the forms documents write it in: each site alone with the shared ending,
# "and" or "&", Centre or Centres, any capitals, a line break between words, a
# phone number run onto the end, a scan cut off at the end. Every form becomes
# [Redacted UsualPractice: UP-…], keyed on its clean form, one per site. A site
# word alone is a town and is never taken (ADR-020): every form keeps the ending.
_PRACTICE_NAME_TTL = 3600
_ORG_WORDS = frozenset("""
medical centre centres center centers clinic clinics surgery surgeries practice
practices family general health healthcare doctors doctor gp gps group medicine
care rooms
""".split())
_JOIN_WORDS = frozenset({'and', '&', '+'})
# Between two words: any whitespace, a line break included, or a literal \\n
# written into a data block (masked while the rules run, ADR-116).
_GAP = r'(?:\s|\\[' + ''.join(_ESCAPE_MARKS.values()) + r'])+'
_JOIN_RE = '(?:' + _GAP + 'and' + _GAP + '|(?:' + _GAP + ')?[&+](?:' + _GAP + ')?)'
_SINGULAR = {'centres': 'centre', 'centers': 'center', 'clinics': 'clinic',
             'surgeries': 'surgery', 'practices': 'practice'}
_CUT_MIN = 4   # letters a cut-off last word must keep ("Centr", "Cent")
# Scans misread "Centre" as these; only after the whole site name and "Medical" (ADR-127).
_OCR_ENDINGS = {'centre': ('centra', 'centro', 'centry'), 'center': ('centra', 'centro', 'centry')}
# Letters, not \w: a superscript digit ("Centre²") is \w but ends the word (ADR-127).
_LETTER = '[A-Za-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u024F]'
_NO_LETTER_AFTER = '(?!' + _LETTER + ')'
_NO_LOWER_AFTER = '(?!(?-i:[a-z\u00DF-\u00F6\u00F8-\u00FF]))'
# Before the name: no letter, or one stray letter a scan stuck to a capitalised town
# ("x<Town> Medical Centre"); the letter stays outside the token (ADR-127).
_NAME_START = ('(?:(?<!' + _LETTER + ')|(?<=(?<!' + _LETTER + ')[A-Za-z])(?=(?-i:[A-Z])))')
# Always-redact labels (as _token_label writes them) for a form of the practice's name.
_PRACTICE_NAME_LABELS = frozenset({'PracticeName', 'UsualPractice'})


def _org_word_re(word: str) -> str:
    """A word of the ending, in its singular, plural and spelling forms."""
    w = _SINGULAR.get(word.lower(), word.lower())
    if w in ('centre', 'center'):
        return r'cent(?:re|er)s?'
    if w == 'surgery':
        return r'surger(?:y|ies)'
    if w in ('clinic', 'practice'):
        return re.escape(w) + 's?'
    return re.escape(word)


def _ending_re(ending: list[str], cut: bool) -> str:
    """The ending ("Medical Centres"). Whole, it may run straight into a capital
    ("CentreAppointments", ADR-127). With cut, its last word may also be cut off by a
    scan, keeping at least _CUT_MIN letters ("Medical Centr"), or misread as one of
    _OCR_ENDINGS ("Centra"); those must end the word, so "Central" stays a word."""
    parts = [_org_word_re(w) for w in ending]
    last = '(?:' + parts[-1] + ')' + _NO_LOWER_AFTER
    if cut:
        stem = _SINGULAR.get(ending[-1].lower(), ending[-1].lower())
        cuts = [re.escape(stem[:n]) for n in range(len(stem) - 1, _CUT_MIN - 1, -1)]
        cuts += [re.escape(o) for o in _OCR_ENDINGS.get(stem, ())]
        if cuts:
            last = '(?:' + last + '|(?:' + '|'.join(cuts) + ')' + _NO_LETTER_AFTER + ')'
    return _GAP.join(parts[:-1] + [last])


def _site_re(site: list[str]) -> str:
    return _GAP.join(re.escape(w) for w in site)


def _form_re(sites: list[list[str]], ending: list[str]) -> str:
    """Sites joined by "and", "&" or "+", then the ending; any whitespace between
    words, a line break included. A cut-off last word only when at least two whole
    words come before it, so a cut never leaves a town and one word to stand for it."""
    whole_before = sum(len(s) for s in sites) + len(ending) - 1
    head = _JOIN_RE.join(_site_re(s) for s in sites)
    body = (head + _GAP if head else '') + _ending_re(ending, cut=whole_before >= 2)
    return _NAME_START + body


def _singular(word: str) -> str:
    """'Centres' → 'Centre', in the word's own capitals; other words unchanged."""
    single = _SINGULAR.get(word.lower())
    if not single:
        return word
    if word.isupper():
        return single.upper()
    return single[:1].upper() + single[1:] if word[:1].isupper() else single


def practice_name_forms(name: str, location: bool = False) -> list[tuple[str, str]]:
    """[(regex, clean form)] for one registered name: the joined name first, then
    each site with the shared ending (singular). Matched ignoring case.

    location: a site's LOCATIONNAME, used only when it ends in the ending's words,
    so a one-word site label (a town) adds nothing.
    """
    words = name.split()
    if len(words) < 2:
        return []
    n_end = 0
    while n_end < len(words) and words[len(words) - 1 - n_end].lower() in _ORG_WORDS:
        n_end += 1
    if not n_end:
        if location:
            return []
        return [(_NAME_START + _site_re(words) + _NO_LETTER_AFTER, ' '.join(words))]
    ending = words[len(words) - n_end:]
    head = words[:len(words) - n_end]
    if head and head[0].lower() == 'the':          # "the" stays outside the token
        head = head[1:]
    if not head:
        return [(_form_re([], ending), ' '.join(ending))]

    # The sites: the head split at "and", "&", "+" or a comma.
    sites: list[list[str]] = [[]]
    for w in head:
        if w.lower() in _JOIN_WORDS:
            sites.append([])
            continue
        sites[-1].append(w.rstrip(','))
        if w.endswith(','):
            sites.append([])
    sites = [s for s in sites if s and sum(len(w) for w in s) >= 3]
    if not sites:
        return []
    if len(sites) == 1:
        return [(_form_re(sites, ending), ' '.join(sites[0] + ending))]

    from itertools import permutations
    orders = permutations(sites) if len(sites) <= 3 else [tuple(sites)]
    forms = [(_form_re(list(order), ending), ' '.join(words)) for order in orders]
    single_end = ending[:-1] + [_singular(ending[-1])]
    forms += [(_form_re([s], ending), ' '.join(s + single_end)) for s in sites]
    return forms


# ── The always-redact list (ADR-030) ─────────────────────────────────────────
# A catch-all for recurring strings the record-based passes don't know: an
# external specialist, an organisation, a place. Admin-managed mapping table
# (`bps-redact lists import always_redact`). Practice-wide, so it is for strings
# that are never ambiguous — not a relative's first name, which would then be
# redacted from every patient's record.
_ALWAYS_TABLE = 'always_redact'
_ALWAYS_TTL = 60  # seconds — a grid edit applies within a minute
_ALWAYS_MIN_LENGTH = 3  # "Al" or "Jo" would tear through ordinary text
_SWITCHED_OFF = frozenset({'false', 'f', 'no', 'n', '0', 'off'})


def _token_label(raw: str) -> str:
    """A label _TOKEN_RE accepts — letters only, one word: 'place name' → 'PlaceName'."""
    words = re.findall(r'[A-Za-z]+', raw or '')
    return ''.join(w[0].upper() + w[1:] for w in words) or 'Name'


def _always_redact_entries() -> list[dict]:
    """Active entries, cached briefly: [{'string', 'label', 'fixed_text'}].

    If the table cannot be read, carry on with the normal redactions and log an
    error. This layer sits on top of redaction that already works, so a read
    failure returns to how things were before it existed.
    """
    cached = _cache('always').get(_ALWAYS_TABLE)
    if cached and (time.monotonic() - cached[1]) < _ALWAYS_TTL:
        return cached[0]

    try:
        rows = _env.env().lists.rows(_ALWAYS_TABLE)
    except Exception as exc:                      # list store boundary
        log.error('always-redact list unreadable: %s', exc)
        rows = []

    entries = []
    for row in rows:
        string = str(row.get('string') or '').strip()
        if not string or str(row.get('active')).strip().lower() in _SWITCHED_OFF:
            continue
        if len(string) < _ALWAYS_MIN_LENGTH:
            # The row id, not the string: a listed string may be a name.
            log.warning('always-redact: row %s skipped, shorter than %d characters',
                                       row.get('id'), _ALWAYS_MIN_LENGTH)
            continue
        entries.append({
            'string': string,
            'label': _token_label(str(row.get('label') or '')),
            'fixed_text': str(row.get('fixed_text') or '').strip() or None,
        })
    _cache('always')[_ALWAYS_TABLE] = (entries, time.monotonic())
    return entries

# ── Labelled identifiers (ADR-076) ───────────────────────────────────────────
# Numbers other services give the patient — a hospital UR, a radiology
# accession number, a lab's patient ID — found by the label written before
# them. Admin-managed mapping table (`bps-redact lists import identifier_labels`):
# `label`, optional `shape` (9 a digit, A a letter, X either, anything else as
# written), `active`, `note`. Labels, not regular expressions: the code supplies
# the matching around them. Only the value goes, as fixed text: the model never
# needs it, and the documents hold it (the maintainer and clinical lead, 2026-10-14).
_ID_LABEL_TABLE = 'identifier_labels'
ID_REMOVED = '[ID Removed]'
_ID_LABEL_MIN_LENGTH = 2
# After the label: "No", "No.", "Number", "#", then a colon or the like.
_ID_LABEL_SUFFIX = r'(?:[ \t]*(?:No\b\.?|Number\b|Num\b\.?|#))?[ \t]*[:#=]?[ \t]{0,3}'
# Without a shape: one run of letters, digits and hyphens holding at least four
# digits, so "UR: pending" or "URN 12" is left; never a date.
_ID_DEFAULT_VALUE = r'(?=(?:[A-Za-z-]*\d){4})[A-Za-z0-9](?:[A-Za-z0-9-]{2,24})[A-Za-z0-9]'
_ID_DATE_RE = re.compile(r'\d{1,4}[-.]\d{1,2}[-.]\d{1,4}')
_ID_YEAR_RE = re.compile(r'(?:19|20)\d\d')


def _shape_pattern(shape: str) -> str:
    """'AA-9999' -> '[A-Za-z]{2}-\\d{4}' (one character class per mask letter)."""
    classes = {'9': r'\d', 'A': '[A-Za-z]', 'X': '[A-Za-z0-9]'}
    return ''.join(classes.get(c, re.escape(c)) for c in shape)


def default_identifier_labels() -> list[dict]:
    """The built-in labels shipped with the package (data/identifier_labels.csv)."""
    import csv
    from importlib import resources
    with resources.files('bps_redaction').joinpath('data/identifier_labels.csv').open(encoding='utf-8') as f:
        return list(csv.DictReader(f))


def identifier_label_rows(practice_rows: list[dict]) -> list[dict]:
    """The built-in labels plus the practice's own (0.2.0). A practice row with the same
    label replaces the built-in one, so `active` false switches a default off."""
    merged = {str(r.get('label') or '').strip().rstrip(':').strip().lower(): r
              for r in default_identifier_labels()}
    for r in practice_rows:
        merged[str(r.get('label') or '').strip().rstrip(':').strip().lower()] = r
    return [r for key, r in merged.items() if key]


def _identifier_label_patterns() -> list[re.Pattern]:
    """Active entries as compiled patterns, cached briefly; group 'value' is the
    identifier. A read failure carries on without them, like the always-redact
    list (ADR-030)."""
    cached = _cache('always').get(_ID_LABEL_TABLE)
    if cached and (time.monotonic() - cached[1]) < _ALWAYS_TTL:
        return cached[0]

    try:
        practice_rows = _env.env().lists.rows(_ID_LABEL_TABLE)
    except Exception as exc:                      # list store boundary
        log.error('identifier-label list unreadable: %s', exc)
        practice_rows = []
    rows = identifier_label_rows(practice_rows)

    patterns = []
    for row in rows:
        label = str(row.get('label') or '').strip().rstrip(':').strip()
        if not label or str(row.get('active')).strip().lower() in _SWITCHED_OFF:
            continue
        if len(label) < _ID_LABEL_MIN_LENGTH:
            log.warning('identifier-label: row %s skipped, shorter than %d characters',
                                       row.get('id'), _ID_LABEL_MIN_LENGTH)
            continue
        shape = str(row.get('shape') or '').strip()
        value = _shape_pattern(shape) if shape else _ID_DEFAULT_VALUE
        label_re = re.escape(label).replace(r'\ ', r'[ \t]+')
        patterns.append(re.compile(
            r'(?<![\w])(?i:' + label_re + r')(?![A-Za-z])\.?' + _ID_LABEL_SUFFIX
            + r'(?<![\w-])(?P<value>' + value + r')(?![\w-])'))
    _cache('always')[_ID_LABEL_TABLE] = (patterns, time.monotonic())
    return patterns


# ── The patient's BP record number (ADR-031) ─────────────────────────────────
# Data-block rows carry INTERNALID, BP's key for the patient. It is an
# identifier — every value is the patient's own record number — and the model
# never needs it: it is a join key. redact() rewrites strings only, so as an
# integer it went through untouched, in almost every run that sent data
# blocks. Dropped from the rows; the string pattern is a backstop for
# any block rendered into the prompt some other way.
_RECORD_NUMBER_KEYS = frozenset({'internalid'})
_RECORD_NUMBER_IN_TEXT = re.compile(
    r"""(?:(['"])INTERNALID\1\s*:\s*['"]?\d+['"]?\s*,\s*"""       # a key followed by others
    r"""|,\s*(['"])INTERNALID\2\s*:\s*['"]?\d+['"]?(?=\s*\}))""",  # the last key in its row
    re.IGNORECASE)

# ── Recipients in BP's auto-generated letter lines (ADR-032) ─────────────────
# BP writes "Letter written to <recipient> re. <subject>." and
# "Letter to <recipient> printed." into the progress notes. The template fixes
# the slot, so whatever sits in it is a recipient: the patient by a shortened
# name no identifier list holds, a relative, or a specialist outside the
# contacts list. An audit found such names sent unredacted in dozens of
# runs a month. Because position proves it is a name, the word itself needs no
# nickname list — and carries none of the over-redaction risk of matching it
# everywhere. Organisations are left alone: they are not personal information.
_LETTER_LINE_RES = (
    re.compile(r'(Letter written to )([^\n\[\]]{1,60}?)( re\.)'),
    re.compile(r'(Letter to )([^\n\[\]]{1,60}?)( printed\.)'),
)
_RECIPIENT_PERSON_RE = re.compile(
    r"(?:(?:Dr|Doctor|Mr|Mrs|Ms|Miss|Prof|Professor|Sr|Sister)\.?\s+)?"
    r"(?:[A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+){0,3}"
    # "Surname, Initial" / "Surname, Firstname", a form seen in real letters
    r"|[A-Z][a-zA-Z'’-]+,\s*[A-Z](?:[a-zA-Z'’-]+|\.)?(?:\s*[A-Z]\.?){0,2})")
_RECIPIENT_ORGANISATION_RE = re.compile(
    r'hospital|podiatr|radiolog|imaging|patholog|clinic|centre|center|health|physio|'
    r'medical|practice|service|specialist|dental|optom|audiolog|dietit|psycholog|'
    r'pharmac|\blab|laborator|surgery|surgical|orthop|oncolog|dermatol|endocrin|'
    r'neurolog|urolog|gastro|respirat|renal|ophthalm|x-?ray|ultrasound|queensland|'
    r'\bqld\b|university|institute|foundation|council|centrelink|insurance|school|'
    r'\bndis\b|\bdva\b|agency|\bpty\b|\bltd\b|nursing|\bcare\b|group|\bunit\b|'
    r'department|\bdept\b|\bward\b|emergency|rehab|allied|diabet|cardiol|i-?med',
    re.IGNORECASE)
# Recipients that are roles or salutations, not people.
_NOT_A_RECIPIENT = frozenset({
    'patient', 'self', 'gp', 'doctor', 'parent', 'parents', 'family', 'carer',
    'whom', 'concern', 'sir', 'madam', 'colleague', 'colleagues', 'dear', 'to',
    'it', 'may', 'the',
})


def _is_person_recipient(slot: str) -> bool:
    slot = slot.strip()
    if not _RECIPIENT_PERSON_RE.fullmatch(slot) or _RECIPIENT_ORGANISATION_RE.search(slot):
        return False
    words = re.findall(r"[A-Za-z'’-]+", slot)
    return not all(w.lower() in _NOT_A_RECIPIENT for w in words)


# ── Links that open a record, and links into the practice network (ADR-043) ─
# Radiology reports carry image-viewer share links whose query string is the
# key (GUID, PIN, code): anyone holding one can open the patient's images.
# Links to a private-network address lead into the practice — one carried the
# patient's surname, first name and date of birth in a file name. Neither is
# any use to a model. Both become fixed text; ordinary public links (a health
# department page, a journal article) are left alone.
LINK_PLACEHOLDER = '[Link removed]'
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://[^\s'\"<>\]\)\\]+")
_PRIVATE_HOST_RE = re.compile(
    r'^(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|127\.|169\.254\.|localhost$)')
_ACCESS_KEYS = frozenset({
    'guid', 'pin', 'code', 'token', 'key', 'access_token', 'accesstoken',
    'accesskey', 'apikey', 'api_key', 'auth', 'sig', 'signature', 'password',
    'pwd', 'sessionid', 'sid', 'otp', 'accesscode', 'ticket',
})


def _strip_link(m: re.Match) -> str:
    url = m.group(0)
    trail = re.search(r'[.,;:!?]+$', url)
    if trail:                      # sentence punctuation is not part of the link
        url, tail = url[:trail.start()], trail.group(0)
    else:
        tail = ''
    from urllib.parse import urlsplit, parse_qsl
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        keys = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    except ValueError:             # malformed — treat as untrustworthy
        return LINK_PLACEHOLDER + tail
    internal = bool(_PRIVATE_HOST_RE.match(host)) or '.' not in host
    if internal or keys & _ACCESS_KEYS:
        return LINK_PLACEHOLDER + tail
    return m.group(0)


# ── Someone addressed by name in a transcript (ADR-046) ──────────────────────
# "[Doctor]: How are you going, Jo?" — the word after the comma is the person
# spoken to: measured on real transcripts, about three in four such words
# were known first names or nicknames, most of them the patient's shortened
# name, which no identifier
# list holds. Only inside a speaker's utterance; '?' anywhere in it, '.' only
# at its end. Roles and ordinary words ("Doc", "mate", "Judge") are left alone.
_SPEAKER_LABEL = r"\[[A-Za-z][A-Za-z0-9 _-]{1,24}\]:"
_UTTERANCE_RE = re.compile(_SPEAKER_LABEL + r"(?:(?!\\[n\ue000]|\n|" + _SPEAKER_LABEL + r").)*")
_VOCATIVE_RE = re.compile(r"(,\s+)([A-Z][a-z'’-]+)(\?|\.\s*$)")
_NOT_ADDRESSED = frozenset({
    'doc', 'doctor', 'dad', 'mum', 'mom', 'mate', 'love', 'darling', 'sweetheart',
    'buddy', 'champ', 'sir', 'madam', 'son', 'boss', 'team', 'guys', 'everyone',
    'okay', 'ok', 'yes', 'no', 'right', 'yeah', 'thanks', 'nurse', 'sister',
    'darl', 'dear', 'pet', 'hun', 'honey', 'kiddo', 'young', 'lady', 'man',
})


# ── Spelling variants of a name (ADR-042) ────────────────────────────────────
# BP stores a name one way; text writes it another. A hyphenated first name
# stored as 'Xxxx-Xxx' was transcribed as 'Xxxx Xxx' and went out unredacted.
# The checker searches the same forms (it takes them from this module's
# builder), so what it reports and what is redacted stay in step.
def _name_spellings(value: str) -> list[str]:
    """Spacing and punctuation variants: 'Xxxx Xxx', 'XxxxXxx', 'Van Dyke' -> 'VanDyke'."""
    text = re.sub(r'\s+', ' ', (value or '').strip())
    out = [text] if text != (value or '').strip() else []   # a double space in BP
    if ' ' in text:
        out.append(text.replace(' ', ''))
    if "'" in text or '-' in text:
        out.append(re.sub(r"['-]", '', text))
        out.append(re.sub(r'\s+', ' ', re.sub(r"['-]", ' ', text)).strip())
    # A hyphen written with a space beside it, 'Xxxx- Yyyy' for a stored 'Xxxx-Yyyy',
    # or the reverse (ADR-155). Both parts, in order: never one part alone (ADR-042).
    if '-' in text:
        out.extend(f for f in hyphen_spacings(text) if f != text)
    return out


def hyphen_spacings(text: str) -> list[str]:
    """'Xxxx-Yyyy' as 'Xxxx-Yyyy', 'Xxxx- Yyyy', 'Xxxx -Yyyy' and 'Xxxx - Yyyy' (ADR-155)."""
    return [re.sub(r'[ \t]*-[ \t]*', sp, text) for sp in ('-', '- ', ' -', ' - ')]


def _given_name_words(value: str) -> list[str]:
    """Each word of a multi-word given name: 'Mary Jane' -> 'Mary', 'Jane'.

    BP often holds first and middle names together in FirstName, so the name
    the patient is called by was never on the list on its own. Single letters
    are dropped; an ordinary word among them is matched only when capitalised,
    as for every identifier (ADR-025).
    """
    parts = [w for w in re.split(r"[\s-]+", (value or '').strip()) if w]
    if len(parts) < 2:
        return []
    return [w for w in parts if len(w) > 1]   # 'Mary A' still gives 'Mary'


# ── Email lines and greetings (ADR-041) ──────────────────────────────────────
# Staff record emails in the notes as "email to <name> re <subject>" and paste
# the email itself, which opens "Hi <name>,". The slot is a person as surely as
# a letter line's, and the name is often the patient's shortened one. Unlike
# BP's letter template this is
# typed text, so the slot ends at the first word that isn't capitalised; a
# second name word in a greeting is taken only when punctuation or a line
# break follows it ("Hi Jo Smith," but not the "Thanks" of "Hi Jo Thanks for").
_NAME_WORD = r"[A-Z][a-zA-Z'’-]+"
_TITLE = r'(?:(?:Dr|Mr|Mrs|Ms|Miss|Prof|Sr|Sister)\.?\s+)?'
_SLOT_END = r"(?=[\s,.;:!)]|\\[n\ue000]|$)"
_EMAIL_LINE_RES = (
    re.compile(r"(\b[Ee]-?mail(?:ed)? to )(%s%s(?: %s){0,2})()%s"
               % (_TITLE, _NAME_WORD, _NAME_WORD, _SLOT_END)),
    re.compile(r"((?:^|(?<=\n)|(?<=\\n)|(?<=\\\ue000)|(?<=['\"])|(?<=\]:)\s*)(?:Hi|Hello|Hey|Dear) )"
               r"(%s%s(?: %s(?=\s*[,.!:;\n]|\\[n\ue000]))?)()%s"
               % (_TITLE, _NAME_WORD, _NAME_WORD, _SLOT_END), re.MULTILINE),
)
# Greeted or emailed, but not a person: "Hi Doctor", "Dear Practice Manager".
_NOT_A_GREETEE = _NOT_A_RECIPIENT | frozenset({
    'dr', 'mr', 'mrs', 'ms', 'miss', 'prof', 'sr', 'sister', 'nurse', 'all',
    'team', 'everyone', 'there', 'guys', 'folks', 'manager', 'reception',
    'admin', 'again', 'yes', 'no', 'thanks', 'we', 'you', 'i', 'he', 'she',
    'they', 'both', 'everybody',
})


def _is_addressed_person(word: str) -> bool:
    """A word after ', ' that names the person spoken to (ADR-046).

    Not a role or endearment, and not an ordinary word — unless it is also a
    common first name among the practice's patients ("Jack", "Will"), which
    in this slot is far likelier to be a name.
    """
    w = word.lower().strip("'’-")
    if w in _NOT_ADDRESSED or w in _NOT_A_GREETEE or len(w) < 2:
        return False
    if w in _lowercase_words():
        from .hcp import HCPRedactionService
        return w in HCPRedactionService._common_first_names()
    return True


def _is_person_greetee(slot: str) -> bool:
    words = re.findall(r"[A-Za-z'’-]+", slot)
    return (_is_person_recipient(slot)
            and not all(w.lower() in _NOT_A_GREETEE for w in words))


# ── The Author slot of document rows (ADR-038) ──────────────────────────────
# A received letter's or result's Author is often an outside person the
# address book does not hold. The field says it is an author, so a
# person-shaped value can be tokenised with the same test as a letter
# recipient (ADR-032) — while organisations, document types and the role
# labels the calling application writes there are left alone.
_NOT_AN_AUTHOR = _NOT_A_RECIPIENT | frozenset({
    'report', 'reports', 'letter', 'letters', 'summary', 'discharge', 'result',
    'results', 'referral', 'correspondence', 'investigation', 'investigations',
    'scan', 'scans', 'test', 'tests', 'request', 'note', 'notes', 'form', 'plan',
    'consult', 'consultation', 'review', 'registrar', 'consultant', 'nurse',
    'practitioner', 'receptionist', 'staff', 'manager', 'previous', 'unknown',
    'author', 'admin', 'administrator', 'system', 'other', 'external', 'imported',
    'document', 'documents', 'attachment', 'email', 'fax', 'print', 'printed',
})
_AUTHOR_SPLIT_RE = re.compile(r'(\s+-\s+)')


# "J. Citizen", "Dr H. A. Citizen": initials before a surname.
_INITIALS_NAME_RE = re.compile(
    r"(?:(?:Dr|Doctor|Mr|Mrs|Ms|Miss|Prof|Professor|Sr|Sister)\.?\s+)?"
    r"(?:[A-Z]\.?\s*){1,3}\s[A-Z][a-zA-Z'’-]{1,}(?:-[A-Z][a-zA-Z'’-]+)?")


_lowercase_word_set: frozenset | None = None


def _lowercase_words() -> frozenset:
    """Words the system list holds in lower case: 'gold', 'coast', 'citizen'.

    Unlike the ordinary-word list used for surnames (ADR-025), it leaves out
    entries that exist only capitalised — first names such as 'Jane' — so a
    pair of common words ("Gold Coast") can be told from a name ("Jane
    Citizen").
    """
    global _lowercase_word_set
    if _lowercase_word_set is None:
        from pathlib import Path
        words: set[str] = set()
        for path in (word_list_path(),):
            candidate = Path(path)
            if candidate.exists():
                try:
                    words = {w.strip() for w in candidate.read_text(errors='ignore').splitlines()
                             if w.strip() and w.strip().islower() and "'" not in w}
                    break
                except OSError:
                    continue
        _lowercase_word_set = frozenset(words)
    return _lowercase_word_set


def _is_person_author(name: str) -> bool:
    """A person-shaped author: first name or initials and a surname.

    Stricter than a letter recipient, because an Author field also holds
    organisations: a single word, a short acronym ("QML") or a name made only
    of ordinary words ("Gold Coast") is left alone unless a title or initials
    say it is a person.
    """
    name = name.strip()
    words = re.findall(r"[A-Za-z'’-]+", name)
    if not words or all(w.lower() in _NOT_AN_AUTHOR for w in words):
        return False
    if _RECIPIENT_ORGANISATION_RE.search(name):
        return False
    if any(w.isupper() and len(w) <= 4 and len(w) > 1 for w in words):
        return False                                    # an acronym: an organisation
    if _INITIALS_NAME_RE.fullmatch(name):
        return True
    comma_form = ',' in name
    # A title, or "Surname, Initial", says it is a person.
    titled = comma_form or bool(re.match(
        r"(?:Dr|Doctor|Mr|Mrs|Ms|Miss|Prof|Professor|Sr|Sister)\.?\s", name))
    name_words = [w for w in words if w.lower() not in ('dr', 'doctor', 'mr', 'mrs', 'ms',
                                                        'miss', 'prof', 'professor', 'sr', 'sister')]
    if len(name_words) < 2 and not comma_form:
        return False                                    # one word: too ambiguous
    common = _lowercase_words()
    real = [w for w in name_words if len(w) > 1]
    if not titled and real and all(w.lower() in common for w in real):
        return False                                    # "Gold Coast", not a person
    return _is_person_recipient(name)


def _is_year_range(head: str, sep: str, tail: str) -> bool:
    """'2021-2022', '2021 2022' — the one clash that is common in real text."""
    return bool(sep) and 1900 <= int(head) <= 2030 and 1900 <= int(tail) <= 2030


def _is_date(digits: str) -> bool:
    """ddmmyyyy or yyyymmdd, 1900-2030. Left alone rather than tokenised: a
    reversible token restores a date in the output, but the model would have
    reasoned without it."""
    for day, month, year in ((digits[:2], digits[2:4], digits[4:]),
                             (digits[6:], digits[4:6], digits[:4])):
        try:
            if 1900 <= int(year) <= 2030:
                datetime(int(year), int(month), int(day))
                return True
        except ValueError:
            continue
    return False


def identifier_entries(d: dict) -> list[tuple[str, str, str]]:
    """(string, kind, form) for every string the patient pass redacts.

    The one builder: get_identifiers() takes the strings, and the checker
    takes the kinds and forms too, so it audits exactly what redaction looks
    for. Kinds are the checker's ('name', 'dob', 'phone', 'nok_name', ...).
    Duplicates are left in; callers deduplicate.
    """
    candidates: list[tuple[str, str, str]] = []

    def _add(*values, kind: str, form: str):
        for v in values:
            s = str(v).strip() if v else ''
            if s and len(s) > 1:
                candidates.append((s, kind, form))

    # Names
    first   = (d.get('FIRSTNAME') or '').strip()
    middle  = (d.get('MIDDLENAME') or '').strip()
    surname = (d.get('SURNAME') or '').strip()
    full    = (d.get('NAME') or '').strip()

    _add(first, kind='name', form='first')
    _add(middle, kind='name', form='middle')
    _add(surname, kind='name', form='surname')
    _add(full, kind='name', form='as_stored')
    if first and surname:
        _add(f'{first} {surname}', kind='name', form='first_surname')
    if first and middle and surname:
        _add(f'{first} {middle} {surname}', kind='name', form='first_middle_surname')
    # Spacing and punctuation variants, and each word of a multi-word
    # given name on its own (ADR-042). Surnames get the variants only:
    # 'Smith' alone from 'Smith-Jones' would catch every other Smith.
    for value in (first, middle, surname,
                  f'{first} {surname}' if first and surname else ''):
        _add(*_name_spellings(value), kind='name', form='spelling')
    _add(*_given_name_words(first), *_given_name_words(middle), kind='name', form='given_word')

    # Preferred name. Previously only ever present inside the composite
    # NAME string ('First "Preferred" Surname'), which never appears in a
    # consultation, so a patient called by their preferred name was not
    # redacted at all.
    preferred = (d.get('PREFERREDNAME') or '').strip()
    if preferred:
        _add(preferred, kind='name', form='preferred')
        if surname:
            _add(f'{preferred} {surname}', kind='name', form='preferred_surname')
        _add(*_name_spellings(preferred), kind='name', form='spelling')
        _add(*_given_name_words(preferred), kind='name', form='given_word')
        if surname:
            _add(*_name_spellings(f'{preferred} {surname}'), kind='name', form='spelling')

    # DOB variants
    dob_raw = d.get('DOB')
    if dob_raw:
        _add(str(dob_raw), kind='dob', form='raw')
        try:
            dob = datetime.strptime(str(dob_raw)[:10], '%Y-%m-%d')
            for fmt in (
                '%d/%m/%Y', '%-d/%-m/%Y', '%d-%m-%Y', '%d %b %Y', '%-d %B %Y',
                '%d-%b-%Y', '%-d-%b-%Y',
                # These renderings were found in text already sent to a
                # cloud provider.
                '%d/%m/%y', '%-d/%-m/%y', '%d.%m.%Y', '%-d.%-m.%Y', '%-d %b %Y',
                '%d %B %Y', '%Y/%m/%d',
                # Eight digits, as in an imaging file name (ADR-044).
                '%Y%m%d', '%d%m%Y',
            ):
                _add(dob.strftime(fmt), kind='dob', form=f'dob:{fmt}')
            # Day and month swapped, as a form filled in month-first writes it
            # (MedUI ADR-153). Only when the swap is a real date that is not the
            # DOB itself; numeric forms with the four-digit year only.
            if dob.day != dob.month and dob.day <= 12:
                swapped = dob.replace(day=dob.month, month=dob.day)
                for fmt in ('%d/%m/%Y', '%-d/%-m/%Y', '%d-%m-%Y', '%-d-%-m-%Y',
                            '%d.%m.%Y', '%-d.%-m.%Y'):
                    _add(swapped.strftime(fmt), kind='dob', form=f'dob_swapped:{fmt}')
        except (ValueError, TypeError):
            pass

    # Contact info. An empty phone field comes back as its bare display
    # label, '(H) ', which is not an identifier: added, it turned every
    # "(H)", "(M)" or "(W)" in that patient's text into a patient token
    # (ADR-062). A phone value counts only if it holds a digit.
    _add(d.get('ADDRESS'), kind='address', form='as_stored')
    for field in ('PHONE_HOME', 'PHONE_MOBILE', 'PHONE_WORK'):
        value = d.get(field)
        if not re.search(r'\d', value or ''):
            continue
        _add(value, kind='phone', form='as_stored')
    _add(d.get('EMAIL'), kind='email', form='as_stored')
    _add(d.get('NOK'), kind='nok_name', form='as_stored')

    # Street line without the suburb — addresses are often written that way —
    # plus abbreviation and unit-number renderings of it.
    address = (d.get('ADDRESS') or '').strip()
    street_line = (d.get('ADDRESS1_PART') or '').strip() or (
        address.split(',')[0].strip() if ',' in address else '')
    if (d.get('ADDRESS2_PART') or '').strip():
        street_line = '{} {}'.format(street_line, d['ADDRESS2_PART'].strip()).strip()
    if street_line:
        _add(*_street_renderings(street_line), kind='address', form='street_line')

    # Phone numbers. The stored strings carry a display prefix ('(H) ',
    # '(M) ', '(W) '), so the raw value above never matches free text; only
    # these derivations do.
    for field in ('PHONE_HOME', 'PHONE_MOBILE', 'PHONE_WORK'):
        raw = (d.get(field) or '')
        stripped = re.sub(r'\D', '', raw)
        if len(stripped) >= 8:
            _add(stripped, kind='phone', form='digits_only')
            _add(*_phone_renderings(stripped), kind='phone', form='spaced_or_prefixed')

    # Next of kin / emergency contact. The NOK field above is a composite
    # display string ('First Last phone1 phone2 Relationship') that never
    # appears verbatim, so these people were previously not redacted.
    for index in (1, 2):
        nok_first = (d.get(f'NOK{index}_FIRSTNAME') or '').strip()
        nok_surname = (d.get(f'NOK{index}_SURNAME') or '').strip()
        _add(nok_first, kind='nok_name', form='first')
        _add(nok_surname, kind='nok_name', form='surname')
        if nok_first and nok_surname:
            _add(f'{nok_first} {nok_surname}', kind='nok_name', form='full_name')
        # Spelling variants, as for the patient's own names (ADR-042).
        _add(*_name_spellings(nok_first), *_name_spellings(nok_surname),
             kind='nok_name', form='spelling')
        for phone_field in (f'NOK{index}_PHONE1', f'NOK{index}_PHONE2'):
            nok_digits = re.sub(r'\D', '', d.get(phone_field) or '')
            if len(nok_digits) >= 8:
                _add(nok_digits, kind='nok_phone', form='digits_only')
                _add(*_phone_renderings(nok_digits), kind='nok_phone', form='spaced_or_prefixed')

    # Government IDs
    for field in ('MEDICARENO', 'PENSIONNO', 'DVANO', 'IHI'):
        _add(d.get(field), kind=field.lower(), form='as_stored')

    # Medicare with IRN appended: imaging/pathology providers often print the
    # 10-digit card number concatenated with the IRN (1–9), giving 11 digits.
    # Add all variants so either form is caught.
    # MEDICARENO is card number + line number + expiry concatenated, so its
    # digit count is never 10 and this branch never used to run — meaning no
    # Medicare number was ever redacted. MEDICARENO_CARD is the bare card.
    medicare_digits = re.sub(r'\D', '', d.get('MEDICARENO_CARD') or d.get('MEDICARENO') or '')
    if len(medicare_digits) == 10:
        _add(medicare_digits, kind='medicareno', form='digits_only')  # in case stored with spaces
        _add(*(medicare_digits + irn for irn in '123456789'), kind='medicareno', form='with_irn')
        # Printed grouping, as it appears on the card and on referrals
        _add(f'{medicare_digits[:4]} {medicare_digits[4:9]} {medicare_digits[9:]}',
             kind='medicareno', form='grouped')

    return candidates


# The patient's own address parts on one line (0.2.16): see
# RedactionService.get_address_run_matcher.
_RUN_SEP = r"[ \t ,.;:/-]"
_RUN_MARKERS = r"(?i:unit|u|flat|apt|villa|lot|shop|suite|lvl|level)"
_RUN_STATES = r"(?:QLD|NSW|VIC|SA|WA|TAS|NT|ACT|Qld|Vic|Tas)"
_RUN_EDGE_L = r"(?<![A-Za-z0-9'’])"
_RUN_EDGE_R = r"(?![A-Za-z0-9])"


def _run_word(word: str, ordinary: frozenset) -> str:
    """One stored word: any case, or Title / capitals for an ordinary word;
    an apostrophe or hyphen inside it may be dropped or spaced."""
    pieces = [p for p in re.split(r"['’-]", word) if p]
    joiner = r"(?:['’-]|[ \t]?)"
    if word.lower().replace("'", '').replace('’', '') in ordinary:
        forms = [joiner.join(re.escape(p) for p in pieces),
                 joiner.join(re.escape(p.upper()) for p in pieces),
                 joiner.join(re.escape(p.title()) for p in pieces)]
        return '(?:' + '|'.join(dict.fromkeys(forms)) + ')'
    return '(?i:' + joiner.join(re.escape(p) for p in pieces) + ')'


def _address_run_matcher(d: dict):
    line1 = (d.get('ADDRESS1_PART') or '').strip()
    line2 = (d.get('ADDRESS2_PART') or '').strip()
    # A second line with no number and no street type is a locality ("Bunyip
    # Flat"), not part of the street: its words never count towards a run.
    line2_street = bool(re.search(r'\d', line2)) or any(
        w.lower().rstrip('.,') in _ALL_STREET_TYPE_WORDS for w in line2.split())
    street = ' '.join(x for x in (line1, line2) if x)
    if not street:
        return None
    locality_from = len([t for t in re.split(r"[\s,]+", line1) if t]) if line2 and not line2_street else None
    from .hcp import _ordinary_words
    ordinary = _ordinary_words()
    types = '|'.join(sorted((re.escape(t) for t in _ALL_STREET_TYPE_WORDS), key=len, reverse=True))
    type_re = r"(?i:(?:" + types + r"))\.?" + _RUN_EDGE_R
    parts, kinds = [], []
    tokens = [t for t in re.split(r"[\s,]+", street) if t]
    i = 0
    while i < len(tokens):
        t = tokens[i]
        low = t.lower().rstrip('.')
        if re.fullmatch(r"(?i:unit|u|flat|apt|villa|lot|shop|suite|lvl|level)", low) and i + 1 < len(tokens) \
                and re.fullmatch(r"\d+[A-Za-z]?", tokens[i + 1]):
            parts.append(_RUN_EDGE_L + r"(?:" + _RUN_MARKERS + r"\.?[ \t]*)?" + re.escape(tokens[i + 1])
                         + _RUN_EDGE_R)
            kinds.append('num')
            i += 2
            continue
        m = re.fullmatch(r"(\d+[A-Za-z]?)(?:[/-](\d+[A-Za-z]?))?", t)
        if m:
            for n in m.groups():
                if n:
                    parts.append(_RUN_EDGE_L + r"(?:" + _RUN_MARKERS + r"\.?[ \t]*)?" + re.escape(n) + _RUN_EDGE_R)
                    kinds.append('num')
            i += 1
            continue
        if low in _ALL_STREET_TYPE_WORDS:
            parts.append(_RUN_EDGE_L + type_re)
            kinds.append('type')
            i += 1
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z'’-]*", t):
            word = _run_word(t, ordinary)
            # In place of the word, between two that matched: its initial or a short
            # abbreviation ("A", "AA", "Aa"), or a word of about its length, as OCR
            # garbles it, that is none of the patient's other words.
            n = len(t)
            others = '|'.join(re.escape(o) for o in sorted(
                {x for x in tokens if x.isalpha() and x != t}
                | set(re.findall(r'[A-Za-z]+', d.get('CITY_PART') or ''))
                | {'QLD', 'NSW', 'VIC', 'SA', 'WA', 'TAS', 'NT', 'ACT'}, key=len, reverse=True)) or '(?!)'
            initial = (r"(?:[A-Za-z]{1,3}\.?(?:[A-Za-z]\.)*|(?!(?i:" + others + r")(?![A-Za-z]))"
                       r"[A-Za-z][A-Za-z'’-]{%d,%d})" % (max(n - 3, 1), n + 1))
            parts.append(_RUN_EDGE_L + r"(?:(?P<W>" + word + r")|(?P<I>" + initial + r"))" + _RUN_EDGE_R
                         + r"(?:" + _RUN_SEP + r"+" + type_re + r")?")
            kinds.append('loc' if locality_from is not None and i >= locality_from
                         else ('oword' if t.lower().replace("'", '').replace('’', '') in ordinary
                               else 'word') if len(t.strip("'’-")) >= 3 else 'short')
            i += 1
            continue
        i += 1
    if not parts:
        return None
    city = (d.get('CITY_PART') or '').strip()
    postcode = (d.get('POSTCODE_PART') or '').strip()
    # Between parts: separators, or none where the run starts at a later part
    # after a non-word character (a line start, a bracket). Never a line break or
    # an escape: neither is a separator, and "\\n" leaves a letter before the part.
    sep = r"(?:" + _RUN_SEP + r"+|(?<![A-Za-z0-9\\]))"
    body = ''
    for k, part in enumerate(parts):
        part = part.replace('(?P<W>', '(?P<w%d>' % k).replace('(?P<I>', '(?P<i%d>' % k)
        body += (r"(?:%s)?" if k == 0 else r"(?:" + sep + r"%s)?") % (r"(?P<p%d>" % k + part + ")")
    tail = ''
    if len(city) >= 3:
        tail += r"(?:" + sep + r"(?P<city>" + _RUN_EDGE_L + r"(?i:" + re.escape(city).replace(r'\ ', r'[ \t]+') + r")"
        tail += _RUN_EDGE_R + r"))?"
    tail += r"(?:" + sep + r"(?P<state>" + _RUN_EDGE_L + _RUN_STATES + r"\.?" + _RUN_EDGE_R + r"))?"
    if re.fullmatch(r"\d{4}", postcode):
        tail += r"(?:" + sep + r"(?P<pc>" + _RUN_EDGE_L + re.escape(postcode) + r"(?!\d)))?"
    return re.compile(body + tail), kinds


def _address_run_span(m: re.Match, kinds: list):
    """(start, end) of a qualifying run in m, or None."""
    found = []
    for k, kind in enumerate(kinds):
        if m.group('p%d' % k) is None:
            continue
        if kind in ('word', 'oword', 'short', 'loc') and m.groupdict().get('i%d' % k) is not None:
            found.append(('initial', m.start('p%d' % k), m.end('p%d' % k)))
        else:
            found.append((kind, m.start('p%d' % k), m.end('p%d' % k)))
    # An initial counts only between two words that matched; a type only after one.
    while found and found[0][0] in ('initial', 'type'):
        found.pop(0)
    while found and found[-1][0] == 'initial':
        found.pop()
    words = sum(1 for kind, _, _ in found if kind in ('word', 'oword'))
    plain = any(kind == 'word' for kind, _, _ in found)
    numbers = sum(1 for kind, _, _ in found if kind == 'num')
    typed = any(kind == 'type' for kind, _, _ in found) or any(
        m.group('p%d' % k) and re.search(r'[A-Za-z]' + _RUN_SEP + r'+[A-Za-z]', m.group('p%d' % k))
        for k, kind in enumerate(kinds) if kind in ('word', 'oword') and m.groupdict().get('w%d' % k))
    locality = any(m.groupdict().get(n) is not None and m.start(n) >= found[-1][2]
                   for n in ('city', 'pc')) if found else False
    marked = any(kind == 'num' and re.match(r'[A-Za-z]', m.group('p%d' % k) or '')
                 for k, kind in enumerate(kinds))
    # Two words; a number and a word; a word with a street type ("Tansy Ave"),
    # or with the patient's own suburb or postcode after it; or a unit marker and
    # number with them ("Unit 10, Thornbury").
    # Two dictionary words alone ("Gold Coast", "Town Centre") are a place, not
    # this patient's street: they need a number, a type or the locality too.
    if not ((words >= 2 and plain) or (numbers >= 1 and words >= 1) or (words >= 1 and (typed or locality))
            or (marked and locality)):
        return None
    end = found[-1][2]
    for name in ('city', 'state', 'pc'):
        if m.groupdict().get(name) is not None and m.start(name) >= end:
            end = m.end(name)
    return found[0][1], end


class RedactionService:

    @staticmethod
    def redact_titled_names(data, vault: TokenVault):
        """Catch-all: redact 'Dr Whoever' even when the name isn't known to us.

        Runs after the patient, HCP and practice passes, so anything already
        identified is a token by now — and a token cannot match, since it does
        not start with a capital letter followed by lower case.

        This is what catches a soundalike misspelling of a contact's surname,
        and practitioners from outside the practice who were never in the
        address book. Tokens are minted as Name, not HCP: we have not
        identified this person, only recognised that they are one.
        """
        from .hcp import _ordinary_words
        words = _ordinary_words()

        def _redact_str(text: str) -> str:
            def _replace(m: re.Match) -> str:
                matched = m.group(0)
                # Stop at the first word that names no one: "Dr Google" is not
                # a person, but in "Dr John Smith Hospital" only "Hospital" is
                # left out — the name before it is still redacted.
                spans = list(re.finditer(r'\S+', matched))
                end = None
                for word in spans[1:]:
                    if word.group(0).strip(".,'-").lower() in _NOT_A_PERSON:
                        break
                    end = word.end()
                if end is None:
                    return matched
                # A third name word only when it is not an ordinary word: in a
                # replay of real prompts it was a surname a little under half
                # the time, and otherwise "Referred", "Referral", "Discharge",
                # "Letter", a specialty or a place. A surname that is also an
                # ordinary word stays, as for
                # surnames in ADR-025.
                names = [w for w in spans[1:] if not w.group(0).startswith('(')
                         and not re.fullmatch(r'[A-Z]\.?', w.group(0))]
                # Written with initials, the first name word is the surname:
                # anything after it is taken only if it is not an ordinary
                # word ("Dr J. Smith Cardiology").
                # A middle initial after the first name word ("Dr Mary J. Smith")
                # means the next name word is the surname, ordinary word or not
                # (ADR-065).
                middle = (len(names) >= 2 and any(
                    names[0].end() <= w.start() < names[1].start()
                    and re.fullmatch(r'[A-Z]\.?', w.group(0))
                    and w.group(0) != 'I' for w in spans[1:]))    # not "Dr Smith I think"
                if not middle and len(names) < len(
                        [w for w in spans[1:] if not w.group(0).startswith('(')]):
                    for word in names[1:]:
                        if word.end() <= end and word.group(0).strip(".,'-").lower() in words:
                            end = names[names.index(word) - 1].end()
                            break
                if (len(names) == 3 and end == names[2].end()
                        and names[2].group(0).strip(".,'-").lower() in words):
                    end = names[1].end()
                # m.string, not text: _safe_sub matches each stretch between
                # tokens on its own, so m.start() counts from that stretch. Read
                # against the whole text, the window moved with every earlier
                # token and could turn a real name away (ADR-070).
                # Only a 'Dr' that can be Drive is checked: no other title is a
                # street type, and 'Dr Q. Name' is a title (ADR-071).
                if (_DRIVE_LIKE_TITLE_RE.match(matched)
                        and _ADDRESS_BEFORE_RE.search(m.string[:m.start()][-40:])):
                    return matched                      # '14 Example Dr Springfield'
                return vault.mint_name(matched[:end]) + matched[end:]

            return _safe_sub(_TITLED_NAME_RE, _replace, text)

        def _walk(obj, key=None):
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

    @staticmethod
    def redact_always_list(data, vault: TokenVault):
        """Final catch-all: strings on the admin always-redact list (ADR-030).

        Reuses redact(), so every guard the patient pass has applies here too:
        whole words only, an ordinary English word only when capitalised,
        clinical collocations and non-name keys left alone, longest first, and
        existing tokens never touched. One call for the whole list, so the
        longest string wins even across labels ("Dr Jane Example" before
        "Example"). Runs after the patient, HCP, practice and contact passes —
        anything they identified is a token by now — and before the titled-name
        pass: a curated list is more specific than that shape, so a listed
        person keeps the label given to them rather than a generic Name.

        Each string becomes a reversible [Redacted <label>: AR-…] token, or its
        row's fixed_text when one is given — which is not restored in the output.
        """
        entries = _always_redact_entries()
        if not entries:
            return data
        by_string = {e['string'].lower(): e for e in entries}

        def _mint(original: str) -> str:
            entry = by_string[original.lower()]
            if entry['fixed_text']:
                return entry['fixed_text']
            # A row for another form of the practice's own name gets its token (ADR-121).
            if entry['label'] in _PRACTICE_NAME_LABELS:
                return vault.mint_usual_practice(original)
            return vault.mint_listed(original, entry['label'])

        return RedactionService.redact(data, [e['string'] for e in entries], vault, mint=_mint)

    @staticmethod
    def drop_record_numbers(data):
        """Remove the patient's BP record number (INTERNALID) from data (ADR-031).

        Dict keys are dropped wherever they appear; in text, the rendered
        "'INTERNALID': 12345" pair is removed. Everything else is untouched.
        """
        if isinstance(data, str):
            return _RECORD_NUMBER_IN_TEXT.sub('', data)
        if isinstance(data, dict):
            return {k: RedactionService.drop_record_numbers(v) for k, v in data.items()
                    if str(k).strip().lower() not in _RECORD_NUMBER_KEYS}
        if isinstance(data, list):
            return [RedactionService.drop_record_numbers(item) for item in data]
        if isinstance(data, tuple):
            return tuple(RedactionService.drop_record_numbers(item) for item in data)
        return data

    @staticmethod
    def redact_links(data):
        """Replace share links and private-network links with fixed text (ADR-043).

        Runs first, before any identifier pass, so a link carrying a name or a
        date of birth goes whole rather than being left with a token inside it.
        Nothing to restore: the output never needs the link.
        """
        if isinstance(data, str):
            return _URL_RE.sub(_strip_link, data) if '://' in data else data
        if isinstance(data, dict):
            return {k: RedactionService.redact_links(v) for k, v in data.items()}
        if isinstance(data, list):
            return [RedactionService.redact_links(item) for item in data]
        if isinstance(data, tuple):
            return tuple(RedactionService.redact_links(item) for item in data)
        return data

    @staticmethod
    def redact_identifier_labels(data):
        """Replace a labelled identifier's value with fixed text (ADR-076).

        "UR: 123456" -> "UR: [ID Removed]". The label stays, so the model still
        reads what kind of number was there. Runs early, after the links: the
        phone and number-shape passes would otherwise take some of these values
        under the wrong label. Nothing to restore.
        """
        patterns = _identifier_label_patterns()
        if not patterns:
            return data

        def _replace(m: re.Match) -> str:
            value = m.group('value')
            if _ID_DATE_RE.fullmatch(value):
                return m.group(0)
            # "Previous episode 2019.": a year straight after the label, with no
            # "No", "#" or colon between, is a date, not an identifier.
            between = m.group(0)[:m.start('value') - m.start()]
            if _ID_YEAR_RE.fullmatch(value) and not re.search(r'[:#=]|\bN(?:o|um|umber)\b', between[-12:], re.I):
                return m.group(0)
            return m.group(0)[:m.start('value') - m.start()] + ID_REMOVED

        def _walk(obj):
            if isinstance(obj, str):
                for pattern in patterns:
                    obj = _safe_sub(pattern, _replace, obj)
                return obj
            if isinstance(obj, dict):
                return {k: _walk(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(item) for item in obj]
            if isinstance(obj, tuple):
                return tuple(_walk(item) for item in obj)
            return obj

        return _walk(data)

    @staticmethod
    def redact_clinician_first_names(data, names, vault: TokenVault):
        """The recording clinician's own first names, in any case, in a transcript (ADR-133).

        `names` comes from the host: the clinician who recorded the consultation
        (their first and preferred names). Without names, nothing changes. Title
        case, capitals and lower case ("thanks sam"); lower case is never used for a
        name that is an everyday word ("will", "mark") or has under three letters.
        A surname-like word next to the name goes with it, as in the first-name
        sweep. One NM token per person: "sam" shares the "Sam" token. Run after the
        patient and practitioner passes, so a patient's own name keeps its label
        and a full practitioner name stays one HCP token.
        """
        from .firstnames import clinician_pattern, token_span
        pattern = clinician_pattern(names)
        if pattern is None:
            return data

        def _replace(m: re.Match) -> str:
            start, end = token_span(m)
            text, base = m.group(0), m.start()
            span = text[start - base:end - base]
            # One token for the person whatever the case speech-to-text gave it.
            if ' ' not in span and (span.islower() or span.isupper()):
                span = '-'.join(p.capitalize() for p in span.lower().split('-'))
            return text[:start - base] + vault.mint_name(span) + text[end - base:]

        def _walk(obj, key=None):
            if isinstance(obj, str):
                if key and str(key).strip().lower() in NON_NAME_KEYS:
                    return obj
                return _safe_sub(pattern, _replace, obj)
            if isinstance(obj, dict):
                return {k: _walk(v, k) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(item, key) for item in obj]
            if isinstance(obj, tuple):
                return tuple(_walk(item, key) for item in obj)
            return obj

        return _walk(data)

    @staticmethod
    def redact_common_first_names(data, vault: TokenVault):
        """Last: the practice's common first names, with a surname beside them (ADR-058, 059).

        Carers and relatives named by first name — rarely the BP next of kin, so
        no record-based pass knows them. Title case or capitals; the capitalised
        word after the name on the same line goes with it unless it is plainly
        not a surname ("Mary Has"). An NM token per person, reversible.
        """
        from .firstnames import pattern as first_name_pattern, token_span
        pattern = first_name_pattern()
        if pattern is None:
            return data

        def _replace(m: re.Match) -> str:
            # "SURNAME, Mary" and "Mary Smith" go whole; a greeting, a
            # relationship or a verb beside the name stays (ADR-059).
            start, end = token_span(m)
            text, base = m.group(0), m.start()
            return (text[:start - base] + vault.mint_name(text[start - base:end - base])
                    + text[end - base:])

        def _walk(obj, key=None):
            if isinstance(obj, str):
                if key and str(key).strip().lower() in NON_NAME_KEYS:
                    return obj
                return _safe_sub(pattern, _replace, obj)
            if isinstance(obj, dict):
                return {k: _walk(v, k) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(item, key) for item in obj]
            if isinstance(obj, tuple):
                return tuple(_walk(item, key) for item in obj)
            return obj

        return _walk(data)

    @staticmethod
    def redact_surname_words(data, patient_id: int, vault: TokenVault):
        """One distinctive word of the patient's multi-word surname, beside a name (ADR-154).

        The whole surname is an identifier already. A document that misspells
        another word of it ('van den Thistlewood' for 'van der Thistlewood'), or
        gives half of a hyphenated one ('Marigold Ashgrove'), left the distinctive
        word in view beside the patient's redacted first name. ADR-042 refused to
        take a surname part anywhere: 'Jones' alone from 'Smith-Jones' would catch
        every other Jones. So only here: within three words of a patient or name
        token on the same line, written with a capital (Title case, capitals or as
        stored). Runs after the patient, titled-name and nickname passes, when those
        tokens exist. A PT token: it is the patient's.
        """
        if not patient_id:
            return data
        demographics = RedactionService._get_demographics(patient_id) or {}
        parts = surname_words(demographics.get('SURNAME') or '')
        if not parts:
            return data
        forms = sorted({f for p in parts for f in (p, p.capitalize(), p.upper())},
                       key=len, reverse=True)
        pattern = re.compile(r"(?<![^\W_])(?<![^\W_]['’])(?:" + '|'.join(re.escape(f) for f in forms)
                             + r")(?![^\W_])(?!['’](?!s\b)[^\W_])" + _COLLOCATION_GUARD)

        def _redact_str(text: str) -> str:
            # Over the whole string, not _safe_sub's stretches between tokens:
            # the test is what stands beside the word.
            if not _PERSON_TOKEN_RE.search(text):
                return text
            spans = [m.span() for m in _TOKEN_RE.finditer(text)]

            def _replace(m: re.Match) -> str:
                if any(a < m.end() and m.start() < b for a, b in spans) \
                        or not _beside_person_token(text, m.start(), m.end()):
                    return m.group(0)
                return vault.mint_patient(m.group(0))
            return pattern.sub(_replace, text)

        def _walk(obj, key=None):
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

    @staticmethod
    def redact_practice_nicknames(data, patient_id: int, vault: TokenVault):
        """The patient's shortened names (ADR-048), then a word of a multi-word surname (ADR-154).

        One call for both: each is a form of the patient's name that BP does not
        record, and each runs after the patient and titled-name passes. A caller
        that sequences the passes itself (MedUI) gets the second without a change.
        """
        data = RedactionService._redact_practice_nicknames(data, patient_id, vault)
        return RedactionService.redact_surname_words(data, patient_id, vault)

    @staticmethod
    def _redact_practice_nicknames(data, patient_id: int, vault: TokenVault):
        """The patient's likely shortened names, from the practice's own table (ADR-048).

        William -> Bill, Robert -> Bob: the forms most patients with this
        patient's first (or middle) name go by, learned from BP preferred names.
        Title case only for forms that are ordinary words — "bill", "rod" —
        any case for the rest (ADR-056); never after
        a hyphen ("X-Ray" is not a Raymond). An NM token, not PT: a relative may
        share the nickname. Runs after the patient pass, so the patient's own
        recorded names are tokens already.
        """
        if not patient_id:
            return data
        from .nicknames import forms_for
        demographics = RedactionService._get_demographics(patient_id) or {}
        recorded = {(demographics.get(k) or '').strip()
                    for k in ('FIRSTNAME', 'MIDDLENAME', 'SURNAME', 'PREFERREDNAME')}
        forms = forms_for(demographics.get('FIRSTNAME') or '', demographics.get('MIDDLENAME') or '',
                          recorded={w for r in recorded for w in re.split(r'[\s-]+', r) if w})
        if not forms:
            return data
        # A form that is an ordinary word ("bill", "rod") matches in Title case
        # only; any other ("tilly") in any case, like the patient's own names
        # (ADR-056).
        from .hcp import _ordinary_words
        words = _ordinary_words()
        ordered = sorted(forms, key=len, reverse=True)
        title_only = [re.escape(f.capitalize()) for f in ordered if f.lower() in words]
        any_case = [re.escape(f) for f in ordered if f.lower() not in words]
        alternatives = ([r'(?i:' + '|'.join(any_case) + ')'] if any_case else []) + title_only
        # A quote around the form, or a possessive 's after it, is a boundary
        # (ADR-117): 'Robbie', Robbie's. An apostrophe joining letters is not:
        # O'Rob…, "Don't".
        # A hyphen blocks the form only after a letter or digit ("X-Ray"); a dash
        # bullet ("-Mick" at a line start, " -Mick") is a boundary (ADR-148).
        # A surname beside the form goes with it, as in the first-name sweep
        # (ADR-148): "Mick Thornbury", "Thornbury, Mick". Before 0.2.14 the sweep
        # took these whole when the form was also a common first name; the nickname
        # pass runs first, so it must take them itself.
        pattern = re.compile(
            r"(?<!\w)(?<!\w-)(?<!\w['’])(?:(?P<pre>[A-Z][a-zA-Z'’-]+),[ \t]+)?"
            r"(?P<name>" + '|'.join(alternatives) + r")"
            r"(?![\w-])(?!['’](?!s\b)\w)" + _COLLOCATION_GUARD
            + r"(?:[ \t]+(?P<post>[A-Z][a-zA-Z'’-]+)(?![\w'’-]))?")
        # With an s straight after it, as for the patient's own names (ADR-141):
        # "Goldies' bloods", "Goldies bloods".
        suffixed = re.compile(
            r"(?<!\w)(?<!\w-)(?<!\w['’])(?P<n>" + '|'.join(alternatives) + r")"
            + _NAME_S_SUFFIX + r"(?![\w-])" + _NOT_A_STREET + _NOT_A_ROAD + _COLLOCATION_GUARD)

        def _suffixed(m: re.Match) -> str:
            if _plain_s_word(m.group('n'), m.group('s'), words) or _after_house_number(m):
                return m.group(0)
            return vault.mint_name(m.group('n')) + m.group('s')

        from .firstnames import token_span

        def _whole(m: re.Match) -> str:
            start, end = token_span(m)
            text, base = m.group(0), m.start()
            return (text[:start - base] + vault.mint_name(text[start - base:end - base])
                    + text[end - base:])

        def _redact_str(text: str) -> str:
            text = _safe_sub(pattern, _whole, text)
            return _safe_sub(suffixed, _suffixed, text)

        def _walk(obj, key=None):
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

    @staticmethod
    def redact_letter_recipients(data, vault: TokenVault):
        """Redact the recipient slot of BP's letter lines (ADR-032), and of email
        lines and greetings (ADR-041).

        A person in the slot becomes [Redacted Name: NM-…] — recognised as a
        person, not identified, as in ADR-026. Runs after the record-based
        passes, so a recipient they already identified is a token by now and
        the slot cannot match. Organisations and salutations are left alone.
        """
        def _replace(m: re.Match) -> str:
            lead, slot, tail = m.group(1), m.group(2), m.group(3)
            if not _is_person_recipient(slot):
                return m.group(0)
            return lead + vault.mint_name(slot.strip()) + tail

        def _replace_greetee(m: re.Match) -> str:
            lead, slot, tail = m.group(1), m.group(2), m.group(3)
            if not _is_person_greetee(slot):
                return m.group(0)
            return lead + vault.mint_name(slot.strip()) + tail

        def _redact_str(text: str) -> str:
            for pattern in _LETTER_LINE_RES:
                text = _safe_sub(pattern, _replace, text)
            # Email lines and greetings (ADR-041).
            for pattern in _EMAIL_LINE_RES:
                text = _safe_sub(pattern, _replace_greetee, text)
            # Someone addressed by name in a transcript (ADR-046).
            if ']:' in text:
                text = _UTTERANCE_RE.sub(
                    lambda u: _safe_sub(_VOCATIVE_RE, _replace_vocative, u.group(0)), text)
            return text

        def _replace_vocative(m: re.Match) -> str:
            word = m.group(2)
            if not _is_addressed_person(word):
                return m.group(0)
            return m.group(1) + vault.mint_name(word) + m.group(3)

        def _walk(obj, key=None):
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

    @staticmethod
    def redact_author_slots(data, vault: TokenVault):
        """Tokenise a person named in a document row's Author field (ADR-038).

        Only the name part is judged — "Jane Citizen - Discharge summary" keeps
        its document type. A value already holding a token, an organisation, a
        document type or one of the calling application's role labels is left alone.
        """
        def _redact_author(value: str) -> str:
            parts = _AUTHOR_SPLIT_RE.split(value, maxsplit=1)
            name = parts[0].strip()
            if not name or _TOKEN_RE.search(name) or not _is_person_author(name):
                return value
            lead = parts[0][:len(parts[0]) - len(parts[0].lstrip())]
            return lead + vault.mint_name(name) + ''.join(parts[1:])

        def _walk(obj, key=None):
            if isinstance(obj, str):
                if key and str(key).strip().lower() == 'author':
                    return _redact_author(obj)
                return obj
            if isinstance(obj, dict):
                return {k: _walk(v, k) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(item, key) for item in obj]
            if isinstance(obj, tuple):
                return tuple(_walk(item, key) for item in obj)
            return obj

        return _walk(data)

    @staticmethod
    def redact_unlinked_contacts(data, vault: TokenVault):
        """Redact email addresses and phone numbers that belong to nobody we know.

        Runs after the patient and practice passes, so the patient's own and the
        practice's details are already tokens (PT, PR) and keep that more
        specific attribution; what is left here is someone else's. Before the
        titled-name pass, which it does not interact with.

        Emails become EM, Medicare numbers MC (ADR-034), provider numbers PV
        (ADR-061), phone numbers PH. A bare 8-digit number becomes PH
        when a label says it is a phone number, and NB otherwise — some are
        record numbers, and the model should not be told they are phones. Year
        ranges and anything that reads as a date are left alone.
        """
        def _email(m: re.Match) -> str:
            return vault.mint_email(m.group(0))

        def _bare_at(m: re.Match) -> str:
            right = m.group(2).lower()
            if right not in _MAIL_PROVIDERS and right in _lowercase_words():
                return m.group(0)                  # "night@home" is not an address
            return vault.mint_email(m.group(0))

        def _spoken(m: re.Match) -> str:
            prefix, name, rest = m.group(1), m.group(2), m.group(0)[len(m.group(1)) + len(m.group(2)):]
            if name.lower() in _NOT_AN_ADDRESS_NAME:
                return m.group(0)                  # "email them at gmail": no address said
            if m.group(3).lower() == 'live' and not m.group(4).strip():
                return m.group(0)                  # "met at live music" — 'live' needs its "dot com"
            take = name
            if _is_numberish(name):                # "ruby nine at gmail": the name comes first
                words = prefix.split()
                kept = []
                for w in reversed(words):
                    if w.lower() in _NOT_AN_ADDRESS_NAME:
                        break
                    kept.insert(0, w)
                if kept:
                    take = ' '.join(kept) + ' ' + name
                    prefix = prefix[:re.search(r'(?:\S+\s+){%d}$' % len(kept), prefix).start()]
            return prefix + vault.mint_email(take + rest)

        def _medicare_labelled(m: re.Match) -> str:
            # No check-digit test: the label says what it is, and a mistyped
            # number is still someone's.
            return m.group(1) + vault.mint_medicare(m.group(2))

        def _medicare_grouped(m: re.Match) -> str:
            if not _medicare_check_ok(m.group(1)):
                return m.group(0)
            return vault.mint_medicare(m.group(1))

        def _phone(m: re.Match) -> str:
            return vault.mint_phone(m.group(0))

        def _bare(m: re.Match) -> str:
            head, sep, tail = m.group(1), m.group(2), m.group(3)
            if _is_year_range(head, sep, tail) or _is_date(head + tail):
                return m.group(0)
            before = m.string[max(0, m.start() - 24):m.start()]
            if _PHONE_LABEL_RE.search(before):
                return vault.mint_phone(m.group(0))
            return vault.mint_number(m.group(0))

        def _redact_str(text: str) -> str:
            # Most specific first; each pass skips the tokens the last one made.
            text = _safe_sub(_EMAIL_RE, _email, text)
            # An address without its ending, and one spoken aloud (ADR-050).
            text = _safe_sub(_BARE_AT_RE, _bare_at, text)
            text = _safe_sub(_SPOKEN_EMAIL_RE, _spoken, text)
            text = _safe_sub(_MEDICARE_LABELLED_RE, _medicare_labelled, text)
            text = _safe_sub(_MEDICARE_GROUPED_RE, _medicare_grouped, text)
            # Provider numbers with a valid check letter (ADR-061).
            text = _safe_sub(_PROVIDER_RE, lambda m: vault.mint_provider(m.group(1))
                             if _provider_number_ok(m.group(1)) else m.group(0), text)
            text = _safe_sub(_MOBILE_RE, _phone, text)
            text = _safe_sub(_LANDLINE_RE, _phone, text)
            return _safe_sub(_BARE_EIGHT_RE, _bare, text)

        def _walk(obj, key=None):
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

    @staticmethod
    def get_practice_identifiers() -> list[str]:
        """Address, phone, fax and email strings for every practice site.

        Not patient-specific, so fetched once and cached for the process.
        Multi-site practices have several rows and BP keeps historical ones;
        all are used, since an old address still appears in old correspondence.

        The practice's *name* is its own pass, with its own token that says what
        it is, so an instruction such as "you are assisting at ..." still reads
        (redact_practice_name, ADR-121).
        """
        cached = _env.state('practice_cache')
        if cached and (time.monotonic() - cached[1]) < _PRACTICE_TTL:
            return cached[0]

        if not _env.has_source():
            return []
        try:
            rows = _env.query('practice_locations')
        except Exception as exc:
            log.error('practice locations lookup failed: %s', exc)
            return []

        out: list[str] = []
        seen: set[str] = set()
        localities: set[tuple[str, str]] = set()

        def add(*values):
            for value in values:
                text = (value or '').strip()
                if len(text) > 4 and text.lower() not in seen:
                    seen.add(text.lower())
                    out.append(text)

        for row in rows:
            street = ' '.join(p for p in (row.get('ADDRESS1'), row.get('ADDRESS2')) if p).strip()
            city = (row.get('CITY') or '').strip()
            postcode = (row.get('POSTCODE') or '').strip()
            if len(city) >= 3 and re.fullmatch(r'\d{4}', postcode):
                localities.add((city.lower(), postcode))
            if street:
                add(*_street_renderings(street))
                if city:
                    add(f'{street}, {city} {postcode}'.strip(), f'{street} {city} {postcode}'.strip())
            for field in ('PHONE', 'AHPHONE', 'FAX'):
                digits = re.sub(r'\D', '', row.get(field) or '')
                if len(digits) >= 8:
                    add(digits, *_phone_renderings(digits))
            add(row.get('EMAIL'))

        # Bare suburb and postcode are never added: the practice suburb is also
        # where the patients live, and tokenising it would gut the text. The
        # pair is another matter (ADR-066): a letterhead puts it on its own line
        # under the street, and a local patient's postcode showed there.
        _env.set_state('practice_patterns', [
            re.compile(r'(?<![^\W_])' + re.escape(city).replace(r'\ ', r'[ \t]+')
                       + r'[ \t]*,?[ \t]*(?:' + _STATE_WORDS + r'\.?[ \t]*,?[ \t]*)?'
                       + postcode + r'(?!\d)', re.IGNORECASE)
            for city, postcode in sorted(localities)])
        _env.set_state('practice_cache', (out, time.monotonic()))
        log.debug('practice identifiers: %d strings from %d locations',
                                 len(out), len(rows))
        return out

    @staticmethod
    def get_practice_patterns() -> list:
        """Each practice site's suburb with its postcode, a state allowed between
        (ADR-066). Passed as patterns alongside get_practice_identifiers()."""
        RedactionService.get_practice_identifiers()
        return _env.state('practice_patterns') or []

    @staticmethod
    def get_practice_name_patterns() -> list[tuple[re.Pattern, str]]:
        """[(pattern, clean form)] for the practice's own name, every form (ADR-121).

        From BP's registration (PRACTICENAME, PracticeLegalName) and each site's
        LOCATIONNAME that carries the ending. Cached for the hour, like the
        practice's sites. If BP can't be read, the pass is skipped and an error
        logged, as for the sites.
        """
        cached = _env.state('practice_name_cache')
        if cached and (time.monotonic() - cached[1]) < _PRACTICE_NAME_TTL:
            return cached[0]
        if not _env.has_source():
            return []
        try:
            rows = _env.query('practice_name')
        except Exception as exc:
            log.error('practice name lookup failed: %s', exc)
            return []

        forms: dict[str, str] = {}
        for row in rows:
            name = ' '.join(str(row.get('NAME') or '').split())
            for regex, clean in practice_name_forms(name, location=row.get('SOURCE') == 'location'):
                forms.setdefault(regex, clean)
        # Longest clean form first, so the joined name goes whole before a site's.
        out = [(re.compile(regex, re.IGNORECASE), clean)
               for regex, clean in sorted(forms.items(), key=lambda f: len(f[1]), reverse=True)]
        _env.set_state('practice_name_cache', (out, time.monotonic()))
        # Counts only: the name itself is never logged.
        log.debug('practice name: %d forms from %d rows', len(out), len(rows))
        return out

    @staticmethod
    def redact_practice_name(data, vault: TokenVault):
        """The practice's own name becomes [Redacted UsualPractice: UP-…] (ADR-121).

        Runs before the patient and practitioner passes. The name holds the
        practice's towns, and a town that is also a patient's suburb or a
        contact's surname would otherwise be taken first and split the name.
        A town alone is never taken.
        """
        patterns = RedactionService.get_practice_name_patterns()
        if not patterns:
            return data

        def _walk(obj, key=None):
            if isinstance(obj, str):
                if key and str(key).strip().lower() in NON_NAME_KEYS:
                    return obj
                for pattern, clean in patterns:
                    obj = _safe_sub(pattern, lambda m, c=clean: vault.mint_usual_practice(c), obj)
                return obj
            if isinstance(obj, dict):
                return {k: _walk(v, k) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(item, key) for item in obj]
            if isinstance(obj, tuple):
                return tuple(_walk(item, key) for item in obj)
            return obj

        return _walk(data)

    @staticmethod
    def _get_demographics(patient_id: int) -> dict:
        """The demographics row, or {} on any failure.

        Cached per patient like the identifiers: the nickname, street-name,
        suburb and pattern passes each read it, and a caller redacting many
        documents for one patient used to pay a BP round trip per document.
        A failure is not cached, so the next call tries again (fail closed).
        """
        cached = _cache('demographics').get(patient_id)
        if cached and (time.monotonic() - cached[1]) < _IDENTIFIER_TTL:
            return cached[0]
        if not _env.has_source():
            log.warning('RedactionService: no identity source configured')
            return {}
        try:
            rows = _env.query('patient_get_demographics', [patient_id, patient_id, patient_id])
        except Exception as exc:
            log.error('RedactionService demographics error: %s', exc)
            return {}
        row = rows[0] if rows else {}
        if row:
            _cache('demographics')[patient_id] = (row, time.monotonic())
        return row

    @staticmethod
    def get_identifiers(patient_id: int) -> list[str]:
        """Return all PII strings to redact for this patient from BPSDB.

        Cached per patient_id for 5 minutes (patient demographics rarely change
        within a session). Returns empty list (without raising) when BPSDB is
        unavailable in tests.
        """
        cached = _cache('identifier').get(patient_id)
        if cached and (time.monotonic() - cached[1]) < _IDENTIFIER_TTL:
            return cached[0]

        if not _env.has_source():
            log.warning('RedactionService: no identity source configured; returning empty identifiers')
            return []

        row = RedactionService._get_demographics(patient_id)
        if not row:
            return []
        rows = [row]

        # Deduplicate preserving order; discard single characters and pure whitespace
        seen: set[str] = set()
        result: list[str] = []
        for s, _kind, _form in identifier_entries(rows[0]):
            key = s.lower()
            if key not in seen and len(s.strip()) > 1:
                seen.add(key)
                result.append(s)

        _cache('identifier')[patient_id] = (result, time.monotonic())
        return result

    @staticmethod
    def get_identifier_patterns(patient_id: int) -> list:
        """Regex identifiers, for what a literal string can't express.

        The patient's Medicare card with any issue number (ADR-034), and the
        patient's street line, followed within a few
        characters by their own suburb or postcode, is redacted as a single
        span. Without it, redacting the street line alone leaves the suburb and
        postcode dangling, which happened often in real prompts.

        Both ends must be this patient's own values, so the span cannot run
        away: it is anchored at both sides and capped in length.
        """
        cached = _cache('pattern').get(patient_id)
        if cached and (time.monotonic() - cached[1]) < _IDENTIFIER_TTL:
            return cached[0]

        demographics = RedactionService._get_demographics(patient_id)
        patterns = []
        if demographics:
            street = (demographics.get('ADDRESS1_PART') or '').strip() or (
                (demographics.get('ADDRESS') or '').split(',')[0].strip())
            if (demographics.get('ADDRESS2_PART') or '').strip():
                street = '{} {}'.format(street, demographics['ADDRESS2_PART'].strip()).strip()
            city = (demographics.get('CITY_PART') or '').strip()
            postcode = (demographics.get('POSTCODE_PART') or '').strip()
            starts = _street_renderings(street)
            if starts:
                alternation = '|'.join(re.escape(s) for s in
                                       sorted(starts, key=len, reverse=True))
                # Postcode first: it sits after the suburb, so a suburb-anchored
                # match would stop short and leave the postcode dangling. The
                # wider gap is what lets this one reach past the suburb.
                if len(postcode) >= 3:
                    patterns.append(re.compile(
                        r'(?<!\w)(?:{})[^\n]{{0,{}}}?{}(?!\w)'.format(
                            alternation, _ADDRESS_SPAN_GAP, re.escape(postcode)),
                        re.IGNORECASE))
                if len(city) >= 3:
                    patterns.append(re.compile(
                        r'(?<!\w)(?:{})[^\n]{{0,{}}}?{}(?!\w)'.format(
                            alternation, _ADDRESS_TAIL_GAP, re.escape(city)),
                        re.IGNORECASE))
                # A suburb that is not theirs after their street line — a former
                # address, or the road written with another town (ADR-064).
                patterns.append(re.compile(
                    r'(?<!\w)(?i:{})(?!\w){}'.format(alternation, _own_locality_tail(postcode))))   # ADR-081

            # Their own and their next of kin's phone numbers in any spacing (ADR-068).
            phone_fields = ('PHONE_HOME', 'PHONE_MOBILE', 'PHONE_WORK',
                            'NOK1_PHONE1', 'NOK1_PHONE2', 'NOK2_PHONE1', 'NOK2_PHONE2')
            for field in phone_fields:
                digits = re.sub(r'\D', '', demographics.get(field) or '')
                own_phone = _own_phone_pattern(digits) if len(digits) >= 8 else None
                if own_phone:
                    patterns.append(own_phone)

            # The patient's Medicare card, whatever its issue number (ADR-034).
            card = re.sub(r'\D', '', demographics.get('MEDICARENO_CARD')
                          or demographics.get('MEDICARENO') or '')
            medicare = _medicare_card_pattern(card)
            if medicare:
                patterns.append(medicare)

        _cache('pattern')[patient_id] = (patterns, time.monotonic())
        return patterns

    @staticmethod
    def get_street_name_matchers(patient_id: int) -> list:
        """Matchers for the patient's street name without its number (ADR-033).

        One-word names included ('Main Street'): losing a clinic or landmark
        on a same-named street was judged an acceptable price. A name
        whose every word is an ordinary word ('Long Way', 'Hill Close') matches
        only in Title Case or capitals, so "a long way to go" is left alone.
        The patient's own suburb or postcode, when it follows, goes with it.
        """
        cached = _cache('street_name').get(patient_id)
        if cached and (time.monotonic() - cached[1]) < _IDENTIFIER_TTL:
            return cached[0]

        demographics = RedactionService._get_demographics(patient_id)
        matchers = []
        if demographics:
            street = (demographics.get('ADDRESS1_PART') or '').strip() or (
                (demographics.get('ADDRESS') or '').split(',')[0].strip())
            if (demographics.get('ADDRESS2_PART') or '').strip():
                street = '{} {}'.format(street, demographics['ADDRESS2_PART'].strip()).strip()
            city = (demographics.get('CITY_PART') or '').strip()
            postcode = (demographics.get('POSTCODE_PART') or '').strip()

            renderings = _street_name_renderings(street)
            if renderings:
                tails = []
                if len(postcode) >= 3:
                    tails.append(r'[^\n]{{0,{}}}?{}(?!\w)'.format(_ADDRESS_SPAN_GAP, re.escape(postcode)))
                if len(city) >= 3:
                    tails.append(r'[^\n]{{0,{}}}?{}(?!\w)'.format(_ADDRESS_TAIL_GAP, re.escape(city)))
                # Their own suburb first, in any case; otherwise someone else's,
                # whose capitals carry the meaning (ADR-064).
                own = '(?i:{})|'.format('|'.join(tails)) if tails else ''
                tail = '(?:{}(?-i:{}))?'.format(own, _own_locality_tail(postcode))   # ADR-081

                from .hcp import _ordinary_words
                words = _ordinary_words()
                longest = max(renderings, key=len)     # full type, 'Mount'
                ordinary = all(w.lower().rstrip('.') in words for w in longest.split())
                if ordinary:
                    forms = set()
                    for r in renderings:
                        forms.update({r, r.upper()})
                    flags = 0
                else:
                    forms, flags = set(renderings), re.IGNORECASE
                # Any run of whitespace between words: a line break or a double
                # space inside the name was the last form a replay still found.
                alternation = '|'.join(re.escape(s).replace(r'\ ', r'\s+')
                                       for s in sorted(forms, key=len, reverse=True))
                # A number in front goes too: the patient's own full address is a
                # token by now, so this one is a former or mistyped one (ADR-064).
                # Their suburb may come first: '<Suburb> <number> <Road> Rd' (ADR-074).
                lead = _STREET_NUMBER_LEAD
                if len(city) >= 3:
                    lead = r'(?:(?<!\w)(?i:{})[ \t,]+(?=\d))?{}'.format(
                        re.escape(city).replace(r'\ ', r'[ \t]+'), _STREET_NUMBER_LEAD)
                matchers.append(re.compile(
                    r'(?:{})?(?<!\w)(?:{})(?!\w)(?:{})'.format(
                        lead, alternation, tail), flags))

        _cache('street_name')[patient_id] = (matchers, time.monotonic())
        return matchers

    @staticmethod
    def get_address_run_matcher(patient_id: int):
        """The patient's own address parts on one line, in BP's order (0.2.16).

        Every street-line rendering above is the whole stored line, or its street
        name with its type. A document that writes part of it, as OCR and letters
        do, matched none of them: "Unit 10,<tab>Word Word.<tab>4999" with the
        third word and the second line left out, or "UNIT 10 WORD W WORD ST,
        SUBURB 4999" with a word as its initial and a type BP doesn't hold.

        Here each part of ADDRESS1 and ADDRESS2 (the unit and number, each word,
        the street type) is optional, in order, then the suburb, state and
        postcode. Between parts: spaces, tabs, commas, full stops, a slash or a
        hyphen, never a line break or an escape. A word matches in any case
        (Title or capitals when it is an ordinary word), or as its initial
        between two words that matched; any street type may follow a word.
        The run is taken from its first part to its last only when it holds the
        unit or house number with a word, or two words, of 3 letters or more;
        a suburb or postcode alone never qualifies, so a practice in the same
        suburb is left to the practice rules. Returns (pattern, kinds) or None.
        """
        cached = _cache('address_run').get(patient_id)
        if cached and (time.monotonic() - cached[1]) < _IDENTIFIER_TTL:
            return cached[0]
        result = None
        demographics = RedactionService._get_demographics(patient_id)
        if demographics:
            result = _address_run_matcher(demographics)
        _cache('address_run')[patient_id] = (result, time.monotonic())
        return result

    @staticmethod
    def redact_other_dobs(data):
        """Replace a labelled date of birth that isn't the patient's with fixed text (ADR-047).

        Runs after the patient pass, so the patient's own date of birth is
        already a token and never reaches here.
        """
        def _redact_str(text: str) -> str:
            return _safe_sub(_OTHER_DOB_RE, lambda m: m.group(1) + DOB_PLACEHOLDER, text)

        def _walk(obj, key=None):
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

    @staticmethod
    def redact_other_addresses(data):
        """Replace any address still left with fixed text (ADR-035).

        Runs after the patient, practice and street-name passes, so the
        patient's current address and the practice's are already tokens that
        de-redaction can restore; what is left belongs to someone else, or is
        the patient's former address.
        """
        def _street(m: re.Match) -> str:
            if any(w.strip(",.'").lower() in _NOT_A_STREET_NAME for w in m.group(1).split()):
                return m.group(0)
            return STREET_NAME_PLACEHOLDER

        def _redact_str(text: str) -> str:
            text = _safe_sub(_OTHER_STREET_RE, _street, text)
            return _safe_sub(_OTHER_POBOX_RE, STREET_NAME_PLACEHOLDER, text)

        def _walk(obj, key=None):
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

    @staticmethod
    def get_suburb_postcode_matchers(patient_id: int) -> list:
        """The patient's own suburb with their postcode, and their postcode where
        a label or a state says it is one (ADR-060).

        "Location: <Suburb>, 4xxx" in a summary has no street line, so the
        address span rule (ADR-020) never starts. The pair is far more specific
        than either part. The suburb alone is left: thousands of patients share
        it, and it is sometimes useful context. A bare postcode is left too,
        unless labelled ("Postcode 4xxx", "P/C 4xxx") or after a state
        ("QLD 4xxx"): other four-digit numbers are everywhere.
        """
        cached = _cache('suburb_postcode').get(patient_id)
        if cached and (time.monotonic() - cached[1]) < _IDENTIFIER_TTL:
            return cached[0]
        demographics = RedactionService._get_demographics(patient_id)
        matchers = []
        city = (demographics.get('CITY_PART') or '').strip() if demographics else ''
        postcode = (demographics.get('POSTCODE_PART') or '').strip() if demographics else ''
        # A rural address keeps its locality on the second line and the postal
        # town that shares the postcode in CITY; a letter may write either
        # (ADR-075). Only a place name: no digits, so "Unit 3" or "PO Box 12"
        # on that line is not taken for one.
        localities = [city]
        line2 = (demographics.get('ADDRESS2_PART') or '').strip() if demographics else ''
        if re.fullmatch(r"[A-Za-z][A-Za-z '’.-]+", line2) and line2.lower() != city.lower():
            localities.append(line2)
        if re.fullmatch(r'\d{4}', postcode):
            pc = re.escape(postcode) + r'(?!\d)'
            for locality in localities:
                if len(locality) < 3:
                    continue
                suburb = re.escape(locality).replace(r'\ ', r'\s+')
                # Same line, at most a comma, a state and spaces between — or
                # a neighbouring town that shares the postcode, often the
                # practice's own ("Hillview, Riverton QLD 4xxx"; ADR-067).
                # A house number in front goes too: a rural street can start
                # with the locality's own name, and the pair then matched from
                # just after the number, leaving it in view (ADR-072). So does
                # a street between them, in any case: a scanned letter's street
                # can come out garbled ("Old lvlill Road") and match neither
                # the patient's street nor the shape of anyone else's (ADR-075).
                matchers.append((re.compile(
                    r'(?:' + _PAIR_LEAD + r'(?:' + _PAIR_STREET + r')?)?(?<!\w)' + suburb
                    + r'[^\n\d\[\]]{0,%d}?(?<!\d)' % _ADDRESS_SPAN_GAP + pc,
                    re.IGNORECASE), None))
                # The end of a suburb of several words, then the postcode: the
                # patient pass's address token can take the rest (ADR-074).
                words = locality.split()
                tails = [' '.join(words[i:]) for i in range(1, len(words)) if len(words[-1]) >= 3]
                if tails:
                    tail = '|'.join(re.escape(s).replace(r'\ ', r'\s+') for s in tails)
                    matchers.append((re.compile(
                        r'(?<!\w)(?:' + tail + r')[ \t,]+(?:' + _STATE_WORDS + r'\.?[ \t,]+)?' + pc,
                        re.IGNORECASE), None))
            matchers.append((re.compile(
                r'(?i:(\b(?:post\s*code|p/?c|qld|nsw|vic|sa|wa|tas|nt|act)\b\.?:?\s{0,3}))(?<!\d)' + pc), 1))
        _cache('suburb_postcode')[patient_id] = (matchers, time.monotonic())
        return matchers

    @staticmethod
    def redact_practice_localities(data, vault: TokenVault):
        """The practice's suburb with its postcode, where no patient's street-name
        pass runs (a prompt with no patient; ADR-066, ADR-067)."""
        patterns = RedactionService.get_practice_patterns()
        if not patterns:
            return data

        def _walk(obj, key=None):
            if isinstance(obj, str):
                if key and str(key).strip().lower() in NON_NAME_KEYS:
                    return obj
                for pattern in patterns:
                    obj = _safe_sub(pattern, lambda m: vault.mint_practice(m.group(0)), obj)
                return obj
            if isinstance(obj, dict):
                return {k: _walk(v, k) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(item, key) for item in obj]
            if isinstance(obj, tuple):
                return tuple(_walk(item, key) for item in obj)
            return obj

        return _walk(data)

    @staticmethod
    def redact_street_names(data, patient_id: int, vault: TokenVault | None = None):
        """Replace the patient's number-less street name with fixed text (ADR-033),
        and their suburb with postcode, or a labelled postcode (ADR-060).

        Runs after the patient and practice passes, so a full address (number
        included) is already a reversible token, and a practice on the same
        street keeps its PR token; only what they left is caught here.

        With a vault, the practice's suburb with its postcode becomes a PR token
        here (ADR-066) — after the patient's own suburb pair, so a patient who
        shares the practice's postcode keeps their suburb hidden ("<suburb>,
        <practice suburb> 4xxx"), and before a labelled postcode, so a letterhead's
        "SUBURB QLD 4xxx" goes whole (ADR-067).
        """
        matchers = RedactionService.get_street_name_matchers(patient_id)
        pairs = RedactionService.get_suburb_postcode_matchers(patient_id)
        practice = RedactionService.get_practice_patterns() if vault is not None else []
        run = RedactionService.get_address_run_matcher(patient_id)
        if not matchers and not pairs and not practice and run is None:
            return data

        def _pair(text: str, pattern, keep) -> str:
            # keep: the label or state stays, only the postcode goes.
            return _safe_sub(pattern, (lambda m: m.group(keep) + STREET_NAME_PLACEHOLDER) if keep
                             else STREET_NAME_PLACEHOLDER, text)

        def _run(m: re.Match) -> str:
            span = _address_run_span(m, run[1])
            if span is None:
                return m.group(0)
            s0, e0 = span[0] - m.start(), span[1] - m.start()
            return m.group(0)[:s0] + STREET_NAME_PLACEHOLDER + m.group(0)[e0:]

        def _redact_str(text: str) -> str:
            for matcher in matchers:
                text = _safe_sub(matcher, STREET_NAME_PLACEHOLDER, text)
            for pattern, keep in pairs:
                if not keep:
                    text = _pair(text, pattern, keep)
            # 0.2.16: what the whole-line and street-name forms left, after them,
            # so a suburb they anchor on is still there for them.
            if run is not None:
                text = _safe_sub(run[0], _run, text)
            for pattern in practice:
                text = _safe_sub(pattern, lambda m: vault.mint_practice(m.group(0)), text)
            for pattern, keep in pairs:
                if keep:
                    text = _pair(text, pattern, keep)
            return text

        def _walk(obj, key=None):
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

    @staticmethod
    def redact(data, identifiers: list[str], vault: TokenVault, patterns: list | None = None,
               mint=None):
        """Recursively replace all identifier strings in data with vault tokens.

        Processes identifiers longest-first to prevent partial-match shadowing.
        Operates on strings, lists, dicts, and tuples; returns same shape.
        """
        if not identifiers:
            return data

        # Sort longest-first; discard blanks
        sorted_ids = sorted(
            (s for s in identifiers if s and s.strip()),
            key=len, reverse=True,
        )

        # Pre-mint tokens and build lookup; compile one combined pattern.
        token_lookup: dict[str, str] = {
            ident.lower(): (mint or vault.mint_patient)(ident) for ident in sorted_ids
        }
        # Boundary guard. Patient identifiers were once matched as
        # bare substrings, so a short name redacted inside ordinary words —
        # "Ann" in "announce", "Rob" in "robust", "Mark" in "market". That
        # silently corrupts the clinical text the model reads, and the risk grew
        # with preferred names and next-of-kin first names being added here.
        # Lookarounds rather than \b, because identifiers may begin or end with
        # punctuation ("+61 491 570 156", "(07) 5550 1234").
        # A one-word identifier that is also an ordinary English word — a
        # patient surnamed Small, Rose or Day — is matched only when
        # capitalised. Clinical text uses these words in lower case ("small
        # bowel", "rose to 140"), and tokenising those corrupts the record.
        # Sentence-initial use is still caught, which errs toward redaction.
        from .hcp import _ordinary_words
        words = _ordinary_words()
        plain, capitalised = [], []
        for s in sorted_ids:
            if s.isalpha() and s.lower() in words:
                capitalised.append(s)
            else:
                plain.append(s)

        # An underscore separates words here (ADR-044): a file name such as
        # SURNAME_FIRST_US_<DOB>.pdf carried the patient's whole identity, and
        # \w counts '_' as part of a word, so none of it matched.
        # An ordinary-word name matches in Title case or in capitals (ADR-045):
        # "RE: MARY ROSE" in a letter header is a name, as surely as "Rose".
        before, after = r'(?<![^\W_])', r'(?![^\W_])'
        matchers = []
        if plain:
            matchers.append(re.compile(
                before + '(?:' + '|'.join(re.escape(s) for s in plain) + ')' + after
                + _COLLOCATION_GUARD, re.IGNORECASE))
        # A name with capitals inside it matches as stored too (ADR-055):
        # capitalize() lowercases the rest, so a surname that is a dictionary
        # word written "McDonald" or "VanDyke" was searched for only as
        # "Mcdonald" and "MCDONALD", and on its own went through.
        if capitalised:
            forms = [f for s in capitalised
                     for f in dict.fromkeys((s.capitalize(), s.upper())
                                            + ((s,) if s[1:] != s[1:].lower() else ()))]
            matchers.append(re.compile(
                before + '(?:' + '|'.join(re.escape(f) for f in forms)
                + ')' + after + _COLLOCATION_GUARD))   # case-sensitive by design
        # A name with an s straight after it (ADR-141): "Marigolds' daughter", a
        # possessive written s', or "Marigolds daughter" with no apostrophe at all.
        # The guard above wants no letter after the name, so these went through
        # whole, and the patient's name with them. The s, and the apostrophe, stay
        # outside the token, as a possessive 's does. In any case, a bare s is not
        # taken when the name and the s make an ordinary word. A name that is an
        # ordinary word itself matches with a capital only, so "jeans" stays and
        # "Jeans" is taken, as "Jean" is.
        suffixed = []
        plain_words = [s for s in plain if s[-1:].isalpha()]
        if plain_words:
            suffixed.append(re.compile(
                before + '(?P<n>' + '|'.join(re.escape(s) for s in plain_words) + ')'
                + _NAME_S_SUFFIX + after + _NOT_A_STREET + _NOT_A_ROAD + _COLLOCATION_GUARD, re.IGNORECASE))
        if capitalised:
            suffixed.append(re.compile(
                before + '(?P<n>' + '|'.join(re.escape(f) for f in forms) + ')'
                + _NAME_S_SUFFIX + after + _NOT_A_STREET + _NOT_A_ROAD + _COLLOCATION_GUARD))

        def _replace(m: re.Match) -> str:
            return token_lookup.get(m.group(0).lower(), m.group(0))

        def _replace_suffixed(m: re.Match) -> str:
            name, tail = m.group('n'), m.group('s')
            if name.lower() not in token_lookup or (
                    m.re is suffixed[0] and plain_words and _plain_s_word(name, tail, words)) \
                    or _after_house_number(m):
                return m.group(0)
            return token_lookup[name.lower()] + tail

        def _apply_patterns(text: str, late: bool) -> str:
            """Regex identifiers first, so a whole span becomes one token.

            Those marked late (ADR-068's any-spacing phones) run after the
            literal identifiers instead, and take only what they left.
            """
            for pattern in (patterns or []):
                if pattern.pattern.startswith(_LATE_PATTERN) == late:
                    text = _safe_sub(pattern, lambda m: (mint or vault.mint_patient)(m.group(0)), text)
            return text

        def _redact_str(text: str) -> str:
            text = _apply_patterns(text, late=False)
            for matcher in matchers:
                text = _safe_sub(matcher, _replace, text)
            for matcher in suffixed:
                text = _safe_sub(matcher, _replace_suffixed, text)
            return _apply_patterns(text, late=True)

        def _walk(obj, key=None):
            # Analyte names, units and codes never hold a person: leave
            # "White Cell Count" alone for a patient named White.
            if isinstance(obj, str):
                if key and str(key).strip().lower() in NON_NAME_KEYS:
                    return obj
                # The patient's own name fields are the name by definition:
                # tokenised whole, whatever the capitals (ADR-051). A middle
                # name that is an ordinary word, stored in lower case, slipped
                # past the Title-case rule and was labelled an HCP.
                if (mint is None and key and str(key).strip().upper() in _OWN_NAME_FIELDS
                        and obj.strip().lower() in token_lookup):
                    return token_lookup[obj.strip().lower()]
                return _redact_str(obj)
            if isinstance(obj, dict):
                return {k: _walk(v, k) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(item, key) for item in obj]
            if isinstance(obj, tuple):
                return tuple(_walk(item, key) for item in obj)
            return obj

        return _walk(data)
