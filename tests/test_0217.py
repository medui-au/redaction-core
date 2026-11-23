"""0.2.17: the patient's own phone number with its groups in columns.

The practice's `/pii_list` measure on 0.2.16 found a patient's whole number
still visible in 7 documents, every one with a run of 4 to 12 spaces inside it: column-
aligned text spreading the groups ('5550        1234', '07    5550    1234'). The any-spacing
rule (ADR-068) allowed up to three. Invented numbers only.
"""
import copy
import json
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists, __version__
from bps_redaction.adapters.fixture import FixtureSource

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
LOCAL8 = {'PHONE_HOME': '55501234'}          # stored with no area code, as most are


def _redactor(**demo):
    data = copy.deepcopy(json.loads(FIXTURE.read_text()))
    data['patients']['1001']['demographics'].update(demo)
    return Redactor(FixtureSource(data), lists=StaticLists({}))


def _out(text, profile='prompt', **demo):
    r = _redactor(**(demo or LOCAL8))
    v = r.vault('k')
    return r.record({'t': text}, 1001, v)[0]['t'] if profile == 'record' else r.prompt(text, 1001, v)


def test_version():
    assert tuple(int(p) for p in __version__.split('.')) >= (0, 2, 17)
    assert RULESET.startswith('medui-adr')


@pytest.mark.parametrize('profile', ['record', 'prompt'])
@pytest.mark.parametrize('text', [
    'H: 5550     1234 W',                         # 4 [5 spaces] 4
    '07.5550            1234 Next Ph:',           # area code with a dot, then 12 spaces
    'PHONE#:07    5550     1234',                 # area code, two wide gaps
    'Ph(H)    07    5550    1234',
    'Home 5550\t\t1234',                          # tabs
    'Home 55  501        234',                    # 2 [2 spaces] 3 [wide] 3
])
def test_local_number_in_columns_is_taken(text, profile):
    out = _out(text, profile)
    assert '5550' not in out and '1234' not in out and '501' not in out.replace('5501', '')


@pytest.mark.parametrize('profile', ['record', 'prompt'])
def test_ten_digits_stored_eight_written_in_columns(profile):
    out = _out('Ph 5550        1234', profile, PHONE_HOME='0755501234')
    assert '5550' not in out and '1234' not in out


@pytest.mark.parametrize('profile', ['record', 'prompt'])
def test_mobile_in_columns_is_taken(profile):
    out = _out('Mob 0491 570        156 ok', profile)     # fixture mobile 0491570156
    assert '570' not in out and '156' not in out


@pytest.mark.parametrize('text', [
    '5550\n1234',                                 # never across a line break
    '5550' + ' ' * 13 + '1234',                   # wider than 12 is two columns
    '5550        9876',                           # someone else's digits
    '15550    1234',                              # inside a longer number
    '5550    12345',
])
def test_not_taken(text):
    assert _out(text) == text


def test_checker_sees_the_column_spaced_number():
    r = _redactor(**LOCAL8)
    hits = [h for h in r.verdict('H: 5550     1234 W', 1001)['hits'] if h['tier'] == 0]
    assert any(h['type'] == 'patient_phone' for h in hits)


@pytest.mark.parametrize('text', ['H: 5550     1234 W', 'Ph(H)    07    5550    1234',
                                  'Mob 0491 570        156 ok'])
def test_redacted_text_is_clean_for_the_checker(text):
    r = _redactor(**LOCAL8)
    out = r.prompt(text, 1001, r.vault('k'))
    assert not [h for h in r.verdict(out, 1001)['hits'] if h['tier'] == 0]
