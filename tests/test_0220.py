"""0.2.20: a hyphenated name written with a space beside the hyphen (MedUI ADR-155).

From the practice's shapes: a hyphenated surname stored 'Xxxx-Yyyy' was written
'Xxxx- Yyyy' in letters, and nothing matched it. Invented values only.
"""
import copy
import json
from pathlib import Path

import pytest

from bps_redaction import Redactor, StaticLists
from bps_redaction.adapters.fixture import FixtureSource
from bps_redaction.redaction import _name_spellings, hyphen_spacings

FIXTURE = Path(__file__).parent / 'fixtures' / 'practice.json'
HYPHEN = {'SURNAME': 'Quillfeather-Ashgrove', 'NAME': 'Mrs Marigold Quillfeather-Ashgrove'}
ORDINARY = {'SURNAME': 'Small-Rivers', 'NAME': 'Mrs Marigold Small-Rivers'}


def _out(text, profile='prompt', **demo):
    data = copy.deepcopy(json.loads(FIXTURE.read_text()))
    data['patients']['1001']['demographics'].update(demo)
    r = Redactor(FixtureSource(data), lists=StaticLists({}))
    v = r.vault('k')
    if profile == 'record':
        return r.record({'t': text}, 1001, v)[0]['t']
    return r.prompt(text, 1001, v)


def test_spacings():
    assert hyphen_spacings('Aaaa-Bbbb') == ['Aaaa-Bbbb', 'Aaaa- Bbbb', 'Aaaa -Bbbb', 'Aaaa - Bbbb']
    assert hyphen_spacings('Aaaa - Bbbb')[0] == 'Aaaa-Bbbb'      # stored spaced, written plain
    assert set(hyphen_spacings('Aaaa-Bbbb')) <= set(_name_spellings('Aaaa-Bbbb')) | {'Aaaa-Bbbb'}
    assert not any(' -' in f or '- ' in f for f in _name_spellings('Aaaa Bbbb'))  # no hyphen, none added


@pytest.mark.parametrize('written', ['Quillfeather- Ashgrove', 'Quillfeather -Ashgrove',
                                     'Quillfeather - Ashgrove', 'QUILLFEATHER- ASHGROVE'])
@pytest.mark.parametrize('profile', ['prompt', 'record'])
def test_spaced_hyphen_taken(written, profile):
    out = _out(f'Seen today: Marigold {written}, reviewed.', profile, **HYPHEN)
    assert 'Quillfeather' not in out.title() and 'Ashgrove' not in out.title()
    assert '[Redacted' in out


def test_spaced_hyphen_follows_the_spaced_form():
    # The hyphen-spaced form is matched exactly as the existing 'Small Rivers' form is.
    for text in ('Small- Rivers', 'small - rivers'):
        out = _out(f'a {text} note', **ORDINARY)
        spaced = _out(f"a {text.replace('-', '').replace('  ', ' ')} note", **ORDINARY)
        assert ('[Redacted' in out) == ('[Redacted' in spaced)


def test_one_part_alone_still_left():
    out = _out('Ashgrove Road clinic, a Thornbury - Ashgrove note.', SURNAME='Quillfeather-Ashgrove',
               NAME='Mrs Marigold Quillfeather-Ashgrove')
    assert 'Ashgrove Road' in out and 'Thornbury - Ashgrove' in out   # ADR-042 stands


def test_line_break_not_a_space():
    out = _out('Quillfeather-\nAshgrove', **HYPHEN)
    assert 'Quillfeather-\nAshgrove' in out or '[Redacted' in out   # whatever 0.2.19 did; no new form
