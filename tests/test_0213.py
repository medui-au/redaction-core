"""0.2.13: a road named for the surname + s, with words before the type (MedUI ADR-143).

0.2.12 stood the name + s match down only when the street type was the very next word, so
"123 QUILLFEATHERS GULLY CREEK RD" came out as "123 [Redacted Patient: …]S GULLY CREEK RD".
Now it stands down after a house number, and before "Rd"/"Road" one or two words on.
Invented people and streets only.
"""
import copy
import json
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists, __version__
from bps_redaction.adapters.fixture import FixtureSource

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'


@pytest.fixture
def r():
    data = copy.deepcopy(json.loads(FIXTURE.read_text()))
    return Redactor(FixtureSource(data), lists=StaticLists({}))


def test_version():
    assert __version__ >= '0.2.13' and RULESET.startswith('medui-adr')


@pytest.mark.parametrize('text', [
    '123 QUILLFEATHERS GULLY CREEK RD',
    '123 Quillfeathers Gully Creek Road',
    '45 Quillfeathers Creek Rd',
    'Lives at 7 Quillfeathers Gully Creek Rd, Mudgeeraba',
])
@pytest.mark.parametrize('profile', ['record', 'prompt'])
def test_never_a_patient_token_then_the_road(r, text, profile):
    vault = r.vault('k')
    out = (r.record({'t': text}, 1001, vault)[0]['t'] if profile == 'record'
           else r.prompt(text, 1001, vault))
    # The 0.2.12 shape: the name taken, the number and the road words left.
    assert ']s ' not in out.lower() and ']s\'' not in out.lower()
    assert 'gully' not in out.lower() and 'creek' not in out.lower()


def test_no_number_two_words_then_rd_never_the_0212_shape(r):
    out = r.prompt('QUILLFEATHERS GULLY CREEK RD', 1001, r.vault('k'))
    assert ']S GULLY' not in out


@pytest.mark.parametrize('text', ["Marigolds' daughter rang.", 'Quillfeathers daughter rang',
                                  "the Quillfeathers' dog at St Vincent's",
                                  'Marigolds wife at St Vincent'])
def test_the_name_with_an_s_is_still_taken(r, text):
    out = r.prompt(text, 1001, r.vault('k'))
    assert 'marigold' not in out.lower() and 'quillfeather' not in out.lower()


def test_the_patients_own_road_without_its_number_goes_whole():
    data = copy.deepcopy(json.loads(FIXTURE.read_text()))
    dm = data['patients']['1001']['demographics']
    dm['ADDRESS1_PART'] = '7 Quillfeathers Gully Creek Rd'
    dm['ADDRESS'] = '7 Quillfeathers Gully Creek Rd, Mudgeeraba QLD 4213'
    r = Redactor(FixtureSource(data), lists=StaticLists({}))
    out = r.prompt('Quillfeathers Gully Creek Rd', 1001, r.vault('k'))
    assert 'gully' not in out.lower() and 'quillfeather' not in out.lower()


def test_another_road_left_whole_pauses(r):
    # Not the patient's road and no number: no pass takes it, so the checker reports
    # the surname + s (tier 0) and MedUI pauses, as for 0.2.12's "Quillfeathers Road".
    from bps_redaction.checker.scan import is_blocking
    out = r.prompt('Quillfeathers Gully Creek Rd', 1001, r.vault('k'))
    assert is_blocking(r.verdict(out, 1001))
