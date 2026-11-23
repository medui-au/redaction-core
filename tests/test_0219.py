"""0.2.19: a zero without the area digit before the own phone digits (MedUI ADR-154).

From the practice's check of 0.2.18: a home number stored as 8 digits, written with a
zero in front and grouped 5 and 4. 0.2.18 took an area digit in front; this is the zero with
the area digit missing. Invented values only.
"""
import copy
import json
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists, __version__
from bps_redaction.adapters.fixture import FixtureSource

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
DEMO = {'PHONE_HOME': '55501234'}


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
    assert tuple(int(p) for p in __version__.split('.')) >= (0, 2, 19)
    assert RULESET.startswith('medui-adr')


@pytest.mark.parametrize('profile', ['record', 'prompt'])
@pytest.mark.parametrize('text', [
    'Ph: 05550 1234(H)',
    'Home 0 5550 1234',
    'H 055501234.',
    'Ph:05550    1234',
])
def test_zero_without_area_digit_is_taken(text, profile):
    out = _out(text, profile)
    assert '5550' not in out and '1234' not in out


def test_zero_lead_with_stored_area_code():
    out = _out('Ph 05550 1234', PHONE_HOME='0755501234')
    assert '5550' not in out


def test_area_digit_forms_still_taken():
    assert '5550' not in _out('Ph 75550 1234')
    assert '5550' not in _out('Ph (07) 5550 1234')
    assert '5550' not in _out('Ph 5550 1234')


@pytest.mark.parametrize('text', [
    '105550 1234',            # a digit before the zero: a longer number
    '+05550 1234',
    '05550 12345',
    '05550 9876',             # not the patient's digits
    '005550 1234',
])
def test_zero_lead_not_taken(text):
    assert _out(text) == text


def test_checker_sees_zero_lead():
    assert _tier0('Ph: 05550 1234(H)')
    r = _redactor(**DEMO)
    out = r.prompt('Ph: 05550 1234(H)', 1001, r.vault('k'))
    assert not [h for h in r.verdict(out, 1001)['hits'] if h['tier'] == 0]
