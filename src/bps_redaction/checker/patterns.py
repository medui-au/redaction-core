"""Local pattern detection — no network, no cost, runs before any cloud call.

Deliberately limited to identifiers with a *shape*: numbers, addresses and
titled names. Judgement calls about free text (is "Margaret" a patient, a
relative or a medication brand?) are left to the identifier passes and to
human review.

Findings are offsets and a type. Matched text is never stored.
"""
import functools
import re

# Detector history. The stamp is reported with every flag, so flags from
# before and after a change in what is searched for can be told apart.
# v20: titled names with a middle initial, and an initial plus surname straight
# after a redacted name (name_after_token) (ADR-065).
# v19: Medicare provider numbers with a valid check letter (ADR-061).
# v18: a nickname form that is not an ordinary word is searched for in any
#      case (ADR-056).
# v17: a tier-0 name that is an ordinary word and has capitals inside it
#      ('McDonald', 'VanDyke') is found as stored (ADR-055).
# v16: a name variant that is also an auxiliary verb ('Will', 'May') is not
#      flagged where it opens a sentence — "...clinic. Will continue" is the
#      verb. Recorded names are still flagged anywhere.
# v15: email addresses without their ending ('ruby9@gmr') and spoken ones
#      ('jsmith at hotmail dot com'), as redaction catches them (ADR-050).
# v14: the practice's own nickname table (ADR-048); a name variant after a
#      hyphen ('X-Ray') is not a name.
# v13: a DOB label followed by a redaction token and then a date is not a
#      DOB — the patient's own was redacted and the date is a later one. A
#      large share of 'someone else's DOB' findings had been this.
# v12: share links and private-network links (ADR-043); ordinary-word names
#      in capitals (ADR-045); 8-digit DOBs (ADR-044).
# v11: each word of a multi-word given name ('Mary Jane' -> 'Mary'), its
#      nicknames, and spelling variants of middle and preferred names (ADR-042).
# v10: fewer false name variants — generated forms that are plain words or
#      months dropped ('hello', 'july'), ordinary-word variants Title case
#      only, and a match followed by 't ("Don't") skipped.
# v9:  tier-1 contact names include the practice's own BP users ("First
#      Surname", "F. Surname") — nurses and receptionists were missed before.
# v8:  the street name without its house number ('Mt Example Road'), two-plus
#      words before the type only.
# v7:  half-spaced and dotted phone groupings ('04xx xxxxxx').
# v6:  spacing and punctuation variants — collapsed street names, and
#      punctuation forms for street lines and NOK names.
DETECTOR_VERSION = 'pattern-v33'
# v33 (MedUI ADR-155): tier 0, a hyphenated name with a space beside the hyphen ('Xxxx- Yyyy'),
#      from the builder's spellings.
# v32 (MedUI ADR-154): tier 0, a zero without the area digit before the patient's 8 phone digits.
# v31 (MedUI ADR-153): tier 0, the area digit without its zero before the patient's 8 phone
#      digits, the DOB with day and month swapped, and the first 7 to 9 Medicare digits alone.
# v30 (MedUI ADR-152): tier 0, the patient's phone digits with a run of 4 to 12 spaces or tabs
#      between any two (column-aligned text), and an 8-digit number without its area code.
# v29 (MedUI ADR-141): the patient's first name, surname or preferred name (5+ letters, not an
#      ordinary word) glued to a letter or digit ('xQuillfeather'): tier 0, outside_rules.
# v28 (MedUI ADR-141): the patient's names and nicknames with an s straight after them ('Marigolds'').
# v27 (MedUI ADR-131): a phone label typed straight onto a mobile or landline ('ph0412…').
# v21 (2026-10-15, MedUI ADR-068 to 081): phone groups with up to three spaces or a
# spaced hyphen between them; a labelled identifier (the value after a label on the
# identifier_labels list); tier 0: the patient's phone digits in any spacing, their
# suburb (or address line 2's locality) with their postcode, rarer street-type
# abbreviations, a hyphen between street words, a space dropped after the number or
# before the type. v22 (MedUI ADR-106/112): practitioners' preferred names, from
# the host's practitioner rows (a BP shadow user, or rows a host adds); no rule change.

