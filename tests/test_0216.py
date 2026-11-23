"""0.2.16: the patient's own address parts on one line, in BP's order.

A measure on a fixed list of patients and a one-patient check with invented stand-ins
found the patient's address left visible when a document wrote only part of
it: the unit and number, a tab, two of the three ADDRESS1 words and the postcode; or the
line in capitals with a word as its initial and a street type BP doesn't hold. Every
rendering the core had was the whole stored line. Invented people and places only.
"""
import copy
import json
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists, __version__
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.redaction import _address_run_matcher, _address_run_span

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
UNIT = {'ADDRESS1_PART': 'Unit 10, Larkspur Bellwether Ferncombe', 'ADDRESS2_PART': 'Tansy Avenue',
        'CITY_PART': 'Thornbury', 'POSTCODE_PART': '4999',
        'ADDRESS': 'Unit 10, Larkspur Bellwether Ferncombe Tansy Avenue, Thornbury QLD 4999'}


def _redactor(**demo):
    data = copy.deepcopy(json.loads(FIXTURE.read_text()))
    data['patients']['1001']['demographics'].update(demo)
    return Redactor(FixtureSource(data), lists=StaticLists({}))


def _out(text, profile='prompt', **demo):
    r = _redactor(**(demo or UNIT))
    v = r.vault('k')
    return r.record({'t': text}, 1001, v)[0]['t'] if profile == 'record' else r.prompt(text, 1001, v)


def test_version():
    assert tuple(int(p) for p in __version__.split('.')) >= (0, 2, 16)
    assert RULESET.startswith('medui-adr')


LEAKS = ['larkspur', 'bellwether', 'ferncombe', 'tansy', 'thornbury', '4999', 'unit 10']


@pytest.mark.parametrize('profile', ['record', 'prompt'])
@pytest.mark.parametrize('text', [
    ' Unit 10,\tLarkspur Bellwether.\t4999',                          # a word and line 2 left out, tabs
    ' UNIT 10 LARKSPUR B FERNCOMBE ST,\tTHORNBURY.\t4999',             # capitals, initial, added type
    'Unit 10 Larkspur Bellwether, Thornbury QLD 4999',
    'Addr: U10 Larkspur Bellwether Ferncombe Tansy Ave Thornbury 4999',
    'Unit 10,  Larkspur   Bellwether',                                 # spaces only, no suburb
    'Larkspur Bellwether Ferncombe',                                   # the words alone
])
def test_the_partial_address_goes(profile, text):
    out = _out(text, profile).lower()
    for leak in LEAKS:
        assert leak not in out, (leak, out)


@pytest.mark.parametrize('profile', ['record', 'prompt'])
def test_the_whole_address_still_goes(profile):
    out = _out('To:\nUnit 10, Larkspur Bellwether Ferncombe\nTansy Ave, Thornbury 4999\nSeen today.',
               profile).lower()
    for leak in LEAKS:
        assert leak not in out
    assert 'seen today.' in out


def _span(text, **demo):
    d = dict(UNIT, **demo)
    pattern, kinds = _address_run_matcher(d)
    spans = [_address_run_span(m, kinds) for m in pattern.finditer(text)]
    return [text[a:b] for a, b in (s for s in spans if s)]


@pytest.mark.parametrize('text', [
    'Thornbury Medical Centre, 99 Main Street, THORNBURY QLD 4999',   # the practice, same suburb
    'Thornbury 4999',
    'Unit 10',                                                          # a number alone
    'reviewed 10 days ago',
    'Bellwether',                                                       # one word alone
    'Dr Larkspur rang',                                                 # one word alone
    'unit 10 of insulin',
])
def test_not_a_run(text):
    assert _span(text) == []


def test_line_breaks_and_escapes_end_a_run():
    assert _span('Unit 10\nLarkspur') == []
    assert _span('Unit 10,\\nLarkspur') == []
    assert _span('Unit 10,\\Larkspur') == []


def test_an_initial_counts_only_between_words():
    assert _span('Larkspur B') == []                    # one word and an initial
    assert _span('Larkspur B Ferncombe') == ['Larkspur B Ferncombe']


def test_an_ordinary_word_needs_its_capitals():
    demo = {'ADDRESS1_PART': '12 Hill Street', 'ADDRESS2_PART': '', 'CITY_PART': 'Thornbury'}
    assert _span('walked 12 hill streets', **demo) == []
    assert _span('lives at 12 Hill', **demo) == ['12 Hill']
    assert _span('12 HILL ST', **demo) == ['12 HILL ST']


def test_apostrophe_forms():
    demo = {'ADDRESS1_PART': "34 O'Rourke Bellwether Road", 'ADDRESS2_PART': ''}
    for text in ("34 ORourke Bellwether", "34 O Rourke Bellwether", "34 O’Rourke  Bellwether Rd"):
        assert _span(text, **demo), text


def test_one_word_needs_a_type_or_the_locality():
    demo = {'ADDRESS1_PART': 'Bellwether Road', 'ADDRESS2_PART': ''}
    assert _span('Bellwether', **demo) == []
    assert _span('Bellwether Road', **demo) == ['Bellwether Road']
    assert _span('Bellwether, Thornbury', **demo) == ['Bellwether, Thornbury']


def test_ordinary_words_in_lower_case_are_left():
    # "larkspur" and "bellwether" are dictionary words: in lower case they are words.
    assert _span('a larkspur bellwether') == []


def test_two_dictionary_words_alone_are_a_place():
    demo = {'ADDRESS1_PART': 'Unit 4, Gold Coast Highway', 'ADDRESS2_PART': '', 'CITY_PART': 'Thornbury'}
    assert _span('Location: Gold Coast QLD', **demo) == []
    assert _span('Unit 4 Gold Coast', **demo) == ['Unit 4 Gold Coast']
    assert _span('Gold Coast Hwy', **demo) == ['Gold Coast Hwy']
    assert _span('Gold Coast, Thornbury', **demo) == ['Gold Coast, Thornbury']
