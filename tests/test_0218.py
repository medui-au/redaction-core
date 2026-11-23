"""0.2.18: three more forms of the patient's own identifiers (MedUI ADR-153).

From the practice's `/pii_list` measure on 0.2.17: the area digit without its
zero before the 8 phone digits, the DOB with day and month swapped, and the Medicare number
cut short to its first 7 digits. Invented values only.
"""
import copy
import re
import json
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists, __version__
from bps_redaction.adapters.fixture import FixtureSource

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
# Fixture card 2953701561. DOB 1950-07-03: swapped is 07/03/1950.
DEMO = {'PHONE_HOME': '55501234', 'DOB': '1950-07-03'}


def _redactor(**demo):
    data = copy.deepcopy(json.loads(FIXTURE.read_text()))
    data['patients']['1001']['demographics'].update(demo)
    return Redactor(FixtureSource(data), lists=StaticLists({}))


def _out(text, profile='prompt', **demo):
    r = _redactor(**{**DEMO, **demo})
    v = r.vault('k')
    return r.record({'t': text}, 1001, v)[0]['t'] if profile == 'record' else r.prompt(text, 1001, v)


def _tier0(text, **demo):
    r = _redactor(**{**DEMO, **demo})
    return [h for h in r.verdict(text, 1001)['hits'] if h['tier'] == 0]


def test_version():
    assert tuple(int(p) for p in __version__.split('.')) >= (0, 2, 18)
    assert RULESET.startswith('medui-adr')


# ── (1) the area digit without its zero ──────────────────────────────────────

@pytest.mark.parametrize('profile', ['record', 'prompt'])
@pytest.mark.parametrize('text', [
    'Ph: 75550 1234(H)',
    'Home 7 5550 1234',
    'H 755501234.',
    'Ph:75550    1234',
])
def test_area_digit_without_zero_is_taken(text, profile):
    out = _out(text, profile)
    assert '5550' not in out and '1234' not in out


def test_area_digit_with_stored_area_code():
    out = _out('Ph 75550 1234', PHONE_HOME='0755501234')
    assert '5550' not in out


@pytest.mark.parametrize('text', [
    '175550 1234',            # a digit before the area digit: a longer number
    '+75550 1234',
    '75550 12345',
    '75550 9876',             # not the patient's digits
])
def test_area_digit_not_taken(text):
    assert _out(text) == text


def test_checker_sees_area_digit():
    assert _tier0('Ph: 75550 1234(H)')
    r = _redactor(**DEMO)
    out = r.prompt('Ph: 75550 1234(H)', 1001, r.vault('k'))
    assert not [h for h in r.verdict(out, 1001)['hits'] if h['tier'] == 0]


# ── (2) the DOB with day and month swapped ───────────────────────────────────

@pytest.mark.parametrize('profile', ['record', 'prompt'])
@pytest.mark.parametrize('text', [
    'Birthdate:\t07/03/1950    Age:',
    'born 7/3/1950',
    'DOB 07-03-1950',
    'DOB 7.3.1950',
])
def test_swapped_dob_is_taken(text, profile):
    assert '1950' not in _out(text, profile)


def test_dob_itself_still_taken():
    assert '1950' not in _out('DOB 03/07/1950')


@pytest.mark.parametrize('dob, text', [
    ('1950-07-15', '07/15/1950'),     # day over 12: no swap exists (and not a d/m date)
    ('1950-07-15', '15/07/1951'),
    ('1950-07-07', '07/07/1951'),
    ('1950-07-03', '07/04/1950'),     # another date in the birth year
    ('1950-07-03', '07/03/1951'),     # the swap in another year
])
def test_swap_not_taken(dob, text):
    assert _out(f'seen {text} ok', DOB=dob) == f'seen {text} ok'


def test_checker_sees_swapped_dob():
    hits = _tier0('Birthdate:\t07/03/1950    Age:')
    assert any(h['type'] in ('patient_dob', 'dob') or 'dob' in h['type'] for h in hits)


# ── (3) the Medicare number cut short ────────────────────────────────────────

@pytest.mark.parametrize('profile', ['record', 'prompt'])
@pytest.mark.parametrize('text', [
    'Medicare No:        :      2953701\n    Something:',
    'ref 2953701;',
    'card 2953 701 only',
    'card 29537015 only',
    'card 2953 70156 only',
    'Medicare No.: 2953 7015 61\n',            # grouped 4, 4, 2: the whole number goes
    'Medicare 29-5370-1561/1',
])
def test_medicare_cut_short_is_taken(text, profile):
    out = _out(text, profile)
    assert '2953' not in out


def test_four_four_two_leaves_nothing():
    out = _out('Medicare No.: 2953 7015 61 x')
    assert re.fullmatch(r'Medicare No\.: \[Redacted [^\]]+\] x', out)


def test_full_medicare_still_taken():
    assert '2953' not in _out('Medicare 2953 70156 1')
    assert '2953' not in _out('Medicare 2953701561/1')


@pytest.mark.parametrize('text', [
    '2953702 ok',           # not the patient's 7th digit
    '12953701 ok',          # inside a longer number
    '2953701-4 ok',
    '295370 ok',            # only 6 digits
])
def test_medicare_cut_short_not_taken(text):
    assert _out(text) == text


def test_checker_sees_medicare_cut_short():
    assert _tier0('ref 2953701;')
    r = _redactor(**DEMO)
    out = r.prompt('ref 2953701;', 1001, r.vault('k'))
    assert not [h for h in r.verdict(out, 1001)['hits'] if h['tier'] == 0]
