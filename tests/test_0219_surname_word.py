"""0.2.19: one distinctive word of a multi-word surname, beside a name token (MedUI ADR-154).

From the practice's counts: a document that misspells another word of a
three-word surname, or gives one half of a hyphenated one, left the distinctive word in view
beside the patient's redacted first name. Invented values only.
"""
import copy
import json
from pathlib import Path

import pytest

from bps_redaction import Redactor, StaticLists
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.redaction import surname_words

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
SPACED = {'SURNAME': 'van der Thistlewood', 'NAME': 'Mrs Marigold van der Thistlewood'}
HYPHEN = {'SURNAME': 'Quillfeather-Ashgrove', 'NAME': 'Mrs Marigold Quillfeather-Ashgrove'}


def _out(text, profile='prompt', **demo):
    data = copy.deepcopy(json.loads(FIXTURE.read_text()))
    data['patients']['1001']['demographics'].update(demo)
    r = Redactor(FixtureSource(data), lists=StaticLists({}))
    v = r.vault('k')
    if profile == 'record':
        return r.record({'t': text}, 1001, v)[0]['t']
    if profile == 'input':
        return r.input({'t': text}, 1001, v)['t']
    return r.prompt(text, 1001, v)


@pytest.mark.parametrize('surname, expected', [
    ('van der Thistlewood', ['Thistlewood']),
    ('Quillfeather-Ashgrove', ['Quillfeather', 'Ashgrove']),
    ('Quillfeather', []),                 # one word: an identifier whole
    ('Small-Rivers', []),                 # ordinary words
    ('de la Cruz', []),                   # particles, and a part under 5 letters
    ('binti Thistlewood', ['Thistlewood']),
    ('', []),
])
def test_surname_words(surname, expected):
    assert surname_words(surname) == expected


@pytest.mark.parametrize('profile', ['record', 'prompt'])
@pytest.mark.parametrize('text', [
    'Marigold van den Thistlewood attended today.',      # a middle word misspelt
    'RE: MARIGOLD VAN DEN THISTLEWOOD',
    'Patient: Thistlewood, Marigold',
    'Mrs Marigold Thistlewood was seen.',
    "Marigold Thistlewood's bloods",
])
def test_word_beside_the_patients_name_is_taken(text, profile):
    assert 'thistlewood' not in _out(text, profile, **SPACED).lower()


@pytest.mark.parametrize('text', [
    'Marigold Ashgrove attended.',
    'ASHGROVE, Marigold',
    'Marigold Quillfeather attended.',
])
def test_hyphenated_part_beside_the_name_is_taken(text):
    out = _out(text, **HYPHEN).lower()
    assert 'ashgrove' not in out and 'quillfeather' not in out


@pytest.mark.parametrize('text', [
    'Referred to the Thistlewood Clinic for review.',    # no name beside it
    'Lives on Thistlewood Road.',
    'thistlewood pollen allergy; Marigold is well.',       # lower case
    'Marigold was seen. She then went on to visit the Thistlewood Clinic.',   # over 3 words away
    'Seen today.\nThistlewood Clinic\nMarigold',          # another line
])
def test_word_on_its_own_is_left(text):
    assert 'thistlewood' in _out(text, **SPACED).lower()


def test_whole_surname_still_one_token():
    out = _out('Marigold van der Thistlewood attended.', **SPACED)
    assert 'Thistlewood' not in out and 'van' not in out


def test_one_word_surname_unchanged():
    text = 'Marigold Quill attended; Quillfeathery is not her.'
    assert 'Quillfeathery' in _out(text)


def test_deredacts():
    data = copy.deepcopy(json.loads(FIXTURE.read_text()))
    data['patients']['1001']['demographics'].update(SPACED)
    r = Redactor(FixtureSource(data), lists=StaticLists({}))
    v = r.vault('k')
    text = 'Marigold van den Thistlewood attended today.'
    out = r.prompt(text, 1001, v)
    assert r.deredact(out, v.get_reverse_map()).text == text
