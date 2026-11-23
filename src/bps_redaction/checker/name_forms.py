"""Given-name variants — diminutives, both directions, plus Australian forms.

Used to widen tier-0 search only. A patient recorded as Catherine may be
called Cathy in a consultation; one recorded as Cathy may be written as
Catherine. Neither is in BP's fields, so exact matching can't find them.

**Audit search terms only.** Nothing here goes into redaction: replacing
ordinary words like "will", "may" or "mark" with tokens would corrupt the
clinical text the model reads. See data/NICKNAMES_SOURCE.md.

Two sources:
  - data/nicknames.csv — carltonnorthern/nicknames, Apache-2.0. US-centric
    and biased towards traditionally African American names, by its authors'
    own account.
  - generated Australian forms (-o, -zza, -y/-ie), which that dataset lacks
    and which are common in speech: Davo, Robbo, Shazza, Bazza, Kezza.
"""
import csv
import functools
import re
from pathlib import Path

_DATA_FILE = Path(__file__).resolve().parents[1] / 'data' / 'nicknames.csv'

# Diminutives that are also ordinary English words. Matching these
# case-insensitively would flag half of every transcript, so they are searched
# for only when capitalised.
AMBIGUOUS = frozenset({
    'will', 'may', 'mark', 'bill', 'art', 'rich', 'bob', 'dick', 'don', 'rob',
    'jack', 'jean', 'june', 'sue', 'pat', 'norm', 'gene', 'hope', 'grace',
    'faith', 'joy', 'chip', 'buck', 'bud', 'dot', 'earl', 'frank', 'guy',
    'hank', 'herb', 'jim', 'lane', 'max', 'mick', 'nick', 'pete', 'ray',
    'red', 'sandy', 'sonny', 'tad', 'van', 'wade', 'ward', 'drew', 'penny',
    # ordinary words that appear as diminutives in the dataset
    'day', 'kit', 'dob', 'hob', 'peg', 'marge', 'midge', 'bell', 'polly',
    'trina', 'lena', 'kay', 'sha', 'shay', 'wil', 'dick', 'chip', 'buck',
    # clinical and document labels that collide with diminutives
    'phil', 'don', 'ray', 'rod', 'del', 'les', 'mel', 'reg', 'vi', 'ida',
})

_VOWELS = 'aeiou'

# Generated forms that are never a person: calendar words the dictionary lists
# only capitalised, so the plain-word test below can't see them ('july').
_NOT_A_NAME = frozenset({
    'january', 'february', 'march', 'september', 'october',
    'november', 'december', 'july', 'monday', 'tuesday', 'wednesday',
    'thursday', 'friday', 'saturday', 'sunday',
})

from ..words import word_list_path

_WORD_LIST_PATHS = (word_list_path(),)


@functools.lru_cache(maxsize=1)
def _plain_words() -> frozenset:
    """Words the dictionary lists in lower case only — never as a name.

    'hello' qualifies; 'sally' and 'terry' don't, as the list also has Sally
    and Terry. Empty if no word list is installed (forms are then kept).
    """
    for path in _WORD_LIST_PATHS:
        try:
            entries = {w.strip() for w in Path(path).read_text(errors='ignore').splitlines()}
        except OSError:
            continue
        return frozenset(w for w in entries
                         if w.isalpha() and w.islower() and w.capitalize() not in entries)
    return frozenset()


@functools.lru_cache(maxsize=1)
def _lookup() -> dict[str, set[str]]:
    """Bidirectional map: every name to every name related to it."""
    related: dict[str, set[str]] = {}
    if not _DATA_FILE.exists():
        return related

    with _DATA_FILE.open(newline='', encoding='utf-8') as handle:
        for row in csv.reader(handle):
            if len(row) < 3 or row[1] != 'has_nickname':
                continue
            canonical, nickname = row[0].strip().lower(), row[2].strip().lower()
            if not canonical or not nickname:
                continue
            related.setdefault(canonical, set()).add(nickname)
            related.setdefault(nickname, set()).add(canonical)
    return related


def _australian_forms(name: str) -> set[str]:
    """Generate -o, -zza and -y/-ie diminutives.

    Approximate by construction: Dav(id) -> Davo, Rob(ert) -> Robbo,
    Sha(ron) -> Shazza, Ba(rry) -> Bazza, Cath(erine) -> Cathy/Cathie.
    Some output is nonsense, which costs a reviewer nothing; missing a real
    one costs a finding.
    """
    lowered = re.sub(r'[^a-z]', '', name.lower())
    if len(lowered) < 3:
        return set()

    forms: set[str] = set()

    # -o: stem of 3-4 characters, trailing vowel dropped, consonant doubled
    # where the stem ends in a single consonant after a short vowel.
    stem = lowered[:4].rstrip(_VOWELS) or lowered[:3]
    if len(stem) >= 2:
        forms.add(stem + 'o')
        if stem[-1] not in _VOWELS and len(stem) >= 3 and stem[-2] in _VOWELS:
            forms.add(stem + stem[-1] + 'o')

    # -zza: everything up to and including the first vowel
    match = re.match(r'^[^aeiou]*[aeiou]', lowered)
    if match and len(match.group()) >= 2:
        forms.add(match.group() + 'zza')

    # -y / -ie on a 3-5 character stem
    for size in (3, 4, 5):
        if len(lowered) > size:
            trimmed = lowered[:size].rstrip(_VOWELS)
            if len(trimmed) >= 3:
                forms.add(trimmed + 'y')
                forms.add(trimmed + 'ie')

    forms.discard(lowered)
    # Construction also makes real words ('hello', 'july', 'bony').
    # Title case can't rule those out — "Hello," opens a sentence.
    return forms - _plain_words() - _NOT_A_NAME


def variants(name: str) -> list[dict]:
    """Every searchable variant of a given name.

    Each is {value, source, match_mode}, where match_mode is 'capitalised'
    for forms that are also ordinary words.
    """
    cleaned = (name or '').strip().lower()
    if len(cleaned) < 3:
        return []

    out: list[dict] = []
    seen: set[str] = {cleaned}

    def add(value: str, source: str) -> None:
        if value in seen or len(value) < 3:
            return
        seen.add(value)
        out.append({
            'value': value,
            'source': source,
            'match_mode': 'capitalised' if value in AMBIGUOUS else 'any',
        })

    for related in sorted(_lookup().get(cleaned, ())):
        add(related, 'nickname')
    for generated in sorted(_australian_forms(cleaned)):
        add(generated, 'au_diminutive')
    return out
