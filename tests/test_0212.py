"""0.2.12: a street named for the patient's surname + s stays whole (MedUI ADR-142).

0.2.11 (ADR-141) took the surname + s before the street-name and address passes ran, so
"12 Quillfeathers Road" for a patient surnamed Quillfeather came out as
"12 [Redacted Patient: …]s Road": the house number and the street type were left.
Invented people and streets only.
"""
import copy
import json
from pathlib import Path

import pytest

from bps_redaction import RULESET, Redactor, StaticLists, __version__
from bps_redaction.adapters.fixture import FixtureSource

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
ADDR = '[Redacted Address]'


@pytest.fixture
def r():
    data = copy.deepcopy(json.loads(FIXTURE.read_text()))
    dm = data['patients']['1001']['demographics']
    dm['ADDRESS1_PART'] = '12 Quillfeathers Road'
    dm['ADDRESS'] = '12 Quillfeathers Road, Mudgeeraba QLD 4213'
    return Redactor(FixtureSource(data), lists=StaticLists({}))


def test_version():
    assert __version__ >= '0.2.12'


@pytest.mark.parametrize('text', [
    '12  Quillfeathers Road',               # two spaces: not the stored line, the street pass takes it
    'Quillfeathers Road',                   # the street without its number
    'Address - 12  Quillfeathers Road',
])
@pytest.mark.parametrize('profile', ['record', 'prompt'])
def test_the_patients_street_is_taken_whole(r, text, profile):
    vault = r.vault('k')
    out = (r.record({'t': text}, 1001, vault)[0]['t'] if profile == 'record'
           else r.prompt(text, 1001, vault))
    assert 'quillfeather' not in out.lower() and 'road' not in out.lower() and '12' not in out
    assert ADDR in out or '[Redacted Patient: PT-' in out


@pytest.mark.parametrize('text', ["Marigolds' daughter rang.", "the Quillfeathers' dog",
                                  'Quillfeathers daughter rang'])
def test_the_name_with_an_s_is_still_taken(r, text):
    out = r.prompt(text, 1001, r.vault('k'))
    assert 'marigold' not in out.lower() and 'quillfeather' not in out.lower()


@pytest.mark.parametrize('street', ['Street', 'St', 'Rd', 'Avenue', 'Ave', 'Court', 'Cres'])
def test_a_street_type_leaves_the_name_for_the_street_passes(r, street):
    # Never the 0.2.11 shape: a patient token, then the s and a street type.
    out = r.prompt(f'12 Quillfeathers {street}', 1001, r.vault('k'))
    assert ']s ' + street not in out