# Between digit groups: up to three spaces, or a hyphen with spaces around it
# (v21, MedUI ADR-068).
_GROUP_GAP = r'(?:[ \t]{1,3}|[ \t]*-[ \t]*)?'

# Redaction tokens. Masked out before matching so a token
# can never itself be reported as a leak.
# A phone label typed straight onto the number (v27); the redactor's list.
_GLUED_LABELS = ('telephone', 'mobile', 'phone', 'cell', 'home', 'work', 'fax', 'mob', 'tel',
                 'ph', 'h', 'w', 'm', 'p', 'f', 't')
_START = r'(?:\b|' + '|'.join(r'(?<=\b(?i:%s))' % w for w in _GLUED_LABELS) + ')'

_TOKEN_RE = re.compile(r'\[Redacted [A-Za-z]+: [A-Z]+-[0-9a-f]+\]')

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ('mobile_number', re.compile(_START + r'(?:\+?61' + _GROUP_GAP + r'|0)4\d{2}' + _GROUP_GAP + r'\d{3}'
                                 + _GROUP_GAP + r'\d{3}\b')),
    ('landline_number', re.compile(r'(?:\(0[2-8]\)' + _GROUP_GAP + r'|' + _START + r'0[2-8]' + _GROUP_GAP + r')\d{4}'
                                   + _GROUP_GAP + r'\d{4}\b')),
    # Medicare needs either its printed grouping (4-5-1) or the word nearby:
    # a bare 10-digit run matches record ids in the data bundle, not cards.
    ('medicare_number', re.compile(
        r'(?:\b[2-6]\d{3}[ -]\d{5}[ -]\d(?:[ /-]?[1-9])?\b'
        r'|\bmedicare\b\D{0,15}[2-6]\d{3}[ -]?\d{5}[ -]?\d(?:[ /-]?[1-9])?\b)', re.I)),
    ('ihi_number', re.compile(r'\b800360\d{10}\b')),
    ('dva_number', re.compile(r'\b[NVQWST][ACGSX]\d{6}[A-Z]?\b')),
    ('email_address', re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b')),
    ('date_of_birth', re.compile(
        r'\b(?:d\.?o\.?b\.?|date of birth|born)\b\D{0,12}'
        r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', re.I)),
    ('street_address', re.compile(
        r'\b\d{1,4}[A-Za-z]?\s+(?:[A-Z][a-z]+\s+){1,3}'
        r'(?:St|Street|Rd|Road|Ave|Avenue|Dr|Drive|Cres|Crescent|Ct|Court|'
        r'Pde|Parade|Pl|Place|Lane|Ln|Tce|Terrace|Hwy|Highway|Cl|Close)\b')),
    ('titled_name', re.compile(
        r'\b(?:Dr|Dr\.|Doctor|Prof|Prof\.|Professor|Mr|Mr\.|Mrs|Mrs\.|Ms|Ms\.|Miss)\s+'
        r'[A-Z][a-z]{2,}(?:(?:\s+[A-Z]\.?(?=\s))?\s+[A-Z][a-z]{2,})?')),
    # An initial and a surname straight after a redacted name: what was left
    # when only "Dr First" of "Dr First I. Surname" became a token (ADR-065).
    # Masked tokens are NUL characters; "I think" is not a name.
    ('name_after_token', re.compile(
        r'(?:(?<=\x00[ \t])|(?<=\x00[ \t]{2}))(?!I[ \t])[A-Z]\.?[ \t]+[A-Z][a-z]{2,}')),
]


# Words that follow a title but are not names. Speaker labels the scribe's
# diarization step inserts ("[Doctor Monologue]") are the main source, plus
# turns of phrase ("Dr Google") and placeholders.
_NOT_A_NAME = frozenset({
    'monologue', 'dialogue', 'dialog', 'speaker', 'patient', 'doctor', 'doctors',
    'clinician', 'nurse', 'receptionist', 'practitioner', 'unknown', 'male',
    'female', 'consultation', 'consult', 'notes', 'note', 'summary', 'transcript',
    'audio', 'silence', 'inaudible', 'crosstalk', 'background', 'google',
    'name', 'surname', 'redacted', 'practice', 'clinic', 'medical', 'centre',
    'center', 'hospital', 'pharmacy', 'today', 'yesterday', 'tomorrow',
})


def _is_not_a_name(matched: str) -> bool:
    """True when every word after the title is a known non-name word."""
    words = matched.split()[1:]
    return bool(words) and all(w.strip('.,;:').lower() in _NOT_A_NAME for w in words)


# Filler for masked tokens: same length so offsets stay true, but NOT
# whitespace. Masking with spaces let '\s+' leap a whole token, so
# "Dr [Redacted HCP: HP-…] Imaging Services" matched as a titled name —
# gluing the title to the words after the token.
_MASK_CHAR = '\x00'


def mask_tokens(text: str) -> str:
    """Blank out redaction tokens, preserving length so offsets stay true.

    The filler is a null character rather than a space: it is neither a word
    character nor whitespace, so no pattern can match across a token.
    """
    return _TOKEN_RE.sub(lambda m: _MASK_CHAR * len(m.group()), text)


# Links that open a record or lead into the practice network (ADR-043): an
# image-viewer share link whose query is the key (GUID, PIN, code), or any
# link to a private address. Redaction replaces both with '[Link removed]'.
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://[^\s'\"<>\]\)\\]+")
_PRIVATE_HOST_RE = re.compile(
    r'^(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|127\.|169\.254\.|localhost$)')
_ACCESS_KEYS = frozenset({
    'guid', 'pin', 'code', 'token', 'key', 'access_token', 'accesstoken',
    'accesskey', 'apikey', 'api_key', 'auth', 'sig', 'signature', 'password',
    'pwd', 'sessionid', 'sid', 'otp', 'accesscode', 'ticket',
})


# Email addresses without their ending, or spoken (ADR-050), as redaction catches them.
# Same shapes, so a rescan measures the rule. Clinical '@' shorthand starts
# with a number ("1g@noon") or ends in an ordinary word ("2@bed") and is left.
_BARE_AT_RE = re.compile(r"(?<![\w.+@-])([A-Za-z][\w.+-]{1,40})@([A-Za-z][\w-]{1,30})(?![\w@-]|\.\w)")
_MAIL_PROVIDERS = ('gmail', 'hotmail', 'outlook', 'yahoo', 'bigpond', 'icloud', 'live',
                   'optusnet', 'iinet', 'internode', 'westnet', 'ozemail', 'tpg', 'aol',
                   'msn', 'protonmail', 'proton')
_SPOKEN_EMAIL_RE = re.compile(
    r"(?i)(?<![\w'’])((?:[a-z0-9][\w.]*\s+){0,2})([a-z0-9][\w.]*)\s+at\s+(" + '|'.join(_MAIL_PROVIDERS) + r")\b"
    r"((?:\s+dot\s+(?:com|net|org|au))*)")
_NOT_AN_ADDRESS_NAME = frozenset({
    'them', 'me', 'him', 'her', 'us', 'you', 'it', 'to', 'email', 'emailed', 'mail',
    'address', 'is', 'was', 'and', 'or', 'the', 'a', 'an', 'on', 'my', 'your',
    'their', 'his', 'its', 'it’s', "it's", 'that', 'this',
})


@functools.lru_cache(maxsize=1)
def _lowercase_words() -> frozenset:
    from ..words import word_list_path
    for path in (word_list_path(),):
        try:
            with open(path, errors='ignore') as handle:
                return frozenset(w.strip() for w in handle if w.strip().islower())
        except OSError:
            continue
    return frozenset()


def _email_shapes(text: str):
    """(finding_type, start, end) for partial and spoken addresses."""
    for m in _BARE_AT_RE.finditer(text):
        right = m.group(2).lower()
        if right in _MAIL_PROVIDERS or right not in _lowercase_words():
            yield 'partial_email', m.start(), m.end()
    for m in _SPOKEN_EMAIL_RE.finditer(text):
        if m.group(2).lower() in _NOT_AN_ADDRESS_NAME:
            continue
        if m.group(3).lower() == 'live' and not m.group(4).strip():
            continue
        yield 'spoken_email', m.start(2), m.end()


def _is_access_link(url: str) -> bool:
    from urllib.parse import urlsplit, parse_qsl
    try:
        parts = urlsplit(url.rstrip('.,;:!?'))
        host = (parts.hostname or '').lower()
        keys = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    except ValueError:
        return True
    return bool(_PRIVATE_HOST_RE.match(host)) or '.' not in host or bool(keys & _ACCESS_KEYS)


_PROVIDER_LOCATION = '0123456789ABCDEFGHJKLMNPQRTUVWXY'
_PROVIDER_RE = re.compile(r'(?<![\w/])(\d{5,6}[0-9A-HJ-NP-RT-Y][ABFHJKLTWXY])(?![\w/])')


def _provider_number_ok(number: str) -> bool:
    """The Medicare provider number check letter (ADR-061)."""
    stem, location, check = number[:-2].zfill(6), number[-2], number[-1]
    total = sum(int(d) * w for d, w in zip(stem, (3, 5, 8, 4, 2, 1))) + _PROVIDER_LOCATION.index(location) * 6
    return 'YXWTLKJHFBA'[total % 11] == check


def _labelled_identifiers(masked: str):
    """(start, end) of each value after a listed label (v21), with the label patterns the
    core's redaction builds from the identifier_labels list, so there is one list and one
    matching. A date, or a bare year without a colon/number word before it, is skipped."""
    from ..redaction import _ID_DATE_RE, _ID_YEAR_RE, _identifier_label_patterns
    for pattern in _identifier_label_patterns():
        for match in pattern.finditer(masked):
            value = match.group('value')
            if _ID_DATE_RE.fullmatch(value):
                continue
            between = match.group(0)[:match.start('value') - match.start()]
            if _ID_YEAR_RE.fullmatch(value) and not re.search(r'[:#=]|\bN(?:o|um|umber)\b', between[-12:], re.I):
                continue
            yield match.start('value'), match.end('value')


def scan_chunk(text: str, offset: int = 0) -> list[dict]:
    """Return findings for one chunk as {finding_type, match_start, match_end, score}.

    Offsets are absolute (chunk offset + position within the chunk), so the
    review page can highlight them in the whole prompt.
    """
    masked = mask_tokens(text)
    findings: list[dict] = []
    for finding_type, pattern in _PATTERNS:
        for match in pattern.finditer(masked):
            # Speaker labels like "[Doctor Monologue]" are excluded by the
            # word list, not by being in brackets: "[Dr Smith]" is a real name.
            if finding_type == 'titled_name' and _is_not_a_name(match.group()):
                continue
            # 'DOB: [token] seen 12/10/2026' — the DOB is the token; the date
            # after it is something else (masked tokens are NUL characters).
            if finding_type == 'date_of_birth' and '\x00' in match.group():
                continue
            findings.append({
                'finding_type': finding_type,
                'match_start': offset + match.start(),
                'match_end': offset + match.end(),
                'score': 1.0,
            })
    for match in _PROVIDER_RE.finditer(masked):
        if _provider_number_ok(match.group(1)):
            findings.append({'finding_type': 'provider_number', 'match_start': offset + match.start(),
                             'match_end': offset + match.end(), 'score': 1.0})
    for finding_type, start, end in _email_shapes(masked):
        findings.append({'finding_type': finding_type, 'match_start': offset + start,
                         'match_end': offset + end, 'score': 1.0})
    for match in _URL_RE.finditer(masked):
        if _is_access_link(match.group()):
            findings.append({
                'finding_type': 'access_link',
                'match_start': offset + match.start(),
                'match_end': offset + match.end(),
                'score': 1.0,
            })
    for start, end in _labelled_identifiers(masked):
        findings.append({'finding_type': 'labelled_identifier', 'match_start': offset + start,
                         'match_end': offset + end, 'score': 1.0})
    return findings
